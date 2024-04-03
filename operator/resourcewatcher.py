import asyncio
import inflection
import kubernetes_asyncio
import logging

from datetime import datetime, timezone
from typing import Mapping, Optional

import poolboy_k8s
import resourceclaim
import resourcehandle

from poolboy import Poolboy

logger = logging.getLogger('resource_watcher')
resource_claim_name_annotation = f"{Poolboy.operator_domain}/resource-claim-name"
resource_claim_namespace_annotation = f"{Poolboy.operator_domain}/resource-claim-namespace"
resource_handle_name_annotation = f"{Poolboy.operator_domain}/resource-handle-name"
resource_handle_namespace_annotation = f"{Poolboy.operator_domain}/resource-handle-namespace"
resource_index_annotation = f"{Poolboy.operator_domain}/resource-index"

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

    def __str__(self):
        return f"ResourceWatch for {self.watch_target_description}"

    @property
    def watch_target_description(self):
        if self.namespace:
            return f"{self.api_version} {self.kind} in {self.namespace}"
        else:
            return f"{self.api_version} {self.kind}"

    def cancel(self):
        self.task.cancel()

    def start(self):
        logger.info(f"Starting {self}")
        self.task = asyncio.create_task(self.watch())

    async def watch(self):
        try:
            if '/' in self.api_version:
                group, version = self.api_version.split('/')
                plural = await poolboy_k8s.kind_to_plural(group=group, version=version, kind=self.kind)
                kwargs = dict(group = group, plural = plural, version = version)
                if self.namespace:
                    method = Poolboy.custom_objects_api.list_namespaced_custom_object
                    kwargs['namespace'] = self.namespace
                else:
                    method = Poolboy.custom_objects_api.list_cluster_custom_object
            elif self.namespace:
                method = getattr(
                    Poolboy.core_v1_api, "list_namespaced_" + inflection.underscore(kind)
                )
                kwargs = dict(namespace=namespace)
            else:
                method = getattr(
                    Poolboy.core_v1_api, "list_" + inflection.underscore(kind)
                )
                kwargs = {}

            while True:
                watch_start_dt = datetime.now(timezone.utc)
                try:
                    await self.__watch(method, **kwargs)
                except asyncio.CancelledError:
                    return
                except ResourceWatchRestartError as e:
                    logger.info(f"{self} restart: {e}")
                    watch_duration = (datetime.now(timezone.utc) - watch_start_dt).total_seconds()
                    if watch_duration < 10:
                        await asyncio.sleep(10 - watch_duration)
                except ResourceWatchFailedError as e:
                    logger.warning(f"{self} failed: {e}")
                    watch_duration = (datetime.now(timezone.utc) - watch_start_dt).total_seconds()
                    if watch_duration < 60:
                        await asyncio.sleep(60 - watch_duration)
                except Exception as e:
                    logger.exception(f"{self} exception")
                    watch_duration = (datetime.now(timezone.utc) - watch_start_dt).total_seconds()
                    if watch_duration < 60:
                        await asyncio.sleep(60 - watch_duration)
                logger.info(f"Restarting {self}")

        except asyncio.CancelledError:
            return

    async def __watch(self, method, **kwargs):
        watch = None
        try:
            watch = kubernetes_asyncio.watch.Watch()
            async for event in watch.stream(method, **kwargs):
                if not isinstance(event, Mapping):
                    raise ResourceWatchFailedError(f"UNKNOWN EVENT: {event}")

                event_obj = event['object']
                event_type = event['type']
                if not isinstance(event_obj, Mapping):
                    event_obj = Poolboy.api_client.sanitize_for_serialization(event_obj)
                if event_type == 'ERROR':
                    if event_obj['kind'] == 'Status':
                        if event_obj['reason'] in ('Expired', 'Gone'):
                            raise ResourceWatchRestartError(event_obj['reason'].lower())
                        else:
                            raise ResourceWatchFailedError(f"{event_obj['reason']} {event_obj['message']}")
                    else:
                        raise ResourceWatchFailedError(f"UNKNOWN EVENT: {event}")

                event_obj_annotations = event_obj['metadata'].get('annotations')
                if not event_obj_annotations:
                    continue

                resource_handle_name = event_obj_annotations.get(resource_handle_name_annotation)
                resource_handle_namespace = event_obj_annotations.get(resource_handle_namespace_annotation)
                resource_index = int(event_obj_annotations.get(resource_index_annotation, 0))
                resource_name = event_obj['metadata']['name']
                resource_namespace = event_obj['metadata'].get('namespace')
                resource_description = (
                    f"{event_obj['apiVersion']} {event_obj['kind']} {resource_name} in {resource_namespace}"
                    if resource_namespace else
                    f"{event_obj['apiVersion']} {event_obj['kind']} {resource_name}"
                )

                if not resource_handle_name or not resource_handle_namespace:
                    continue

                resource_handle = resourcehandle.ResourceHandle.get_from_cache(
                    name = resource_handle_name
                )
                if resource_handle:
                    await resource_handle.handle_resource_event(
                        logger = logger,
                        resource_index = resource_index,
                        resource_state = event_obj,
                    )
                else:
                    logger.debug(
                        f"Received event for ResourceHande {resource_handle_name} "
                        f"which seems to have been deleted."
                    )
                    continue

                resource_claim_name = event_obj_annotations.get(resource_claim_name_annotation)
                resource_claim_namespace = event_obj_annotations.get(resource_claim_namespace_annotation)

                if not resource_claim_name or not resource_claim_namespace:
                    continue

                resource_claim_description = f"ResourceClaim {resource_claim_name} in {resource_claim_namespace}"
                try:
                    resource_claim = await resourceclaim.ResourceClaim.get(
                        name = resource_claim_name,
                        namespace = resource_claim_namespace,
                    )

                    # Do not manage status for detached ResourceClaim
                    if resource_claim.is_detached:
                        continue

                    prev_state = resource_claim.status_resources[resource_index].get('state')
                    prev_description = (
                        f"{prev_state['apiVersion']} {prev_state['kind']} {resource_name} in {resource_namespace}"
                        if resource_namespace else
                        f"{prev_state['apiVersion']} {prev_state['kind']} {resource_name}"
                    ) if prev_state else None
                    if resource_claim.resource_handle_name != resource_handle_name:
                        logger.info(
                            f"Ignoring resource update for {resource_claim_description} due to ResourceHandle "
                            f"name mismatch, {self.resource_handle_name} != {resource_handle_name}"
                        )
                    elif prev_state and prev_description != resource_description:
                        logger.info(
                            f"Ignoring resource update for {resource_claim_description} due to resource "
                            f"mismatch, {resource_description} != {prev_description}"
                        )
                    elif event_type == 'DELETED':
                        if prev_state:
                            await resource_claim.remove_resource_from_status(resource_index)
                        else:
                            logger.info(
                                f"Ignoring resource delete for {resource_claim_description} due to resource "
                                f"state not present for {resource_description}"
                            )
                    else:
                        await resource_claim.update_resource_in_status(resource_index, event_obj)
                except kubernetes_asyncio.client.exceptions.ApiException as e:
                    if e.status != 404:
                        logger.warning(
                            f"Received {e.status} response when attempting to patch resource state for "
                            f"{event_type.lower()} resource for {resource_claim_description}: "
                            f"{e}"
                        )
                except Exception as e:
                    logger.exception(
                        f"Exception when attempting to patch resource state for {event_type.lower()} resource "
                        f"for {resource_claim_description}"
                    )
        except kubernetes_asyncio.client.exceptions.ApiException as e:
            if e.status == 410:
                raise ResourceWatchRestartError("Received 410 expired response.")
            else:
                raise
        finally:
            if watch:
                await watch.close()
