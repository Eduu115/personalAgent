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

# Un prefijo de id mas corto casa con demasiadas cosas por accidente.
MIN_PREFIJO_ID = 6


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


def _nombres(c: dict[str, Any]) -> list[str]:
    return [n.lstrip("/") for n in (c.get("Names") or [])]


def _formatear(c: dict[str, Any]) -> dict[str, Any]:
    estado = c.get("Status", "")
    return {
        "nombre": (_nombres(c) or ["?"])[0],
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


async def _crudos(todos: bool = True) -> list[dict[str, Any]]:
    return (await _get("/containers/json", all=1 if todos else 0)).json()


async def listar_contenedores(todos: bool = True) -> list[dict[str, Any]]:
    salida = [_formatear(c) for c in await _crudos(todos)]
    return sorted(salida, key=lambda x: (x["proyecto"] or "~", x["nombre"]))


# Los unicos que se pueden reiniciar. La lista que manda es la de
# config/haproxy.cfg, que da 403 al resto: esta esta aqui para dar un error
# claro sin gastar la llamada. Ni postgres (se lleva los datos por delante) ni
# el socket-proxy (es quien concede esto) ni nada de APIArena, que es produccion.
REINICIABLES = (
    "puente-agent",
    "puente-homelab-mcp",
    "puente-google-mcp",
    "puente-ntfy",
    "puente-redis",
    "puente-litellm",
)


async def _post(ruta: str, **params: Any) -> httpx.Response:
    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as cli:
        try:
            r = await cli.post(ruta, params=params)
        except httpx.HTTPError as exc:
            raise ErrorDocker(f"no se pudo hablar con el socket-proxy: {exc}") from exc
    if r.status_code == 403:
        raise ErrorDocker(
            f"el socket-proxy no permite {ruta}. La lista de lo que se puede reiniciar "
            "esta en config/haproxy.cfg, con los nombres escritos uno a uno."
        )
    if r.status_code >= 400:
        raise ErrorDocker(f"docker devolvio {r.status_code} en {ruta}: {r.text[:200]}")
    return r


async def reiniciar(nombre: str) -> dict[str, Any]:
    """Para y arranca un contenedor del stack del puente."""
    c = await _resolver(nombre)
    if c["nombre"] not in REINICIABLES:
        raise ErrorDocker(
            f"'{c['nombre']}' no se puede reiniciar desde aqui. Solo: {', '.join(REINICIABLES)}"
        )
    await _post(f"/containers/{c['nombre']}/restart")
    return {
        "contenedor": c["nombre"],
        "estado": "reiniciando",
        "estaba": c.get("detalle"),
        "aviso": "Tarda unos segundos en volver; su healthcheck puede tardar un poco mas.",
    }


async def _resolver(nombre: str) -> dict[str, Any]:
    """Valida el argumento contra los contenedores que existen de verdad.

    Vale el nombre exacto o un prefijo del id de al menos MIN_PREFIJO_ID
    caracteres que case con uno solo. No hay allowlist que mantener, pero
    tampoco se acepta un identificador inventado, vacio o ambiguo.
    """
    if not nombre or not nombre.strip():
        raise ErrorDocker("hace falta el nombre exacto del contenedor o un prefijo de su id")

    crudos = await _crudos(todos=True)
    for c in crudos:
        if nombre in _nombres(c):
            return _formatear(c)

    conocidos = ", ".join(sorted(n for c in crudos for n in _nombres(c)[:1]))
    if len(nombre) < MIN_PREFIJO_ID:
        raise ErrorDocker(
            f"no hay ningun contenedor llamado '{nombre}', y como prefijo de id es "
            f"demasiado corto (minimo {MIN_PREFIJO_ID}). Hay estos: {conocidos}"
        )

    casan = [c for c in crudos if c.get("Id", "").startswith(nombre.lower())]
    if len(casan) == 1:
        return _formatear(casan[0])
    if len(casan) > 1:
        varios = ", ".join(f"{_formatear(c)['nombre']} ({c['Id'][:12]})" for c in casan)
        raise ErrorDocker(f"el prefijo '{nombre}' casa con varios contenedores: {varios}")
    raise ErrorDocker(f"no existe ningun contenedor '{nombre}'. Hay estos: {conocidos}")


def _es_multiplexado(bruto: bytes) -> bool:
    """Sin TTY, Docker entrama cada trozo con 8 bytes: [stream, 0, 0, 0, tamano x4].

    stream es 0 (stdin), 1 (stdout) o 2 (stderr). Con TTY la salida va en crudo
    y empieza por texto, que nunca tiene esa forma. Se mira la cabecera en vez
    de preguntar a /containers/{id}/json por Config.Tty, porque ese endpoint
    devuelve tambien Config.Env: las variables de entorno, secretos incluidos.
    """
    return len(bruto) >= 8 and bruto[0] in (0, 1, 2) and bruto[1:4] == b"\x00\x00\x00"


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

    r = await _get(
        f"/containers/{c['id']}/logs",
        stdout=1, stderr=1, tail=lineas, timestamps=1,
    )
    bruto = r.content
    texto = _demultiplexar(bruto) if _es_multiplexado(bruto) else bruto.decode("utf-8", "replace")

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
