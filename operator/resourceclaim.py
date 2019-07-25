class ResourceClaim(object):

    @classmethod
    def get(_class, runtime, namespace, name):
        return _class(
            runtime.kube_custom_objects.get_namespaced_custom_object(
                runtime.operator_domain,
                'v1',
                namespace,
                'resourceclaims',
                name
            )
        )

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)

    def name(self):
        return self.metadata['name']

    def namespace(self):
        return self.metadata['namespace']

    def namespace_name(self):
        return self.metadata['namespace'] + '/' + self.metadata['name']

    def resource_version(self):
        return self.metadata['resourceVersion']

    def uid(self):
        return self.metadata['uid']

    def template(self):
        return self.spec['template']

    def is_bound(self):
        return self.status and 'handle' in self.status

    def bound_resource_ref(self):
        if self.is_bound():
            return self.status['handle']

    def get_handle_resource(self, runtime):
        # FIXME - Actually do search
        return None

    def set_handle(self, runtime, handle):
        handle_ref = {
            "apiVersion": 'v1',
            "kind": 'ResourceHandle',
            "name": handle.name(),
            "namespace": handle.namespace()
        }

        if self.status == None:
            self.patch_status(runtime, [{
                "op": "add",
                "path": "/status",
                "value": { "handle": handle_ref }
            }])
        else:
            self.patch_status(runtime, [{
                "op": "replace" if 'handle' in self.status else "add",
                "path": "/status/handle",
                "value": handle_ref
            }])

    def patch_status(self, runtime, patch):
        resource = runtime.kube_custom_objects.patch_namespaced_custom_object_status(
            runtime.operator_domain,
            'v1',
            self.namespace(),
            'resourceclaims',
            self.name(),
            patch
        )
        self.__init__(resource)
