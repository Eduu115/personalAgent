"""Proactividad: avisar de TRANSICIONES, no de estados.

Cuando algo pasa de bien a mal, un aviso. Mientras sigue mal, silencio. Cuando
vuelve a bien, otro aviso. Un sistema que avisa mas de una vez al dia de media
acaba silenciado, y un canal silenciado es peor que no tenerlo: crees que te
avisaria y ya no lo hace. Ante la duda, se calla.

Para que eso sea posible hace falta guardar el estado anterior, y vive en
Postgres (tabla `vigilancias`, migracion 004). Sin eso no hay transiciones,
solo umbrales que se repiten cada cinco minutos.

Dos cosas evitan el ruido, y las dos hacen falta:

  - **Histeresis por repeticion**: un estado nuevo no cuenta hasta verlo
    CONFIRMACIONES veces seguidas. Un pico de CPU de treinta segundos no es una
    incidencia.
  - **Umbrales distintos de subida y de bajada**: se avisa al 85% y se da por
    recuperado al 80%. Con un solo umbral, algo que oscila alrededor del limite
    manda un aviso por oscilacion.

**Esto no arregla nada, y no puede.** Las comprobaciones llaman al MCP por el
atajo de `herramientas.solo_lectura()`, que revienta con cualquier cosa que no
sea de nivel `read`. La proactividad detecta y avisa; el dia que quiera arreglar
algo, eso pasa por la cola de aprobaciones como todo lo demas.

Va a su propio topic de ntfy, `avisos`, y no al de `aprobaciones`: las
aprobaciones son lo unico que no se puede permitir silenciar, y compartiendo
canal, el dia que te hartes de los avisos silencias las dos cosas.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from . import db, herramientas, ntfy
from .config import MADRID, settings

log = logging.getLogger(__name__)

# Comprobaciones seguidas viendo lo mismo antes de dar un cambio por bueno. Con
# el job cada 5 minutos, son 15 minutos de confirmacion en cada sentido.
CONFIRMACIONES = 3


@dataclass(frozen=True)
class Medida:
    """Lo que ve una comprobacion ahora mismo.

    `estado` a None significa "ni una cosa ni la otra": es la banda entre los
    dos umbrales, donde lo correcto es no mover nada.
    """

    estado: str | None
    detalle: str
    accion: str = ""


@dataclass(frozen=True)
class Transicion:
    nombre: str
    estado: str
    detalle: str
    accion: str
    desde: datetime | None


def por_umbral(valor: float, sube: float, baja: float) -> str | None:
    """Histeresis de manual: se pasa a mal en `sube` y no se vuelve hasta `baja`."""
    if valor >= sube:
        return "mal"
    if valor <= baja:
        return "bien"
    return None


def decidir(
    medidas: dict[str, Medida], estados: dict[str, dict[str, Any]]
) -> tuple[list[Transicion], dict[str, tuple[str, str | None, int, str, bool]]]:
    """La parte que decide, sin tocar nada de fuera: (transiciones, que guardar).

    Separada a proposito de la que hace IO: las transiciones son lo unico que
    de verdad hay que probar, y asi se prueban con estado inventado y sin
    esperar a que el disco se llene.
    """
    transiciones: list[Transicion] = []
    guardar: dict[str, tuple[str, str | None, int, str, bool]] = {}

    for nombre, medida in medidas.items():
        fila = estados.get(nombre)
        # Lo que no se ha visto nunca se da por bueno: asi la primera vez que
        # algo esta mal sale un aviso, en vez de estrenarse en silencio.
        anterior = fila["estado"] if fila else "bien"
        candidato_previo = fila.get("candidato") if fila else None
        racha_previa = fila.get("racha", 0) if fila else 0

        if medida.estado is None or medida.estado == anterior:
            # Sigue como estaba (o esta en la banda de en medio): se olvida
            # cualquier cambio a medio confirmar.
            guardar[nombre] = (anterior, None, 0, medida.detalle, False)
            continue

        racha = racha_previa + 1 if candidato_previo == medida.estado else 1
        if racha < CONFIRMACIONES:
            guardar[nombre] = (anterior, medida.estado, racha, medida.detalle, False)
            continue

        transiciones.append(
            Transicion(
                nombre=nombre,
                estado=medida.estado,
                detalle=medida.detalle,
                accion=medida.accion,
                desde=fila["desde"] if fila else None,
            )
        )
        guardar[nombre] = (medida.estado, None, 0, medida.detalle, True)

    return transiciones, guardar


def cuanto(desde: datetime | None) -> str:
    if desde is None:
        return "desde que se mira"
    minutos = max(0, int((datetime.now(MADRID) - desde).total_seconds() // 60))
    if minutos < 1:
        return "menos de un minuto"
    if minutos < 60:
        return f"{minutos} min"
    if minutos < 60 * 48:
        return f"{minutos // 60} h"
    return f"{minutos // 1440} días"


async def avisar(t: Transicion) -> bool:
    """Un push por transicion. Con lo que ha cambiado, desde cuando y que mirar."""
    if t.estado == "mal":
        titulo = f"⚠️ {t.detalle}"
        cuerpo = t.accion or "Sin nada evidente que mirar."
        prioridad, etiquetas = 4, ["warning"]
    else:
        titulo = f"✅ Recuperado: {t.detalle}"
        cuerpo = f"Estuvo mal {cuanto(t.desde)}."
        prioridad, etiquetas = 3, ["white_check_mark"]
    return await ntfy.publicar(
        settings.ntfy_topic_avisos, titulo, cuerpo, prioridad=prioridad, etiquetas=etiquetas
    )


# ------------------------------------------------------------------ que se vigila


async def _del_host() -> dict[str, Medida]:
    """Disco y RAM del anfitrion, y los contenedores que no estan bien."""
    medidas: dict[str, Medida] = {}
    contenedores, anfitrion = await herramientas.solo_lectura("lab_status", "lab_host")

    m = anfitrion["memoria"]
    medidas["ram"] = Medida(
        por_umbral(m["usado_pct"], settings.umbral_ram, settings.umbral_ram_baja),
        f"RAM al {m['usado_pct']}%",
        f"Quedan {m.get('disponible_mib', 0)} MiB de {m['total_mib']}. "
        "Mira quien se la come: lab_stats, o la vista de Estado de la consola.",
    )
    for disco in anfitrion.get("discos", []):
        punto = disco["punto_montaje"]
        medidas[f"disco:{punto}"] = Medida(
            por_umbral(disco["usado_pct"], settings.umbral_disco, settings.umbral_disco_baja),
            f"Disco {punto} al {disco['usado_pct']}%",
            f"Quedan {disco['libre_gib']} GiB de {disco['total_gib']}. "
            "Lo de siempre: imagenes y logs de Docker (docker system df).",
        )

    for c in contenedores.get("contenedores", []):
        nombre = c["nombre"]
        reiniciando = "Restarting" in (c.get("detalle") or "")
        if c.get("salud") == "unhealthy":
            medidas[f"contenedor:{nombre}"] = Medida(
                "mal", f"{nombre} unhealthy",
                f"Su healthcheck lleva fallando. Mira sus logs: lab_logs {nombre}.")
        elif reiniciando:
            medidas[f"contenedor:{nombre}"] = Medida(
                "mal", f"{nombre} en bucle de reinicio",
                f"Arranca y se cae. lab_logs {nombre} deberia decir por que.")
        elif c.get("estado") == "running":
            medidas[f"contenedor:{nombre}"] = Medida("bien", f"{nombre} bien")
        # Un contenedor parado a proposito no se vigila: no se anade medida.
    return medidas


async def _del_helper() -> dict[str, Medida]:
    """Que el helper del host siga contestando. Solo si hay algo que dependa de el."""
    if not settings.equipos:
        return {}
    from . import acciones

    try:
        r = await acciones._al_helper("ping")
        bien = bool(r.get("ok"))
        motivo = "" if bien else str(r.get("error", ""))[:120]
    except Exception as exc:
        bien, motivo = False, f"{type(exc).__name__}: {exc}"[:120]
    return {
        "helper": Medida(
            "bien" if bien else "mal",
            "El helper del host responde" if bien else "El helper del host no responde",
            "" if bien else f"{motivo}. En el server: systemctl status puente-helper. "
            "Mientras esté así no se pueden actualizar stacks ni encender el PC.",
        )
    }


async def _del_briefing() -> dict[str, Medida]:
    """Que el briefing de la manana haya salido. Si se cae, hoy no te enteras."""
    if not settings.briefing_cron:
        return {}
    ahora = datetime.now(MADRID)
    # Se mira a partir de MARGEN despues de su hora: antes no es un fallo, es
    # que todavia no toca. La hora sale del mismo cron que lo programa.
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab(settings.briefing_cron, timezone=MADRID)
    tocaba = trigger.get_next_fire_time(None, ahora - timedelta(days=1))
    if tocaba is None or ahora < tocaba + timedelta(minutes=settings.briefing_margen_min):
        return {}
    salio = await db.hay_briefing_desde(tocaba)
    return {
        "briefing": Medida(
            "bien" if salio else "mal",
            "El briefing sale" if salio else f"No ha salido el briefing de las {tocaba:%H:%M}",
            "" if salio else "Lánzalo a mano con POST /api/briefing, o mira el log del agente. "
            "Hoy no tienes ni agenda ni correos revisados.",
        )
    }


async def _del_presupuesto() -> dict[str, Medida]:
    """El tope de gasto de LiteLLM.

    Es la degradacion silenciosa del mes: cuando se agota, el agente deja de
    responder y no dice por que. Dos avisos, al 80% y al 95%, cada uno con su
    propia vigilancia: asi el segundo no se pierde por estar ya "mal" del primero.
    """
    base = settings.litellm_base_url.rstrip("/").removesuffix("/v1")
    async with httpx.AsyncClient(timeout=10) as cliente:
        r = await cliente.get(
            f"{base}/global/spend",
            headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
        )
        r.raise_for_status()
        datos = r.json()
    tope = float(datos.get("max_budget") or 0)
    if tope <= 0:
        return {}   # sin tope no hay nada que agotar
    gastado = float(datos.get("spend") or 0)
    pct = round(gastado / tope * 100, 1)
    medidas = {}
    # El nombre NO lleva el porcentaje dentro: si lo llevara, cambiar el umbral
    # renombraria la vigilancia, la fila vieja se quedaria en "mal" para siempre
    # y la recuperacion no llegaria nunca. Paso probandolo.
    for etiqueta, aviso in (("aviso", settings.presupuesto_aviso),
                            ("critico", settings.presupuesto_critico)):
        medidas[f"presupuesto:{etiqueta}"] = Medida(
            # La bajada es 2 puntos por debajo: el gasto solo sube, asi que esto
            # solo se vuelve "bien" cuando el periodo se renueva.
            por_umbral(pct, aviso, aviso - 2),
            f"Presupuesto de la API al {pct}% ({gastado:.2f} de {tope:.0f} USD, aviso al {aviso:g}%)",
            "Cuando llegue al 100% el agente deja de responder y no lo dice. "
            "Sube max_budget en config/litellm.yaml o espera a que renueve el periodo.",
        )
    return medidas


async def _del_token_github() -> dict[str, Medida]:
    """Cuando caduca el token de GitHub. La fecha la publica github-mcp."""
    url = settings.mcp_servidores.get("github", "")
    if not url:
        return {}
    async with httpx.AsyncClient(timeout=10) as cliente:
        r = await cliente.get(url.replace("/mcp", "/healthz"))
        r.raise_for_status()
        datos = r.json()
    dias = datos.get("dias_para_caducar")
    if dias is None:
        return {}   # un token sin fecha no se puede vigilar; github-mcp ya avisa al arrancar
    return {
        "token:github": Medida(
            "mal" if dias <= settings.aviso_caducidad_dias else "bien",
            f"El token de GitHub caduca en {dias} días" if dias > 0
            else f"El token de GitHub caducó hace {-dias} días",
            f"Renuévalo en Settings → Developer settings → Fine-grained tokens y cambia "
            f"GITHUB_TOKEN en el .env. Caduca el {str(datos.get('token_caduca'))[:10]}.",
        )
    }


COMPROBACIONES = (_del_host, _del_helper, _del_briefing, _del_presupuesto, _del_token_github)


async def vigilar() -> dict[str, Any]:
    """Una pasada: mirar, comparar con lo de antes, avisar solo de lo que cambia."""
    medidas: dict[str, Medida] = {}
    fallos: list[str] = []
    for comprobacion in COMPROBACIONES:
        try:
            medidas |= await comprobacion()
        except Exception as exc:
            # Una comprobacion que no se puede hacer no cambia ningun estado: no
            # saber si el disco esta lleno no es lo mismo que saber que no lo esta.
            fallos.append(f"{comprobacion.__name__}: {type(exc).__name__}")
            log.warning("vigilancia: %s ha fallado: %s", comprobacion.__name__, exc)

    estados = await db.vigilancias()
    transiciones, guardar = decidir(medidas, estados)

    for nombre, (estado, candidato, racha, detalle, cambia) in guardar.items():
        await db.vigilancia_guardar(nombre, estado, candidato, racha, detalle, cambia)
    # Lo que ya no existe deja de vigilarse, y sin aviso: quitar un contenedor o
    # desmontar un disco a proposito no es una incidencia. Solo estos dos: el
    # resto de vigilancias son fijas, y que falten significa que no se han
    # podido mirar, que no es lo mismo que que hayan desaparecido.
    for nombre in estados.keys() - guardar.keys():
        if nombre.startswith(("contenedor:", "disco:")):
            await db.vigilancia_olvidar(nombre)

    for t in transiciones:
        await avisar(t)
        log.info("vigilancia: %s -> %s (%s)", t.nombre, t.estado, t.detalle)

    return {
        "vigiladas": len(medidas),
        "transiciones": [f"{t.nombre}:{t.estado}" for t in transiciones],
        "sin_comprobar": fallos,
    }
