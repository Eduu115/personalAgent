"""Comprobaciones del agente que no necesitan ni red ni base de datos.

    docker compose run --rm --no-deps agent python -m app.pruebas

Las corre deploy.sh dentro de la imagen recien construida, ANTES de aplicar
migraciones: si el codigo nuevo no arranca, la base no se toca.

Estan por algo concreto: la F2 se llevo por delante briefing.por_cron en un
refactor, main.py seguia llamandola y el agente entro en bucle de reinicio en el
server. En el portatil no salto porque el override de desarrollo apaga el
briefing y esa rama del lifespan no se ejecutaba nunca.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import pkgutil
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import main
from .config import settings

AQUI = Path(__file__).parent
PAQUETE = __package__ or "app"


def referencias() -> None:
    """Ningun `modulo.atributo` del paquete apunta a algo que ya no existe.

    Es lo que fallo: una funcion borrada en un refactor y una llamada olvidada.
    Un AttributeError asi solo aparece cuando se ejecuta esa linea, que puede
    ser a las 7:30 de la manana en el server.
    """
    modulos = {m.name for m in pkgutil.iter_modules([str(AQUI)])}
    colgando = []
    for fichero in sorted(AQUI.rglob("*.py")):
        arbol = ast.parse(fichero.read_text(), str(fichero))
        # Nombres que en este fichero son modulos nuestros: from . import x, y
        locales = {
            alias.asname or alias.name
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.ImportFrom) and nodo.level and nodo.module is None
            for alias in nodo.names
            if alias.name in modulos
        }
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Attribute)
                and isinstance(nodo.value, ast.Name)
                and nodo.value.id in locales
                and not hasattr(importlib.import_module(f".{nodo.value.id}", PAQUETE), nodo.attr)
            ):
                colgando.append(f"{fichero.relative_to(AQUI)}:{nodo.lineno} -> {nodo.value.id}.{nodo.attr}")
    assert not colgando, "referencias que ya no existen:\n  " + "\n  ".join(colgando)
    print(f"OK referencias: {len(modulos)} modulos, ningun atributo colgando")


async def arranque() -> None:
    """El lifespan entero, con los jobs del planificador registrados.

    Con BRIEFING_CRON puesto, que es la rama que se rompio: en desarrollo va
    vacia y no se ejecuta nunca.
    """
    from . import briefing, db

    registrados: list[tuple[str | None, object]] = []

    class Espia(AsyncIOScheduler):
        """Apunta los jobs y arranca en pausa: se programan pero no se ejecutan."""

        def add_job(self, func, *args, **kwargs):  # type: ignore[override]
            registrados.append((kwargs.get("id"), func))
            return super().add_job(func, *args, **kwargs)

        def start(self, *args, **kwargs):  # type: ignore[override]
            return super().start(paused=True)

    async def nada(*_args, **_kwargs):
        return None

    original = (main.AsyncIOScheduler, db.open_pool, db.close_pool, db.hay_briefing_desde, briefing.lanzar)
    main.AsyncIOScheduler = Espia
    db.open_pool = nada
    db.close_pool = nada
    briefing.lanzar = nada  # por si toca recuperar uno: aqui no se llama al modelo
    try:
        # El cron de la recuperacion tiene que tener un disparo dentro de la
        # ventana de 2 h, o no hay nada que recuperar y la prueba dependeria de
        # la hora a la que se ejecute.
        for cron, hecho_ya, esperados in (
            ("30 7 * * *", True, {"briefing", "caducar-aprobaciones", "purgar-audit"}),
            # Sin briefing reciente: ademas se programa el que se perdio.
            ("*/5 * * * *", False, {"briefing", "caducar-aprobaciones", "purgar-audit", "briefing-recuperado"}),
        ):
            registrados.clear()
            settings.briefing_cron = cron
            db.hay_briefing_desde = lambda *_a, **_k: asyncio.sleep(0, result=hecho_ya)
            async with main.lifespan(main.app):
                pass
            ids = {i for i, _ in registrados}
            assert ids == esperados, f"jobs registrados {ids}, esperados {esperados}"
            for nombre, funcion in registrados:
                assert callable(funcion), f"el job {nombre} no es llamable"
            print(f"OK lifespan (cron '{cron}', briefing hecho={hecho_ya}): {', '.join(sorted(ids))}")

        # Y sin briefing, que es como corre el portatil.
        registrados.clear()
        settings.briefing_cron = ""
        async with main.lifespan(main.app):
            pass
        assert {i for i, _ in registrados} == {"caducar-aprobaciones", "purgar-audit"}
        print("OK lifespan sin briefing: caducar-aprobaciones, purgar-audit")
    finally:
        (main.AsyncIOScheduler, db.open_pool, db.close_pool, db.hay_briefing_desde, briefing.lanzar) = original


def identidad() -> None:
    """El prompt lleva quien es el dueno, y sale de configuracion.

    Con valores inventados: si algo de esto estuviera escrito en el codigo, no
    cambiaria al cambiar la configuracion y la prueba lo veria.
    """
    original = (settings.dueno, settings.gmail_usuario, settings.zona_horaria)
    try:
        settings.dueno, settings.gmail_usuario, settings.zona_horaria = "Prueba", "p@ejemplo.org", "America/Lima"
        prompt = settings.prompt
        for dato in ("Prueba", "p@ejemplo.org", "America/Lima"):
            assert dato in prompt, f"el prompt no lleva {dato}"
        assert "instrucciones" in prompt and "herramienta" in prompt, "falta decir que es identidad, no datos"
        # Sin correo configurado no se lo inventa: dice que lo pregunte.
        settings.gmail_usuario = ""
        assert "pregúntasela" in settings.prompt and "p@ejemplo.org" not in settings.prompt
    finally:
        (settings.dueno, settings.gmail_usuario, settings.zona_horaria) = original
    print(f"OK identidad en el prompt: {settings.dueno}, {settings.gmail_usuario or '(sin correo)'}, {settings.zona_horaria}")


def rutas() -> None:
    """Las rutas que abren los botones del push siguen existiendo."""
    caminos = {r.path for r in main.app.routes}
    for ruta in (
        "/api/chat",
        "/api/briefing",
        "/api/aprobaciones/{tool_call_id}/aprobar",
        "/api/aprobaciones/{tool_call_id}/rechazar",
        "/healthz",
        "/readyz",
    ):
        assert ruta in caminos, f"falta la ruta {ruta}"
    print(f"OK rutas: {len(caminos)} registradas")


if __name__ == "__main__":
    referencias()
    identidad()
    rutas()
    asyncio.run(arranque())
    print("todo OK")
