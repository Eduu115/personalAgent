"""Cliente MCP del agente: habla con homelab-mcp por streamable HTTP.

Una sesion por peticion de chat, perezosa: se abre con la primera llamada que
la necesita, se reutiliza en todas las rondas y se cierra al terminar. Un chat
que no usa herramientas (con el catalogo en cache) no paga el handshake.

La sesion vive en su propia tarea y se le habla por una cola. El transporte de
mcp abre un task group, y si una peticion HTTP falla (homelab-mcp caido o
reiniciado) cancela todo lo que envuelve y sale con un ExceptionGroup. Abierta
dentro del bucle del chat, eso tumbaria la respuesta entera, "hola" incluido.
En su tarea solo falla la llamada en curso, y el modelo puede contarlo.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .config import settings

log = logging.getLogger(__name__)

Pedido = tuple[Callable[[ClientSession], Awaitable[Any]], asyncio.Future]


def _raiz(exc: BaseException) -> BaseException:
    """El error de verdad, sin las capas de ExceptionGroup de anyio."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


class Sesion:
    def __init__(self) -> None:
        self._pedidos: asyncio.Queue[Pedido | None] = asyncio.Queue()
        self._tarea: asyncio.Task[None] | None = None

    async def _servir(self, pedidos: asyncio.Queue[Pedido | None]) -> None:
        timeout = timedelta(seconds=settings.timeout_herramienta)
        async with streamable_http_client(settings.homelab_mcp_url) as (leer, escribir, _):
            async with ClientSession(leer, escribir, read_timeout_seconds=timeout) as s:
                await s.initialize()
                log.debug("sesion MCP abierta")
                while (pedido := await pedidos.get()) is not None:
                    fn, futuro = pedido
                    if futuro.done():
                        # Quien lo pidio ya se canso de esperar: no se ejecuta.
                        continue
                    try:
                        resultado = await fn(s)
                    except Exception as exc:
                        if not futuro.done():
                            futuro.set_exception(exc)
                    else:
                        if not futuro.done():
                            futuro.set_result(resultado)

    def _causa(self) -> str:
        assert self._tarea is not None
        if self._tarea.cancelled():
            return "cancelada"
        exc = self._tarea.exception()
        return repr(_raiz(exc)) if exc else "cerrada"

    async def _pedir(self, fn: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        if self._tarea is None or self._tarea.done():
            if self._tarea is not None:
                log.warning("la sesion MCP se ha caido (%s), se reabre", self._causa())
            self._pedidos = asyncio.Queue()
            self._tarea = asyncio.create_task(self._servir(self._pedidos))

        futuro: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pedidos.put_nowait((fn, futuro))
        await asyncio.wait(
            {futuro, self._tarea},
            timeout=settings.timeout_herramienta,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if futuro.done():
            return futuro.result()
        futuro.cancel()
        if self._tarea.done():
            raise ConnectionError(f"homelab-mcp no disponible: {self._causa()}")
        raise TimeoutError(f"homelab-mcp no ha respondido en {settings.timeout_herramienta:g} s")

    async def listar(self) -> list[types.Tool]:
        return (await self._pedir(lambda s: s.list_tools())).tools

    async def invocar(self, nombre: str, argumentos: dict[str, Any]) -> tuple[bool, str]:
        """Llama a una herramienta. Devuelve (hubo_error, texto)."""
        r = await self._pedir(lambda s: s.call_tool(nombre, argumentos))
        texto = "\n".join(
            b.text if isinstance(b, types.TextContent) else b.model_dump_json() for b in r.content
        )
        return bool(r.isError), texto

    async def cerrar(self) -> None:
        if self._tarea is None:
            return
        self._pedidos.put_nowait(None)
        try:
            await asyncio.wait_for(self._tarea, 5)
        except Exception as exc:  # TimeoutError incluido: wait_for ya la ha cancelado
            log.debug("cierre de la sesion MCP: %r", _raiz(exc))
