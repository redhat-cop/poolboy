import inflection
import jsonpatch
import kubernetes.client.rest
import re

from resourceclaim import ResourceClaim
from resourcehandle import ResourceHandle
from resourcehandler import ResourceHandler

class OperatorRuntime(object):
    def __init__(self, kube_api, kube_custom_objects, logger, operator_domain, operator_namespace):
        self.api_groups = {}
        self.kube_api = kube_api
        self.kube_custom_objects = kube_custom_objects
        self.logger = logger
        self.operator_domain = operator_domain
        self.operator_namespace = operator_namespace

    def get_namespace(self, namespace):
        return self.kube_api.read_namespace(namespace)

    def get_resource_claim(self, namespace, name):
        return ResourceClaim.get(self, namespace, name)

    def get_resource_handle(self, name):
        return ResourceHandle.get(self, name)

    def get_resource_handler(self, name):
        return ResourceHandler.get(name)

    def create_resource(self, resource_definition):
        if '/' in resource_definition['apiVersion']:
            return self.create_custom_resource(resource_definition)
        else:
            return self.create_core_resource(resource_definition)

    def create_core_resource(self, resource_definition):
        namespace = resource_definition['metadata'].get('namespace', None)
        if namespace:
            method = getattr(
                self.kube_api,
                'create_namespaced_' + inflection.underscore(kind)
            )
            return method(namespace, resource_definition)
        else:
            method = getattr(
                self.kube_api,
                'create_' + inflection.underscore(kind)
            )
            return method(resource_definition)

    def create_custom_resource(self, resource_definition):
        api_group, version = resource_definition['apiVersion'].split('/')
        namespace = resource_definition['metadata'].get('namespace', None)
        plural = self.kind_to_plural(api_group, version, resource_definition['kind'])
        if namespace:
            return self.kube_custom_objects.create_namespaced_custom_object(
                api_group,
                version,
                namespace,
                plural,
                resource_definition
            )
        else:
            return self.kube_custom_objects.create_cluster_custom_object(
                api_group,
                version,
                plural,
                resource_definition
            )

    def get_resource(self, resource_definition):
        if '/' in resource_definition['apiVersion']:
            api_group, version = resource_definition['apiVersion'].split('/')
            return self.get_custom_resource(
                api_group,
                version,
                resource_definition['kind'],
                resource_definition['metadata'].get('namespace', None),
                resource_definition['metadata']['name']
            )
        else:
            return self.get_core_resource(
                resource_definition['kind'],
                resource_definition['metadata'].get('namespace', None),
                resource_definition['metadata']['name']
            )

    def get_core_resource(self, kind, namespace, name):
        if namespace:
            method = getattr(
                self.kube_api,
                'read_namespaced_' + inflection.underscore(kind)
            )
            return method(name, namespace)
        else:
            method = getattr(
                self.kube_api,
                'read_' + inflection.underscore(kind)
            )
            return method(name)

    def get_custom_resource(self, api_group, version, kind, namespace, name):
        plural = self.kind_to_plural(api_group, version, kind)
        try:
            if namespace:
                return self.kube_custom_objects.get_namespaced_custom_object(
                    api_group,
                    version,
                    namespace,
                    plural,
                    name
                )
            else:
                return self.kube_custom_objects.get_cluster_custom_object(
                    api_group,
                    version,
                    plural,
                    name
                )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def filter_patch_item(self, update_filters, item):
        path = item['path']
        op = item['op']

        # Allow changes to resource related operator annotations
        if path.startswith('/metadata/annotations/' + self.operator_domain + '~1resource-'):
            return True

        for f in update_filters:
            if op in f.get('allowedOps', ['add','remove','replace']) \
            and re.match(f['pathMatch'] + '$', path):
                return True

        return False

    def patch_resource(self, resource, resource_definition, update_filters):
        patch = [
            item for item in jsonpatch.JsonPatch.from_diff(
                resource,
                resource_definition
            ) if self.filter_patch_item(update_filters, item)
        ]
        if not patch:
            return resource

        if '/' in resource_definition['apiVersion']:
            api_group, version = resource_definition['apiVersion'].split('/')
            return self.patch_custom_resource(
                api_group,
                version,
                resource_definition['kind'],
                resource_definition['metadata'].get('namespace', None),
                resource_definition['metadata']['name'],
                patch
            )
        else:
            return self.patch_core_resource(
                resource_definition['kind'],
                resource_definition['metadata'].get('namespace', None),
                resource_definition['metadata']['name'],
                patch
            )

    def patch_core_resource(self, kind, namespace, name, patch):
        if namespace:
            method = getattr(
                self.kube_api,
                'patch_namespaced_' + inflection.underscore(kind)
            )
            return method(name, namespace, patch)
        else:
            method = getattr(
                self.kube_api,
                'patch_' + inflection.underscore(kind)
            )
            return method(name, patch)

    def patch_custom_resource(self, api_group, version, kind, namespace, name, patch):
        plural = self.kind_to_plural(api_group, version, kind)
        if namespace:
            return self.kube_custom_objects.patch_namespaced_custom_object(
                api_group,
                version,
                namespace,
                plural,
                name,
                patch
            )
        else:
            return self.kube_custom_objects.patch_cluster_custom_object(
                api_group,
                version,
                plural,
                name,
                patch
            )

    def kind_to_plural(self, api_group, version, kind):
        if api_group in self.api_groups \
        and version in self.api_groups[api_group]:
            for resource in self.api_groups[api_group][version]['resources']:
                if resource['kind'] == kind:
                    return resource['name']

        resp = self.kube_custom_objects.api_client.call_api(
            '/apis/{}/{}'.format(api_group,version),
            'GET',
            response_type='object',
        )
        api_group_info = resp[0]
        if api_group not in self.api_groups:
            self.api_groups[api_group] = {}
        self.api_groups[api_group][version] = api_group_info

        for resource in api_group_info['resources']:
            if resource['kind'] == kind:
                return resource['name']
        raise Exception('Unable to find kind {} in {}/{}', kind, api_group, version)
