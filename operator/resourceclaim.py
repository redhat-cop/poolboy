import asyncio
import jsonschema
import kopf
import kubernetes_asyncio

from copy import deepcopy
from datetime import datetime
from typing import List, Mapping, Optional, TypeVar, Union

from config import custom_objects_api, operator_domain, operator_api_version, operator_version
from deep_merge import deep_merge
from jsonpatch_from_diff import jsonpatch_from_diff
from poolboy_templating import recursive_process_template_strings

import resourcehandle
import resourcepool
import resourceprovider

ResourceClaimT = TypeVar('ResourceClaimT', bound='ResourceClaim')
ResourceHandleT = TypeVar('ResourceHandleT', bound='ResourceHandle')
ResourceProviderT = TypeVar('ResourceProviderT', bound='ResourceProvider')

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

    def __str__(self) -> str:
        return f"ResourceClaim {self.name} in {self.namespace}"

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
    def has_resource_handle(self) -> bool:
        """Return whether this ResourceClaim is bound to  a ResourceHandle."""
        return self.status and 'resourceHandle' in self.status

    @property
    def has_resource_provider(self) -> bool:
        """Return whether this ResourceClaim is managed by a ResourceProvider."""
        return 'provider' in self.spec

    @property
    def has_spec_resources(self) -> bool:
        """Return whether this ResourceClaim is has an explicit list of Resources."""
        return 'resources' in self.spec

    @property
    def have_resource_providers(self) -> bool:
        """Return whether this ResourceClaim has ResourceProviders assigned for all resources."""
        if not self.status \
        or not 'resources' in self.status \
        or len(self.spec.get('resources', [])) > len(self.status.get('resources', [])):
            return False
        for resource in self.status.get('resources', []):
            if not 'provider' in resource:
                return False
        return True

    @property
    def lifespan_end_datetime(self) -> Optional[datetime]:
        """Return datetime object representing when this ResourceClaim will be automatically deleted.
        Return None if lifespan end is not set.
        """
        timestamp = self.lifespan_end_timestamp
        if timestamp:
            return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")

    @property
    def lifespan_end_timestamp(self) -> Optional[str]:
        """Return timestamp representing when this ResourceClaim will be automatically deleted.
        Return None if lifespan end is not set.
        """
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
    def lifespan_start_datetime(self) -> Optional[datetime]:
        timestamp = self.lifespan_start_timestamp
        if timestamp:
            return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")

    @property
    def lifespan_start_timestamp(self) -> Optional[str]:
        timestamp = self.status.get('lifespan', {}).get('start')
        if timestamp:
            return timestamp
        return self.creation_timestamp

    @property
    def metadata(self) -> Mapping:
        return self.meta

    @property
    def parameter_values(self) -> Mapping:
        return self.status.get('provider', {}).get('parameterValues', {})

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
    def resource_provider_name(self) -> str:
        """Return name of ResourceProvider which manages this ResourceClaim.
        Status provider configuration overrides spec to prevent changes to provider.
        """
        return self.resource_provider_name_from_status or self.resource_provider_name_from_spec

    @property
    def resource_provider_name_from_status(self) -> Optional[str]:
        """Return name of managing ResourceProvider from ResourceClaim status."""
        return self.status.get('provider', {}).get('name')

    @property
    def resource_provider_name_from_spec(self) -> str:
        """Return name of managing ResourceProvider from ResourceClaim spec."""
        return self.spec['provider']['name']

    @property
    def resources(self):
        return self.spec.get('resources', [])

    @property
    def status_resources(self):
        if self.status:
            return self.status.get('resources', [])
        return []

    @property
    def validation_failed(self) -> bool:
        if self.status.get('provider', {}).get('validationErrors'):
            return True
        for resource in self.status_resources:
            if 'validationError' in resource:
                return True
        return False

    async def bind_resource_handle(self,
        logger: kopf.ObjectLogger,
        resource_claim_resources: List[Mapping],
    ):
        resource_handle = await resourcehandle.ResourceHandle.bind_handle_to_claim(
            logger = logger,
            resource_claim = self,
            resource_claim_resources = resource_claim_resources,
        )

        if not resource_handle:
            for provider in await self.get_resource_providers(resource_claim_resources):
                if provider.create_disabled:
                    raise kopf.TemporaryError(
                        f"Found no matching ResourceHandles for {self} and "
                        f"ResourceHandle creation is disabled for {provider}",
                        delay=600
                    )
            resource_handle = await resourcehandle.ResourceHandle.create_for_claim(
                logger = logger,
                resource_claim = self,
                resource_claim_resources = resource_claim_resources,
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

        await self.json_patch_status(status_patch)
        logger.info(f"Set {resource_handle} for {self}")

        return resource_handle

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
            await self.json_patch_status(patch)

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
        """Assign resource providers in status for ResourceClaim with resources listed in spec."""
        resources = self.spec.get('resources', None)

        if not resources:
            raise kopf.TemporaryError(f"{self} has no resources", delay=600)

        providers = []
        for resource in resources:
            provider_name = resource.get('provider', {}).get('name')
            if provider_name:
                provider = await resourceprovider.ResourceProvider.get(provider_name)
            elif 'template' in resource:
                provider = resourceprovider.ResourceProvider.find_provider_by_template_match(resource['template'])
            else:
                raise kopf.TemporaryError(f"ResourceClaim spec.resources require either an explicit provider or a resource template to match.", delay=600)
            providers.append(provider)

        await self.merge_patch_status({
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
        })

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

    async def get_resource_provider(self) -> ResourceProviderT:
        """Return ResourceProvider configured to manage ResourceClaim."""
        return await resourceprovider.ResourceProvider.get(self.resource_provider_name)

    async def get_resource_providers(self, resources:Optional[List[Mapping]]=None) -> List[ResourceProviderT]:
        """Return list of ResourceProviders assigned to each resource entry of ResourceClaim."""
        resource_providers = []
        if resources == None:
            resources = self.status.get('resources', [])
        for resource in resources:
            resource_providers.append(
                await resourceprovider.ResourceProvider.get(resource['provider']['name'])
            )
        return resource_providers

    async def get_resources_from_provider(self, resource_handle: Optional[ResourceHandleT]=None) -> List[Mapping]:
        """Return resources for this claim as defined by ResourceProvider"""
        resource_provider = await self.get_resource_provider()
        return await resource_provider.get_claim_resources(
            resource_claim = self,
            resource_handle = resource_handle,
        )

    async def initialize_claim(self, logger):
        """Set defaults into resource templates in claim and set init timestamp."""
        claim_status_resources = self.status.get('resources', [])
        patch = {
            "metadata": {
                "annotations": {
                    f"{operator_domain}/resource-claim-init-timestamp": datetime.utcnow().strftime('%FT%TZ')
                }
            },
        }

        claim_resources = self.spec.get('resources')
        if claim_resources:
            patch['spec'] = {
                "resources": claim_resources
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

        await self.merge_patch(patch)
        logger.info(f"ResourceClaim {self.name} in {self.namespace} initialized")

    async def json_patch_status(self, patch: List[Mapping]) -> None:
        """Apply json patch to object status and update definition."""
        definition = await custom_objects_api.patch_namespaced_custom_object_status(
            group = operator_domain,
            name = self.name,
            namespace = self.namespace,
            plural = 'resourceclaims',
            version = operator_version,
            body = patch,
            _content_type = 'application/json-patch+json',
        )
        self.refresh_from_definition(definition)

    async def manage(self, logger) -> None:
        async with self.lock:
            if self.lifespan_start_datetime \
            and self.lifespan_start_datetime > datetime.utcnow():
                return

            if self.has_resource_provider:
                if self.has_spec_resources:
                    raise kopf.TemporaryError(
                        f"{self} has both spec.provider and spec.resources!",
                        delay = 600
                    )
                if not 'provider' in self.status:
                    await self.merge_patch_status({
                        "provider": {
                            "name": self.resource_provider_name_from_spec
                        }
                    })
                elif self.resource_provider_name_from_spec != self.resource_provider_name_from_status:
                    raise kopf.TemporaryError(
                        f"spec.provider value ({self.resource_provider_name_from_spec}) does not "
                        f"match status.provider value ({self.resource_provider_name_from_status})! "
                        f"spec.provider is immutable!",
                        delay = 600
                    )

            elif self.has_spec_resources:
                if not self.have_resource_providers:
                    await self.assign_resource_providers(logger=logger)

            else:
                raise kopf.TemporaryError(
                    f"{self} has neither spec.provider nor spec.resources!",
                    delay = 600
                )

            if not self.claim_is_initialized:
                await self.initialize_claim(logger=logger)

            if self.has_resource_handle:
                try:
                    resource_handle = await self.get_resource_handle()
                except kubernetes_asyncio.client.exceptions.ApiException as e:
                    if e.status == 404:
                        logger.info(f"Found {self.resource_handle_description} deleted, propagating to {self}")
                        await self.delete()
                        return
                    else:
                        raise
            else:
                resource_handle = None

            await self.validate(logger=logger, resource_handle=resource_handle)
            if self.validation_failed:
                return

            if self.has_resource_provider:
                resource_claim_resources = await self.get_resources_from_provider()
            else:
                resource_claim_resources = self.resources

            if not resource_handle:
                resource_handle = await self.bind_resource_handle(
                    logger = logger,
                    resource_claim_resources = resource_claim_resources,
                )

            await self.__manage_resource_handle(
                logger = logger,
                resource_claim_resources = resource_claim_resources,
                resource_handle = resource_handle,
            )

    async def __manage_resource_handle(self,
        logger: kopf.ObjectLogger,
        resource_claim_resources: List[Mapping],
        resource_handle: Optional[ResourceHandleT],
    ) -> None:
        patch = []

        # Add any new resources from claim to handle
        for resource_index in range(len(resource_handle.resources), len(resource_claim_resources)):
            resource = resource_claim_resources[resource_index]
            logger.info(
                f"Adding new resource from {self} to {resource_handle} "
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
        for resource_index, claim_resource in enumerate(resource_claim_resources):
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
                    f"{self} specifies lifespan end of "
                    f"{set_lifespan_end.strftime('%FT%TZ')} which exceeds "
                    f"{resource_handle} maximum of {maximum_datetime.strftime('%FT%TZ')}"
                )
                set_lifespan_end = maximum_datetime

            set_lifespan_end_timestamp = set_lifespan_end.strftime('%FT%TZ')

            if not 'lifespan' in resource_handle.spec:
                logger.info(f"Setting lifespan end for {resource_handle} to {set_lifespan_end_timestamp}")
                patch.append({
                    "op": "add",
                    "path": "/spec/lifespan",
                    "value": {
                        "end": set_lifespan_end_timestamp,
                    }
                })
            elif set_lifespan_end != resource_handle.lifespan_end_datetime:
                logger.info(f"Changing lifespan end for {resource_handle} to {set_lifespan_end_timestamp}")
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

    async def merge_patch(self, patch: Mapping) -> None:
        """Apply merge patch to object status and update definition."""
        definition = await custom_objects_api.patch_namespaced_custom_object(
            group = operator_domain,
            name = self.name,
            namespace = self.namespace,
            plural = 'resourceclaims',
            version = operator_version,
            body = patch,
            _content_type = 'application/merge-patch+json'
        )
        self.refresh_from_definition(definition)

    async def merge_patch_status(self, patch: Mapping) -> None:
        """Apply merge patch to object status and update definition."""
        definition = await custom_objects_api.patch_namespaced_custom_object_status(
            group = operator_domain,
            name = self.name,
            namespace = self.namespace,
            plural = 'resourceclaims',
            version = operator_version,
            body = {
                "status": patch
            },
            _content_type = 'application/merge-patch+json'
        )
        self.refresh_from_definition(definition)

    async def validate(self,
        logger: kopf.ObjectLogger,
        resource_handle: Optional[ResourceHandleT]
    ) -> None:
        if self.has_resource_provider:
            await self.validate_with_provider(logger=logger, resource_handle=resource_handle)
        elif self.has_spec_resources:
            await self.validate_resources(logger=logger, resource_handle=resource_handle)

    async def validate_resources(self,
        logger: kopf.ObjectLogger,
        resource_handle: Optional[ResourceHandleT]
    ) -> None:
        """Validate ResourceClaim with explicit list of resources.
        Patch status with failure details if validation fails.
        """
        resources = self.spec.get('resources', [])
        resource_providers = await self.get_resource_providers()
        status_json_patch = []

        for i, resource in enumerate(resources):
            resource_provider = resource_providers[i]
            resource_template = resource.get('template', {})
            status_resource = self.status['resources'][i]
            try:
                resource_providers[i].validate_resource_template(
                    resource_claim = self,
                    resource_handle = resource_handle,
                    template = resource.get('template', {}),
                )
                if 'validationError' in status_resource:
                    logger.info(f"Clearing validation error for {self}")
                    status_json_patch.append({
                        "op": "remove",
                        "path": f"/status/resources/{i}/validationError",
                    })
            except Exception as exception:
                logger.warning(f"Validation failed for {self}: {exception}")
                status_json_patch.append({
                    "op": "add",
                    "path": f"/status/resources/{i}/validationError",
                    "value": str(exception),
                })
        if status_json_patch:
            await self.json_patch_status(status_json_patch)

    async def validate_with_provider(self,
        logger: kopf.ObjectLogger,
        resource_handle: Optional[ResourceHandleT]
    ) -> None:
        """Validate ResourceClaim using its ResourceProvider.
        Patch status with failure details if validation fails.
        """
        resource_handle_vars = resource_handle.vars if resource_handle else {}
        try:
            resource_provider = await self.get_resource_provider()
        except kubernetes_asyncio.client.exceptions.ApiException as e:
            if e.status == 404:
                raise kopf.TemporaryError(
                    f"ResourceProvider {self.resource_provider_name} not found.",
                    delay=600
                )
            else:
                raise

        validation_errors = []
        vars_ = {
            **resource_provider.vars,
            **resource_handle_vars,
            "resource_claim": self,
            "resource_handle": resource_handle,
            "resource_provider": resource_provider,
        }

        parameter_values = self.spec.get('provider', {}).get('parameterValues', {})
        parameter_states = self.status.get('provider', {}).get('parameterValues')

        for parameter in resource_provider.get_parameters():
            if parameter.name not in parameter_values and parameter.default != None:
                parameter_values[parameter.name] = parameter.default

        parameter_names = set()
        for parameter in resource_provider.get_parameters():
            parameter_names.add(parameter.name)
            if parameter.name in parameter_values:
                value = parameter_values[parameter.name]
                if parameter_states:
                    if value == parameter_states.get(parameter.name):
                        # Unchanged from current state is automatically considered valid
                        # even if validation rules have changed.
                        continue
                    if not parameter.allow_update:
                        validation_errors.append(f"Parameter {parameter.name} is immutable.")
                        continue

                if parameter.open_api_v3_schema_validator:
                    try:
                        parameter.open_api_v3_schema_validator.validate(value)
                    except Exception as exception:
                        validation_errors.append(f"Parameter {parameter.name} schema validation failed: {exception}")

                for validation_check in parameter.validation_checks:
                    try:
                        successful = recursive_process_template_strings(
                            '{{(' + validation_check.check + ')|bool}}',
                            variables = { **vars_, **parameter_values, "value": value }
                        )
                        if not check_successful:
                            validation_errors.append(f"Parameter {parameter.name} failed check: {validation_check.name}")
                    except Exception as exception:
                        validation_errors.append(f"Parameter {parameter.name} exception on check {validation_check.name}: {exception}")
            elif parameter.required:
                validation_errors.append(f"Parameter {parameter.name} is required.")

        for name, value in parameter_values.items():
            if parameter_states and value == parameter_states.get(name):
                # Unchanged from current state is automatically considered valid
                # even if parameter has been removed.
                continue
            if name not in parameter_names:
                validation_errors.append(f"No such parameter {name}.")

        if parameter_values != parameter_states \
        or validation_errors != self.status.get('provider', {}).get('validationErrors'):
            patch = {
                "provider": {
                    "name": resource_provider.name,
                    "parameterValues": parameter_values if parameter_values else None,
                    "validationErrors": validation_errors if validation_errors else None,
                }
            }
            if 'resources' not in self.status:
                resources = await resource_provider.get_claim_resources(
                    parameter_values = parameter_values,
                    resource_claim = self,
                )
                patch['resources'] = [
                    {
                        "name": resource['name'],
                        "provider": resource['provider'],
                    } for resource in resources
                ]
            await self.merge_patch_status(patch)