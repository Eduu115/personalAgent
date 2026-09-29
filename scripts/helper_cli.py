#!/usr/bin/env python3
"""Una peticion al helper del host, para deploy.sh y para mirar a mano.

    python3 scripts/helper_cli.py /run/puente/helper.sock '{"op":"ping"}'

Los fallos de conexion salen con su motivo en vez de con un traceback: "no
existe", "no tengo permiso" y "nadie escucha" son tres problemas distintos y
mandan a sitios distintos.
"""
import socket
import sys

ruta = sys.argv[1]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(30)
try:
    s.connect(ruta)
except PermissionError:
    import getpass
    sys.exit(
        f"sin permiso para usar {ruta}: el socket es root:puente-helper 0660 y "
        f"{getpass.getuser()} no esta en ese grupo. Arreglalo con:\n"
        f"    sudo usermod -aG puente-helper {getpass.getuser()}\n"
        f"y cierra sesion y vuelve a entrar: los grupos se leen al iniciar sesion, "
        f"asi que en esta misma shell seguira sin funcionar."
    )
except FileNotFoundError:
    sys.exit(f"no existe {ruta}: el servicio no esta corriendo (systemctl status puente-helper)")
except ConnectionRefusedError:
    sys.exit(f"{ruta} existe pero no escucha nadie: el helper se cayo y dejo el socket "
             f"(systemctl restart puente-helper)")
s.sendall(sys.argv[2].encode() + b"\n")
print(s.makefile().readline().strip())
