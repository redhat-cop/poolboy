import copy
import jinja2
import kubernetes.client.rest
import re

from util import dict_merge

jinja2env = jinja2.Environment(
    block_start_string='{%:',
    block_end_string=':%}',
    comment_start_string='{#:',
    comment_end_string=':#}',
    variable_start_string='{{:',
    variable_end_string=':}}'
)
jinja2env.filters['to_json'] = lambda x: json.dumps(x)

def recursive_process_template_strings(template, params):
    if isinstance(template, dict):
        return { k: recursive_process_template_strings(v, params) for k, v in template.items() }
    elif isinstance(template, list):
        return [ recursive_process_template_strings(item) for item in template ]
    elif isinstance(template, str):
        j2template = jinja2env.from_string(template)
        return j2template.render(params)
    else:
        return template

def process_resource_template(template, provider, params):
    provider_spec = provider['spec']
    if 'default' in provider_spec:
        resource = copy.deepcopy(provider_spec['default'])
    else:
        resource = {}
    dict_merge(resource, template)
    if 'override' in provider_spec:
        dict_merge(resource, provider_spec['override'])
    if 'metadata' not in resource:
        resource['metadata'] = {}
    if 'generateName' not in resource['metadata']:
        resource['metadata']['generateName'] = 'guid-'
    resource['metadata']['name'] = \
        resource['metadata']['generateName'] + \
        '{{: resource_handle.metadata.name[-5:] :}}'
    return recursive_process_template_strings(resource, params)

class ResourceHandleHandler(object):
    def __init__(self, ko):
        self.ko = ko
        self.resource_watchers = {}

    def added(self, handle):
        handle_name = handle['metadata']['name']
        handle_uid = handle['metadata']['uid']
        handle_version = handle['metadata']['resourceVersion']
        claim_ref = handle['spec'].get('claim', None)

        if claim_ref:
            claim_name, claim_namespace = claim_ref['name'], claim_ref['namespace']
            resource_claim = self.get_resource_claim(claim_namespace, claim_name)
            if not resource_claim:
                # Claim deleted, delete resource handle
                delete_resource_handle(handle_name)
                return
            resource_claim_namespace = self.ko.core_v1_api.read_namespace(claim_namespace)
            requester_user_name = resource_claim_namespace.metadata.annotations.get('openshift.io/requester', None)
        else:
            resource_claim = {}
            requester_user_name = None

        requester_user = self.get_user(requester_user_name)
        requester_identity = self.get_identity(requester_user['identities'][0]) \
            if requester_user else None

        provider_name = handle['spec']['provider']['name']
        provider = self.ko.watchers['ResourceProvider'].get_cached_resource(provider_name)
        if not provider:
            self.ko.logger.warn(
                "ResourceHandle %s refers to missing ResourceProvider %s",
                handle_name,
                provider_name
            )
            return

        resource_definition = process_resource_template(
            handle['spec']['template'],
            provider,
            params={
                "requester_identity": requester_identity,
                "requester_user": requester_user,
                "resource_handle": handle
            }
        )

        if 'annotations' not in resource_definition['metadata']:
            resource_definition['metadata']['annotations'] = {}

        self.set_annotations_for_resource_handle(resource_definition, handle)
        self.set_annotations_for_resource_provider(resource_definition, provider)
        self.set_annotations_for_requester(resource_definition, requester_identity, requester_user)
        if resource_claim:
            self.set_annotations_for_resource_claim(resource_definition, resource_claim)

        self.start_resource_watcher(
            resource_definition['apiVersion'],
            resource_definition['kind'],
            resource_definition['metadata'].get('namespace', None)
        )

        resource = self.ko.get_resource(
            api_version=resource_definition['apiVersion'],
            kind=resource_definition['kind'],
            name=resource_definition['metadata']['name'],
            namespace=resource_definition['metadata'].get('namespace', None)
        )

        if resource:
            annotations = resource['metadata']['annotations']
            if handle_uid != annotations.get(self.ko.operator_domain + '/resource-handle-uid') \
            or handle_version != annotations.get(self.ko.operator_domain + '/resource-handle-version'):
                # Allow changes to resource related operator annotations
                update_filters = provider['spec'].get('updateFilters', []) + [{
                    'pathMatch': '/metadata/annotations/' + re.escape(self.ko.operator_domain) + '~1resource-'
                }]
                self.ko.patch_resource(
                    resource=resource,
                    patch=resource_definition,
                    update_filters=update_filters
                )
        else:
            resource_ref = {
                'apiVersion': resource_definition['apiVersion'],
                'kind': resource_definition['kind'],
                'name': resource_definition['metadata']['name'],
            }
            if 'namespace' in resource_definition['metadata']:
                resource_ref['namespace'] = resource_definition['metadata']['namespace']
            handle = self.patch_handle(
                handle,
                {
                    'spec': {
                        'resource': resource_ref
                    }
                }
            )
            resource_definition['metadata']['annotations'][self.ko.operator_domain + '/resource-handle-version'] = handle['metadata']['resourceVersion']
            resource = self.ko.create_resource(resource_definition)

    def deleted(self, handle):
        # FIXME
        pass

    def modified(self, handle):
        # Handle modified the same as added
        self.added(handle)

    def get_user(self, name):
        if not name:
            return None
        try:
            requester_user = ko.custom_objects_api.get_cluster_custom_object(
                'user.openshift.io',
                'v1',
                'users',
                name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def get_identity(self, name):
        if not name:
            return None
        try:
            return ko.custom_objects_api.get_cluster_custom_object(
                'user.openshift.io',
                'v1',
                'identities',
                name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def get_resource_claim(self, namespace, name):
        try:
            return self.ko.custom_objects_api.get_namespaced_custom_object(
                self.ko.operator_domain,
                'v1',
                namespace,
                'resourceclaims',
                name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def patch_handle(self, handle, update):
        return self.ko.patch_resource(
            handle,
            update,
            [{ 'pathMatch': '/.*', 'allowedOps': ['add','replace'] }]
        )

    def set_annotations_for_resource_claim(self, resource, claim):
        claim_metadata = claim['metadata']
        resource['metadata']['annotations'].update({
            self.ko.operator_domain + '/resource-claim-name': claim_metadata['name'],
            self.ko.operator_domain + '/resource-claim-namespace': claim_metadata['namespace']
        })

    def set_annotations_for_resource_handle(self, resource, handle):
        handle_metadata = handle['metadata']
        resource['metadata']['annotations'].update({
            self.ko.operator_domain + '/resource-handle-name': handle_metadata['name'],
            self.ko.operator_domain + '/resource-handle-namespace': handle_metadata['namespace'],
            self.ko.operator_domain + '/resource-handle-uid': handle_metadata['uid'],
            self.ko.operator_domain + '/resource-handle-version': handle_metadata['resourceVersion']
        })

    def set_annotations_for_resource_provider(self, resource, provider):
        provider_metadata = provider['metadata']
        resource['metadata']['annotations'].update({
            self.ko.operator_domain + '/resource-provider-name': provider_metadata['name'],
            self.ko.operator_domain + '/resource-provider-namespace': provider_metadata['namespace']
        })

    def set_annotations_for_requester(self, resource, user, identity):
        if identity:
            resource_definition['metadata']['annotations'].update({
                runtime.operator_domain + '/resource-requester-email': \
                    identity.get('extra', {}).get('email', ''),
                runtime.operator_domain + '/resource-requester-name': \
                    identity.get('extra', {}).get('name', ''),
                runtime.operator_domain + '/resource-requester-preferred-username': \
                    identity.get('extra', {}).get('preferred_username', ''),
            })
        if user:
            resource_definition['metadata']['annotations'].update({
                runtime.operator_domain + '/resource-requester-user': \
                    user['metadata']['name']
            })

    def resource_watch(self, event):
        self.ko.logger.warn(event)
        resource = event.resource
        metadata = resource['metadata']
        annotations = metadata.get('annotations', {})
        annotation_prefix = self.ko.operator_domain + '/resource-'
        if annotation_prefix + 'handle-name' not in annotations:
            return
        handle_namespace = annotations[annotation_prefix + 'handle-namespace']
        claim_name = annotations.get(annotation_prefix + 'claim-name', None)
        claim_namespace = annotations.get(annotation_prefix + 'claim-namespace', None)

        if handle_namespace == self.ko.operator_namespace \
        and claim_name != None:
            self.update_claim_resource_status(claim_name, claim_namespace, resource)

    def start_resource_watcher(self, api_version, kind, namespace):
        if namespace:
            watcher_name = '{}:{}:{}'.format(api_version, kind, namespace)
        else:
            watcher_name = '{}:{}'.format(api_version, kind)

        if watcher_name in self.resource_watchers:
            return

        if '/' in api_version:
            group, version = api_version.split('/')
        else:
            group, version = None, api_version

        w = self.ko.watcher(
            name=watcher_name,
            plural=self.ko.kind_to_plural(group, version, kind),
            group=group,
            namespace=namespace,
            version=version
        )
        self.resource_watchers[watcher_name] = w
        w.handler = self.resource_watch
        w.start()

    def update_claim_resource_status(self, claim_name, claim_namespace, resource):
        try:
            claim = self.ko.custom_objects_api.get_namespaced_custom_object(
                self.ko.operator_domain,
                'v1',
                claim_namespace,
                'resourceclaims',
                claim_name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise
        self.ko.patch_resource_status(
            claim,
            { 'resource': resource },
            [{ 'pathMatch': '/.*', 'allowedOps': ['add','replace'] }]
        )
