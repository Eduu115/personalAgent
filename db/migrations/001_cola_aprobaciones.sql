-- F2: la cola de aprobaciones vive en tool_calls, no en una tabla aparte.
--
-- Una escritura no se ejecuta: se encola aqui con status='pending', un nonce de
-- un solo uso y una caducidad. Al resolverse, la misma fila pasa a approved y
-- luego a executed o failed, asi que el audit log cuenta la historia entera sin
-- tener que cruzar dos tablas.
--
-- Idempotente: se puede aplicar dos veces sin romper nada.

ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS nonce text;
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS resolved_at timestamptz;
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS resolved_by text;
ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL;

-- Se consulta en cada mensaje de chat ("¿esta conversacion esta bloqueada?") y
-- cada minuto (las que han caducado).
CREATE INDEX IF NOT EXISTS tool_calls_pendientes_conv_idx
    ON tool_calls (conversation_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS tool_calls_pendientes_caducan_idx
    ON tool_calls (expires_at) WHERE status = 'pending';

-- El nonce viaja en la URL del boton del push y no se reutiliza: al resolver,
-- la fila deja de estar pending y esa URL ya no vale para nada.
COMMENT ON COLUMN tool_calls.nonce IS 'de un solo uso: autoriza el boton del push mientras la fila siga pending';
COMMENT ON COLUMN tool_calls.resolved_by IS 'quien resolvio: usuario, caducidad...';
