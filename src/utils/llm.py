"""Shared Anthropic client factory routed through the platform's LiteLLM gateway.

LiteLLM exposes the Anthropic-native `/v1/messages` route, so pointing the
official SDK's `base_url` at the gateway gives model routing and usage
accounting without changing any call sites.
"""

from __future__ import annotations

from functools import lru_cache

import anthropic

from src.utils import settings


@lru_cache(maxsize=1)
def anthropic_client() -> anthropic.AsyncAnthropic:
    cfg = settings.gateway
    return anthropic.AsyncAnthropic(
        base_url=cfg.url, api_key=cfg.key.get_secret_value()
    )
