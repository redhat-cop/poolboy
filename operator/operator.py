#!/usr/bin/env python

import copy
import datetime
import gpte.kubeoperative
import json
import kopf
import kubernetes
import logging
import openapi_schema_validator
import os
import prometheus_client
import re
import threading
import time

from gpte.util import TimeDelta, TimeStamp, defaults_from_schema, dict_merge, recursive_process_template_strings

logging_level = os.environ.get('LOGGING_LEVEL', 'INFO')
manage_handles_interval = int(os.environ.get('MANAGE_HANDLES_INTERVAL', 60))

@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    # Disable scanning for CustomResourceDefinitions
    settings.scanning.disabled = True

ko = gpte.kubeoperative.KubeOperative(
    operator_domain = os.environ.get('OPERATOR_DOMAIN', 'poolboy.gpte.redhat.com')
)
providers = {}
provider_init_delay = int(os.environ.get('PROVIDER_INIT_DELAY', 10))
start_time = time.time()

pool_management_lock = threading.Lock()
manage_handle_lock = threading.Lock()
manage_handle_locks = {}

def add_finalizer_to_handle(handle, logger):
    handle_meta = handle['metadata']
    handle_namespace = handle_meta['namespace']
    handle_name = handle_meta['name']
    ko.custom_objects_api.patch_namespaced_custom_object(
        ko.operator_domain, ko.version, handle_namespace, 'resourcehandles', handle_name,
        { 'metadata': { 'finalizers': [ko.operator_domain] } }
    )

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
    pool_ref = handle['spec'].get('resourcePool')

    # Check if ResourceClaim includes requested lifespan end
    lifespan_end = claim['spec'].get('lifespan', {}).get('end')

    if lifespan_end:
        # If ResourceClaim defined lifespan end, then translate to TimeStamp
        lifespan_end = TimeStamp(lifespan_end)
    else:
        # Default lifespan end to ResourceHandle default lifespan if defined
        handle_lifespan = handle['spec'].get('lifespan')
        if handle_lifespan:
            handle_lifespan_default = handle_lifespan.get('default')
            if handle_lifespan_default:
                lifespan_end = TimeStamp(claim_meta['creationTimestamp']) + TimeDelta(handle_lifespan_default)

    if lifespan_end:
        maximum_lifespan_end, maximum_type = maximum_lifespan_end_for_handle(handle, claim)
        if lifespan_end > maximum_lifespan_end:
            lifespan_end = maximum_lifespan_end
            log_claim_warning(
                claim, logger,
                'requested lifespan end {} exceeds {} for ResourceHandle {}'.format(
                    lifespan_end, maximum_type, maximum_lifespan_end
                )
            )
        lifespan_end = str(lifespan_end)

    log_claim_event(
        claim, logger,
        'Binding ResourceHandle to ResourceClaim',
        {
            'ResourceHandle': {
               'name': handle_name,
            },
        },
    )

    handle = ko.custom_objects_api.patch_namespaced_custom_object(
        ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', handle_name,
        {
            'metadata': {
                'labels': {
                    ko.operator_domain + '/resource-claim-name': claim_name,
                    ko.operator_domain + '/resource-claim-namespace': claim_namespace
                }
            },
            'spec': {
                'lifespan': {
                    'end': lifespan_end,
                },
                'resourceClaim': {
                    'apiVersion': ko.api_version,
                    'kind': 'ResourceClaim',
                    'name': claim_name,
                    'namespace': claim_namespace
                }
            }
        }
    )

    if pool_ref:
        manage_pool_by_ref(pool_ref, logger)

    return handle

def create_handle_for_claim(claim, logger):
    """
    Create resource handle for claim, called after claim is validated and failed
    to match an available handle.
    """
    claim_meta = claim['metadata']
    claim_spec = claim['spec']
    claim_name = claim_meta['name']
    claim_namespace = claim_meta['namespace']
    claim_resource_statuses = claim['status']['resources']

    resources = []
    default_lifespan = None
    maximum_lifespan = None
    relative_maximum_lifespan = None
    for i, claim_resource in enumerate(claim['spec']['resources']):
        provider_ref = claim_resource_statuses[i]['provider']
        provider = ResourceProvider.find_provider_by_name(provider_ref['name'])
        if provider.default_lifespan \
        and (not default_lifespan or provider.default_lifespan < default_lifespan):
            default_lifespan = provider.default_lifespan
        if provider.maximum_lifespan \
        and (not maximum_lifespan or provider.maximum_lifespan < maximum_lifespan):
            maximum_lifespan = provider.maximum_lifespan
        if provider.relative_maximum_lifespan \
        and (not relative_maximum_lifespan or provider.relative_maximum_lifespan < relative_maximum_lifespan):
            relative_maximum_lifespan = provider.relative_maximum_lifespan
        resources_item = {'provider': provider_ref}
        if 'template' in claim_resource:
            resources_item['template'] = claim_resource['template']
        resources.append(resources_item)

    log_claim_event(claim, logger, 'Creating ResourceHandle for ResourceClaim')

    handle = {
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

    lifespan_end = claim_spec.get('lifespan', {}).get('end')
    creation_timestamp = TimeStamp(claim_meta['creationTimestamp'])
    if lifespan_end:
        lifespan_end = TimeStamp(requested_end)
    elif default_lifespan:
        lifespan_end = creation_timestamp + default_lifespan
    elif relative_maximum_lifespan:
        lifespan_end = creation_timestamp + relative_maximum_lifespan
    elif maximum_lifespan:
        lifespan_end = creation_timestamp + maximum_lifespan

    if lifespan_end:
        if relative_maximum_lifespan and lifespan_end > creation_timestamp + relative_maximum_lifespan:
            log_claim_warning(
                claim, logger,
                'requested lifespan end {} exceeds relativeMaximum for ResourceProvider {}'.format(
                    lifespan_end, relative_maximum_lifespan
                )
            )
            lifespan_end = creation_timestamp + relative_maximum_lifespan
        if maximum_lifespan and lifespan_end > creation_timestamp + maximum_lifespan:
            log_claim_warning(
                claim, logger,
                'requested lifespan end {} exceeds maximum for ResourceProvider {}'.format(
                    lifespan_end, maximum_lifespan
                )
            )
            lifespan_end = creation_timestamp + maximum_lifespan

    if lifespan_end or maximum_lifespan or relative_maximum_lifespan:
        handle['spec']['lifespan'] = {}
        if lifespan_end:
            handle['spec']['lifespan']['end'] = str(lifespan_end)
        if maximum_lifespan:
            handle['spec']['lifespan']['maximum'] = str(maximum_lifespan)
        if relative_maximum_lifespan:
            handle['spec']['lifespan']['relativeMaximum'] = str(relative_maximum_lifespan)

    return ko.custom_objects_api.create_namespaced_custom_object(
        ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', handle
    )

def create_handle_for_pool(pool, logger):
    pool_meta = pool['metadata']
    pool_spec = pool['spec']
    pool_namespace = pool_meta['namespace']
    pool_name = pool_meta['name']

    handle_spec = {
        'resourcePool': {
            'apiVersion': ko.api_version,
            'kind': 'ResourcePool',
            'name': pool_name,
            'namespace': pool_namespace
        },
        'resources': pool_spec['resources']
    }

    if 'lifespan' in pool_spec:
        handle_spec['lifespan'] = {
            k: v for k, v in pool_spec['lifespan'].items() if k != 'unclaimed'
        }
        if 'unclaimed' in pool_spec['lifespan']:
            handle_spec['lifespan']['end'] = str(TimeStamp().add(pool_spec['lifespan']['unclaimed']))

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
                    ko.operator_domain + '/resource-pool-namespace': pool_namespace,
                },
            },
            'spec': handle_spec,
        }
    )

def delete_resource_claim(namespace, name, logger):
    try:
        ko.custom_objects_api.delete_namespaced_custom_object(
            ko.operator_domain, ko.version, namespace, 'resourceclaims', name
        )
    except kubernetes.client.rest.ApiException as e:
        if e.status != 404:
            raise

def delete_resource_handle(name, logger):
    try:
        ko.custom_objects_api.delete_namespaced_custom_object(
            ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', name
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
             ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', handle_name
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
        if e.status not in (404, 422):
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

def log_claim_extra(claim, extra={}):
    ret = copy.deepcopy(extra)
    ret['ResourceClaim'] = {
        'name': claim['metadata']['name'],
        'namespace': claim['metadata']['namespace'],
        'uid': claim['metadata']['uid'],
    }
    return ret

def log_claim_event(claim, logger, msg, extra={}):
    logger.info(msg, extra=log_claim_extra(claim, extra))
    # FIXME - Create event for claim

def log_claim_warning(claim, logger, msg, extra={}):
    logger.warning(msg, extra=log_claim_extra(claim, extra))
    # FIXME - Create event for claim

def log_claim_error(claim, logger, msg, extra={}):
    logger.error(msg, extra=log_claim_extra(claim, extra))
    # FIXME - Create event for claim

def log_handle_extra(handle, extra={}):
    ret = copy.deepcopy(extra)
    ret['ResourceHandle'] = {
        'name': handle['metadata']['name'],
        'uid': handle['metadata']['uid'],
    }
    return ret

def log_handle_event(handle, logger, msg, extra={}):
    logger.info(msg, extra=log_handle_extra(handle, extra))
    # FIXME - Create event for handle

def log_handle_warning(handle, logger, msg, extra={}):
    logger.warning(msg, extra=log_handle_extra(handle, extra))
    # FIXME - Create event for handle

def log_handle_error(handle, logger, msg, extra={}):
    logger.error(msg, extra=log_handle_extra(handle, extra))
    # FIXME - Create event for handle

def log_pool_extra(pool, extra={}):
    ret = copy.deepcopy(extra)
    ret['ResourceHandle'] = {
        'name': pool['metadata']['name'],
        'uid': pool['metadata']['uid'],
    }
    return ret

def log_pool_event(pool, logger, msg, extra={}):
    logger.info(msg, extra=log_pool_extra(pool, extra))
    # FIXME - Create event for pool

def log_pool_warning(pool, logger, msg, extra={}):
    logger.warning(msg, extra=log_pool_extra(pool, extra))
    # FIXME - Create event for pool

def log_pool_error(pool, logger, msg, extra={}):
    logger.error(msg, extra=log_pool_extra(pool, extra))
    # FIXME - Create event for pool

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
        handle = bind_handle_to_claim(handle, claim, logger)
    else:
        handle = create_handle_for_claim(claim, logger)

    claim_meta = claim['metadata']
    claim_name = claim_meta['name']
    claim_namespace = claim_meta['namespace']
    handle_meta = handle['metadata']
    handle_spec = handle['spec']
    handle_name = handle_meta['name']
    status = {
        'resourceHandle': {
            'apiVersion': ko.api_version,
            'kind': 'ResourceHandle',
            'name': handle_name,
            'namespace': handle_meta['namespace']
        }
    }

    handle_lifespan = handle_spec.get('lifespan')
    if handle_lifespan:
        status['lifespan'] = {
            'end': handle_lifespan.get('end'),
            'maximum': handle_lifespan.get('maximum'),
            'relativeMaximum': handle_lifespan.get('relativeMaximum'),
        }

    ko.custom_objects_api.patch_namespaced_custom_object_status(
        ko.operator_domain, ko.version, claim_namespace, 'resourceclaims', claim_name,
        {
            'status': status
        }
    )

    log_claim_event(
        claim, logger,
        'Bound ResourceHandle to ResourceClaim',
        {
            'ResourceHandle': {
                'name': handle_name,
                'uid': handle_meta['uid']
            },
        }
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
            resource_providers.append(provider)
        elif 'template' in resource:
            provider = ResourceProvider.find_provider_by_template_match(resource['template'])
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
        log_claim_event(
            claim, logger,
            'Propagating delete to ResourceHandle',
            {
                'ResourceHandle': {
                    'name': handle_name
                }
            }
        )
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
            claim_resources_item = { 'provider': provider_ref }
            template_defaults = provider.resource_claim_template_defaults(
                resource_claim = claim,
                resource_index = i
            )
            if template_defaults:
                claim_resources_item['template'] = template_defaults
            claim_resources_update.append(claim_resources_item)

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
            { 'pathMatch': '/spec/resources/[0-9]+/template(/.*)?', 'allowedOps': ['add'] },
        ]
    )

def manage_claim_resource_delete(claim_namespace, claim_name, resource, resource_index, logger):
    resource_kind = resource['kind']
    resource_meta = resource['metadata']
    resource_name = resource_meta['name']
    resource_namespace = resource_meta.get('namespace', None)
    try:
        claim = ko.custom_objects_api.get_namespaced_custom_object(
            ko.operator_domain, ko.version, claim_namespace, 'resourceclaims', claim_name
        )
        if resource_namespace:
            logger.info('ResourceClaim {} in {} lost resource {} {} in {}'.format(
                claim_name, claim_namespace, resource_kind, resource_name, resource_namespace
            ))
        else:
            logger.info('ResourceClaim {} in {} lost resource {} {}'.format(
                claim_name, claim_namespace, resource_kind, resource_name
            ))
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

def manage_claim_resource_update(claim_namespace, claim_name, resource, resource_index, logger):
    resource_kind = resource['kind']
    resource_meta = resource['metadata']
    resource_name = resource_meta['name']
    resource_namespace = resource_meta.get('namespace', None)
    resource_ref = {
        'apiVersion': resource['apiVersion'],
        'kind': resource_kind,
        'name': resource_name,
    }
    if resource_namespace:
        resource_ref['namespace'] = resource_namespace

    logger.info(
        'ResourceClaim resource status update',
        extra = {
            'Resource': resource_ref,
            'ResourceClaim': {
                'name': claim_name,
                'namespace': claim_namespace,
            },
        },
    )

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
            logger.info('ResourceClaim %s in %s not found', claim_name, claim_namespace)
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
            claim, logger,
            'ResourceHandle has been lost',
            {
                'ResourceHandle': {
                    'name': handle_name,
                },
            }
        )
        return

    have_update = False
    handle_resources = resource_handle['spec']['resources']
    for i, claim_resource in enumerate(claim['spec']['resources']):
        handle_resource = handle_resources[i]
        if 'template' in claim_resource \
        and handle_resource.get('template') != claim_resource['template']:
            handle_resource['template'] = claim_resource['template']
            have_update = True

    lifespan_end = claim['spec'].get('lifespan', {}).get('end')
    handle_lifespan_end = resource_handle['spec'].get('lifespan', {}).get('end')
    if lifespan_end and lifespan_end != handle_lifespan_end:
        lifespan_end = TimeStamp(lifespan_end)
        maximum_lifespan_end, maximum_type = maximum_lifespan_end_for_handle(resource_handle, claim)
        if maximum_lifespan_end:
            if lifespan_end > maximum_lifespan_end:
                lifespan_end = maximum_lifespan_end
                log_claim_warning(
                    claim, logger,
                    'requested change to lifespan end {} exceeds {} for ResourceHandle {}'.format(
                        lifespan_end, maximum_type, maximum_lifespan_end
                    )
                )
            if not handle_lifespan_end or lifespan_end != TimeStamp(handle_lifespan_end):
                # Recheck for update after possible adjustment for maximum
                have_update = True
        else:
            have_update = True

    if have_update:
        patch = { 'spec': { 'resources': handle_resources } }
        if lifespan_end:
            patch['spec']['lifespan'] = { 'end': str(lifespan_end) }
        ko.custom_objects_api.patch_namespaced_custom_object(
            ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', handle_name, patch
        )

def manage_handle(handle, logger):
    """
    Called on all ResourceHandle events except delete
    """
    handle_meta = handle['metadata']
    handle_spec = handle['spec']
    handle_name = handle_meta['name']
    handle_lock = None

    with manage_handle_lock:
        handle_lock = manage_handle_locks.get(handle_name)
        if not handle_lock:
            handle_lock = threading.Lock()
            manage_handle_locks[handle_name] = handle_lock

    with handle_lock:
        if 'deletionTimestamp' in handle['metadata']:
            manage_handle_pending_delete(handle, logger)
            return
        elif 'finalizers' not in handle_meta:
            add_finalizer_to_handle(handle, logger)
            return
        elif 'resourceClaim' in handle_spec:
            claim = get_claim_for_handle(handle, logger)
            if claim:
                handle_lifespan = handle_spec.get('lifespan')
                if handle_lifespan:
                    set_claim_status_lifespan = { k: v for k, v in handle_lifespan.items() if k != 'default' }
                    claim_status_lifespan = claim.get('status', {}).get('lifespan')
                    if claim_status_lifespan != set_claim_status_lifespan:
                        ko.custom_objects_api.patch_namespaced_custom_object_status(
                            ko.operator_domain, ko.version, claim['metadata']['namespace'],
                            'resourceclaims', claim['metadata']['name'],
                            {
                                'status': {
                                    'lifespan': set_claim_status_lifespan
                                }
                            }
                        )
            else:
                logger.info(
                    'Propagating delete to ResourceHandle after discovering ResourceClaim deleted',
                    extra={
                        'ResourceClaim': {
                            'namespace': handle_spec['resourceClaim']['namespace'],
                            'name': handle_spec['resourceClaim']['name'],
                        },
                        'ResourceHandle': {
                            'name': handle_name
                        }
                    }
                )
                delete_resource_handle(handle_name, logger)
                return
        else:
            claim = None

        lifespan_end = handle_spec.get('lifespan', {}).get('end')
        if lifespan_end:
            if datetime.datetime.utcnow() > datetime.datetime.strptime(lifespan_end, "%Y-%m-%dT%H:%M:%SZ"):
                if claim:
                    claim_namespace = claim['metadata']['namespace']
                    claim_name = claim['metadata']['name']
                    logger.info(
                        'Delete ResourceClaim at end of lifespan',
                        extra={
                            'ResourceClaim': {
                                'namespace': claim_namespace,
                                'name': claim_name,
                            }
                        }
                    )
                    delete_resource_claim(claim_namespace, claim_name, logger)
                else:
                    logger.info(
                        'Delete ResourceHandle at end of lifespan',
                        extra={
                            'ResourceHandle': {
                                'name': handle_name
                            }
                        }
                    )
                    delete_resource_handle(handle_name, logger)
                return

        providers = []
        handle_resources = handle_spec['resources']
        for handle_resource in handle_resources:
            provider_name = handle_resource['provider']['name']
            providers.append(
                ResourceProvider.find_provider_by_name(provider_name)
            )

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
            if hasattr(resource, 'to_dict'):
                resource = resource.to_dict()

            if resource:
                provider.update_resource(handle, resource, resource_definition, logger)
            else:
                resources_to_create.append(resource_definition)

        if have_handle_update:
            try:
                handle = ko.custom_objects_api.patch_namespaced_custom_object(
                    ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles', handle_name,
                    { 'spec': { 'resources': handle_resources } }
                )
            except kubernetes.client.rest.ApiException as e:
                if e.status != 404:
                    raise

        for resource_definition in resources_to_create:
            resource_definition['metadata']['annotations'][ko.operator_domain + '/resource-handle-version'] = \
                handle['metadata']['resourceVersion']
            ko.create_resource(resource_definition)

def manage_handle_deleted(handle, logger):
    handle_meta = handle['metadata']
    handle_name = handle_meta['name']
    logger.info('ResourceHandle deleted')

    claim_ref = handle['spec'].get('resourceClaim')
    pool_ref = handle['spec'].get('resourcePool')

    # Delete of unclaimed handle from pool may require replacement
    if pool_ref and not claim_ref:
        manage_pool_by_ref(pool_ref, logger)

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

    resource_claim = handle['spec'].get('resourceClaim')
    if resource_claim:
        delete_resource_claim(resource_claim['namespace'], resource_claim['name'], logger)

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
        logger.debug(
            'Ignoring ResourcePool {} defined in namespace {}'.format(
                pool_name,
                pool_namespace,
            ),
        )
        return

    if not pool['metadata'].get('finalizers', None):
        add_finalizer_to_pool(pool, logger)
        return

    with pool_management_lock:
        unbound_handle_count = len(get_unbound_handles_for_pool(pool_name, logger))
        handle_deficit = pool['spec'].get('minAvailable', 0) - unbound_handle_count
        if handle_deficit <= 0:
            return
        for i in range(handle_deficit):
            handle = create_handle_for_pool(pool, logger)
            log_pool_event(
                pool, logger, 'Created ResourceHandle for ResourcePool',
                {
                    'ResourceHandle': {
                        'name': handle['metadata']['name'],
                        'uid': handle['metadata']['uid'],
                    }
                },
            )

def manage_pool_by_ref(ref, logger):
    try:
        pool = ko.custom_objects_api.get_namespaced_custom_object(
            ko.operator_domain, ko.version, ref['namespace'],
            'resourcepools', ref['name']
        )
    except kubernetes.client.rest.ApiException as e:
        if e.status == 404:
            logger.warning('Unable to find ResourcePool %s in %s', ref['name'], ref['namespace'])
            return
        else:
            raise
    manage_pool(pool, logger)

def manage_pool_deleted(pool, logger):
    pool_meta = pool['metadata']
    pool_name = pool_meta['name']
    log_pool_event(pool, logger, 'ResourcePool deleted')

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

    best_match = None
    best_match_diff_count = None
    best_match_creation_timestamp = None
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

        diff_count = 0
        is_match = True
        for i, claim_resource in enumerate(claim_resources):
            handle_resource = handle_resources[i]
            provider_name = status_resources[i]['provider']['name']
            if provider_name != handle_resource['provider']['name']:
                is_match = False
                break
            provider = ResourceProvider.find_provider_by_name(provider_name)
            diff_patch = provider.check_template_match(
                handle_resource.get('template', {}),
                claim_resource.get('template', {}),
                logger
            )
            if diff_patch != None:
                # Match with (possibly empty) difference list
                diff_count += len(diff_patch)
            else:
                is_match = False
                break

        if is_match:
            # Prefer match with the smallest diff_count and the earliest creation timestamp
            if not best_match \
            or best_match_diff_count > diff_count \
            or (best_match_diff_count == diff_count and  best_match_creation_timestamp > handle['metadata']['creationTimestamp']):
                best_match = handle
                best_match_creation_timestamp = handle['metadata']['creationTimestamp']
                best_match_diff_count = diff_count

    return best_match

def maximum_lifespan_end_for_handle(handle, claim):
    """
    Return TimeStamp and type name for maximum lifespan end of ResourceHandle with bound ResourceClaim
    """
    end = None
    maximum_type = None

    maximum_lifespan = handle['spec'].get('lifespan', {}).get('maximum')
    if maximum_lifespan:
        end = TimeStamp(claim['metadata']['creationTimestamp']) + TimeDelta(maximum_lifespan)
        maximum_type = 'maximum'

    relative_maximum_lifespan = handle['spec'].get('lifespan', {}).get('relativeMaximum')
    if relative_maximum_lifespan:
        relative_end = TimeStamp() + TimeDelta(relative_maximum_lifespan)
        if relative_end < end:
            end = relative_end
            maximum_type = 'relativeMaximum'

    return end, maximum_type

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

    if watcher_name in ko.watchers:
        return

    if '/' in api_version:
        group, version = api_version.split('/')
    else:
        group, version = None, api_version

    w = ko.create_watcher(
        name=watcher_name,
        kind=kind,
        group=group,
        namespace=namespace,
        version=version
    )
    w.handler = watch_resource_event
    w.start()

def validate_claim(claim, logger):
    """
    Check claim validity against providers for resources
    """
    resources = claim['spec'].get('resources', [])
    status_resources = claim['status'].get('resources', [])

    if len(status_resources) != len(resources):
        log_pool_error(claim, logger, 'Number of resources in status does not match resources in spec')
        return False

    for i, resource in enumerate(resources):
        try:
            status_resource = status_resources[i]
            provider_name = status_resource['provider']['name']
            provider = ResourceProvider.find_provider_by_name(provider_name)
            validation_error = provider.validate_resource_template(resource.get('template', {}), logger)
            if validation_error:
                log_claim_error(
                    claim, logger,
                    'Validation failure for spec.resources[{}].template: {}'.format(i, validation_error)
                )
                return False

        except IndexError:
            log_claim_error(
                claim, logger, 'ResourceClaim has more resources than resourceProviders!'
            )
            return False
    return True

def watch_resource_event(event, logger):
    event_type = event['type']
    if event_type in ['ADDED', 'DELETED', 'MODIFIED']:
        resource = event['object']
        if hasattr(resource, 'to_dict'):
            resource = resource.to_dict()
        metadata = resource['metadata']
        annotations = metadata.get('annotations', None)
        if not annotations:
            return
        annotation_prefix = ko.operator_domain + '/resource-'
        handle_name = annotations.get(annotation_prefix + 'handle-namespace', None)
        handle_namespace = annotations.get(annotation_prefix + 'handle-namespace', None)
        claim_name = annotations.get(annotation_prefix + 'claim-name', None)
        claim_namespace = annotations.get(annotation_prefix + 'claim-namespace', None)
        resource_index = int(annotations.get(annotation_prefix + 'index', 0))

        if not handle_name \
        or handle_namespace != ko.operator_namespace:
            return

        if event_type == 'DELETED':
            manage_handle_lost_resource(handle_name, resource, resource_index)

        if claim_name and claim_namespace:
            if event_type == 'DELETED':
                manage_claim_resource_delete(claim_namespace, claim_name, resource, resource_index, logger)
            else:
                manage_claim_resource_update(claim_namespace, claim_name, resource, resource_index, logger)
    else:
        logger.warning(event)

class ResourceProvider(object):

    providers = {}
    resource_watchers = {}

    @staticmethod
    def find_provider_by_name(name):
        provider = ResourceProvider.providers.get(name)
        if not provider:
            raise kopf.TemporaryError(f"ResourceProvider {name} not found")
        return provider

    @staticmethod
    def find_provider_by_template_match(template):
        provider_matches = []
        for provider in ResourceProvider.providers.values():
            if provider.is_match_for_template(template):
                provider_matches.append(provider)
        if len(provider_matches) == 0:
            raise kopf.TemporaryError(f"Unable to match template to ResourceProvider")
        elif len(provider_matches) == 1:
            return provider_matches[0]
        else:
            raise kopf.PermanentError(f"Resource template matches multiple ResourceProviders")

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
        open_api_v3_schema = self.spec.get('validation', {}).get('openAPIV3Schema', None)
        if not open_api_v3_schema:
            self.resource_validator = None
            return
        self.resource_validator = openapi_schema_validator.OAS30Validator(open_api_v3_schema)

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def default_lifespan(self):
        if 'lifespan' in self.spec \
        and 'default' in self.spec['lifespan']:
            return TimeDelta(self.spec['lifespan']['default'])

    @property
    def match(self):
        return self.spec.get('match', None)

    @property
    def match_ignore(self):
        return self.spec.get('matchIgnore', [])

    @property
    def override(self):
        return self.spec.get('override', {})

    @property
    def maximum_lifespan(self):
        if 'lifespan' in self.spec \
        and 'maximum' in self.spec['lifespan']:
            return TimeDelta(self.spec['lifespan']['maximum'])

    @property
    def relative_maximum_lifespan(self):
        if 'lifespan' in self.spec \
        and 'relativeMaximum' in self.spec['lifespan']:
            return TimeDelta(self.spec['lifespan']['relativeMaximum'])

    @property
    def resource_requires_claim(self):
        return self.spec.get('resourceRequiresClaim', False)

    @property
    def template_enable(self):
        if 'template' not in self.spec:
            return True
        return self.spec['template'].get('enable')

    @property
    def template_style(self):
        if 'template' not in self.spec:
            return 'legacy'
        return self.spec['template'].get('style', 'jinja2')

    def check_template_match(self, handle_resource, claim_resource, logger):
        """
        Check if a resource in a handle matches a resource in a claim
        """
        patch = [
            item for item in gpte.kubeoperative.jsonpatch_from_diff(
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
                return None
        return patch

    def is_match_for_template(self, template):
        """
        Check if this provider is a match for the resource template by checking
        that all fields in the match definition match the template.
        """
        if not self.match:
            return False
        cmp_template = copy.deepcopy(template)
        dict_merge(cmp_template, self.match)
        return template == cmp_template

    def resource_claim_template_defaults(self, resource_claim, resource_index):
        defaults = self.spec.get('default', {})
        open_api_v3_schema = self.spec.get('validation', {}).get('openAPIV3Schema', None)

        if open_api_v3_schema:
            schema_defaults = defaults_from_schema(open_api_v3_schema)
            if schema_defaults:
                dict_merge(defaults, schema_defaults)

        if not defaults:
            return
        elif self.template_enable:
            return recursive_process_template_strings(
                defaults, self.template_style,
                {
                    'resource_claim': resource_claim,
                    'resource_index': resource_index,
                    'resource_provider': self
                }
            )
        else:
            return defaults

    def resource_definition_from_template(self, handle, claim, resource_index, logger):
        if claim:
            requester_identity, requester_user = get_requester_from_namespace(
                claim['metadata']['namespace']
            )
        else:
            requester_identity = requester_user = None

        handle_name = handle['metadata']['name']
        handle_generate_name = handle['metadata'].get('generateName')
        if handle_generate_name and handle_name.startswith(handle_generate_name):
            guid = handle_name[len(handle_generate_name):]
        elif handle_name.startswith('guid-'):
            guid = handle_name[5:]
        else:
            guid = handle_name[-5:]

        resource_reference = handle['spec']['resources'][resource_index].get('reference', {})
        resource_template = handle['spec']['resources'][resource_index].get('template', {})
        resource = copy.deepcopy(resource_template)
        if 'override' in self.spec:
            if self.template_enable:
                dict_merge(
                    resource,
                    recursive_process_template_strings(
                        self.override, self.template_style,
                        {
                            "requester_identity": requester_identity,
                            "requester_user": requester_user,
                            "resource_provider": self,
                            "resource_handle": handle,
                            "resource_claim": claim,
                            "resource_reference": resource_reference,
                            "resource_template": resource_template,
                        },
                    ),
                )
            else:
                dict_merge(resource, self.override)

        if 'metadata' not in resource:
            resource['metadata'] = {}
        if 'name' not in resource['metadata']:
            # If name prefix was not given then use prefix "guidN-" with resource index to
            # prevent name conflicts. If the resource template does specify a name prefix
            # then it is expected that the template configuration prevents conflicts.
            if 'generateName' not in resource['metadata']:
                resource['metadata']['generateName'] = 'guid{}-'.format(resource_index)
            resource['metadata']['name'] = \
                resource['metadata']['generateName'] + guid
        if 'annotations' not in resource['metadata']:
            resource['metadata']['annotations'] = {}
        if 'apiVersion' not in resource:
            kopf.TemporaryError(f"Template processing for ResourceProvider {self.name} produced definition without an apiVersion!")
        if 'kind' not in resource:
            kopf.TemporaryError(f"Template processing for ResourceProvider {self.name} produced definition without a kind!")
        if resource_reference:
            # If there is an existing resoruce reference, then don't allow changes
            # to properties in the reference.
            resource['metadata']['name'] = resource_reference['name']
            if 'namespace' in resource_reference:
                resource['metadata']['namespace'] = resource_reference['namespace']
            if resource['apiVersion'] != resource_reference['apiVersion']:
                kopf.TemporaryError(f"Unable to change apiVersion for resource!")
            if resource['kind'] != resource_reference['kind']:
                kopf.TemporaryError(f"Unable to change kind for resource!")

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
        return resource

    def update_resource(self, handle, resource, resource_definition, logger):
        handle_uid = handle['metadata']['uid']
        handle_version = handle['metadata']['resourceVersion']

        resource_ref = {
            'apiVersion': resource['apiVersion'],
            'kind': resource['kind'],
            'name': resource['metadata']['name'],
        }
        if 'namespace' in resource['metadata']:
            resource_ref['namespace'] = resource['metadata']['namespace']

        update_filters = self.spec.get('updateFilters', []) + [{
            'pathMatch': '/metadata/annotations/' + re.escape(ko.operator_domain) + '~1resource-.*'
        }]
        patched_resource, changed = ko.patch_resource(
            resource=resource,
            patch=resource_definition,
            update_filters=update_filters
        )
        if changed:
            log_handle_event(handle, logger, 'Updated resource', {'Resource': resource_ref})
        else:
            logger.debug(
                'Resource unchanged',
                extra=log_handle_extra(handle, {'resource': resource_ref})
            )

    def validate_resource_template(self, template, logger):
        try:
            if self.resource_validator:
                validation_result = self.resource_validator.validate(template)
        except Exception as e:
            return str(e)

@kopf.on.event(ko.operator_domain, ko.version, 'resourceproviders')
def resource_provider_event(event, logger, **_):
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
def resource_claim_event(event, logger, **_):
    pause_for_provider_init()
    claim = event['object']
    if event['type'] == 'DELETED':
        manage_claim_deleted(claim, logger)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        manage_claim(claim, logger)
    else:
        logger.warning('Unhandled ResourceClaim event %s', event)

@kopf.on.event(ko.operator_domain, ko.version, 'resourcehandles')
def resource_handle_event(event, logger, **_):
    pause_for_provider_init()
    handle = event['object']
    if event['type'] == 'DELETED':
        manage_handle_deleted(handle, logger)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        manage_handle(handle, logger)
    else:
        logger.warning('Unhandled ResourceHandle event %s', event)

@kopf.on.event(ko.operator_domain, ko.version, 'resourcepools')
def resource_pool_event(event, logger, **_):
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

def manage_handles(logger):
    _continue = None

    while True:
        kwargs = { "limit": 20 }
        if _continue:
            kwargs['_continue'] = _continue

        resp = ko.custom_objects_api.list_namespaced_custom_object(
            ko.operator_domain, ko.version, ko.operator_namespace, 'resourcehandles',
            **kwargs
        )
        for handle in resp.get('items', []):
            manage_handle(handle, logger)

        _continue = resp['metadata'].get('continue')
        if not _continue:
            break

def manage_handles_loop():
    logger = logging.getLogger('resourcehandles')
    while True:
        time.sleep(manage_handles_interval)
        try:
            manage_handles(logger)
        except Exception as e:
            logger.exception("Error in resourcehandles")

@kopf.on.startup()
def on_startup(logger, **kwargs):
    """Main function."""
    threading.Thread(
        name = 'manage_handles',
        daemon = True,
        target = manage_handles_loop,
    ).start()
