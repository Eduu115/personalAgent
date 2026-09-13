#!/usr/bin/env bash
# Despliegue por git pull en el server. Idempotente: se puede lanzar siempre,
# tanto la primera vez como en cada actualizacion.
#
# Uso:  ./scripts/deploy.sh [rama]      (por defecto master)
#
# Se para antes de tocar nada si algo no cuadra: .env incompleto, cambios
# locales, puertos cogidos por otro, nombres de contenedor de otro proyecto o
# poca RAM libre. El host sirve APIArena en produccion: mejor fallar aqui que
# a medio levantar.

set -euo pipefail

cd "$(dirname "$0")/.."
RAMA="${1:-master}"

log() { printf '\n==> %s\n' "$*"; }
fallo() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# Con command substitution y no con "| grep -q": con pipefail, grep -q cierra
# la tuberia antes de tiempo y el SIGPIPE daria falsos negativos.
puerto_ocupado() { [ -n "$(ss -ltnH "( sport = :$1 )")" ]; }
contenedor_vivo() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

# ---------------------------------------------------------------- comprobaciones previas

log "comprobaciones previas"

[ -f .env ] || fallo "no hay .env: cp .env.example .env y rellenalo (ver README)"
grep -q $'\r' .env && fallo ".env tiene saltos de linea CRLF: dos2unix .env"
grep -qE '^ANTHROPIC_API_KEY=sk-ant-\.\.\.$' .env && fallo "ANTHROPIC_API_KEY sigue siendo el placeholder"
grep -qE '^POSTGRES_PASSWORD=.+' .env || fallo "POSTGRES_PASSWORD vacio en .env"
grep -qE '^LITELLM_MASTER_KEY=sk-.+' .env || fallo "LITELLM_MASTER_KEY vacio en .env"

# En el server no se edita a mano: lo que no esta en git no existe.
if ! git diff --quiet || ! git diff --cached --quiet; then
    fallo "hay cambios locales sin commitear en el server"
fi

AGENT_PORT="$(grep -E '^AGENT_PORT=' .env | cut -d= -f2)"
AGENT_PORT="${AGENT_PORT:-8420}"

# Un puerto ocupado solo vale si lo tiene nuestro propio contenedor.
if puerto_ocupado "$AGENT_PORT" && ! contenedor_vivo puente-agent; then
    fallo "el puerto $AGENT_PORT esta ocupado por otro proceso: ss -ltnp | grep $AGENT_PORT"
fi
if puerto_ocupado 4141 && ! contenedor_vivo puente-litellm; then
    fallo "el puerto 4141 esta ocupado por otro proceso: ss -ltnp | grep 4141"
fi

# Contenedores llamados puente-* que no son de este proyecto compose.
ajenos="$(docker ps -a --filter 'name=^puente-' \
    --format '{{.Names}} {{.Label "com.docker.compose.project"}}' | awk '$2 != "puente" {print $1}')"
[ -z "$ajenos" ] || fallo "hay contenedores puente-* de otro proyecto: $ajenos"

# Primer arranque: LiteLLM pica ~1,3 GiB mientras Prisma migra. Con el stack ya
# vivo, recrear contenedores no suma memoria (se para el viejo antes del nuevo).
disponible_mib="$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)"
if ! contenedor_vivo puente-litellm && [ "$disponible_mib" -lt 1800 ]; then
    fallo "solo hay ${disponible_mib} MiB disponibles y el primer arranque necesita ~1,8 GiB"
fi
echo "RAM disponible: ${disponible_mib} MiB"

# ---------------------------------------------------------------- codigo

log "git pull ($RAMA)"
antes="$(git rev-parse --short HEAD)"
git fetch --prune origin
git checkout -q "$RAMA"
git merge --ff-only "origin/$RAMA"
despues="$(git rev-parse --short HEAD)"
echo "$antes -> $despues"

# A partir de aqui, si algo falla se ve el estado sin tener que ir a buscarlo.
trap 'printf "\n--- fallo: estado del stack\n"; docker compose ps; docker compose logs --tail 30' ERR

# ---------------------------------------------------------------- base de datos

log "postgres"
docker compose up -d --wait postgres

# El init de /docker-entrypoint-initdb.d solo corre con el volumen vacio. Si el
# volumen ya existia, la base de LiteLLM hay que crearla aqui.
existe="$(docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA' <<'SQL'
SELECT count(*) FROM pg_database WHERE datname = 'litellm';
SQL
)"
if [ "$existe" = "0" ]; then
    echo "creando la base litellm"
    docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q' <<'SQL'
CREATE DATABASE litellm;
SQL
fi

# ---------------------------------------------------------------- stack

log "levantando el stack"
docker compose up -d --build --remove-orphans --wait --wait-timeout 300

# ---------------------------------------------------------------- verificacion

log "verificacion"
curl -fsS -m 5 "http://127.0.0.1:${AGENT_PORT}/readyz"
echo

oom=0
for c in $(docker compose ps -q); do
    linea="$(docker inspect -f '{{.Name}} oom={{.State.OOMKilled}} reinicios={{.RestartCount}}' "$c")"
    echo "$linea"
    case "$linea" in *oom=true*) oom=1 ;; esac
done
[ "$oom" = 0 ] || fallo "algun contenedor ha muerto por OOM: revisa su mem_limit"

log "consumo"
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}' | grep -E 'NAME|puente'
free -h

printf '\nDesplegado %s (%s). Para volver atras: git checkout %s && docker compose up -d --build --wait\n' \
    "$despues" "$RAMA" "$antes"
