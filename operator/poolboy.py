import kubernetes_asyncio
import os

class Poolboy():
    manage_claims_interval = int(os.environ.get('MANAGE_CLAIMS_INTERVAL', 60))
    manage_handles_interval = int(os.environ.get('MANAGE_HANDLES_INTERVAL', 60))
    operator_domain = os.environ.get('OPERATOR_DOMAIN', 'poolboy.gpte.redhat.com')
    operator_version = os.environ.get('OPERATOR_VERSION', 'v1')
    operator_api_version = f"{operator_domain}/{operator_version}"
    resource_refresh_interval = int(os.environ.get('RESOURCE_REFRESH_INTERVAL', 600))

    @classmethod
    async def on_cleanup(cls):
        await cls.api_client.close()

    @classmethod
    async def on_startup(cls):
        if os.path.exists('/run/secrets/kubernetes.io/serviceaccount'):
            kubernetes_asyncio.config.load_incluster_config()
            with open('/run/secrets/kubernetes.io/serviceaccount/namespace') as f:
                cls.namespace = f.read()
        else:
            await kubernetes_asyncio.config.load_kube_config()
            if 'OPERATOR_NAMESPACE' in os.environ:
                cls.namespace = os.environ['OPERATOR_NAMESPACE']
            else:
                raise Exception(
                    'Unable to determine operator namespace. '
                    'Please set OPERATOR_NAMESPACE environment variable.'
                )

        cls.api_client = kubernetes_asyncio.client.ApiClient()
        cls.core_v1_api = kubernetes_asyncio.client.CoreV1Api(cls.api_client)
        cls.custom_objects_api = kubernetes_asyncio.client.CustomObjectsApi(cls.api_client)
