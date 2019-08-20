#!/usr/bin/env python

import copy
import datetime
import gpte.kubeoperative
import json
import kopf
import kubernetes
import openapi_core.shortcuts
import openapi_core.wrappers.mock
import openapi_core.extensions.models.models
import openapi_core.schema.schemas.exceptions
import os
import prometheus_client
import re
import time

from gpte.util import dict_merge, recursive_process_template_strings

ko = gpte.kubeoperative.KubeOperative()
providers = {}
provider_init_delay = int(os.environ.get('PROVIDER_INIT_DELAY', 10))
start_time = time.time()

def pause_for_provider_init():
    if time.time() < start_time + provider_init_delay:
        time.sleep(time.time() - start_time)

class ResourceProvider(object):

    providers = {}
    resource_watchers = {}

    @staticmethod
    def delete_handle_on_lost_claim(handle, logger):
        claim_ref = handle['spec']['resourceClaim']
        claim_name = claim_ref['name']
        claim_namespace = claim_ref['namespace']
        ResourceProvider.log_handle_event(
            handle, logger, 'Lost ResourceClaim {} in {}'.format(
                claim_name, claim_namespace
            )
        )
        ResourceProvider.delete_resource_handle(handle['metadata']['name'], logger)

    @staticmethod
    def delete_resource_handle(handle_name, logger):
        ko.custom_objects_api.delete_namespaced_custom_object(
            ko.operator_domain, 'v1', ko.operator_namespace,
            'resourcehandles', handle_name,
            kubernetes.client.V1DeleteOptions()
        )

    @staticmethod
    def find_provider_by_name(name):
        return ResourceProvider.providers.get(name, None)

    @staticmethod
    def find_provider_by_template_match(template):
        for provider in ResourceProvider.providers.values():
            if provider.is_match_for_template(template):
                return provider

    @staticmethod
    def get_claim_for_handle(handle, logger):
        if 'resourceClaim' not in handle['spec']:
            return None

        claim_ref = handle['spec']['resourceClaim']
        claim_name = claim_ref['name']
        claim_namespace = claim_ref['namespace']
        try:
            claim = ko.custom_objects_api.get_namespaced_custom_object(
                ko.operator_domain, 'v1', claim_namespace,
                'resourceclaims', claim_name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise
        return claim

    @staticmethod
    def get_requester_from_namespace(namespace):
        resource_claim_namespace = ko.core_v1_api.read_namespace(namespace)
        requester_user_name = resource_claim_namespace.metadata.annotations.get(
            'openshift.io/requester', None
        )
        if not requester_user_name:
            return None, None

        requester_user = requester_identity = None
        try:
            requester_user = ko.custom_objects_api.get_cluster_custom_object(
                'user.openshift.io',
                'v1',
                'users',
                requester_user_name
            )
            if requester_user.get('identities', None):
                requester_identity = ko.custom_objects_api.get_cluster_custom_object(
                    'user.openshift.io',
                    'v1',
                    'identities',
                    requester_user['identities'][0]
                )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

        return requester_identity, requester_user


    @staticmethod
    def log_claim_event(claim, logger, msg):
        logger.info(
            "ResourceClaim %s in %s: %s",
            claim['metadata']['name'],
            claim['metadata']['namespace'],
            msg
        )
        # FIXME - Create event for claim

    @staticmethod
    def log_handle_event(handle, logger, msg):
        logger.info(
            "ResourceHandle %s: %s",
            handle['metadata']['name'],
            msg
        )
        # FIXME - Create event for handle

    @staticmethod
    def manage_claim(claim, logger):
        provider_name = claim['status']['resourceProvider']['name']

        provider = ResourceProvider.find_provider_by_name(provider_name)
        if not provider:
            ResourceProvider.log_claim_event(
                claim, logger,
                "Unable to find ResourceProvider " + provider_name
            )
            return
        provider._manage_claim(claim, logger)

    @staticmethod
    def manage_claim_create(claim, logger):
        claim_meta = claim['metadata']
        annotations = claim_meta.get('annotations', {})
        provider_name = annotations.get(ko.operator_domain + '/resource-provider-name', None)

        if provider_name:
            provider = ResourceProvider.find_provider_by_name(provider_name)
            if not provider:
                ResourceProvider.log_claim_event(
                    claim, logger,
                    "Unable to find ResourceProvider " + provider_name
                )
                return
        else:
            provider = ResourceProvider.find_provider_by_template_match(claim['spec']['template'])
            if not provider:
                ResourceProvider.log_claim_event(
                    claim, logger,
                    "Unable to find matching ResourceProvider",
                )
                return

        provider.initialize_claim_status(claim, logger)

    @staticmethod
    def manage_claim_deleted(claim, logger):
        claim_meta = claim['metadata']
        claim_name = claim_meta['name']
        claim_namespace = claim_meta['namespace']
        handle_ref = claim.get('status', {}).get('resourceHandle', None)

        logger.info('ResourceClaim %s in %s deleted', claim_name, claim_namespace)
        if handle_ref:
            handle_name = handle_ref['name']
            logger.info(
                'Deleting ResourceHandle %s for deleted ResourceClaim %s in %s',
                handle_name, claim_name, claim_namespace
            )
            ResourceProvider.delete_resource_handle(handle_name, logger)

    @staticmethod
    def manage_claim_lost_resource(claim_namespace, claim_name, resource):
        resource_kind = resource['kind']
        resource_meta = resource['metadata']
        resource_name = resource_meta['name']
        resource_namespace = resource_meta.get('namespace', None)
        if resource_namespace:
            ko.logger.info('ResourceClaim {} in {} lost resource {} {} in {}'.format(
                claim_name, claim_namespace, resource_kind, resource_name, resource_namespace
            ))
        else:
            ko.logger.info('ResourceClaim {} in {} lost resource {} {}'.format(
                claim_name, claim_namespace, resource_kind, resource_name
            ))
            
        try:
            claim = ko.custom_objects_api.get_namespaced_custom_object(
                ko.operator_domain,
                'v1',
                claim_namespace,
                'resourceclaims',
                claim_name
            )
            old_resource = claim.get('status', {}).get('resource', None)
            if old_resource \
            and old_resource['metadata']['name'] == resource_name \
            and old_resource['metadata']['namespace'] == resource_namespace:
                ko.custom_objects_api.patch_namespaced_custom_object_status(
                    ko.operator_domain, 'v1', claim_namespace,
                    'resourceclaims', claim_name,
                    { 'status': { 'resource': None } }
                )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                ko.logger.info('ResourceClaim %s in %s not found', claim_name, claim_namespace)
            else:
                raise

    @staticmethod
    def manage_claim_resource_update(claim_namespace, claim_name, resource):
        resource_kind = resource['kind']
        resource_meta = resource['metadata']
        resource_name = resource_meta['name']
        resource_namespace = resource_meta.get('namespace', None)
        if resource_namespace:
            ko.logger.info('ResourceClaim {} in {} resource status change for {} {} in {}'.format(
                claim_name, claim_namespace, resource_kind, resource_name, resource_namespace
            ))
        else:
            ko.logger.info('ResourceClaim {} in {} resource status change for {} {}'.format(
                claim_name, claim_namespace, resource_kind, resource_name
            ))
            
        try:
            claim = ko.custom_objects_api.get_namespaced_custom_object(
                ko.operator_domain, 'v1', claim_namespace, 'resourceclaims', claim_name
            )
            ko.custom_objects_api.patch_namespaced_custom_object_status(
                ko.operator_domain, 'v1', claim_namespace, 'resourceclaims', claim_name,
                { 'status': { 'resource': resource } }
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                ko.logger.info('ResourceClaim %s in %s not found', claim_name, claim_namespace)
            else:
                raise

    @staticmethod
    def manage_handle(handle, logger):
        handle_meta = handle['metadata']

        if 'resourceClaim' in handle['spec']:
            claim = ResourceProvider.get_claim_for_handle(handle, logger)
            if not claim:
                ResourceProvider.delete_handle_on_lost_claim(handle, logger)
                return
        else:
            claim = None

        provider_name = handle['spec']['resourceProvider']['name']
        provider = ResourceProvider.find_provider_by_name(provider_name)
        if not provider:
            ResourceProvider.log_handle_event(
                handle, logger,
                "Unable to find ResourceProvider " + provider_name
            )
            return

        provider.manage_resource_for_handle(handle, claim, logger)

    @staticmethod
    def manage_handle_deleted(handle, logger):
        handle_meta = handle['metadata']
        handle_name = handle_meta['name']
        logger.info('ResourceHandle %s deleted', handle_name)

    @staticmethod
    def manage_handle_lost_resource(handle_name, resource):
        try:
            handle = ko.custom_objects_api.get_namespaced_custom_object(
                ko.operator_domain, 'v1', ko.operator_namespace,
                'resourcehandles', handle_name
            )
            if 'resource' in handle['spec'] \
            and 'deletionTimestamp' not in handle['metadata']:
                ko.custom_objects_api.patch_namespaced_custom_object(
                    ko.operator_domain, 'v1', ko.operator_namespace,
                    'resourcehandles', handle_meta['name'],
                    { 'spec': { 'resource': None } }
                )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    @staticmethod
    def manage_handle_pending_delete(handle, logger):
        handle_meta = handle['metadata']
        if 'resourceClaim' in handle['spec']:
            claim = ResourceProvider.get_claim_for_handle(handle, logger)
        else:
            claim = None

        provider_name = handle['spec']['resourceProvider']['name']
        provider = ResourceProvider.find_provider_by_name(provider_name)
        if provider:
            resource = provider.resource_definition_from_template(handle, claim, logger)
            resource_meta = resource['metadata']
            ko.delete_resource(
                resource['apiVersion'], resource['kind'],
                resource_meta['name'], resource_meta.get('namespace', None)
            )
        else:
            ResourceProvider.log_handle_event(
                handle, logger,
                "Unable to find ResourceProvider " + provider_name
            )
        ko.custom_objects_api.patch_namespaced_custom_object(
            ko.operator_domain, 'v1', ko.operator_namespace,
            'resourcehandles', handle_meta['name'],
            { 'metadata': { 'finalizers': None } }
        )

    @staticmethod
    def register_provider(provider):
        provider = ResourceProvider(provider)
        ResourceProvider.providers[provider.name] = provider

    @staticmethod
    def reject_claim(msg, claim, logger):
        logger.info(
            "Rejecting ResourceClaim %s in %s: %s",
            claim['metadata']['name'],
            claim['metadata']['namespace'],
            msg
        )
        # FIXME - Create event for claim

    @staticmethod
    def start_resource_watch(resource_definition):
        api_version = resource_definition['apiVersion']
        metadata = resource_definition['metadata']
        kind = resource_definition['kind']
        namespace = metadata.get('namespace', None)

        if namespace:
            watcher_name = '{}:{}:{}'.format(api_version, kind, namespace)
        else:
            watcher_name = '{}:{}'.format(api_version, kind)

        if watcher_name in ResourceProvider.resource_watchers:
            return

        if '/' in api_version:
            group, version = api_version.split('/')
        else:
            group, version = None, api_version

        w = ko.watcher(
            name=watcher_name,
            plural=ko.kind_to_plural(group, version, kind),
            group=group,
            namespace=namespace,
            version=version
        )
        ResourceProvider.resource_watchers[watcher_name] = w
        w.handler = ResourceProvider.watch_resource_event
        w.start()

    @staticmethod
    def watch_resource_event(event):
        event_type = event['type']
        if event_type in ['ADDED', 'DELETED', 'MODIFIED']:
            resource = event['object']
            metadata = resource['metadata']
            annotations = metadata.get('annotations', {})
            annotation_prefix = ko.operator_domain + '/resource-'
            handle_name = annotations.get(annotation_prefix + 'handle-namespace', None)
            handle_namespace = annotations.get(annotation_prefix + 'handle-namespace', None)
            claim_name = annotations.get(annotation_prefix + 'claim-name', None)
            claim_namespace = annotations.get(annotation_prefix + 'claim-namespace', None)

            if not handle_name \
            or handle_namespace != ko.operator_namespace:
                return

            if event_type == 'DELETED':
                ResourceProvider.manage_handle_lost_resource(handle_name, resource)

            if claim_name and claim_namespace:
                if event_type == 'DELETED':
                    ResourceProvider.manage_claim_lost_resource(claim_namespace, claim_name, resource)
                else:
                    ResourceProvider.manage_claim_resource_update(claim_namespace, claim_name, resource)
        else:
            ko.logger.warning(event)

    def __init__(self, provider):
        self.metadata = provider['metadata']
        self.spec = provider['spec']
        self.__init_template_validator()

    def __init_template_validator(self):
        self.template_validator = openapi_core.shortcuts.RequestValidator(
            openapi_core.shortcuts.create_spec({
                "openapi": "3.0.0",
                "info": { "title": "", "version": "0.1" },
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
                        "ClaimTemplate": self.spec['validation']['openAPIV3Schema']
                    }
                }
            })
        )

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def override(self):
        return self.spec.get('override', {})

    def bind_available_handle_to_claim(self, claim):
        # FIXME
        pass

    def bind_or_create_handle_for_claim(self, claim, logger):
        claim_name = claim['metadata']['name']
        claim_namespace = claim['metadata']['namespace']
        handle = self.get_handle_for_claim(claim)
        if handle:
            logger.warning(
                "Found unlisted ResourceHandle %s for ResourceClaim %s/%s",
                handle['metadata']['name'],
                claim_namespace,
                claim_name
            )
        else:
            handle = self.bind_available_handle_to_claim(claim)
            if not handle:
                handle = self.create_handle_for_claim(claim)
        self.set_handle_in_claim_status(claim, handle, logger)

    def create_handle_for_claim(self, claim):
        provider_name = self.name
        provider_namespace = self.namespace
        claim_name = claim['metadata']['name']
        claim_namespace = claim['metadata']['namespace']

        return ko.custom_objects_api.create_namespaced_custom_object(
            ko.operator_domain,
            'v1',
            ko.operator_namespace,
            'resourcehandles',
            {
                'apiVersion': ko.operator_domain + '/v1',
                'kind': 'ResourceHandle',
                'metadata': {
                    'finalizers': [
                        ko.operator_domain + '/resource-claim-operator'
                    ],
                    'generateName': 'guid-',
                    'labels': {
                        ko.operator_domain + '/resource-claim-namespace': claim_namespace,
                        ko.operator_domain + '/resource-claim-name': claim_name,
                        ko.operator_domain + '/resource-provider-namespace': provider_namespace,
                        ko.operator_domain + '/resource-provider-name': provider_name
                    }
                },
                'spec': {
                    'resourceClaim': {
                        'apiVersion': 'v1',
                        'kind': 'ResourceClaim',
                        'name': claim_name,
                        'namespace': claim_namespace
                    },
                    'resourceProvider': {
                        'apiVersion': 'v1',
                        'kind': 'ResourceProvider',
                        'name': provider_name,
                        'namespace': provider_namespace
                    },
                    'template': claim['spec']['template']
                }
            }
        )

    def create_resource_for_handle(self, handle, resource_definition, logger):
        resource_meta = resource_definition['metadata']
        resource_ref = {
            'apiVersion': resource_definition['apiVersion'],
            'kind': resource_definition['kind'],
            'name': resource_meta['name']
        }
        if 'namespace' in resource_meta:
            resource_ref['namespace'] = resource_meta['namespace']

        handle = ko.custom_objects_api.patch_namespaced_custom_object(
            ko.operator_domain,
            'v1',
            ko.operator_namespace,
            'resourcehandles',
            handle['metadata']['name'],
            { 'spec': { 'resource': resource_ref } }
        )
        version_annotation = ko.operator_domain + '/resource-handle-version'
        resource_definition['metadata']['annotations'][version_annotation] = \
            handle['metadata']['resourceVersion']

        ko.create_resource(resource_definition)

    def get_handle_for_claim(self, claim):
        claim_name = claim['metadata']['name']
        claim_namespace = claim['metadata']['namespace']
        items = ko.custom_objects_api.list_namespaced_custom_object(
             ko.operator_domain,
             'v1',
             ko.operator_namespace,
             'resourcehandles',
             label_selector='{0}/resource-claim-namespace={1},{0}/resource-claim-name={2}'.format(
                 ko.operator_domain, claim_namespace, claim_name
             )
        ).get('items', [])
        if items:
            return items[0]

    def initialize_claim(self, claim, logger):
        # FIXME - log
        init_timestamp = datetime.datetime.utcnow().strftime('%FT%TZ')
        update = {
            "metadata": {
                "annotations": {
                    ko.operator_domain + '/resource-claim-init-timestamp': init_timestamp,
                    ko.operator_domain + '/resource-provider-namespace': self.namespace,
                    ko.operator_domain + '/resource-provider-name': self.name
                }
            }
        }
        if 'default' in self.spec:
            update['spec'] = {
                'template': recursive_process_template_strings(
                    self.spec['default'],
                    { 'resource_claim': claim, 'resource_provider': self }
                )
            }
        return ko.patch_resource(
            claim, update, [{ 'pathMatch': '/.*', 'allowedOps': ['add','replace'] }]
        )

    def initialize_claim_status(self, claim, logger):
        ko.custom_objects_api.patch_namespaced_custom_object_status(
            ko.operator_domain, 'v1', claim['metadata']['namespace'],
            'resourceclaims', claim['metadata']['name'],
            {
                'status': {
                    'resourceProvider': {
                        'apiVersion': 'v1',
                        'kind': 'ResourceProvider',
                        'name': self.name,
                        'namespace': self.namespace
                    }
                }
            }
        )

    def is_match_for_template(self, template):
        cmp_template = copy.deepcopy(template)
        dict_merge(cmp_template, self.spec['match'])
        return template == cmp_template

    def _manage_claim(self, claim, logger):
        annotations = claim['metadata'].get('annotations', {})
        if ko.operator_domain + '/resource-claim-init-timestamp' not in annotations:
            self.initialize_claim(claim, logger)
            return

        error_msg = self.validate_claim(claim, logger)
        if error_msg:
            ResourceProvider.log_claim_event(
                claim, logger, 'ResourceClaim validation error - ' + error_msg
            )
            return

        if 'resourceHandle' in claim['status']:
            self.manage_handle_for_claim(claim, logger)
        else:
            self.bind_or_create_handle_for_claim(claim, logger)

    def manage_handle_for_claim(self, claim, logger):
        handle_name = claim['status']['resourceHandle']['name']
        try:
            handle = ko.custom_objects_api.get_namespaced_custom_object(
                ko.operator_domain, 'v1', ko.operator_namespace, 'resourcehandles', handle_name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                ResourceProvider.log_claim_event(
                    claim, logger, 'Missing ResourceHandle ' + handle_name
                )
                return
            else:
                raise
        self.update_handle_for_claim(claim, handle, logger)

    def manage_resource_for_handle(self, handle, claim, logger):
        resource_definition = self.resource_definition_from_template(handle, claim, logger)
        ResourceProvider.start_resource_watch(resource_definition)
        if 'resource' in handle['spec']:
            resource_ref = handle['spec']['resource']
            resource = ko.get_resource(
                api_version = resource_ref['apiVersion'],
                kind = resource_ref['kind'],
                name = resource_ref['name'],
                namespace = resource_ref.get('namespace', None)
            )
            if resource:
                self.update_resource_for_handle(handle, resource, resource_definition, logger)
            else:
                ko.custom_objects_api.patch_namespaced_custom_object(
                    ko.operator_domain, 'v1', ko.operator_namespace,
                    'resourcehandles', handle_meta['name'],
                    { 'spec': { 'resource': None } }
                )
        else:
            self.create_resource_for_handle(handle, resource_definition, logger)

    def resource_definition_from_template(self, handle, claim, logger):
        if claim:
            requester_identity, requester_user = ResourceProvider.get_requester_from_namespace(
                claim['metadata']['namespace']
            )
        else:
            requester_identity = requester_user = None

        guid = handle['metadata']['name'][-5:]
        resource = copy.deepcopy(handle['spec']['template'])
        if 'override' in self.spec:
            dict_merge(resource, self.override)
        if 'metadata' not in resource:
            resource['metadata'] = {}
        if 'generateName' not in resource['metadata']:
            resource['metadata']['generateName'] = 'guid-'
        resource['metadata']['name'] = \
            resource['metadata']['generateName'] + guid
        if 'annotations' not in resource['metadata']:
            resource['metadata']['annotations'] = {}
        resource['metadata']['annotations'].update({
            ko.operator_domain + '/resource-provider-name': self.name,
            ko.operator_domain + '/resource-provider-namespace': self.namespace,
            ko.operator_domain + '/resource-handle-name': handle['metadata']['name'],
            ko.operator_domain + '/resource-handle-namespace': handle['metadata']['namespace'],
            ko.operator_domain + '/resource-handle-uid': handle['metadata']['uid'],
            ko.operator_domain + '/resource-handle-version': handle['metadata']['resourceVersion']
        })
        if claim:
            resource['metadata']['annotations'].update({
                ko.operator_domain + '/resource-claim-name': claim['metadata']['name'],
                ko.operator_domain + '/resource-claim-namespace': claim['metadata']['namespace'],
            })
        if requester_identity:
            resource['metadata']['annotations'].update({
                runtime.operator_domain + '/resource-requester-email':
                    requester_identity.get('extra', {}).get('email', ''),
                runtime.operator_domain + '/resource-requester-name':
                    requester_identity.get('extra', {}).get('name', ''),
                runtime.operator_domain + '/resource-requester-preferred-username':
                    requester_identity.get('extra', {}).get('preferred_username', ''),
            })
        if requester_user:
            resource_definition['metadata']['annotations'].update({
                runtime.operator_domain + '/resource-requester-user':
                    requester_user['metadata']['name']
            })

        return recursive_process_template_strings(resource, {
            "requester_identity": requester_identity,
            "requester_user": requester_user,
            "resource_provider": self,
            "resource_handle": handle,
            "resource_claim": claim
        })

    def set_handle_in_claim_status(self, claim, handle, logger):
        claim_meta = claim['metadata']
        handle_meta = handle['metadata']
        handle_name = handle_meta['name']
        handle_namespace = handle_meta['namespace']

        ko.custom_objects_api.patch_namespaced_custom_object_status(
            ko.operator_domain, 'v1', claim_meta['namespace'],
            'resourceclaims', claim_meta['name'],
            {
                'status': {
                    'resourceHandle': {
                        'apiVersion': 'v1',
                        'kind': 'ResourceHandle',
                        'name': handle_name,
                        'namespace': handle_namespace
                    }
                }
            }
        )

        ResourceProvider.log_claim_event(
            claim, logger, 'Bound to resourceHandle ' + handle_name
        )

    def update_handle_for_claim(self, claim, handle, logger):
        claim_template = claim['spec']['template']
        handle_name = handle['metadata']['name']

        if handle['spec']['template'] == claim_template:
            return

        ko.custom_objects_api.patch_namespaced_custom_object(
            ko.operator_domain, 'v1', ko.operator_namespace,
            'resourcehandles', handle_name,
            { 'spec': { 'template': claim_template } }
        )
        ResourceProvider.log_claim_event(
            claim, logger, 'Update ResourceHandle' + handle_name
        )

    def update_resource_for_handle(self, handle, resource, resource_definition, logger):
        handle_uid = handle['metadata']['uid']
        handle_version = handle['metadata']['resourceVersion']

        annotations = resource['metadata'].get('annotations', {})
        if handle_uid != annotations.get(ko.operator_domain + '/resource-handle-uid') \
        or handle_version != annotations.get(ko.operator_domain + '/resource-handle-version'):
            update_filters = self.spec.get('updateFilters', []) + [{
                'pathMatch': '/metadata/annotations/' + re.escape(ko.operator_domain) + '~1resource-.*'
            }]
            ko.patch_resource(
                resource=resource,
                patch=resource_definition,
                update_filters=update_filters
            )

    def validate_claim(self, claim, logger):
        claim_meta = claim['metadata']
        claim_spec = claim['spec']
        try:
            self.validate_resource_template(claim_spec['template'])
            # FIXME - Add custom validation
        except Exception as e:
            return str(e)

    def validate_resource_template(self, template):
        validation_result = self.template_validator.validate(
            openapi_core.wrappers.mock.MockRequest(
                'http://example.com', 'post', '/claimTemplate',
                path_pattern='/claimTemplate',
                data=json.dumps(template)
            )
        )
        validation_result.raise_for_errors()

@kopf.on.event(ko.operator_domain, 'v1', 'resourceproviders')
def watch_providers(event, logger, **_):
    if event['type']:
        logger.warning(event)
    else:
        provider = event['object']
        provider_name = provider['metadata']['name']
        provider_namespace = provider['metadata']['namespace']
        if provider_namespace == ko.operator_namespace:
            ResourceProvider.register_provider(provider)
            logger.info('Discovered ResourceProvider %s', provider_name)
        else:
            logger.warning(
                'Ignoring ResourceProvider %s in namespace %s',
                provider_name, provider_namespace
            )

@kopf.on.event(ko.operator_domain, 'v1', 'resourceclaims')
def watch_resource_claims(event, logger, **_):
    pause_for_provider_init()
    logger.warning(event)
    if event['type'] == 'DELETED':
        claim = event['object']
        ResourceProvider.manage_claim_deleted(claim, logger)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        claim = event['object']
        if claim['status']:
            ResourceProvider.manage_claim(claim, logger)
        else:
            ResourceProvider.manage_claim_create(claim, logger)
    else:
        logger.warning(event)

@kopf.on.event(ko.operator_domain, 'v1', 'resourcehandles')
def watch_resource_handles(event, logger, **_):
    pause_for_provider_init()
    if event['type'] == 'DELETED':
        handle = event['object']
        ResourceProvider.manage_handle_deleted(handle, logger)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        handle = event['object']
        if 'deletionTimestamp' in handle['metadata']:
            ResourceProvider.manage_handle_pending_delete(handle, logger)
        else:  
            ResourceProvider.manage_handle(handle, logger)
