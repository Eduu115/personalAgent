from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import db, llm
from ..config import settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32_000)
    conversation_id: UUID | None = None
    model: str | None = None


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat")
async def chat(req: ChatRequest):
    if req.conversation_id is None:
        conversation_id = await db.create_conversation()
        is_new = True
    else:
        if not await db.conversation_exists(req.conversation_id):
            raise HTTPException(404, "esa conversacion no existe")
        conversation_id = req.conversation_id
        is_new = False

    await db.add_message(conversation_id, "user", req.message)
    model = req.model or settings.smart_model

    async def generate():
        yield sse("start", {"conversation_id": str(conversation_id), "model": model})

        pieces: list[str] = []
        usage = None
        try:
            async for delta, chunk_usage in llm.stream_chat(
                [{"role": "system", "content": settings.system_prompt}]
                + await db.history(conversation_id, settings.history_limit),
                model=model,
            ):
                if chunk_usage is not None:
                    usage = chunk_usage
                if delta:
                    pieces.append(delta)
                    yield sse("delta", {"text": delta})
        except asyncio.CancelledError:
            # El cliente cerro la pestana. Guardamos lo generado hasta ahora
            # para no perder la respuesta a medias.
            if pieces:
                await db.add_message(
                    conversation_id, "assistant", "".join(pieces), model=model
                )
            raise
        except Exception as exc:
            log.exception("fallo generando la respuesta")
            yield sse("error", {"message": str(exc)})
            return

        answer = "".join(pieces)
        if answer:
            await db.add_message(
                conversation_id,
                "assistant",
                answer,
                model=model,
                prompt_tokens=usage.prompt_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
            )

        if is_new:
            await db.set_title_if_empty(conversation_id, await llm.title_for(req.message))

        yield sse(
            "done",
            {
                "conversation_id": str(conversation_id),
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
            },
        )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations")
async def conversations(limit: int = 50):
    return {"conversations": await db.list_conversations(min(limit, 200))}


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: UUID, limit: int = 200):
    if not await db.conversation_exists(conversation_id):
        raise HTTPException(404, "esa conversacion no existe")
    return {
        "conversation_id": str(conversation_id),
        "messages": await db.history(conversation_id, min(limit, 500)),
    }
