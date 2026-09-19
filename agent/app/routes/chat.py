from __future__ import annotations

import json
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import bucle, db, llm
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
        async for evento, datos in bucle.conversar(conversation_id, model=model):
            if evento == "done" and is_new:
                await db.set_title_if_empty(conversation_id, await llm.title_for(req.message))
            yield sse(evento, datos)

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
