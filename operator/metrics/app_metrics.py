from __future__ import annotations
import time
from functools import wraps
from aioprometheus import Summary


class AppMetrics:
    # Summary: Similar to a histogram, a summary samples observations
    # (usually things like request durations and response sizes).
    # While it also provides a total count of observations and a sum of all observed values,
    # it calculates configurable quantiles over a sliding time window.
    response_time_seconds = Summary("response_time_seconds",
                                    "Response time in seconds",
                                    {"method": "Method used for the request",
                                     "resource_type": "Type of resource requested"
                                     }
                                    )

    @staticmethod
    def measure_execution_time(metric_name, **labels):

        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                metric = getattr(AppMetrics, metric_name, None)
                start_time = time.time()
                result = await func(*args, **kwargs)
                end_time = time.time()
                duration = end_time - start_time
                if metric and callable(getattr(metric, 'observe', None)):
                    metric.observe(labels=labels, value=duration)
                else:
                    print(f"Metric {metric_name} not found or doesn't support observe()")
                return result

            return wrapper

        return decorator
