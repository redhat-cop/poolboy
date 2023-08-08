#!/usr/bin/python
# Copyright: (c) 2023, Johnathan Kupferer <jkupfere@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import kubernetes
kubernetes.config.load_kube_config()
custom_objects_api = kubernetes.client.CustomObjectsApi()

DOCUMENTATION = r'''
---
module: poolboy_test_merge_patch_status

short_description: Module to patch custom resource status for testing.

version_added: "1.0.0"

description: Part of poolboy test suite to allow for patching resource status in approval test.

options:
    api_version:
        description: apiVersion for resource.
        required: true
        type: str
    name:
        description: name of resource.
        required: true
    namespace:
        description: namespace of resource.
        required: true
        type: str
    plural:
        description: plural of resource to construct api url.
        required: true
        type: str
    status_patch:
        type: dict

author:
    - Johnathan Kupferer (@jkupferer)
'''

EXAMPLES = r'''
- name: Mark ResourceClaim as approved
  poolboy_test_merge_patch_status:
    api_version: "{{ poolboy_domain }}/v1"
    name: test-approval-01
    namespace: "{{ poolboy_test_namespace }}"
    plural: resourceclaims
    status_patch:
      approval:
        state: approved
'''

RETURN = r'''
result:
    description: The result resource object.
    type: dict
    returned: success
'''

from ansible.module_utils.basic import AnsibleModule


def run_module():
    module_args = dict(
        api_version=dict(type='str', required=True),
        name=dict(type='str', required=True),
        namespace=dict(type='str', required=True),
        plural=dict(type='str', required=True),
        status_patch=dict(type='dict', required=True)
    )

    result = dict(
        changed=False,
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    if module.check_mode:
        module.exit_json(**result)

    group, version = module.params['api_version'].split('/')

    result['result'] = custom_objects_api.patch_namespaced_custom_object_status(
        body = dict(status=module.params['status_patch']),
        group = group,
        name = module.params['name'],
        namespace = module.params['namespace'],
        plural = module.params['plural'],
        version = version,
    )

    module.exit_json(**result)


def main():
    run_module()


if __name__ == '__main__':
    main()
