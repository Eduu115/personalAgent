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
openssl rand -hex 24   # -> REDIS_PASSWORD
nano .env              # pega los tres y tu ANTHROPIC_API_KEY
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

Si la pregunta necesita herramientas ("¿cómo está el server?") aparecen ademas
parejas de `event: tool` con `estado` `inicio` y `fin` (nombre, argumentos,
`resultado` y `duracion_ms`). Si el modelo agota las
`MAX_RONDAS_HERRAMIENTAS` (8) sin terminar, llega un `event: limite` y la
respuesta lo dice.

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
| `lab_reiniciar` | **Escritura.** Reinicia un contenedor del asistente, y solo esos. Pasa por la cola de aprobaciones |

El socket de Docker solo lo ve `docker-socket-proxy`, un HAProxy con lista blanca
en `config/haproxy.cfg`: solo GET y solo `/_ping`, `/info`, `/version`,
`/containers/json`, `/containers/{id}/logs` y `/containers/{id}/stats`. Todo lo
demas da 403, incluido `/containers/{id}/json`, que devolveria las variables de
entorno (los secretos) de todos los contenedores. `deploy.sh` lo verifica en cada
despliegue: un POST, `json`, `archive` y `export` tienen que dar 403, y el
despliegue falla si alguno pasa.

Lo unico que se concede fuera de GET es `POST /containers/<nombre>/restart`, con
los seis contenedores del asistente escritos uno a uno en el propio regex: ni
`puente-postgres`, ni el socket-proxy, ni nada de APIArena. `deploy.sh` lo
comprueba en cada despliegue, y la comprobacion que importa es que
`apiarena-postgres` da 403.

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

## google-mcp

Correo y calendario de Edu, solo lectura. Sin OAuth ni Google Cloud: IMAP con
contrasena de aplicacion y la URL secreta del calendario en formato iCal (el
porque, en `CLAUDE.md`).

| Herramienta | Que devuelve |
|---|---|
| `mail_buscar` | Correos que casan con una busqueda de Gmail (`is:unread newer_than:2d`, `from:banco`...): id, remitente, asunto, fecha, etiquetas, si esta leido y un snippet. Nunca el cuerpo |
| `mail_leer` | Un correo por su id: cabeceras, adjuntos y el texto (HTML pasado a texto plano), maximo 4.000 caracteres |
| `cal_agenda` | Eventos de hoy y los proximos dias de todos los calendarios, en hora de Madrid, con los solapes |
| `mail_borrador` | **Escritura.** Guarda un borrador en Gmail (IMAP APPEND). No envia: no hay herramienta de enviar |

Se configura en `.env` con `GMAIL_USUARIO`, `GMAIL_APP_PASSWORD` y
`GOOGLE_ICAL_URLS` (como conseguir cada una, en `.env.example`). Sin ellas el
servidor arranca igual y las herramientas devuelven un error claro.

Lo que no hace, a proposito: ninguna herramienta escribe. El buzon se abre en
modo solo lectura y todo se pide con `BODY.PEEK`, asi que leer un correo no lo
marca como leido. Asunto, snippet y cuerpo pasan por la redaccion de secretos y
`mail_leer` avisa en su propio resultado de que el texto lo ha escrito un
tercero.

Desde Claude Code, igual que homelab-mcp:

```bash
claude mcp add --transport http google http://127.0.0.1:8422/mcp
```

Comprobaciones sin red ni credenciales (parseo de correos feos, cabeceras MIME,
respuestas IMAP, iCal con recurrentes y excepciones, redaccion):

```bash
docker compose exec google-mcp python -m app.pruebas
```

## Aprobaciones: las herramientas que escriben

`mail_borrador` y `lab_reiniciar` no se ejecutan solas. Cuando el modelo pide
una, el agente la encola y manda un push con dos botones; hasta que lo toques,
esa conversacion no sigue.

| Endpoint | Que hace |
|---|---|
| `POST /api/aprobaciones/{id}/aprobar?n=<nonce>` | Ejecuta y retoma la conversacion. Responde 200 al momento |
| `POST /api/aprobaciones/{id}/rechazar?n=<nonce>` | No ejecuta nada y se lo dice al modelo |

El nonce va en la URL del boton y es de un solo uso: al resolver, la fila deja
de estar `pending` y esa URL ya no vale (409). Un nonce que no cuadra, 403. Si
en 15 minutos no lo tocas, caduca sola y la conversacion se desbloquea.

Al terminar llega un segundo push con lo que ha dicho el modelo: sin el,
apruebas y te quedas sin saber como acabo.

Para verlo entero:

```bash
docker compose exec postgres psql -U puente -d puente \
  -c "select id, tool_name, risk, status, resolved_by, arguments from tool_calls order by id desc limit 10;"
```

Con `READ_ONLY=true` las escrituras ni se le ofrecen al modelo. El briefing y
cualquier otra tarea programada tampoco pueden encolarlas: solo lectura.

## Migraciones de la base de datos

`db/init/` solo se aplica la primera vez que arranca Postgres. Los cambios de
esquema posteriores van en `db/migrations/NNN_*.sql`, y los aplica `deploy.sh`
**antes** de levantar el agente: cada uno en una transaccion, apuntado en
`schema_migrations`, y si uno falla el despliegue se para con el stack viejo
sirviendo. Nunca se aplican desde el arranque del agente.

```bash
docker compose exec postgres psql -U puente -d puente -c "select * from schema_migrations;"
```

## Briefing de la manana y avisos (ntfy)

Cada dia a las 7:30 (hora de Madrid) el agente prepara un briefing: agenda de
hoy, correos que importan y una linea sobre el server si algo va raro. Lo deja
en una conversacion nueva, "Briefing del sabado 20/09", para seguir tirando del
hilo desde el chat, y avisa al movil por ntfy con un enlace a esa conversacion.
Si el briefing falla, llega un aviso con prioridad alta en su lugar.

Usa el mismo bucle de herramientas que el chat, con `origin="schedule"`: solo
herramientas de lectura, tambien cuando llegue la cola de aprobaciones. A las
7:30 no hay nadie delante para aprobar nada.

Si no ha podido consultar algo, lo dice en la primera linea ("No he podido
consultar el correo (google-mcp no responde)"), tambien en la notificacion. Si
el agente estaba parado a las 7:30, el briefing sale al arrancar si no han
pasado 2 horas, con la hora a la que tocaba.

Configuracion en `.env` (detalles en `.env.example`):

| Variable | Para que |
|---|---|
| `BRIEFING_CRON` | Cuando, en cron de 5 campos y hora de Madrid. Vacio, apagado |
| `NTFY_TOKEN_PUBLICAR` | Con el que publica el agente. Solo escribe en el topic `briefing` |
| `NTFY_TOKEN_SUSCRIBIR` | Con el que se suscribe el movil. Solo lee |
| `AGENTE_URL_PUBLICA` | Base del enlace de la notificacion |

Los tokens se generan con `docker run --rm binwiederhier/ntfy:v2.28.0 token generate`.
Mientras no haya PWA, el enlace abre el JSON de `/api/conversations/<id>`.

Lanzarlo a mano, sin esperar al cron (espera a que termine y devuelve el texto):

```bash
curl -s -X POST localhost:8420/api/briefing
```

### Exponer ntfy al tailnet

ntfy es propio, nada de ntfy.sh: el briefing lleva asuntos de correos. Escucha en
`127.0.0.1:8423` y lo saca al tailnet `tailscale serve`, como al agente, en el
8444 (el 443 lo tiene nginx-proxy, ver `CLAUDE.md`):

```bash
sudo tailscale serve --bg --https=8444 http://127.0.0.1:8423
```

Todo esta cerrado por defecto (`deny-all`) y la web de ntfy, apagada. Esa
misma URL, sin barra final, va en `NTFY_BASE_URL`: la necesita el iPhone (abajo).

### Los clientes: iPhone y tablet Android

Los dos cuentan: el iPhone es el movil del dia a dia y la tablet Android sera la
consola de casa (F3). Cada uno recibe los avisos de una forma distinta.

**Como entra la app al servidor.** De una de estas dos formas, **y solo de estas
dos**, en cualquiera de las dos apps:

| Forma | Usuario en la app | Contrasena en la app |
|---|---|---|
| Token | **vacio** | el `NTFY_TOKEN_SUSCRIBIR` |
| Usuario y contrasena | `edu` | la contrasena cuyo hash esta en `NTFY_PASS_HASH_EDU` |

Lo que **no** funciona, y costo tres intentos averiguarlo: usuario `edu` con el
token de contrasena. La app manda Basic auth, y ntfy solo acepta un token como
contrasena si el usuario va vacio. Si la app no deja el usuario vacio, usa la
segunda forma. Comprobado contra el servidor:

| Autenticacion | Resultado |
|---|---|
| `Authorization: Bearer <token>` | 200 |
| Basic `(vacio):<token>` | 200 |
| Basic `edu:<contrasena>` | 200 |
| Basic `edu:<token>` | **401** |

Las dos formas dan lo mismo: `edu` solo puede leer el topic `briefing`.

**La URL del servidor en las apps** es `NTFY_BASE_URL`, exactamente: por ejemplo
`https://puente.<tailnet>.ts.net:8444`, sin barra final. En el iPhone no es un
detalle, ver abajo.

#### iPhone

iOS no deja a una app mantener una conexion abierta, asi que un ntfy propio no
puede avisar al iPhone directamente: sin mas, las notificaciones tardan horas.
Por eso ntfy tiene `upstream-base-url` apuntando a ntfy.sh:

1. El agente publica el briefing en nuestro ntfy.
2. Nuestro ntfy manda a ntfy.sh **solo** el id del mensaje y el SHA256 de la URL
   del topic (`https://puente...:8444/briefing`). Ni el titulo ni el texto.
3. ntfy.sh despierta la app por APNs, y la app se baja el contenido de
   **nuestro** servidor.

El briefing no sale del tailnet, que era el motivo de autohospedarlo. Dos
consecuencias:

- **Tailscale tiene que estar conectado en el iPhone** cuando llega el aviso: el
  contenido se descarga de nuestro servidor en ese momento. Con Tailscale
  apagado llega el aviso de ntfy.sh, pero la app no puede bajarse el mensaje.
  Deja activada la VPN a demanda de Tailscale.
- **`NTFY_BASE_URL` tiene que ser exactamente la URL que pones en la app.** El
  iPhone se apunta en ntfy.sh con el SHA256 de su URL; si no coincide con la
  del servidor, nunca le llega nada.

En la app ntfy de la App Store: **Ajustes > Usuarios**, anade el servidor con una
de las dos formas de arriba. Despues **+**, tema `briefing`, *Usar otro servidor*
y la misma URL.

#### Tablet Android

La tablet no pasa por ntfy.sh: usa **entrega instantanea**, una conexion propia y
permanente contra nuestro servidor. Como vive enchufada, la bateria da igual.

1. **Ajustes > Usuarios > Anadir usuario**, con una de las dos formas de arriba.
2. **+ > Suscribirse a un tema**, tema `briefing`, *Usar otro servidor* y la URL.
3. Activa **Entrega instantanea** en esa suscripcion.
4. Quita **ntfy y Tailscale** de la optimizacion de bateria de Android (Ajustes >
   Aplicaciones > ntfy > Bateria > Sin restricciones, y lo mismo con Tailscale).
   Si no, el sistema les corta la conexion y los avisos llegan tarde o no llegan.

### Las contrasenas de ntfy

Los usuarios `puente` y `edu` llevan el hash bcrypt de su contrasena en `.env`
(`NTFY_PASS_HASH_PUENTE` y `NTFY_PASS_HASH_EDU`). La de `edu` es la de la app si
no usas el token. La de `puente` no la usa nadie, porque el agente publica con
su token: ponle una aleatoria y olvidala. Cada hash sale de:

```bash
docker run --rm -it binwiederhier/ntfy:v2.28.0 user hash
```

**Cuidado con los `$` del hash** (`$2a$10$...`): Compose los toma por variables y
corta el hash **sin avisar**. ntfy recibe otro hash y la contrasena no entra.

- En `.env`, el hash va **entre comillas simples**:
  `NTFY_PASS_HASH_EDU='$2a$10$...'`. Sin comillas o con comillas dobles, un trozo
  como `$CSmhGlHGuuIUf3y` desaparece como si fuera una variable vacia.
  `deploy.sh` falla si no estan las comillas simples.
- Si alguna vez escribes un hash **directamente en `docker-compose.yml`**, cada
  `$` va doble: `$$2a$$10$$...`.

Para ver lo que le llega de verdad a ntfy:

```bash
docker compose exec ntfy sh -c 'echo "$NTFY_AUTH_USERS"'
```

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
`.gitignore`: en el server no se copia y no se aplica. El override tambien apaga
el briefing de las 7:30: desde un portatil gastaria tokens cada manana para
avisar por un ntfy que no mira nadie. A mano sigue funcionando.

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

# el audit log: cada llamada a herramienta, rechazos incluidos
docker compose exec postgres psql -U puente -d puente \
  -c "select requested_at, tool_name, risk, status from tool_calls order by 1 desc limit 20;"
```

## Kill switch

```bash
sed -i 's/^READ_ONLY=.*/READ_ONLY=true/' .env && docker compose up -d agent
```

Con `READ_ONLY=true`, `mail_borrador` y `lab_reiniciar` ni se le ofrecen al
modelo, y si las pidiera de todas formas se rechazan y queda la fila en
`tool_calls`. Las de lectura siguen funcionando.

## Que viene ahora

Lee `CLAUDE.md` — tiene el contexto completo, las reglas que no se negocian y la
hoja de ruta. La F1 empieza por `homelab-mcp`, que es la herramienta mas divertida
y la unica que hay que escribir desde cero.
