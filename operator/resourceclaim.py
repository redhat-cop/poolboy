import asyncio
import jsonschema
import kopf
import kubernetes_asyncio

from copy import deepcopy
from datetime import datetime
from typing import Mapping, Optional, TypeVar, Union

from config import custom_objects_api, operator_domain, operator_api_version, operator_version
from deep_merge import deep_merge
from jsonpatch_from_diff import jsonpatch_from_diff

import resourcehandle
import resourcepool
import resourceprovider

ResourceClaimT = TypeVar('ResourceClaimT', bound='ResourceClaim')
ResourceHandleT = TypeVar('ResourceHandleT', bound='ResourceHandle')

class ResourceClaim:
    instances = {}
    lock = asyncio.Lock()

    @staticmethod
    def __register_definition(definition: Mapping) -> ResourceClaimT:
        name = definition['metadata']['name']
        namespace = definition['metadata']['namespace']
        resource_claim = ResourceClaim.instances.get((namespace, name))
        if resource_claim:
            resource_claim.refresh_from_definition(definition=definition)
        else:
            resource_claim = ResourceClaim(
                annotations = definition['metadata'].get('annotations', {}),
                labels = definition['metadata'].get('labels', {}),
                meta = definition['metadata'],
                name = name,
                namespace = namespace,
                spec = definition['spec'],
                status = definition.get('status', {}),
                uid = definition['metadata']['uid'],
            )
            ResourceClaim.instances[(namespace, name)] = resource_claim
        return resource_claim

    @staticmethod
    async def get(name: str, namespace: str) -> ResourceClaimT:
        async with ResourceClaim.lock:
            resource_claim = ResourceClaim.instances.get((namespace, name))
            if resource_claim:
                return resource_claim
            definition = await custom_objects_api.get_namespaced_custom_object(
                operator_domain, operator_version, namespace, 'resourceclaims', name
            )
            return ResourceClaim.__register_definition(definition=definition)

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
    ) -> ResourceClaimT:
        async with ResourceClaim.lock:
            resource_claim = ResourceClaim.instances.get((namespace, name))
            if resource_claim:
                resource_claim.refresh(
                    annotations = annotations,
                    labels = labels,
                    meta = meta,
                    spec = spec,
                    status = status,
                    uid = uid,
                )
            else:
                resource_claim = ResourceClaim(
                    annotations = annotations,
                    labels = labels,
                    meta = meta,
                    name = name,
                    namespace = namespace,
                    spec = spec,
                    status = status,
                    uid = uid,
                )
                ResourceClaim.instances[(namespace, name)] = resource_claim
            return resource_claim

    @staticmethod
    async def register_definition(
        definition: Mapping,
    ) -> ResourceClaimT:
        async with ResourceClaim.lock:
            return ResourceClaim.__register_definition(definition=definition)

    @staticmethod
    async def unregister(name: str, namespace: str) -> Optional[ResourceClaimT]:
        async with ResourceClaim.lock:
            return ResourceClaim.instances.pop((namespace, name), None)

    def __init__(self,
        annotations: Union[kopf.Annotations, Mapping],
        labels: Union[kopf.Labels, Mapping],
        meta: Union[kopf.Meta, Mapping],
        name: str,
        namespace: str,
        spec: Union[kopf.Spec, Mapping],
        status: Union[kopf.Status, Mapping],
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

    @property
    def claim_is_initialized(self) -> bool:
        return f"{operator_domain}/resource-claim-init-timestamp" in self.annotations

    @property
    def creation_datetime(self):
        return datetime.strptime(self.creation_timestamp, "%Y-%m-%dT%H:%H:%SZ")

    @property
    def creation_timestamp(self) -> str:
        return self.meta['creationTimestamp']

    @property
    def description(self) -> str:
        return f"ResourceClaim {self.name} in {self.namespace}"

    @property
    def has_resource_handle(self) -> bool:
        return self.status and 'resourceHandle' in self.status

    @property
    def have_resource_providers(self):
        if not self.status \
        or not 'resources' in self.status \
        or len(self.spec.get('resources', [])) > len(self.status.get('resources', [])):
            return False
        for resource in self.status.get('resources', []):
            if not 'provider' in resource:
                return False
        return True

    @property
    def lifespan_end_datetime(self):
        timestamp = self.lifespan_end_timestamp
        if timestamp:
            return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")

    @property
    def lifespan_end_timestamp(self) -> Optional[str]:
        lifespan = self.status.get('lifespan')
        if lifespan:
            return lifespan.get('end')

    @property
    def lifespan_maximum(self) -> Optional[str]:
        lifespan = self.status.get('lifespan')
        if lifespan:
            return lifespan.get('maximum')

    @property
    def lifespan_relative_maximum(self) -> Optional[str]:
        lifespan = self.status.get('lifespan')
        if lifespan:
            return lifespan.get('relativeMaximum')

    @property
    def lifespan_start_datetime(self):
        timestamp = self.lifespan_start_timestamp
        if timestamp:
            return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")

    @property
    def lifespan_start_timestamp(self) -> Optional[str]:
        lifespan = self.status.get('lifespan')
        if lifespan:
            return lifespan.get('start')
        return self.creation_timestamp

    @property
    def metadata(self) -> Mapping:
        return self.meta

    @property
    def requested_lifespan_end_datetime(self):
        timestamp = self.requested_lifespan_end_timestamp
        if timestamp:
            return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")

    @property
    def requested_lifespan_end_timestamp(self) -> Optional[str]:
        lifespan = self.spec.get('lifespan')
        if lifespan:
            return lifespan.get('end')

    @property
    def requested_lifespan_start_datetime(self):
        timestamp = self.requested_lifespan_start_timestamp
        if timestamp:
            return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")

    @property
    def requested_lifespan_start_timestamp(self) -> Optional[str]:
        lifespan = self.spec.get('lifespan')
        if lifespan:
            return lifespan.get('start')

    @property
    def resource_handle_description(self) -> str:
        return f"ResourceHandle {self.resource_handle_name}"

    @property
    def resource_handle_name(self):
        if not self.status:
            return None
        return self.status.get('resourceHandle', {}).get('name')

    @property
    def resource_handle_namespace(self):
        if not self.status:
            return None
        return self.status.get('resourceHandle', {}).get('namespace')

    @property
    def resource_pool_name(self):
        if not self.annotations:
            return None
        return self.annotations.get(f"{operator_domain}/resource-pool-name")

    @property
    def resources(self):
        return self.spec.get('resources', [])

    @property
    def status_resources(self):
        if self.status:
            return self.status.get('resources', [])
        return []

    async def bind_resource_handle(self, logger):
        resource_handle = await resourcehandle.ResourceHandle.bind_handle_to_claim(
            logger = logger,
            resource_claim = self,
        )

        if not resource_handle:
            for provider in await self.get_resource_providers():
                if provider.create_disabled:
                    raise kopf.TemporaryError(
                        f"Found no matching ResourceHandles for ResourceClaim "
                        f"{self.name} in {self.namespace} and ResourceHandle "
                        f"creation is disabled for ResourceProvider {provider.name}",
                        delay=600
                    )
            resource_handle = await resourcehandle.ResourceHandle.create_for_claim(
                logger=logger,
                resource_claim=self,
            )

        status_patch = [{
            "op": "add",
            "path": "/status/resourceHandle",
            "value": resource_handle.reference,
        }]

        lifespan_value = {
            "start": datetime.utcnow().strftime('%FT%TZ'),
            "end": resource_handle.lifespan_end_timestamp,
            "maximum": resource_handle.get_lifespan_maximum(resource_claim=self),
            "relativeMaximum": resource_handle.get_lifespan_relative_maximum(resource_claim=self),
        }
        status_patch.append({
            "op": "add",
            "path": "/status/lifespan",
            "value": { k: v for k, v in lifespan_value.items() if v },
        })

        definition = await custom_objects_api.patch_namespaced_custom_object_status(
            body = status_patch,
            group = operator_domain,
            name = self.name,
            namespace = self.namespace,
            plural = 'resourceclaims',
            version = operator_version,
            _content_type = 'application/json-patch+json',
        )
        self.refresh_from_definition(definition)

        logger.info(f"Set {resource_handle.description} for {self.description}")

    def get_resource_state_from_status(self, resource_number):
        if not self.status \
        or not 'resources' in self.status \
        or resource_number >= len(self.status['resources']):
            return None
        return self.status['resources'][resource_number].get('state')

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

    async def update_status_from_handle(self,
        logger: kopf.ObjectLogger,
        resource_handle: ResourceHandleT
    ) -> None:
        patch = []

        lifespan_maximum = resource_handle.get_lifespan_maximum(resource_claim=self)
        lifespan_relative_maximum = resource_handle.get_lifespan_relative_maximum(resource_claim=self)
        if 'lifespan' in resource_handle.spec \
        and 'lifespan' not in self.status:
            lifespan_value = {
                "end": resource_handle.lifespan_end_timestamp,
                "maximum": lifespan_maximum,
                "relativeMaximum": lifespan_relative_maximum,
            }
            patch.append({
                "op": "add",
                "path": "/status/lifespan",
                "value": { k: v for k, v in lifespan_value.items() if v }
            })
        else:
            if self.lifespan_end_timestamp \
            and not resource_handle.lifespan_end_timestamp:
                patch.append({
                    "op": "remove",
                    "path": "/status/lifespan/end",
                })
            elif resource_handle.lifespan_end_timestamp \
            and self.lifespan_end_timestamp != resource_handle.lifespan_end_timestamp:
                patch.append({
                    "op": "add",
                    "path": "/status/lifespan/end",
                    "value": resource_handle.lifespan_end_timestamp,
                })

            if lifespan_maximum:
                if lifespan_maximum != self.lifespan_maximum:
                    patch.append({
                        "op": "add",
                        "path": "/status/lifespan/maximum",
                        "value": lifespan_maximum,
                    })
            elif self.lifespan_maximum:
                patch.append({
                    "op": "remove",
                    "path": "/status/lifespan/maximum",
                })

            if lifespan_relative_maximum:
                if lifespan_relative_maximum != self.lifespan_relative_maximum:
                    patch.append({
                        "op": "add",
                        "path": "/status/lifespan/relativeMaximum",
                        "value": lifespan_relative_maximum,
                    })
            elif self.lifespan_relative_maximum:
                patch.append({
                    "op": "remove",
                    "path": "/status/lifespan/relativeMaximum",
                })

        if patch:
            definition = await custom_objects_api.patch_namespaced_custom_object_status(
                group = operator_domain,
                name = self.name,
                namespace = self.namespace,
                plural = 'resourceclaims',
                version = operator_version,
                body = patch,
                _content_type = 'application/json-patch+json'
            )
            self.refresh_from_definition(definition)

    async def handle_delete(self, logger: kopf.ObjectLogger):
        if self.has_resource_handle:
            try:
                await custom_objects_api.delete_namespaced_custom_object(
                    group = operator_domain,
                    name = self.resource_handle_name,
                    namespace = self.resource_handle_namespace,
                    plural = 'resourcehandles',
                    version = operator_version,
                )
                logger.info(
                    f"Propagated delete of ResourceClaim {self.name} in {self.namespace} "
                    f"to ResourceHandle {self.resource_handle_name}"
                )
            except kubernetes_asyncio.client.exceptions.ApiException as e:
                if e.status != 404:
                    raise

    async def assign_resource_providers(self, logger) -> None:
        """
        Assign resource providers in status.
        """
        resources = self.spec.get('resources', None)
        if not resources:
            raise kopf.PermanentError(f"ResourceClaim {self.name} in {self.namespace} has no spec.resources")

        providers = []
        for resource in resources:
            provider_name = resource.get('provider', {}).get('name')
            if provider_name:
                provider = await resourceprovider.ResourceProvider.get(provider_name)
            elif 'template' in resource:
                provider = resourceprovider.ResourceProvider.find_provider_by_template_match(resource['template'])
            else:
                raise kopf.PermanentError(f"ResourceClaim spec.resources require either an explicit provider or a resource template to match.")
            providers.append(provider)

        definition = await custom_objects_api.patch_namespaced_custom_object_status(
            group = operator_domain,
            name = self.name,
            namespace = self.namespace,
            plural = 'resourceclaims',
            version = operator_version,
            body = {
                "status": {
                    "resources": [{
                        "name": resources[i].get('name'),
                        "provider": {
                            "apiVersion": operator_api_version,
                            "kind": "ResourceProvider",
                            "name": provider.name,
                            "namespace": provider.namespace,
                        },
                        "state": self.get_resource_state_from_status(resource_number=i),
                    } for i, provider in enumerate(providers)]
                }
            },
            _content_type = 'application/merge-patch+json'
        )
        self.refresh_from_definition(definition)
        logger.info(
            f"Assigned ResourceProviders {', '.join([p.name for p in providers])} "
            f"to ResourceClaim {self.name} in {self.namespace}"
        )

    async def delete(self):
        await custom_objects_api.delete_namespaced_custom_object(
            group = operator_domain,
            name = self.name,
            namespace = self.namespace,
            plural = 'resourceclaims',
            version = operator_version,
        )

    async def get_resource_handle(self):
        return await resourcehandle.ResourceHandle.get(self.resource_handle_name)

    async def get_resource_providers(self):
        resource_providers = []
        for resource in self.status.get('resources', []):
            resource_providers.append(
                await resourceprovider.ResourceProvider.get(resource['provider']['name'])
            )
        return resource_providers

    async def initialize_claim(self, logger):
        claim_resources = self.spec.get('resources', [])
        claim_status_resources = self.status.get('resources', [])
        patch = {
            "metadata": {
                "annotations": {
                    f"{operator_domain}/resource-claim-init-timestamp": datetime.utcnow().strftime('%FT%TZ')
                }
            },
            "spec": {
                "resources": claim_resources
            }
        }
        for i, claim_resource in enumerate(claim_resources):
            claim_status_resource = claim_status_resources[i]
            provider_ref = claim_status_resource['provider']
            provider = await resourceprovider.ResourceProvider.get(provider_ref['name'])
            claim_resource['provider'] = provider_ref
            template_with_defaults = provider.apply_template_defaults(
                resource_claim = self,
                resource_index = i,
            )
            if template_with_defaults:
                claim_resource['template'] = template_with_defaults

        definition = await custom_objects_api.patch_namespaced_custom_object(
            operator_domain, operator_version, self.namespace, 'resourceclaims', self.name, patch,
            _content_type = 'application/merge-patch+json'
        )
        self.refresh_from_definition(definition)
        logger.info(f"ResourceClaim {self.name} in {self.namespace} initialized")

    async def manage(self, logger) -> None:
        async with self.lock:
            if self.lifespan_start_datetime \
            and self.lifespan_start_datetime > datetime.utcnow():
                return

            if not self.have_resource_providers:
                await self.assign_resource_providers(logger=logger)

            if not self.claim_is_initialized:
                await self.initialize_claim(logger=logger)

            await self.validate()

            if not self.has_resource_handle:
                await self.bind_resource_handle(logger=logger)

            await self.manage_resource_handle(logger=logger)

    async def manage_resource_handle(self, logger: kopf.ObjectLogger):
        try:
            resource_handle = await self.get_resource_handle()
        except kubernetes_asyncio.client.exceptions.ApiException as e:
            if e.status == 404:
                logger.info(f"Found {self.resource_handle_description} deleted, propagating to {self.description}")
                await self.delete()
                return
            else:
                raise

        patch = []

        # Add any new resources from claim to handle
        for resource_index in range(len(resource_handle.resources), len(self.resources)):
            resource = self.resources[resource_index]
            logger.info(
                f"Adding new resource from {self.description} to {resource_handle.description} "
                f"with ResourceProvider {resource['provider']['name']}"
            )
            patch.append({
                "op": "add",
                "path": f"/spec/resources/{resource_index}",
                "value": {
                    "provider": resource['provider'],
                }
            })

        # Add changes from claim resource template to resource handle template
        for resource_index, claim_resource in enumerate(self.resources):
            claim_resource_template = claim_resource.get('template')
            if not claim_resource_template:
                continue
            if resource_index < len(resource_handle.spec['resources']):
                handle_resource_template = resource_handle.spec['resources'][resource_index].get('template', {})
            else:
                handle_resource_template = {}
            merged_resource_template = deepcopy(handle_resource_template)
            deep_merge(merged_resource_template, claim_resource_template)
            if handle_resource_template != merged_resource_template:
                patch.append({
                    "op": "add",
                    "path": f"/spec/resources/{resource_index}/template",
                    "value": merged_resource_template,
                })

        set_lifespan_end = self.requested_lifespan_end_datetime
        if set_lifespan_end:
            maximum_datetime = resource_handle.get_lifespan_end_maximum_datetime(resource_claim=self)
            if maximum_datetime \
            and set_lifespan_end > maximum_datetime:
                logger.warning(
                    f"{self.description} specifies lifespan end of "
                    f"{set_lifespan_end.strftime('%FT%TZ')} which exceeds "
                    f"{resource_handle.description} maximum of {maximum_datetime.strftime('%FT%TZ')}"
                )
                set_lifespan_end = maximum_datetime

            set_lifespan_end_timestamp = set_lifespan_end.strftime('%FT%TZ')

            if not 'lifespan' in resource_handle.spec:
                logger.info(f"Setting lifespan end for {resource_handle.description} to {set_lifespan_end_timestamp}")
                patch.append({
                    "op": "add",
                    "path": "/spec/lifespan",
                    "value": {
                        "end": set_lifespan_end_timestamp,
                    }
                })
            elif set_lifespan_end != resource_handle.lifespan_end_datetime:
                logger.info(f"Changing lifespan end for {resource_handle.description} to {set_lifespan_end_timestamp}")
                patch.append({
                    "op": "add",
                    "path": "/spec/lifespan/end",
                    "value": set_lifespan_end_timestamp,
                })

        if patch:
            definition = await custom_objects_api.patch_namespaced_custom_object(
                body = patch,
                group = operator_domain,
                name = resource_handle.name,
                namespace = resource_handle.namespace,
                plural = 'resourcehandles',
                version = operator_version,
                _content_type = 'application/json-patch+json',
            )
            resource_handle.refresh_from_definition(definition)

    async def validate(self):
        resources = self.spec.get('resources', [])
        resource_providers = await self.get_resource_providers()
        error_messages = []
        json_patch = []
        for i, resource in enumerate(resources):
            resource_provider = resource_providers[i]
            status_resource = self.status['resources'][i]
            try:
                resource_providers[i].validate_resource_template(resource.get('template', {}))
                if 'validationError' in status_resource:
                    json_patch.append({
                        "op": "remove",
                        "path": f"/status/resources/{i}/validationError",
                    })
            except jsonschema.exceptions.ValidationError as e:
                validation_error = str(e)
                if status_resource.get('validationError') != validation_error:
                    error_messages.append(
                        f"Template validation failed for ResourceProvider "
                        f"{resource_provider.name} on resources[{i}]: {validation_error}"
                    )
                    json_patch.append({
                        "op": "add",
                        "path": f"/status/resources/{i}/validationError",
                        "value": validation_error
                    })
        if json_patch:
            definition = await custom_objects_api.patch_namespaced_custom_object_status(
                operator_domain, operator_version, self.namespace,
                'resourceclaims', self.name, json_patch,
                _content_type = 'application/json-patch+json'
            )
            self.refresh_from_definition(definition)

        if error_messages:
            raise kopf.PermanentError(
                "\n".join(
                    [f"ResourceClaim {self.name} in {self.namespace} validation failed:"] +
                    error_messages
                )
            )
