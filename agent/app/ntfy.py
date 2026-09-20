"""Publicar en ntfy. Lo usan el briefing y la cola de aprobaciones.

Siempre por JSON en el cuerpo, nunca por cabeceras: las cabeceras HTTP no
llevan acentos y los titulos y los cuerpos de aqui si.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)

# ntfy corta los mensajes a 4 KB.
_MAX_BYTES = 3800


def recortar(texto: str, maximo: int = _MAX_BYTES, cola: str = "\n… (sigue en la conversación)") -> str:
    crudo = texto.encode()
    if len(crudo) <= maximo:
        return texto
    return crudo[:maximo].decode("utf-8", "ignore").rstrip() + cola


async def publicar(
    topic: str,
    titulo: str,
    mensaje: str,
    *,
    prioridad: int = 3,
    enlace: str | None = None,
    etiquetas: list[str] | None = None,
    acciones: list[dict[str, Any]] | None = None,
) -> bool:
    if not settings.ntfy_token_publicar:
        log.error("NTFY_TOKEN_PUBLICAR sin configurar: no se publica en '%s'", topic)
        return False
    cuerpo: dict[str, Any] = {
        "topic": topic,
        "title": titulo,
        "message": recortar(mensaje),
        "priority": prioridad,
    }
    if etiquetas:
        cuerpo["tags"] = etiquetas
    if acciones:
        cuerpo["actions"] = acciones
    if enlace:
        cuerpo["click"] = enlace
        if not acciones:
            cuerpo["actions"] = [{"action": "view", "label": "Abrir conversación", "url": enlace}]
    try:
        async with httpx.AsyncClient(timeout=10) as cliente:
            r = await cliente.post(
                settings.ntfy_url,
                json=cuerpo,
                headers={"Authorization": f"Bearer {settings.ntfy_token_publicar}"},
            )
            r.raise_for_status()
    except Exception as exc:
        log.error("no se pudo publicar en ntfy (topic '%s'): %s", topic, exc)
        return False
    return True
