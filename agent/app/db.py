"""Pool de Postgres y acceso a datos.

Deliberadamente sin ORM: el esquema es pequeno y las consultas se leen mejor
en SQL plano. Si un dia crece, SQLAlchemy entra sin drama.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import settings

log = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None


async def open_pool() -> None:
    global _pool
    _pool = AsyncConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await _pool.open(wait=True, timeout=30)
    log.info("pool de postgres abierto")


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("pool de postgres cerrado")


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("el pool no esta abierto")
    return _pool


async def ping() -> bool:
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 AS ok")
            row = await cur.fetchone()
            return bool(row and row["ok"] == 1)


# ------------------------------------------------------------------ conversaciones


async def create_conversation(title: str | None = None) -> UUID:
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO conversations (title) VALUES (%s) RETURNING id",
                (title,),
            )
            row = await cur.fetchone()
            return row["id"]


async def conversation_exists(conversation_id: UUID) -> bool:
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM conversations WHERE id = %s", (conversation_id,)
            )
            return await cur.fetchone() is not None


async def list_conversations(limit: int = 50) -> list[dict[str, Any]]:
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT c.id,
                       c.title,
                       c.created_at,
                       c.updated_at,
                       count(m.id) AS message_count
                  FROM conversations c
                  LEFT JOIN messages m ON m.conversation_id = c.id
                 GROUP BY c.id
                 ORDER BY c.updated_at DESC
                 LIMIT %s
                """,
                (limit,),
            )
            return await cur.fetchall()


async def hay_briefing_desde(desde: Any) -> bool:
    """Si ya hay un briefing creado desde esa hora (programado o lanzado a mano)."""
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM conversations WHERE title LIKE 'Briefing del %%' AND created_at >= %s LIMIT 1",
                (desde,),
            )
            return await cur.fetchone() is not None


async def set_title_if_empty(conversation_id: UUID, title: str) -> None:
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE conversations
                   SET title = %s
                 WHERE id = %s AND (title IS NULL OR title = '')
                """,
                (title[:120], conversation_id),
            )


# ------------------------------------------------------------------ mensajes


async def anteponer_a_respuesta(conversation_id: UUID, texto: str) -> None:
    """Pone `texto` delante de la ultima respuesta del asistente.

    Para avisos que solo se conocen al acabar: el briefing no sabe hasta el
    final que parte no pudo consultar, y eso tiene que ir arriba.
    """
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE messages
                   SET content = %s || content
                 WHERE id = (SELECT max(id) FROM messages
                              WHERE conversation_id = %s AND role = 'assistant')
                """,
                (texto, conversation_id),
            )


async def add_message(
    conversation_id: UUID,
    role: str,
    content: str,
    model: str | None = None,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
) -> int:
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO messages
                    (conversation_id, role, content, model, prompt_tokens, output_tokens)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (conversation_id, role, content, model, prompt_tokens, output_tokens),
            )
            row = await cur.fetchone()
            return row["id"]


async def history(conversation_id: UUID, limit: int) -> list[dict[str, str]]:
    """Devuelve los ultimos `limit` mensajes en orden cronologico.

    Solo user y assistant. Un 'tool' releido llegaria a la API sin el assistant
    con tool_calls que lo justifica y la peticion se rechazaria. Los resultados
    de herramientas viven en tool_calls y en la peticion en curso, no aqui.
    """
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT role, content FROM (
                    SELECT role, content, created_at, id
                      FROM messages
                     WHERE conversation_id = %s
                       AND role IN ('user', 'assistant')
                     ORDER BY created_at DESC, id DESC
                     LIMIT %s
                ) AS recent
                ORDER BY created_at ASC, id ASC
                """,
                (conversation_id, limit),
            )
            return [{"role": r["role"], "content": r["content"]} for r in await cur.fetchall()]


# ------------------------------------------------------------------ memoria


async def guardar_briefing(resumen: str, publicado: bool) -> int:
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO briefings (resumen, publicado) VALUES (%s, %s) RETURNING id",
                (resumen, publicado),
            )
            return (await cur.fetchone())["id"]


async def ultimos_briefings(cuantos: int) -> list[dict[str, Any]]:
    """Los ultimos, para que el de hoy no repita lo que conto el de ayer."""
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT creado_en, resumen FROM briefings ORDER BY creado_en DESC LIMIT %s",
                (cuantos,),
            )
            return list(reversed(await cur.fetchall()))


async def hechos_vigentes() -> list[dict[str, Any]]:
    """Los hechos que siguen valiendo: vigentes y sin caducar."""
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, texto, ambito, creado_en, caduca_en
                  FROM hechos
                 WHERE vigente AND (caduca_en IS NULL OR caduca_en > now())
                 ORDER BY ambito, id
                """
            )
            return await cur.fetchall()


async def guardar_hecho(
    texto: str, ambito: str, *, conversation_id: UUID | None, caduca_en: Any = None
) -> dict[str, Any]:
    """Guarda un hecho. origen siempre 'usuario': la base no admite otra cosa."""
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO hechos (texto, ambito, origen, conversation_id, caduca_en)
                VALUES (%s, %s, 'usuario', %s, %s)
                RETURNING id, texto, ambito, creado_en, caduca_en
                """,
                (texto, ambito, conversation_id, caduca_en),
            )
            return await cur.fetchone()


async def olvidar_hecho(hecho_id: int) -> dict[str, Any] | None:
    """Borrado logico: la fila se queda, deja de estar vigente."""
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE hechos SET vigente = false WHERE id = %s AND vigente RETURNING id, texto, ambito",
                (hecho_id,),
            )
            return await cur.fetchone()


# ------------------------------------------------------------------ cola de aprobaciones


async def crear_pendiente(
    tool_name: str,
    *,
    conversation_id: UUID,
    risk: str,
    arguments: dict[str, Any],
    model: str,
    nonce: str,
    minutos: int,
) -> dict[str, Any]:
    """Encola una escritura y devuelve la fila. Los argumentos van sin truncar:
    el push tiene que ensenar exactamente lo que se va a ejecutar."""
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO tool_calls
                    (conversation_id, tool_name, risk, status, arguments, model, origin,
                     nonce, expires_at)
                VALUES (%s, %s, %s, 'pending', %s, %s, 'user', %s, now() + make_interval(mins => %s))
                RETURNING *
                """,
                (conversation_id, tool_name, risk, json.dumps(arguments), model, nonce, minutos),
            )
            return await cur.fetchone()


async def pendiente_igual(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Una pendiente viva con la misma herramienta y los mismos argumentos.

    Da igual de que conversacion sea: dos notificaciones identicas en el movil
    no se distinguen, y aprobar una dejaria la otra esperando para ejecutar lo
    mismo otra vez. Con un reinicio da igual; con un borrador, son dos correos.
    """
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT * FROM tool_calls
                 WHERE status = 'pending' AND tool_name = %s AND arguments = %s::jsonb
                   AND expires_at > now()
                 ORDER BY id LIMIT 1
                """,
                (tool_name, json.dumps(arguments)),
            )
            return await cur.fetchone()


async def pendiente_de(conversation_id: UUID) -> dict[str, Any] | None:
    """La accion que espera un OK en esa conversacion, si hay alguna.

    Mientras la haya, la conversacion no admite mensajes nuevos: un tool_use sin
    su tool_result con mensajes de usuario por medio es lo que la API rechaza.
    """
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT *, EXTRACT(EPOCH FROM (expires_at - now())) AS quedan_seg
                  FROM tool_calls
                 WHERE conversation_id = %s AND status = 'pending'
                 ORDER BY id LIMIT 1
                """,
                (conversation_id,),
            )
            return await cur.fetchone()


async def pendientes() -> list[dict[str, Any]]:
    """Todo lo que espera un OK y no ha caducado, para la consola."""
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT *, EXTRACT(EPOCH FROM (expires_at - now())) AS quedan_seg
                  FROM tool_calls
                 WHERE status = 'pending' AND expires_at > now()
                 ORDER BY id
                """
            )
            return await cur.fetchall()


async def llamada(tool_call_id: int) -> dict[str, Any] | None:
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM tool_calls WHERE id = %s", (tool_call_id,))
            return await cur.fetchone()


async def resolver_pendiente(tool_call_id: int, status: str, resolved_by: str) -> dict[str, Any] | None:
    """Saca la fila de pending. Devuelve None si ya no lo estaba.

    El WHERE status = 'pending' es lo que hace el nonce de un solo uso: dos
    toques al mismo boton, o un toque y una caducidad, solo resuelven una vez.
    """
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE tool_calls
                   SET status = %s, resolved_by = %s, resolved_at = now(), nonce = NULL
                 WHERE id = %s AND status = 'pending'
                RETURNING *
                """,
                (status, resolved_by, tool_call_id),
            )
            return await cur.fetchone()


async def cerrar_llamada(
    tool_call_id: int, *, status: str, result: Any = None, error: str | None = None
) -> None:
    """El desenlace de una llamada aprobada: executed o failed."""
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE tool_calls
                   SET status = %s, result = %s, error = %s, resolved_at = now()
                 WHERE id = %s
                """,
                (status, json.dumps(result) if result is not None else None, error, tool_call_id),
            )


async def caducadas() -> list[dict[str, Any]]:
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM tool_calls WHERE status = 'pending' AND expires_at <= now() ORDER BY id"
            )
            return await cur.fetchall()


async def purgar_payloads(dias: int) -> int:
    """Borra el contenido de las llamadas viejas y deja la metadata.

    tool_calls es append-only (hay un trigger que bloquea los DELETE), asi que
    no se borran filas: se vacia lo que tiene dentro. Quien llamo a que, cuando
    y como acabo se queda para siempre; los asuntos y los cuerpos de los correos
    no tienen por que.
    """
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE tool_calls
                   SET arguments = '{"_purgado": true}'::jsonb, result = NULL, error = NULL
                 WHERE requested_at < now() - make_interval(days => %s)
                   AND arguments <> '{"_purgado": true}'::jsonb
                """,
                (dias,),
            )
            return cur.rowcount


# ------------------------------------------------------------------ auditoria


async def log_tool_call(
    tool_name: str,
    *,
    conversation_id: UUID | None = None,
    risk: str = "read",
    status: str = "executed",
    arguments: dict[str, Any] | None = None,
    result: Any = None,
    error: str | None = None,
    model: str | None = None,
    origin: str = "user",
) -> int:
    """Registra una llamada a herramienta. Append-only, sin excepciones."""
    async with pool().connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO tool_calls
                    (conversation_id, tool_name, risk, status, arguments, result,
                     error, model, origin, resolved_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s IN ('executed','failed','rejected','expired')
                             THEN now() ELSE NULL END)
                RETURNING id
                """,
                (
                    conversation_id,
                    tool_name,
                    risk,
                    status,
                    json.dumps(arguments or {}),
                    json.dumps(result) if result is not None else None,
                    error,
                    model,
                    origin,
                    status,
                ),
            )
            row = await cur.fetchone()
            return row["id"]
