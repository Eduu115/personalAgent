"""El briefing de la manana: agenda, correos que importan y el server si algo va raro.

Lo lanza el planificador del agente (BRIEFING_CRON, 7:30 en Madrid por defecto)
o POST /api/briefing a mano. No usa el bucle del chat: lo que mira es siempre lo
mismo, asi que las lecturas las hace el codigo (por herramientas.ejecutar, con
origin="schedule": solo lectura, auditadas y en su sobre de datos) y el modelo
barato solo redacta, en UNA llamada. Con el bucle eran 3-8 rondas de Sonnet
reenviando todo el contexto: ~6 $/mes de los ~6 $ que gastaba el agente. Deja una conversacion
nueva, "Briefing del ...", para seguir tirando del hilo desde el chat, y avisa
por ntfy. Si falla, avisa tambien y con prioridad alta: es peor un briefing que
no llega en silencio que uno que avisa de que se ha roto.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any
from apscheduler.triggers.cron import CronTrigger

from . import db, herramientas, llm, mcp_client, ntfy
from .config import MADRID, settings

log = logging.getLogger(__name__)

_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
# Un briefing que no salio a su hora sale igual si no han pasado mas de esto:
# un despliegue o un reinicio a las 7:30 no pueden dejar el dia sin briefing.
VENTANA = timedelta(hours=2)
# Con mas retraso que esto, el texto lo dice: un resumen de la manana leido a
# mediodia sin saber de cuando es confunde.
_RETRASO_AVISABLE = timedelta(minutes=10)

PROMPT_PLANTILLA = """Prepárame el briefing de hoy. Lo leo en el móvil recién levantado: corto, directo, sin preámbulos ni despedidas.

• Agenda de hoy (lo de cal_agenda). Hora y título de cada cosa; di si algo se solapa. Si no hay nada, una línea.
• Correos que importan de las últimas 24 horas (lo de mail_buscar, consulta: {query}).
  De ahí, cuéntame solo lo que me pide hacer algo o que querría saber hoy: quién y de qué va, una línea cada uno. Newsletters y publicidad no cuentan. Si no hay nada, dilo.
• Pull requests que reclamen algo (lo de dev_prs). Son varios repos, así que el filtro es lo importante: SOLO las que piden algo de mí, o sea CI en rojo, sin tocar en más de {pr_dias} días, o con reviews pendientes. Una PR abierta ayer que va bien NO se menciona. Una línea cada una: repo, número, título corto y qué le pasa. Si ninguna reclama nada, esta sección no sale: ni "todo en orden", ni el recuento de las que hay abiertas.
  Ojo: "sin checks" no es "pasando". Quiere decir que ese commit no lo comprueba nadie, y en estos repos es lo normal; no lo cuentes como que el CI va bien ni como que va mal.
• El server (lo de lab_status y lab_host): solo si hay algo raro, en una línea. Un contenedor caído, unhealthy o reiniciando, o la memoria o el disco al límite. Si todo va bien, no hace falta decirlo.

Sobre las alarmas: no tienes memoria de lo que hago yo. Un aviso de seguridad de Google, un inicio de sesión nuevo o una contraseña de aplicación recién creada casi siempre los he provocado yo, y no lo sabes. No des la alarma salvo que haya evidencia clara de que algo va mal. Si algo te parece raro, descríbelo en una línea, sin sacar conclusiones y sin dramatizar. Un briefing que grita "que viene el lobo" cada mañana se deja de leer a los tres días.

Más abajo van los resultados de esas herramientas, ya consultadas: no puedes llamar a ninguna más. Si una ha fallado, no des esa parte por vacía: no es lo mismo "no hay correos" que "no he podido mirarlos".

Formato: texto plano para una notificación del móvil. Viñetas con "•", nada de encabezados, negritas ni tablas. Unas 15 líneas como mucho."""

# Lo que mira el briefing y de donde sale. Si una parte no se ha podido
# consultar, el briefing lo dice arriba: un briefing que omite la mitad en
# silencio es peor que uno que avisa, porque "no hay correos" tranquiliza.
_AREAS = ["el calendario", "el correo", "las PRs", "el server"]
_AREA_DE_HERRAMIENTA = {
    "cal_agenda": "el calendario",
    "mail_buscar": "el correo",
    "mail_leer": "el correo",
    "dev_prs": "las PRs",
    "dev_checks": "las PRs",
    "dev_diff": "las PRs",
    "lab_status": "el server",
    "lab_host": "el server",
    "lab_stats": "el server",
    "lab_logs": "el server",
}
_AREAS_DE_SERVIDOR = {
    "google": ["el calendario", "el correo"],
    "homelab": ["el server"],
    "github": ["las PRs"],
}


def aviso_no_consultado(no_consultado: dict[str, str]) -> str | None:
    """{area: motivo} -> "No he podido consultar el calendario ni el correo (google-mcp no responde)." """
    if not no_consultado:
        return None
    por_motivo: dict[str, list[str]] = {}
    for area in sorted(no_consultado, key=lambda a: _AREAS.index(a) if a in _AREAS else len(_AREAS)):
        por_motivo.setdefault(no_consultado[area], []).append(area)
    partes = [f"{' ni '.join(areas)} ({motivo})" for motivo, areas in por_motivo.items()]
    return "⚠️ No he podido consultar " + "; ni ".join(partes) + "."


async def linea_de_vigilancia() -> tuple[str, bool]:
    """(linea, esta_muerta). Cierra el circulo de la proactividad.

    Si el job de vigilancia deja de ejecutarse no llega ningun aviso, y eso es
    exactamente igual que si todo fuera bien. El briefing lo lee todos los dias,
    asi que basta con que diga cuando fue la ultima comprobacion: el dia que
    ponga "hace 9 horas", el silencio de la noche deja de significar nada y lo
    sabes. Ni monitor externo ni heartbeat aparte.

    Si esta parada, la linea sube arriba con los demas avisos en vez de quedarse
    de ultima, que es donde no la leeria nadie.
    """
    ultima = await db.ultima_vigilancia()
    if ultima is None:
        return "Vigilancia: no ha corrido ninguna vez todavía.", True
    minutos = max(0, int((datetime.now(MADRID) - ultima).total_seconds() // 60))
    # Tres veces su intervalo: con el job cada 5 min, mas de 15 es que no corre.
    muerta = minutos > max(15, settings.vigilancia_minutos * 3)
    if minutos < 1:
        hace = "hace menos de un minuto"
    elif minutos < 60:
        hace = f"hace {minutos} min"
    elif minutos < 60 * 48:
        hace = f"hace {minutos // 60} h"
    else:
        hace = f"hace {minutos // 1440} días"
    if muerta:
        return (f"⚠️ La vigilancia no comprueba nada desde {hace} "
                f"({ultima.astimezone(MADRID):%d/%m %H:%M}): que no haya avisos no significa "
                "que todo vaya bien. Mira el log del agente."), True
    return f"Vigilancia: última comprobación {hace}.", False


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


# Lo que lee el briefing, siempre lo mismo. dev_checks no esta: era la unica
# lectura que dependia de lo que viera el modelo, y "CI en rojo" ya sale de dev_prs.
_LECTURAS: list[tuple[str, dict[str, Any]]] = [
    ("cal_agenda", {}),
    ("mail_buscar", {}),  # la consulta se pone al llamar: sale de configuracion
    ("dev_prs", {}),
    ("lab_status", {}),
    ("lab_host", {}),
]


async def _recoger(conversation_id: Any, no_consultado: dict[str, str]) -> str:
    """Hace las lecturas y devuelve sus sobres de datos, uno por herramienta.

    Van por herramientas.ejecutar() y no por el atajo de la consola a
    proposito: con origin="schedule" solo pasa lo que sea `read`, y cada
    llamada queda en tool_calls como cuando las pedia el modelo (regla 4).
    Lo que falla se apunta en no_consultado, que es lo que va arriba.
    """
    sesiones = mcp_client.sesiones()
    try:
        catalogo = await herramientas.ofrecidas(sesiones)
        llamadas = []
        for i, (nombre, args) in enumerate(_LECTURAS):
            if nombre == "mail_buscar":
                args = {"query": settings.query_briefing}
            llamadas.append(llm.Llamada(f"briefing-{i}", nombre, json.dumps(args), args))
        resultados = await asyncio.gather(*(
            herramientas.ejecutar(
                sesiones, catalogo.ruta, ll, conversation_id=conversation_id,
                model=settings.fast_model, origin="schedule",
            )
            for ll in llamadas
        ))
    finally:
        await mcp_client.cerrar(sesiones)
    # Un fallo suelto con otra lectura de la misma parte que salio bien (lab_host
    # roto, lab_status bien) no es "no he podido consultar el server".
    bien = {_AREA_DE_HERRAMIENTA[ll.nombre] for ll, r in zip(llamadas, resultados) if r.status == "executed"}
    for ll, res in zip(llamadas, resultados):
        area = _AREA_DE_HERRAMIENTA[ll.nombre]
        if res.status != "executed" and area not in bien:
            no_consultado.setdefault(area, f"{ll.nombre} ha fallado")
    return "\n\n".join(r.sobre for r in resultados)


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
        prompt = PROMPT_PLANTILLA.format(
            query=settings.query_briefing, pr_dias=settings.briefing_pr_dias
        )
        if anteriores := await db.ultimos_briefings(3):
            # Para que no le cuente tres dias seguidos la misma alerta de Google.
            contados = "\n\n".join(
                f"— {x['creado_en'].astimezone(MADRID):%d/%m}:\n{x['resumen']}" for x in anteriores
            )
            prompt += (
                "\n\nEsto es lo que ya le contaste en los últimos briefings. No repitas nada de aquí "
                "salvo que haya cambiado: si algo sigue igual, o lo omites o lo despachas en media "
                "línea. Lo nuevo es lo que importa.\n\n" + contados
            )
        if no_consultado:
            prompt += (
                f"\n\nHoy no puedes consultar {' ni '.join(no_consultado)}: sus herramientas no "
                "responden. Ese aviso lo pongo yo arriba; tú no digas nada de esa parte, ni que no hay nada."
            )

        conversation_id = await db.create_conversation(titulo)
        async with asyncio.timeout(settings.briefing_timeout):
            datos = await _recoger(conversation_id, no_consultado)
            prompt += "\n\n" + datos
            await db.add_message(conversation_id, "user", prompt)
            respuesta = await llm.complete(prompt, model=settings.fast_model, max_tokens=1024)

        aviso = aviso_no_consultado(no_consultado)
        # El latido de la proactividad. Va SIEMPRE y no lo escribe el modelo:
        # una linea que se puede quedar fuera segun el dia no sirve para
        # comprobar nada. Si la vigilancia esta parada, sube con los avisos.
        latido, vigilancia_muerta = await linea_de_vigilancia()
        prefijo = "\n".join(filter(None, [nota_retraso, aviso, latido if vigilancia_muerta else ""]))
        if not error and not respuesta:
            error = "el modelo no ha devuelto texto"
        elif not error:
            if not vigilancia_muerta:
                respuesta = f"{respuesta.rstrip()}\n\n— {latido}"
            if prefijo:
                respuesta = f"{prefijo}\n\n{respuesta}"
            await db.add_message(conversation_id, "assistant", respuesta, model=settings.fast_model)
            if aviso and set(_AREAS) <= set(no_consultado):
                # Nada que contar: un briefing vacio no, el aviso de fallo.
                error = aviso
    except TimeoutError:
        error = f"no ha terminado en {settings.briefing_timeout:g} s"
    except Exception as exc:
        log.exception("el briefing ha fallado")
        error = str(exc) or type(exc).__name__

    enlace = None
    if settings.puente_base_url and conversation_id:
        enlace = f"{settings.puente_base_url.rstrip('/')}/api/conversations/{conversation_id}"

    if error:
        log.error("%s: ha fallado: %s", titulo, error)
        publicado = await ntfy.publicar(
            settings.ntfy_topic,
            f"{titulo}: ha fallado",
            f"El briefing de hoy no ha salido: {error[:300]}",
            prioridad=4,
            enlace=enlace,
            etiquetas=["warning"],
        )
    else:
        publicado = await ntfy.publicar(
            settings.ntfy_topic, titulo, respuesta or "", prioridad=3, enlace=enlace,
            etiquetas=["warning"] if no_consultado else None,
        )
        log.info("%s listo (publicado en ntfy: %s)", titulo, publicado)

    if respuesta and not error:
        # Se guarda para que el de manana sepa lo que conto el de hoy.
        await db.guardar_briefing(respuesta, publicado)

    return {
        "conversation_id": str(conversation_id) if conversation_id else None,
        "titulo": titulo,
        "ok": error is None,
        "error": error,
        "publicado": publicado,
        "respuesta": respuesta,
        "no_consultado": no_consultado,
    }
