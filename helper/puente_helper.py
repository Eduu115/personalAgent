#!/usr/bin/env python3
"""Helper del puente: actualiza un stack de compose. Corre en el HOST, como root.

Es el primer trozo de esto que vive fuera de los contenedores, y tiene acceso al
socket de Docker, que es equivalente a root. Por eso es pequeno, sin
dependencias y se lee de una sentada. Si crece, algo se ha hecho mal.

Habla JSON por lineas en un socket unix. No hay puerto ni token: el permiso es
el dueno y el modo del socket. Una sola operacion util, `actualizar`, y el stack
es una CLAVE de /etc/puente/stacks.conf (root, 600, fuera del repo). Nunca una
ruta, ni un comando, ni un fichero compose.
"""

import json
import logging
import os
import socketserver
import subprocess
import time

# Estos no se actualizan desde aqui aunque alguien los meta en stacks.conf:
# apiarena es el TFG en produccion, y puente se mataria a si mismo a mitad de la
# operacion dejando la aprobacion colgada. La lista vive en el codigo a proposito.
EXCLUIDOS = {"apiarena", "puente"}

CONFIG = os.environ.get("PUENTE_STACKS", "/etc/puente/stacks.conf")
SOCKET = os.environ.get("PUENTE_SOCKET", "/run/puente/helper.sock")
GRUPO = int(os.environ.get("PUENTE_GRUPO_GID", "0"))
ESPERA_SALUD = 90       # s esperando a que los healthchecks pasen a healthy
TOPE_PULL = 480         # s: un pull lento no puede colgar esto para siempre
TOPE_UP = 300
TOPE_TOTAL = 600

log = logging.getLogger("puente-helper")


def stacks() -> dict[str, str]:
    """nombre=/ruta por linea. Se relee en cada peticion: sin estado que refrescar."""
    mapa = {}
    with open(CONFIG) as f:
        for linea in f:
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                nombre, ruta = linea.split("=", 1)
                mapa[nombre.strip()] = ruta.strip()
    return mapa


def compose(ruta: str, *args: str, tope: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "--project-directory", ruta, *args],
        capture_output=True, text=True, timeout=tope,
    )


def _filas(ruta: str) -> list[dict]:
    salida = compose(ruta, "ps", "--all", "--format", "json", tope=30).stdout.strip()
    if not salida:
        return []
    try:  # segun la version, compose devuelve una lista o una linea por contenedor
        datos = json.loads(salida)
        return datos if isinstance(datos, list) else [datos]
    except json.JSONDecodeError:
        return [json.loads(l) for l in salida.splitlines() if l.strip()]


def digests(ruta: str) -> dict[str, str]:
    """Lo que hay AHORA, para poder volver atras sin investigar nada."""
    actuales = {}
    for fila in _filas(ruta):
        imagen = fila.get("Image", "")
        if not imagen:
            continue
        r = subprocess.run(
            ["docker", "image", "inspect", "--format",
             "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}", imagen],
            capture_output=True, text=True, timeout=30,
        )
        actuales[fila.get("Service") or imagen] = r.stdout.strip() or imagen
    return actuales


def salud(ruta: str) -> dict[str, str]:
    """Contenedor -> healthy | unhealthy | starting | running | exited..."""
    return {f.get("Name", "?"): (f.get("Health") or f.get("State") or "?") for f in _filas(ruta)}


def _sanos(estado: dict[str, str]) -> bool:
    return all(v in ("healthy", "running") for v in estado.values()) and bool(estado)


def actualizar(stack: str) -> dict:
    if stack in EXCLUIDOS:
        log.warning("rechazado: '%s' esta excluido en el codigo", stack)
        return {"ok": False, "error": f"'{stack}' no se actualiza desde aqui nunca"}
    mapa = stacks()
    ruta = mapa.get(stack)
    if ruta is None:
        log.warning("rechazado: '%s' no esta en %s", stack, CONFIG)
        return {"ok": False, "error": f"'{stack}' no esta configurado. Hay: {', '.join(sorted(mapa)) or 'ninguno'}"}
    if os.path.basename(os.path.realpath(ruta)) in EXCLUIDOS:
        log.warning("rechazado: '%s' apunta a un directorio excluido", stack)
        return {"ok": False, "error": f"'{stack}' apunta a un stack excluido"}

    empezo = time.monotonic()
    anteriores = digests(ruta)
    log.info("actualizando '%s' (%s)", stack, ruta)
    for orden, tope in (("pull", TOPE_PULL), ("up", TOPE_UP)):
        args = ("pull",) if orden == "pull" else ("up", "-d", "--remove-orphans")
        r = compose(ruta, *args, tope=tope)
        if r.returncode != 0:
            log.error("'%s' fallo en %s: %s", stack, orden, r.stderr[-300:])
            return {"ok": False, "error": f"compose {orden} fallo: {r.stderr[-300:].strip()}",
                    "digests_anteriores": anteriores, "estado": salud(ruta)}

    estado = salud(ruta)
    while not _sanos(estado) and time.monotonic() - empezo < ESPERA_SALUD:
        time.sleep(3)
        estado = salud(ruta)

    ok = _sanos(estado)
    log.info("'%s' %s en %.0f s: %s", stack, "listo" if ok else "con problemas", time.monotonic() - empezo, estado)
    return {
        "ok": ok,
        "stack": stack,
        "estado": estado,
        "digests_anteriores": anteriores,
        "segundos": round(time.monotonic() - empezo),
        "aviso": None if ok else f"Hay contenedores que no estan sanos tras {ESPERA_SALUD} s.",
    }


class Handler(socketserver.StreamRequestHandler):
    timeout = TOPE_TOTAL

    def handle(self) -> None:
        try:
            peticion = json.loads(self.rfile.readline() or b"{}")
            op = peticion.get("op")
            if op == "ping":
                respuesta = {"ok": True, "stacks": sorted(set(stacks()) - EXCLUIDOS)}
            elif op == "actualizar":
                respuesta = actualizar(str(peticion.get("stack", "")))
            else:
                respuesta = {"ok": False, "error": f"operacion desconocida: {op!r}"}
        except Exception as exc:  # que un fallo no tumbe el helper
            log.exception("peticion fallida")
            respuesta = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.wfile.write(json.dumps(respuesta, ensure_ascii=False).encode() + b"\n")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    os.makedirs(os.path.dirname(SOCKET), exist_ok=True)
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    # Una peticion cada vez: dos compose a la vez sobre el mismo stack no.
    servidor = socketserver.UnixStreamServer(SOCKET, Handler)
    if os.geteuid() == 0:  # en produccion corre como root; en pruebas, no se puede
        os.chown(SOCKET, 0, GRUPO)
    os.chmod(SOCKET, 0o660)  # el permiso ES esto: dueno root, grupo el del MCP
    log.info("escuchando en %s (grupo %s), stacks en %s", SOCKET, GRUPO, CONFIG)
    servidor.serve_forever()


if __name__ == "__main__":
    main()
