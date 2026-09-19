from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from uuid import UUID

import anyio
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import db, herramientas, llm, mcp_client
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

        mensajes = [{"role": "system", "content": settings.system_prompt}] + await db.history(
            conversation_id, settings.history_limit
        )
        sesiones = {n: mcp_client.Sesion(url) for n, url in settings.mcp_servidores.items()}
        # Solo se persiste el texto de la ultima ronda: las intermedias y los
        # resultados de herramientas se quedan en esta peticion y en tool_calls.
        pieces: list[str] = []
        usage = llm.Usage()
        try:
            tools, ruta = await herramientas.ofrecidas(sesiones)
            for ronda_n in itertools.count(1):
                pieces = []
                ronda = None
                async for ev in llm.stream_chat(mensajes, model=model, tools=tools):
                    if isinstance(ev, llm.Ronda):
                        ronda = ev
                    else:
                        pieces.append(ev)
                        yield sse("delta", {"text": ev})
                if ronda is None:
                    raise RuntimeError("stream_chat ha terminado sin emitir la Ronda final")
                if ronda.usage:
                    usage.prompt_tokens = (usage.prompt_tokens or 0) + (ronda.usage.prompt_tokens or 0)
                    usage.output_tokens = (usage.output_tokens or 0) + (ronda.usage.output_tokens or 0)

                if not ronda.llamadas:
                    break

                tope = settings.max_rondas_herramientas
                if ronda_n > tope:
                    # Se cortan y se dice, nunca en silencio. Lo que pidio de
                    # mas queda en tool_calls como rechazado.
                    for llamada in ronda.llamadas:
                        await herramientas.ejecutar(
                            sesiones, ruta, llamada, conversation_id=conversation_id, model=model,
                            rechazo=f"tope de {tope} rondas de herramientas agotado",
                        )
                    aviso = (
                        f"\n\n(He cortado después de {tope} rondas de herramientas "
                        "sin llegar a una respuesta final.)"
                    )
                    pieces.append(aviso)
                    yield sse("delta", {"text": aviso})
                    yield sse("limite", {"rondas": tope, "llamadas_sin_ejecutar": len(ronda.llamadas)})
                    break

                mensajes.append(llm.mensaje_asistente(ronda))
                for llamada in ronda.llamadas:
                    argumentos = llamada.argumentos if llamada.argumentos is not None else llamada.crudo
                    yield sse("tool", {"estado": "inicio", "id": llamada.id, "nombre": llamada.nombre,
                                       "argumentos": argumentos})
                    t0 = time.monotonic()
                    status, sobre = await herramientas.ejecutar(
                        sesiones, ruta, llamada, conversation_id=conversation_id, model=model
                    )
                    yield sse("tool", {"estado": "fin", "id": llamada.id, "nombre": llamada.nombre,
                                       "argumentos": argumentos, "resultado": status,
                                       "duracion_ms": round((time.monotonic() - t0) * 1000)})
                    mensajes.append({"role": "tool", "tool_call_id": llamada.id, "content": sobre})
        except asyncio.CancelledError:
            # El cliente cerro la pestana. Guardamos lo generado hasta ahora
            # para no perder la respuesta a medias. Con escudo: Starlette
            # cancela con anyio, que repite la cancelacion en cada await.
            if pieces:
                with anyio.CancelScope(shield=True):
                    await db.add_message(
                        conversation_id, "assistant", "".join(pieces), model=model
                    )
            raise
        except Exception as exc:
            log.exception("fallo generando la respuesta")
            # Mismo trato que si corta el cliente: lo generado no se pierde.
            # Si lo que ha fallado es la base de datos, esto tambien fallara, y
            # el evento de error tiene que llegar igual.
            if pieces:
                try:
                    await db.add_message(
                        conversation_id, "assistant", "".join(pieces), model=model
                    )
                except Exception:
                    log.exception("no se pudo guardar la respuesta a medias")
            yield sse("error", {"message": str(exc)})
            return
        finally:
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*(s.cerrar() for s in sesiones.values()))

        answer = "".join(pieces)
        if answer:
            await db.add_message(
                conversation_id,
                "assistant",
                answer,
                model=model,
                prompt_tokens=usage.prompt_tokens,
                output_tokens=usage.output_tokens,
            )

        if is_new:
            await db.set_title_if_empty(conversation_id, await llm.title_for(req.message))

        yield sse(
            "done",
            {
                "conversation_id": str(conversation_id),
                "prompt_tokens": usage.prompt_tokens,
                "output_tokens": usage.output_tokens,
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
