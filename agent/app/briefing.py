"""El briefing de la manana: agenda, correos que importan y el server si algo va raro.

Lo lanza el planificador del agente (BRIEFING_CRON, 7:30 en Madrid por defecto)
o POST /api/briefing a mano. Usa el mismo bucle que el chat (bucle.conversar)
con origin="schedule": solo herramientas de lectura. Deja una conversacion
nueva, "Briefing del ...", para seguir tirando del hilo desde el chat, y avisa
por ntfy. Si falla, avisa tambien y con prioridad alta: es peor un briefing que
no llega en silencio que uno que avisa de que se ha roto.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import bucle, db
from .config import settings

log = logging.getLogger(__name__)

MADRID = ZoneInfo("Europe/Madrid")
_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
# ntfy corta los mensajes a 4 KB: lo que no quepa sigue en la conversacion.
_MAX_BYTES = 3800

PROMPT = """Prepárame el briefing de hoy. Lo leo en el móvil recién levantado: corto, directo, sin preámbulos ni despedidas.

• Agenda de hoy (cal_agenda). Hora y título de cada cosa; di si algo se solapa. Si no hay nada, una línea.
• Correos que importan de las últimas 24 horas (mail_buscar, por ejemplo "in:inbox newer_than:1d -category:promotions -category:social"). Solo lo que me pide hacer algo o que querría saber hoy: quién y de qué va, una línea cada uno. Newsletters y publicidad no cuentan. Si no hay nada, dilo.
• El server (lab_status, y lab_host si hace falta): solo si hay algo raro, en una línea. Un contenedor caído, unhealthy o reiniciando, o la memoria o el disco al límite. Si todo va bien, no hace falta decirlo.

Sobre las alarmas: no tienes memoria de lo que hago yo. Un aviso de seguridad de Google, un inicio de sesión nuevo o una contraseña de aplicación recién creada casi siempre los he provocado yo, y no lo sabes. No des la alarma salvo que haya evidencia clara de que algo va mal. Si algo te parece raro, descríbelo en una línea, sin sacar conclusiones y sin dramatizar. Un briefing que grita "que viene el lobo" cada mañana se deja de leer a los tres días.

Formato: texto plano para una notificación del móvil. Viñetas con "•", nada de encabezados, negritas ni tablas. Unas 15 líneas como mucho."""


def _recortar(texto: str) -> str:
    crudo = texto.encode()
    if len(crudo) <= _MAX_BYTES:
        return texto
    return crudo[:_MAX_BYTES].decode("utf-8", "ignore").rstrip() + "\n… (sigue en la conversación)"


async def _publicar(
    titulo: str, mensaje: str, *, prioridad: int, enlace: str | None = None, etiquetas: list[str] | None = None
) -> bool:
    if not settings.ntfy_token_publicar:
        log.error("NTFY_TOKEN_PUBLICAR sin configurar: el briefing no se publica")
        return False
    # En JSON y no en cabeceras: el titulo lleva tildes y las cabeceras HTTP no.
    cuerpo: dict[str, Any] = {
        "topic": settings.ntfy_topic,
        "title": titulo,
        "message": _recortar(mensaje),
        "priority": prioridad,
    }
    if etiquetas:
        cuerpo["tags"] = etiquetas
    if enlace:
        cuerpo["click"] = enlace
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
        log.error("no se pudo publicar en ntfy: %s", exc)
        return False
    return True


async def lanzar() -> dict[str, Any]:
    ahora = datetime.now(MADRID)
    titulo = f"Briefing del {_DIAS[ahora.weekday()]} {ahora:%d/%m}"
    conversation_id = None
    respuesta: str | None = None
    error: str | None = None
    log.info("empieza el %s", titulo)
    try:
        conversation_id = await db.create_conversation(titulo)
        await db.add_message(conversation_id, "user", PROMPT)
        async with asyncio.timeout(settings.briefing_timeout):
            async for evento, datos in bucle.conversar(
                conversation_id, model=settings.smart_model, origin="schedule"
            ):
                if evento == "error":
                    error = datos["message"]
                elif evento == "done":
                    respuesta = datos["respuesta"]
        if not error and not respuesta:
            error = "el modelo no ha devuelto texto"
    except TimeoutError:
        error = f"no ha terminado en {settings.briefing_timeout:g} s"
    except Exception as exc:
        log.exception("el briefing ha fallado")
        error = str(exc) or type(exc).__name__

    enlace = None
    if settings.agente_url_publica and conversation_id:
        enlace = f"{settings.agente_url_publica.rstrip('/')}/api/conversations/{conversation_id}"

    if error:
        log.error("%s: ha fallado: %s", titulo, error)
        publicado = await _publicar(
            f"{titulo}: ha fallado",
            f"El briefing de hoy no ha salido: {error[:300]}",
            prioridad=4,
            enlace=enlace,
            etiquetas=["warning"],
        )
    else:
        publicado = await _publicar(titulo, respuesta or "", prioridad=3, enlace=enlace)
        log.info("%s listo (publicado en ntfy: %s)", titulo, publicado)

    return {
        "conversation_id": str(conversation_id) if conversation_id else None,
        "titulo": titulo,
        "ok": error is None,
        "error": error,
        "publicado": publicado,
        "respuesta": respuesta,
    }
