"""Cliente del helper del host. Es la unica forma de actualizar un stack.

El socket-proxy no puede hacer esto: `compose pull && up -d` son muchas llamadas
a la API de Docker (crear contenedores, borrar los viejos, tocar redes), y abrir
POST /containers/create y DELETE /containers/{id} en HAProxy tiraria la frontera
entera: con eso, un agente comprometido podria borrar y recrear
apiarena-postgres aunque su nombre no salga en ningun regex.

Asi que la operacion vive fuera, en un proceso del host (helper/puente_helper.py)
que solo sabe hacer una cosa y solo con las claves de su propia configuracion.
Aqui no hay ni rutas ni comandos: se le pasa un nombre y punto.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

SOCKET = os.environ.get("HELPER_SOCKET", "/run/puente/helper.sock")
# El helper se planta solo a los 10 min; aqui se espera un poco mas para poder
# contar su respuesta en vez de un timeout nuestro.
TOPE = 660


async def pedir(op: str, **extra: Any) -> dict[str, Any]:
    try:
        lector, escritor = await asyncio.wait_for(asyncio.open_unix_connection(SOCKET), 10)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"el helper del host no responde en {SOCKET} ({type(exc).__name__}). "
            "Sin el no se puede actualizar ningun stack: systemctl status puente-helper"
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
