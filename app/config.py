from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class YandexCloudConfig(BaseModel):
    service_account_key_file: str
    node_group_id: str
    cluster_id: str
    master_endpoint: Literal["internal", "external"] = "internal"


class KubernetesConfig(BaseModel):
    namespace: str
    label_selectors: list[str] = Field(min_length=1)


class NodeCapacityFallback(BaseModel):
    cpu: float
    memory_gib: float


class ScalingConfig(BaseModel):
    max_size: int
    pending_pod_threshold: int = 0
    headroom: float = 0.15
    poll_interval_seconds: int = 30
    dry_run: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    min_size: int = Field(default=1, ge=0)
    scale_down_cooldown_polls: int = Field(default=3, ge=1)
    node_capacity_fallback: NodeCapacityFallback = NodeCapacityFallback(
        cpu=4, memory_gib=16
    )

    @model_validator(mode="after")
    def _check_size_bounds(self):
        if self.min_size > self.max_size:
            raise ValueError(
                f"min_size ({self.min_size}) must not exceed max_size "
                f"({self.max_size})"
            )
        return self


class MetricsConfig(BaseModel):
    enabled: bool = True
    port: int = Field(default=9090, ge=1, le=65535)


class Config(BaseModel):
    yandex_cloud: YandexCloudConfig
    kubernetes: KubernetesConfig
    scaling: ScalingConfig
    metrics: MetricsConfig = MetricsConfig()


def load_config(path: str) -> Config:
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    return Config.model_validate(raw)
