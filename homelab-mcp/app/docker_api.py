"""Cliente del API de Docker, siempre a traves del socket-proxy.

Nunca se monta /var/run/docker.sock en este contenedor. El socket lo tiene el
proxy, que ademas esta configurado con POST=0: aunque alguien se colara aqui,
por este camino no se puede arrancar, parar ni borrar nada.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .redact import redactar

log = logging.getLogger(__name__)

BASE = os.environ.get("DOCKER_API", "http://docker-socket-proxy:2375")
TIMEOUT = httpx.Timeout(10.0, read=20.0)

MAX_LINEAS = 500


class ErrorDocker(RuntimeError):
    pass


async def _get(ruta: str, **params: Any) -> httpx.Response:
    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as cli:
        try:
            r = await cli.get(ruta, params=params)
        except httpx.HTTPError as exc:
            raise ErrorDocker(f"no se pudo hablar con el socket-proxy: {exc}") from exc
    if r.status_code == 403:
        raise ErrorDocker(
            f"el socket-proxy no permite {ruta}. Esta bien: solo se conceden "
            "endpoints de lectura. Si hace falta uno nuevo, se anade explicitamente."
        )
    if r.status_code >= 400:
        raise ErrorDocker(f"docker devolvio {r.status_code} en {ruta}: {r.text[:200]}")
    return r


def _mib(valor: int | float | None) -> float | None:
    return round(valor / 1048576, 1) if valor else None


async def listar_contenedores(todos: bool = True) -> list[dict[str, Any]]:
    r = await _get("/containers/json", all=1 if todos else 0)
    salida = []
    for c in r.json():
        nombre = (c.get("Names") or ["/?"])[0].lstrip("/")
        estado = c.get("Status", "")
        salida.append(
            {
                "nombre": nombre,
                "id": c.get("Id", "")[:12],
                "imagen": c.get("Image"),
                "estado": c.get("State"),
                "detalle": estado,
                # Docker mete la salud dentro de Status: "Up 2 hours (healthy)"
                "salud": (
                    "healthy" if "(healthy)" in estado
                    else "unhealthy" if "(unhealthy)" in estado
                    else "starting" if "(health: starting)" in estado
                    else None
                ),
                "proyecto": (c.get("Labels") or {}).get("com.docker.compose.project"),
            }
        )
    return sorted(salida, key=lambda x: (x["proyecto"] or "~", x["nombre"]))


async def _resolver(nombre: str) -> dict[str, Any]:
    """Valida el nombre contra los contenedores que existen de verdad.

    Es la validacion que evita que el argumento sea cualquier cosa: no hay
    allowlist que mantener, pero tampoco se acepta un identificador inventado.
    """
    for c in await listar_contenedores(todos=True):
        if c["nombre"] == nombre or c["id"].startswith(nombre):
            return c
    conocidos = ", ".join(c["nombre"] for c in await listar_contenedores())
    raise ErrorDocker(f"no existe ningun contenedor '{nombre}'. Hay estos: {conocidos}")


def _demultiplexar(bruto: bytes) -> str:
    """Los logs sin TTY vienen en tramas de 8 bytes de cabecera + payload."""
    trozos: list[str] = []
    i = 0
    while i + 8 <= len(bruto):
        largo = int.from_bytes(bruto[i + 4 : i + 8], "big")
        i += 8
        trozos.append(bruto[i : i + largo].decode("utf-8", "replace"))
        i += largo
    return "".join(trozos)


async def logs(nombre: str, lineas: int = 100) -> dict[str, Any]:
    c = await _resolver(nombre)
    lineas = max(1, min(int(lineas), MAX_LINEAS))

    insp = (await _get(f"/containers/{c['id']}/json")).json()
    tiene_tty = bool(insp.get("Config", {}).get("Tty"))

    r = await _get(
        f"/containers/{c['id']}/logs",
        stdout=1, stderr=1, tail=lineas, timestamps=1,
    )
    bruto = r.content
    texto = bruto.decode("utf-8", "replace") if tiene_tty else _demultiplexar(bruto)

    texto, redactados = redactar(texto)
    if redactados:
        log.info("redactados %d posibles secretos en los logs de %s", redactados, nombre)

    return {
        "contenedor": c["nombre"],
        "lineas_pedidas": lineas,
        "secretos_redactados": redactados,
        "logs": texto,
    }


async def stats(nombre: str) -> dict[str, Any]:
    c = await _resolver(nombre)
    s = (await _get(f"/containers/{c['id']}/stats", stream="false", **{"one-shot": "true"})).json()
    mem = s.get("memory_stats") or {}
    uso = mem.get("usage")
    # docker stats descuenta el cache de fichero inactivo; sin esto el numero
    # sale inflado y no cuadra con lo que ve el usuario en la terminal.
    inactivo = (mem.get("stats") or {}).get("inactive_file", 0)
    limite = mem.get("limit")
    real = (uso - inactivo) if uso is not None else None
    return {
        "contenedor": c["nombre"],
        "memoria_mib": _mib(real),
        "limite_mib": _mib(limite),
        "porcentaje": round(real / limite * 100, 1) if real and limite else None,
    }


async def info() -> dict[str, Any]:
    d = (await _get("/info")).json()
    return {
        "contenedores": d.get("Containers"),
        "en_marcha": d.get("ContainersRunning"),
        "parados": d.get("ContainersStopped"),
        "imagenes": d.get("Images"),
        "version_docker": d.get("ServerVersion"),
        "cpus": d.get("NCPU"),
        "memoria_total_mib": _mib(d.get("MemTotal")),
    }
