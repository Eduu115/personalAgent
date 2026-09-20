"""Los dos botones del push: aprobar y rechazar.

Los abre el movil desde la notificacion, por el tailnet. La unica credencial es
el nonce de la fila, de un solo uso: al resolver deja de ser pending y esa URL
ya no vale. Se responde 200 enseguida; ejecutar y retomar la conversacion van en
una tarea de fondo, que el movil no tiene por que esperar al modelo.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import JSONResponse

from .. import aprobaciones

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/aprobaciones", tags=["aprobaciones"])


async def _resolver(tool_call_id: int, nonce: str, accion: str, tareas: BackgroundTasks):
    try:
        fila = await aprobaciones.resolver(tool_call_id, nonce, accion)
    except aprobaciones.Rechazada as exc:
        return JSONResponse({"estado": "no", "motivo": exc.motivo}, status_code=exc.codigo)
    tareas.add_task(aprobaciones.completar, fila)
    hecho = "aprobado, ejecutando" if accion == "aprobar" else "rechazado, no se ejecuta nada"
    return {"estado": hecho, "herramienta": fila["tool_name"], "id": fila["id"]}


@router.post("/{tool_call_id}/aprobar")
async def aprobar(tool_call_id: int, tareas: BackgroundTasks, n: str = Query(default="")):
    return await _resolver(tool_call_id, n, "aprobar", tareas)


@router.post("/{tool_call_id}/rechazar")
async def rechazar(tool_call_id: int, tareas: BackgroundTasks, n: str = Query(default="")):
    return await _resolver(tool_call_id, n, "rechazar", tareas)
