-- Esquema de la F0.
--
-- La tabla tool_calls existe ya aunque todavia no haya herramientas.
-- El audit log se pone el dia uno o no se pone nunca: lo vas a querer
-- justo el dia que algo se comporte raro, y ese dia ya es tarde.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- conversaciones

CREATE TABLE IF NOT EXISTS conversations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversations_updated_idx
    ON conversations (updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id               bigserial PRIMARY KEY,
    conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             text NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content          text NOT NULL,
    model            text,
    -- contabilidad de tokens, para saber a que se va el presupuesto
    prompt_tokens    integer,
    output_tokens    integer,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_conversation_idx
    ON messages (conversation_id, created_at);

-- ---------------------------------------------------------------- auditoria

-- Niveles de riesgo:
--   read      -> se ejecuta sola, se loguea, no se pregunta
--   write     -> confirmacion en un toque desde la consola
--   sensitive -> confirmacion con el comando o el diff a la vista
CREATE TABLE IF NOT EXISTS tool_calls (
    id               bigserial PRIMARY KEY,
    conversation_id  uuid REFERENCES conversations(id) ON DELETE SET NULL,
    tool_name        text NOT NULL,
    risk             text NOT NULL DEFAULT 'read'
                     CHECK (risk IN ('read', 'write', 'sensitive')),
    status           text NOT NULL DEFAULT 'executed'
                     CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'executed', 'failed')),
    arguments        jsonb NOT NULL DEFAULT '{}'::jsonb,
    result           jsonb,
    error            text,
    model            text,
    -- de donde salio la orden: 'user' (tu, en la consola) o 'schedule'.
    -- Nunca 'content': el contenido de un correo no es una orden.
    origin           text NOT NULL DEFAULT 'user',
    requested_at     timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz
);

CREATE INDEX IF NOT EXISTS tool_calls_pending_idx
    ON tool_calls (requested_at DESC) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS tool_calls_conversation_idx
    ON tool_calls (conversation_id, requested_at DESC);

-- El audit log no se edita ni se borra. Se anade y punto.
CREATE OR REPLACE FUNCTION tool_calls_no_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'tool_calls es append-only: no se borran filas del audit log';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tool_calls_block_delete ON tool_calls;
CREATE TRIGGER tool_calls_block_delete
    BEFORE DELETE ON tool_calls
    FOR EACH ROW EXECUTE FUNCTION tool_calls_no_delete();

-- ---------------------------------------------------------------- utilidades

CREATE OR REPLACE FUNCTION touch_conversation() RETURNS trigger AS $$
BEGIN
    UPDATE conversations SET updated_at = now() WHERE id = NEW.conversation_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS messages_touch_conversation ON messages;
CREATE TRIGGER messages_touch_conversation
    AFTER INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION touch_conversation();
