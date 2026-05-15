from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from app.llm.types import TaskTier


class ProviderConfig(BaseModel):
    type: Literal["ollama", "anthropic", "openai", "gemini", "mock"]
    model: str = ""
    base_url: str | None = None
    api_key_env: str | None = None
    timeout_s: int = 60
    # Pricing per 1k tokens — used by cost_estimate.
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0


class TierConfig(BaseModel):
    providers: list[str]
    bypass_budget: bool = False
    schema_retry: bool = True


class CacheConfig(BaseModel):
    enabled: bool = True
    ttl_hours: int = 24
    backend: Literal["postgres", "memory"] = "postgres"


class BudgetConfig(BaseModel):
    hourly_usd: float = 5.0


class RouterConfig(BaseModel):
    default_locale: Literal["local", "hosted", "mixed"] = "local"
    tiers: dict[TaskTier, TierConfig]
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)


def load_router_config(path: str | Path) -> RouterConfig:
    raw = yaml.safe_load(Path(path).read_text())
    # Allow tier list shorthand: `extraction: [a, b]` → `{providers: [a, b]}`.
    tiers = raw.get("tiers") or {}
    for k, v in list(tiers.items()):
        if isinstance(v, list):
            tiers[k] = {"providers": v}
    raw["tiers"] = tiers
    return RouterConfig.model_validate(raw)
