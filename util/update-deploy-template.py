#!/usr/bin/env python3

import os
import subprocess
import yaml

def get_template_without_crds(path):
    template = yaml.safe_load(open(path))
    template['objects'] = [
        obj for obj in template['objects'] if obj['kind'] != 'CustomResourceDefinition'
    ]
    return template

def get_crds_from_helm_template(chart):
    crds = []
    for item in yaml.safe_load_all(subprocess.check_output(['helm', 'template', chart])):
        if item['kind'] == 'CustomResourceDefinition':
            item['metadata']['name'] = item['spec']['names']['plural'] + '.${OPERATOR_DOMAIN}'
            item['spec']['group'] = '${OPERATOR_DOMAIN}'
            crds.append(item)
    return crds

def main():
    deploy_template_path = os.path.join(os.path.dirname(__file__), '../deploy-template.yaml')
    template = get_template_without_crds(deploy_template_path)
    template['objects'].extend(
        get_crds_from_helm_template(os.path.join(os.path.dirname(__file__), '../helm'))
    )
    yaml.safe_dump(template, stream=open(deploy_template_path, 'w'))

if __name__ == '__main__':
    main()
