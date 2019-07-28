class ResourceHandle(object):

    @classmethod
    def create_for_claim(_class, runtime, claim, handler):
        resource = runtime.kube_custom_objects.create_namespaced_custom_object(
            runtime.operator_domain,
            'v1',
            runtime.operator_namespace,
            'resourcehandles',
            {
                'apiVersion': runtime.operator_domain + '/v1',
                'kind': 'ResourceHandle',
                'metadata': {
                    'finalizers': [
                        runtime.operator_domain + '/resource-claim-operator'
                    ],
                    'generateName': 'guid-',
                    'labels': {
                        runtime.operator_domain + '/resource-claim-namespace': claim.namespace(),
                        runtime.operator_domain + '/resource-claim-name': claim.name(),
                        runtime.operator_domain + '/resource-handler': handler.name()
                    }
                },
                'spec': {
                    'claim': {
                        'apiVersion': 'v1',
                        'kind': 'ResourceClaim',
                        'name': claim.name(),
                        'namespace': claim.namespace()
                    },
                    'handler': {
                        'apiVersion': 'v1',
                        'kind': 'ResourceHandler',
                        'name': handler.name(),
                        'namespace': handler.namespace()
                    },
                    'template': claim.template()
                }
            }
        )
        return _class(resource)

    @classmethod
    def get(_class, runtime, name):
        return _class(
            runtime.kube_custom_objects.get_namespaced_custom_object(
                runtime.operator_domain,
                'v1',
                runtime.operator_namespace,
                'resourcehandles',
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

    def resource_version(self):
        return self.metadata['resourceVersion']

    def uid(self):
        return self.metadata['uid']

    def handler_name(self):
        return self.spec['handler']['name']

    def create_or_patch_resource(self, runtime):
        resource_definition, patch_filters = self.process_resource_template(runtime)
        resource = runtime.get_resource(resource_definition)
        if resource:
            if self.uid() != resource['metadata'].get('/resource-handle-uid', '') \
            or self.resource_version() != resource['metadata'].get('/resource-handle-version', ''):
                runtime.patch_resource(resource, resource_definition, patch_filters)
        else:
            self.patch(runtime, {
                "spec": {
                    "resource": {
                        "apiVersion": resource_definition['apiVersion'],
                        "kind": resource_definition['kind'],
                        "name": resource_definition['metadata']['name'],
                        "namespace": resource_definition['metadata']['namespace']
                    }
                }
            })
            resource_definition['metadata']['annotations'][runtime.operator_domain + '/resource-handle-version'] = self.resource_version()
            runtime.create_resource(resource_definition)

    def process_resource_template(self, runtime):
        claim_reference = self.spec.get('claim', None)
        if claim_reference:
            resource_claim = runtime.get_resource_claim(
                claim_reference['namespace'],
                claim_reference['name']
            )
            resource_claim_namespace = runtime.get_namespace(claim_reference['namespace'])
            requester_user_name = resource_claim_namespace.metadata.annotations.get('openshift.io/requester', None)
        else:
            resource_claim = {}
            requester_user_name = None

        if requester_user_name:
            requester_user = runtime.kube_custom_objects.get_cluster_custom_object(
                'user.openshift.io',
                'v1',
                'users',
                requester_user_name
            )
            requester_identity = runtime.kube_custom_objects.get_cluster_custom_object(
                'user.openshift.io',
                'v1',
                'identities',
                requester_user['identities'][0]
            )
        else:
            requester_identity = None
            requester_user = None

        handler = runtime.get_resource_handler(self.handler_name())
        resource_definition = handler.process_resource_template(
            self.spec['template'],
            params={
                "requester_identity": requester_identity,
                "requester_user": requester_user,
                "resource_handle": self
            }
        )

        if 'annotations' not in resource_definition['metadata']:
            resource_definition['metadata']['annotations'] = {}

        # Set annotations for resource handle and handler
        resource_definition['metadata']['annotations'].update({
            runtime.operator_domain + '/resource-handle-name': self.name(),
            runtime.operator_domain + '/resource-handle-namespace': self.namespace(),
            runtime.operator_domain + '/resource-handle-uid': self.uid(),
            runtime.operator_domain + '/resource-handle-version': self.resource_version(),
            runtime.operator_domain + '/resource-handler-name': handler.name(),
            runtime.operator_domain + '/resource-handler-namespace': handler.namespace()
        })

        # Set annotations for resource requester
        if requester_user and requester_identity:
            resource_definition['metadata']['annotations'].update({
                runtime.operator_domain + '/resource-requester-email': \
                    requester_identity.get('extra', {}).get('email', ''),
                runtime.operator_domain + '/resource-requester-name': \
                    requester_identity.get('extra', {}).get('name', ''),
                runtime.operator_domain + '/resource-requester-preferred-username': \
                    requester_identity.get('extra', {}).get('preferred_username', ''),
                runtime.operator_domain + '/resource-requester-user': \
                    requester_user['metadata']['name']
            })

        # Set annotations for resource claim
        if resource_claim:
            resource_definition['metadata']['annotations'].update({
                runtime.operator_domain + '/resource-claim-name': resource_claim.name(),
                runtime.operator_domain + '/resource-claim-namespace': resource_claim.namespace()
            })
        runtime.logger.warn(resource_definition)

        return resource_definition, handler.update_filters()

    def patch(self, runtime, patch):
        runtime.logger.info("Before patch resource version " + self.metadata['resourceVersion'])
        resource = runtime.patch_resource(
            {
                'apiVersion': runtime.operator_domain + '/v1',
                'kind': 'ResourceHandle',
                'metadata': self.metadata,
                'spec': self.spec,
            },
            patch,
            [{ 'pathMatch': '/.*', 'allowedOps': ['add','replace'] }]
        )
        self.__init__(resource)
        runtime.logger.info("After patch resource version " + self.metadata['resourceVersion'])
