import asyncio
import jinja2
import jsonpointer
import kopf
import kubernetes_asyncio
import logging
import pytimeparse

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, List, Mapping, Optional, TypeVar, Union

import poolboy_k8s
import resourceclaim
import resourcepool
import resourceprovider
import resourcewatcher

from poolboy import Poolboy
from poolboy_templating import recursive_process_template_strings, seconds_to_interval

ResourceClaimT = TypeVar('ResourceClaimT', bound='ResourceClaim')
ResourceHandleT = TypeVar('ResourceHandleT', bound='ResourceHandle')
ResourcePoolT = TypeVar('ResourcePoolT', bound='ResourcePool')

class ResourceHandle:
    all_instances = {}
    bound_instances = {}
    unbound_instances = {}
    lock = asyncio.Lock()

    @staticmethod
    def __register_definition(definition: Mapping) -> ResourceHandleT:
        name = definition['metadata']['name']
        resource_handle = ResourceHandle.all_instances.get(name)
        if resource_handle:
            resource_handle.refresh_from_definition(definition=definition)
        else:
            resource_handle = ResourceHandle(
                annotations = definition['metadata'].get('annotations', {}),
                labels = definition['metadata'].get('labels', {}),
                meta = definition['metadata'],
                name = name,
                namespace = Poolboy.namespace,
                spec = definition['spec'],
                status = definition.get('status', {}),
                uid = definition['metadata']['uid'],
            )
        resource_handle.__register()
        return resource_handle

    @staticmethod
    async def bind_handle_to_claim(
        logger: kopf.ObjectLogger,
        resource_claim: ResourceClaimT,
        resource_claim_resources: List[Mapping],
    ) -> Optional[ResourceHandleT]:
        async with ResourceHandle.lock:
            # Check if there is already an assigned claim
            resource_handle = ResourceHandle.bound_instances.get((resource_claim.namespace, resource_claim.name))
            if resource_handle:
                return resource_handle

            matched_resource_handle = None
            matched_resource_handle_diff_count = None
            claim_status_resources = resource_claim.status_resources

            # Loop through unbound instances to find best match
            for resource_handle in ResourceHandle.unbound_instances.values():
                # Honor explicit pool requests
                if resource_claim.resource_pool_name \
                and resource_claim.resource_pool_name != resource_handle.resource_pool_name:
                    continue

                # Do not bind to handles that are near end of lifespan
                if resource_handle.has_lifespan_end \
                and resource_handle.timedelta_to_lifespan_end.total_seconds() < 120:
                    continue

                diff_count = 0
                is_match = True
                handle_resources = resource_handle.resources
                if len(resource_claim_resources) < len(handle_resources):
                    # ResourceClaim cannot match ResourceHandle if there are more
                    # resources in the ResourceHandle than the ResourceClaim
                    continue
                elif len(resource_claim_resources) > len(handle_resources):
                    # Claim that adds resources strongly weighted in favor of normal diff match
                    diff_count += 1000

                for i, handle_resource in enumerate(handle_resources):
                    claim_resource = resource_claim_resources[i]

                    # ResourceProvider must match
                    provider_name = claim_status_resources[i]['provider']['name']
                    if provider_name != handle_resource['provider']['name']:
                        is_match = False
                        break

                    # If handle has a resource name then resource_claim must specify the same name
                    claim_resource_name = claim_resource.get('name')
                    handle_resource_name = handle_resource.get('name')
                    if handle_resource_name:
                        if claim_resource_name != handle_resource_name:
                            is_match = False
                            break
                    elif claim_resource_name:
                        diff_count += 1

                    # Use provider to check if templates match and get list of allowed differences
                    provider = await resourceprovider.ResourceProvider.get(provider_name)
                    diff_patch = provider.check_template_match(
                        handle_resource_template = handle_resource.get('template', {}),
                        claim_resource_template = claim_resource.get('template', {}),
                    )
                    if diff_patch == None:
                        is_match = False
                        break
                    # Match with (possibly empty) difference list
                    diff_count += len(diff_patch)

                if not is_match:
                    continue
                if matched_resource_handle != None:
                    if matched_resource_handle_diff_count < diff_count:
                        continue
                    if matched_resource_handle_diff_count == diff_count \
                    and matched_resource_handle.creation_timestamp < resource_handle.creation_timestamp:
                        continue

                matched_resource_handle = resource_handle
                matched_resource_handle_diff_count = diff_count

            if not matched_resource_handle:
                return None

            patch = [
                {
                    "op": "add",
                    "path": "/spec/resourceClaim",
                    "value": {
                        "apiVersion": Poolboy.operator_api_version,
                        "kind": "ResourceClaim",
                        "name": resource_claim.name,
                        "namespace": resource_claim.namespace,
                    }
                }
            ]

            # Add any additional resources to handle
            for resource_index in range(len(matched_resource_handle.resources), len(resource_claim_resources)):
                patch.append({
                    "op": "add",
                    "path": f"/spec/resources/{resource_index}",
                    "value": {
                        "provider": resource_claim_resources[resource_index]['provider'],
                    },
                })

            # Set lifespan end from default on claim bind
            lifespan_default = matched_resource_handle.get_lifespan_default(resource_claim)
            if lifespan_default:
                patch.append({
                    "op": "add",
                    "path": "/spec/lifespan/end",
                    "value": (
                        datetime.utcnow() + matched_resource_handle.get_lifespan_default_timedelta(resource_claim)
                    ).strftime('%FT%TZ'),
                })

            definition = await Poolboy.custom_objects_api.patch_namespaced_custom_object(
                group = Poolboy.operator_domain,
                name = matched_resource_handle.name,
                namespace = matched_resource_handle.namespace,
                plural = 'resourcehandles',
                version = Poolboy.operator_version,
                _content_type = 'application/json-patch+json',
                body = patch,
            )
            matched_resource_handle = ResourceHandle.__register_definition(definition=definition)
            logger.info(f"Bound {matched_resource_handle} to {resource_claim}")

        if matched_resource_handle.is_from_resource_pool:
            resource_pool = await resourcepool.ResourcePool.get(matched_resource_handle.resource_pool_name)
            if resource_pool:
                await resource_pool.manage(logger=logger)
            else:
                logger.warning(
                    f"Unable to find ResourcePool {matched_resource_handle.resource_pool_name} for "
                    f"{matched_resource_handle} claimed by {resource_claim}"
                )

        return matched_resource_handle

    @staticmethod
    async def create_for_claim(
        logger: kopf.ObjectLogger,
        resource_claim: ResourceClaimT,
        resource_claim_resources: List[Mapping],
    ):
        resource_providers = await resource_claim.get_resource_providers(resource_claim_resources)
        vars_ = {}
        resources = []
        lifespan_default_timedelta = None
        lifespan_maximum = None
        lifespan_maximum_timedelta = None
        lifespan_relative_maximum = None
        lifespan_relative_maximum_timedelta = None
        for i, claim_resource in enumerate(resource_claim_resources):
            provider = resource_providers[i]
            vars_.update(provider.vars)

            provider_lifespan_default_timedelta = provider.get_lifespan_default_timedelta(resource_claim)
            if provider_lifespan_default_timedelta:
                if not lifespan_default_timedelta \
                or provider_lifespan_default_timedelta < lifespan_default_timedelta:
                    lifespan_default_timedelta = provider_lifespan_default_timedelta

            provider_lifespan_maximum_timedelta = provider.get_lifespan_maximum_timedelta(resource_claim)
            if provider_lifespan_maximum_timedelta:
                if not lifespan_maximum_timedelta \
                or provider_lifespan_maximum_timedelta < lifespan_maximum_timedelta:
                    lifespan_maximum = provider.lifespan_maximum
                    lifespan_maximum_timedelta = provider_lifespan_maximum_timedelta

            provider_lifespan_relative_maximum_timedelta = provider.get_lifespan_relative_maximum_timedelta(resource_claim)
            if provider_lifespan_relative_maximum_timedelta:
                if not lifespan_relative_maximum_timedelta \
                or provider_lifespan_relative_maximum_timedelta < lifespan_relative_maximum_timedelta:
                    lifespan_relative_maximum = provider.lifespan_relative_maximum
                    lifespan_relative_maximum_timedelta = provider_lifespan_relative_maximum_timedelta

            resources_item = {"provider": provider.as_reference()}
            if 'name' in claim_resource:
                resources_item['name'] = claim_resource['name']
            if 'template' in claim_resource:
                resources_item['template'] = claim_resource['template']
            resources.append(resources_item)

        definition = {
            'apiVersion': Poolboy.operator_api_version,
            'kind': 'ResourceHandle',
            'metadata': {
                'finalizers': [ Poolboy.operator_domain ],
                'generateName': 'guid-',
                'labels': {
                    f"{Poolboy.operator_domain}/resource-claim-name": resource_claim.name,
                    f"{Poolboy.operator_domain}/resource-claim-namespace": resource_claim.namespace,
                }
            },
            'spec': {
                'resourceClaim': {
                    'apiVersion': Poolboy.operator_api_version,
                    'kind': 'ResourceClaim',
                    'name': resource_claim.name,
                    'namespace': resource_claim.namespace
                },
                'resources': resources,
                'vars': vars_,
            }
        }


        lifespan_end_datetime = None
        lifespan_start_datetime = datetime.utcnow()
        requested_lifespan_end_datetime = resource_claim.requested_lifespan_end_datetime
        if requested_lifespan_end_datetime:
            lifespan_end_datetime = requested_lifespan_end_datetime
        elif lifespan_default_timedelta:
            lifespan_end_datetime = lifespan_start_datetime + lifespan_default_timedelta
        elif lifespan_relative_maximum_timedelta:
            lifespan_end_datetime = lifespan_start_datetime + lifespan_relative_maximum_timedelta
        elif lifespan_maximum_timedelta:
            lifespan_end_datetime = lifespan_start_datetime + lifespan_maximum_timedelta

        if lifespan_end_datetime:
            if lifespan_relative_maximum_timedelta \
            and lifespan_end_datetime > lifespan_start_datetime + lifespan_relative_maximum_timedelta:
                logger.warning(
                    f"Requested lifespan end {resource_claim.requested_lifespan_end_timestamp} "
                    f"for ResourceClaim {resource_claim.name} in {resource_claim.namespace} "
                    f"exceeds relativeMaximum for ResourceProviders"
                )
                lifespan_end = lifespan_start_datetime + lifespan_relative_maximum_timedelta
            if lifespan_maximum_timedelta \
            and lifespan_end_datetime > lifespan_start_datetime + lifespan_maximum_timedelta:
                logger.warning(
                    f"Requested lifespan end {resource_claim.requested_lifespan_end_timestamp} "
                    f"for ResourceClaim {resource_claim.name} in {resource_claim.namespace} "
                    f"exceeds maximum for ResourceProviders"
                )
                lifespan_end = lifespan_start_datetime + lifespan_maximum_timedelta

        if lifespan_end_datetime \
        or lifespan_maximum_timedelta \
        or lifespan_relative_maximum_timedelta:
            definition['spec']['lifespan'] = {}

        if lifespan_end_datetime:
            definition['spec']['lifespan']['end'] = lifespan_end_datetime.strftime('%FT%TZ')

        if lifespan_maximum:
            definition['spec']['lifespan']['maximum'] = lifespan_maximum

        if lifespan_relative_maximum:
            definition['spec']['lifespan']['relativeMaximum'] = lifespan_relative_maximum

        definition = await Poolboy.custom_objects_api.create_namespaced_custom_object(
            body = definition,
            group = Poolboy.operator_domain,
            namespace = Poolboy.namespace,
            plural = 'resourcehandles',
            version = Poolboy.operator_version,
        )
        resource_handle = await ResourceHandle.register_definition(definition=definition)
        logger.info(
            f"Created ResourceHandle {resource_handle.name} for "
            f"ResourceClaim {resource_claim.name} in {resource_claim.namespace}"
        )
        return resource_handle

    @staticmethod
    async def create_for_pool(
        logger: kopf.ObjectLogger,
        resource_pool: ResourcePoolT,
    ):
        definition = {
            "apiVersion": Poolboy.operator_api_version,
            "kind": "ResourceHandle",
            "metadata": {
                "generateName": "guid-",
                "labels": {
                    f"{Poolboy.operator_domain}/resource-pool-name": resource_pool.name,
                    f"{Poolboy.operator_domain}/resource-pool-namespace": resource_pool.namespace,
                },
            },
            "spec": {
                "resourcePool": resource_pool.ref,
                "resources": resource_pool.resources,
                "vars": resource_pool.vars,
            }
        }
        if resource_pool.has_lifespan:
            definition['spec']['lifespan'] = {}
            if resource_pool.lifespan_default:
                definition['spec']['lifespan']['default'] = resource_pool.lifespan_default
            if resource_pool.lifespan_maximum:
                definition['spec']['lifespan']['maximum'] = resource_pool.lifespan_maximum
            if resource_pool.lifespan_relative_maximum:
                definition['spec']['lifespan']['relativeMaximum'] = resource_pool.lifespan_relative_maximum
            if resource_pool.lifespan_unclaimed:
                definition['spec']['lifespan']['end'] = (
                    datetime.utcnow() + resource_pool.lifespan_unclaimed_timedelta
                ).strftime("%FT%TZ")

        definition = await Poolboy.custom_objects_api.create_namespaced_custom_object(
            body = definition,
            group = Poolboy.operator_domain,
            namespace = Poolboy.namespace,
            plural = "resourcehandles",
            version = Poolboy.operator_version,
        )
        resource_handle = await ResourceHandle.register_definition(definition=definition)
        logger.info(f"Created ResourceHandle {resource_handle.name} for ResourcePool {resource_pool.name}")
        return resource_handle

    @staticmethod
    async def delete_unbound_handles_for_pool(
        logger: kopf.ObjectLogger,
        resource_pool: ResourcePoolT,
    ) -> List[ResourceHandleT]:
        async with ResourceHandle.lock:
            resource_handles = []
            for resource_handle in list(ResourceHandle.unbound_instances.values()):
                if resource_handle.resource_pool_name == resource_pool.name \
                and resource_handle.resource_pool_namespace == resource_pool.namespace:
                    logger.info(
                        f"Deleting unbound ResourceHandle {resource_handle.name} "
                        f"for ResourcePool {resource_pool.name}"
                    )
                    resource_handle.__unregister()
                    await resource_handle.delete()
            return resource_handles

    @staticmethod
    async def get(name: str) -> Optional[ResourceHandleT]:
        async with ResourceHandle.lock:
            resource_handle = ResourceHandle.all_instances.get(name)
            if resource_handle:
                return resource_handle
            definition = await Poolboy.custom_objects_api.get_namespaced_custom_object(
                Poolboy.operator_domain, Poolboy.operator_version, Poolboy.namespace, 'resourcehandles', name
            )

            return ResourceHandle.__register_definition(definition=definition)

    @staticmethod
    def get_from_cache(name: str) -> Optional[ResourceHandleT]:
        return ResourceHandle.all_instances.get(name)

    @staticmethod
    async def get_unbound_handles_for_pool(resource_pool: ResourcePoolT, logger) -> List[ResourceHandleT]:
        async with ResourceHandle.lock:
            resource_handles = []
            for resource_handle in ResourceHandle.unbound_instances.values():
                if resource_handle.resource_pool_name == resource_pool.name \
                and resource_handle.resource_pool_namespace == resource_pool.namespace:
                    resource_handles.append(resource_handle)
            return resource_handles

    @staticmethod
    async def preload(logger: kopf.ObjectLogger) -> None:
        async with ResourceHandle.lock:
            _continue = None
            while True:
                resource_handle_list = await Poolboy.custom_objects_api.list_namespaced_custom_object(
                    Poolboy.operator_domain, Poolboy.operator_version, Poolboy.namespace, 'resourcehandles',
                    _continue = _continue,
                    limit = 50,
                )
                for definition in resource_handle_list['items']:
                    ResourceHandle.__register_definition(definition=definition)
                _continue = resource_handle_list['metadata'].get('continue')
                if not _continue:
                    break

    @staticmethod
    async def register(
        annotations: kopf.Annotations,
        labels: kopf.Labels,
        meta: kopf.Meta,
        name: str,
        namespace: str,
        spec: kopf.Spec,
        status: kopf.Status,
        uid: str,
    ) -> ResourceHandleT:
        async with ResourceHandle.lock:
            resource_handle = ResourceHandle.all_instances.get(name)
            if resource_handle:
                resource_handle.refresh(
                    annotations = annotations,
                    labels = labels,
                    meta = meta,
                    spec = spec,
                    status = status,
                    uid = uid,
                )
            else:
                resource_handle = ResourceHandle(
                    annotations = annotations,
                    labels = labels,
                    meta = meta,
                    name = name,
                    namespace = namespace,
                    spec = spec,
                    status = status,
                    uid = uid,
                )
            resource_handle.__register()
            return resource_handle

    @staticmethod
    async def register_definition(definition: Mapping) -> ResourceHandleT:
        async with ResourceHandle.lock:
            return ResourceHandle.__register_definition(definition)

    @staticmethod
    async def unregister(name: str) -> Optional[ResourceHandleT]:
        async with ResourceHandle.lock:
            resource_handle = ResourceHandle.all_instances.pop(name, None)
            if resource_handle:
                resource_handle.__unregister()
                return resource_handle

    def __init__(self,
        annotations: kopf.Annotations,
        labels: kopf.Labels,
        meta: kopf.Meta,
        name: str,
        namespace: str,
        spec: kopf.Spec,
        status: kopf.Status,
        uid: str,
    ):
        self.annotations = annotations
        self.labels = labels
        self.lock = asyncio.Lock()
        self.meta = meta
        self.name = name
        self.namespace = namespace
        self.spec = spec
        self.status = status
        self.uid = uid
        self.resource_states = []
        self.resource_refresh_datetime = []

    def __str__(self) -> str:
        return f"ResourceHandle {self.name}"

    def __register(self) -> None:
        """
        Add ResourceHandle to register of bound or unbound instances.
        This method must be called with the ResourceHandle.lock held.
        """
        ResourceHandle.all_instances[self.name] = self
        if self.is_bound:
            ResourceHandle.bound_instances[(
                self.resource_claim_namespace,
                self.resource_claim_name
            )] = self
            ResourceHandle.unbound_instances.pop(self.name, None)
        elif not self.is_deleting:
            ResourceHandle.unbound_instances[self.name] = self

    def __unregister(self) -> None:
        ResourceHandle.all_instances.pop(self.name, None)
        ResourceHandle.unbound_instances.pop(self.name, None)
        if self.is_bound:
            ResourceHandle.bound_instances.pop(
                (self.resource_claim_namespace, self.resource_claim_name),
                None,
            )

    @property
    def creation_datetime(self):
        return datetime.strptime(self.creation_timestamp, "%Y-%m-%dT%H:%M:%SZ")

    @property
    def creation_timestamp(self) -> str:
        return self.meta['creationTimestamp']

    @property
    def deletion_timestamp(self) -> str:
        return self.meta.get('deletionTimestamp')

    @property
    def guid(self) -> str:
        name = self.name
        generate_name = self.meta.get('generateName')
        if generate_name and name.startswith(generate_name):
            return name[len(generate_name):]
        elif name.startswith('guid-'):
            return name[5:]
        else:
            return name[-5:]

    @property
    def has_lifespan_end(self) -> bool:
        'end' in self.spec.get('lifespan', {})

    @property
    def ignore(self) -> bool:
        return Poolboy.ignore_label in self.labels

    @property
    def is_bound(self) -> bool:
        return 'resourceClaim' in self.spec

    @property
    def is_deleting(self) -> bool:
        return True if self.deletion_timestamp else False

    @property
    def is_from_resource_pool(self) -> bool:
        return 'resourcePool' in self.spec

    @property
    def is_past_lifespan_end(self) -> bool:
        dt = self.lifespan_end_datetime
        if not dt:
            return False
        return dt < datetime.utcnow()

    @property
    def lifespan_end_datetime(self) -> Any:
        timestamp = self.lifespan_end_timestamp
        if timestamp:
            return datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ')

    @property
    def lifespan_end_timestamp(self) -> Optional[str]:
        lifespan = self.spec.get('lifespan')
        if lifespan:
            return lifespan.get('end')

    @property
    def metadata(self) -> Mapping:
        return self.meta

    @property
    def reference(self) -> Mapping:
        return {
            "apiVersion": Poolboy.operator_api_version,
            "kind": "ResourceHandle",
            "name": self.name,
            "namespace": self.namespace,
        }

    @property
    def resource_claim_name(self) -> Optional[str]:
        return self.spec.get('resourceClaim', {}).get('name')

    @property
    def resource_claim_namespace(self) -> Optional[str]:
        return self.spec.get('resourceClaim', {}).get('namespace')

    @property
    def resource_pool_name(self) -> Optional[str]:
        if 'resourcePool' in self.spec:
            return self.spec['resourcePool']['name']

    @property
    def resource_pool_namespace(self) -> Optional[str]:
        if 'resourcePool' in self.spec:
            return self.spec['resourcePool'].get('namespace', Poolboy.namespace)

    @property
    def resources(self) -> List[Mapping]:
        return self.spec.get('resources', [])

    @property
    def vars(self) -> Mapping:
        return self.spec.get('vars', {})

    @property
    def timedelta_to_lifespan_end(self) -> Any:
        dt = self.lifespan_end_datetime
        if dt:
            return dt - datetime.utcnow()

    def __lifespan_value(self, name, resource_claim):
        value = self.spec.get('lifespan', {}).get(name)
        if not value:
            return

        value = recursive_process_template_strings(
            template = value,
            variables = {
                **self.vars,
                "resource_claim": resource_claim,
                "resource_handle": self,
            },
        )

        return value

    def __lifespan_value_as_timedelta(self, name, resource_claim):
        value = self.__lifespan_value(name, resource_claim)
        if not value:
            return

        seconds = pytimeparse.parse(value)
        if seconds == None:
            raise kopf.TemporaryError(f"Failed to parse {name} time interval: {value}", delay=60)

        return timedelta(seconds=seconds)

    def get_lifespan_default(self, resource_claim=None):
        return self.__lifespan_value('default', resource_claim=resource_claim)

    def get_lifespan_default_timedelta(self, resource_claim=None):
        return self.__lifespan_value_as_timedelta('default', resource_claim=resource_claim)

    def get_lifespan_maximum(self, resource_claim=None):
        return self.__lifespan_value('maximum', resource_claim=resource_claim)

    def get_lifespan_maximum_timedelta(self, resource_claim=None):
        return self.__lifespan_value_as_timedelta('maximum', resource_claim=resource_claim)

    def get_lifespan_relative_maximum(self, resource_claim=None):
        return self.__lifespan_value('relativeMaximum', resource_claim=resource_claim)

    def get_lifespan_relative_maximum_timedelta(self, resource_claim=None):
        return self.__lifespan_value_as_timedelta('relativeMaximum', resource_claim=resource_claim)

    def get_lifespan_end_maximum_datetime(self, resource_claim=None):
        lifespan_start_datetime = resource_claim.lifespan_start_datetime if resource_claim else self.creation_datetime
        maximum_timedelta = self.get_lifespan_maximum_timedelta(resource_claim=resource_claim)
        maximum_end = lifespan_start_datetime + maximum_timedelta if maximum_timedelta else None
        relative_maximum_timedelta = self.get_lifespan_relative_maximum_timedelta(resource_claim=resource_claim)
        relative_maximum_end = datetime.utcnow() + relative_maximum_timedelta if relative_maximum_timedelta else None

        if relative_maximum_end \
        and (not maximum_end or relative_maximum_end < maximum_end):
            return relative_maximum_end
        else:
            return maximum_end

    def refresh(self,
        annotations: kopf.Annotations,
        labels: kopf.Labels,
        meta: kopf.Meta,
        spec: kopf.Spec,
        status: kopf.Status,
        uid: str,
    ) -> None:
        self.annotations = annotations
        self.labels = labels
        self.meta = meta
        self.spec = spec
        self.status = status
        self.uid = uid

    def refresh_from_definition(self, definition: Mapping) -> None:
        self.annotations = definition['metadata'].get('annotations', {})
        self.labels = definition['metadata'].get('labels', {})
        self.meta = definition['metadata']
        self.spec = definition['spec']
        self.status = definition.get('status', {})
        self.uid = definition['metadata']['uid']

    async def delete(self):
        try:
            await Poolboy.custom_objects_api.delete_namespaced_custom_object(
                group = Poolboy.operator_domain,
                name = self.name,
                namespace = self.namespace,
                plural = 'resourcehandles',
                version = Poolboy.operator_version,
            )
        except kubernetes_asyncio.client.exceptions.ApiException as e:
            if e.status != 404:
                raise

    async def get_resource_claim(self) -> Optional[ResourceClaimT]:
        if not self.is_bound:
            return None
        return await resourceclaim.ResourceClaim.get(
            name = self.resource_claim_name,
            namespace = self.resource_claim_namespace,
        )

    async def get_resource_pool(self) -> Optional[ResourcePoolT]:
        if not self.is_from_resource_pool:
            return None
        return await resourcepool.ResourcePool.get(self.resource_pool_name)

    async def get_resource_providers(self):
        resource_providers = []
        for resource in self.spec.get('resources', []):
            resource_providers.append(
                await resourceprovider.ResourceProvider.get(resource['provider']['name'])
            )
        return resource_providers

    async def get_resource_states(self, logger: kopf.ObjectLogger) -> List[Mapping]:
        for i, resource in enumerate(self.spec['resources']):
            if i >= len(self.resource_states):
                self.resource_states.append(None)
                self.resource_refresh_datetime.append(None)
            elif (
                self.resource_states[i] and
                self.resource_refresh_datetime[i] and
                (datetime.utcnow() - self.resource_refresh_datetime[i]).total_seconds() > Poolboy.resource_refresh_interval
            ):
                continue

            reference = resource.get('reference')
            if not reference:
                continue

            api_version = reference['apiVersion']
            kind = reference['kind']
            name = reference['name']
            namespace = reference.get('namespace')
            try:
                self.resource_states[i] = await poolboy_k8s.get_object(
                    api_version = api_version,
                    kind = kind,
                    name = name,
                    namespace = namespace,
                )
                self.resource_refresh_datetime[i] = datetime.utcnow()
            except kubernetes_asyncio.client.exceptions.ApiException as e:
                if e.status == 404:
                    _name = f"{name} in {namespace}" if namespace else name
                    logger.warning(f"Mangaged resource {api_version} {kind} {_name} not found.")
                else:
                    raise

        return self.resource_states

    async def handle_delete(self, logger: kopf.ObjectLogger) -> None:
        for resource in self.spec.get('resources', []):
            reference = resource.get('reference')
            if reference:
                try:
                    resource_description = f"{reference['apiVersion']} {reference['kind']} " + (
                        f"{reference['name']} in {reference['namespace']}"
                        if 'namespace' in reference else reference['name']
                    )
                    logger.info(f"Propagating delete of {self} to {resource_description}")
                    await poolboy_k8s.delete_object(
                        api_version = reference['apiVersion'],
                        kind = reference['kind'],
                        name = reference['name'],
                        namespace = reference.get('namespace'),
                    )
                except kubernetes_asyncio.client.exceptions.ApiException as e:
                    if e.status != 404:
                        raise

        if self.is_bound:
            try:
                resource_claim = await self.get_resource_claim()
                if not resource_claim.is_detached:
                    await resource_claim.delete()
                    logger.info(f"Propagated delete of ResourceHandle {self.name} to ResourceClaim {self.resource_claim_name} in {self.resource_claim_namespace}")
            except kubernetes_asyncio.client.exceptions.ApiException as e:
                if e.status != 404:
                    raise

        if self.is_from_resource_pool:
            resource_pool = await resourcepool.ResourcePool.get(self.resource_pool_name)
            if resource_pool:
                await resource_pool.manage(logger=logger)

    async def handle_resource_event(self,
        logger: Union[logging.Logger, logging.LoggerAdapter],
        resource_index: int,
        resource_state: Mapping,
    ) -> None:
        async with self.lock:
            # Extend resource_states as needed
            if resource_index >= len(self.resource_states):
                self.resource_states.extend(
                    [None] * (1 + resource_index - len(self.resource_states))
                )
            if resource_index >= len(self.resource_refresh_datetime):
                self.resource_refresh_datetime.extend(
                    [None] * (1 + resource_index - len(self.resource_refresh_datetime))
                )
            self.resource_states[resource_index] = resource_state
            self.resource_refresh_datetime[resource_index] = datetime.utcnow()

    async def manage(self, logger: kopf.ObjectLogger) -> None:
        async with self.lock:
            resource_claim = None
            if self.is_bound:
                try:
                    resource_claim = await self.get_resource_claim()
                    await resource_claim.update_status_from_handle(logger=logger, resource_handle=self)
                except kubernetes_asyncio.client.exceptions.ApiException as e:
                    if e.status == 404:
                        logger.info(
                            f"Propagating deletion of ResourceClaim "
                            f"{self.resource_claim_name} in {self.resource_claim_namespace} "
                            f"to ResourceHandle {self.name}"
                        )
                        await self.delete()
                    else:
                        raise

            if self.is_past_lifespan_end:
                logger.info(f"Deleting {self} at end of lifespan ({self.lifespan_end_timestamp})")
                await self.delete()
                return

            resource_providers = await self.get_resource_providers()
            resource_states = await self.get_resource_states(logger=logger)
            patch = []
            resources_to_create = []

            for resource_index, resource in enumerate(self.spec['resources']):
                resource_provider = resource_providers[resource_index]
                resource_state = resource_states[resource_index]

                if resource_provider.resource_requires_claim and not resource_claim:
                    continue

                vars_ = deepcopy(self.vars)
                wait_for_linked_provider = False
                for linked_provider in resource_provider.linked_resource_providers:
                    linked_resource_provider = None
                    linked_resource_state = None
                    for pn, pv in enumerate(resource_providers):
                        if pv.name == linked_provider.name:
                            linked_resource_provider = pv
                            linked_resource_state = resource_states[pn]
                            break
                    else:
                        raise kopf.TemporaryError(
                            f"{self} uses {resource_provider} which has "
                            f"linked ResourceProvider {resource_provider.name} but no resource in this "
                            f"ResourceHandle use this provider.",
                            delay=600
                        )

                    if not linked_provider.check_wait_for(
                        linked_resource_provider = linked_resource_provider,
                        linked_resource_state = linked_resource_state,
                        resource_claim = resource_claim,
                        resource_handle = self,
                        resource_provider = resource_provider,
                        resource_state = resource_state,
                    ):
                        wait_for_linked_provider = True
                        break

                    if linked_resource_state:
                        for template_var in linked_provider.template_vars:
                            vars_[template_var.name] = jsonpointer.resolve_pointer(
                                linked_resource_state, template_var.value_from,
                                default = jinja2.ChainableUndefined()
                            )

                if wait_for_linked_provider:
                    continue

                resource_definition = await resource_provider.resource_definition_from_template(
                    logger = logger,
                    resource_claim = resource_claim,
                    resource_handle = self,
                    resource_index = resource_index,
                    resource_states = resource_states,
                    vars_ = vars_,
                )
                if not resource_definition:
                    continue

                resource_api_version = resource_definition['apiVersion']
                resource_kind = resource_definition['kind']
                resource_name = resource_definition['metadata']['name']
                resource_namespace = resource_definition['metadata'].get('namespace', None)

                reference = {
                    'apiVersion': resource_api_version,
                    'kind': resource_kind,
                    'name': resource_name
                }
                if resource_namespace:
                    reference['namespace'] = resource_namespace

                if 'reference' not in resource:
                    patch.append({
                        "op": "add",
                        "path": f"/spec/resources/{resource_index}/reference",
                        "value": reference,
                    })
                    try:
                        resource_states[resource_index] = resource_state = await poolboy_k8s.get_object(
                            api_version = resource_api_version,
                            kind = resource_kind,
                            name = resource_name,
                            namespace = resource_namespace,
                        )
                    except kubernetes_asyncio.client.exceptions.ApiException as e:
                        if e.status != 404:
                            raise
                elif resource_api_version != resource['reference']['apiVersion']:
                    raise kopf.TemporaryError(
                        f"ResourceHandle {self.name} would change from apiVersion "
                        f"{resource['reference']['apiVersion']} to {resource_api_version}!",
                        delay=600
                    )
                elif resource_kind != resource['reference']['kind']:
                    raise kopf.TemporaryError(
                        f"ResourceHandle {self.name} would change from kind "
                        f"{resource['reference']['kind']} to {resource_kind}!",
                        delay=600
                    )
                else:
                    # Maintain name and namespace
                    if resource_name != resource['reference']['name']:
                        resource_name = resource['reference']['name']
                        resource_definition['metadata']['name'] = resource_name
                    if resource_namespace != resource['reference']['namespace']:
                        resource_namespace = resource['reference']['namespace']
                        resource_definition['metadata']['namespace'] = resource_namespace

                resource_description = f"{resource_api_version} {resource_kind} {resource_name}"
                if resource_namespace:
                    resource_description += f" in {resource_namespace}"

                await resourcewatcher.ResourceWatcher.start_resource_watch(
                    api_version = resource_api_version,
                    kind = resource_kind,
                    namespace = resource_namespace,
                )

                if resource_state:
                    changes = await resource_provider.update_resource(
                        logger = logger,
                        resource_definition = resource_definition,
                        resource_handle = self,
                        resource_state = resource_state,
                    )
                    if changes:
                        logger.info(f"Updated {resource_description} for ResourceHandle {self.name}")
                else:
                    resources_to_create.append(resource_definition)

            if patch:
                definition = await Poolboy.custom_objects_api.patch_namespaced_custom_object(
                    group = Poolboy.operator_domain,
                    name = self.name,
                    namespace = self.namespace,
                    plural = 'resourcehandles',
                    version = Poolboy.operator_version,
                    _content_type = 'application/json-patch+json',
                    body = patch,
                )
                self.refresh_from_definition(definition)

            for resource_definition in resources_to_create:
                changes = await poolboy_k8s.create_object(resource_definition)
                if changes:
                    logger.info(f"Created {resource_description} for ResourceHandle {self.name}")

    async def refetch(self) -> Optional[ResourceHandleT]:
        try:
            definition = await Poolboy.custom_objects_api.get_namespaced_custom_object(
                Poolboy.operator_domain, Poolboy.operator_version, Poolboy.namespace, 'resourcehandles', self.name
            )
            self.refresh_from_definition(definition)
            return self
        except kubernetes_asyncio.client.exceptions.ApiException as e:
            if e.status == 404:
                ResourceHandle.unregister(name=self.name)
                return None
            else:
                raise
