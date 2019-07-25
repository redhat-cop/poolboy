#!/usr/bin/env python

import base64
import kubernetes
import kubernetes.client.rest
import logging
import os
import prometheus_client
import random
import re
import socket
import sys
import string
import threading
import time

from operatorruntime import OperatorRuntime
from resourcehandler import ResourceHandler
from resourcehandle import ResourceHandle
from resourceclaim import ResourceClaim

# Variables initialized during init()
kube_api = None
kube_custom_objects = None
logger = None
service_account_token = None
operator_domain = os.environ.get('OPERATOR_DOMAIN','gpte.redhat.com')
operator_namespace = None

def init():
    """Initialization function before management loops."""
    init_logging()
    init_namespace()
    init_service_account_token()
    init_kube_api()
    init_runtime()
    init_resource_handlers()
    init_resource_handles()
    logger.debug("Completed init")

def init_logging():
    """Define logger global and set default logging level.
    Default logging level is INFO and may be overridden with the
    LOGGING_LEVEL environment variable.
    """
    global logger
    logging.basicConfig(
        format='%(levelname)s %(threadName)s - %(message)s',
    )
    logger = logging.getLogger('anarchy')
    logger.setLevel(os.environ.get('LOGGING_LEVEL', 'DEBUG'))

def init_namespace():
    """
    Set the namespace global based on the namespace in which this pod is
    running.
    """
    global operator_namespace
    with open('/run/secrets/kubernetes.io/serviceaccount/namespace') as f:
        operator_namespace = f.read()

def init_service_account_token():
    global service_account_token
    with open('/run/secrets/kubernetes.io/serviceaccount/token') as f:
        service_account_token = f.read()

def init_kube_api():
    """Set kube_api global to communicate with the local kubernetes cluster."""
    global kube_api, kube_custom_objects, operator_domain
    kube_config = kubernetes.client.Configuration()
    kube_config.api_key['authorization'] = service_account_token
    kube_config.api_key_prefix['authorization'] = 'Bearer'
    kube_config.host = os.environ['KUBERNETES_PORT'].replace('tcp://', 'https://', 1)
    kube_config.ssl_ca_cert = '/run/secrets/kubernetes.io/serviceaccount/ca.crt'
    kube_api = kubernetes.client.CoreV1Api(
        kubernetes.client.ApiClient(kube_config)
    )
    kube_custom_objects = kubernetes.client.CustomObjectsApi(
        kubernetes.client.ApiClient(kube_config)
    )

def init_runtime():
    global runtime
    runtime = OperatorRuntime(
        kube_api = kube_api,
        kube_custom_objects = kube_custom_objects,
        logger = logger,
        operator_domain = operator_domain,
        operator_namespace = operator_namespace,
    )

def init_resource_handlers():
    """Get initial list of anarchy apis"""
    for resource in kube_custom_objects.list_namespaced_custom_object(
        operator_domain, 'v1', operator_namespace, 'resourcehandlers'
    ).get('items', []):
        ResourceHandler.register(resource)

def handler_added(resource):
    handler = ResourceHandler.register(resource)

def handler_modified(resource):
    handler = ResourceHandler.register(resource)

def handler_deleted(resource):
    handler = ResourceHandler.get(resource['metadata']['name'])
    ResourceHandler.unregister(handler)

def watch_handlers():
    stream = kubernetes.watch.Watch().stream(
        kube_custom_objects.list_namespaced_custom_object,
        operator_domain,
        'v1',
        operator_namespace,
        'resourcehandlers'
    )
    for event in stream:
        logger.debug(event)
        event_obj = event['object']
        if event_obj['kind'] == 'Status':
            logger.debug('Watch %s - reason %s, %s',
                event_obj['status'],
                event_obj['reason'],
                event_obj['message']
            )
            if event_obj['status'] == 'Failure':
                if event_obj['reason'] == 'Expired':
                    return
                else:
                    raise Exception("Watch failure: " + event_obj['message'])
        else:
            logger.debug("ResourceHandler %s %s",
                event_obj['metadata']['name'],
                event['type']
            )
            if event['type'] == 'ADDED':
                handler_added(event_obj)
            elif event['type'] == 'MODIFIED':
                handler_modified(event_obj)
            elif event['type'] == 'DELETED':
                handler_deleted(event_obj)

def watch_handlers_loop():
    while True:
        try:
            watch_handlers()
        except Exception as e:
            logger.exception("Error while watching handlers: %s", e)
            time.sleep(60)

def handle_added(resource):
    handle = ResourceHandle(resource)
    handle.create_or_patch_resource(runtime)

def handle_modified(resource):
    pass

def handle_deleted(resource):
    handle = ResourceHandle(resource)
    pass

def init_resource_handles():
    """Get initial list of anarchy apis"""
    for resource in kube_custom_objects.list_namespaced_custom_object(
        operator_domain, 'v1', operator_namespace, 'resourcehandles'
    ).get('items', []):
        handle_added(resource)

def watch_handles():
    stream = kubernetes.watch.Watch().stream(
        kube_custom_objects.list_namespaced_custom_object,
        operator_domain,
        'v1',
        operator_namespace,
        'resourcehandles'
    )
    for event in stream:
        logger.debug(event)
        event_obj = event['object']
        if event_obj['kind'] == 'Status':
            logger.debug('Watch %s - reason %s, %s',
                event_obj['status'],
                event_obj['reason'],
                event_obj['message']
            )
            if event_obj['status'] == 'Failure':
                if event_obj['reason'] == 'Expired':
                    return
                else:
                    raise Exception("Watch failure: " + event_obj['message'])
        else:
            logger.debug("ResourceHandle %s %s",
                event_obj['metadata']['name'],
                event['type']
            )
            if event['type'] == 'ADDED':
                handle_added(event_obj)
            elif event['type'] == 'MODIFIED':
                handle_modified(event_obj)
            elif event['type'] == 'DELETED':
                handle_deleted(event_obj)

def watch_handles_loop():
    while True:
        try:
            watch_handles()
        except Exception as e:
            logger.exception("Error while watching handles: %s", e)
            time.sleep(60)

def bind_available_handle_to_claim(handler, claim):
    # FIXME - get list of available handles for this handler and then check
    #         each to see if they match the claim
    pass

def find_handler_for_claim(claim):
    handler = ResourceHandler.find_handler(claim)
    if not handler:
        raise Exception("No handler matched claim.")
    handler.validate_claim_template(claim)
    return handler

def bind_handle_to_claim(claim):
    handle = None

    handle_resource = claim.get_handle_resource(runtime)
    if handle_resource:
        handle = ResourceHandle(handle_resource)
        logger.warn(
            "Found unlisted ResourceHandle %s for ResourceClaim %s",
            handle.name(),
            claim.namespace_name()
        )
    else:
        try:
            handler = find_handler_for_claim(claim)
        except Exception as err:
            logger.warn(
                "Unable to find handler for claim %s, %s: '%s'",
                claim.namespace_name(),
                type(err).__name__,
                str(err)
            )
            return

        handle = bind_available_handle_to_claim(handler, claim)
        if not handle:
            handle = ResourceHandle.create_for_claim(runtime, claim, handler)

    claim.set_handle(runtime, handle)

def check_handle_status(claim):
    # FIXME - Verify that handle still exists

    # FIXME - Update resource handle if claim is bound and handle spec.template
    # differs from claim spec.template. The handler updateFilter should apply
    # to changes propagated from claim template to handle template.
    pass

def resource_claim_added(resource):
    claim = ResourceClaim(resource)

    if claim.is_bound():
        check_handle_status(claim)
    else:
        bind_handle_to_claim(claim)

def resource_claim_modified(resource):
    resource_claim_added(resource)

def resource_claim_deleted(resource):
    pass

def watch_resource_claims():
    stream = kubernetes.watch.Watch().stream(
        kube_custom_objects.list_cluster_custom_object,
        operator_domain,
        'v1',
        'resourceclaims'
    )
    for event in stream:
        logger.debug(event)
        event_obj = event['object']
        if event_obj['kind'] == 'Status':
            logger.debug('Watch %s - reason %s, %s',
                event_obj['status'],
                event_obj['reason'],
                event_obj['message']
            )
            if event_obj['status'] == 'Failure':
                if event_obj['reason'] == 'Expired':
                    return
                else:
                    raise Exception("Watch failure: " + event_obj['message'])
        else:
            logger.debug("ResourceClaim %s %s",
                event_obj['metadata']['name'],
                event['type']
            )
            if event['type'] == 'ADDED':
                resource_claim_added(event_obj)
            elif event['type'] == 'MODIFIED':
                resource_claim_modified(event_obj)
            elif event['type'] == 'DELETED':
                resource_claim_deleted(event_obj)

def watch_resource_claims_loop():
    while True:
        try:
            watch_resource_claims()
        except Exception as e:
            logger.exception("Error while watching resource_claims: %s", e)
            time.sleep(60)

def main():
    """Main function."""
    init()

    threading.Thread(
        name = 'handlers',
        target = watch_handlers_loop
    ).start()

    threading.Thread(
        name = 'handles',
        target = watch_handles_loop
    ).start()

    threading.Thread(
        name = 'resourceclaims',
        target = watch_resource_claims_loop
    ).start()

    prometheus_client.start_http_server(8000)

if __name__ == '__main__':
    main()
