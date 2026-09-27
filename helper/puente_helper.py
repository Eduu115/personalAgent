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

# Estos no se actualizan desde aqui nunca, aunque alguien los meta en
# stacks.conf. La lista vive en el codigo a proposito, y se comprueba tres veces:
# la clave, el directorio al que apunta y los servicios que define su compose.
#
#   apiarena   el TFG, en produccion.
#   puente     se mataria a si mismo a mitad de la operacion y dejaria la
#              aprobacion colgada.
#   nextcloud  aloja fotos familiares irreemplazables, y un pull a ciegas puede
#              saltarse varias versiones mayores: Nextcloud solo migra el
#              esquema de una version mayor a la siguiente, asi que saltar dos
#              deja la base a medias. Se actualiza a mano, de una en una.
EXCLUIDOS = {"apiarena", "puente", "nextcloud"}

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


def servicios(ruta: str) -> set[str]:
    """Los servicios que define ese compose. Solo lee el YAML, no arranca nada."""
    r = compose(ruta, "config", "--services", tope=30)
    if r.returncode != 0:
        raise RuntimeError(f"no se puede leer el compose de {ruta}: {r.stderr[-200:].strip()}")
    return set(r.stdout.split())


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
    # Y el contenido: una clave inocente puede apuntar a un compose que levante
    # nextcloud. Si el compose no se puede leer tampoco se sigue: sin saber que
    # hay dentro no se actualiza (el pull fallaria igual, pero mas tarde).
    try:
        prohibidos = sorted(servicios(ruta) & EXCLUIDOS)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        log.warning("rechazado: '%s': %s", stack, exc)
        return {"ok": False, "error": str(exc)}
    if prohibidos:
        log.warning("rechazado: el compose de '%s' define %s", stack, ", ".join(prohibidos))
        return {"ok": False,
                "error": f"el compose de '{stack}' define {', '.join(prohibidos)}, que esta excluido"}

    empezo = time.monotonic()
    anteriores = digests(ruta)
    log.info("actualizando '%s' (%s)", stack, ruta)
    # Solo pull y up. Nada de --remove-orphans: borra contenedores del proyecto
    # que ya no esten en el compose, y este proceso no borra nada de nada.
    for args, tope in ((("pull",), TOPE_PULL), (("up", "-d"), TOPE_UP)):
        r = compose(ruta, *args, tope=tope)
        if r.returncode != 0:
            log.error("'%s' fallo en %s: %s", stack, args[0], r.stderr[-300:])
            return {"ok": False, "error": f"compose {args[0]} fallo: {r.stderr[-300:].strip()}",
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
