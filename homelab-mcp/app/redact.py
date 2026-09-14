"""Redaccion de secretos en texto que va a acabar en el contexto de un modelo.

Los logs de un contenedor llevan claves mas veces de las que uno cree: una
libreria que loguea la cabecera Authorization, un DSN completo en un traceback,
un token en una URL. Todo eso pasaria intacto al prompt y de ahi al proveedor.

Esto no es infalible y no pretende serlo: es la ultima red antes de que un
secreto salga de la maquina. La primera sigue siendo no loguearlos.
"""

from __future__ import annotations

import re

_PATRONES: list[tuple[re.Pattern[str], str]] = [
    # Claves de API con prefijo reconocible (sk-, sk-ant-, ghp_, xoxb-, ...)
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{8,})"), "[REDACTADO:clave]"),
    # Cabeceras de autorizacion
    (re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*\S+"), r"\1: [REDACTADO]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}=*"), "Bearer [REDACTADO]"),
    # Credenciales dentro de una URL: postgresql://user:password@host
    (re.compile(r"\b([a-z][a-z0-9+.-]*://[^\s:/@]+):[^\s@]+@"), r"\1:[REDACTADO]@"),
    # Asignaciones con nombre sospechoso
    (
        re.compile(
            r"(?i)\b([a-z_]*(?:api[_-]?key|secret|token|passwd|password|credential)[a-z_]*)"
            r"[\"']?\s*[:=]\s*[\"']?([^\s\"',}]{6,})"
        ),
        r"\1=[REDACTADO]",
    ),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTADO:jwt]"),
]


def redactar(texto: str) -> tuple[str, int]:
    """Devuelve (texto_limpio, numero_de_sustituciones)."""
    total = 0
    for patron, reemplazo in _PATRONES:
        texto, n = patron.subn(reemplazo, texto)
        total += n
    return texto, total
