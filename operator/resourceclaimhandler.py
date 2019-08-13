import copy
import json
import kubernetes.client.rest
import openapi_core.shortcuts
import openapi_core.wrappers.mock
import openapi_core.extensions.models.models
import openapi_core.schema.schemas.exceptions

from util import dict_merge, recursive_process_template_strings

def claim_is_bound(claim):
    return 'status' in claim and 'handle' in claim['status']

def provider_matches_claim(provider, claim):
    claim_template = claim['spec']['template']
    cmp_claim_template = copy.deepcopy(claim_template)
    dict_merge(cmp_claim_template, provider['spec']['match'])
    return claim_template == cmp_claim_template

def provider_template_validator(provider):
    return openapi_core.shortcuts.RequestValidator(
        openapi_core.shortcuts.create_spec({
            "openapi": "3.0.0",
            "info": {
                "title": "",
                "version": "0.1"
            },
            "paths": {
                "/claimTemplate": {
                    "post": {
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/ClaimTemplate"
                                    }
                                }
                            }
                        },
                        "responses": {}
                    }
                }
            },
            "components": {
                "schemas": {
                    "ClaimTemplate": provider['spec']['validation']['openAPIV3Schema']
                }
            }
        })
    )

def validate_claim_template(provider, claim):
    validator = provider_template_validator(provider)
    validation_result = validator.validate(
        openapi_core.wrappers.mock.MockRequest(
            'http://example.com', 'post', '/claimTemplate',
            path_pattern='/claimTemplate',
            data=json.dumps(claim['spec']['template'])
        )
    )
    validation_result.raise_for_errors()

class ResourceClaimHandler(object):
    def __init__(self, ko):
        self.ko = ko

    def added(self, claim):
        if claim_is_bound(claim):
            self.update_handle(claim)
        else:
            self.bind_handle_to_claim(claim)

    def deleted(self, claim):
        handle = self.get_handle_for_claim(claim)
        if handle and not 'deletionTimestamp' in handle['metadata']:
            self.ko.delete_custom_resource(
                group=self.ko.operator_domain,
                version='v1',
                kind='ResourceHandle',
                name=handle['metadata']['name'],
                namespace=handle['metadata']['namespace']
            )

    def modified(self, claim):
        # Handle modified the same as added
        self.added(claim)

    def bind_available_handle_to_claim(self, provider, claim):
        # FIXME - get list of available handles for this provider and then check
        #         each to see if they match the claim
        pass

    def bind_handle_to_claim(self, claim):
        claim_name = claim['metadata']['name']
        claim_namespace = claim['metadata']['namespace']

        handle = self.get_handle_for_claim(claim)
        if handle:
            self.ko.logger.warn(
                "Found unlisted ResourceHandle %s for ResourceClaim %s/%s",
                handle['metadata']['name'],
                claim_namespace,
                claim_name
            )
        else:
            try:
                provider = self.find_provider_for_claim(claim)
            except Exception as err:
                self.ko.logger.warn(
                    "Unable to find provider for claim %s/%s, %s: '%s'",
                    claim_namespace,
                    claim_name,
                    type(err).__name__,
                    str(err)
                )
                return

            claim = self.initialize_claim(provider, claim)

            handle = self.bind_available_handle_to_claim(provider, claim)
            if not handle:
                handle = self.create_handle_for_claim(provider, claim)

        self.set_handle_for_claim(claim, handle)

    def create_handle_for_claim(self, provider, claim):
        claim_name = claim['metadata']['name']
        claim_namespace = claim['metadata']['namespace']
        claim_template = claim['spec']['template']
        provider_name = provider['metadata']['name']
        provider_namespace = provider['metadata']['namespace']

        return self.ko.custom_objects_api.create_namespaced_custom_object(
            self.ko.operator_domain,
            'v1',
            self.ko.operator_namespace,
            'resourcehandles',
            {
                'apiVersion': self.ko.operator_domain + '/v1',
                'kind': 'ResourceHandle',
                'metadata': {
                    'finalizers': [
                        self.ko.operator_domain + '/resource-claim-operator'
                    ],
                    'generateName': 'guid-',
                    'labels': {
                        self.ko.operator_domain + '/resource-claim-namespace': claim_namespace,
                        self.ko.operator_domain + '/resource-claim-name': claim_name,
                        self.ko.operator_domain + '/resource-provider': provider_name
                    }
                },
                'spec': {
                    'claim': {
                        'apiVersion': 'v1',
                        'kind': 'ResourceClaim',
                        'name': claim_name,
                        'namespace': claim_namespace
                    },
                    'provider': {
                        'apiVersion': 'v1',
                        'kind': 'ResourceHandler',
                        'name': provider_name,
                        'namespace': provider_namespace
                    },
                    'template': claim_template
                }
            }
        )

    def find_provider_for_claim(self, claim):
        for cache_ent in self.ko.watchers['ResourceProvider'].cache.values():
            provider = cache_ent.resource
            if provider_matches_claim(provider, claim):
                validate_claim_template(provider, claim)
                return provider
        raise Exception("No provider matched claim {}/{}".format(
            claim['metadata']['namespace'], claim['metadata']['name']
        ))

    def get_handle_for_claim(self, claim):
        claim_name = claim['metadata']['name']
        claim_namespace = claim['metadata']['namespace']
        items = self.ko.custom_objects_api.list_namespaced_custom_object(
             self.ko.operator_domain,
             'v1',
             self.ko.operator_namespace,
             'resourcehandles',
             label_selector='{0}/resource-claim-namespace={1},{0}/resource-claim-name={2}'.format(
                 self.ko.operator_domain, claim_namespace, claim_name
             )
        ).get('items', [])
        if items:
            return items[0]

    def initialize_claim(self, provider, claim):
        claim_init = provider['spec'].get('claimInit', None)
        if not claim_init:
            return claim
        template_variables = {
            "resource_claim": claim,
            "resource_provider": provider
        }
        return self.ko.patch_resource(
            claim,
            {
                "spec": {
                    "template": recursive_process_template_strings(claim_init, template_variables),
                }
            },
            [{ 'pathMatch': '/.*', 'allowedOps': ['add','replace'] }]
        )

    def set_handle_for_claim(self, claim, handle):
        self.ko.patch_resource_status(
            claim,
            {
                "handle": {
                    "apiVersion": 'v1',
                    "kind": 'ResourceHandle',
                    "name": handle['metadata']['name'],
                    "namespace": handle['metadata']['namespace']
                }
            },
            [{ 'pathMatch': '/.*', 'allowedOps': ['add','replace'] }]
        )

    def update_handle(self, claim):
        claim_name = claim['metadata']['name']
        claim_namespace = claim['metadata']['namespace']
        claim_template = claim['spec']['template']

        handle = self.get_handle_for_claim(claim)
        if not handle:
            self.ko.logger.warn("Handle missing for claim %s/%s", claim_namespace, claim_name)
            return

        # FIXME - Add provider control for changes from claim template to handle
        if handle['spec']['template'] == claim_template:
            return

        self.ko.patch_resource(
            handle,
            {
                "spec": {
                    "template": claim_template
                }
            },
            [{ 'pathMatch': '/spec/template(/.*)?' }]
        )
