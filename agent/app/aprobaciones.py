"""La cola de aprobaciones: lo que el agente no hace solo.

Una herramienta `write` o `sensitive` no se ejecuta cuando el modelo la pide.
Se encola en `tool_calls` con status='pending', un nonce de un solo uso y 15
minutos de caducidad, y sale un push con dos botones. La misma fila pasa luego
a approved y a executed o failed: el audit log cuenta la historia entera.

Mientras hay una fila pending, su conversacion no admite mensajes nuevos
(routes/chat.py): un tool_use sin su tool_result, con mensajes de usuario por
medio, es justo lo que la API rechaza.

Al resolver, la ejecucion y la reanudacion del bucle van en una tarea de fondo:
el movil recibe su 200 y no espera a que el modelo termine de hablar.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from . import db, mcp_client, ntfy
from .config import MADRID, settings
from .llm import Llamada

log = logging.getLogger(__name__)

# Lo que va a pasar de verdad si se aprueba, para el push. Para `sensitive` no
# basta con el nombre de la herramienta (regla 3 de CLAUDE.md). Se formatean con
# los argumentos de la llamada: el push tiene que decir QUE se toca.
_QUE_PASA = {
    "lab_reiniciar": "Se para y se arranca {contenedor}: unos segundos sin servicio.",
    "mail_borrador": "Se guarda un borrador en Gmail. NO se envía: no existe ninguna herramienta que envíe.",
    "lab_update_stack": (
        "Descarga las imágenes nuevas de {stack} y recrea sus contenedores: ese servicio "
        "estará caído unos segundos. No hay vuelta atrás automática, pero el aviso del "
        "final trae los digests de ahora para poder volver."
    ),
}


def _que_pasa(fila: dict[str, Any]) -> str:
    plantilla = _QUE_PASA.get(fila["tool_name"], "Es una acción con efectos.")
    try:
        return plantilla.format(**(fila["arguments"] or {}))
    except (KeyError, IndexError):
        return plantilla


class Rechazada(Exception):
    """Un intento de resolver que no cuela. Lleva el codigo HTTP y el motivo."""

    def __init__(self, codigo: int, motivo: str) -> None:
        super().__init__(motivo)
        self.codigo = codigo
        self.motivo = motivo


async def _sin_efecto(motivo: str, fila: dict[str, Any] | None = None) -> None:
    """Avisa de una pulsacion que no ha hecho nada.

    La app de ntfy no da ninguna senal al pulsar un boton, asi que sin esto el
    usuario no sabe si ha pulsado. Siempre hay respuesta a un boton.
    """
    nombre = fila["tool_name"] if fila else None
    await ntfy.publicar(
        settings.ntfy_topic_aprobaciones,
        f"Sin efecto: {nombre}" if nombre else "Sin efecto",
        f"{motivo}. No se ha ejecutado nada.",
        etiquetas=["no_entry"],
    )


def _detalle(fila: dict[str, Any]) -> str:
    """Los argumentos tal cual, sin recortar: es lo que se va a ejecutar."""
    lineas = []
    for clave, valor in (fila["arguments"] or {}).items():
        texto = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)
        lineas.append(f"{clave}:\n{texto}" if "\n" in texto else f"{clave}: {texto}")
    return "\n".join(lineas)


async def encolar(
    llamada: Llamada, *, conversation_id: Any, riesgo: str, model: str
) -> dict[str, Any]:
    """Deja la llamada esperando un OK y manda el push con los botones.

    Si ya hay una pendiente igual (misma herramienta, mismos argumentos, viva),
    se reutiliza y no sale un segundo push: dos notificaciones identicas acaban
    en la accion ejecutada dos veces.
    """
    if (ya := await db.pendiente_igual(llamada.nombre, llamada.argumentos or {})) is not None:
        log.info("pendiente #%s reutilizada: %s con los mismos argumentos", ya["id"], llamada.nombre)
        ya["reutilizada"] = True
        return ya

    nonce = secrets.token_urlsafe(24)
    fila = await db.crear_pendiente(
        llamada.nombre,
        conversation_id=conversation_id,
        risk=riesgo,
        arguments=llamada.argumentos or {},
        model=model,
        nonce=nonce,
        minutos=settings.aprobacion_minutos,
    )
    log.info("pendiente #%s: %s (%s), caduca %s", fila["id"], llamada.nombre, riesgo, fila["expires_at"])

    base = settings.puente_base_url.rstrip("/")
    acciones = []
    if base:
        acciones = [
            {
                "action": "http",
                "label": etiqueta,
                "url": f"{base}/api/aprobaciones/{fila['id']}/{accion}?n={nonce}",
                "method": "POST",
                "clear": True,
            }
            for etiqueta, accion in (("Aprobar", "aprobar"), ("Rechazar", "rechazar"))
        ]
    else:
        log.error("PUENTE_BASE_URL sin configurar: el push va sin botones")

    caduca = fila["expires_at"].astimezone(MADRID).strftime("%H:%M")
    mensaje = "\n".join(
        [
            _detalle(fila),
            "",
            _que_pasa(fila),
            f"Caduca a las {caduca}. Si caduca, no se hace nada.",
        ]
    )
    await ntfy.publicar(
        settings.ntfy_topic_aprobaciones,
        f"¿{fila['tool_name']}?",
        mensaje,
        prioridad=4,
        etiquetas=["lock"],
        acciones=acciones,
    )
    return fila


async def resolver(tool_call_id: int, nonce: str, accion: str) -> dict[str, Any]:
    """Valida el toque del boton y saca la fila de pending. Sin ejecutar nada."""
    fila = await db.llamada(tool_call_id)
    if fila is None:
        await _sin_efecto("Esa aprobación no existe")
        raise Rechazada(404, "esa aprobación no existe")
    if fila["status"] != "pending":
        cuando = fila["resolved_at"].astimezone(MADRID).strftime("%H:%M") if fila["resolved_at"] else "antes"
        como = {
            "approved": "ya se aprobó", "executed": "ya se aprobó y se ejecutó",
            "failed": "ya se aprobó, y al ejecutarla fallo", "rejected": "ya la rechazaste",
            "expired": "ya había caducado",
        }.get(fila["status"], f"ya estaba '{fila['status']}'")
        await _sin_efecto(f"Esa acción {como} ({cuando})", fila)
        raise Rechazada(409, f"esa acción ya estaba '{fila['status']}': no se hace nada")
    # En tiempo constante y con el nonce de la fila, que es de un solo uso.
    if not fila["nonce"] or not secrets.compare_digest(fila["nonce"], nonce):
        # Esto no es un despiste: es alguien tocando una URL de aprobacion con
        # un nonce que no le toca.
        log.warning(
            "NONCE INVALIDO en la aprobacion #%s (%s): la pulsacion no se atiende",
            tool_call_id, fila["tool_name"],
        )
        await _sin_efecto("Ese enlace no vale", fila)
        raise Rechazada(403, "ese enlace no vale")
    if fila["expires_at"] and fila["expires_at"] <= datetime.now(timezone.utc):
        caduco = fila["expires_at"].astimezone(MADRID).strftime("%H:%M")
        await _sin_efecto(f"Esa acción caducó a las {caduco}: pídela otra vez si la sigues queriendo", fila)
        raise Rechazada(409, "esa acción ha caducado: pídela otra vez si la sigues queriendo")

    resuelta = await db.resolver_pendiente(
        tool_call_id, "approved" if accion == "aprobar" else "rejected", "usuario"
    )
    if resuelta is None:  # alguien la resolvio entre el SELECT y el UPDATE
        raise Rechazada(409, "esa acción acaba de resolverse por otro lado")
    log.info("aprobacion #%s: %s", tool_call_id, resuelta["status"])
    return resuelta


async def _ejecutar(fila: dict[str, Any]) -> str:
    """Ejecuta de verdad una llamada aprobada y cierra su fila. Devuelve el sobre."""
    from . import herramientas  # aqui: herramientas nos importa a nosotros

    sesiones = mcp_client.sesiones()
    try:
        catalogo = await herramientas.ofrecidas(sesiones)
        servidor = catalogo.ruta.get(fila["tool_name"])
        if servidor is None:
            raise RuntimeError(f"'{fila['tool_name']}' no la ofrece ahora ningún servidor MCP")
        fallo, texto = await sesiones[servidor].invocar(fila["tool_name"], fila["arguments"] or {})
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        await db.cerrar_llamada(fila["id"], status="failed", error=error)
        log.warning("la aprobacion #%s ha fallado al ejecutarse: %s", fila["id"], error)
        return herramientas.sobre(fila["tool_name"], "failed", error)
    finally:
        await mcp_client.cerrar(sesiones)

    resultado: Any
    try:
        resultado = json.loads(texto)
        texto = json.dumps(resultado, ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        resultado = texto
    if fallo:
        await db.cerrar_llamada(fila["id"], status="failed", error=texto)
        return herramientas.sobre(fila["tool_name"], "failed", texto)
    await db.cerrar_llamada(fila["id"], status="executed", result=herramientas.para_guardar(texto, resultado))
    return herramientas.sobre(fila["tool_name"], "executed", texto)


async def completar(fila: dict[str, Any]) -> None:
    """Lo que pasa despues del boton: ejecutar (o no), reanudar y avisar.

    Va en una tarea de fondo. Si el modelo tarda, el movil ya tiene su 200.
    """
    from . import bucle, herramientas  # bucle nos importa a nosotros

    if fila["status"] == "approved":
        sobre = await _ejecutar(fila)
    else:
        motivo = {
            "rejected": "El usuario ha rechazado esta acción. No se ha ejecutado nada.",
            "expired": "La petición ha caducado sin respuesta. No se ha ejecutado nada.",
        }[fila["status"]]
        sobre = herramientas.sobre(fila["tool_name"], fila["status"], motivo)

    # Lo que la herramienta quiera que salga en el aviso pase lo que pase con el
    # modelo: para lab_update_stack, la salud y los digests de antes.
    cerrada = await db.llamada(fila["id"])
    extra = ""
    if isinstance((cerrada or {}).get("result"), dict) and cerrada["result"].get("resumen_push"):
        extra = "\n\n" + cerrada["result"]["resumen_push"]

    respuesta = None
    try:
        async for evento, datos in bucle.conversar(
            fila["conversation_id"],
            model=fila["model"] or settings.smart_model,
            reanudacion=bucle.Reanudacion(
                tool_call_id=fila["id"],
                nombre=fila["tool_name"],
                argumentos=fila["arguments"] or {},
                sobre=sobre,
            ),
        ):
            if evento == "done":
                respuesta = datos["respuesta"]
            elif evento == "error":
                respuesta = f"El modelo ha fallado al retomar la conversación: {datos['message']}"
    except Exception as exc:
        log.exception("fallo al reanudar la conversacion de la aprobacion #%s", fila["id"])
        respuesta = f"No se ha podido retomar la conversación: {exc}"

    enlace = None
    if settings.puente_base_url:
        enlace = f"{settings.puente_base_url.rstrip('/')}/api/conversations/{fila['conversation_id']}"
    estado = {"approved": "Hecho", "rejected": "Rechazado", "expired": "Caducado"}[fila["status"]]
    await ntfy.publicar(
        settings.ntfy_topic_aprobaciones,
        f"{estado}: {fila['tool_name']}",
        (respuesta or "(sin respuesta del modelo)") + extra,
        enlace=enlace,
        etiquetas=["white_check_mark"] if fila["status"] == "approved" else ["x"],
    )


async def caducar() -> None:
    """Cada minuto: lo que nadie ha resuelto a tiempo se trata como un rechazo.

    Si no, la conversacion se queda bloqueada para siempre por una notificacion
    que nadie miro.
    """
    for fila in await db.caducadas():
        resuelta = await db.resolver_pendiente(fila["id"], "expired", "caducidad")
        if resuelta is None:
            continue
        log.warning("la aprobacion #%s ha caducado sin respuesta", fila["id"])
        await completar(resuelta)


async def purgar() -> None:
    """Cada dia: las llamadas viejas se quedan sin contenido, con su metadata."""
    n = await db.purgar_payloads(settings.retencion_dias)
    if n:
        log.info("audit log: vaciado el contenido de %s llamadas de mas de %s dias", n, settings.retencion_dias)
