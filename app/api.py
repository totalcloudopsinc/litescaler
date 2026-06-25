from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.config import load_config
from app.service import ScalerService


class ScaleRequest(BaseModel):
    nodes_to_add: int = Field(
        ge=1, description="How many nodes to add (clamped to max_size)."
    )

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _build_service() -> ScalerService:
    config = load_config(os.environ.get("CONFIG_PATH", "config.yaml"))
    return ScalerService.from_config(config)


async def _poll_loop(app: FastAPI):
    service: ScalerService = app.state.service
    interval = service.config.scaling.poll_interval_seconds
    while True:
        try:
            await asyncio.to_thread(service.evaluate)
        except Exception:  # noqa: BLE001 - loop must never die
            logger.exception("Evaluation failed in poll loop")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.service = _build_service()
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
