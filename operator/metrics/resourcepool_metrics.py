from __future__ import annotations
from aioprometheus import Counter, Gauge

from metrics import AppMetrics


class ResourcePoolMetrics(AppMetrics):
    resource_pool_labels = {'name': "Resource pool name",
                            'namespace': "Resource pool namespace"
                            }

    resource_pool_min_available = Gauge(
        'resource_pool_min_available',
        'Minimum number of available environments in each resource pool',
        resource_pool_labels
        )

    resource_pool_available = Gauge(
        'resource_pool_available',
        'Number of available environments in each resource pool',
        resource_pool_labels
        )

    resource_pool_used_total = Counter(
        'resource_pool_used_total',
        'Total number of environments used in each resource pool',
        resource_pool_labels
    )

    resource_pool_state_labels = {'name': "Resource pool name",
                                  'namespace': "Resource pool namespace",
                                  'state': "State of the resource pool"
                                  }
    resource_pool_state = Gauge(
        'resource_pool_state',
        'State of each resource pool, including available and used resources',
        resource_pool_state_labels
    )
