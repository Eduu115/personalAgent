-- 002: que el invariante de las pendientes lo imponga la base, no el codigo.
--
-- Una fila 'pending' es la que autoriza una escritura cuando se toca el boton
-- del push. Sin nonce no hay nada que comparar, y sin expires_at no caduca
-- nunca: en los dos casos seria una autorizacion abierta. Que no pueda existir.

ALTER TABLE tool_calls DROP CONSTRAINT IF EXISTS tool_calls_pending_completa;
ALTER TABLE tool_calls ADD CONSTRAINT tool_calls_pending_completa
    CHECK (status <> 'pending' OR (nonce IS NOT NULL AND expires_at IS NOT NULL));

-- El indice parcial de la F0 se queda sin trabajo: las dos consultas que hay
-- sobre pendientes van por conversacion y por caducidad, y esas son las de la
-- 001. tool_calls es la tabla con mas INSERTs del sistema, asi que un indice
-- que nadie lee solo cuesta.
DROP INDEX IF EXISTS tool_calls_pending_idx;
