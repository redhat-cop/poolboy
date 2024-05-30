from __future__ import annotations
from aioprometheus import REGISTRY, Counter, Gauge

from .app_metrics import AppMetrics
from .resourcepool_metrics import ResourcePoolMetrics


class MetricsManager:
    metrics_classes = [AppMetrics, ResourcePoolMetrics]

    @classmethod
    def register(cls):
        for metrics_class in cls.metrics_classes:
            for attr_name in dir(metrics_class):
                attr = getattr(metrics_class, attr_name)
                if isinstance(attr, (Counter, Gauge)):
                    if attr.name not in REGISTRY.collectors:
                        REGISTRY.register(attr)
