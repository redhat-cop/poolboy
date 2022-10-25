import asyncio
import jinja2
import jsonpointer
import kopf
import pytimeparse
import re

from copy import deepcopy
from datetime import timedelta
from openapi_schema_validator import OAS30Validator
from openapi_schema_util import defaults_from_schema
from typing import List, Mapping, Optional, TypeVar

import poolboy_k8s

from config import custom_objects_api, operator_domain, operator_namespace, operator_version 
from deep_merge import deep_merge
from jsonpatch_from_diff import jsonpatch_from_diff
from poolboy_templating import recursive_process_template_strings

ResourceClaimT = TypeVar('ResourceClaimT', bound='ResourceClaim')
ResourceHandleT = TypeVar('ResourceHandleT', bound='ResourceHandle')
ResourceProviderT = TypeVar('ResourceProviderT', bound='ResourceProvider')

class LinkedResourceProvider:
    def __init__(self, spec):
        self.name = spec['name']
        self.wait_for = spec.get('waitFor')
        self.template_vars = [
            TemplateVar(item) for item in spec.get('templateVars', [])
        ]

    def check_wait_for(self,
        linked_resource_provider,
        linked_resource_state,
        resource_claim,
        resource_handle,
        resource_provider,
        resource_state,
    ) -> bool:
        '''
        Check wait condition. True means resource management should proceed.
        '''
        if not self.wait_for:
            # No condition to wait for
            return True
        if not linked_resource_state:
            # No linked resource state, so definitely wait
            return False

        template_vars = {
            'linked_resource_provider': linked_resource_provider,
            'linked_resource_state': linked_resource_state,
            'resource_claim': resource_claim,
            'resource_handle': resource_handle,
            'resource_provider': resource_provider,
            'resource_state': resource_state,
        }

        for template_var in self.template_vars:
            template_vars[template_var.name] = jsonpointer.resolve_pointer(
                linked_resource_state, template_var.value_from,
                default = jinja2.ChainableUndefined()
            )

        return recursive_process_template_strings(
            '{{(' + self.wait_for + ')|bool}}', resource_provider.template_style, template_vars
        )

class ResourceProvider:
    instances = {}
    lock = asyncio.Lock()

    @staticmethod
    def __register_definition(definition: Mapping) -> ResourceProviderT:
        name = definition['metadata']['name']
        resource_provider = ResourceProvider.instances.get(name)
        if resource_provider:
            resource_provider.definition = definition
            self.__init_resource_template_validator()
        else:
            resource_provider = ResourceProvider(definition=definition)
            ResourceProvider.instances[name] = resource_provider
        return resource_provider

    @staticmethod
    def find_provider_by_template_match(template: Mapping) -> ResourceProviderT:
        provider_matches = []
        for provider in ResourceProvider.instances.values():
            if provider.is_match_for_template(template):
                provider_matches.append(provider)
        if len(provider_matches) == 0:
            raise kopf.TemporaryError(f"Unable to match template to ResourceProvider", delay=60)
        elif len(provider_matches) == 1:
            return provider_matches[0]
        else:
            raise kopf.PermanentError(f"Resource template matches multiple ResourceProviders")

    @staticmethod
    async def get(name: str) -> ResourceProviderT:
        async with ResourceProvider.lock:
            resource_provider = ResourceProvider.instances.get(name)
            if resource_provider:
                return resource_provider
            definition = await custom_objects_api.get_cluster_custom_object(
                operator_domain, operator_version, 'resourceproviders', name
            )
            return ResourceProvider.__register_definition(definition=definition)

    @staticmethod
    async def preload(logger: kopf.ObjectLogger) -> None:
        async with ResourceProvider.lock:
            _continue = None
            while True:
                resource_provider_list = await custom_objects_api.list_namespaced_custom_object(
                    operator_domain, operator_version, operator_namespace, 'resourceproviders',
                    _continue = _continue,
                    limit = 50,
                )
                for definition in resource_provider_list['items']:
                    ResourceProvider.__register_definition(definition=definition)
                _continue = resource_provider_list['metadata'].get('continue')
                if not _continue:
                    break

    @staticmethod
    async def register(definition: Mapping, logger: kopf.ObjectLogger) -> ResourceProviderT:
        async with ResourceProvider.lock:
            name = definition['metadata']['name']
            resource_provider = ResourceProvider.instances.get(name)
            if resource_provider:
                resource_provider.__init__(definition=definition)
                logger.info(f"Refreshed definition of ResourceProvider {name}")
            else:
                resource_provider = ResourceProvider.__register_definition(definition=definition)
                logger.info(f"Registered ResourceProvider {name}")
            return resource_provider

    @staticmethod
    async def unregister(name: str, logger: kopf.ObjectLogger) -> Optional[ResourceProviderT]:
        async with ResourceProvider.lock:
            if name in ResourceProvider.instances:
                logger.info(f"Unregistered ResourceProvider {name}")
                return ResourceProvider.instances.pop(name)

    def __init__(self, definition: Mapping) -> None:
        self.meta = definition['metadata']
        self.spec = definition['spec']
        self.__init_resource_template_validator()

    def __init_resource_template_validator(self) -> None:
        open_api_v3_schema = self.spec.get('validation', {}).get('openAPIV3Schema', None)
        if open_api_v3_schema:
            self.resource_template_validator = OAS30Validator(open_api_v3_schema)
        else:
            self.resource_template_validator = None

    @property
    def create_disabled(self) -> bool:
        return self.spec.get('disableCreation', False)

    @property
    def description(self) -> str:
        return f"ResourceProvider {self.name}"

    @property
    def lifespan_default(self) -> Optional[str]:
        lifespan = self.spec.get('lifespan')
        if lifespan:
            return lifespan.get('default')

    @property
    def lifespan_default_seconds(self) -> Optional[int]:
        interval = self.lifespan_default
        if interval:
            return pytimeparse.parse(interval)

    @property
    def lifespan_default_timedelta(self):
        seconds = self.lifespan_default_seconds
        if seconds:
            return timedelta(seconds=seconds)

    @property
    def lifespan_maximum(self) -> Optional[str]:
        lifespan = self.spec.get('lifespan')
        if lifespan:
            return lifespan.get('maximum')

    @property
    def lifespan_maximum_seconds(self) -> Optional[int]:
        interval = self.lifespan_maximum
        if interval:
            return pytimeparse.parse(interval)

    @property
    def lifespan_maximum_timedelta(self):
        seconds = self.lifespan_maximum_seconds
        if seconds:
            return timedelta(seconds=seconds)

    @property
    def lifespan_relative_maximum(self) -> Optional[str]:
        lifespan = self.spec.get('lifespan')
        if lifespan:
            return lifespan.get('relativeMaximum')

    @property
    def lifespan_relative_maximum_seconds(self) -> Optional[int]:
        interval = self.lifespan_maximum
        if interval:
            return pytimeparse.parse(interval)

    @property
    def lifespan_relative_maximum_timedelta(self):
        seconds = self.lifespan_relative_maximum_seconds
        if seconds:
            return timedelta(seconds=seconds)

    @property
    def linked_resource_providers(self) -> List[ResourceProviderT]:
        return [
            LinkedResourceProvider(item) for item in self.spec.get('linkedResourceProviders', [])
        ]

    @property
    def match(self):
        return self.spec.get('match', None)

    @property
    def match_ignore(self):
        return self.spec.get('matchIgnore', [])

    @property
    def metadata(self) -> Mapping:
        return self.meta

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def override(self):
        return self.spec.get('override', {})

    @property
    def resource_requires_claim(self) -> bool:
        return self.spec.get('resourceRequiresClaim', False)

    @property
    def template_enable(self):
        return self.spec.get('template', {}).get('enable', False)

    @property
    def template_style(self):
        return self.spec.get('template', {}).get('style', 'jinja2')

    @property
    def update_filters(self):
        return self.spec.get('updateFilters', [])

    def apply_template_defaults(self, resource_claim, resource_index) -> Mapping:
        """
        Return Resource template for ResourceClaim with defaults filled in.
        """
        resource = resource_claim.spec['resources'][resource_index]
        template = deepcopy(resource.get('template', {}))
        if 'default' in self.spec:
            if self.template_enable:
                deep_merge(
                    template,
                    recursive_process_template_strings(
                        template = self.spec['default'],
                        template_style = self.template_style,
                        variables = {
                            "resource_claim": resource_claim,
                            "resource_index": resource_index,
                            "resource_name": resource.get('name'),
                            "resource_provider": self,
                        }
                    ),
                    overwrite=False,
                )
            else:
                deep_merge(template, self.spec['default'], overwrite=False)

        open_api_v3_schema = self.spec.get('validation', {}).get('openAPIV3Schema', None)
        if open_api_v3_schema:
            deep_merge(template, defaults_from_schema(open_api_v3_schema), overwrite=False)

        return template

    def check_template_match(self,
        claim_resource_template: Mapping,
        handle_resource_template: Mapping,
    ) -> Optional[List[Mapping]]:
        """
        Check if a resource in a handle matches a resource in a claim.
        Returns a jsondiff of any allowed differences on match or None otherwise.
        """
        patch = [
            item for item in jsonpatch_from_diff(
                handle_resource_template, claim_resource_template
            ) if item['op'] in ['add', 'replace']
        ]
        # Return false if any item from the patch is not ignored
        ignore_re_list = [ re.compile(pattern + '$') for pattern in self.match_ignore ]
        for item in patch:
            ignored = False
            for ignore_re in ignore_re_list:
                if ignore_re.match(item['path']):
                    ignored = True
            if not ignored:
                return None
        return patch

    def is_match_for_template(self, template: Mapping) -> bool:
        """
        Check if this provider is a match for the resource template by checking
        that all fields in the match definition match the template.
        """
        if not self.match:
            return False
        cmp_template = deepcopy(template)
        deep_merge(cmp_template, self.match)
        return template == cmp_template

    def validate_resource_template(self, template: Mapping) -> None:
        if self.resource_template_validator:
            self.resource_template_validator.validate(template)

    async def resource_definition_from_template(self,
        logger: kopf.ObjectLogger,
        resource_claim: Optional[ResourceClaimT],
        resource_handle: ResourceHandleT,
        resource_index: int,
        resource_states: List[Optional[Mapping]],
        template_vars: Mapping,
    ):
        if resource_claim:
            requester_user, requester_identities = await poolboy_k8s.get_requester_from_namespace(
                resource_claim.namespace
            )
        else:
            requester_user = None
            requester_identities = []
        requester_identity = requester_identities[0] if len(requester_identities) > 0 else None

        resource_name = resource_handle.spec['resources'][resource_index].get('name')
        resource_references = [r.get('reference') for r in resource_handle.spec['resources']]
        resource_reference = resource_references[resource_index] or {}
        resource_templates = [r.get('template') for r in resource_handle.spec['resources']]
        resource_template = resource_templates[resource_index] or {}
        resource_definition = deepcopy(resource_template)
        if 'override' in self.spec:
            if self.template_enable:
                all_template_vars = template_vars.copy()
                all_template_vars.update({
                    "guid": resource_handle.guid,
                    "requester_identities": requester_identities,
                    "requester_identity": requester_identity,
                    "requester_user": requester_user,
                    "resource_provider": self,
                    "resource_handle": resource_handle,
                    "resource_claim": resource_claim,
                    "resource_index": resource_index,
                    "resource_name": resource_name,
                    "resource_reference": resource_reference,
                    "resource_references": resource_references,
                    "resource_state": resource_states[resource_index],
                    "resource_states": resource_states,
                    "resource_template": resource_templates[resource_index],
                    "resource_templates": resource_templates,
                })
                deep_merge(
                    resource_definition,
                    recursive_process_template_strings(
                        self.override, self.template_style, all_template_vars
                    )
                )
            else:
                deep_merge(resource_definition, self.override)

        if 'apiVersion' not in resource_definition:
            raise kopf.PermanentError(
                f"Template processing by ResourceProvider {self.name} for ResourceHandle {resource_handle.name} "
                f"produced definition without an apiVersion!"
            )
        if 'kind' not in resource_definition:
            raise kopf.PermanentError(
                f"Template processing by ResourceProvider {self.name} for ResourceHandle {resource_handle.name} "
                f"produced definition without a kind!"
            )
        if 'metadata' not in resource_definition:
            resource_definition['metadata'] = {}
        if 'name' not in resource_definition['metadata']:
            # If name prefix was not given then use prefix "guidN-" with resource index to
            # prevent name conflicts. If the resource template does specify a name prefix
            # then it is expected that the template configuration prevents conflicts.
            if 'generateName' not in resource_definition['metadata']:
                resource_definition['metadata']['generateName'] = f"guid{resource_index}-"
            resource_definition['metadata']['name'] = resource_definition['metadata']['generateName'] + resource_handle.guid

        if resource_reference:
            # If there is an existing resource reference, then don't allow changes
            # to identifying properties in the reference.
            resource_definition['metadata']['name'] = resource_reference['name']
            if 'namespace' in resource_reference:
                resource_definition['metadata']['namespace'] = resource_reference['namespace']
            if resource_definition['apiVersion'] != resource_reference['apiVersion']:
                raise kopf.PermanentError(f"Unable to change apiVersion for resource!")
            if resource_definition['kind'] != resource_reference['kind']:
                raise kopf.PermanentError(f"Unable to change kind for resource!")

        if 'annotations' not in resource_definition['metadata']:
            resource_definition['metadata']['annotations'] = {}

        resource_definition['metadata']['annotations'].update({
            f"{operator_domain}/resource-provider-name": self.name,
            f"{operator_domain}/resource-provider-namespace": self.namespace,
            f"{operator_domain}/resource-handle-name": resource_handle.name,
            f"{operator_domain}/resource-handle-namespace": resource_handle.namespace,
            f"{operator_domain}/resource-handle-uid": resource_handle.uid,
            f"{operator_domain}/resource-index": str(resource_index)
        })

        if resource_claim:
            resource_definition['metadata']['annotations'].update({
                f"{operator_domain}/resource-claim-name": resource_claim.name,
                f"{operator_domain}/resource-claim-namespace": resource_claim.namespace,
            })

        if resource_handle.resource_pool_name:
            resource_definition['metadata']['annotations'].update({
                f"{operator_domain}/resource-pool-name": resource_handle.resource_pool_name,
                f"{operator_domain}/resource-pool-namespace": resource_handle.resource_pool_namespace,
            })

        if requester_user:
            resource_definition['metadata']['annotations'].update({
                f"{operator_domain}/resource-requester-user": requester_user['metadata']['name'],
            })

        if requester_identity:
            resource_definition['metadata']['annotations'].update({
                f"{operator_domain}/resource-requester-email": requester_identity.get('extra', {}).get('email', ''),
                f"{operator_domain}/resource-requester-name": requester_identity.get('extra', {}).get('name', ''),
                f"{operator_domain}/resource-requester-preferred-username": requester_identity.get('extra', {}).get('preferred_username', ''),
            })

        return resource_definition

    async def update_resource(self,
        logger: kopf.ObjectLogger,
        resource_definition: Mapping,
        resource_handle: ResourceHandleT,
        resource_state: Mapping,
    ) -> Optional[List]:
        update_filters = self.update_filters + [{
            'pathMatch': f"/metadata/annotations/{re.escape(operator_domain)}~1resource-.*"
        }]
        patch = jsonpatch_from_diff(resource_state, resource_definition, update_filters=update_filters)
        if patch:
            await poolboy_k8s.patch_object(
                api_version = resource_definition['apiVersion'],
                kind = resource_definition['kind'],
                name = resource_definition['metadata']['name'],
                namespace = resource_definition['metadata'].get('namespace'),
                patch = patch,
            )
            return patch


class TemplateVar:
    def __init__(self, spec):
        self.name = spec['name']
        self.value_from = spec['from']