"""La cuota REAL de Claude: `/usage` de Claude Code, leído por herdr.

`harness quota` estima la cuota leyendo los `.jsonl` locales: no ve otras
máquinas ni otros usuarios. Las barras de `/usage` ("Current session",
"Current week (all models)", "Current week (<modelo>)") son de la CUENTA,
server-side: incluyen todo. Sólo la sección de abajo ("What's contributing
to your limits usage?") es local, y esa no se lee.

Dos fuentes de la cuota real, en este orden:

1. la statusline de las sesiones de Claude del harness
   (`scripts/statusline-harness`): Claude Code le pasa por stdin
   `rate_limits` (sesión de 5h y semana, con `resets_at` en epoch) y el
   script lo anota en `~/.local/state/harness/rate_limits.json`. Si está
   fresco (`quota.statusline_max_edad_min`), se usa sin abrir nada. No trae
   el bucket por modelo (Fable) y sólo aparece después de la primera
   respuesta del modelo en una sesión;
2. `/usage`, capturado por herdr: la única fuente en frío y la única con
   los buckets por modelo.

Las piezas:

- `escribir_rate_limits` / `leer_rate_limits`: el archivo de la statusline.
- `leer_cuota`: la elección de fuente.
- `parsear_usage` (puro): el texto de la pantalla -> `Usage`, con el % y el
  reset de cada bucket como datetime con zona (America/Santiago). El texto
  no tiene contrato: lo que no se entiende es `UsageIlegible`, con una
  línea legible. Nunca se inventa un número.
- `capturar` (herdr): abre un pane temporal, arranca `claude`, manda
  `/usage`, espera con `herdr pane wait-output` (no con sleeps fijos), lee
  y SIEMPRE cierra el pane. Mirar `/usage` no llama al modelo.
- la política de `harness run` (`decidir`, `aplicar_cuota`, `Cuota`):
  con la sesión de 5h sobre el umbral no se lanza Claude y, si sólo queda
  Claude, se espera su reset (con tope en la hora límite de la corrida);
  la semana dice cuántos tickets de Claude caben, más agresivo mientras
  más cerca del reset y más cuota sin usar. Si `/usage` no se puede leer,
  la estimación de `harness quota`, y se dice.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TZ = ZoneInfo("America/Santiago")


class UsageIlegible(Exception):
    """`/usage` no se pudo leer o no se entendió. El mensaje es una línea
    para humanos: qué faltó y dónde."""


# ------------------------------------------------------------------- parser
@dataclass
class Bucket:
    nombre: str                    # "Current session", "Fable", ...
    pct: float                     # 0-100, tal cual lo muestra /usage
    reset: Optional[datetime]      # None = /usage no mostró reset
    fuente: str = "usage"          # "usage" | "statusline"
    leido: Optional[datetime] = None  # cuándo se capturó el dato


@dataclass
class Usage:
    sesion: Bucket                 # la ventana de 5h
    semana: Bucket                 # la semana, todos los modelos
    por_modelo: List[Bucket] = field(default_factory=list)  # "Current week (X)"


HEADER_SESION = "Current session"
HEADER_SEMANA = "Current week (all models)"
_HEADER_RE = re.compile(r"^(Current session|Current week \((?P<modelo>[^)]+)\))$")
_FIN_RE = re.compile(r"^What's contributing", re.I)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%\s*used\b")
_RESET_RE = re.compile(r"^Resets?\b")

_MESES = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}
_ZONA_RE = re.compile(r"\s*\(([^()]+)\)\s*$")
_REL_RE = re.compile(r"^in\s+((?:\d+\s*[dhm]\s*)+)$", re.I)
_REL_PARTE_RE = re.compile(r"(\d+)\s*([dhm])", re.I)
_ABS_RE = re.compile(
    r"^(?:(?P<mes>[A-Za-z]{3,9})\.?\s+(?P<dia>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)?$", re.I)


def _pct(valor):
    v = float(valor)
    return int(v) if v.is_integer() else v


def parsear_reset(linea, ahora):
    """'Resets 1:10am (America/Santiago)' -> datetime en America/Santiago.

    Formatos: hora sola ('6pm', '1:10am', '18:30'), con fecha ('Oct 23,
    6pm', 'Oct 23 at 6pm'), relativo ('in 2h 15m'), con o sin zona entre
    paréntesis (sin zona: America/Santiago). Una hora sin fecha es la
    próxima vez que llega; una fecha sin año, la de este año salvo que ya
    pasó hace más de un día (entonces es del que viene). Lo que no calza es
    `UsageIlegible` con la línea entera: no se adivina."""
    original = linea.strip()

    def ilegible(por):
        return UsageIlegible("no entiendo el reset {!r}: {}".format(original, por))

    resto = _RESET_RE.sub("", original, count=1).strip()
    zona = TZ
    m = _ZONA_RE.search(resto)
    if m:
        try:
            zona = ZoneInfo(m.group(1).strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ilegible("zona desconocida {!r}".format(m.group(1)))
        resto = resto[:m.start()].strip()
    ahora_z = ahora.astimezone(zona)

    rel = _REL_RE.match(resto)
    if rel:
        delta = timedelta()
        for n, u in _REL_PARTE_RE.findall(rel.group(1)):
            delta += {"d": timedelta(days=int(n)), "h": timedelta(hours=int(n)),
                      "m": timedelta(minutes=int(n))}[u.lower()]
        return (ahora + delta).astimezone(TZ)

    a = _ABS_RE.match(resto)
    if not a:
        raise ilegible("formato desconocido")
    h = int(a.group("h"))
    mi = int(a.group("m") or 0)
    ampm = (a.group("ampm") or "").lower()
    if ampm:
        if not 1 <= h <= 12:
            raise ilegible("hora {} fuera de 1-12".format(h))
        h = h % 12 + (12 if ampm == "pm" else 0)
    elif a.group("m") is None:
        raise ilegible("hora sin am/pm ni minutos")
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ilegible("hora inválida")
    if a.group("mes"):
        mes = _MESES.get(a.group("mes")[:3].lower())
        if mes is None:
            raise ilegible("mes desconocido {!r}".format(a.group("mes")))
        try:
            dt = ahora_z.replace(month=mes, day=int(a.group("dia")), hour=h,
                                 minute=mi, second=0, microsecond=0)
            if dt < ahora_z - timedelta(days=1):
                dt = dt.replace(year=dt.year + 1)
        except ValueError:
            raise ilegible("fecha inválida")
    else:
        dt = ahora_z.replace(hour=h, minute=mi, second=0, microsecond=0)
        if dt <= ahora_z:
            dt = (dt + timedelta(days=1)).replace(hour=h, minute=mi)
    # `replace` sobre un datetime con ZoneInfo recalcula el offset (DST).
    return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute,
                    tzinfo=zona).astimezone(TZ)


def parsear_usage(texto, ahora):
    """El texto de la pantalla de `/usage` -> `Usage`.

    Sólo se miran los bloques "Current session" y "Current week (...)"; lo
    de arriba (el bloque "Session", el footprint de plugins) y lo de abajo
    ("What's contributing...", que es local y trae sus propios %) se
    ignora. Si un encabezado aparece dos veces, gana el último (la pantalla
    más reciente). Sin sesión o sin semana, o un bucket sin "% used", es
    `UsageIlegible`: nunca un número inventado."""
    bloques = {}
    orden = []
    actual = None
    for cruda in texto.splitlines():
        linea = cruda.strip()
        if _FIN_RE.match(linea):
            actual = None
            continue
        h = _HEADER_RE.match(linea)
        if h:
            actual = linea
            bloques[actual] = []
            if actual in orden:
                orden.remove(actual)
            orden.append(actual)
            continue
        if actual is not None and linea:
            bloques[actual].append(linea)

    for requerido in (HEADER_SESION, HEADER_SEMANA):
        if requerido not in bloques:
            raise UsageIlegible(
                "no encuentro el bloque {!r} en la salida de /usage "
                "(¿cambió el formato?); encontrados: {}".format(
                    requerido, ", ".join(repr(o) for o in orden) or "ninguno"))

    def bucket(header):
        pct, reset = None, None
        for l in bloques[header]:
            if pct is None:
                m = _PCT_RE.search(l)
                if m:
                    pct = _pct(m.group(1))
                    continue
            if _RESET_RE.match(l):
                reset = parsear_reset(l, ahora)
        if pct is None:
            raise UsageIlegible("{!r}: no encuentro el '% used' en {!r}".format(
                header, " | ".join(bloques[header])))
        m = _HEADER_RE.match(header)
        nombre = m.group("modelo") if m.group("modelo") else header
        return Bucket(nombre, pct, reset, "usage", ahora)

    sesion = bucket(HEADER_SESION)
    semana = bucket(HEADER_SEMANA)
    semana.nombre = HEADER_SEMANA
    por_modelo = [bucket(h) for h in orden
                  if h not in (HEADER_SESION, HEADER_SEMANA)]
    return Usage(sesion=sesion, semana=semana, por_modelo=por_modelo)


def _bucket_dict(b, ahora):
    edad = (int(round((ahora - b.leido).total_seconds()))
            if (ahora is not None and b.leido is not None) else None)
    return {"pct": b.pct, "reset": b.reset.isoformat() if b.reset else None,
            "fuente": b.fuente,
            "leido": b.leido.isoformat() if b.leido else None,
            "edad_s": edad}


def as_dict(u, ahora=None):
    """El contrato de `harness usage --json`: lo que consumen el cupo por
    noche (#145), el router (#146) y la ventana de quema (#147). Los % son
    0-100 como en /usage; los resets, ISO 8601 con offset de Santiago. Cada
    dato dice de qué fuente vino y su antigüedad (`edad_s` contra `ahora`)."""
    fuentes = []
    for b in [u.sesion, u.semana] + list(u.por_modelo):
        if b.fuente not in fuentes:
            fuentes.append(b.fuente)
    return {
        "fuente": "+".join(fuentes),
        "alcance": "cuenta",
        "consultado": ahora.isoformat() if ahora else None,
        "sesion": _bucket_dict(u.sesion, ahora),
        "semana": _bucket_dict(u.semana, ahora),
        "por_modelo": {b.nombre: _bucket_dict(b, ahora) for b in u.por_modelo},
    }


def _cuando(dt):
    if dt is None:
        return "sin reset"
    dias = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    d = dt.astimezone(TZ)
    return "resetea {} {:%d-%m %H:%M}".format(dias[d.weekday()], d)


def render_usage(u, ahora=None):
    """La tabla corta de `harness usage`, con la fuente de cada dato."""
    filas = [("sesión (5h)", u.sesion), ("semana (todos)", u.semana)]
    filas += [("semana ({})".format(b.nombre), b) for b in u.por_modelo]
    lineas = ["cuota de Claude (a nivel de cuenta: incluye otros equipos)"]
    for nombre, b in filas:
        origen = b.fuente
        if ahora is not None and b.leido is not None:
            origen += ", hace {}s".format(int((ahora - b.leido).total_seconds()))
        lineas.append("  {:<22} {:>5}%  {}  ({})".format(
            nombre, b.pct, _cuando(b.reset), origen))
    lineas.append("  (America/Santiago)")
    return "\n".join(lineas) + "\n"


# --------------------------------------------------------------- statusline
def ruta_rate_limits(env=None):
    """Dónde anota la statusline: `$XDG_STATE_HOME/harness/rate_limits.json`
    (por defecto `~/.local/state/harness/`), junto al log de eventos."""
    env = os.environ if env is None else env
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "harness" / "rate_limits.json"


def escribir_rate_limits(entrada, ruta, ahora):
    """Anota `rate_limits` del JSON de statusline, con la hora de captura y
    el `session_id`. Atómico (temporal + `os.replace`): un lector nunca ve
    medio archivo. Sin `rate_limits` (la sesión todavía no tuvo respuesta
    del modelo) no escribe nada: no pisa un dato bueno con nada. Devuelve
    si escribió."""
    rl = entrada.get("rate_limits") if isinstance(entrada, dict) else None
    if not isinstance(rl, dict) or not (rl.get("five_hour") or rl.get("seven_day")):
        return False
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_name(".{}.{}.tmp".format(ruta.name, os.getpid()))
    tmp.write_text(json.dumps({"rate_limits": rl,
                               "capturado": ahora.isoformat(),
                               "session_id": entrada.get("session_id")},
                              ensure_ascii=False))
    os.replace(str(tmp), str(ruta))
    return True


def _bucket_rl(nombre, crudo, leido):
    if not isinstance(crudo, dict):
        raise ValueError("falta {}".format(nombre))
    pct = crudo.get("used_percentage")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        raise ValueError("{}.used_percentage no es un número".format(nombre))
    reset = crudo.get("resets_at")
    if reset is not None:
        if isinstance(reset, bool) or not isinstance(reset, (int, float)):
            raise ValueError("{}.resets_at no es epoch".format(nombre))
        reset = datetime.fromtimestamp(reset, TZ)
    return Bucket(nombre, _pct(pct), reset, "statusline", leido)


# Desajuste de reloj tolerado entre quien escribió el archivo y quien lo lee.
TOLERANCIA_RELOJ = timedelta(seconds=60)


def leer_rate_limits(ruta, ahora, max_edad):
    """`(Usage, "")` desde el archivo de la statusline si está fresco (más
    nuevo que `max_edad`), o `(None, motivo)`: no existe, está roto o es
    viejo. Sin buckets por modelo: la statusline no los trae."""
    ruta = Path(ruta)
    if not ruta.exists():
        return None, "statusline: {} no existe".format(ruta)
    try:
        data = json.loads(ruta.read_text())
        leido = datetime.fromisoformat(data["capturado"])
        if leido.tzinfo is None:
            raise ValueError("capturado sin zona")
        rl = data["rate_limits"]
        sesion = _bucket_rl("Current session", rl.get("five_hour"), leido)
        semana = _bucket_rl(HEADER_SEMANA, rl.get("seven_day"), leido)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return None, "statusline: {} ilegible ({})".format(ruta, e)
    edad = ahora - leido
    if edad < -TOLERANCIA_RELOJ:
        return None, "statusline: capturado en el futuro ({}): reloj " \
                     "desajustado o archivo ajeno".format(leido.isoformat())
    if edad > max_edad:
        return None, "statusline: viejo: {} min (máx {} min)".format(
            int(edad.total_seconds() // 60), int(max_edad.total_seconds() // 60))
    # Un bucket cuyo reset ya pasó es de antes del reset: su % no vale,
    # aunque el archivo sea "fresco" (revisión de #159). Se descarta el
    # archivo entero y se va a /usage.
    for b in (sesion, semana):
        if b.reset is not None and b.reset <= ahora:
            return None, ("statusline: el reset de {!r} ({}) ya pasó: el dato "
                          "es de antes del reset".format(b.nombre, _hhmm(b.reset)))
    return Usage(sesion=sesion, semana=semana), ""


def leer_cuota(leer_statusline, capturar_usage, fuente="auto"):
    """La cuota real, de la mejor fuente disponible.

    `fuente`: "auto" (la statusline si está fresca; si no, `/usage` por
    herdr), "statusline" (sólo el archivo) o "usage" (sólo el pane: el
    único con los buckets por modelo). Lanza `UsageIlegible` con los
    motivos de todas las que se probaron."""
    motivo_sl = ""
    if fuente in ("auto", "statusline"):
        u, motivo_sl = leer_statusline()
        if u is not None:
            return u
        if fuente == "statusline":
            raise UsageIlegible(motivo_sl)
    try:
        return capturar_usage()
    except UsageIlegible as e:
        if motivo_sl:
            raise UsageIlegible("{}; /usage: {}".format(motivo_sl, e))
        raise


# ------------------------------------------------------------------ captura
# Claude listo para recibir: el pie de la TUI ("? for shortcuts" en modo
# normal, "shift+tab to cycle" en auto) o el diálogo de confianza.
LISTO_RE = r"for shortcuts|shift\+tab|trust"
CONFIANZA_RE = re.compile(r"Do you trust|trust the files|trust this folder", re.I)


def _json(out):
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return None


def pane_base_de(run_cmd, env):
    """El pane desde el que se abre el temporal: dentro de herdr, el propio
    (`HERDR_PANE_ID`); fuera, el enfocado de `herdr pane list` (o el
    primero). Sin server de herdr no hay de dónde: `UsageIlegible`."""
    propio = env.get("HERDR_PANE_ID")
    if propio:
        return propio
    ok, out = run_cmd(["herdr", "pane", "list"], timeout=30)
    data = _json(out) if ok else None
    panes = ((data or {}).get("result") or {}).get("panes") or []
    if not panes:
        raise UsageIlegible("no hay pane de herdr desde el cual abrir el de "
                            "/usage (¿está corriendo el server de herdr?): "
                            "{}".format((out or "").strip()[:200]))
    enfocado = [p for p in panes if p.get("focused")]
    return (enfocado or panes)[0].get("pane_id")


def capturar(run_cmd, pane_base=None, cwd=None, comando="claude",
             dormir=time.sleep, arranque_ms=60000, usage_ms=45000,
             refresco_max=15, lineas=120):
    """El texto de `/usage`, leído de un pane temporal de herdr. Devuelve
    `(texto, avisos)`; lanza `UsageIlegible` con el paso que falló.

    El pane se cierra SIEMPRE (Escape + `pane close`), también si algo
    falla o explota en el medio; un cierre que falla queda en `avisos`.
    Las esperas son `herdr pane wait-output`; el único poll es el de
    "Refreshing…" (los buckets por modelo llegan un instante después)."""
    avisos = []
    split = ["herdr", "pane", "split"]
    split += ["--pane", pane_base] if pane_base else ["--current"]
    split += ["--direction", "right"]
    if cwd:
        split += ["--cwd", str(cwd)]
    split += ["--no-focus"]
    ok, out = run_cmd(split, timeout=60)
    pane = ((((_json(out) or {}).get("result") or {}).get("pane") or {})
            .get("pane_id")) if ok else None
    if not pane:
        raise UsageIlegible("no se pudo abrir el pane de herdr: {}".format(
            (out or "").strip()[:200]))

    def esperar(regex=None, match=None, ms=30000):
        args = ["herdr", "pane", "wait-output", pane]
        args += ["--regex", regex] if regex else ["--match", match]
        args += ["--timeout", str(ms)]
        ok, out = run_cmd(args, timeout=ms / 1000 + 15)
        return ok and "error" not in (_json(out) or {})

    def leer():
        ok, out = run_cmd(["herdr", "pane", "read", pane, "--lines",
                           str(lineas), "--source", "recent-unwrapped"],
                          timeout=30)
        if not ok:
            raise UsageIlegible("no se pudo leer el pane {}: {}".format(
                pane, (out or "").strip()[:200]))
        return out

    try:
        ok, out = run_cmd(["herdr", "pane", "run", pane, comando], timeout=30)
        if not ok:
            raise UsageIlegible("no se pudo correr {!r} en el pane: {}".format(
                comando, (out or "").strip()[:200]))
        if not esperar(regex=LISTO_RE, ms=arranque_ms):
            raise UsageIlegible("claude no arrancó en {}s en el pane {}".format(
                arranque_ms // 1000, pane))
        if CONFIANZA_RE.search(leer()):
            raise UsageIlegible(
                "claude pide confiar en la carpeta {} (diálogo de trust): "
                "aceptarla a mano una vez o usar otra cwd".format(cwd))
        run_cmd(["herdr", "pane", "send-text", pane, "/usage"], timeout=30)
        run_cmd(["herdr", "pane", "send-keys", pane, "Enter"], timeout=30)
        if not esperar(match="Current week", ms=usage_ms):
            raise UsageIlegible("/usage no mostró 'Current week' en {}s".format(
                usage_ms // 1000))
        # wait-output ya vio "Current week", pero el primer read puede
        # devolver la pantalla de antes (visto en legion), y los buckets
        # por modelo llegan después de "Refreshing…": se relee.
        texto = leer()
        for _ in range(refresco_max):
            if "Refreshing" not in texto and HEADER_SESION in texto:
                break
            dormir(1)
            texto = leer()
        else:
            if "Refreshing" in texto:
                avisos.append("/usage seguía en 'Refreshing…' tras {}s: los "
                              "números pueden ser los de antes".format(refresco_max))
        return texto, avisos
    finally:
        run_cmd(["herdr", "pane", "send-keys", pane, "esc"], timeout=30)
        ok, out = run_cmd(["herdr", "pane", "close", pane], timeout=30)
        if not ok:
            ok, out = run_cmd(["herdr", "pane", "close", pane], timeout=30)
        if not ok:
            avisos.append("no se pudo cerrar el pane {}: {}".format(
                pane, (out or "").strip()[:200]))


# ----------------------------------------------------------------- política
@dataclass
class PoliticaCuota:
    """Los umbrales de `harness run` (config: `quota.*`, docs/harness/quota.md).

    - `umbral_sesion`: fracción de la sesión de 5h desde la cual no se lanza
      Claude; si sólo queda Claude, se espera su reset.
    - `reserva_diaria`: fracción de la semana que se deja libre por cada día
      que falta al reset (para el 9-5 y el resto). Mientras más cerca del
      reset, menos reserva: más agresivo.
    - `costo_ticket`: fracción de la semana que se estima que se come un
      ticket de Claude (peldaño 3/4); dimensiona la tanda.
    - `hora_limite`: "HH:MM" en America/Santiago; la espera del reset de la
      sesión no pasa de acá.
    """

    umbral_sesion: float = 0.90
    reserva_diaria: float = 0.08
    costo_ticket: float = 0.02
    hora_limite: str = "07:00"


@dataclass
class EstadoCuota:
    """Lo que se sabe de la cuota ahora. `fuente`: "usage" (real, de la
    cuenta), "estimacion" (`harness quota`, local: sin sesión de 5h) o
    "sin dato". Los % son 0-100."""

    fuente: str
    sesion_pct: Optional[float] = None
    sesion_reset: Optional[datetime] = None
    semana_pct: Optional[float] = None
    semana_reset: Optional[datetime] = None
    motivo: str = ""


@dataclass
class DecisionCuota:
    claude: bool                       # ¿se puede lanzar Claude ahora?
    max_claude: Optional[int]          # tickets de Claude en la tanda; None = sin tope
    esperar_hasta: Optional[datetime]  # sesión llena: hasta cuándo esperar
    motivo: str


def proxima_hora_limite(ahora, hora_limite):
    """La próxima vez (estrictamente después de `ahora`) que llega
    `hora_limite` ("HH:MM") en America/Santiago."""
    h, m = (int(x) for x in hora_limite.split(":"))
    a = ahora.astimezone(TZ)
    dt = datetime(a.year, a.month, a.day, h, m, tzinfo=TZ)
    if dt <= a:
        d = a + timedelta(days=1)
        dt = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
    return dt


def _hhmm(dt):
    return dt.astimezone(TZ).strftime("%H:%M") if dt else "?"


def decidir(estado, ahora, politica):
    """Qué Claude se lanza con este estado de cuota. Puro.

    1. Sesión de 5h >= umbral: nada de Claude. Si resetea antes de la hora
       límite, `esperar_hasta` = el reset; si no (o sin reset), no se espera.
    2. Semana: queda `1 - usado`; se reserva `reserva_diaria` por día al
       reset; lo que sobra (`holgura`) dividido `costo_ticket` es el tope de
       tickets de Claude. Sin holgura, nada de Claude. Más cerca del reset
       o con más cuota sin usar -> más tickets.
    3. Sin dato de la semana: sin tope (lo de antes de leer /usage).

    Una sesión cuyo reset ya pasó no cuenta: ese % es de la ventana
    anterior (revisión de #159)."""
    sesion_vigente = (estado.sesion_reset is None
                      or estado.sesion_reset > ahora)
    if (estado.sesion_pct is not None and sesion_vigente
            and estado.sesion_pct >= politica.umbral_sesion * 100):
        base = "sesión de 5h al {}% (umbral {:.0f}%)".format(
            estado.sesion_pct, politica.umbral_sesion * 100)
        limite = proxima_hora_limite(ahora, politica.hora_limite)
        reset = estado.sesion_reset
        if reset is None:
            return DecisionCuota(False, 0, None,
                                 base + ", sin hora de reset: no se espera")
        if reset > limite:
            return DecisionCuota(
                False, 0, None,
                "{}: resetea {}, después de la hora límite {}: Claude no "
                "corre más en esta corrida".format(base, _hhmm(reset),
                                                   politica.hora_limite))
        return DecisionCuota(False, 0, reset,
                             "{}: resetea {}".format(base, _hhmm(reset)))
    if estado.semana_pct is None:
        return DecisionCuota(True, None, None,
                             "sin dato de la semana: sin tope por cuota")
    restante = max(0.0, 1 - estado.semana_pct / 100)
    if estado.semana_reset is not None:
        dias = max(0.0, (estado.semana_reset - ahora).total_seconds() / 86400)
    else:
        dias = 7.0
    reserva = politica.reserva_diaria * dias
    holgura = restante - reserva
    base = ("semana al {}%, resetea en {:.1f} días: reserva {:.0f}%, holgura "
            "{:.0f}%".format(estado.semana_pct, dias, reserva * 100,
                             max(holgura, 0) * 100))
    if holgura <= 0:
        return DecisionCuota(False, 0, None, base + ": sin Claude")
    maximo = int(math.floor(holgura / politica.costo_ticket + 1e-9))
    if maximo <= 0:
        return DecisionCuota(False, 0, None, base + ": no alcanza un ticket")
    return DecisionCuota(True, maximo, None,
                         "{}: hasta {} ticket(s) de Claude".format(base, maximo))


def es_claude(job):
    return (getattr(job, "kind", "") or "") == "claude"


def esperar_hasta(hasta, ahora, dormir, freno=None, paso=300.0):
    """Duerme hasta `hasta` en tramos de `paso` segundos, mirando el freno
    entre tramos. True si llegó; False si apareció el freno."""
    while True:
        if freno and Path(freno).exists():
            return False
        falta = (hasta - ahora()).total_seconds()
        if falta <= 0:
            return True
        dormir(min(paso, falta))


# Después del reset de la sesión se espera un poco antes de volver a
# consultar: el servidor tarda en reflejar la ventana nueva.
MARGEN_RESET = timedelta(seconds=60)


def aplicar_cuota(candidatos, cuota, ahora, dormir, anotar, freno=None,
                  max_esperas=3):
    """Los candidatos de la tanda, filtrados por la cuota de Claude.
    Devuelve `(entran, fuera)` con `fuera = [(job, motivo)]`.

    Sin candidatos de Claude no se consulta nada (Qwen no gasta cuota). Con
    la sesión de 5h llena: si hay trabajo que no es Claude, Claude queda
    fuera de esta tanda y la noche sigue; si sólo hay Claude, se espera el
    reset (tope: la hora límite, y el freno) y se vuelve a consultar."""
    if not any(es_claude(j) for j in candidatos):
        return list(candidatos), []
    d = cuota.decidir()
    for _ in range(max_esperas):
        solo_claude = all(es_claude(j) for j in candidatos)
        if d.claude or d.esperar_hasta is None or not solo_claude:
            break
        hasta = d.esperar_hasta + MARGEN_RESET
        anotar("{}: sólo queda Claude, espero hasta {}".format(
            d.motivo, _hhmm(hasta)))
        if not esperar_hasta(hasta, ahora, dormir, freno):
            anotar("freno puesto durante la espera de la cuota")
            break
        d = cuota.decidir()
    entran, fuera = [], []
    claude_ok = 0
    for j in candidatos:
        if not es_claude(j):
            entran.append(j)
            continue
        if d.claude and (d.max_claude is None or claude_ok < d.max_claude):
            claude_ok += 1
            entran.append(j)
        else:
            motivo = d.motivo if not d.claude else (
                "{} (tope de la tanda alcanzado)".format(d.motivo))
            fuera.append((j, "cuota de Claude: " + motivo))
    return entran, fuera


class Cuota:
    """La cuota de la corrida: la cuota real (statusline o `/usage`)
    primero, la estimación de `harness quota` si no se puede leer (y se
    dice). Se consulta al inicio de cada tanda con Claude y antes de cada
    ticket de Claude.

    Caché corto (`ttl`, 2 min) con lock: los tickets de Claude que arrancan
    en paralelo comparten UNA captura en vez de abrir un pane cada uno
    (revisión de #159); el caché se invalida al pasar cualquier reset.

    `leer_usage() -> Usage` (lanza `UsageIlegible`), `estimar() ->
    EstadoCuota | None`, `ahora() -> datetime`, `anotar(str)` (el log)."""

    def __init__(self, leer_usage, estimar, politica, ahora, anotar,
                 ttl=timedelta(minutes=2)):
        self.leer_usage = leer_usage
        self.estimar = estimar
        self.politica = politica
        self.ahora = ahora
        self.anotar = anotar
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache = None  # (EstadoCuota, cuándo)

    def _cache_vigente(self, ahora):
        if self._cache is None:
            return None
        e, cuando = self._cache
        if ahora - cuando >= self.ttl:
            return None
        for reset in (e.sesion_reset, e.semana_reset):
            if reset is not None and reset <= ahora:
                return None
        return e

    def estado(self):
        with self._lock:
            ahora = self.ahora()
            e = self._cache_vigente(ahora)
            if e is None:
                e = self._consultar()
                self._cache = (e, ahora)
            return e

    def _consultar(self):
        try:
            u = self.leer_usage()
        except UsageIlegible as e:
            est = self.estimar()
            if est is None:
                self.anotar("/usage ilegible ({}); sin estimación de harness "
                            "quota (falta tope_semanal): sin dato de "
                            "cuota".format(e))
                return EstadoCuota(fuente="sin dato", motivo=str(e))
            self.anotar("/usage ilegible ({}); uso la estimación de harness "
                        "quota: semana ~{:.0f}% (local, sin sesión de "
                        "5h)".format(e, est.semana_pct or 0))
            est.motivo = str(e)
            return est
        # La fuente real del dato: la statusline o /usage (revisión #159).
        fuente = (u.sesion.fuente if u.sesion.fuente == u.semana.fuente
                  else "{}+{}".format(u.sesion.fuente, u.semana.fuente))
        etiqueta = "/usage" if fuente == "usage" else fuente
        self.anotar("{}: sesión {}% ({}), semana {}% ({})".format(
            etiqueta, u.sesion.pct, _hhmm(u.sesion.reset), u.semana.pct,
            _cuando(u.semana.reset)))
        return EstadoCuota(fuente=fuente, sesion_pct=u.sesion.pct,
                           sesion_reset=u.sesion.reset,
                           semana_pct=u.semana.pct,
                           semana_reset=u.semana.reset)

    def decidir(self):
        d = decidir(self.estado(), self.ahora(), self.politica)
        self.anotar("decisión: " + d.motivo)
        return d

    def permitir_claude(self):
        """La costura `DispatchSpec.permitir_claude`: se consulta antes de
        cada ticket de Claude (entre tickets)."""
        d = self.decidir()
        return d.claude, d.motivo
