"""Lo que la consola necesita y no existia: la cola y el estado del homelab.

La puerta sigue siendo la identidad del tailnet, la misma que la de /api/chat:
aqui no hay ningun mecanismo de sesion nuevo. El nonce viaja a la pagina porque
es lo que autoriza el boton, igual que viaja en la URL del push del movil.

Nada de aqui llama al modelo, y eso es la regla, no una casualidad: son
pantallas que se refrescan solas en una tablet encendida todo el dia. Pintar
unos tiles con lo que devuelve un `docker ps` no vale tokens, y la vista de
Briefing lee la tabla `briefings` en vez de generar uno. El unico sitio que
genera es POST /api/briefing, y ese lo pulsa una persona.

Casi todo esto es de lectura. La excepcion es POST /api/acciones/{id}, que
ejecuta al momento y sin cola: el porque esta en `acciones.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import acciones, aprobaciones, db, herramientas, mcp_client
from ..config import VERSION, settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["consola"])

# La tablet refresca cada 15 s; con varias pestanas abiertas, esto evita
# repetir la misma consulta al MCP por cada una.
_TTL_ESTADO = 5.0
# La agenda sale de un feed iCal que hay que bajar de Google. Los eventos de hoy
# no cambian cada 30 s, y en ambient la tablet la pide todo el dia: 5 min.
_TTL_AGENDA = 300.0
_cache: dict[str, tuple[float, Any]] = {}


async def _cacheado(clave: str, ttl: float, calcular) -> Any:
    """Lo de la ultima vez si es reciente. Un fallo no se cachea: se reintenta."""
    guardado = _cache.get(clave)
    if guardado and time.monotonic() - guardado[0] < ttl:
        return guardado[1]
    datos = await calcular()
    _cache[clave] = (time.monotonic(), datos)
    return datos


@router.get("/aprobaciones")
async def pendientes() -> dict[str, Any]:
    """Lo que espera un OK ahora mismo, para pintarlo y poder resolverlo."""
    filas = await db.pendientes()
    return {
        "pendientes": [
            {
                "id": f["id"],
                "herramienta": f["tool_name"],
                # Para que la consola pueda sondear la conversacion y ensenar
                # como acabo: si no, el unico canal seria el push al movil.
                "conversation_id": str(f["conversation_id"]) if f["conversation_id"] else None,
                "riesgo": f["risk"],
                "argumentos": f["arguments"],
                "que_pasa": aprobaciones.que_pasa(f),
                "caduca_en": f["expires_at"].isoformat(),
                "quedan_seg": max(0, round(f["quedan_seg"] or 0)),
                "nonce": f["nonce"],
            }
            for f in filas
        ]
    }


async def _estado() -> dict[str, Any]:
    contenedores, anfitrion = await herramientas.solo_lectura("lab_status", "lab_host")
    return {
        "docker": contenedores.get("docker", {}),
        "contenedores": contenedores.get("contenedores", []),
        "host": anfitrion,
        "ts": time.time(),
    }


@router.get("/estado")
async def estado():
    """Contenedores y anfitrion, directo del MCP. Sin modelo por medio.

    Lleva tambien la version desplegada: la consola ya sondea esto cada 15 s, y
    con eso se entera de que hay codigo nuevo sin un endpoint mas. Va fuera de
    la cache y tambien en el error, que un MCP caido no puede dejar a la tablet
    con la version de hace tres semanas.
    """
    try:
        datos = await _cacheado("estado", _TTL_ESTADO, _estado)
    except Exception as exc:
        log.warning("no se pudo leer el estado para la consola: %s", exc)
        return JSONResponse(
            {"error": str(exc) or type(exc).__name__, "version": VERSION}, status_code=503
        )
    return datos | {"version": VERSION}


async def _agenda() -> dict[str, Any]:
    (datos,) = await herramientas.solo_lectura("cal_agenda")   # dias=1 por defecto: hoy
    return datos


@router.get("/agenda")
async def agenda():
    """Los eventos de hoy, por el mismo camino que /api/estado. Sin modelo."""
    try:
        return await _cacheado("agenda", _TTL_AGENDA, _agenda)
    except Exception as exc:
        log.warning("no se pudo leer la agenda para la consola: %s", exc)
        return JSONResponse({"error": str(exc) or type(exc).__name__}, status_code=503)


@router.get("/briefings")
async def briefings(limite: int = 5):
    """Los ultimos briefings GUARDADOS. Solo lee la tabla: no genera ninguno.

    Generar uno cuesta una llamada al modelo con varias rondas de herramientas.
    Esta vista se refresca sola, asi que aqui no se genera nada; para eso esta
    el boton, que llama a POST /api/briefing y lo pulsa una persona.
    """
    filas = await db.ultimos_briefings(max(1, min(limite, 20)))
    return {
        "zona": settings.zona_horaria,
        "briefings": [
            {
                "id": f["id"],
                "creado_en": f["creado_en"].isoformat(),
                "resumen": f["resumen"],
                "publicado": f["publicado"],
            }
            for f in reversed(filas)   # la consulta los da del mas viejo al mas nuevo
        ],
    }


# ------------------------------------------------------------------ acciones
#
# Lo unico de este fichero que hace algo en vez de mirarlo. No pasa por la cola
# de aprobaciones porque el boton ES la aprobacion; el porque largo, en
# acciones.py. Se audita y READ_ONLY lo apaga, eso no se negocia.


@router.get("/acciones")
async def acciones_disponibles() -> dict[str, Any]:
    """Lo que los botones de la consola pueden hacer. Fijo y corto."""
    return {
        "read_only": settings.read_only,
        "acciones": [
            {"id": a.id, "etiqueta": a.etiqueta, "detalle": a.detalle, "riesgo": a.riesgo}
            for a in acciones.catalogo().values()
        ],
    }


@router.post("/acciones/{accion_id}")
async def hacer_accion(accion_id: str):
    """Ejecuta una accion del catalogo. Al pulsar, sin segunda confirmacion."""
    try:
        return await acciones.ejecutar(accion_id)
    except KeyError:
        # Lo que no este en el catalogo no existe, y no se audita: la fila
        # llevaria como nombre lo que venga en la URL.
        log.warning("accion desconocida desde la consola: %r", accion_id[:80])
        return JSONResponse({"ok": False, "error": "esa acción no existe"}, status_code=404)


# ------------------------------------------------------------------ vigilancia


@router.get("/vigilancia")
async def vigilancia_estado() -> dict[str, Any]:
    """Lo que vigila el agente y como esta, tal cual esta guardado.

    Es lo que le da a la consola la misma verdad que a los push: la linea de
    arriba dice lo que la vigilancia habria avisado, asi que un silencio en la
    pantalla y un silencio en el movil significan lo mismo. Solo LEE la tabla:
    no comprueba nada (para eso esta POST /api/vigilancia) y no llama al modelo.
    """
    filas = await db.vigilancias_ordenadas()
    return {
        "mal": [
            {"nombre": f["nombre"], "detalle": f["detalle"], "desde": f["desde"].isoformat()}
            for f in filas if f["estado"] == "mal"
        ],
        "vigiladas": len(filas),
        "ultima": max((f["visto_en"] for f in filas), default=None),
    }
