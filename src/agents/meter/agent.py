"""Meter agent: Claude-vision extraction wrapped in an A2A executor.

The agent itself (`MeterAgent`) is plain Anthropic SDK code: one image in,
one validated `MeterReading` out. `MeterAgentExecutor` adapts it to the A2A
protocol — images arrive as `FilePart`s on the incoming message, the result
goes back as a `DataPart` on the completed task.
"""

from __future__ import annotations

import base64
import logging

import anthropic
import httpx
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    DataPart,
    FilePart,
    FileWithBytes,
    FileWithUri,
    Message,
    Part,
    TaskState,
    UnsupportedOperationError,
)
from a2a.utils import new_agent_parts_message, new_agent_text_message, new_task
from a2a.utils.errors import ServerError

from src.agents.meter.models import EmailEnvelope, MeterReading, TriageDecision
from src.agents.meter.prompts import EXTRACTION_PROMPT, TRIAGE_PROMPT
from src.utils import settings
from src.utils.llm import anthropic_client

log = logging.getLogger(__name__)

MAX_TOKENS = 2048  # the reading is a small JSON document
MAX_TRIAGE_BODY_CHARS = 2000


class ImageAttachment:
    """One image extracted from an A2A message, ready for the vision call."""

    def __init__(self, name: str, media_type: str, base64_data: str) -> None:
        self.name = name
        self.media_type = media_type
        self.base64_data = base64_data


class MeterAgent:
    """Extracts a structured reading from a single water-meter photo.

    `analyze` is the single-call primitive; consensus voting and arbitration
    on top of it live in `src.agents.meter.consensus`.
    """

    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.llm = settings.agent_config_by_name("meter").llm
        self._client = client or anthropic_client()

    async def analyze(
        self,
        image: ImageAttachment,
        *,
        model: str | None = None,
        temperature: float | None = None,
        prompt: str = EXTRACTION_PROMPT,
    ) -> MeterReading:
        # The arbiter model (claude-fable-5) rejects sampling params — only
        # pass temperature when explicitly requested for voter diversity.
        kwargs: dict = {"temperature": temperature} if temperature is not None else {}
        response = await self._client.messages.parse(
            model=model or self.llm.voter_model,
            max_tokens=MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": image.media_type,
                                "data": image.base64_data,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            output_format=MeterReading,
            **kwargs,
        )

        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise ValueError(
                f"Model returned no usable reading for {image.name} "
                f"(stop_reason={response.stop_reason})"
            )
        return response.parsed_output


class TriageAgent:
    """Classifies an inbound email: water report or not, and which apartment."""

    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._model = settings.agent_config_by_name("meter").llm.voter_model
        self._client = client or anthropic_client()

    async def triage(self, envelope: EmailEnvelope) -> TriageDecision:
        prompt = TRIAGE_PROMPT.format(
            subject=envelope.subject,
            sender=envelope.sender,
            attachments=", ".join(envelope.attachment_filenames) or "(none)",
            body=envelope.body[:MAX_TRIAGE_BODY_CHARS],
        )
        response = await self._client.messages.parse(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            output_format=TriageDecision,
        )

        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise ValueError(
                f"Model returned no usable triage decision for {envelope.msg_id} "
                f"(stop_reason={response.stop_reason})"
            )
        return response.parsed_output


class MeterAgentExecutor(AgentExecutor):
    """A2A executor for the meter agent's extract_meter_reading skill."""

    def __init__(self, agent: MeterAgent | None = None) -> None:
        self._agent = agent or MeterAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if not task:
            task = new_task(context.message)  # type: ignore[arg-type]
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        images = await self._image_attachments(context.message)
        if not images:
            await updater.update_status(
                TaskState.input_required,
                new_agent_text_message(
                    "Attach at least one meter photo (image file part) to read.",
                    task.context_id,
                    task.id,
                ),
                final=True,
            )
            return

        # local import: consensus builds on MeterAgent, so a top-level import
        # here would be circular
        from src.agents.meter.consensus import consensus_read

        readings: list[dict] = []
        try:
            for index, image in enumerate(images, start=1):
                await updater.update_status(
                    TaskState.working,
                    new_agent_text_message(
                        f"Analyzing image {index}/{len(images)}: {image.name}",
                        task.context_id,
                        task.id,
                    ),
                )
                result = await consensus_read(image, self._agent)
                readings.append(
                    {
                        "source_image": image.name,
                        **result.reading.model_dump(),
                        "escalated": result.escalated,
                    }
                )
        except Exception as exc:
            log.exception("meter analysis failed")
            await updater.update_status(
                TaskState.failed,
                new_agent_text_message(
                    f"Meter analysis failed: {exc}", task.context_id, task.id
                ),
                final=True,
            )
            return

        await updater.update_status(
            TaskState.completed,
            new_agent_parts_message(
                [Part(root=DataPart(data={"meters": readings}))],
                task.context_id,
                task.id,
            ),
            final=True,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())

    @staticmethod
    async def _image_attachments(message: Message | None) -> list[ImageAttachment]:
        """Collect image file parts from the incoming message.

        Inline bytes (`FileWithBytes.bytes` is already base64) are used as-is;
        URI parts are downloaded and re-encoded.
        """
        if message is None:
            return []

        images: list[ImageAttachment] = []
        for part in message.parts:
            inner = part.root
            if not isinstance(inner, FilePart):
                continue
            file = inner.file
            media_type = file.mime_type or "image/jpeg"
            if not media_type.startswith("image/"):
                continue
            name = file.name or f"attachment-{len(images) + 1}"

            if isinstance(file, FileWithBytes):
                images.append(ImageAttachment(name, media_type, file.bytes))
            elif isinstance(file, FileWithUri):
                async with httpx.AsyncClient() as http:
                    response = await http.get(file.uri)
                    response.raise_for_status()
                data = base64.standard_b64encode(response.content).decode()
                images.append(ImageAttachment(name, media_type, data))
        return images
