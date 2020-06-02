#!/usr/bin/env python

import kopf
import kubernetes
import os

if os.path.exists('/run/secrets/kubernetes.io/serviceaccount/token'):
    f = open('/run/secrets/kubernetes.io/serviceaccount/token')
    kube_auth_token = f.read()
    kube_config = kubernetes.client.Configuration()
    kube_config.api_key['authorization'] = kube_auth_token
    kube_config.api_key_prefix['authorization'] = 'Bearer'
    kube_config.host = os.environ['KUBERNETES_PORT'].replace('tcp://', 'https://', 1)
    kube_config.ssl_ca_cert = '/run/secrets/kubernetes.io/serviceaccount/ca.crt'
else:
    kubernetes.config.load_kube_config()
    kube_config = None

api_client = kubernetes.client.ApiClient(kube_config)
custom_objects_api = kubernetes.client.CustomObjectsApi(api_client)
operator_domain = os.environ.get('OPERATOR_DOMAIN', 'poolboy.gpte.redhat.com')

def delete_resource_claim(resource_claim, logger):
    resource_claim_meta = resource_claim['metadata']
    resource_claim_name = resource_claim_meta['name']
    resource_claim_namespace = resource_claim_meta['namespace']
    logger.info("Deleting ResourceClaim %s in %s", resource_claim_name, resource_claim_namespace)
    custom_objects_api.delete_namespaced_custom_object(
        operator_domain, 'v1', resource_claim_namespace,
        'resourceclaims', resource_claim_name
    )

def remove_template_instance_finalizers(template_instance_name, template_instance_namespace, logger):
    logger.info("Deleting TemplateInstance %s in %s", template_instance_name, template_instance_namespace)
    custom_objects_api.patch_namespaced_custom_object(
        'template.openshift.io', 'v1', template_instance_namespace,
        'templateinstances', template_instance_name,
        { 'metadata': { 'finalizers': None } }
    )

def handle_broker_template_instance_delete(broker_template_instance, logger):
    template_instance_ref = broker_template_instance['spec']['templateInstance']
    template_instance_name = template_instance_ref['name']
    template_instance_namespace = template_instance_ref['namespace']
    template_instance_uid = template_instance_ref['uid']
    logger.warning(template_instance_name)

    template_instance = custom_objects_api.get_namespaced_custom_object(
        'template.openshift.io', 'v1', template_instance_namespace,
        'templateinstances', template_instance_name
    )
    if not template_instance['spec']['template'] \
       .get('metadata', {}) \
       .get('labels', {}) \
       .get('gpte.redhat.com/agnosticv', None):
        return

    for resource_claim in custom_objects_api.list_namespaced_custom_object(
        operator_domain, 'v1', template_instance_namespace, 'resourceclaims',
        label_selector='template.openshift.io/template-instance-owner=' + template_instance_uid
    ).get('items', []):
        delete_resource_claim(resource_claim, logger)
        deleted_resource_claim = True

    remove_template_instance_finalizers(template_instance_name, template_instance_namespace, logger)

@kopf.on.event('template.openshift.io', 'v1', 'brokertemplateinstances')
def handle_broker_template_instances(event, logger, **_):
    if event['type'] in ['ADDED', 'MODIFIED', None]:
        obj = event['object']
        if 'deletionTimestamp' in obj['metadata']:
            handle_broker_template_instance_delete(obj, logger)
