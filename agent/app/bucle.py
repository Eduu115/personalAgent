"""El bucle de herramientas: el mismo para el chat y para las tareas programadas.

Si el chat y el briefing tuvieran cada uno su copia, divergirian y el briefing
empezaria a comportarse distinto que el chat sin que nadie supiera por que.
Aqui hay una sola: /api/chat la convierte en SSE y el briefing la consume.

Emite (evento, datos):
    delta   {"text"}                            texto segun llega, de todas las rondas
    tool    {"estado": inicio|fin, ...}         cada llamada a herramienta
    limite  {"rondas", "llamadas_sin_ejecutar"} se agotaron las rondas
    aprobacion {"id", "herramienta", "caduca"}  una escritura espera el OK de Edu
    error   {"message"}                         y se acaba ahi
    done    {"conversation_id", "prompt_tokens", "output_tokens", "respuesta"}
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import anyio

from . import db, herramientas, llm, mcp_client
from .config import MADRID, settings

log = logging.getLogger(__name__)


@dataclass
class Reanudacion:
    """El turno que quedo a medias esperando un OK, para retomarlo."""

    tool_call_id: int
    nombre: str
    argumentos: dict[str, Any]
    sobre: str  # el resultado de la herramienta, o el motivo del rechazo


async def conversar(
    conversation_id: UUID,
    *,
    model: str,
    origin: str = "user",
    reanudacion: Reanudacion | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Responde al ultimo mensaje de la conversacion y persiste la respuesta.

    `origin` va a cada llamada: "user" desde la consola, "schedule" las tareas
    programadas, que solo pueden usar herramientas de lectura.

    Con `reanudacion`, retoma un turno que se quedo esperando una aprobacion.
    """
    mensajes = [{"role": "system", "content": settings.system_prompt}] + await db.history(
        conversation_id, settings.history_limit
    )
    if reanudacion:
        # El tool_use que pidio permiso y su resultado. No estan en `messages` a
        # proposito (el historial persistido es solo user/assistant, que es lo
        # que evita los tool huerfanos): se reconstruyen desde tool_calls.
        idl = f"call_{reanudacion.tool_call_id}"
        mensajes += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": idl,
                    "type": "function",
                    "function": {
                        "name": reanudacion.nombre,
                        "arguments": json.dumps(reanudacion.argumentos, ensure_ascii=False),
                    },
                }],
            },
            {"role": "tool", "tool_call_id": idl, "content": reanudacion.sobre},
        ]
    sesiones = mcp_client.sesiones()
    # Solo se persiste el texto de la ultima ronda: las intermedias y los
    # resultados de herramientas se quedan en esta peticion y en tool_calls.
    pieces: list[str] = []
    usage = llm.Usage()
    try:
        catalogo = await herramientas.ofrecidas(sesiones)
        # Lo que este origen no puede ejecutar ni se le ofrece; ejecutar() lo
        # rechaza igual si lo pide, que es donde de verdad se hace cumplir.
        tools = [t for t in catalogo.tools if herramientas.permitida(t["function"]["name"], origin)]
        ruta = catalogo.ruta
        for ronda_n in itertools.count(1):
            pieces = []
            ronda = None
            async for ev in llm.stream_chat(mensajes, model=model, tools=tools):
                if isinstance(ev, llm.Ronda):
                    ronda = ev
                else:
                    pieces.append(ev)
                    yield "delta", {"text": ev}
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
                        rechazo=f"tope de {tope} rondas de herramientas agotado", origin=origin,
                    )
                aviso = (
                    f"\n\n(He cortado después de {tope} rondas de herramientas "
                    "sin llegar a una respuesta final.)"
                )
                pieces.append(aviso)
                yield "delta", {"text": aviso}
                yield "limite", {"rondas": tope, "llamadas_sin_ejecutar": len(ronda.llamadas)}
                break

            mensajes.append(llm.mensaje_asistente(ronda))
            pendiente = None
            for llamada in ronda.llamadas:
                argumentos = llamada.argumentos if llamada.argumentos is not None else llamada.crudo
                yield "tool", {"estado": "inicio", "id": llamada.id, "nombre": llamada.nombre,
                               "argumentos": argumentos}
                t0 = time.monotonic()
                res = await herramientas.ejecutar(
                    sesiones, ruta, llamada, conversation_id=conversation_id, model=model, origin=origin
                )
                yield "tool", {"estado": "fin", "id": llamada.id, "nombre": llamada.nombre,
                               "argumentos": argumentos, "resultado": res.status,
                               "duracion_ms": round((time.monotonic() - t0) * 1000)}
                if res.status == "pending":
                    # Se corta la ronda aqui: sin el resultado de esta llamada no
                    # hay nada mas que decirle al modelo. Lo que pidiera despues
                    # en la misma ronda no se ejecuta.
                    pendiente = res.pendiente
                    break
                mensajes.append({"role": "tool", "tool_call_id": llamada.id, "content": res.sobre})

            if pendiente:
                if pendiente.get("reutilizada"):
                    desde = pendiente["requested_at"].astimezone(MADRID).strftime("%H:%M")
                    aviso = (
                        f"\n\nEsa misma acción ({pendiente['tool_name']}) ya está esperando tu OK "
                        f"desde las {desde}, así que no he pedido permiso otra vez. "
                        "Resuelve esa notificación y vale para las dos."
                    )
                else:
                    aviso = (
                        f"\n\nHe pedido permiso para {pendiente['tool_name']}. "
                        "Te aviso en cuanto lo apruebes o lo rechaces."
                    )
                pieces.append(aviso)
                yield "delta", {"text": aviso}
                yield "aprobacion", {
                    "id": pendiente["id"],
                    "herramienta": pendiente["tool_name"],
                    "riesgo": pendiente["risk"],
                    "caduca": pendiente["expires_at"].isoformat(),
                    "reutilizada": bool(pendiente.get("reutilizada")),
                }
                break
    except asyncio.CancelledError:
        # El cliente cerro la pestana (o vencio el tiempo del briefing).
        # Guardamos lo generado hasta ahora para no perder la respuesta a
        # medias. Con escudo: Starlette cancela con anyio, que repite la
        # cancelacion en cada await.
        if pieces:
            with anyio.CancelScope(shield=True):
                await db.add_message(conversation_id, "assistant", "".join(pieces), model=model)
        raise
    except Exception as exc:
        log.exception("fallo generando la respuesta")
        # Mismo trato que si corta el cliente: lo generado no se pierde.
        # Si lo que ha fallado es la base de datos, esto tambien fallara, y
        # el evento de error tiene que llegar igual.
        if pieces:
            try:
                await db.add_message(conversation_id, "assistant", "".join(pieces), model=model)
            except Exception:
                log.exception("no se pudo guardar la respuesta a medias")
        yield "error", {"message": str(exc)}
        return
    finally:
        with anyio.CancelScope(shield=True):
            await mcp_client.cerrar(sesiones)

    respuesta = "".join(pieces)
    if respuesta:
        await db.add_message(
            conversation_id,
            "assistant",
            respuesta,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            output_tokens=usage.output_tokens,
        )
    yield "done", {
        "conversation_id": str(conversation_id),
        "prompt_tokens": usage.prompt_tokens,
        "output_tokens": usage.output_tokens,
        "respuesta": respuesta,
    }
