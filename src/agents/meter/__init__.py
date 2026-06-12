"""A2A server bootstrap for the meter agent.

Modelled on the foresight-a2a remote-agent layout: an A2AFastAPIApplication
serving the agent card and task endpoints, plus plain health probes, run by
uvicorn on the host/port from config/config.yaml.
"""

import uvicorn
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from src.agents.meter.agent import MeterAgentExecutor
from src.agents.meter.skills import agent_card
from src.utils import settings

__app__: FastAPI | None = None
__server__: uvicorn.Server | None = None


def get_app() -> FastAPI:
    global __app__
    if __app__ is None:
        request_handler = DefaultRequestHandler(
            agent_executor=MeterAgentExecutor(),
            task_store=InMemoryTaskStore(),
        )
        __app__ = A2AFastAPIApplication(
            agent_card=agent_card, http_handler=request_handler
        ).build()

        @__app__.get("/healthz", include_in_schema=False)
        async def healthz() -> PlainTextResponse:
            return PlainTextResponse("OK")

        @__app__.get("/readiness", include_in_schema=False)
        async def readiness() -> PlainTextResponse:
            return PlainTextResponse("OK")

        @__app__.post("/inbox/process")
        async def inbox_process(max_results: int = 10) -> list[dict]:
            """Discrete trigger (UI/CLI) to process unread water-report mail."""
            from src.agents.meter.pipeline import process_inbox
            from src.agents.meter.repository import ensure_schema
            from src.utils.s3 import s3_client

            ensure_schema()
            s3_client().ensure_bucket()
            results = await process_inbox(max_results=max_results)
            return [result.model_dump(mode="json") for result in results]

    return __app__


def get_server() -> uvicorn.Server:
    global __server__
    if __server__ is None:
        cfg = settings.agent_config_by_name("meter")
        config = uvicorn.Config(app=get_app(), host=cfg.host, port=cfg.port)
        __server__ = uvicorn.Server(config)
    return __server__


async def run() -> None:
    await get_server().serve()
