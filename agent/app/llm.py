"""Cliente de modelos.

Habla con LiteLLM, no con el proveedor. Todo lo que sepa este modulo sobre
el mundo exterior es "hay un endpoint compatible con OpenAI en esta URL".
Cambiar de modelo o de proveedor no toca este fichero.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from openai import AsyncOpenAI

from .config import settings

log = logging.getLogger(__name__)

client = AsyncOpenAI(
    base_url=settings.litellm_base_url,
    api_key=settings.litellm_master_key,
    max_retries=2,
    timeout=120.0,
)


@dataclass
class Usage:
    prompt_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class Llamada:
    """Una llamada a herramienta tal como la pidio el modelo."""

    id: str
    nombre: str
    crudo: str  # function.arguments tal cual, un string JSON
    argumentos: dict[str, Any] | None  # None si crudo no es un objeto JSON


@dataclass
class Ronda:
    texto: str
    llamadas: list[Llamada]
    usage: Usage | None


def _parsear(crudo: str) -> dict[str, Any] | None:
    try:
        # Una herramienta sin parametros puede llegar con los argumentos vacios.
        argumentos = json.loads(crudo.strip() or "{}")
    except ValueError:
        return None
    return argumentos if isinstance(argumentos, dict) else None


async def stream_chat(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> AsyncIterator[str | Ronda]:
    """Emite los fragmentos de texto segun llegan y, al final, la Ronda entera."""
    model = model or settings.smart_model

    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        **({"tools": tools} if tools else {}),
    )

    texto: list[str] = []
    trozos: dict[int, dict[str, str]] = {}
    usage = None
    async for chunk in stream:
        if chunk.usage is not None:
            usage = Usage(
                prompt_tokens=chunk.usage.prompt_tokens,
                output_tokens=chunk.usage.completion_tokens,
            )
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if not delta:
            continue
        if delta.content:
            texto.append(delta.content)
            yield delta.content
        # Los tool_calls llegan troceados: id y nombre en el primer trozo, los
        # argumentos repartidos entre todos. Se casan por index.
        for tc in delta.tool_calls or []:
            t = trozos.setdefault(tc.index, {"id": "", "nombre": "", "crudo": ""})
            if tc.id:
                t["id"] = tc.id
            if tc.function and tc.function.name:
                t["nombre"] = tc.function.name
            if tc.function and tc.function.arguments:
                t["crudo"] += tc.function.arguments

    llamadas = [
        Llamada(
            # Sin id no se puede casar el resultado con la llamada.
            id=t["id"] or f"call_{uuid4().hex[:24]}",
            nombre=t["nombre"],
            crudo=t["crudo"],
            argumentos=_parsear(t["crudo"]),
        )
        for _, t in sorted(trozos.items())
    ]
    yield Ronda("".join(texto), llamadas, usage)


def mensaje_asistente(ronda: Ronda) -> dict[str, Any]:
    """El turno del asistente que pide herramientas, para el contexto de la ronda siguiente.

    content None y no "": Anthropic rechaza los bloques de texto vacios. Unos
    argumentos que no son JSON se devuelven como {}: el proveedor los parsea y
    un JSON roto tumbaria la peticion en vez de dejar que el modelo reintente.
    """
    return {
        "role": "assistant",
        "content": ronda.texto or None,
        "tool_calls": [
            {
                "id": ll.id,
                "type": "function",
                "function": {
                    "name": ll.nombre,
                    "arguments": json.dumps(ll.argumentos or {}, ensure_ascii=False),
                },
            }
            for ll in ronda.llamadas
        ],
    }


async def complete(prompt: str, model: str | None = None, max_tokens: int = 64) -> str:
    """Una sola respuesta corta, sin streaming. Para titulos y clasificaciones."""
    resp = await client.chat.completions.create(
        model=model or settings.fast_model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


async def title_for(first_message: str) -> str:
    """Titula una conversacion. Se le pide al modelo barato a proposito."""
    try:
        title = await complete(
            "Resume en 4 palabras como maximo, sin comillas ni punto final, "
            f"de que va este mensaje:\n\n{first_message[:500]}"
        )
        return title or first_message[:60]
    except Exception:
        log.warning("no se pudo generar titulo, uso el mensaje", exc_info=True)
        return first_message[:60]
