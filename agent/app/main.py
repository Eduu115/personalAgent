from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import db
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
    try:
        yield
    finally:
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
    """Readiness: ademas, la base de datos contesta."""
    try:
        ok = await db.ping()
    except Exception as exc:
        return JSONResponse({"status": "error", "db": str(exc)}, status_code=503)
    if not ok:
        return JSONResponse({"status": "error", "db": "sin respuesta"}, status_code=503)
    return {"status": "ok", "db": "ok", "read_only": settings.read_only}
