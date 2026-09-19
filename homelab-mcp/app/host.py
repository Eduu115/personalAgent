"""Metricas del host, leidas de /proc y del filesystem montado en solo lectura.

Los montajes son :ro y el contenedor corre sin root, asi que esto solo puede
leer lo que ya es legible por cualquiera. Es el patron de node-exporter.

Dos montajes separados y no uno anidado: un bind de / no arrastra los
submontajes, asi que /proc va aparte. Y el bind de / lleva propagation rslave
para que las particiones montadas debajo si se vean; sin eso, el statvfs de
/home devolveria los datos de / y el informe de discos mentiria sin dar
ningun error, que es la peor clase de fallo.
"""

from __future__ import annotations

import os
from typing import Any

HOSTFS = os.environ.get("HOSTFS", "/hostfs")
PROC = os.environ.get("PROC_PATH", "/host/proc")

# Filesystems virtuales que no interesan en un informe de disco.
_FS_IGNORADOS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "securityfs", "cgroup",
    "cgroup2", "pstore", "efivarfs", "bpf", "debugfs", "tracefs", "fusectl",
    "configfs", "ramfs", "hugetlbfs", "mqueue", "autofs", "squashfs", "overlay",
    "nsfs", "binfmt_misc",
}


def comprobar_montajes() -> None:
    for ruta, pista in ((PROC, "/proc:/host/proc:ro"), (HOSTFS, "/:/hostfs:ro")):
        if not os.path.isdir(ruta):
            raise RuntimeError(
                f"falta el montaje {ruta} en el contenedor (esperado: {pista}). "
                "Sin el no se pueden leer las metricas del anfitrion."
            )


def _mib(kb: float) -> float:
    return round(kb / 1024)


def _meminfo() -> dict[str, int]:
    datos: dict[str, int] = {}
    with open(f"{PROC}/meminfo", encoding="utf-8") as f:
        for linea in f:
            clave, _, resto = linea.partition(":")
            partes = resto.split()
            if partes:
                datos[clave] = int(partes[0])  # kB
    return datos


def memoria() -> dict[str, Any]:
    comprobar_montajes()
    m = _meminfo()
    total = m.get("MemTotal", 0)
    disponible = m.get("MemAvailable", 0)
    swap_total = m.get("SwapTotal", 0)
    swap_libre = m.get("SwapFree", 0)
    return {
        "total_mib": _mib(total),
        "disponible_mib": _mib(disponible),
        "usado_mib": _mib(total - disponible),
        "usado_pct": round((total - disponible) / total * 100, 1) if total else None,
        "cache_mib": _mib(m.get("Cached", 0) + m.get("Buffers", 0)),
        "swap_total_mib": _mib(swap_total),
        "swap_usado_mib": _mib(swap_total - swap_libre),
    }


def carga() -> dict[str, Any]:
    comprobar_montajes()
    with open(f"{PROC}/loadavg", encoding="utf-8") as f:
        p = f.read().split()
    with open(f"{PROC}/uptime", encoding="utf-8") as f:
        segundos = float(f.read().split()[0])
    nucleos = os.cpu_count() or 1
    return {
        "load_1m": float(p[0]),
        "load_5m": float(p[1]),
        "load_15m": float(p[2]),
        "nucleos": nucleos,
        # Por encima de 1.0 hay mas trabajo listo que CPUs para atenderlo.
        "carga_por_nucleo_1m": round(float(p[0]) / nucleos, 2),
        "uptime_dias": round(segundos / 86400, 1),
    }


def discos() -> list[dict[str, Any]]:
    comprobar_montajes()
    vistos: set[str] = set()
    salida: list[dict[str, Any]] = []
    # /proc/1/mounts y no /proc/mounts: este ultimo enlaza a self/mounts, y
    # "self" es este proceso, asi que devolveria los montajes del contenedor
    # (/etc/hostname, /etc/hosts...) y no los del host. Con python suelto en el
    # host coinciden y el fallo no se ve. El pid 1 del host es su init.
    with open(f"{PROC}/1/mounts", encoding="utf-8") as f:
        lineas = f.readlines()
    for linea in lineas:
        campos = linea.split()
        if len(campos) < 3:
            continue
        dispositivo, punto, tipo = campos[0], campos[1], campos[2]
        if tipo in _FS_IGNORADOS or dispositivo in vistos:
            continue
        if not dispositivo.startswith("/dev/"):
            continue
        ruta = HOSTFS if punto == "/" else f"{HOSTFS}{punto}"
        try:
            st = os.statvfs(ruta)
        except OSError:
            continue
        # Mismas cuentas que df. ext4 reserva un 5% para root: f_bfree lo
        # incluye y f_bavail no. Calcular (total - bavail) / total cuenta esa
        # reserva como ocupada y en el disco de 1,8T se va 2,4 puntos (92 GiB)
        # respecto a df. df hace usado / (usado + disponible), que la deja fuera.
        total = st.f_blocks * st.f_frsize
        usado = total - st.f_bfree * st.f_frsize
        disponible = st.f_bavail * st.f_frsize  # la columna "Disp" de df
        if total == 0:
            continue
        vistos.add(dispositivo)
        salida.append(
            {
                "punto_montaje": punto,
                "dispositivo": dispositivo,
                "total_gib": round(total / 1073741824, 1),
                "libre_gib": round(disponible / 1073741824, 1),
                "usado_pct": round(usado / (usado + disponible) * 100, 1) if usado + disponible else None,
            }
        )
    return sorted(salida, key=lambda d: d["punto_montaje"])
