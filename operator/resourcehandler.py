import collections
import copy
import jinja2
import json
import openapi_core.shortcuts
import openapi_core.wrappers.mock
import openapi_core.extensions.models.models
import openapi_core.schema.schemas.exceptions

jinja2env = jinja2.Environment(
    block_start_string='{%:',
    block_end_string=':%}',
    comment_start_string='{#:',
    comment_end_string=':#}',
    variable_start_string='{{:',
    variable_end_string=':}}'
)
jinja2env.filters['to_json'] = lambda x: json.dumps(x)

def dict_merge(dct, merge_dct):
    """ Recursive dict merge. Inspired by :meth:``dict.update()``, instead of
    updating only top-level keys, dict_merge recurses down into dicts nested
    to an arbitrary depth, updating keys. The ``merge_dct`` is merged into
    ``dct``.
    :param dct: dict onto which the merge is executed
    :param merge_dct: dct merged into dct
    :return: None
    """
    for k, v in merge_dct.items():
        if k in dct \
        and isinstance(dct[k], dict) \
        and isinstance(merge_dct[k], collections.Mapping):
            dict_merge(dct[k], merge_dct[k])
        else:
            dct[k] = copy.deepcopy(merge_dct[k])
    # FIXME? What about lists within dicts? Such as container lists within a pod?

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

class ResourceHandler(object):

    handlers = {}

    @classmethod
    def find_handler(_class, claim):
        for handler in ResourceHandler.handlers.values():
            if handler.match_claim(claim):
                return handler

    @classmethod
    def get(_class, name):
        return ResourceHandler.handlers.get(name, None)

    @classmethod
    def register(_class, resource):
        handler = _class(resource)
        ResourceHandler.handlers[handler.name()] = handler
        return handler

    @classmethod
    def unregister(_class, handler):
        del ResourceHandler.handlers[handler.name()]

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.validator = openapi_core.shortcuts.RequestValidator(
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
                        "ClaimTemplate": self.spec['validation']['openAPIV3Schema']
                    }
                }
            })
        )

    def name(self):
        return self.metadata['name']

    def namespace(self):
        return self.metadata['namespace']

    def resource_version(self):
        return self.metadata['resourceVersion']

    def uid(self):
        return self.metadata['uid']

    def update_filters(self):
        return self.spec.get('updateFilter', [])

    def default_resource(self):
        return copy.deepcopy(self.spec.get('default', {}))

    def match_claim(self, claim):
        claim_template = claim.template()
        cmp_claim_template = copy.deepcopy(claim.template())
        dict_merge(cmp_claim_template, self.spec['match'])
        return claim_template == cmp_claim_template

    def validate_claim_template(self, claim):
        validation_result = self.validator.validate(
            openapi_core.wrappers.mock.MockRequest(
                'http://example.com', 'post', '/claimTemplate',
                path_pattern='/claimTemplate',
                data=json.dumps(claim.template())
            )
        )
        validation_result.raise_for_errors()

    def process_resource_template(self, template, params):
        if 'default' in self.spec:
            resource_definition = copy.deepcopy(self.spec['default'])
        else:
            resource_definition = {}
        dict_merge(resource_definition, template)
        dict_merge(resource_definition, self.spec['override'])
        if 'metadata' not in resource_definition:
            resource_definition['metadata'] = {}
        if 'generateName' not in resource_definition['metadata']:
            resource_definition['metadata']['generateName'] = 'guid-'
        resource_definition['metadata']['name'] = \
            resource_definition['metadata']['generateName'] + \
            '{{: resource_handle.metadata.name[-5:] :}}'

        return recursive_process_template_strings(resource_definition, params)
