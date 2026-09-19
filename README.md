# Puente de mando — F0

Esqueleto del asistente. Al terminar estos pasos tienes un agente que responde por
HTTPS desde cualquier dispositivo de tu tailnet, con las conversaciones guardadas
en Postgres.

## Antes de empezar

Comprueba que los puertos estan libres (el host ya tiene nginx-proxy y APIArena):

```bash
ss -ltnp | grep -E '8420|4141' || echo "libres"
```

Y que Tailscale tiene MagicDNS y HTTPS activados. En la consola de administracion
de Tailscale: **DNS → MagicDNS** y **DNS → HTTPS Certificates**. Sin eso,
`tailscale serve` no puede emitir el certificado.

```bash
tailscale status | head -3
tailscale cert --help >/dev/null && echo "HTTPS disponible"
```

## 1. Clonar y configurar

```bash
cd ~/apps
git clone https://github.com/Eduu115/personalAgent.git puente && cd puente

cp .env.example .env
openssl rand -hex 24   # -> POSTGRES_PASSWORD
openssl rand -hex 24   # -> LITELLM_MASTER_KEY (con el prefijo sk-)
nano .env              # pega los dos y tu ANTHROPIC_API_KEY
```

Revisa `config/litellm.yaml` y confirma que los identificadores de modelo siguen
vigentes en <https://docs.claude.com/en/docs/about-claude/models>.

## 2. Levantar

```bash
./scripts/deploy.sh
```

El script es el mismo para el primer despliegue y para cada actualizacion:
comprueba `.env`, puertos, RAM libre y que no haya cambios locales; hace
`git pull --ff-only`, crea la base `litellm` si falta, levanta el stack
esperando a que todo este *healthy* y termina con `/readyz`, OOM y consumo.
Si algo no cuadra se para antes de tocar nada. Acepta una rama como argumento
(`./scripts/deploy.sh mi-rama`); por defecto `master`.

A mano, sin el script:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f agent
```

El esquema SQL se aplica solo la primera vez que arranca Postgres (via
`/docker-entrypoint-initdb.d`). Si cambias el esquema despues, o lo migras a mano
o tiras el volumen con `docker compose down -v` (borra las conversaciones).

LiteLLM usa su propia base `litellm` en ese mismo Postgres (la crea
`db/init/02_litellm_db.sql`). Sin ella el tope de gasto **no se aplica**: LiteLLM
falla en abierto sin avisar. Si el volumen ya existia de antes, creala a mano:

```bash
docker compose exec postgres psql -U puente -d puente -c "CREATE DATABASE litellm"
docker compose up -d litellm
```

## 3. Comprobar en local

```bash
curl -s localhost:8420/healthz
curl -s localhost:8420/readyz          # este toca la base de datos

curl -N -X POST localhost:8420/api/chat \
  -H 'content-type: application/json' \
  -d '{"message":"Hola, preséntate en una frase"}'
```

Deberias ver el flujo SSE: `event: start`, varios `event: delta` y un `event: done`
con el recuento de tokens.

## 4. Exponerlo en el tailnet

```bash
sudo tailscale serve --bg http://127.0.0.1:8420
tailscale serve status
```

Eso te da `https://<nombre-del-host>.<tu-tailnet>.ts.net` con certificado valido,
sin tocar nginx-proxy ni los puertos 80/443, y sin contenedores extra.

Para retirarlo: `sudo tailscale serve --https=443 off`

> `serve` publica solo dentro del tailnet, que es justo lo que queremos.
> `tailscale funnel` lo abriria a internet — **no lo uses aqui**: el agente
> todavia no tiene autenticacion propia.

## 5. El hito de la F0

Tailscale en el movil, sales a la calle con datos moviles, y:

```
https://<host>.<tailnet>.ts.net/docs
```

Prueba `POST /api/chat` desde ahi. Si contesta, la F0 esta hecha.

---

## Vigilar el consumo

Esta maquina sirve APIArena en produccion. Despues de levantar el stack:

```bash
docker stats --no-stream --format \
  'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}' | grep puente
free -h
```

El stack completo deberia quedarse sobre 1,2-1,5 GB en reposo (LiteLLM es el que
mas come). Si algun contenedor esta pegado a su `mem_limit`, subelo en el compose
en vez de quitarlo — un limite alcanzado es informacion, no un estorbo.

## homelab-mcp

Los ojos del asistente sobre el homelab. Cuatro herramientas, todas de lectura:

| Herramienta | Que devuelve |
|---|---|
| `lab_status` | Estado, salud e imagen de cada contenedor, mas el resumen de Docker |
| `lab_host` | Memoria, swap, carga por nucleo y discos del anfitrion |
| `lab_stats` | Memoria de un contenedor y cuanto le queda para su `mem_limit` |
| `lab_logs` | Ultimas lineas de un contenedor, con los secretos redactados |

El socket de Docker solo lo ve `docker-socket-proxy`, un HAProxy con lista blanca
en `config/haproxy.cfg`: solo GET y solo `/_ping`, `/info`, `/version`,
`/containers/json`, `/containers/{id}/logs` y `/containers/{id}/stats`. Todo lo
demas da 403, incluido `/containers/{id}/json`, que devolveria las variables de
entorno (los secretos) de todos los contenedores. `deploy.sh` lo verifica en cada
despliegue: un POST, `json`, `archive` y `export` tienen que dar 403, y el
despliegue falla si alguno pasa.

Corre sin root; entra al socket por el grupo que es su dueno, `DOCKER_GID` en
`.env`. `deploy.sh` lo detecta y lo anade si falta.

### Usarlo desde Claude Code

Se publica en `127.0.0.1:8421`, asi que el mismo servidor sirve al agente y a
Claude Code sin duplicar la integracion:

```bash
claude mcp add --transport http homelab http://127.0.0.1:8421/mcp
```

Y a probar:

```
> como esta el server?
> ensename las ultimas 50 lineas de apiarena-kafka
```

### Comprobarlo a mano

El transporte tiene sesiones: un `tools/list` a pelo devuelve
`400 Missing session ID`. Primero `initialize`, y con el `mcp-session-id` que
devuelve, el resto:

```bash
H=(-H 'accept: application/json, text/event-stream' -H 'content-type: application/json')

SID=$(curl -si localhost:8421/mcp "${H[@]}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' \
  | awk -F': ' 'tolower($1)=="mcp-session-id" {print $2}' | tr -d '\r')

curl -s localhost:8421/mcp "${H[@]}" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

curl -s localhost:8421/mcp "${H[@]}" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | grep -o '"name":"lab_[a-z_]*"'
```

Tiene que listar las cuatro herramientas. Para llamar a una, el mismo patron con
`"method":"tools/call","params":{"name":"lab_host","arguments":{}}`.

## Desarrollo en Mac o Windows

El server es Ubuntu y `deploy.sh` es solo para el (usa `ss`, `/proc` y `stat` de
GNU). En local se levanta a mano, con Docker Desktop.

Docker Desktop no admite `propagation: rslave` en el bind de `/` que usa
`homelab-mcp`, y sin mas el contenedor no arranca. El override lo quita:

```bash
cp .env.example .env                  # y rellenalo como en el paso 1
cp docker-compose.override.yml.example docker-compose.override.yml

# DOCKER_GID: el grupo dueno del socket visto desde la VM (suele ser 0)
docker run --rm -v /var/run/docker.sock:/s alpine stat -c %g /s

docker compose up -d --build --wait
docker compose ps
```

Compose carga `docker-compose.override.yml` solo si existe, y esta en
`.gitignore`: en el server no se copia y no se aplica.

> **`lab_host` en Docker Desktop devuelve datos de la VM de Linux, no de tu
> maquina.** macOS no tiene `/proc`: la memoria, la carga y los discos que ves son
> los de la VM que monta Docker Desktop (unos 8 GiB y `/dev/vda1`). Es lo esperado,
> no un fallo del calculo. Los datos buenos solo salen en el server.

## Endpoints

| Metodo | Ruta | Que hace |
|---|---|---|
| `GET` | `/healthz` | Liveness. El proceso responde. |
| `GET` | `/readyz` | Readiness. Ademas la base de datos contesta. |
| `POST` | `/api/chat` | Chat con streaming SSE. Crea conversacion si no le pasas `conversation_id`. |
| `GET` | `/api/conversations` | Ultimas conversaciones con su numero de mensajes. |
| `GET` | `/api/conversations/{id}` | Historial de una conversacion. |
| `GET` | `/docs` | OpenAPI interactivo. |

## Operacion

```bash
docker compose logs -f agent            # seguir el agente
docker compose restart agent            # reiniciar solo el agente
docker compose up -d --build agent      # reconstruir tras tocar codigo
docker compose down                     # parar (conserva los datos)
docker compose down -v                  # parar y BORRAR las conversaciones

# psql dentro del contenedor
docker compose exec postgres psql -U puente -d puente

# el audit log, cuando haya herramientas
docker compose exec postgres psql -U puente -d puente \
  -c "select requested_at, tool_name, risk, status from tool_calls order by 1 desc limit 20;"
```

## Kill switch

```bash
sed -i 's/^READ_ONLY=.*/READ_ONLY=true/' .env && docker compose up -d agent
```

Hoy no cambia nada porque aun no hay herramientas, pero el interruptor ya esta
cableado para la F2.

## Que viene ahora

Lee `CLAUDE.md` — tiene el contexto completo, las reglas que no se negocian y la
hoja de ruta. La F1 empieza por `homelab-mcp`, que es la herramienta mas divertida
y la unica que hay que escribir desde cero.
