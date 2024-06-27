import asyncio

from datetime import datetime, timezone
from typing import List, Mapping, Optional, TypeVar, Union

import kopf
import kubernetes_asyncio

from poolboy import Poolboy

class KopfObject:
    def __init__(self,
        annotations: Union[kopf.Annotations, Mapping],
        labels: Union[kopf.Labels, Mapping],
        meta: Union[kopf.Meta, Mapping],
        name: str,
        namespace: str,
        spec: Union[kopf.Spec, Mapping],
        status: Union[kopf.Status, Mapping],
        uid: str,
    ):
        self.annotations = annotations
        self.labels = labels
        self.lock = asyncio.Lock()
        self.meta = meta
        self.name = name
        self.namespace = namespace
        self.spec = spec
        self.status = status
        self.uid = uid

    def __str__(self) -> str:
        return f"{self.kind} {self.name} in {self.namespace}"

    @property
    def api_group_version(self):
        return f"{self.api_group}/{self.api_version}"

    @property
    def creation_datetime(self):
        return datetime.strptime(self.creation_timestamp, "%Y-%m-%dT%H:%H:%S%z")

    @property
    def creation_timestamp(self) -> str:
        return self.meta['creationTimestamp']

    @property
    def deletion_timestamp(self) -> Optional[str]:
        return self.meta.get('deletionTimestamp')

    @property
    def metadata(self) -> Mapping:
        return self.meta

    @property
    def reference(self) -> Mapping:
        return {
            "apiVersion": self.api_group_version,
            "kind": self.kind,
            "name": self.name,
            "namespace": self.namespace,
        }

    def refresh(self,
        annotations: kopf.Annotations,
        labels: kopf.Labels,
        meta: kopf.Meta,
        spec: kopf.Spec,
        status: kopf.Status,
        uid: str,
    ) -> None:
        self.annotations = annotations
        self.labels = labels
        self.meta = meta
        self.spec = spec
        self.status = status
        self.uid = uid

    def refresh_from_definition(self, definition: Mapping) -> None:
        self.annotations = definition['metadata'].get('annotations', {})
        self.labels = definition['metadata'].get('labels', {})
        self.meta = definition['metadata']
        self.spec = definition['spec']
        self.status = definition.get('status', {})
        self.uid = definition['metadata']['uid']

    async def delete(self):
        try:
            await Poolboy.custom_objects_api.delete_namespaced_custom_object(
                group = self.api_group,
                name = self.name,
                namespace = self.namespace,
                plural = self.plural,
                version = self.api_version,
            )
        except kubernetes_asyncio.client.exceptions.ApiException as e:
            if e.status != 404:
                raise

    async def json_patch(self, patch: List[Mapping]) -> None:
        """Apply json patch to object status and update definition."""
        definition = await Poolboy.custom_objects_api.patch_namespaced_custom_object(
            group = self.api_group,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = self.api_version,
            body = patch,
            _content_type = 'application/json-patch+json',
        )
        self.refresh_from_definition(definition)

    async def json_patch_status(self, patch: List[Mapping]) -> None:
        definition = await Poolboy.custom_objects_api.patch_namespaced_custom_object_status(
            group = self.api_group,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = self.api_version,
            body = patch,
            _content_type = 'application/json-patch+json',
        )
        self.refresh_from_definition(definition)

    async def merge_patch(self, patch: Mapping) -> None:
        """Apply merge patch to object status and update definition."""
        definition = await Poolboy.custom_objects_api.patch_namespaced_custom_object(
            group = self.api_group,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = self.api_version,
            body = patch,
            _content_type = 'application/merge-patch+json'
        )
        self.refresh_from_definition(definition)

    async def merge_patch_status(self, patch: Mapping) -> None:
        """Apply merge patch to object status and update definition."""
        definition = await Poolboy.custom_objects_api.patch_namespaced_custom_object_status(
            group = self.api_group,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = self.api_version,
            body = {
                "status": patch
            },
            _content_type = 'application/merge-patch+json'
        )
        self.refresh_from_definition(definition)
