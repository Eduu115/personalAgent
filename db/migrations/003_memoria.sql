-- F2: memoria a largo plazo. Dos cosas distintas y dos tablas distintas.
--
-- Ni pgvector ni embeddings: los hechos duraderos sobre una persona son decenas
-- de lineas y caben enteros en el prompt. Buscar por similitud resuelve un
-- problema que todavia no tenemos. El dia que el bloque no quepa, ese es el
-- momento; el codigo avisa con un WARNING cuando se acerca al tope.

-- 1. Continuidad: que el briefing de manana sepa lo que conto el de hoy.
CREATE TABLE IF NOT EXISTS briefings (
    id         bigserial PRIMARY KEY,
    creado_en  timestamptz NOT NULL DEFAULT now(),
    resumen    text NOT NULL,
    publicado  boolean NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS briefings_recientes_idx ON briefings (creado_en DESC);

-- 2. Hechos sobre el usuario. Solo lo que ha dicho el.
CREATE TABLE IF NOT EXISTS hechos (
    id               bigserial PRIMARY KEY,
    texto            text NOT NULL,
    ambito           text NOT NULL
                     CHECK (ambito IN ('perfil', 'preferencia', 'proyecto', 'contexto')),
    -- La base se niega a guardar un hecho que no venga del usuario. El dia que
    -- queramos hechos deducidos por el agente sera una migracion deliberada y
    -- no un descuido, igual que el CHECK de las pendientes.
    origen           text NOT NULL CHECK (origen = 'usuario'),
    conversation_id  uuid REFERENCES conversations(id) ON DELETE SET NULL,
    creado_en        timestamptz NOT NULL DEFAULT now(),
    caduca_en        timestamptz,  -- null = permanente
    vigente          boolean NOT NULL DEFAULT true
);

-- Lo que se lee en cada turno: los vigentes, por ambito.
CREATE INDEX IF NOT EXISTS hechos_vigentes_idx ON hechos (ambito, id) WHERE vigente;

COMMENT ON COLUMN hechos.vigente IS 'olvidar es ponerlo a false: aqui no se borran filas';
