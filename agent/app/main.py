from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import briefing, db, herramientas, mcp_client
from .config import settings
from .routes.chat import router as chat_router

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
log = logging.getLogger("puente")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    log.info(
        "puente arrancado (modelo=%s, solo_lectura=%s)",
        settings.smart_model,
        settings.read_only,
    )
    # En memoria: un solo proceso, y si el agente esta caido a las 7:30 ese
    # briefing no se recupera al volver. Un cron mal escrito no arranca el agente.
    planificador = AsyncIOScheduler(timezone=briefing.MADRID)
    if settings.briefing_cron:
        planificador.add_job(
            briefing.lanzar,
            CronTrigger.from_crontab(settings.briefing_cron, timezone=briefing.MADRID),
            id="briefing",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
    planificador.start()
    if settings.briefing_cron:
        log.info(
            "briefing programado: '%s' en hora de Madrid, el siguiente %s",
            settings.briefing_cron,
            planificador.get_job("briefing").next_run_time,
        )
    else:
        log.info("briefing apagado: BRIEFING_CRON esta vacio")
    try:
        yield
    finally:
        planificador.shutdown(wait=False)
        await db.close_pool()


app = FastAPI(
    title="Puente de mando",
    description="Asistente personal. Solo accesible por el tailnet.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.include_router(chat_router)


@app.get("/healthz")
async def healthz():
    """Liveness: el proceso responde."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Readiness: la base de datos contesta y ninguna herramienta esta en conflicto.

    Un servidor MCP caido no es un 503: el agente sigue sin sus herramientas, que
    es lo previsto, y aqui se ve en sin_respuesta. Dos servidores anunciando el
    mismo nombre si lo es: es un error de despliegue y el ERROR del log no lo
    lee nadie. deploy.sh pega aqui y falla con el.
    """
    try:
        ok = await db.ping()
    except Exception as exc:
        return JSONResponse({"status": "error", "db": str(exc)}, status_code=503)
    if not ok:
        return JSONResponse({"status": "error", "db": "sin respuesta"}, status_code=503)

    sesiones = mcp_client.sesiones()
    try:
        catalogo = await herramientas.ofrecidas(sesiones)
    finally:
        await mcp_client.cerrar(sesiones)
    cuerpo = {
        "status": "error" if catalogo.conflictos else "ok",
        "db": "ok",
        "read_only": settings.read_only,
        "mcp": {
            "herramientas": len(catalogo.tools),
            "conflictos": catalogo.conflictos,
            "sin_respuesta": catalogo.sin_respuesta,
        },
    }
    return JSONResponse(cuerpo, status_code=503 if catalogo.conflictos else 200)


@app.post("/api/briefing")
async def briefing_ahora():
    """Lanza el briefing ya, sin esperar al cron, y espera a que termine.

    Es el mismo que el programado: origin="schedule", conversacion nueva y
    aviso por ntfy.
    """
    return await briefing.lanzar()
