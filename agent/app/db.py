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
