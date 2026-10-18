"""La cuota de Claude, medida leyendo las sesiones locales (ticket #39, #54).

La fuente es `~/.claude/projects/**/*.jsonl` —la misma que lee el `/usage`
de Claude Code. Cada línea de mensaje asistente trae el modelo y el `usage`
completo (`input`, `cache_creation`, `cache_read`, `output`, `thinking`), así
que la cuota se mide sola, sin anotar nada a mano.

De ahí salen:

- el total ponderado por costo relativo (`PESOS`), no la suma cruda de los
  cuatro componentes: `cache_read` sola, sin ponderar, no es comparable con
  nada (ticket #54). Los componentes crudos siguen enteros en `--json`
- el total por modelo, para que un ticket corrido con `--model fable` se vea
  con su propio 5h y sepa si come su bucket y también el general
- el pico por modelo dentro de cualquier ventana de 5h (la ventana rodante
  de la cuota)
- el total por semana, con el reset de viernes 21:00 UTC (#148: 17:00 en
  Santiago con UTC-4, 18:00 en horario de verano)
- la atribución por proyecto y por ticket: el path de la sesión codifica el
  worktree (`...--worktrees-<repo>-ticket-<n>`), que es lo que permite separar
  el consumo del harness del resto

Es aproximado, a propósito: sólo ve las sesiones locales de esta máquina —
ni otros dispositivos. Por defecto son las de un solo usuario; con varias
fuentes (`--projects` por coma o `quota.projects` en config, ticket #75) suma
los usuarios declarados, y una fuente que no se puede leer se salta y se
dice cuál y por qué — el total queda marcado como piso (`≥`), no como
medición. Está documentado en el `--help` del CLI y en `docs/harness/quota.md`.

Además de los directorios locales, una fuente puede ser **remota** por SSH
(#144): el usuario del 9-5 comparte la misma bolsa semanal de la cuenta.
`leer_fuentes` la corre con `scripts/quota_remota.py --json` en el host
remoto (el `run_cmd` es inyectable para los tests: sin red), con un timeout
corto (30 s por defecto) para que un host colgado no congele el bucle; si
falla, cuenta como ilegible (#75) y el total queda marcado como piso.

Acá vive la lógica pura (parsear, atribuir, agregar, dibujar). El que toca el
disco es `leer_sesiones`/`leer_fuentes`; el que escribe el evento lo hace el
CLI.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from stat import S_ISDIR
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict
from zoneinfo import ZoneInfo

from harness.summary import parse_fecha

# El reset de la semana de la cuota (#148): fijo en UTC, viernes 21:00 UTC.
# En Santiago es 17:00 con UTC-4 y 18:00 en horario de verano (UTC-3): lo que
# dice `/usage` ("Resets Oct 23, 6pm"). La zona local es sólo para mostrar.
SEMANA_TZ = ZoneInfo("America/Santiago")
RESET_HORA_UTC = 21
RESET_WEEKDAY = 4  # viernes
VENTANA_5H = timedelta(hours=5)

# Worktrees del harness: la carpeta bajo la que herdr clava cada ticket.
WORKTREES_DIR = ".worktrees"

# Los contadores de un uso: el `total` suma los cuatro que entran a la cuota.
# `thinking` va DENTRO de `output` (Claude lo reporta aparte pero no suma
# dos veces), así que no se suma al total.
CAMPOS = ("input", "cache_creation", "cache_read", "output", "thinking")

# El peso de cada componente contra el costo de un token de entrada (ticket
# #54): la primera corrida real de `harness quota` dio 13.617.021.213 tokens
# porque el total sumaba `cache_read` crudo, que se re-cuenta entero en cada
# mensaje y no es comparable con nada —ni con `/usage`, donde pesa una
# fracción de un token de salida. Los ratios salen del precio por millón de
# tokens de la API de Claude (Sonnet/Opus): input=1x de referencia,
# cache write (5m)≈1.25x, cache read≈0.1x, output≈5x. Son estables entre
# modelos aunque el precio base cambie.
PESOS = {"input": 1.0, "cache_creation": 1.25, "cache_read": 0.1, "output": 5.0}

# Patrón de ticket en el nombre del worktree: `...-ticket-<n>` o `...-t<n>`.
TICKET_RE = re.compile(r"[-_.]?ticket[-_.]?(\d+)$")
TICKET_CORTO_RE = re.compile(r"[-_.]t(\d+)$")

# La fuente remota por SSH (#144): el timeout corto es la línea contra un
# host colgado (un bucle de la noche no puede congelarse en un ssh muerto),
# y el script vive en el repo, no en el host remoto (autocontenido, stdlib).
REMOTA_TIMEOUT_S = 30.0
SCRIPT_REMOTA = Path(__file__).resolve().parent.parent / "scripts" / "quota_remota.py"


def vacio():
    """Un bloque de contadores a cero, en la forma que sale por `--json`."""
    return {c: 0 for c in CAMPOS + ("total",)}


def sumar_en(base, otros):
    """Suma `otros` (contadores) sobre `base`, en el lugar."""
    for c in CAMPOS:
        base[c] += otros[c]
    base["total"] += otros["total"]
    return base


def ponderar(c):
    """El total pesado por costo relativo (`PESOS`), no la suma cruda: la
    vista comparable contra `/usage`. `c` es cualquier contador con las claves
    de `PESOS` (p.ej. `total` o una entrada de `por_modelo`)."""
    return sum(c[campo] * peso for campo, peso in PESOS.items())


# ------------------------------------------------------------------------ parseo
def _num(v):
    """Un contador de `usage`: número, o la suma de un dict (p.ej. la forma
    `cache_creation` con sus tokens efímeros por duración)."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict):
        return sum(x for x in v.values()
                   if isinstance(x, (int, float)) and not isinstance(x, bool))
    return 0


def decodificar_uso(usage):
    """El `usage` de un mensaje (nombres largos reales o cortos) como
    contadores, o None si no coopera."""
    if not isinstance(usage, dict):
        return None

    def tomar(*nombres):
        for n in nombres:
            if n in usage:
                return _num(usage[n])
        return 0

    thinking = tomar("thinking_tokens")
    if not thinking:
        det = usage.get("output_tokens_details")
        if isinstance(det, dict):
            thinking = _num(det.get("thinking_tokens"))
    c = {
        "input": tomar("input_tokens", "input"),
        "cache_creation": tomar("cache_creation_input_tokens", "cache_creation"),
        "cache_read": tomar("cache_read_input_tokens", "cache_read"),
        "output": tomar("output_tokens", "output"),
        "thinking": thinking,
    }
    c["total"] = c["input"] + c["cache_creation"] + c["cache_read"] + c["output"]
    return c


def parsear_linea(linea):
    """Una línea JSONL de sesión con consumo: el modelo, el timestamp (UTC) y
    los contadores. None si no es un mensaje asistente con `usage` válido."""
    try:
        d = json.loads(linea)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    msg = d.get("message")
    if not isinstance(msg, dict):
        return None
    tokens = decodificar_uso(msg.get("usage"))
    if tokens is None:
        return None
    modelo = msg.get("model")
    if not isinstance(modelo, str) or not modelo:
        modelo = "?"
    dt = parse_fecha(str(d.get("timestamp", "")))
    if dt is None:
        return None
    if tokens["total"] == 0:
        return None  # mensaje sintético sin consumo
    return {"modelo": modelo, "timestamp": dt, "tokens": tokens}


def leer_sesiones(projects):
    """Todos los mensajes con consumo de `projects/**/*.jsonl`, en orden.

    Cada registro trae `dir` (el nombre codificado de la carpeta del proyecto)
    y `archivo`. Directorio inexistente = sin registros: no hay sesiones, no
    hay error. Las líneas rotas se saltan, igual que el log de eventos."""
    out = []
    base = Path(projects).expanduser()
    if not base.is_dir():
        return out
    for f in sorted(base.rglob("*.jsonl")):
        try:
            handle = f.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for linea in handle:
                r = parsear_linea(linea)
                if r is not None:
                    out.append({**r, "dir": f.parent.name, "archivo": str(f)})
    return out


def leer_fuentes(rutas, remotas=None, run_cmd=None, timeout=REMOTA_TIMEOUT_S,
                 script=None, offline=False):
    """`(registros, fuentes)` de las fuentes declaradas (#75, #144).

    `rutas` son los directorios de sesiones locales (una lista de str/Path,
    o una sola); `remotas` son las fuentes por SSH (`user@host`). `fuentes`
    trae una entrada por fuente declarada, en orden: `{"ruta", "ok",
    "error"}` —la remota además `remota: True` y los cortes ponderados
    (`ponderado`, `por_dia`, `por_modelo`, `por_semana`), porque por SSH no
    llegan mensajes crudos, sino el resumen de `quota_remota.py --json`.
    La fuente que no se puede leer no rompe el comando: se salta y su razón
    queda en `error` — es lo que hace del total un piso y no una medición.
    Las rutas/ssh repetidas se leen una vez; los registros salen en el
    orden de las fuentes (los locales; los remotos no traen registros).

    `run_cmd` es inyectable para los tests (sin red): recibe
    `(ssh, script, timeout)` y devuelve algo con `returncode`/`stdout`/
    `stderr` (por defecto, `_run_ssh`). `offline` no intenta nada por red:
    cada remota queda marcada ilegible con la razón.
    """
    rutas = [rutas] if isinstance(rutas, (str, Path)) else list(rutas or [])
    registros, fuentes, vistos = [], [], set()
    for ruta in rutas:
        p = Path(ruta).expanduser()
        clave = str(p)
        if clave in vistos:
            continue
        vistos.add(clave)
        error = _error_fuente(p)
        if error is not None:
            fuentes.append({"ruta": clave, "ok": False, "error": error})
            continue
        registros.extend(leer_sesiones(p))
        fuentes.append({"ruta": clave, "ok": True, "error": None})
    for ssh in list(remotas or []):
        if ssh in vistos:
            continue
        vistos.add(ssh)
        fuentes.append(leer_remota(ssh, run_cmd=run_cmd, timeout=timeout,
                                   script=script, offline=offline))
    return registros, fuentes


def _run_ssh(ssh, script, timeout):
    """`ssh <ssh> 'python3 - --json'` con el script por stdin: la corrida real
    de la fuente remota (la que los tests reemplazan con su `run_cmd`).
    `python3 - --json` corre el script de stdin con `sys.argv = ['-',
    '--json']` — la variante JSON de `quota_remota.py`. El
    `ConnectTimeout=10` es la línea de red corta; el timeout del
    `subprocess` (corto, 30 s por defecto) cubre el host que conecta y se
    cuelga."""
    return subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o",
         "ConnectTimeout=10", ssh, "python3", "-", "--json"],
        input=script.encode("utf-8"), capture_output=True, timeout=timeout)


def leer_remota(ssh, run_cmd=None, timeout=REMOTA_TIMEOUT_S, script=None,
                offline=False):
    """Una fuente remota por SSH (#144): una entrada de `fuentes` con
    `remota: True`.

    Corre `quota_remota.py --json` en el host (autocontenido, stdlib: no
    necesita el harness instalado allá) y trae los tokens **ponderados** por
    día, por modelo y por semana —la misma unidad que el total local; por
    SSH no llegan mensajes crudos. `run_cmd` es inyectable (tests, sin red);
    `offline` ni lo intenta. Nunca lanza: cualquier falla (timeout, salida
    no cero, JSON roto) es `ok: False` con la razón legible, que es lo que
    marca el total como piso (#75).
    """
    entrada = {"ruta": "ssh: " + ssh, "remota": True, "ok": False, "error": None,
               "ponderado": 0.0, "por_dia": {}, "por_modelo": {}, "por_semana": {}}
    if offline:
        entrada["error"] = "modo offline (sin red)"
        return entrada
    try:
        texto = (Path(script) if script else SCRIPT_REMOTA).read_text()
    except OSError as e:
        entrada["error"] = "no se pudo leer el script ({})".format(e.strerror or e)
        return entrada
    run_cmd = run_cmd or _run_ssh
    try:
        proc = run_cmd(ssh, texto, timeout)
    except subprocess.TimeoutExpired:
        entrada["error"] = "timeout SSH a los {} s".format(
            int(timeout) if float(timeout).is_integer() else timeout)
        return entrada
    except OSError as e:
        entrada["error"] = "SSH no se pudo correr ({})".format(e.strerror or e)
        return entrada
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        ultima = stderr.splitlines()[-1] if stderr else "sin detalle"
        entrada["error"] = "SSH salió con {}: {}".format(proc.returncode, ultima)
        return entrada
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        entrada["error"] = "salida no es JSON"
        return entrada
    if not isinstance(data, dict) or not all(
            isinstance(data.get(k), dict) for k in ("por_dia", "por_modelo",
                                                    "por_semana")):
        entrada["error"] = "salida sin por_dia/por_modelo/por_semana"
        return entrada
    try:
        por_dia = {str(k): float(v) for k, v in data["por_dia"].items()}
        por_modelo = {str(k): float(v) for k, v in data["por_modelo"].items()}
        por_semana = {str(k): float(v) for k, v in data["por_semana"].items()}
    except (AttributeError, TypeError, ValueError):
        entrada["error"] = "salida con valores que no son números"
        return entrada
    entrada.update(ok=True, ponderado=sum(por_dia.values()), por_dia=por_dia,
                   por_modelo=por_modelo, por_semana=por_semana)
    return entrada

# ----------------------------------------------------------------------- fuentes
def fuentes_desde_spec(spec):
    """Una lista de directorios de sesiones como texto separado por coma
    (#75) → lista de rutas. Los segmentos vacíos (comas sueltas o finales)
    se descartan; las rutas repetidas, una sola vez, en el orden declarado."""
    out, vistos = [], set()
    for p in str(spec).split(","):
        p = p.strip()
        if not p or p in vistos:
            continue
        vistos.add(p)
        out.append(p)
    return out


def _error_fuente(base):
    """Por qué un directorio de sesiones no se puede leer (#75), o None si se
    puede. Distingue "no existe" de "no se puede acceder": el segundo es el
caso multiusuario (el `~/.claude/projects` de otro usuario de la máquina),
y decir "no existe" lo taparía."""
    try:
        st = base.stat()
    except PermissionError:
        return "no se puede acceder (permiso denegado)"
    except FileNotFoundError:
        return "no existe"
    except NotADirectoryError:
        return "no es un directorio"
    except OSError as e:
        return "no se puede acceder ({})".format(e.strerror or e)
    if not S_ISDIR(st.st_mode):
        return "no es un directorio"
    # `stat` puede pasar por encima de permisos que `scandir` no; el probe
    # de verdad es abrir el listado.
    try:
        with os.scandir(base) as it:
            next(it, None)
    except PermissionError:
        return "no se puede acceder (permiso denegado)"
    except OSError as e:
        return "no se puede acceder ({})".format(e.strerror or e)
    return None


# ------------------------------------------------------------------ atribución
def codificar_path(p):
    """La codificación de Claude Code para nombres de carpeta: el path con `/`
    y puntos por `-`. `/u/docs/.worktrees` → `-u-docs--worktrees`."""
    return str(p).replace("/", "-").replace(".", "-")


def atribuir(dir_name, raices):
    """(proyecto, ticket, es_harness) de una carpeta de sesiones codificada.

    `raices` son los `repos.root` de la config (Path, ya expandidos). Un path
    bajo `<root>/.worktrees/` es del harness, y de ahí se saca el proyecto y el
    ticket cuando el nombre lo permite; lo demás es "resto"."""
    raices = sorted((Path(r) for r in raices), key=str, reverse=True)
    for root in raices:
        enc = codificar_path(root)
        if dir_name == enc:
            return (root.name, None, False)
        if not dir_name.startswith(enc + "-"):
            continue
        rest = dir_name[len(enc) + 1:]
        enc_wt = codificar_path(Path(root) / WORKTREES_DIR)
        if dir_name.startswith(enc_wt + "-"):
            # El resto es el nombre del worktree (puede traer guiones:
            # `f7league-ticket-256`).
            comp = dir_name[len(enc_wt) + 1:]
            m = TICKET_RE.search(comp) or TICKET_CORTO_RE.search(comp)
            if m:
                proyecto = comp[:m.start()].strip("-_.") or root.name
                return (proyecto, int(m.group(1)), True)
            return (comp or root.name, None, True)
        # Un repo no-harness bajo el root puede estar anidado (el caso
        # `entrevestidos/*`: `docs/harness/config.md`), así que `rest` puede
        # traer más de un tramo de path codificado (`f7league-app-calendario`).
        # Cortar en el último `-` perdía el proyecto real y dejaba el último
        # tramo suelto como si fuera uno (`calendario`, `mesa`, `bugs`...).
        return (rest, None, False)
    return (dir_name.lstrip("-"), None, False)


# ------------------------------------------------------------------------ tiempo
def semana_inicio(dt, tz=SEMANA_TZ):
    """El inicio de la semana de la cuota que contiene `dt`: el último viernes
    21:00 UTC (inclusive, #148), expresado en `tz` para mostrar."""
    s = dt.astimezone(timezone.utc)
    ref = s.replace(hour=RESET_HORA_UTC, minute=0, second=0, microsecond=0)
    if s < ref:
        ref -= timedelta(days=1)
    ref -= timedelta(days=(ref.weekday() - RESET_WEEKDAY) % 7)
    return ref.astimezone(tz)


def semana_reset(dt, tz=SEMANA_TZ):
    """El próximo reset de la semana de la cuota después de `dt`: el inicio
    de su semana más 7 días, sumados en UTC (en hora local, las semanas de
    cambio de horario darían 1 h de error). Expresado en `tz`."""
    inicio = semana_inicio(dt).astimezone(timezone.utc)
    return (inicio + timedelta(days=7)).astimezone(tz)


def pico_5h(puntos, ventana=VENTANA_5H):
    """El máximo total dentro de CUALQUIER ventana de `ventana` sobre
    `puntos` [(datetime, tokens)]. Devuelve (total, inicio, inicio+ventana)
    o None si no hay puntos. El máximo se alcanza con la ventana anclada en un
    punto, así que basta mirar esas."""
    pts = sorted(puntos)
    if not pts:
        return None
    ts = [p[0] for p in pts]
    pref = [0]
    for _, t in pts:
        pref.append(pref[-1] + t)
    mejor, inicio = None, None
    j = 0
    for i in range(len(pts)):
        while j < len(pts) and ts[j] <= ts[i] + ventana:
            j += 1
        v = pref[j] - pref[i]
        if mejor is None or v > mejor:
            mejor, inicio = v, ts[i]
    return (mejor, inicio, inicio + ventana)


# ----------------------------------------------------------------------- agregar
def clave_semana(fecha):
    """El inicio de semana de cuota en ISO (la clave de `por_semana_*`)
    correspondiente a la fecha de viernes `fecha` (date o datetime): el
    viernes 21:00 UTC expresado en `SEMANA_TZ`. Alinea la semana remota
    (que llega como fecha simple, #144) con las claves locales de `agregar`."""
    d = fecha.date() if isinstance(fecha, datetime) else fecha
    return datetime(d.year, d.month, d.day, RESET_HORA_UTC, tzinfo=timezone.utc) \
        .astimezone(SEMANA_TZ).isoformat()


def agregar(registros, raices, fuentes=None):
    """El agregado crudo (la forma de `--json`, con los timestamps como
    datetime, que `as_dict` convierte a ISO).

    `por_semana_cuota`, `por_semana_ponderado` y `por_dia` (#47) son las
    vistas ponderadas —la semana partida harness/resto para el resumen de la
    mañana ("cuota de la semana, cuánto es del harness"), la semana y el
    modelo ponderados que dibuja la tabla (#74: la misma unidad que el total),
    y la evolución por noche del reporte HTML—: `por_semana` sigue crudo y por
    modelo, sin tocar, porque `test_por_semana` ya lo fija así y `--json` no
    pierde el crudo. `por_dia` bucketea por fecha calendario en `SEMANA_TZ`:
    es la "noche" del harness, no UTC.

    `fuentes` (de `leer_fuentes`, #75) se lleva en el agregado: con alguna
    faltante, `completa` es False y el total es un piso (`≥`), no una
    medición — es la marca que un consumidor (p.ej. el piso de cuota del
    router, #45) tiene que respetar. Sin `fuentes` no hay marca: es el
    agregado de antes, y `completa` queda sin información para negar.

    Las fuentes **remotas legibles** (#144) se suman a las vistas ponderadas:
    al total (`ponderado`), a `por_modelo` (por modelo real), a `por_dia`
    (en `resto`: las sesiones de otro usuario no son del harness) y a
    `por_semana_ponderado`/`por_semana_cuota` (bajo la clave de modelo
    `remoto`, para que el total de la fila sea local + remota y la
    estimación de `harness run` respete la bolsa compartida). Sus cifras son
    de los últimos 35 días (ventana de `quota_remota.py`), no del histórico
    completo: la tabla lo dice al pie. El crudo (`total`, `por_semana`)
    sigue local.
    """
    total = vacio()
    por_modelo: Dict[str, dict] = {}
    puntos: Dict[str, list] = {}
    semanas: Dict[str, Dict[str, int]] = {}
    por_semana_cuota: Dict[str, Dict[str, float]] = {}
    por_semana_ponderado: Dict[str, Dict[str, float]] = {}
    por_dia: Dict[str, Dict[str, float]] = {}
    proyectos: Dict[str, dict] = {}
    harness = vacio()
    resto = vacio()
    for r in registros:
        t, m, dt = r["tokens"], r["modelo"], r["timestamp"]
        sumar_en(total, t)
        sumar_en(por_modelo.setdefault(m, {**vacio(), "ponderado": 0.0}), t)
        por_modelo[m]["mensajes"] = por_modelo[m].get("mensajes", 0) + 1
        puntos.setdefault(m, []).append((dt, t["total"]))
        wk = semanas.setdefault(semana_inicio(dt).isoformat(), {})
        wk[m] = wk.get(m, 0) + t["total"]
        proyecto, ticket, es_h = atribuir(r["dir"], raices)
        peso = ponderar(t)
        por_modelo[m]["ponderado"] += peso
        wkp = por_semana_ponderado.setdefault(semana_inicio(dt).isoformat(), {})
        wkp[m] = wkp.get(m, 0.0) + peso
        wkc = por_semana_cuota.setdefault(semana_inicio(dt).isoformat(),
                                          {"harness": 0.0, "resto": 0.0})
        wkc["harness" if es_h else "resto"] += peso
        pd = por_dia.setdefault(dt.astimezone(SEMANA_TZ).date().isoformat(),
                                {"harness": 0.0, "resto": 0.0})
        pd["harness" if es_h else "resto"] += peso
        clave = proyecto + (" [harness]" if es_h else "")
        p = proyectos.setdefault(clave, {"harness": es_h, **vacio(),
                                         "ponderado": 0.0,
                                         "tickets": {}, "tickets_ponderado": {}})
        sumar_en(p, t)
        p["ponderado"] += peso
        if ticket is not None:
            p["tickets"][str(ticket)] = p["tickets"].get(str(ticket), 0) + t["total"]
            p["tickets_ponderado"][str(ticket)] = (
                p["tickets_ponderado"].get(str(ticket), 0.0) + peso)
        sumar_en(harness if es_h else resto, t)
    remoto_peso = 0.0
    for f in [x for x in (fuentes or []) if x.get("remota") and x["ok"]]:
        remoto_peso += f["ponderado"]
        for m, w in f["por_modelo"].items():
            por_modelo.setdefault(m, {**vacio(), "ponderado": 0.0})["ponderado"] += w
        for dia, w in f["por_dia"].items():
            pd = por_dia.setdefault(dia, {"harness": 0.0, "resto": 0.0})
            pd["resto"] += w
        for fecha, w in f["por_semana"].items():
            iso = clave_semana(fecha)
            wkp = por_semana_ponderado.setdefault(iso, {})
            wkp["remoto"] = wkp.get("remoto", 0.0) + w
            wkc = por_semana_cuota.setdefault(iso, {"harness": 0.0, "resto": 0.0})
            wkc["resto"] += w
    return {
        "fuentes": fuentes,
        "completa": fuentes is None or all(f["ok"] for f in fuentes),
        "archivos": len({r["archivo"] for r in registros}),
        "mensajes": len(registros),
        "total": total,
        "ponderado": ponderar(total) + remoto_peso,
        "por_modelo": por_modelo,
        "pico_5h": {m: pico_5h(pts) for m, pts in puntos.items()},
        "por_semana": semanas,
        "por_semana_cuota": por_semana_cuota,
        "por_semana_ponderado": por_semana_ponderado,
        "por_dia": por_dia,
        "por_proyecto": proyectos,
        "harness": harness,
        "resto": resto,
    }


def semana_de(agg, ahora):
    """(harness, resto) ponderados de la semana de cuota que contiene
    `ahora`, sobre `agg["por_semana_cuota"]`. Cero de las dos si esa semana
    todavía no tiene mensajes: no es un error, es que no pasó nada."""
    wk = agg["por_semana_cuota"].get(semana_inicio(ahora).isoformat(),
                                     {"harness": 0.0, "resto": 0.0})
    return wk["harness"], wk["resto"]


# ------------------------------------------------------------------- por ticket
def puntos_de_ticket(registros, raices, proyecto, ticket):
    """[(timestamp, peso)] de los mensajes de un ticket puntual, ordenados.

    Más fino que `agregar` (que sólo suma): sirve para repartir la cuota
    entre los peldaños de la escalada de un ticket (#38, ver
    `harness.state.pasos`), cruzando cada punto contra la ventana de tiempo
    de cada intento con `cuota_por_ventana`. Vacío si no hay coincidencias,
    no error: un ticket que nunca corrió con Claude no tiene cuota, y eso
    no es distinto de no tener datos."""
    out = []
    for r in registros:
        p, t, es_h = atribuir(r["dir"], raices)
        if not es_h or t != ticket or p != proyecto:
            continue
        out.append((r["timestamp"], ponderar(r["tokens"])))
    return sorted(out)


def cuota_por_ventana(puntos, inicio, fin):
    """La cuota ponderada de `puntos` [(dt, peso)] dentro de [inicio, fin]
    (bordes inclusive). `None` en cualquier borde no filtra por ese lado."""
    total = 0.0
    for dt, peso in puntos:
        if inicio is not None and dt < inicio:
            continue
        if fin is not None and dt > fin:
            continue
        total += peso
    return total


def as_dict(agg):
    """El agregado serializable: los timestamps de los picos a ISO."""
    d = json.loads(json.dumps(agg, default=lambda o: o.isoformat()
                              if isinstance(o, datetime) else str(o),
                              ensure_ascii=False))
    return d


# ----------------------------------------------------------------------- dibujo
def _fmt(n):
    return f"{n:,}"


def _pct(n, tot):
    return f"{int(round(100.0 * n / tot))}%" if tot else "0%"


def _utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def render_quota(agg, tope_semanal=None):
    """La tabla corta: sin flags, esto es lo que se imprime.

    Todo ponderado, en la misma unidad que el total (ticket #74): los crudos
    se quedaron bajo la línea del total y en `--json`. `tope_semanal` (la
    constante de calibración de la config, #74) agrega el porcentaje estimado
    del tope semanal a cada corte; sin ella no se inventa un porcentaje: se
    muestran los tokens y se dice que falta calibrar."""
    out = []
    a = out.append
    total = agg["total"]["total"]
    t = agg["total"]
    total_w = agg["ponderado"]
    # Con fuentes faltantes el total no se lee como medición: se marca piso
    # (≥) y se dice cuál falta y por qué (#75).
    fuentes = agg.get("fuentes") or []
    faltan = [f for f in fuentes if not f["ok"]]
    remotos_ok = [f for f in fuentes if f.get("remota") and f["ok"]]
    piso = bool(faltan)
    a(f"Cuota · sesiones locales ({agg['archivos']} archivo(s), "
      f"{agg['mensajes']} mensaje(s))")
    if fuentes:
        a("  fuentes: {}/{} leídas{}".format(
            len(fuentes) - len(faltan), len(fuentes),
            " — el total es un piso" if piso else ""))
        for f in faltan:
            a("    falta {}: {}".format(f["ruta"], f["error"]))
    signo = "≥ " if piso else ""
    a(f"  total ponderado {signo}{_fmt(round(total_w))} tokens "
      f"(por costo relativo, ver PESOS)" + (" · piso" if piso else ""))
    a(f"    crudo: input {_fmt(t['input'])} · cache_creation "
      f"{_fmt(t['cache_creation'])} · cache_read {_fmt(t['cache_read'])} · "
      f"output {_fmt(t['output'])} · thinking {_fmt(t['thinking'])} "
      f"(suma cruda {_fmt(total)})")
    a("")

    def del_tope(n):
        return f" · {100.0 * n / tope_semanal:.1f}% del tope" \
            if tope_semanal else ""

    a(f"  por modelo (ponderado):{del_tope(total_w)}")
    if not agg["por_modelo"]:
        a("    (sin sesiones)")
    for m in sorted(agg["por_modelo"], key=lambda m: -agg["por_modelo"][m]["ponderado"]):
        d = agg["por_modelo"][m]
        a(f"    {m:<28} {_fmt(round(d['ponderado'])):>12}  "
          f"({_pct(d['ponderado'], total_w)})")
    a("")
    a("  pico en 5h (ventana rodante, crudo):")
    for m in sorted(agg["pico_5h"], key=lambda m: -agg["pico_5h"][m][0]):
        v, desde, hasta = agg["pico_5h"][m]
        a(f"    {m:<28} {_fmt(v):>12}  ({_utc(desde)} → {_utc(hasta)})")
    a("")
    a("  por semana (ponderado, reset vie 21:00 UTC; inicio en hora de "
      "America/Santiago):")
    if not agg["por_semana_ponderado"]:
        a("    (sin sesiones)")
    for iso in sorted(agg["por_semana_ponderado"], reverse=True):
        n = sum(agg["por_semana_ponderado"][iso].values())
        a(f"    {iso[:10]} {iso[11:16]}   {_fmt(round(n)):>12}{del_tope(n)}")
    if any("remoto" in v for v in agg["por_semana_ponderado"].values()):
        a("    (las filas con fuente remota suman local + remota)")
    a("")
    a("  por día (ponderado, últimos 10, America/Santiago):")
    dias = sorted(agg["por_dia"], reverse=True)[:10]
    if not dias:
        a("    (sin sesiones)")
    for dia in dias:
        n = agg["por_dia"][dia]["harness"] + agg["por_dia"][dia]["resto"]
        a(f"    {dia}   {_fmt(round(n)):>12}{del_tope(n)}")
    a("")
    a(f"  por proyecto (ponderado):{del_tope(total_w)}")
    if not agg["por_proyecto"]:
        a("    (sin sesiones)")
    for clave in sorted(agg["por_proyecto"],
                        key=lambda n: -agg["por_proyecto"][n]["ponderado"]):
        d = agg["por_proyecto"][clave]
        nombre = clave[:-len(" [harness]")] if d["harness"] else clave
        tickets = ", ".join(f"ticket {k} {_fmt(round(v))}"
                            for k, v in sorted(d["tickets_ponderado"].items(),
                                               key=lambda kv: -kv[1]))
        marca = " [harness]" if d["harness"] else ""
        sufijo = f"  ({tickets})" if tickets else ""
        a(f"    {nombre}{marca:<10} {_fmt(round(d['ponderado'])):>12}{sufijo}")
    hw, rw = ponderar(agg["harness"]), ponderar(agg["resto"])
    a(f"  harness {_fmt(round(hw))} ({_pct(hw, total_w)}) · "
      f"resto {_fmt(round(rw))} ({_pct(rw, total_w)})")
    if tope_semanal:
        a(f"  (tope semanal estimado: {_fmt(round(tope_semanal))} tokens "
          f"ponderados; el % es aproximado)")
    else:
        a("  (sin calibración: fijar \"quota\": {\"tope_semanal\": N} en la "
          "config para estimar el tope semanal)")
    if remotos_ok:
        a("  (aproximado: las sesiones locales de esta máquina + la remota "
          "por SSH, de los últimos 35 días)")
    else:
        a("  (aproximado: solo las sesiones locales de esta máquina)")
    return "\n".join(out) + "\n"


def resumen_evento(agg):
    """La línea para el log de eventos: una corrida, una línea."""
    partes = [f"total {round(agg['ponderado'])} tokens ponderados "
              f"({agg['total']['total']} crudo)",
              f"{agg['archivos']} archivo(s)", f"{agg['mensajes']} mensaje(s)"]
    picos = sorted(agg["pico_5h"].items(), key=lambda kv: -kv[1][0])
    partes.append("pico5h " + ", ".join(f"{m}={v[0]}" for m, v in picos[:3]))
    semanas = sorted(agg["por_semana"].items(), reverse=True)
    partes.append("semanas " + ", ".join(
        f"{iso[:10]}={sum(v.values())}" for iso, v in semanas[:3]))
    partes.append(f"harness={agg['harness']['total']} "
                  f"({_pct(agg['harness']['total'], agg['total']['total'])})")
    fuentes = agg.get("fuentes") or []
    faltan = [f for f in fuentes if not f["ok"]]
    if faltan:
        partes.append("piso: faltan {} de {} fuente(s)".format(
            len(faltan), len(fuentes)))
    remotos = [f for f in fuentes if f.get("remota") and f["ok"]]
    if remotos:
        partes.append("remota: " + ", ".join(
            "{} {} tokens (35 días)".format(f["ruta"][len("ssh: "):],
                                            int(f["ponderado"]))
            for f in remotos))
    return "; ".join(partes)
