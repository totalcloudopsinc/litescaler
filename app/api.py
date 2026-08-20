from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Callable

from fastapi import FastAPI, HTTPException
from prometheus_client import start_http_server
from pydantic import BaseModel, Field

from app import metrics
from app.config import Config, load_config
from app.service import ScalerService


class ScaleRequest(BaseModel):
    nodes_to_add: int = Field(
        ge=1, description="How many nodes to add (clamped to max_size)."
    )

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S%z"

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_logging(level: int | str = logging.INFO) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
    for name in _UVICORN_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            formatter = handler.formatter
            fmt = getattr(formatter, "_fmt", None)
            if not fmt or "%(asctime)s" in fmt:
                continue

            handler.setFormatter(
                type(formatter)(f"%(asctime)s {fmt}", datefmt=LOG_DATEFMT)
            )


configure_logging()
logger = logging.getLogger(__name__)


def _build_service() -> ScalerService:
    config = load_config(os.environ.get("CONFIG_PATH", "config.yaml"))
    logging.getLogger("app").setLevel(config.scaling.log_level)
    logger.info(
        "Starting lite-scaler: group=%s dry_run=%s log_level=%s "
        "poll_interval=%ds",
        config.yandex_cloud.node_group_id, config.scaling.dry_run,
        config.scaling.log_level, config.scaling.poll_interval_seconds,
    )
    return ScalerService.from_config(config)


def start_metrics_server(config: Config) -> None:
    scaling = config.scaling
    metrics.set_static(
        max_size=scaling.max_size,
        min_size=scaling.min_size,
        dry_run=scaling.dry_run,
        version=metrics.version(),
    )
    if not config.metrics.enabled:
        logger.info("Metrics disabled; not serving /metrics")
        return
    start_http_server(config.metrics.port, registry=metrics.registry)
    logger.info("Serving /metrics on port %d", config.metrics.port)


async def _run_one_poll(
    service: ScalerService,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
) -> None:
    started = monotonic()
    failed = False
    try:
        await asyncio.to_thread(service.evaluate)
    except Exception:  # noqa: BLE001 - loop must never die
        failed = True
        logger.exception("Evaluation failed in poll loop")
    metrics.record_poll(
        duration_seconds=monotonic() - started,
        finished_at=wall_clock(),
        failed=failed,
    )


async def _poll_loop(app: FastAPI):
    service: ScalerService = app.state.service
    interval = service.config.scaling.poll_interval_seconds
    while True:
        await _run_one_poll(service)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.service = _build_service()
    start_metrics_server(app.state.service.config)
    task = asyncio.create_task(_poll_loop(app))
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="lite-scaler", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/status")
def status():
    service: ScalerService = app.state.service
    last = service.last_decision
    return {
        "node_group_id": service.config.yandex_cloud.node_group_id,
        "namespace": service.config.kubernetes.namespace,
        "label_selectors": service.config.kubernetes.label_selectors,
        "dry_run": service.config.scaling.dry_run,
        "last_decision": asdict(last) if last else None,
    }


@app.post("/evaluate")
def evaluate(request: ScaleRequest):
    """Manually add a specific number of nodes (clamped to max_size).

    The background poll loop continues to auto-scale from the pending-pod queue;
    this endpoint always performs an explicit, caller-specified scale-up.
    """
    service: ScalerService = app.state.service
    try:
        decision = service.scale_by(request.nodes_to_add)
    except Exception as exc:
        logger.exception("Manual scale failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return asdict(decision)
