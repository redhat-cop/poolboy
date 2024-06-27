import asyncio
import inflection
import kubernetes_asyncio
import logging

from datetime import datetime, timezone
from typing import Mapping, Optional, TypeVar

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

ResourceWatcherT = TypeVar('ResourceWatcherT', bound='ResourceWatcher')

class ResourceWatcher:
    instances = {}
    class_lock = asyncio.Lock()

    class CacheEntry:
        def __init__(self, resource):
            self.resource = resource
            self.cache_datetime = datetime.now(timezone.utc)

        @property
        def is_expired(self):
            return (datetime.now(timezone.utc) - self.cache_datetime).total_seconds() > Poolboy.resource_refresh_interval

    @classmethod
    def get_watcher(cls,
        api_version: str,
        kind: str,
        namespace: Optional[str] = None,
    ) -> Optional[ResourceWatcherT]:
        key = (api_version, kind, namespace) if namespace else (api_version, kind)
        return ResourceWatcher.instances.get(key)

    @classmethod
    async def get_resource(cls,
        api_version: str,
        kind: str,
        name: str,
        namespace: Optional[str] = None,
    ) -> Optional[Mapping]:
        watcher = cls.get_watcher(api_version=api_version, kind=kind, namespace=namespace)
        if watcher:
            cache_entry = watcher.cache.get(name)
            if cache_entry and not cache_entry.is_expired:
                return cache_entry.resource
        try:
            resource = await poolboy_k8s.get_object(api_version=api_version, kind=kind, name=name, namespace=namespace)
            if resource and watcher:
                watcher.cache[name] = ResourceWatcher.CacheEntry(resource)
            return resource
        except kubernetes_asyncio.client.exceptions.ApiException as exception:
            if exception.status == 404:
                return None
            else:
                raise

    @classmethod
    async def start_resource_watch(cls,
        api_version: str,
        kind: str,
        namespace: str,
    ) -> None:
        key = (api_version, kind, namespace) if namespace else (api_version, kind)
        async with cls.class_lock:
            resource_watcher = cls.instances.get(key)
            if resource_watcher:
                return
            resource_watcher = cls(
                api_version = api_version,
                kind = kind,
                namespace = namespace,
            )
            cls.instances[key] = resource_watcher
            resource_watcher.start()

    @classmethod
    async def stop_all(cls):
        async with cls.class_lock:
            tasks = []
            for resource_watcher in cls.instances.values():
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
                kwargs = dict(group=group, plural=plural, version=version)
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
                    logger.info(f"{self} cancelled")
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
        self.cache.clear()
        try:
            _continue = None
            while True:
                obj_list = await method(**kwargs, _continue=_continue, limit=50)
                for obj in obj_list.get('items', []):
                    if not isinstance(obj, Mapping):
                        obj = Poolboy.api_client.sanitize_for_serialization(event_obj)
                    await self.__watch_event(event_type='PRELOAD', event_obj=obj)
                _continue = obj_list['metadata'].get('continue')
                if not _continue:
                    break

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

                name = event_obj['metadata']['name']
                if event_type == 'DELETED':
                    self.cache.pop(name, None)
                else:
                    self.cache[name] = self.CacheEntry(event_obj)

                await self.__watch_event(event_type=event_type, event_obj=event_obj)
        except kubernetes_asyncio.client.exceptions.ApiException as exception:
            if exception.status == 410:
                raise ResourceWatchRestartError("Received 410 expired response.")
            else:
                raise
        finally:
            if watch:
                await watch.close()

    async def __watch_event(self, event_type, event_obj):
        event_obj_annotations = event_obj['metadata'].get('annotations')
        if not event_obj_annotations:
            return

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
            return

        resource_handle = resourcehandle.ResourceHandle.get_from_cache(name=resource_handle_name)

        if resource_handle:
            await resource_handle.handle_resource_event(logger=logger)
        else:
            logger.debug(
                f"Received event for ResourceHandle {resource_handle_name} "
                f"which seems to have been deleted."
            )
            return

        resource_claim_name = event_obj_annotations.get(resource_claim_name_annotation)
        resource_claim_namespace = event_obj_annotations.get(resource_claim_namespace_annotation)

        if not resource_claim_name or not resource_claim_namespace:
            return

        resource_claim_description = f"ResourceClaim {resource_claim_name} in {resource_claim_namespace}"
        try:
            resource_claim = await resourceclaim.ResourceClaim.get(
                name = resource_claim_name,
                namespace = resource_claim_namespace,
            )

            # Do not manage status for detached ResourceClaim
            if resource_claim.is_detached:
                logger.debug(
                    f"Not handling event for {resource_description} for detached {resource_claim_description}",
                )
                return

            prev_state = resource_claim.status_resources[resource_index].get('state')
            prev_description = (
                f"{prev_state['apiVersion']} {prev_state['kind']} {resource_name} in {resource_namespace}"
                if resource_namespace else
                f"{prev_state['apiVersion']} {prev_state['kind']} {resource_name}"
            ) if prev_state else None
            if resource_claim.resource_handle_name != resource_handle_name:
                logger.info(
                    f"Ignoring resource update on {resource_description} for "
                    f"{resource_claim_description} due to ResourceHandle "
                    f"name mismatch, {resource_claim.resource_handle_name} != {resource_handle_name}"
                )
            elif prev_state and prev_description != resource_description:
                logger.info(
                    f"Ignoring resource update for {resource_claim_description} due to resource "
                    f"mismatch, {resource_description} != {prev_description}"
                )
            elif event_type == 'DELETED':
                if prev_state:
                    await resource_claim.remove_resource_from_status(
                        index=resource_index,
                        logger=logger,
                    )
                else:
                    logger.info(
                        f"Ignoring resource delete for {resource_claim_description} due to resource "
                        f"state not present for {resource_description}"
                    )
            else:
                logger.debug(f"Updating {resource_description} in {resource_claim_description}")
                await resource_claim.update_resource_in_status(
                    index=resource_index,
                    logger=logger,
                    state=event_obj,
                )
        except kubernetes_asyncio.client.exceptions.ApiException as exception:
            if exception.status != 404:
                logger.warning(
                    f"Received {exception.status} response when attempting to patch resource state for "
                    f"{event_type.lower()} {resource_description} for {resource_claim_description}: "
                    f"{exception}"
                )
        except Exception:
            logger.exception(
                f"Exception when attempting to patch resource state for {event_type.lower()} resource "
                f"for {resource_claim_description}"
            )
