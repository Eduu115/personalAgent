-- Base de datos propia de LiteLLM, en el mismo Postgres pero separada de la
-- del agente. Prisma crea ahi sus ~90 tablas (LiteLLM_*) y las migra solo.
--
-- Sin esta base LiteLLM no aplica max_budget: sin DATABASE_URL el tope falla
-- en abierto. Solo corre en el primer arranque del volumen; en un volumen ya
-- inicializado hay que crearla a mano (ver README).

CREATE DATABASE litellm;
