"""Las acciones rapidas de la consola: el humano pulsa y pasa.

NO entran en la cola de aprobaciones, y eso rompe el patron a proposito. La cola
existe porque el MODELO propone y el humano decide; un boton en la consola ES el
humano decidiendo, y pedir una segunda confirmacion en el movil de algo que se
acaba de pulsar con el dedo es teatro.

Lo que si mantienen, porque eso no depende de quien lo pida:

  - quedan en `tool_calls` como cualquier otra llamada, con `origin='consola'`
    para poder separarlas de las que pidio el modelo;
  - `READ_ONLY` las apaga;
  - la lista es fija y cerrada: lo que no este en el catalogo no existe.

Y lo que NO hay detras: ninguna herramienta MCP. **El modelo no puede encender
el PC, ni sabe que se puede.** Por eso `despertar` vive en el helper del host y
se le pide desde aqui por su socket, sin pasar por homelab-mcp: si fuera una
herramienta, estaria en el catalogo que ve el modelo y bastaria con que un
correo le convenciera. `pruebas.wol_no_es_herramienta()` lo comprueba.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from . import db
from .config import settings

log = logging.getLogger(__name__)

# El mismo socket que usa homelab-mcp para actualizar stacks. El permiso es el
# grupo del socket, y este contenedor esta en el (group_add en el compose).
SOCKET = os.environ.get("HELPER_SOCKET", "/run/puente/helper.sock")
# Mandar tres paquetes UDP es instantaneo: si no contesta en esto, no contesta.
TOPE = 15.0


@dataclass(frozen=True)
class Accion:
    id: str
    etiqueta: str
    detalle: str
    riesgo: str
    correr: Callable[[], Awaitable[dict[str, Any]]]


async def _al_helper(op: str, **extra: Any) -> dict[str, Any]:
    """Una peticion JSON por linea al helper del host. Sin reintentos."""
    try:
        lector, escritor = await asyncio.wait_for(asyncio.open_unix_connection(SOCKET), 5)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"el helper del host no responde en {SOCKET} ({type(exc).__name__}): "
            "systemctl status puente-helper"
        ) from None
    try:
        escritor.write(json.dumps({"op": op, **extra}).encode() + b"\n")
        await escritor.drain()
        linea = await asyncio.wait_for(lector.readline(), TOPE)
    finally:
        escritor.close()
    if not linea:
        raise RuntimeError("el helper del host ha cortado sin contestar")
    return json.loads(linea)


async def _despertar(equipo: str) -> dict[str, Any]:
    """El nombre viaja; la MAC vive en /etc/puente/equipos.conf y no sale de ahi."""
    return await _al_helper("despertar", equipo=equipo)


def catalogo() -> dict[str, Accion]:
    """La lista fija de acciones. Hoy solo encender equipos.

    Sale de EQUIPOS_DESPERTABLES: sin esa variable no hay ninguna accion y la
    vista de la consola lo dice. Quien manda de verdad es el fichero del host,
    que este contenedor no ve.
    """
    return {
        f"despertar:{equipo}": Accion(
            id=f"despertar:{equipo}",
            etiqueta=f"Encender {equipo}",
            detalle=(
                "Manda un paquete mágico a la LAN. Wake-on-LAN no tiene acuse de "
                "recibo: si estaba apagado, tarda un rato en arrancar."
            ),
            riesgo="write",
            correr=partial(_despertar, equipo),
        )
        for equipo in settings.equipos
    }


async def ejecutar(accion_id: str) -> dict[str, Any]:
    """Ejecuta una accion del catalogo al momento. KeyError si no esta en el.

    Al momento: sin cola y sin nonce. Lo que no se salta es el audit log ni el
    kill switch.
    """
    accion = catalogo()[accion_id]

    if settings.read_only:
        log.warning("accion '%s' rechazada: READ_ONLY", accion.id)
        await db.log_tool_call(
            accion.id, risk=accion.riesgo, status="rejected",
            error="READ_ONLY esta activo", origin="consola",
        )
        return {"ok": False, "error": "El agente está en modo solo lectura: no ejecuta nada con efectos."}

    try:
        salida = await accion.correr()
    except Exception as exc:
        motivo = str(exc) or type(exc).__name__
        log.error("accion '%s' ha fallado: %s", accion.id, motivo)
        await db.log_tool_call(
            accion.id, risk=accion.riesgo, status="failed", error=motivo[:500], origin="consola",
        )
        return {"ok": False, "error": motivo}

    await db.log_tool_call(
        accion.id,
        risk=accion.riesgo,
        status="executed" if salida.get("ok") else "failed",
        result=salida,
        error=None if salida.get("ok") else str(salida.get("error"))[:500],
        origin="consola",
    )
    log.info("accion '%s': ok=%s", accion.id, salida.get("ok"))
    return salida
