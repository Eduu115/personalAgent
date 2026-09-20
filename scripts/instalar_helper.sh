#!/usr/bin/env bash
# Instala el helper del puente en el host. Se ejecuta A MANO, con sudo, y dice
# todo lo que va a hacer antes de hacerlo. El despliegue no instala nada de esto.
#
#   sudo ./scripts/instalar_helper.sh
#
# Para desinstalarlo, ./scripts/instalar_helper.sh --desinstalar (ver README).

set -euo pipefail
cd "$(dirname "$0")/.."

GRUPO=puente-helper
DESTINO=/usr/local/lib/puente/puente_helper.py
CONFIG=/etc/puente/stacks.conf
UNIDAD=/etc/systemd/system/puente-helper.service

fallo() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" = "0" ] || fallo "esto va con sudo: sudo $0 $*"

if [ "${1:-}" = "--desinstalar" ]; then
    cat <<FIN

Voy a DESINSTALAR el helper:
  - parar y deshabilitar el servicio puente-helper
  - borrar $UNIDAD y $DESTINO
  - DEJAR $CONFIG y el grupo $GRUPO (por si vuelves a instalarlo)

FIN
    read -rp "¿Sigo? [s/N] " ok
    [ "$ok" = "s" ] || { echo "no se ha tocado nada"; exit 0; }
    systemctl disable --now puente-helper.service 2>/dev/null || true
    rm -f "$UNIDAD" "$DESTINO"
    systemctl daemon-reload
    echo "hecho. La configuracion sigue en $CONFIG y el grupo $GRUPO existe todavia."
    exit 0
fi

cat <<FIN

Voy a instalar el helper del puente. Esto es lo que va a pasar, y nada mas:

  1. Crear el grupo $GRUPO si no existe. Quien este en ese grupo puede pedirle
     al helper que actualice un stack; es todo el control de acceso que hay.
  2. Copiar helper/puente_helper.py a $DESTINO (root:root, 0755).
  3. Crear $CONFIG (root:root, 0600) si no existe, con un ejemplo comentado.
     Ahi decides tu que stacks se pueden actualizar. El helper NUNCA acepta una
     ruta: solo claves de ese fichero.
  4. Instalar la unidad $UNIDAD y arrancarla.

El helper corre como root porque habla con el socket de Docker. Nunca actualiza
'apiarena' ni 'puente', aunque los pongas en $CONFIG: esa lista esta en su codigo.

FIN
read -rp "¿Sigo? [s/N] " ok
[ "$ok" = "s" ] || { echo "no se ha tocado nada"; exit 0; }

getent group "$GRUPO" >/dev/null || { groupadd --system "$GRUPO"; echo "grupo $GRUPO creado"; }
GID="$(getent group "$GRUPO" | cut -d: -f3)"

install -d -m 0755 /usr/local/lib/puente
install -m 0755 -o root -g root helper/puente_helper.py "$DESTINO"
echo "helper instalado en $DESTINO"

if [ ! -f "$CONFIG" ]; then
    install -d -m 0755 /etc/puente
    cat > "$CONFIG" <<'CONF'
# Stacks que el helper puede actualizar: nombre=/ruta/al/directorio
# El nombre es lo unico que viaja desde el agente. Ejemplo:
#nextcloud=/home/edu/apps/nextcloud
CONF
    chmod 0600 "$CONFIG"; chown root:root "$CONFIG"
    echo "configuracion creada en $CONFIG (vacia: anade tus stacks)"
fi

cat > "$UNIDAD" <<UNIT
[Unit]
Description=Helper del puente: actualiza stacks de docker compose
After=docker.service
Requires=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 $DESTINO
Environment=PUENTE_GRUPO_GID=$GID
Restart=on-failure
RestartSec=5
RuntimeDirectory=puente
RuntimeDirectoryMode=0750
# Corre como root porque el socket de Docker lo es todo, pero sin nada mas:
# no puede escalar privilegios, el sistema de ficheros es de solo lectura salvo
# lo suyo, y solo habla por sockets unix (quien baja las imagenes es el demonio).
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX
RestrictSUIDSGID=true
ReadWritePaths=/run/puente

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now puente-helper.service
sleep 1
systemctl is-active --quiet puente-helper.service || fallo "el servicio no ha arrancado: journalctl -u puente-helper -n 30"

cat <<FIN

Listo. El helper escucha en /run/puente/helper.sock (root:$GRUPO, 0660).

Lo que falta, en el .env del proyecto:

  HELPER_GID=$GID
  STACKS_ACTUALIZABLES=<los nombres de $CONFIG, separados por comas>

Sin STACKS_ACTUALIZABLES, la herramienta lab_update_stack no existe para el
agente. Despues, ./scripts/deploy.sh, que comprueba que todo esto responde.

FIN
