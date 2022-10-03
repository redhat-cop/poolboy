import asyncio
import kubernetes_asyncio
import os

manage_claims_interval = int(os.environ.get('MANAGE_CLAIMS_INTERVAL', 60))
manage_handles_interval = int(os.environ.get('MANAGE_HANDLES_INTERVAL', 60))
operator_domain = os.environ.get('OPERATOR_DOMAIN', 'poolboy.gpte.redhat.com')
operator_version = os.environ.get('OPERATOR_VERSION', 'v1')
operator_api_version = f"{operator_domain}/{operator_version}"
resource_refresh_interval = int(os.environ.get('RESOURCE_REFRESH_INTERVAL', 600))

if os.path.exists('/run/secrets/kubernetes.io/serviceaccount'):
    kubernetes_asyncio.config.load_incluster_config()
    with open('/run/secrets/kubernetes.io/serviceaccount/namespace') as f:
        operator_namespace = f.read()
else:
    asyncio.get_event_loop().run_until_complete(kubernetes_asyncio.config.load_kube_config())
    operator_namespace = kubernetes_asyncio.config.list_kube_config_contexts()[1]['context']['namespace']

core_v1_api = kubernetes_asyncio.client.CoreV1Api()
custom_objects_api = kubernetes_asyncio.client.CustomObjectsApi()
