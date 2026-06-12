"""Shared test helpers: stub Anthropic client, reachability gates, factories."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.agents.meter.agent import ImageAttachment
from src.agents.meter.models import MeterReading
from src.utils import settings

DATA_DIR = Path(__file__).parent / "data"
IMAGES = sorted(DATA_DIR.glob("*.jpg"))


@pytest.fixture(autouse=True)
def _fresh_anthropic_client():
    """New gateway client per test — the cached client's httpx connection pool
    binds to one asyncio event loop, and each test runs its own loop."""
    from src.utils.llm import anthropic_client

    anthropic_client.cache_clear()
    yield


def refusal() -> SimpleNamespace:
    """A parse() result for a safety-classifier decline."""
    return SimpleNamespace(parsed_output=None, stop_reason="refusal")


class StubAsyncAnthropic:
    """Scripted stand-in for AsyncAnthropic.

    `responses` items are consumed in call order. Each may be a pydantic model
    (wrapped as a successful parse), a SimpleNamespace (returned verbatim,
    e.g. `refusal()`), or an Exception instance (raised).
    """

    def __init__(self, responses: list) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses)
        outer = self

        class _Messages:
            async def parse(self, **kwargs):
                outer.calls.append(kwargs)
                item = outer._responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, SimpleNamespace):
                    return item
                return SimpleNamespace(parsed_output=item, stop_reason="end_turn")

        self.messages = _Messages()


def attachment(path: Path) -> ImageAttachment:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return ImageAttachment(path.name, "image/jpeg", data)


def reading(**overrides) -> MeterReading:
    values = {
        "meter_nr": "8APA2080051414",
        "meter_type": "cold_water",
        "meter_reading": 29.890,
        "confidence": 0.95,
        **overrides,
    }
    return MeterReading(**values)


def _gateway_reachable() -> bool:
    try:
        response = httpx.get(
            f"{settings.gateway.url}/v1/models",
            headers={"Authorization": f"Bearer {settings.gateway.key.get_secret_value()}"},
            timeout=2.0,
        )
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _postgres_reachable() -> bool:
    import psycopg

    cfg = settings.sql
    try:
        psycopg.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password.get_secret_value(),
            dbname=cfg.name,
            connect_timeout=2,
        ).close()
        return True
    except psycopg.OperationalError:
        return False


requires_gateway = pytest.mark.skipif(
    not _gateway_reachable(), reason="LiteLLM gateway not reachable"
)
requires_postgres = pytest.mark.skipif(
    not _postgres_reachable(), reason="mollebakken Postgres not reachable"
)
requires_gmail = pytest.mark.skipif(
    not (Path(__file__).parent.parent / ".auth" / "token.json").exists(),
    reason="no Gmail OAuth token in .auth/",
)
