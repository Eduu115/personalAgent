#!/usr/bin/env bash
# Backup de las bases de puente en el disco de datos, fuera del NVMe donde vive
# Postgres. Lo lanza el cron a diario (la linea la instala deploy.sh) y
# deploy.sh antes de cada migracion.
#
#   ./scripts/backup.sh
#
# Lo que no se puede regenerar vive en la base del agente: el audit log
# (tool_calls, append-only por la regla 4), las conversaciones, los briefings y
# la memoria. La de LiteLLM lleva el gasto acumulado contra el tope. Redis y
# ntfy no se copian: son cache y mensajes ya entregados.
#
# Un volcado solo cuenta si pg_restore lo lee entero. Si algo falla, aviso por
# ntfy al topic de avisos y salida con error: un backup que falla callado es
# peor que no tener ninguno, porque crees que lo tienes.
#
# Restaurar una base (con el agente parado):
#   docker compose exec -T postgres pg_restore -U puente -d puente --clean --if-exists < /mnt/nas/backups/puente/puente-FECHA.dump

set -euo pipefail
cd "$(dirname "$0")/.."

DESTINO="${PUENTE_BACKUPS:-/mnt/nas/backups/puente}"
RETENCION_DIAS=30
FECHA="$(date +%Y%m%d-%H%M%S)"

leer_env() { sed -n "s/^$1=//p" .env | tail -1; }

avisar() {
    local token topic puerto
    token="$(leer_env NTFY_TOKEN_PUBLICAR)"
    topic="$(leer_env NTFY_TOPIC_AVISOS)"
    puerto="$(leer_env NTFY_PORT)"
    [ -n "$token" ] || return 0
    # El token por un descriptor y no en la linea de comandos: lo que va en
    # argv lo ve cualquier usuario del host con ps.
    curl -fsS -m 10 -o /dev/null \
        -H @<(printf 'Authorization: Bearer %s\n' "$token") \
        -H "Title: Backup de puente fallido" -H "Priority: high" -H "Tags: floppy_disk" \
        -d "$1" "http://127.0.0.1:${puerto:-8423}/${topic:-avisos}" \
        || echo "ademas, no se ha podido avisar por ntfy" >&2
}

fallo() {
    echo "ERROR: $*" >&2
    avisar "$*"
    exit 1
}

# Sin el HDD montado, /mnt/nas es un directorio inmutable de la raiz y escribir
# ahi falla: mejor decirlo claro que dejar un "Permission denied" suelto.
mountpoint -q /mnt/nas || fallo "/mnt/nas no está montado: hoy no hay backup de puente"
mkdir -p "$DESTINO"
chmod 700 "$DESTINO"
umask 077

usuario="$(leer_env POSTGRES_USER)"
base="$(leer_env POSTGRES_DB)"
for db in "${base:-puente}" litellm; do
    fichero="$DESTINO/$db-$FECHA.dump"
    if ! docker compose exec -T postgres pg_dump -U "${usuario:-puente}" -d "$db" -Fc > "$fichero.tmp"; then
        rm -f "$fichero.tmp"
        fallo "pg_dump de la base $db ha fallado"
    fi
    tablas="$(docker compose exec -T postgres pg_restore --list < "$fichero.tmp" | grep -c 'TABLE DATA' || true)"
    if [ "${tablas:-0}" -eq 0 ]; then
        rm -f "$fichero.tmp"
        fallo "el volcado de la base $db no se puede leer o no tiene tablas"
    fi
    mv "$fichero.tmp" "$fichero"
    echo "$db: $(stat -c %s "$fichero") bytes, $tablas tablas -> $fichero"
done

find "$DESTINO" -maxdepth 1 -name '*.dump' -mtime +"$RETENCION_DIAS" -print -delete | sed 's/^/borrado: /'
