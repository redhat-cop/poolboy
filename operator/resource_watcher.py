import asyncio
import inflection
import kubernetes_asyncio
import logging

from datetime import datetime
from typing import Mapping, Optional

import poolboy_k8s
import resource_claim as resource_claim_module
import resource_handle as resource_handle_module

from config import core_v1_api, custom_objects_api, operator_domain, operator_api_version , operator_version 

logger = logging.getLogger('resource_watcher')
resource_claim_name_annotation = f"{operator_domain}/resource-claim-name"
resource_claim_namespace_annotation = f"{operator_domain}/resource-claim-namespace"
resource_handle_name_annotation = f"{operator_domain}/resource-handle-name"
resource_handle_namespace_annotation = f"{operator_domain}/resource-handle-namespace"
resource_index_annotation = f"{operator_domain}/resource-index"

class ResourceWatchFailedError(Exception):
    pass

class ResourceWatchRestartError(Exception):
    pass

class ResourceWatcher:
    instances = {}
    lock = asyncio.Lock()

    async def start_resource_watch(
        api_version: str,
        kind: str,
        namespace: str,
    ) -> None:
        key = (api_version, kind, namespace) if namespace else (api_version, kind)
        async with ResourceWatcher.lock:
            resource_watcher = ResourceWatcher.instances.get(key)
            if resource_watcher:
                return
            resource_watcher = ResourceWatcher(
                api_version = api_version,
                kind = kind,
                namespace = namespace,
            )
            ResourceWatcher.instances[key] = resource_watcher
            resource_watcher.start()

    async def stop_all():
        async with ResourceWatcher.lock:
            tasks = []
            for resource_watcher in ResourceWatcher.instances.values():
                resource_watcher.cancel()
                tasks.append(resource_watcher.task)
            await asyncio.gather(*tasks)

    def __init__(self,
        api_version: str,
        kind: str,
        namespace: Optional[str] = None,
    ):
        self.api_version = api_version
        self.cache = {}
        self.kind = kind
        self.namespace = namespace

    @property
    def watch_target_description(self):
        if self.namespace:
            return f"{self.api_version} {self.kind} in {self.namespace}"
        else:
            return f"{self.api_version} {self.kind}"

    def cancel(self):
        self.task.cancel()

    def start(self):
        logger.info(f"Starting resource watch for {self.watch_target_description}")
        self.task = asyncio.create_task(self.watch())

    async def watch(self):
        try:
            if '/' in self.api_version:
                group, version = self.api_version.split('/')
                plural = await poolboy_k8s.kind_to_plural(group=group, version=version, kind=self.kind)
                kwargs = dict(group = group, plural = plural, version = version)
                if self.namespace:
                    method = custom_objects_api.list_namespaced_custom_object
                    kwargs['namespace'] = self.namespace
                else:
                    method = custom_objects_api.list_cluster_custom_object
            elif self.namespace:
                method = getattr(
                    core_v1_api, "list_namespaced_" + inflection.underscore(kind)
                )
                kwargs = dict(namespace=namespace)
            else:
                method = getattr(
                    core_v1_api, "list_" + inflection.underscore(kind)
                )
                kwargs = {}

    
            while True:
                watch_start_dt = datetime.utcnow()
                try:
                    await self.__watch(method, **kwargs)
                except asyncio.CancelledError:
                    return
                except ResourceWatchRestartError as e:
                    logger.info(f"Resource watch for {self.watch_target_description} {e}")
                    watch_duration = (datetime.utcnow() - watch_start_dt).total_seconds()
                    if watch_duration < 10:
                        await asyncio.sleep(10 - watch_duration)
                except ResourceWatchFailedError as e:
                    logger.info(f"Resource watch error for {self.watch_target_description}: {e}")
                    watch_duration = (datetime.utcnow() - watch_start_dt).total_seconds()
                    if watch_duration < 60:
                        await asyncio.sleep(60 - watch_duration)
                except Exception as e:
                    logger.exception(f"Exception watching {self.watch_target_description}")
                    watch_duration = (datetime.utcnow() - watch_start_dt).total_seconds()
                    if watch_duration < 60:
                        await asyncio.sleep(60 - watch_duration)
                logger.info(f"Restarting watch for {self.watch_target_description}")

        except asyncio.CancelledError:
            return

    async def __watch(self, method, **kwargs):
        watch = None
        try:
            watch = kubernetes_asyncio.watch.Watch()
            async for event in watch.stream(method, **kwargs):
                event_obj = event['object']
                event_type = event['type']
                if not isinstance(event_obj, Mapping):
                    event_obj = core_v1_api.api_client.sanitize_for_serialization(event_obj)
                if event_type == 'ERROR':
                    if event_obj['kind'] == 'Status':
                        if event_obj['reason'] in ('Expired', 'Gone'):
                            raise ResourceWatchRestartError(event_obj['reason'].lower())
                        else:
                            raise ResourceWatchFailedError(f"{event_obj['reason']} {event_obj['message']}")
                    else:
                        raise ResourceWatchFailedError(f"UKNOWN EVENT: {event}")

                event_obj_annotations = event_obj['metadata'].get('annotations')
                if not event_obj_annotations:
                    continue

                resource_handle_name = event_obj_annotations.get(resource_handle_name_annotation)
                resource_handle_namespace = event_obj_annotations.get(resource_handle_namespace_annotation)
                resource_index = int(event_obj_annotations.get(resource_index_annotation, 0))

                if not resource_handle_name or not resource_handle_namespace:
                    continue

                resource_handle = resource_handle_module.ResourceHandle.get_from_cache(
                    name = resource_handle_name
                )
                if resource_handle:
                    await resource_handle.handle_resource_event(
                        logger = logger,
                        resource_index = resource_index,
                        resource_state = event_obj,
                    )

                resource_claim_name = event_obj_annotations.get(resource_claim_name_annotation)
                resource_claim_namespace = event_obj_annotations.get(resource_claim_namespace_annotation)
                if not resource_claim_name or not resource_claim_namespace:
                    continue

                if event_type == 'DELETED':
                    try:
                        definition = await custom_objects_api.patch_namespaced_custom_object_status(
                            group = operator_domain,
                            name = resource_claim_name,
                            namespace = resource_claim_namespace,
                            plural = "resourceclaims",
                            version = operator_version,
                            _content_type = 'application/json-patch+json',
                            body = [{
                                "op": "remove",
                                "path": f"/status/resources/{resource_index}/state",
                            }],
                        )
                        await resource_claim_module.ResourceClaim.register_definition(definition=definition)
                    except kubernetes_asyncio.client.exceptions.ApiException as e:
                        if e.status != 404:
                            logger.warning(
                                f"Received {e.status} response when attempting to patch resource state for deleted "
                                f"resource for ResourceClaim {resource_claim_name} in {resource_claim_namespace}: "
                                f"{e}"
                            )
                    except Exception as e:
                        logger.exception(
                            f"Exception when attempting to patch resource state for deleted resource for "
                            f"ResourceClaim {resource_claim_name} in {resource_claim_namespace}"
                        )
                else:
                    try:
                        definition = await custom_objects_api.patch_namespaced_custom_object_status(
                            group = operator_domain,
                            name = resource_claim_name,
                            namespace = resource_claim_namespace,
                            plural = "resourceclaims",
                            version = operator_version,
                            _content_type = 'application/json-patch+json',
                            body = [{
                                "op": "add",
                                "path": f"/status/resources/{resource_index}/state",
                                "value": event_obj,
                            }],
                        )
                        await resource_claim_module.ResourceClaim.register_definition(definition=definition)
                    except kubernetes_asyncio.client.exceptions.ApiException as e:
                        if e.status != 404:
                            logger.warning(
                                f"Received {e.status} response when attempting to patch resource state for "
                                f"ResourceClaim {resource_claim_name} in {resource_claim_namespace}: "
                                f"{e}"
                            )
                    except Exception as e:
                        logger.exception(
                            f"Exception when attempting to patch resource state for ResourceClaim "
                            f"{resource_claim_name} in {resource_claim_namespace}"
                        )
        except kubernetes_asyncio.client.exceptions.ApiException as e:
            if e.status == 410:
                raise ResourceWatchRestartError("Received 410 expired response.")
            else:
                raise
        finally:
            if watch:
                await watch.close()
