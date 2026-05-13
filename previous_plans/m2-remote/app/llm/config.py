from __future__ import annotations

import os
from typing import Literal

import yaml
from pydantic import BaseModel


class ProviderConfig(BaseModel):
    type: Literal["vllm", "anthropic", "openai", "gemini", "mock"]
    base_url: str | None = None
    model: str
    api_key_env: str | None = None
    timeout_s: int = 60

    @property
    def api_key(self) -> str | None:
        if self.api_key_env:
            return os.environ.get(self.api_key_env) or None
        return None


class CacheConfig(BaseModel):
    enabled: bool = True
    ttl_hours: int = 24
    backend: Literal["postgres"] = "postgres"


class RouterConfig(BaseModel):
    default_locale: str = "local"
    tiers: dict[str, list[str]]
    providers: dict[str, ProviderConfig]
    cache: CacheConfig = CacheConfig()


def load_router_config(path: str) -> RouterConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return RouterConfig.model_validate(raw)
