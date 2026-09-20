"""Lo que la consola necesita y no existia: la cola y el estado del homelab.

La puerta sigue siendo la identidad del tailnet, la misma que la de /api/chat:
aqui no hay ningun mecanismo de sesion nuevo. El nonce viaja a la pagina porque
es lo que autoriza el boton, igual que viaja en la URL del push del movil.

/api/estado no pasa por el modelo a proposito: pintar unos tiles con lo que
devuelve un `docker ps` no vale tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import aprobaciones, db, herramientas, mcp_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["consola"])

# La tablet refresca cada 15 s; con varias pestanas abiertas, esto evita
# repetir la misma consulta al MCP por cada una.
_TTL_ESTADO = 5.0
_cache: dict[str, Any] = {"ts": 0.0, "datos": None}


@router.get("/aprobaciones")
async def pendientes() -> dict[str, Any]:
    """Lo que espera un OK ahora mismo, para pintarlo y poder resolverlo."""
    filas = await db.pendientes()
    return {
        "pendientes": [
            {
                "id": f["id"],
                "herramienta": f["tool_name"],
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


async def _leer(sesiones: dict[str, Any], ruta: dict[str, str], nombre: str) -> Any:
    """Llama a una herramienta saltandose ejecutar(). Solo de lectura.

    Este atajo existe para no pagar tokens por un `docker ps`, pero se salta el
    mapa de riesgo, el audit log, el kill switch y la comprobacion de origin.
    Hoy las dos que usa son de lectura; el dia que alguien enchufe aqui una de
    escritura, que reviente en vez de colarse por la puerta de atras.
    """
    if herramientas.RIESGO.get(nombre) != "read":
        raise RuntimeError(
            f"'{nombre}' no es de lectura: la consola no puede llamarla por el atajo de /api/estado"
        )
    servidor = ruta.get(nombre)
    if servidor is None:
        raise RuntimeError(f"'{nombre}' no la ofrece ahora ningun servidor MCP")
    fallo, texto = await sesiones[servidor].invocar(nombre, {})
    if fallo:
        raise RuntimeError(texto[:200])
    return json.loads(texto)


@router.get("/estado")
async def estado():
    """Contenedores y anfitrion, directo del MCP. Sin modelo por medio."""
    if _cache["datos"] is not None and time.monotonic() - _cache["ts"] < _TTL_ESTADO:
        return _cache["datos"]

    sesiones = mcp_client.sesiones()
    try:
        catalogo = await herramientas.ofrecidas(sesiones)
        contenedores, anfitrion = await asyncio.gather(
            _leer(sesiones, catalogo.ruta, "lab_status"),
            _leer(sesiones, catalogo.ruta, "lab_host"),
        )
    except Exception as exc:
        log.warning("no se pudo leer el estado para la consola: %s", exc)
        return JSONResponse({"error": str(exc) or type(exc).__name__}, status_code=503)
    finally:
        await mcp_client.cerrar(sesiones)

    datos = {
        "docker": contenedores.get("docker", {}),
        "contenedores": contenedores.get("contenedores", []),
        "host": anfitrion,
        "ts": time.time(),
    }
    _cache.update(ts=time.monotonic(), datos=datos)
    return datos
