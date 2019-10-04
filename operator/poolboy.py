#!/usr/bin/env python

import copy
import datetime
import gpte.kubeoperative
import json
import jsonpatch
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

ko = gpte.kubeoperative.KubeOperative(
    operator_domain = os.environ.get('OPERATOR_DOMAIN', 'poolboy.gpte.redhat.com')
)
providers = {}
provider_init_delay = int(os.environ.get('PROVIDER_INIT_DELAY', 10))
start_time = time.time()

def add_finalizer_to_pool(pool, logger):
    pool_meta = pool['metadata']
    pool_namespace = pool_meta['namespace']
    pool_name = pool_meta['name']
    ko.custom_objects_api.patch_namespaced_custom_object(
        ko.operator_domain, ko.version, pool_namespace, 'resourcepools', pool_name,
        { 'metadata': { 'finalizers': [ko.operator_domain] } }
    )

def bind_handle_to_claim(handle, claim, logger):
    claim_meta = claim['metadata']
    claim_namespace = claim_meta['namespace']
    claim_name = claim_meta['name']
    handle_meta = handle['metadata']
    handle_name = handle_meta['name']

    logger.info(
        'binding ResourceHandle %s to RsourceClaim %s in %s',
        handle_name, claim_name, claim_namespace
    )

    return ko.custom_objects_api.patch_namespaced_custom_object(
        ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', handle_name,
        {
            'metadata': {
                'labels': {
                    ko.operator_domain + '/resource-claim-name': claim_name,
                    ko.operator_domain + '/resource-claim-namespace': claim_namespace
                }
            },
            'spec': {
                'resourceClaim': {
                    'apiVersion': ko.api_version,
                    'kind': 'ResourceClaim',
                    'name': claim_name,
                    'namespace': claim_namespace
                }
            }
        }
    )

def create_handle_for_claim(claim, logger):
    """
    Create resoruce handle for claim, called after claim is validated and failed
    to match an available handel.
    """
    claim_name = claim['metadata']['name']
    claim_namespace = claim['metadata']['namespace']
    claim_resource_statuses = claim['status']['resources']

    resources = []
    for i, claim_resource in enumerate(claim['spec']['resources']):
        resources.append({
            'provider': claim_resource_statuses[i]['provider'],
            'template': claim_resource['template']
        })

    return ko.custom_objects_api.create_namespaced_custom_object(
        ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles',
        {
            'apiVersion': ko.api_version,
            'kind': 'ResourceHandle',
            'metadata': {
                'finalizers': [ ko.operator_domain ],
                'generateName': 'guid-',
                'labels': {
                    ko.operator_domain + '/resource-claim-name': claim_name,
                    ko.operator_domain + '/resource-claim-namespace': claim_namespace
                }
            },
            'spec': {
                'resourceClaim': {
                    'apiVersion': ko.api_version,
                    'kind': 'ResourceClaim',
                    'name': claim_name,
                    'namespace': claim_namespace
                },
                'resources': resources
            }
        }
    )

def create_handle_for_pool(pool, logger):
    pool_meta = pool['metadata']
    pool_spec = pool['spec']
    pool_namespace = pool_meta['namespace']
    pool_name = pool_meta['name']

    return ko.custom_objects_api.create_namespaced_custom_object(
        ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles',
        {
            'apiVersion': ko.api_version,
            'kind': 'ResourceHandle',
            'metadata': {
                'finalizers': [
                    ko.operator_domain
                ],
                'generateName': 'guid-',
                'labels': {
                    ko.operator_domain + '/resource-pool-name': pool_name,
                    ko.operator_domain + '/resource-pool-namespace': pool_namespace
                }
            },
            'spec': {
                'resourcePool': {
                    'apiVersion': ko.api_version,
                    'kind': 'ResourcePool',
                    'name': pool_name,
                    'namespace': pool_namespace
                },
                'resources': pool_spec['resources']
            }
        }
    )

def delete_resource_handle(name, logger):
    try:
        ko.custom_objects_api.delete_namespaced_custom_object(
            ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', name,
            kubernetes.client.V1DeleteOptions()
        )
    except kubernetes.client.rest.ApiException as e:
        if e.status != 404:
            raise

def delete_unbound_handles_for_pool(pool, logger):
    pool_meta = pool['metadata']
    pool_name = pool_meta['name']
    for handle in get_unbound_handles_for_pool(pool_name, logger):
         handle_meta = handle['metadata']
         handle_name = handle_meta['name']
         ko.custom_objects_api.delete_namespaced_custom_object(
             ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', handle_name,
             kubernetes.client.V1DeleteOptions()
         )

def get_claim_for_handle(handle, logger):
    if 'resourceClaim' not in handle['spec']:
        return None

    claim_ref = handle['spec']['resourceClaim']
    claim_name = claim_ref['name']
    claim_namespace = claim_ref['namespace']
    try:
        return ko.custom_objects_api.get_namespaced_custom_object(
            ko.operator_domain, ko.version, claim_namespace,
            'resourceclaims', claim_name
        )
    except kubernetes.client.rest.ApiException as e:
        if e.status == 404:
            return None
        else:
            raise

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
            'user.openshift.io', 'v1', 'users', requester_user_name
        )
        if requester_user.get('identities', None):
            requester_identity = ko.custom_objects_api.get_cluster_custom_object(
                'user.openshift.io', 'v1', 'identities', requester_user['identities'][0]
            )
    except kubernetes.client.rest.ApiException as e:
        if e.status != 404:
            raise

    return requester_identity, requester_user

def get_resource_handle(name, logger):
    try:
        return ko.custom_objects_api.get_namespaced_custom_object(
            ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', name
        )
    except kubernetes.client.rest.ApiException as e:
        if e.status == 404:
            return None
        else:
            raise

def get_unbound_handles_for_pool(pool_name, logger):
    return ko.custom_objects_api.list_namespaced_custom_object(
        ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles',
        label_selector='{0}/resource-pool-name={1},!{0}/resource-claim-name'.format(
            ko.operator_domain, pool_name
        )
    ).get('items', [])

def log_claim_event(claim, logger, msg):
    logger.info(
        "ResourceClaim %s in %s: %s",
        claim['metadata']['name'],
        claim['metadata']['namespace'],
        msg
    )
    # FIXME - Create event for claim

def log_handle_event(handle, logger, msg):
    logger.info(
        "ResourceHandle %s: %s",
        handle['metadata']['name'],
        msg
    )
    # FIXME - Create event for handle

def manage_claim(claim, logger):
    """
    Called on each ResourceClaim event
    """
    claim_status = claim.get('status', None)
    if not claim_status:
        manage_claim_create(claim, logger)
        return

    annotations = claim['metadata'].get('annotations', {})
    if ko.operator_domain + '/resource-claim-init-timestamp' not in annotations:
        manage_claim_init(claim, logger)
    elif validate_claim(claim, logger):
        if 'resourceHandle' not in claim_status:
            manage_claim_bind(claim, logger)
        else:
            manage_claim_update(claim, logger)

def manage_claim_bind(claim, logger):
    """
    Called on claim event if ResourceClaim is not bound to a ResourceHandle
    """
    handle = match_handle_to_claim(claim, logger)
    if handle:
        bind_handle_to_claim(handle, claim, logger)
    else:
        handle = create_handle_for_claim(claim, logger)

    claim_meta = claim['metadata']
    handle_meta = handle['metadata']
    handle_name = handle_meta['name']
    ko.custom_objects_api.patch_namespaced_custom_object_status(
        ko.operator_domain, ko.version, claim_meta['namespace'], 'resourceclaims', claim_meta['name'],
        {
            'status': {
                'resourceHandle': {
                    'apiVersion': ko.api_version,
                    'kind': 'ResourceHandle',
                    'name': handle_name,
                    'namespace': handle_meta['namespace']
                }
            }
        }
    )

    log_claim_event(
        claim, logger, 'Bound ResourceHandle ' + handle_name
    )

def manage_claim_create(claim, logger):
    """
    Called on ch claim event if the claim does not have a status
    This method will attempt to match ResourceProviders to each resource
    for the claim and set the names of the resource providers in the
    status.
    """
    claim_meta = claim['metadata']
    claim_spec = claim['spec']

    resources = claim_spec.get('resources', None)
    if not resources:
        log_claim_event(claim, logger, 'no resources found')
        return

    resource_providers = []
    for i, resource in enumerate(resources):
        # Use specified provider if given
        provider_name = resource.get('provider', {}).get('name')
        if provider_name:
            provider = ResourceProvider.find_provider_by_name(provider_name)
            if not provider:
                log_claim_event(
                    claim, logger, 'ResourceProvider {} not found'.format(provider_name)
                )
                return
            resource_providers.append(provider)
        else:
            provider = ResourceProvider.find_provider_by_template_match(resource['template'])
            if not provider:
                log_claim_event(
                    claim, logger,
                    'Unable to match spec.resources[{}].template to ResourceProvider'.format(i)
                )
                return
            resource_providers.append(provider)

    ko.custom_objects_api.patch_namespaced_custom_object_status(
        ko.operator_domain, ko.version, claim_meta['namespace'], 'resourceclaims', claim_meta['name'],
        {
            'status': {
                'resources': [{
                    'provider': {
                        'apiVersion': ko.api_version,
                        'kind': 'ResourceProvider',
                        'name': provider.name,
                        'namespace': provider.namespace
                    },
                    'resource': None
                } for provider in resource_providers]
            }
        }
    )

def manage_claim_deleted(claim, logger):
    claim_meta = claim['metadata']
    claim_name = claim_meta['name']
    claim_namespace = claim_meta['namespace']
    handle_ref = claim.get('status', {}).get('resourceHandle', None)

    if handle_ref:
        handle_name = handle_ref['name']
        logger.info('Propagating delete to ResourceHandle %s', handle_name)
        delete_resource_handle(handle_name, logger)

def manage_claim_init(claim, logger):
    """
    Called after claim has resources matched to providers but
    resource-claim-init-timestamp annotation is not yet set.
    """
    claim_resources = claim['spec'].get('resources', [])
    claim_status_resources = claim['status'].get('resources', [])
    claim_resources_update = []
    update = {
        "metadata": {
            "annotations": {
                ko.operator_domain + '/resource-claim-init-timestamp':
                    datetime.datetime.utcnow().strftime('%FT%TZ')
            }
        },
        'spec': { 'resources': claim_resources_update }
    }
    for i, resource in enumerate(claim_resources):
        try:
            claim_status_resource = claim_status_resources[i]
            provider_ref = claim_status_resource['provider']
            provider = ResourceProvider.find_provider_by_name(provider_ref['name'])
            if not provider:
                log_claim_event(
                    claim, logger,
                    "Unable to find ResourceProvider " + provider_name
                )
                return
            else:
                claim_resources_update.append({
                    'provider': provider_ref,
                    'template': provider.resource_claim_template_defaults(
                        resource_claim = claim,
                        resource_index = i
                    )
                })

        except IndexError:
            logger.warning('ResourceClaim has more resources in spec than resourceProviders in status!')
            return

    ko.patch_resource(
        claim, update, [
            # Update anything in metadata on init
            { 'pathMatch': '/metadata/.*', 'allowedOps': ['add', 'replace'] },
            # Update anything in resources[].provider
            { 'pathMatch': '/spec/resources/[0-9]+/provider(/.*)?', 'allowedOps': ['add', 'replace'] },
            # Only process default overrides in template
            { 'pathMatch': '/spec/resources/[0-9]+/template/.*', 'allowedOps': ['add'] }
        ]
    )

def manage_claim_resource_delete(claim_namespace, claim_name, resource, resource_index):
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
            ko.operator_domain, ko.version, claim_namespace, 'resourceclaims', claim_name
        )
        status_resources = claim['status']['resources']
        status_resource = status_resources[resource_index].get('state', None)

        if status_resource \
        and status_resource['metadata']['name'] == resource_name \
        and status_resource['metadata']['namespace'] == resource_namespace:
            status_resources[resource_index]['state'] = resource
            ko.custom_objects_api.patch_namespaced_custom_object_status(
                ko.operator_domain, ko.version, claim_namespace, 'resourceclaims', claim_name,
                { 'status': { 'resources': status_resources } }
            )
    except (IndexError, KeyError):
        pass
    except kubernetes.client.rest.ApiException as e:
        if e.status != 404:
            raise

def manage_claim_resource_update(claim_namespace, claim_name, resource, resource_index):
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
            ko.operator_domain, ko.version, claim_namespace, 'resourceclaims', claim_name
        )
        status_resources = claim['status']['resources']
        status_resource = status_resources[resource_index].get('state', None)

        if not status_resource or (
            status_resource['metadata']['name'] == resource_name and
            status_resource['metadata']['namespace'] == resource_namespace and
            status_resource != resource
        ):
            status_resources[resource_index]['state'] = resource
            ko.custom_objects_api.patch_namespaced_custom_object_status(
                ko.operator_domain, ko.version, claim_namespace, 'resourceclaims', claim_name,
                { 'status': { 'resources': status_resources } }
            )
    except (IndexError, KeyError):
        pass
    except kubernetes.client.rest.ApiException as e:
        if e.status == 404:
            ko.logger.info('ResourceClaim %s in %s not found', claim_name, claim_namespace)
        else:
            raise

def manage_claim_update(claim, logger):
    """
    Called on each claim event once ResourceClaim is bound to a ResourceHandle

    Claim has already been validated. Propagate changes from claim to handle.
    """
    handle_name = claim['status']['resourceHandle']['name']
    resource_handle = get_resource_handle(handle_name, logger)
    if not resource_handle:
         log_claim_event(
             claim, logger, 'ResourceHandle {} has been lost'.format(handle_name)
         )
         return

    have_update = False
    handle_resources = resource_handle['spec']['resources']
    for i, claim_resource in enumerate(claim['spec']['resources']):
        handle_resource = handle_resources[i]
        if handle_resource['template'] != claim_resource['template']:
            handle_resource['template'] = claim_resource['template']
            have_update = True

    if have_update:
        ko.custom_objects_api.patch_namespaced_custom_object(
            ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', handle_name,
            { 'spec': { 'resources': handle_resources } }
        )

def manage_handle(handle, logger):
    """
    Called on all ResourceHandle events except delete
    """
    handle_meta = handle['metadata']
    handle_name = handle_meta['name']
    if 'deletionTimestamp' in handle['metadata']:
        manage_handle_pending_delete(handle, logger)
        return
    elif 'resourceClaim' in handle['spec']:
        claim = get_claim_for_handle(handle, logger)
        if not claim:
            delete_resource_handle(handle_name, logger)
            return
    else:
        claim = None

    providers = []
    handle_resources = handle['spec']['resources']
    for handle_resource in handle_resources:
        provider_name = handle_resource['provider']['name']
        provider = ResourceProvider.find_provider_by_name(provider_name)
        if provider:
            providers.append(provider)
        else:
            log_handle_event(handle, logger, 'Unable to find ResourceProvider ' + provider_name)
            return

    have_handle_update = False
    resources_to_create = []
    for i, handle_resource in enumerate(handle_resources):
        provider = providers[i]

        if provider.resource_requires_claim and not claim:
            continue

        resource_definition = provider.resource_definition_from_template(
            handle, claim, i, logger
        )
        start_resource_watch(resource_definition)

        resource_api_version = resource_definition['apiVersion']
        resource_kind = resource_definition['kind']
        resource_name = resource_definition['metadata']['name']
        resource_namespace = resource_definition['metadata'].get('namespace', None)

        reference = {
            'apiVersion': resource_definition['apiVersion'],
            'kind': resource_definition['kind'],
            'name': resource_definition['metadata']['name']
        }
        if 'namespace' in resource_definition['metadata']:
            reference['namespace'] = resource_definition['metadata']['namespace']

        if reference != handle_resource.get('reference', None):
            have_handle_update = True
            handle_resource['reference'] = reference

        resource = ko.get_resource(
            api_version = resource_api_version,
            kind = resource_kind,
            name = resource_name,
            namespace = resource_namespace
        )

        if resource:
            provider.update_resource(handle, resource, resource_definition, logger)
        else:
            resources_to_create.append(resource_definition)

    if have_handle_update:
        handle = ko.custom_objects_api.patch_namespaced_custom_object(
            ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', handle_name,
            { 'spec': { 'resources': handle_resources } }
        )
    for resource_definition in resources_to_create:
        resource_definition['metadata']['annotations'][ko.operator_domain + '/resource-handle-version'] = \
            handle['metadata']['resourceVersion']
        ko.create_resource(resource_definition)

def manage_handle_deleted(handle, logger):
    handle_meta = handle['metadata']
    handle_name = handle_meta['name']
    logger.info('ResourceHandle %s deleted', handle_name)

def manage_handle_lost_resource(handle_name, resource, resource_index):
    try:
        handle = ko.custom_objects_api.get_namespaced_custom_object(
            ko.operator_domain, ko.version, ko.operator_namespace,
            'resourcehandles', handle_name
        )
        if 'deletionTimestamp' not in handle['metadata']:
            return

        reference = handle['spec']['resources'][resource_index]['reference']
        if reference['apiVersion'] == resource['apiVersion'] \
        and reference['kind'] == resource['kind'] \
        and reference['name'] == resource['metadata']['name'] \
        and reference['namespace'] == resource['metadata']['namespace']:
            resources_update = [{} for i in range(resource_index)]
            resources_update[resource_index] = {'reference': None}
            ko.custom_objects_api.patch_namespaced_custom_object(
                ko.operator_domain, ko.version, ko.operator_namespace,
                'resourcehandles', handle_meta['name'],
                { 'spec': { 'resources': resources_update } }
            )
    except IndexError:
        pass
    except KeyError:
        pass
    except kubernetes.client.rest.ApiException as e:
        if e.status != 404:
            raise

def manage_handle_pending_delete(handle, logger):
    for resource in handle['spec']['resources']:
        reference = resource.get('reference', None)
        if reference:
            ko.delete_resource(
                reference['apiVersion'], reference['kind'],
                reference['name'], reference.get('namespace', None)
            )
    ko.custom_objects_api.patch_namespaced_custom_object(
        ko.operator_domain, ko.version, ko.operator_namespace,
        'resourcehandles', handle['metadata']['name'],
        { 'metadata': { 'finalizers': None } }
    )

def manage_pool(pool, logger):
    pool_meta = pool['metadata']
    pool_namespace = pool_meta['namespace']
    pool_name = pool_meta['name']

    if pool_namespace != ko.operator_namespace:
        logger.info('Ignoring ResourcePool %s in namespace %s', pool_name, pool_namespace)
        return

    if not pool['metadata'].get('finalizers', None):
        add_finalizer_to_pool(pool, logger)
        return

    unbound_handle_count = len(get_unbound_handles_for_pool(pool_name, logger))
    handle_deficit = pool['spec'].get('minAvailable', 0) - unbound_handle_count
    if handle_deficit <= 0:
        return
    for i in range(handle_deficit):
         create_handle_for_pool(pool, logger)

def manage_pool_deleted(pool, logger):
    pool_meta = pool['metadata']
    pool_name = pool_meta['name']
    logger.info('ResourcePool %s deleted', pool_name)

def manage_pool_pending_delete(pool, logger):
    pool_meta = pool['metadata']
    pool_name = pool_meta['name']
    delete_unbound_handles_for_pool(pool, logger)
    handle = ko.custom_objects_api.patch_namespaced_custom_object(
        ko.operator_domain, ko.version, ko.operator_namespace, 'resourcepools', pool_name,
        { 'metadata': { 'finalizers': None } }
    )

def match_handle_to_claim(claim, logger):
    """
    List unbound ResourceHandles and attempt to match one to ResourceClaim

    The claim may specify a specific resource pool in an annotation to restrict
    the search to a specific pool.
    """
    claim_meta = claim['metadata']
    annotations = claim_meta.get('annotations', {})
    pool_name = annotations.get(ko.operator_domain + '/resource-pool-name', None)
    if pool_name:
        label_selector = '!{0}/resource-claim-name,{0}/resource-pool-name={1}'.format(
            ko.operator_domain, pool_name
        )
    else:
        label_selector = '!{0}/resource-claim-name'.format(ko.operator_domain)

    for handle in ko.custom_objects_api.list_namespaced_custom_object(
        ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles',
        label_selector=label_selector
    ).get('items', []):
        claim_resources = claim['spec']['resources']
        status_resources = claim['status']['resources']
        handle_resources = handle['spec']['resources']
        if len(claim_resources) != len(handle_resources):
            # Claim cannot match handle if there is a different resource count
            continue

        is_match = True
        for i, claim_resource in enumerate(claim_resources):
            handle_resource = handle_resources[i]
            provider_name = status_resources[i]['provider']['name']
            if provider_name != handle_resource['provider']['name']:
                is_match = False
                break
            provider = ResourceProvider.find_provider_by_name(provider_name)
            if not provider.check_template_match(handle_resource['template'], claim_resource['template'], logger):
                is_match = False
                break
        if is_match:
            return handle

def pause_for_provider_init():
    if time.time() < start_time + provider_init_delay:
        time.sleep(time.time() - start_time)

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
    w.handler = watch_resource_event
    w.start()

def validate_claim(claim, logger):
    """
    Check claim validity against providers for resources
    """
    resources = claim['spec'].get('resources', [])
    status_resources = claim['status'].get('resources', [])

    if len(status_resources) != len(resources):
        logger.warning('Number of resources in status does not match resources in spec')
        return False

    for i, resource in enumerate(resources):
        try:
            status_resource = status_resources[i]
            provider_name = status_resource['provider']['name']
            provider = ResourceProvider.find_provider_by_name(provider_name)
            if not provider:
                logger.warning('Unable to find ResourceProvider %s', provider_name)
                return False
            validation_error = provider.validate_resource_template(resource['template'], logger)
            if validation_error:
                logger.warning('Validation failure for spec.resources[%s].template: %s', i, validation_error)
                return False
        except IndexError:
            logger.warning('ResourceClaim has more resources than resourceProviders!')
            return False
    return True

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
        resource_index = int(annotations.get(annotation_prefix + 'index', None))

        if not handle_name \
        or handle_namespace != ko.operator_namespace:
            return

        if event_type == 'DELETED':
            manage_handle_lost_resource(handle_name, resource, resource_index)

        if claim_name and claim_namespace:
            if event_type == 'DELETED':
                manage_claim_resource_delete(claim_namespace, claim_name, resource, resource_index)
            else:
                manage_claim_resource_update(claim_namespace, claim_name, resource, resource_index)
    else:
        ko.logger.warning(event)

class ResourceProvider(object):

    providers = {}
    resource_watchers = {}

    @staticmethod
    def find_provider_by_name(name):
        return ResourceProvider.providers.get(name, None)

    @staticmethod
    def find_provider_by_template_match(template):
        for provider in ResourceProvider.providers.values():
            if provider.is_match_for_template(template):
                return provider

    @staticmethod
    def manage_provider(provider):
        provider = ResourceProvider(provider)
        ResourceProvider.providers[provider.name] = provider

    @staticmethod
    def manage_provider_deleted(provider_name):
        if provider_name in ResourceProvider.providers:
            del ResourceProvider.providers[provider_name]

    def __init__(self, provider):
        self.metadata = provider['metadata']
        self.spec = provider['spec']
        self.__init_resource_validator()

    def __init_resource_validator(self):
        self.resource_validator = openapi_core.shortcuts.RequestValidator(
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
    def match_ignore(self):
        return self.spec.get('matchIgnore', [])

    @property
    def override(self):
        return self.spec.get('override', {})

    @property
    def resource_requires_claim(self):
        return self.spec.get('resourceRequiresClaim', False)

    def check_template_match(self, handle_resource, claim_resource, logger):
        """
        Check if a resource in a handle matches a resource in a claim
        """
        patch = [
            item for item in jsonpatch.JsonPatch.from_diff(
                handle_resource, claim_resource
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
                return False
        return True

    def is_match_for_template(self, template):
        """
        Check if this provider is a match for the resource template by checking
        that all fields in the match definition match the template.
        """
        cmp_template = copy.deepcopy(template)
        dict_merge(cmp_template, self.spec['match'])
        return template == cmp_template

    def resource_claim_template_defaults(self, resource_claim, resource_index):
        return recursive_process_template_strings(
            self.spec['default'],
            {
                'resource_claim': resource_claim,
                'resource_index': resource_index,
                'resource_provider': self
            }
        )

    def resource_definition_from_template(self, handle, claim, resource_index, logger):
        if claim:
            requester_identity, requester_user = get_requester_from_namespace(
                claim['metadata']['namespace']
            )
        else:
            requester_identity = requester_user = None

        guid = handle['metadata']['name'][-5:]

        resource = copy.deepcopy(handle['spec']['resources'][resource_index]['template'])
        if 'override' in self.spec:
            dict_merge(resource, self.override)
        if 'metadata' not in resource:
            resource['metadata'] = {}
        if 'generateName' not in resource['metadata']:
            resource['metadata']['generateName'] = 'guid{}-'.format(resource_index)
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
            ko.operator_domain + '/resource-handle-version': handle['metadata']['resourceVersion'],
            ko.operator_domain + '/resource-index': str(resource_index)
        })
        if claim:
            resource['metadata']['annotations'].update({
                ko.operator_domain + '/resource-claim-name': claim['metadata']['name'],
                ko.operator_domain + '/resource-claim-namespace': claim['metadata']['namespace'],
            })
        if requester_identity:
            resource['metadata']['annotations'].update({
                ko.operator_domain + '/resource-requester-email':
                    requester_identity.get('extra', {}).get('email', ''),
                ko.operator_domain + '/resource-requester-name':
                    requester_identity.get('extra', {}).get('name', ''),
                ko.operator_domain + '/resource-requester-preferred-username':
                    requester_identity.get('extra', {}).get('preferred_username', ''),
            })
        if requester_user:
            resource['metadata']['annotations'].update({
                ko.operator_domain + '/resource-requester-user':
                    requester_user['metadata']['name']
            })

        return recursive_process_template_strings(resource, {
            "requester_identity": requester_identity,
            "requester_user": requester_user,
            "resource_provider": self,
            "resource_handle": handle,
            "resource_claim": claim
        })

    def update_resource(self, handle, resource, resource_definition, logger):
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

    def validate_resource_template(self, template, logger):
        try:
            validation_result = self.resource_validator.validate(
                openapi_core.wrappers.mock.MockRequest(
                    'http://example.com', 'post', '/claimTemplate',
                    path_pattern='/claimTemplate',
                    data=json.dumps(template)
                )
            )
            validation_result.raise_for_errors()
        except Exception as e:
            return str(e)

@kopf.on.event(ko.operator_domain, ko.version, 'resourceproviders')
def watch_providers(event, logger, **_):
    if event['type'] == 'DELETED':
        provider = event['object']
        provider_name = provider['metadata']['name']
        provider_namespace = provider['metadata']['namespace']
        if provider_namespace == ko.operator_namespace:
            ResourceProvider.manage_provider_deleted(provider_name)
            logger.info('Removed ResourceProvider %s', provider_name)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        provider = event['object']
        provider_name = provider['metadata']['name']
        provider_namespace = provider['metadata']['namespace']
        if provider_namespace == ko.operator_namespace:
            ResourceProvider.manage_provider(provider)
            logger.info('Discovered ResourceProvider %s', provider_name)
        else:
            logger.info(
                'Ignoring ResourceProvider %s in namespace %s',
                provider_name, provider_namespace
            )
    else:
        logger.warning('Unhandled ResourceProvider event %s', event)

@kopf.on.event(ko.operator_domain, ko.version, 'resourceclaims')
def watch_resource_claims(event, logger, **_):
    pause_for_provider_init()
    claim = event['object']
    if event['type'] == 'DELETED':
        manage_claim_deleted(claim, logger)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        manage_claim(claim, logger)
    else:
        logger.warning('Unhandled ResourceClaim event %s', event)

@kopf.on.event(ko.operator_domain, ko.version, 'resourcehandles')
def watch_resource_handles(event, logger, **_):
    pause_for_provider_init()
    handle = event['object']
    if event['type'] == 'DELETED':
        manage_handle_deleted(handle, logger)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        manage_handle(handle, logger)
    else:
        logger.warning('Unhandled ResourceHandle event %s', event)

@kopf.on.event(ko.operator_domain, ko.version, 'resourcepools')
def watch_resource_pools(event, logger, **_):
    pause_for_provider_init()
    if event['type'] == 'DELETED':
        pool = event['object']
        manage_pool_deleted(pool, logger)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        pool = event['object']
        if 'deletionTimestamp' in pool['metadata']:
            manage_pool_pending_delete(pool, logger)
        else:
            manage_pool(pool, logger)
    else:
        logger.warning('Unhandled ResourcePool event %s', event)
