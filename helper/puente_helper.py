#!/usr/bin/env python3
"""Helper del puente: actualiza un stack de compose. Corre en el HOST, como root.

Es el primer trozo de esto que vive fuera de los contenedores, y tiene acceso al
socket de Docker, que es equivalente a root. Por eso es pequeno, sin
dependencias y se lee de una sentada. Si crece, algo se ha hecho mal.

Habla JSON por lineas en un socket unix. No hay puerto ni token: el permiso es
el dueno y el modo del socket. Dos operaciones utiles y las dos van por CLAVE de
un fichero del host (root, 600, fuera del repo), nunca por parametro libre:

  actualizar(stack)   clave de /etc/puente/stacks.conf  -> compose pull + up -d
  despertar(equipo)   clave de /etc/puente/equipos.conf -> paquete magico a la LAN

Nunca una ruta, ni un comando, ni un fichero compose, ni una MAC suelta.
"""

import json
import logging
import os
import pwd
import socket
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
EQUIPOS = os.environ.get("PUENTE_EQUIPOS", "/etc/puente/equipos.conf")
# La broadcast de la LAN. La limitada vale en cualquier red sin saber la mascara;
# si algun dia hace falta la dirigida (192.168.1.255), se cambia aqui.
BROADCAST = os.environ.get("PUENTE_BROADCAST", "255.255.255.255")
# El 9 es el de toda la vida; alguna BIOS escucha en el 7.
PUERTO_WOL = int(os.environ.get("PUENTE_PUERTO_WOL", "9"))
# Quien manda el paquete: no hace falta root para un UDP a broadcast.
SIN_PRIVILEGIOS = os.environ.get("PUENTE_USUARIO_WOL", "nobody")
SOCKET = os.environ.get("PUENTE_SOCKET", "/run/puente/helper.sock")
GRUPO = int(os.environ.get("PUENTE_GRUPO_GID", "0"))
ESPERA_SALUD = 90       # s esperando a que los healthchecks pasen a healthy
TOPE_PULL = 480         # s: un pull lento no puede colgar esto para siempre
TOPE_UP = 300
TOPE_TOTAL = 600

log = logging.getLogger("puente-helper")


def mapa(fichero: str) -> dict[str, str]:
    """clave=valor por linea. Se relee en cada peticion: sin estado que refrescar.

    Si el fichero no existe, no hay nada configurado; quien pregunte se llevara
    un "no esta configurado. Hay: ninguno", que se entiende mejor que un traceback.
    """
    salida = {}
    try:
        with open(fichero) as f:
            for linea in f:
                linea = linea.strip()
                if linea and not linea.startswith("#") and "=" in linea:
                    clave, valor = linea.split("=", 1)
                    salida[clave.strip()] = valor.strip()
    except FileNotFoundError:
        log.warning("no existe %s: no hay nada configurado ahi", fichero)
    return salida


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
    configurados = mapa(CONFIG)
    ruta = configurados.get(stack)
    if ruta is None:
        log.warning("rechazado: '%s' no esta en %s", stack, CONFIG)
        return {"ok": False, "error": f"'{stack}' no esta configurado. Hay: {', '.join(sorted(configurados)) or 'ninguno'}"}
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


# ------------------------------------------------------------------ despertar


def paquete(mac: str) -> bytes:
    """El paquete magico: seis 0xFF y la MAC dieciseis veces. Eso es todo."""
    limpio = "".join(c for c in mac if c in "0123456789abcdefABCDEF")
    if len(limpio) != 12:
        raise ValueError("la MAC configurada no tiene 12 digitos hexadecimales")
    return b"\xff" * 6 + bytes.fromhex(limpio) * 16


def _enviar(datos: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):   # UDP y sin acuse: tres copias por si se pierde una
            s.sendto(datos, (BROADCAST, PUERTO_WOL))


def sin_root(hacer) -> None:
    """Corre algo en un hijo que ha soltado root, y espera a que acabe.

    Este proceso es root porque habla con el socket de Docker, que es
    equivalente a root. Mandar un UDP a broadcast no necesita nada de eso, asi
    que no lo hace como root: si el envio tuviera un fallo, lo tendria un
    proceso que no puede tocar Docker ni el sistema de ficheros.
    """
    hijo = os.fork()
    if hijo:
        if os.waitstatus_to_exitcode(os.waitpid(hijo, 0)[1]) != 0:
            raise RuntimeError("el proceso que manda el paquete ha fallado")
        return
    try:
        if os.geteuid() == 0:
            nadie = pwd.getpwnam(SIN_PRIVILEGIOS)
            os.setgroups([])
            os.setgid(nadie.pw_gid)
            os.setuid(nadie.pw_uid)   # sin vuelta atras: setuid desde root es definitivo
        hacer()
        os._exit(0)
    except BaseException as exc:
        log.error("el envio ha fallado en el hijo: %s: %s", type(exc).__name__, exc)
        os._exit(1)


def despertar(equipo: str) -> dict:
    """Manda el paquete magico al equipo. La MAC no sale de aqui."""
    configurados = mapa(EQUIPOS)
    mac = configurados.get(equipo)
    if mac is None:
        log.warning("rechazado: '%s' no esta en %s", equipo, EQUIPOS)
        return {"ok": False,
                "error": f"'{equipo}' no esta configurado. Hay: {', '.join(sorted(configurados)) or 'ninguno'}"}
    try:
        # Validar antes de forkear: si no, una MAC mal escrita en el fichero
        # muere en el hijo y lo unico que se sabe es que "algo ha fallado".
        datos = paquete(mac)
    except ValueError as exc:
        log.warning("rechazado: la MAC de '%s' no vale", equipo)
        return {"ok": False, "error": f"'{equipo}': {exc}"}
    try:
        sin_root(lambda: _enviar(datos))
    except Exception as exc:
        log.error("no se pudo despertar '%s': %s", equipo, exc)
        return {"ok": False, "error": f"no se pudo mandar el paquete: {exc}"}
    log.info("paquete magico enviado a '%s' por %s:%s", equipo, BROADCAST, PUERTO_WOL)
    return {
        "ok": True,
        "equipo": equipo,
        # Wake-on-LAN no tiene acuse de recibo: esto es lo unico que se puede decir.
        "aviso": "Paquete enviado. Wake-on-LAN no confirma nada: si el equipo estaba "
                 "apagado y lo tiene activado, tarda un rato en arrancar.",
    }


class Handler(socketserver.StreamRequestHandler):
    timeout = TOPE_TOTAL

    def handle(self) -> None:
        try:
            peticion = json.loads(self.rfile.readline() or b"{}")
            op = peticion.get("op")
            if op == "ping":
                respuesta = {"ok": True,
                             "stacks": sorted(set(mapa(CONFIG)) - EXCLUIDOS),
                             "equipos": sorted(mapa(EQUIPOS))}
            elif op == "actualizar":
                respuesta = actualizar(str(peticion.get("stack", "")))
            elif op == "despertar":
                respuesta = despertar(str(peticion.get("equipo", "")))
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
