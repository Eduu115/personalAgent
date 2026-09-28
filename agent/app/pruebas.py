"""Comprobaciones del agente que no necesitan ni red ni base de datos.

    docker compose run --rm --no-deps agent python -m app.pruebas

Las corre deploy.sh dentro de la imagen recien construida, ANTES de aplicar
migraciones: si el codigo nuevo no arranca, la base no se toca.

Estan por algo concreto: la F2 se llevo por delante briefing.por_cron en un
refactor, main.py seguia llamandola y el agente entro en bucle de reinicio en el
server. En el portatil no salto porque el override de desarrollo apaga el
briefing y esa rama del lifespan no se ejecutaba nunca.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import pkgutil
import re
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import main
from .config import settings

AQUI = Path(__file__).parent
PAQUETE = __package__ or "app"


def referencias() -> None:
    """Ningun `modulo.atributo` del paquete apunta a algo que ya no existe.

    Es lo que fallo: una funcion borrada en un refactor y una llamada olvidada.
    Un AttributeError asi solo aparece cuando se ejecuta esa linea, que puede
    ser a las 7:30 de la manana en el server.
    """
    modulos = {m.name for m in pkgutil.iter_modules([str(AQUI)])}
    colgando = []
    for fichero in sorted(AQUI.rglob("*.py")):
        arbol = ast.parse(fichero.read_text(), str(fichero))
        # Nombres que en este fichero son modulos nuestros: from . import x, y
        locales = {
            alias.asname or alias.name
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.ImportFrom) and nodo.level and nodo.module is None
            for alias in nodo.names
            if alias.name in modulos
        }
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Attribute)
                and isinstance(nodo.value, ast.Name)
                and nodo.value.id in locales
                and not hasattr(importlib.import_module(f".{nodo.value.id}", PAQUETE), nodo.attr)
            ):
                colgando.append(f"{fichero.relative_to(AQUI)}:{nodo.lineno} -> {nodo.value.id}.{nodo.attr}")
    assert not colgando, "referencias que ya no existen:\n  " + "\n  ".join(colgando)
    print(f"OK referencias: {len(modulos)} modulos, ningun atributo colgando")


async def arranque() -> None:
    """El lifespan entero, con los jobs del planificador registrados.

    Con BRIEFING_CRON puesto, que es la rama que se rompio: en desarrollo va
    vacia y no se ejecuta nunca.
    """
    from . import briefing, db

    registrados: list[tuple[str | None, object]] = []

    class Espia(AsyncIOScheduler):
        """Apunta los jobs y arranca en pausa: se programan pero no se ejecutan."""

        def add_job(self, func, *args, **kwargs):  # type: ignore[override]
            registrados.append((kwargs.get("id"), func))
            return super().add_job(func, *args, **kwargs)

        def start(self, *args, **kwargs):  # type: ignore[override]
            return super().start(paused=True)

    async def nada(*_args, **_kwargs):
        return None

    original = (main.AsyncIOScheduler, db.open_pool, db.close_pool, db.hay_briefing_desde, briefing.lanzar)
    main.AsyncIOScheduler = Espia
    db.open_pool = nada
    db.close_pool = nada
    briefing.lanzar = nada  # por si toca recuperar uno: aqui no se llama al modelo
    try:
        # El cron de la recuperacion tiene que tener un disparo dentro de la
        # ventana de 2 h, o no hay nada que recuperar y la prueba dependeria de
        # la hora a la que se ejecute.
        for cron, hecho_ya, esperados in (
            ("30 7 * * *", True, {"briefing", "caducar-aprobaciones", "purgar-audit", "vigilancia"}),
            # Sin briefing reciente: ademas se programa el que se perdio.
            ("*/5 * * * *", False, {"briefing", "caducar-aprobaciones", "purgar-audit", "vigilancia", "briefing-recuperado"}),
        ):
            registrados.clear()
            settings.briefing_cron = cron
            db.hay_briefing_desde = lambda *_a, **_k: asyncio.sleep(0, result=hecho_ya)
            async with main.lifespan(main.app):
                pass
            ids = {i for i, _ in registrados}
            assert ids == esperados, f"jobs registrados {ids}, esperados {esperados}"
            for nombre, funcion in registrados:
                assert callable(funcion), f"el job {nombre} no es llamable"
            print(f"OK lifespan (cron '{cron}', briefing hecho={hecho_ya}): {', '.join(sorted(ids))}")

        # Y sin briefing, que es como corre el portatil.
        registrados.clear()
        settings.briefing_cron = ""
        async with main.lifespan(main.app):
            pass
        assert {i for i, _ in registrados} == {"caducar-aprobaciones", "purgar-audit", "vigilancia"}
        print("OK lifespan sin briefing: caducar-aprobaciones, purgar-audit, vigilancia")
    finally:
        (main.AsyncIOScheduler, db.open_pool, db.close_pool, db.hay_briefing_desde, briefing.lanzar) = original


def identidad() -> None:
    """El prompt lleva quien es el dueno, y sale de configuracion.

    Con valores inventados: si algo de esto estuviera escrito en el codigo, no
    cambiaria al cambiar la configuracion y la prueba lo veria.
    """
    original = (settings.dueno, settings.gmail_usuario, settings.zona_horaria)
    try:
        settings.dueno, settings.gmail_usuario, settings.zona_horaria = "Prueba", "p@ejemplo.org", "America/Lima"
        prompt = settings.prompt
        for dato in ("Prueba", "p@ejemplo.org", "America/Lima"):
            assert dato in prompt, f"el prompt no lleva {dato}"
        assert "instrucciones" in prompt and "herramienta" in prompt, "falta decir que es identidad, no datos"
        # Sin correo configurado no se lo inventa: dice que lo pregunte.
        settings.gmail_usuario = ""
        assert "pregúntasela" in settings.prompt and "p@ejemplo.org" not in settings.prompt
    finally:
        (settings.dueno, settings.gmail_usuario, settings.zona_horaria) = original
    print(f"OK identidad en el prompt: {settings.dueno}, {settings.gmail_usuario or '(sin correo)'}, {settings.zona_horaria}")


async def catalogo_propio() -> None:
    """El catalogo sale sin servidores MCP: las herramientas del propio agente."""
    from . import herramientas

    catalogo = await herramientas.ofrecidas({})
    nombres = {t["function"]["name"] for t in catalogo.tools}
    assert nombres == herramientas.PROPIAS, f"sin MCP tendrian que quedar {herramientas.PROPIAS}, hay {nombres}"
    assert all(catalogo.ruta[n] == "agente" for n in nombres), catalogo.ruta
    assert not catalogo.conflictos
    print(f"OK catalogo sin MCP: {', '.join(sorted(nombres))}")


async def candados_memoria() -> None:
    """Escribir en memoria: solo en turnos del usuario y sin contenido externo.

    Un hecho guardado entra en el prompt todos los dias: si un correo pudiera
    dejar uno, seria una inyeccion de prompt con efecto permanente.
    """
    from . import db, herramientas, llm, memoria

    auditado: list[tuple[str, str, str | None]] = []

    async def log_falso(nombre, **kw):
        auditado.append((nombre, kw["status"], kw.get("error")))
        return 1

    async def no_guardar(*_a, **_k):
        raise AssertionError("no tendria que haber llegado a guardar")

    original = (db.log_tool_call, memoria.guardar, memoria.olvidar)
    db.log_tool_call, memoria.guardar, memoria.olvidar = log_falso, no_guardar, no_guardar
    try:
        ruta = {n: "agente" for n in herramientas.PROPIAS}
        guardar = llm.Llamada("m1", "memoria_guardar", "{}", {"texto": "prueba", "ambito": "perfil"})
        olvidar = llm.Llamada("m2", "memoria_olvidar", "{}", {"id": 1})
        casos = [
            ("el briefing (origin=schedule)", guardar, {"origin": "schedule"}),
            ("un turno que ya leyo algo de fuera", guardar, {"contenido_externo": True}),
            ("olvidar desde el briefing", olvidar, {"origin": "schedule"}),
            ("olvidar tras leer algo de fuera", olvidar, {"contenido_externo": True}),
        ]
        for titulo, llamada, extra in casos:
            r = await herramientas.ejecutar(
                {}, ruta, llamada, conversation_id=None, model="prueba", **extra
            )
            assert r.status == "rejected", f"{titulo}: status={r.status}"
            assert r.pendiente is None, f"{titulo}: no puede acabar en la cola"
        assert [e[1] for e in auditado] == ["rejected"] * len(casos), auditado
        # Y que se le ofrecen al modelo solo cuando toca.
        assert not herramientas.permitida("memoria_guardar", "schedule")
        assert herramientas.permitida("memoria_guardar", "user")
        print(f"OK candados de memoria: {len(casos)} intentos rechazados y auditados")
    finally:
        (db.log_tool_call, memoria.guardar, memoria.olvidar) = original


def query_del_briefing() -> None:
    """La consulta de correo del briefing sale de configuracion, no del prompt."""
    from . import briefing
    from .config import QUERY_BRIEFING, settings

    assert "-category:promotions" in QUERY_BRIEFING, "el filtro de publicidad no esta en la de por defecto"
    original = settings.briefing_query
    try:
        assert settings.query_briefing == QUERY_BRIEFING, "sin BRIEFING_QUERY tiene que valer la de por defecto"
        settings.briefing_query = "in:inbox otra-cosa"
        prompt = briefing.PROMPT_PLANTILLA.format(
            query=settings.query_briefing, pr_dias=settings.briefing_pr_dias
        )
        assert "in:inbox otra-cosa" in prompt and QUERY_BRIEFING not in prompt, "el prompt no coge la configurada"
        # Y que el briefing pida SOLO las PRs que reclaman algo. Son cuatro
        # repos: un listado de todas las abiertas cada manana se deja de leer a
        # los tres dias, que es como se pierde un briefing.
        assert "dev_prs" in prompt and f"{settings.briefing_pr_dias} días" in prompt, prompt
        for exigencia in (
            "SOLO las que piden algo",            # el filtro
            "va bien NO se menciona",             # una abierta ayer y sana, fuera
            "esta sección no sale",               # sin nada que reclamar, no hay seccion
            '"sin checks" no es "pasando"',       # y sin CI no se da por bueno
        ):
            assert exigencia in prompt, f"al prompt del briefing le falta: {exigencia}"
    finally:
        settings.briefing_query = original
    print(f"OK consulta del briefing desde configuracion: {settings.query_briefing}")
    print(f"OK el briefing pide solo las PRs que reclaman algo (paradas >{settings.briefing_pr_dias} dias)")


async def atajo_del_estado() -> None:
    """El atajo directo al MCP solo deja pasar herramientas de lectura.

    Lo usan la consola (tiles cada 15 s) y la vigilancia (disco y contenedores
    cada 5 min), los dos saltandose ejecutar(): ni audit log, ni kill switch, ni
    origin. El candado es este, asi que se prueba aqui y no en cada usuario.
    """
    from . import herramientas

    for nombre in ("lab_reiniciar", "lab_update_stack", "mail_borrador", "memoria_guardar", "no_existe"):
        try:
            await herramientas._leer({}, {nombre: "homelab"}, nombre)
        except RuntimeError as exc:
            assert "no es de lectura" in str(exc), (nombre, exc)
        else:
            raise AssertionError(f"'{nombre}' ha pasado por el atajo")
    # Y una de lectura si pasa el candado: falla despues, al buscar la sesion.
    assert herramientas.RIESGO["lab_status"] == "read"
    try:
        await herramientas._leer({}, {"lab_status": "homelab"}, "lab_status")
    except KeyError:
        pass
    else:
        raise AssertionError("sin sesiones tendria que haber fallado al enrutar")
    print("OK atajo directo al MCP: solo herramientas de lectura")


def consola() -> None:
    """La consola se sirve y NO se ha comido las rutas de la API.

    Montar los estaticos en "/" antes de /healthz deja el contenedor unhealthy
    para siempre: paso al escribirla.
    """
    from fastapi.testclient import TestClient

    cliente = TestClient(main.app)
    assert cliente.get("/healthz").status_code == 200, "el montaje de la consola tapa /healthz"
    pagina = cliente.get("/")
    assert pagina.status_code == 200 and "Puente de mando" in pagina.text, "no se sirve la consola"
    for fichero in ("/manifest.webmanifest", "/sw.js", "/icono-192.png", "/icono-512.png"):
        assert cliente.get(fichero).status_code == 200, f"falta {fichero}"
    ultima = main.app.routes[-1]
    assert getattr(ultima, "name", None) == "consola", (
        f"la ultima ruta es {ultima!r}: el montaje de la consola tiene que ir el ultimo o tapa la API")
    print("OK consola: pagina, manifest, service worker e iconos; /healthz intacto")


def consola_no_llama_al_modelo() -> None:
    """Ningun refresco automatico de la consola llama al modelo.

    La tablet esta encendida todo el dia y en ambient se repinta sola: un
    setInterval que acabe tocando /api/chat o /api/briefing es gasto de API sin
    que nadie haya pedido nada. Se mira el cierre transitivo, no la llamada
    directa: vale igual que la que pague sea una funcion tres saltos mas abajo.
    """
    fuente = (AQUI.parent / "consola" / "index.html").read_text()
    cuerpos = dict(re.findall(r"^(?:async )?function (\w+)\([^)]*\) \{\n(.*?)^\}", fuente, re.S | re.M))
    assert len(cuerpos) > 12, f"el parser de funciones solo ha visto {len(cuerpos)}: se ha quedado corto"

    # Las que cuestan dinero. /api/briefings (en plural) solo lee la tabla.
    caras = {n for n, cuerpo in cuerpos.items() if re.search(r"/api/(chat|briefing)(?!s)", cuerpo)}
    assert caras == {"enviar", "generarBriefing"}, f"llaman al modelo: {sorted(caras)}"
    sueltas = re.findall(r"/api/(?:chat|briefing)(?!s)", fuente)
    assert len(sueltas) == 2, f"hay {len(sueltas)} usos de /api/chat o /api/briefing, y solo 2 estan en una funcion"

    # De donde arranca solo: la tabla de refrescos y cualquier temporizador.
    tabla = re.search(r"const REFRESCOS = \{(.*?)\n\};", fuente, re.S)
    assert tabla, "falta la tabla REFRESCOS: sin ella esto no comprueba nada"
    raices = set(re.findall(r"\[(\w+),", tabla.group(1)))
    assert {"cargarAprobaciones", "cargarEstado", "cargarAgenda", "cargarBriefings"} <= raices, raices
    for linea in re.findall(r"set(?:Interval|Timeout)\((.*)$", fuente, re.M):
        raices |= set(re.findall(r"\w+", linea))   # tambien lo que haya en una flecha en linea

    alcanzables: set[str] = set()
    por_ver = [r for r in raices if r in cuerpos]
    while por_ver:
        nombre = por_ver.pop()
        if nombre in alcanzables:
            continue
        alcanzables.add(nombre)
        por_ver += [m for m in re.findall(r"(\w+)\(", cuerpos[nombre]) if m in cuerpos]

    culpables = alcanzables & caras
    assert not culpables, f"un refresco automatico acaba llamando al modelo: {sorted(culpables)}"
    print(f"OK consola: {len(alcanzables)} funciones se refrescan solas y ninguna llama al modelo")


def wol_no_es_herramienta() -> None:
    """Encender el PC no es una herramienta. Es la mitad del diseno, asi que se mira.

    Si algun dia aparece en el mapa de riesgo o entre las propias del agente, el
    modelo la vera en su catalogo y bastara con que un correo le convenza. El
    boton de la consola tiene que ser el unico camino. deploy.sh comprueba
    ademas lo que anuncian los servidores MCP de verdad.
    """
    from . import acciones, herramientas

    prohibidas = ("despertar", "wol", "encender", "wake")
    ofrecibles = set(herramientas.RIESGO) | herramientas.PROPIAS
    culpables = [n for n in ofrecibles if any(p in n.lower() for p in prohibidas)]
    assert not culpables, f"esto no puede ser una herramienta del modelo: {culpables}"

    original = settings.equipos_despertables
    try:
        settings.equipos_despertables = "sobremesa"
        ids = set(acciones.catalogo())
        assert ids == {"despertar:sobremesa"}, ids
        assert not (ids & ofrecibles), "una accion rapida tambien es herramienta: elige una"
    finally:
        settings.equipos_despertables = original
    print(f"OK encender no es herramienta: {len(ofrecibles)} ofrecibles y ninguna despierta nada")


async def candado_de_acciones() -> None:
    """Las acciones rapidas: catalogo cerrado, READ_ONLY las para, todas auditadas.

    No pasan por la cola de aprobaciones a proposito, asi que estas son las
    unicas barreras que les quedan y no pueden fallar en silencio.
    """
    from . import acciones, db

    auditado: list[tuple[str, str, str]] = []
    pedido: list[tuple[str, dict]] = []

    async def log_falso(nombre, **kw):
        auditado.append((nombre, kw["status"], kw["origin"]))
        return 1

    async def helper_falso(op, **kw):
        pedido.append((op, kw))
        return {"ok": True, "equipo": kw.get("equipo"), "aviso": "Paquete enviado."}

    async def no_llamar(*_a, **_k):
        raise AssertionError("con READ_ONLY no puede llegar a hablar con el helper")

    original = (db.log_tool_call, acciones._al_helper, settings.read_only, settings.equipos_despertables)
    db.log_tool_call = log_falso
    try:
        settings.equipos_despertables = "sobremesa, portatil"
        assert set(acciones.catalogo()) == {"despertar:sobremesa", "despertar:portatil"}

        # Con el kill switch puesto no se ejecuta, pero queda en el audit log.
        settings.read_only = True
        acciones._al_helper = no_llamar
        r = await acciones.ejecutar("despertar:sobremesa")
        assert not r["ok"] and "solo lectura" in r["error"], r
        assert auditado == [("despertar:sobremesa", "rejected", "consola")], auditado

        # Sin el, se ejecuta al momento: nada de cola, nada de nonce.
        settings.read_only = False
        acciones._al_helper = helper_falso
        r = await acciones.ejecutar("despertar:portatil")
        assert r["ok"] and pedido == [("despertar", {"equipo": "portatil"})], (r, pedido)
        assert auditado[-1] == ("despertar:portatil", "executed", "consola"), auditado

        # Y lo que no este en el catalogo no existe, venga como venga.
        for fuera in ("despertar:otro", "actualizar:puente", "", "despertar:../../x", "lab_reiniciar"):
            try:
                await acciones.ejecutar(fuera)
            except KeyError:
                continue
            raise AssertionError(f"'{fuera}' no esta en el catalogo y se ha ejecutado")
        assert len(auditado) == 2, f"una accion inexistente no se audita: {auditado}"

        settings.equipos_despertables = ""
        assert acciones.catalogo() == {}, "sin EQUIPOS_DESPERTABLES no puede haber ninguna accion"
        print("OK acciones rapidas: catalogo cerrado, READ_ONLY las para y las dos quedan auditadas")
    finally:
        (db.log_tool_call, acciones._al_helper, settings.read_only, settings.equipos_despertables) = original


def github_solo_lee() -> None:
    """Ninguna herramienta de github puede escribir, ni existe apagada.

    El mapa de riesgo es lo que el agente esta dispuesto a ejecutar: si algun
    dia alguien mete ahi un dev_merge, esto lo ve antes que el server. Lo que
    ANUNCIA el servidor lo comprueba deploy.sh, que es la otra mitad.
    """
    from . import herramientas

    dev = {n: r for n, r in herramientas.RIESGO.items() if n.startswith("dev_")}
    assert dev, "no hay ninguna herramienta de github en el mapa de riesgo"
    assert set(dev.values()) == {"read"}, f"esto no es de lectura: {dev}"
    prohibidas = ("merge", "aprob", "approve", "cerrar", "close", "comenta", "comment", "review")
    culpables = [n for n in herramientas.RIESGO if any(p in n.lower() for p in prohibidas)]
    assert not culpables, f"herramientas que actuan sobre PRs: {culpables}"
    print(f"OK github: {', '.join(sorted(dev))}, las tres read y ninguna que escriba")


def transiciones_de_vigilancia() -> None:
    """bien->mal, mal->mal->mal (silencio) y mal->bien, con estado inventado.

    El del medio es el que importa: es el que garantiza que algo roto durante
    tres dias no te manda 864 avisos. Sin base de datos y sin esperar a que el
    disco se llene: `decidir()` es una funcion pura y por eso existe.
    """
    from datetime import datetime, timedelta

    from . import vigilancia
    from .config import MADRID

    def correr(secuencia, estado_inicial=None):
        """Pasa una secuencia de estados por el motor y devuelve cuando avisa."""
        estados = dict(estado_inicial or {})
        avisos = []
        for paso, estado in enumerate(secuencia, 1):
            medidas = {"disco:/": vigilancia.Medida(estado, f"Disco / paso {paso}")}
            trans, guardar = vigilancia.decidir(medidas, estados)
            avisos += [(paso, t.estado) for t in trans]
            for nombre, (est, cand, racha, det, cambia) in guardar.items():
                fila = estados.setdefault(nombre, {"desde": datetime.now(MADRID)})
                fila.update(estado=est, candidato=cand, racha=racha, detalle=det)
                if cambia:
                    fila["desde"] = datetime.now(MADRID)
        return avisos, estados

    n = vigilancia.CONFIRMACIONES
    assert n >= 2, "con una sola comprobacion no hay histeresis que valga"

    # 1. bien -> mal: no avisa hasta la enesima seguida.
    avisos, estados = correr(["mal"] * n)
    assert avisos == [(n, "mal")], f"tendria que avisar solo en el paso {n}: {avisos}"
    assert estados["disco:/"]["estado"] == "mal"

    # 2. mal -> mal -> mal...: silencio absoluto. Este es el que importa.
    avisos, _ = correr(["mal"] * 200, estados)
    assert avisos == [], f"algo roto un rato largo ha mandado {len(avisos)} avisos de mas"

    # 3. mal -> bien: tambien confirmandose, y solo un aviso.
    avisos, estados = correr(["bien"] * n, estados)
    assert avisos == [(n, "bien")], avisos
    assert estados["disco:/"]["estado"] == "bien"

    # 4. Un pico suelto no cuenta: se rompe la racha y vuelta a empezar.
    avisos, _ = correr((["mal"] * (n - 1) + ["bien"]) * 20, estados)
    assert avisos == [], f"un pico de treinta segundos ha avisado: {avisos}"

    # 5. La banda entre los dos umbrales no mueve nada (estado None).
    avisos, _ = correr([None] * 50, estados)
    assert avisos == []

    # 6. Y lo que no se ha visto nunca se da por bueno: la primera vez que algo
    #    esta mal, avisa. Si no, estrenaria en silencio.
    avisos, _ = correr(["mal"] * n, {})
    assert avisos == [(n, "mal")], avisos

    # 7. El "desde" solo se mueve en la transicion, que es lo que deja decir
    #    "lleva tres horas asi" en el aviso.
    hace_rato = datetime.now(MADRID) - timedelta(hours=3)
    estados = {"disco:/": {"estado": "mal", "candidato": None, "racha": 0, "desde": hace_rato}}
    trans, guardar = vigilancia.decidir({"disco:/": vigilancia.Medida("mal", "sigue")}, estados)
    assert not trans and guardar["disco:/"][4] is False, "sin cambio no se toca desde"
    assert vigilancia.cuanto(hace_rato) == "3 h", vigilancia.cuanto(hace_rato)

    # 8. Y los umbrales de subida y bajada son distintos: entre 80 y 85 no pasa nada.
    sube, baja = 85.0, 80.0
    assert vigilancia.por_umbral(86, sube, baja) == "mal"
    assert vigilancia.por_umbral(84, sube, baja) is None, "84 esta en la banda: no mueve"
    assert vigilancia.por_umbral(81, sube, baja) is None
    assert vigilancia.por_umbral(80, sube, baja) == "bien"
    print(f"OK vigilancia: avisa a la {n}a seguida, calla 200 veces seguidas, y un pico no cuenta")


async def vigilancia_no_escribe() -> None:
    """La proactividad detecta y avisa. No arregla, y no puede.

    Todo lo que mira va por el atajo de solo lectura, que revienta con
    cualquier cosa que no sea de nivel `read`. Si algun dia quiere arreglar
    algo, eso pasa por la cola de aprobaciones como todo lo demas.
    """
    import inspect

    from . import herramientas, vigilancia

    fuente = inspect.getsource(vigilancia)
    for prohibido in ("ejecutar(", "herramientas.ejecutar", "aprobaciones.encolar", "_al_helper(\"despertar"):
        assert prohibido not in fuente, f"vigilancia.py usa {prohibido}"

    # Las herramientas que nombra, todas de lectura.
    usadas = {n for n in herramientas.RIESGO if f'"{n}"' in fuente}
    assert usadas, "no se ha encontrado ninguna herramienta en vigilancia.py"
    no_lectura = {n for n in usadas if herramientas.RIESGO[n] != "read"}
    assert not no_lectura, f"la vigilancia usa herramientas que escriben: {no_lectura}"

    # Y el atajo que usa no deja pasar otra cosa, aunque alguien lo intente.
    for escritura in ("lab_reiniciar", "mail_borrador"):
        try:
            await herramientas.solo_lectura(escritura)
        except RuntimeError as exc:
            assert "no es de lectura" in str(exc), exc
        else:
            raise AssertionError(f"{escritura} ha pasado por el atajo de la vigilancia")
    print(f"OK vigilancia: solo lee ({', '.join(sorted(usadas))}) y el atajo rechaza lo demas")


def rutas() -> None:
    """Las rutas que abren los botones del push siguen existiendo."""
    caminos = {r.path for r in main.app.routes}
    for ruta in (
        "/api/chat",
        "/api/briefing",
        "/api/vigilancia",
        "/api/aprobaciones",
        "/api/estado",
        "/api/agenda",
        "/api/briefings",
        "/api/acciones",
        "/api/acciones/{accion_id}",
        "/api/aprobaciones/{tool_call_id}/aprobar",
        "/api/aprobaciones/{tool_call_id}/rechazar",
        "/healthz",
        "/readyz",
    ):
        assert ruta in caminos, f"falta la ruta {ruta}"
    print(f"OK rutas: {len(caminos)} registradas")


if __name__ == "__main__":
    referencias()
    identidad()
    query_del_briefing()
    github_solo_lee()
    transiciones_de_vigilancia()
    rutas()
    consola()
    consola_no_llama_al_modelo()
    wol_no_es_herramienta()
    asyncio.run(atajo_del_estado())
    asyncio.run(catalogo_propio())
    asyncio.run(candados_memoria())
    asyncio.run(candado_de_acciones())
    asyncio.run(vigilancia_no_escribe())
    asyncio.run(arranque())
    print("todo OK")
