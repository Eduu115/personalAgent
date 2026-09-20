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

# El .env lleva los secretos: solo para su dueno. Se comprueba en cada
# despliegue porque esta barrera ya aparecio caida una vez en el server (664).
# Con umask 002, cp .env.example .env lo crea asi desde el principio.
modo="$(stat -c %a .env)"
if [ "${modo: -2}" != "00" ]; then
    chmod 600 .env
    echo "AVISO: .env tenia permisos $modo, accesible por grupo u otros; corregido a 600"
fi

grep -q $'\r' .env && fallo ".env tiene saltos de linea CRLF: dos2unix .env"
grep -qE '^ANTHROPIC_API_KEY=sk-ant-\.\.\.$' .env && fallo "ANTHROPIC_API_KEY sigue siendo el placeholder"
grep -qE '^POSTGRES_PASSWORD=.+' .env || fallo "POSTGRES_PASSWORD vacio en .env"
grep -qE '^LITELLM_MASTER_KEY=sk-.+' .env || fallo "LITELLM_MASTER_KEY vacio en .env"
grep -qE '^REDIS_PASSWORD=.+' .env || fallo "REDIS_PASSWORD vacio en .env: openssl rand -hex 24"
for t in NTFY_TOKEN_PUBLICAR NTFY_TOKEN_SUSCRIBIR; do
    grep -qE "^$t=tk_[a-z0-9]{29}\$" .env || fallo "$t vacio o mal formado en .env: docker run --rm binwiederhier/ntfy:v2.28.0 token generate"
done
grep -qE '^PUENTE_BASE_URL=https://[^/]+[^/]$' .env || fallo "PUENTE_BASE_URL vacia, sin https o con barra final en .env: es la URL del agente en el tailnet, la que abren los botones del push"
# Con upstream (iPhone) ntfy no arranca sin base-url.
grep -qE '^NTFY_BASE_URL=https://[^/]+[^/]$' .env || fallo "NTFY_BASE_URL vacia o con barra final en .env: la URL de tailscale serve de ntfy, la misma que en las apps"
# Entre comillas simples o Compose se come los $ del hash y ntfy recibe otro.
for h in NTFY_PASS_HASH_PUENTE NTFY_PASS_HASH_EDU; do
    grep -qE "^$h='\\\$2[aby]\\\$[0-9]{2}\\\$[./A-Za-z0-9]{53}'\$" .env \
        || fallo "$h vacio o sin comillas simples en .env: $h='\$2a\$10\$...' (docker run --rm -it binwiederhier/ntfy:v2.28.0 user hash)"
done

# En el server no se edita a mano: lo que no esta en git no existe.
if ! git diff --quiet || ! git diff --cached --quiet; then
    fallo "hay cambios locales sin commitear en el server"
fi

# sed y no grep | cut: si la variable no esta, grep sale con 1 y con set -e y
# pipefail el script muere en silencio, sin llegar al valor por defecto.
AGENT_PORT="$(sed -n 's/^AGENT_PORT=//p' .env)"
AGENT_PORT="${AGENT_PORT:-8420}"

# Un puerto ocupado solo vale si lo tiene nuestro propio contenedor.
if puerto_ocupado "$AGENT_PORT" && ! contenedor_vivo puente-agent; then
    fallo "el puerto $AGENT_PORT esta ocupado por otro proceso: ss -ltnp | grep $AGENT_PORT"
fi
if puerto_ocupado 4141 && ! contenedor_vivo puente-litellm; then
    fallo "el puerto 4141 esta ocupado por otro proceso: ss -ltnp | grep 4141"
fi

MCP_PORT="$(sed -n 's/^MCP_PORT=//p' .env)"
MCP_PORT="${MCP_PORT:-8421}"
if puerto_ocupado "$MCP_PORT" && ! contenedor_vivo puente-homelab-mcp; then
    fallo "el puerto $MCP_PORT esta ocupado por otro proceso: ss -ltnp | grep $MCP_PORT"
fi

NTFY_PORT="$(sed -n 's/^NTFY_PORT=//p' .env)"
NTFY_PORT="${NTFY_PORT:-8423}"
if puerto_ocupado "$NTFY_PORT" && ! contenedor_vivo puente-ntfy; then
    fallo "el puerto $NTFY_PORT esta ocupado por otro proceso: ss -ltnp | grep $NTFY_PORT"
fi

GOOGLE_MCP_PORT="$(sed -n 's/^GOOGLE_MCP_PORT=//p' .env)"
GOOGLE_MCP_PORT="${GOOGLE_MCP_PORT:-8422}"
if puerto_ocupado "$GOOGLE_MCP_PORT" && ! contenedor_vivo puente-google-mcp; then
    fallo "el puerto $GOOGLE_MCP_PORT esta ocupado por otro proceso: ss -ltnp | grep $GOOGLE_MCP_PORT"
fi

# El socket de Docker solo lo ve el proxy. Si no esta, homelab-mcp no arranca.
[ -S /var/run/docker.sock ] || fallo "no existe /var/run/docker.sock"

# El proxy corre sin root y abre el socket por el grupo que es su dueno.
DOCKER_GID="$(sed -n 's/^DOCKER_GID=//p' .env)"
if [ -z "$DOCKER_GID" ]; then
    DOCKER_GID="$(stat -Lc %g /var/run/docker.sock)"
    # Con > se trunca y se escribe en el mismo inodo: modo y propietario no
    # cambian, sin depender de lo que haga sed -i con el fichero nuevo. Sin
    # temporal en /tmp, que el .env lleva secretos. $(...) se come los saltos
    # de linea del final: da igual si el .env acababa en uno o no.
    resto="$(sed '/^DOCKER_GID=/d' .env)"
    printf '%s\nDOCKER_GID=%s\n' "$resto" "$DOCKER_GID" > .env
    echo "DOCKER_GID=$DOCKER_GID anadido a .env"
fi

# Los botones de aprobar van a un topic aparte: si ntfy no lo concede, el push
# sale con 403 y las aprobaciones no llegan a ningun sitio.
docker compose config 2>/dev/null | grep -E '^\s+NTFY_AUTH_ACCESS:' | grep -q 'aprobaciones' \
    || fallo "el topic 'aprobaciones' no esta en NTFY_AUTH_ACCESS (docker-compose.yml)"

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
echo "OK: rama $RAMA, puertos agente $AGENT_PORT / litellm 4141 / mcp $MCP_PORT / google-mcp $GOOGLE_MCP_PORT / ntfy $NTFY_PORT, DOCKER_GID $DOCKER_GID, ${disponible_mib} MiB disponibles"

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

# ---------------------------------------------------------------- el codigo arranca

# Antes de tocar la base de datos: si el codigo nuevo no levanta, el despliegue
# se para aqui y el viejo sigue sirviendo con su esquema. Al reves (migrar y
# luego descubrir que el agente no arranca) deja la base adelantada respecto al
# codigo. La F2 se llevo por delante una funcion que main.py seguia llamando y
# el agente entro en bucle de reinicio: esto es para que eso pare aqui.
log "comprobando que el codigo nuevo arranca"
docker compose build agent homelab-mcp google-mcp
docker compose run --rm --no-deps -T agent python -m app.pruebas \
    || fallo "el agente no arranca con este codigo: no se ha tocado la base de datos"
docker compose run --rm --no-deps -T google-mcp python -m app.pruebas \
    || fallo "google-mcp no pasa sus comprobaciones: no se ha tocado la base de datos"
docker compose run --rm --no-deps -T homelab-mcp python -c "import app.server" \
    || fallo "homelab-mcp no arranca con este codigo: no se ha tocado la base de datos"

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

# ---------------------------------------------------------------- migraciones

# Antes de levantar el agente, nunca desde su arranque: si una migracion peta,
# el despliegue se para aqui y el codigo viejo sigue sirviendo.
log "migraciones"
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q' <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
SQL
for fichero in db/migrations/*.sql; do
    version="$(basename "$fichero")"
    # La consulta por la entrada estandar: asi no hay que anidar comillas.
    aplicada="$(printf "SELECT 1 FROM schema_migrations WHERE version = '%s';" "$version" \
        | docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA')"
    if [ "$aplicada" = "1" ]; then
        continue
    fi
    echo "aplicando $version"
    # La migracion y su fila van en la misma transaccion: o entra entera o nada.
    { cat "$fichero"; printf "INSERT INTO schema_migrations (version) VALUES ('%s');\n" "$version"; } \
        | docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q --single-transaction' \
        || fallo "la migracion $version ha fallado: el stack se queda como estaba"
done
echo "migraciones al dia"

# ---------------------------------------------------------------- stack

log "levantando el stack"

# El socket-proxy, siempre recreado. Su config va por bind mount y HAProxy la
# lee solo al arrancar: si cambia haproxy.cfg, compose no recrea el contenedor
# (el fichero no es parte de su configuracion) y el proceso se queda con la
# config vieja en memoria, con el fichero nuevo en disco. Paso: la F2 abrio el
# POST /restart y el proxy siguio denegandolo. Es un proxy sin estado, tarda un
# segundo y tiene healthcheck.
docker compose up -d --force-recreate --wait docker-socket-proxy

docker compose up -d --build --remove-orphans --wait --wait-timeout 300

# ---------------------------------------------------------------- verificacion

log "verificacion"
curl -fsS -m 5 "http://127.0.0.1:${AGENT_PORT}/readyz"
echo

# Una barrera que no se prueba no existe. El socket-proxy solo deja pasar GET a
# seis rutas; si algo de esto no da 403, el agente podria parar contenedores o
# leer Config.Env, los secretos de todos. Se prueba contra un contenedor que no
# existe para que un proxy roto no pare nada de verdad (daria 404, que tambien
# falla). /containers/json tiene que pasar: un proxy que lo corta todo tambien
# esta mal.
docker compose exec -T homelab-mcp python - <<'PY' || fallo "el socket-proxy no se comporta como la lista blanca de config/haproxy.cfg"
import sys, urllib.error, urllib.request

def codigo(metodo, ruta):
    req = urllib.request.Request("http://docker-socket-proxy:2375" + ruta, method=metodo)
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        return f"error: {exc}"

casos = [
    ("POST", "/containers/no-existe/stop", 403),
    # Lo unico que se puede escribir es reiniciar los contenedores del puente.
    # Que apiarena-postgres de 403 es la comprobacion que importa: al lado corre
    # produccion. Un 204 en puente-ntfy reinicia ntfy de verdad, que es la unica
    # forma de comprobar que el camino permitido tambien funciona.
    ("POST", "/containers/apiarena-postgres/restart", 403),
    ("POST", "/containers/puente-postgres/restart", 403),
    ("POST", "/containers/puente-socket-proxy/restart", 403),
    # Lo que se abre es la ruta /restart, no el contenedor: con el mismo nombre
    # permitido, cualquier otro verbo de docker sigue dando 403.
    ("POST", "/containers/puente-ntfy/stop", 403),
    ("POST", "/containers/puente-agent/pause", 403),
    ("POST", "/v1.44/containers/apiarena-postgres/restart", 403),
    ("POST", "/containers/puente-ntfy/restart", 204),
    ("POST", "/v1.44/containers/puente-ntfy/restart", 204),
    ("GET", "/containers/no-existe/json", 403),
    ("GET", "/v1.44/containers/no-existe/json", 403),
    ("GET", "/containers/no-existe/archive?path=/", 403),
    ("GET", "/containers/no-existe/export", 403),
    ("GET", "/containers/json", 200),
]
mal = 0
for metodo, ruta, esperado in casos:
    obtenido = codigo(metodo, ruta)
    mal += obtenido != esperado
    veredicto = "OK" if obtenido == esperado else f"MAL, esperado {esperado}"
    print(f"socket-proxy: {metodo} {ruta} -> {obtenido} ({veredicto})")
sys.exit(1 if mal else 0)
PY

# El reinicio de prueba de ntfy lo deja arrancando: que vuelva a estar sano.
docker compose up -d --wait ntfy >/dev/null

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
