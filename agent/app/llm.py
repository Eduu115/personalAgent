"""Cliente de modelos.

Habla con LiteLLM, no con el proveedor. Todo lo que sepa este modulo sobre
el mundo exterior es "hay un endpoint compatible con OpenAI en esta URL".
Cambiar de modelo o de proveedor no toca este fichero.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

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


async def stream_chat(
    messages: list[dict[str, str]],
    model: str | None = None,
) -> AsyncIterator[tuple[str, Usage | None]]:
    """Emite (fragmento_de_texto, None) y, al final, ("", Usage)."""
    model = model or settings.smart_model

    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
    )

    async for chunk in stream:
        if chunk.usage is not None:
            yield "", Usage(
                prompt_tokens=chunk.usage.prompt_tokens,
                output_tokens=chunk.usage.completion_tokens,
            )
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content, None


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
