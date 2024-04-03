from __future__ import annotations
import time
from functools import wraps
from aioprometheus import Summary, Counter


class AppMetrics:
    response_time_seconds = Summary("response_time_seconds",
                                    "Response time in seconds",
                                    {"method": "Method used for the request",
                                     "resource_type": "Type of resource requested"
                                     }
                                    )

    request_handler_exceptions = Counter(
        "request_handler_exceptions",
        "Count of exceptions by handler function.",
        {"handler": "Handler function name"}
        )
