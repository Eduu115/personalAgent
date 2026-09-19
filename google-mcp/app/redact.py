"""Redaccion de secretos en texto que va a acabar en el contexto de un modelo.

Los logs de un contenedor llevan claves mas veces de las que uno cree: una
libreria que loguea la cabecera Authorization, un DSN completo en un traceback,
un token en una URL. Todo eso pasaria intacto al prompt y de ahi al proveedor.

Esto no es infalible y no pretende serlo: es la ultima red antes de que un
secreto salga de la maquina. La primera sigue siendo no loguearlos.

GEMELO: homelab-mcp/app/redact.py es una copia exacta de este fichero. Si cambias
uno, cambia el otro. Esta duplicado a proposito (deuda apuntada en CLAUDE.md):
40 lineas son mas baratas que compartir contexto de build entre dos servidores.
"""

from __future__ import annotations

import re

_PATRONES: list[tuple[re.Pattern[str], str]] = [
    # Claves de API con prefijo reconocible (sk-, sk-ant-, ghp_, xoxb-, ...)
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{8,})"), "[REDACTADO:clave]"),
    # Cabeceras de autorizacion, con el esquema delante (Bearer, Basic, Token...)
    # y tambien como clave de un dict o un JSON logueado. Sin el esquema opcional
    # el valor era la palabra "Bearer" y el token pasaba intacto.
    (
        re.compile(
            r"(?i)\b(authorization|proxy-authorization)[\"']?[ \t]*[:=][ \t]*[\"']?"
            r"(?:[a-z0-9-]+[ \t]+)?[^\s\"',}]+"
        ),
        r"\1: [REDACTADO]",
    ),
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


if __name__ == "__main__":
    # python -m app.redact: lo que tiene que desaparecer y lo que no.
    fuera = {
        "Authorization: Bearer 8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c": "8f7a6b5c",
        "authorization=Basic dXNlcjpwYXNzd29yZA==": "dXNlcjpw",
        "Proxy-Authorization: Basic Zm9vOmJhcg==": "Zm9vOmJh",
        "{'Authorization': 'Bearer 8f7a6b5c4d3e2f1a', 'Accept': 'json'}": "8f7a6b5c",
        '{"authorization": "Token abcdef123456"}': "abcdef12",
        "Authorization: 8f7a6b5c4d3e2f1a": "8f7a6b5c",
        'curl -H "Bearer 8f7a6b5c4d3e2f1a0b9c"': "8f7a6b5c",
        "OPENAI_API_KEY=sk-abcdefgh12345678": "sk-abcdefgh",
        "postgresql://app:hunter2secreto@db:5432/x": "hunter2",
        "jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIx.c2lnbmF0dXJl": "eyJhbGci",
    }
    for texto, secreto in fuera.items():
        limpio, n = redactar(texto)
        assert secreto not in limpio and n, (texto, limpio)
    # Lo que no es un secreto se queda como esta.
    for texto in ("authorization failed for user bob", "{'Accept': 'json'}"):
        assert redactar(texto) == (texto, 0), (texto, redactar(texto))
    # Ni el esquema salta de linea ni la redaccion se come las claves siguientes.
    assert redactar("Authorization: Bearer\nsiguiente linea")[0].endswith("\nsiguiente linea")
    assert redactar("{'Authorization': 'Bearer 8f7a6b5c4d3e2f1a', 'Accept': 'json'}")[0].endswith("'Accept': 'json'}")
    print("redact OK:", len(fuera), "secretos fuera")
