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
git clone <tu-repo> puente && cd puente

cp .env.example .env
openssl rand -hex 24   # -> POSTGRES_PASSWORD
openssl rand -hex 24   # -> LITELLM_MASTER_KEY (con el prefijo sk-)
nano .env              # pega los dos y tu ANTHROPIC_API_KEY
```

Revisa `config/litellm.yaml` y confirma que los identificadores de modelo siguen
vigentes en <https://docs.claude.com/en/docs/about-claude/models>.

## 2. Levantar

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f agent
```

El esquema SQL se aplica solo la primera vez que arranca Postgres (via
`/docker-entrypoint-initdb.d`). Si cambias el esquema despues, o lo migras a mano
o tiras el volumen con `docker compose down -v` (borra las conversaciones).

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
