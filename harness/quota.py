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
- el total por semana, con el reset de viernes 17:00 America/Santiago
- la atribución por proyecto y por ticket: el path de la sesión codifica el
  worktree (`...--worktrees-<repo>-ticket-<n>`), que es lo que permite separar
  el consumo del harness del resto

Es aproximado, a propósito: sólo ve las sesiones de este usuario de esta
máquina —ni otros dispositivos ni otros usuarios. Está documentado en el
`--help` del CLI y en `docs/harness/quota.md`.

Acá vive la lógica pura (parsear, atribuir, agregar, dibujar). El que toca el
disco es `leer_sesiones`; el que escribe el evento lo hace el CLI.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict
from zoneinfo import ZoneInfo

from harness.summary import parse_fecha

# El reset de la semana de la cuota: viernes 17:00 America/Santiago.
SEMANA_TZ = ZoneInfo("America/Santiago")
RESET_HORA = 17
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
    17:00 America/Santiago (inclusive)."""
    s = dt.astimezone(tz)
    ref = s.replace(hour=RESET_HORA, minute=0, second=0, microsecond=0)
    if s < ref:
        ref -= timedelta(days=1)
    ref -= timedelta(days=(ref.weekday() - RESET_WEEKDAY) % 7)
    return ref


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
def agregar(registros, raices):
    """El agregado crudo (la forma de `--json`, con los timestamps como
    datetime, que `as_dict` convierte a ISO).

    `por_semana_cuota`, `por_semana_ponderado` y `por_dia` (#47) son las
    vistas ponderadas —la semana partida harness/resto para el resumen de la
    mañana ("cuota de la semana, cuánto es del harness"), la semana y el
    modelo ponderados que dibuja la tabla (#74: la misma unidad que el total),
    y la evolución por noche del reporte HTML—: `por_semana` sigue crudo y por
    modelo, sin tocar, porque `test_por_semana` ya lo fija así y `--json` no
    pierde el crudo. `por_dia` bucketea por fecha calendario en `SEMANA_TZ`:
    es la "noche" del harness, no UTC."""
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
    return {
        "archivos": len({r["archivo"] for r in registros}),
        "mensajes": len(registros),
        "total": total,
        "ponderado": ponderar(total),
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
    a(f"Cuota · sesiones locales ({agg['archivos']} archivo(s), "
      f"{agg['mensajes']} mensaje(s))")
    a(f"  total ponderado {_fmt(round(total_w))} tokens "
      f"(por costo relativo, ver PESOS)")
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
    a("  por semana (ponderado, reset vie 17:00, America/Santiago):")
    if not agg["por_semana_ponderado"]:
        a("    (sin sesiones)")
    for iso in sorted(agg["por_semana_ponderado"], reverse=True):
        n = sum(agg["por_semana_ponderado"][iso].values())
        a(f"    {iso[:10]}   {_fmt(round(n)):>12}{del_tope(n)}")
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
    a("  (aproximado: solo este usuario de esta máquina)")
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
    return "; ".join(partes)
