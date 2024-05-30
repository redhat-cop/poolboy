from __future__ import annotations
import os
import logging
from aioprometheus.service import Service


logger = logging.getLogger(__name__)


class MetricsService:
    service = Service()

    @classmethod
    async def start(cls, addr="0.0.0.0", port=8000) -> None:
        port = int(os.environ.get("METRICS_PORT", port))
        await cls.service.start(addr=addr, port=port, metrics_url="/metrics")
        logger.info(f"Serving metrics on: {cls.service.metrics_url}")

    @classmethod
    async def stop(cls) -> None:
        logger.info("Stopping metrics service")
        await cls.service.stop()
