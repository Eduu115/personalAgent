-- F4: proactividad. Una fila por cosa vigilada, con su estado anterior.
--
-- Sin estado guardado no hay transiciones, solo umbrales: cada comprobacion
-- volveria a decir "el disco esta al 87%" y a los tres dias el canal esta
-- silenciado. Lo que se avisa es el CAMBIO de bien a mal y el de vuelta.
--
-- La fila sobrevive a los despliegues a proposito: si el disco ya estaba mal
-- ayer, un reinicio del agente no es motivo para volver a avisar.

CREATE TABLE IF NOT EXISTS vigilancias (
    nombre      text PRIMARY KEY,
    estado      text NOT NULL CHECK (estado IN ('bien', 'mal')),
    -- Desde cuando esta asi. Es lo que deja decir "lleva 3 horas" en el aviso,
    -- que es la mitad de lo que hace falta para decidir si te levantas.
    desde       timestamptz NOT NULL DEFAULT now(),
    -- La histeresis: el estado que se esta confirmando y cuantas comprobaciones
    -- seguidas lleva. Un pico de CPU de treinta segundos no es una incidencia.
    candidato   text CHECK (candidato IN ('bien', 'mal')),
    racha       int NOT NULL DEFAULT 0,
    detalle     text,
    visto_en    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE vigilancias IS 'estado anterior de cada vigilancia: se avisa de transiciones, no de estados';
COMMENT ON COLUMN vigilancias.racha IS 'comprobaciones seguidas viendo el candidato; al llegar al tope, transicion';
