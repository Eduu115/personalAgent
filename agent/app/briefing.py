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
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from apscheduler.triggers.cron import CronTrigger

from . import bucle, db, herramientas, mcp_client
from .config import settings

log = logging.getLogger(__name__)

MADRID = ZoneInfo("Europe/Madrid")
_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
# ntfy corta los mensajes a 4 KB: lo que no quepa sigue en la conversacion.
_MAX_BYTES = 3800
# Un briefing que no salio a su hora sale igual si no han pasado mas de esto:
# un despliegue o un reinicio a las 7:30 no pueden dejar el dia sin briefing.
VENTANA = timedelta(hours=2)
# Con mas retraso que esto, el texto lo dice: un resumen de la manana leido a
# mediodia sin saber de cuando es confunde.
_RETRASO_AVISABLE = timedelta(minutes=10)

PROMPT = """Prepárame el briefing de hoy. Lo leo en el móvil recién levantado: corto, directo, sin preámbulos ni despedidas.

• Agenda de hoy (cal_agenda). Hora y título de cada cosa; di si algo se solapa. Si no hay nada, una línea.
• Correos que importan de las últimas 24 horas (mail_buscar, por ejemplo "in:inbox newer_than:1d -category:promotions -category:social"). Solo lo que me pide hacer algo o que querría saber hoy: quién y de qué va, una línea cada uno. Newsletters y publicidad no cuentan. Si no hay nada, dilo.
• El server (lab_status, y lab_host si hace falta): solo si hay algo raro, en una línea. Un contenedor caído, unhealthy o reiniciando, o la memoria o el disco al límite. Si todo va bien, no hace falta decirlo.

Sobre las alarmas: no tienes memoria de lo que hago yo. Un aviso de seguridad de Google, un inicio de sesión nuevo o una contraseña de aplicación recién creada casi siempre los he provocado yo, y no lo sabes. No des la alarma salvo que haya evidencia clara de que algo va mal. Si algo te parece raro, descríbelo en una línea, sin sacar conclusiones y sin dramatizar. Un briefing que grita "que viene el lobo" cada mañana se deja de leer a los tres días.

Si una herramienta falla, no des esa parte por vacía: no es lo mismo "no hay correos" que "no he podido mirarlos".

Formato: texto plano para una notificación del móvil. Viñetas con "•", nada de encabezados, negritas ni tablas. Unas 15 líneas como mucho."""

# Lo que mira el briefing y de donde sale. Si una parte no se ha podido
# consultar, el briefing lo dice arriba: un briefing que omite la mitad en
# silencio es peor que uno que avisa, porque "no hay correos" tranquiliza.
_AREAS = ["el calendario", "el correo", "el server"]
_AREA_DE_HERRAMIENTA = {
    "cal_agenda": "el calendario",
    "mail_buscar": "el correo",
    "mail_leer": "el correo",
    "lab_status": "el server",
    "lab_host": "el server",
    "lab_stats": "el server",
    "lab_logs": "el server",
}
_AREAS_DE_SERVIDOR = {"google": ["el calendario", "el correo"], "homelab": ["el server"]}


def aviso_no_consultado(no_consultado: dict[str, str]) -> str | None:
    """{area: motivo} -> "No he podido consultar el calendario ni el correo (google-mcp no responde)." """
    if not no_consultado:
        return None
    por_motivo: dict[str, list[str]] = {}
    for area in sorted(no_consultado, key=lambda a: _AREAS.index(a) if a in _AREAS else len(_AREAS)):
        por_motivo.setdefault(no_consultado[area], []).append(area)
    partes = [f"{' ni '.join(areas)} ({motivo})" for motivo, areas in por_motivo.items()]
    return "⚠️ No he podido consultar " + "; ni ".join(partes) + "."


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


def ultimo_disparo(trigger: CronTrigger, ahora: datetime) -> datetime | None:
    """La ultima hora a la que tocaba el cron, si cae dentro de VENTANA."""
    t = trigger.get_next_fire_time(None, ahora - VENTANA)
    ultimo = None
    while t is not None and t <= ahora:
        ultimo = t
        t = trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    return ultimo


async def por_cron(trigger: CronTrigger) -> None:
    """Lo que ejecuta el cron: sabe a que hora tocaba, por si llega tarde."""
    await lanzar(programado=ultimo_disparo(trigger, datetime.now(MADRID)))


async def pendiente(trigger: CronTrigger) -> datetime | None:
    """La hora de un briefing que no ha salido y todavia esta a tiempo, o None.

    El planificador vive en memoria: si el agente estaba parado a las 7:30, al
    arrancar calcula la siguiente para manana y misfire_grace_time no llega a
    mirar la de hoy. Esto si: si tocaba hace menos de VENTANA y no hay
    briefing desde entonces, toca ahora.
    """
    ultimo = ultimo_disparo(trigger, datetime.now(MADRID))
    if ultimo is None or await db.hay_briefing_desde(ultimo):
        return None
    return ultimo


async def lanzar(programado: datetime | None = None) -> dict[str, Any]:
    ahora = datetime.now(MADRID)
    dia = programado or ahora
    titulo = f"Briefing del {_DIAS[dia.weekday()]} {dia:%d/%m}"
    nota_retraso = None
    if programado and ahora - programado > _RETRASO_AVISABLE:
        nota_retraso = f"(Briefing de las {programado:%H:%M}, generado a las {ahora:%H:%M}.)"
    conversation_id = None
    respuesta: str | None = None
    error: str | None = None
    # area -> por que no se ha podido consultar
    no_consultado: dict[str, str] = {}
    log.info("empieza el %s", titulo)
    try:
        # Antes de empezar, que servidores no responden: sus herramientas ni se
        # ofrecen, asi que el modelo no sabe que existen y podria dar por vacio
        # el correo. Se le dice en el prompt, y el aviso de arriba lo pone esto.
        sesiones = mcp_client.sesiones()
        try:
            caidos = (await herramientas.ofrecidas(sesiones)).sin_respuesta
        finally:
            await mcp_client.cerrar(sesiones)
        for servidor in caidos:
            for area in _AREAS_DE_SERVIDOR.get(servidor, [f"lo de {servidor}"]):
                no_consultado[area] = f"{servidor}-mcp no responde"
        prompt = PROMPT
        if no_consultado:
            prompt += (
                f"\n\nHoy no puedes consultar {' ni '.join(no_consultado)}: sus herramientas no "
                "responden. Ese aviso lo pongo yo arriba; tú no digas nada de esa parte, ni que no hay nada."
            )

        conversation_id = await db.create_conversation(titulo)
        await db.add_message(conversation_id, "user", prompt)
        fallidas: dict[str, str] = {}
        bien: set[str] = set()
        async with asyncio.timeout(settings.briefing_timeout):
            async for evento, datos in bucle.conversar(
                conversation_id, model=settings.smart_model, origin="schedule"
            ):
                if evento == "tool" and datos["estado"] == "fin":
                    area = _AREA_DE_HERRAMIENTA.get(datos["nombre"])
                    if area and datos["resultado"] == "executed":
                        bien.add(area)
                    elif area and datos["resultado"] == "failed":
                        fallidas.setdefault(area, f"{datos['nombre']} ha fallado")
                elif evento == "error":
                    error = datos["message"]
                elif evento == "done":
                    respuesta = datos["respuesta"]
        # Un fallo suelto con otra llamada de la misma parte que salio bien (un
        # mail_leer roto entre varios) no es "no he podido consultar el correo".
        for area, motivo in fallidas.items():
            if area not in bien:
                no_consultado.setdefault(area, motivo)

        aviso = aviso_no_consultado(no_consultado)
        prefijo = "\n".join(filter(None, [nota_retraso, aviso]))
        if not error and not respuesta:
            error = "el modelo no ha devuelto texto"
        elif not error and prefijo:
            await db.anteponer_a_respuesta(conversation_id, prefijo + "\n\n")
            respuesta = f"{prefijo}\n\n{respuesta}"
            if aviso and set(_AREAS) <= set(no_consultado):
                # Nada que contar: un briefing vacio no, el aviso de fallo.
                error = aviso
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
        publicado = await _publicar(
            titulo, respuesta or "", prioridad=3, enlace=enlace,
            etiquetas=["warning"] if no_consultado else None,
        )
        log.info("%s listo (publicado en ntfy: %s)", titulo, publicado)

    return {
        "conversation_id": str(conversation_id) if conversation_id else None,
        "titulo": titulo,
        "ok": error is None,
        "error": error,
        "publicado": publicado,
        "respuesta": respuesta,
        "no_consultado": no_consultado,
    }
