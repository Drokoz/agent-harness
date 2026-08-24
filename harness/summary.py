"""El resumen de la mañana: "qué pasó desde la última vez que miré" (ticket #7).

La fuente es el log JSONL de eventos del dispatcher (ver `harness/dispatch.py`),
más los PRs abiertos que trae el snapshot en vivo. La lógica pura vive acá:
leer el log, filtrar el período, contar lo que cuenta. La marca de "última vez
que miré" es un archivo junto al log (misma carpeta de estado); el CLI la
actualiza cada vez que muestra el resumen, salvo cuando `--since` mira
otro período a propósito.

Acá no se decide nada de dibujo (eso es `harness.render`) ni de CLI: las
funciones toman lo que se les pasa y devuelven lo que cuentan. `leer_eventos`
no se rompe con líneas mal escritas ni con el log inexistente: el log vacío
significa "no pasó nada", no un error.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

# Costo de job: el cuerpo de la línea "costo" es exactamente "$0.0560".
# El delta de créditos de la corrida ("creditos antes ... / delta $0.2") NO
# se suma: mide el mismo gasto por otra vía y sumar ambos lo contaría dos veces.
COSTO_JOB_RE = re.compile(r"^\$(\d+(?:\.\d+)?)$")

# El PR de un evento "pr" viene en el cuerpo: "PR #19 abierto (gate verde)".
PR_NUM_RE = re.compile(r"PR #(\d+)")

# El estado en vivo de `gh pr view` (OPEN/MERGED/CLOSED) a como se muestra.
ESTADOS_VIVOS = {"OPEN": "abierto", "MERGED": "mergeado", "CLOSED": "cerrado"}


@dataclass
class Ticket:
    """Un ticket cuyo trabajo quedó hecho: el dispatcher lo anota como `pr`.

    `estado` es lo que dice el log al momento de escribirlo: siempre
    "abierto", porque el merge es humano y no deja evento. `reconciliar` lo
    corrige contra GitHub cuando puede; `en_vivo` distingue esa corrección
    de lo que el log todavía cree.

    `costo` (dólares OpenRouter) y `cuota` (tokens de Claude ponderados)
    son el gasto acumulado de TODO el historial del ticket, no sólo del
    período (#47): un ticket que escaló gastó en noches anteriores, y ese
    gasto es parte de lo que costó cerrarlo. `peldanos` es el desglose por
    intento cuando hubo más de uno; ninguno de los tres lo puede calcular
    `resumir` sola —hace falta cruzar contra `harness.quota` y
    `harness.state`— así que entran por `con_cuota`.
    """

    contexto: str
    ref: str
    detalle: str
    repo: Optional[str] = None
    numero: Optional[int] = None
    estado: str = "abierto"
    en_vivo: bool = False
    costo: float = 0.0
    cuota: float = 0.0
    peldanos: List[dict] = field(default_factory=list)


@dataclass
class Trabado:
    """Un agente que no terminó: el dispatcher lo anota como `abandono`."""

    contexto: str
    ref: str
    motivo: str


@dataclass
class PrAbierto:
    """Un PR abierto ahora, traído por el snapshot, no por el log."""

    repo: str
    number: int
    title: str


@dataclass
class Resumen:
    """Lo que pasó en un período. `estado`: ok | offline.

    `desde` es el inicio del período en ISO; None = desde el principio
    (primera vez que se mira, sin marca guardada).

    `cuota_semana` y `cuota_semana_harness` (tokens de Claude ponderados,
    #47) son la semana de cuota vigente, no el período: `None` cuando no se
    calcularon (offline, o el CLI no tiene de dónde leer sesiones locales),
    para que la pantalla sepa distinguir "no hay dato" de "dio cero". Ver
    `con_cuota`.
    """

    estado: str
    desde: Optional[str] = None
    hasta: Optional[str] = None
    tickets: List[Ticket] = field(default_factory=list)
    trabados: List[Trabado] = field(default_factory=list)
    prs: List[PrAbierto] = field(default_factory=list)
    costo: float = 0.0
    cuota_semana: Optional[float] = None
    cuota_semana_harness: Optional[float] = None

    @property
    def paso_algo(self):
        return bool(self.tickets or self.trabados or self.prs or self.costo > 0)


# --------------------------------------------------------------------- eventos
def parse_evento(linea):
    """Una línea JSONL del log, o None si no coopera.

    El log es append-only y lo escribe el dispatcher; una línea rota (disk
    lleno, corte a medio escribir) no puede tirar el resumen: se salta.
    """
    try:
        e = json.loads(linea)
    except (ValueError, TypeError):
        return None
    if not isinstance(e, dict):
        return None
    if parse_fecha(str(e.get("timestamp", ""))) is None:
        return None
    return e


def leer_eventos(path):
    """Todo el log, en orden, sin líneas rotas. Log inexistente = sin eventos."""
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return []
    return [e for e in (parse_evento(l) for l in raw.splitlines()) if e is not None]


def parse_fecha(s):
    """Una fecha ISO (con o sin hora, con o sin Z) como datetime UTC, o None.

    Python 3.9: `fromisoformat` no entiende la Z, así que se convierte antes.
    Naive = UTC: el log lo escribe en UTC.
    """
    if not s:
        return None
    s = s.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def filtrar(eventos, desde):
    """Los eventos desde `desde` (datetime, inclusive). `desde` None = todos."""
    if desde is None:
        return list(eventos)
    out = []
    for e in eventos:
        dt = parse_fecha(str(e.get("timestamp", "")))
        if dt is not None and dt >= desde:
            out.append(e)
    return out


def _ticket_repo(e):
    """El repo de un evento, del campo `ticket` ("repo#issue"). None cuando
    el evento no lo trae (líneas viejas, u otro tipo de evento)."""
    ticket = e.get("ticket")
    if not isinstance(ticket, str) or "#" not in ticket:
        return None
    return ticket.rsplit("#", 1)[0] or None


def resumir(eventos):
    """(tickets, trabados, costo) de un período ya filtrado.

    `tickets` son los eventos `pr` (job terminado con gate verde y PR abierto),
    `trabados` los `abandono`, y `costo` la suma de los costos por job.
    """
    tickets: List[Ticket] = []
    trabados: List[Trabado] = []
    costo = 0.0
    for e in eventos:
        tipo, ref = e.get("tipo"), e.get("ref", "?")
        ctx, cuerpo = e.get("contexto", "?"), str(e.get("cuerpo", ""))
        if tipo == "pr":
            m = PR_NUM_RE.search(cuerpo)
            numero = int(m.group(1)) if m else None
            tickets.append(Ticket(contexto=ctx, ref=ref, detalle=cuerpo,
                                  repo=_ticket_repo(e), numero=numero))
        elif tipo == "abandono":
            trabados.append(Trabado(contexto=ctx, ref=ref, motivo=cuerpo))
        elif tipo == "costo":
            m = COSTO_JOB_RE.match(cuerpo.strip())
            if m:
                costo += float(m.group(1))
    return tickets, trabados, costo


def reconciliar(tickets, resolver):
    """Los tickets con su estado en vivo, cuando se puede consultar.

    `resolver(repo, numero)` es la única frontera de red: devuelve
    OPEN/MERGED/CLOSED, o None cuando no se pudo (sin red, sin repo o número
    en el evento, PR inexistente). Sin resultado el ticket se queda con lo
    que dice el log —abierto, `en_vivo=False`— en vez de romper el resumen.
    """
    out = []
    for t in tickets:
        vivo = resolver(t.repo, t.numero) if t.repo and t.numero is not None else None
        if vivo in ESTADOS_VIVOS:
            out.append(replace(t, estado=ESTADOS_VIVOS[vivo], en_vivo=True))
        else:
            out.append(t)
    return out


def construir(eventos, prs, estado="ok", desde=None, hasta=None, resolver_pr=None):
    """El `Resumen` de un período ya filtrado, con los PRs abiertos en vivo.

    `resolver_pr`, si se pasa, reconcilia cada ticket contra su estado real
    (ver `reconciliar`): el merge es humano y no deja evento, así que sin
    esto un PR mergeado se sigue mostrando como recién abierto.
    """
    tickets, trabados, costo = resumir(eventos)
    if resolver_pr is not None:
        tickets = reconciliar(tickets, resolver_pr)
    return Resumen(estado=estado, desde=desde, hasta=hasta,
                   tickets=tickets, trabados=trabados, prs=list(prs), costo=costo)


def con_cuota(resumen, cuota_semana=None, cuota_semana_harness=None,
              costos_por_ticket=None, cuotas_por_ticket=None,
              peldanos_por_ticket=None):
    """Un `Resumen` nuevo con la cuota cruzada adentro (#47).

    Nada de esto sale del log solo: la cuota (tokens de Claude) sale de
    `harness.quota` (sesiones locales) y los peldaños de `harness.state`
    (el historial completo del ticket, no sólo el período); el CLI hace
    ese cruce y llama acá con el resultado ya listo. Los tres dicts van por
    `(repo, ref)` — no `(repo, numero)`: `numero` es el PR ("PR #19
    abierto"), y `state.pasos`/`quota.puntos_de_ticket` necesitan el
    issue, que sólo `ref` ("ticket/256") trae confiable. Un ticket sin
    entrada se queda con lo que traía (costo 0.0, cuota 0.0, sin peldaños).
    """
    costos_por_ticket = costos_por_ticket or {}
    cuotas_por_ticket = cuotas_por_ticket or {}
    peldanos_por_ticket = peldanos_por_ticket or {}

    def _enriquecido(t):
        clave = (t.repo, t.ref)
        return replace(t, costo=costos_por_ticket.get(clave, t.costo),
                       cuota=cuotas_por_ticket.get(clave, t.cuota),
                       peldanos=peldanos_por_ticket.get(clave, t.peldanos))

    tickets = [_enriquecido(t) for t in resumen.tickets]
    return replace(resumen, tickets=tickets, cuota_semana=cuota_semana,
                   cuota_semana_harness=cuota_semana_harness)


# ------------------------------------------------------------------------ marca
def leer_marca(path):
    """La marca de "última vez que miré" como datetime UTC, o None."""
    try:
        texto = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return parse_fecha(texto)


def guardar_marca(path, reloj: Callable[[], str]):
    """Guarda la marca con el timestamp que devuelva `reloj` (ISO)."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(reloj(), encoding="utf-8")


# ------------------------------------------------------------------------ JSON
def as_dict(r: Resumen):
    """El resumen para `--json`: la misma forma que el resto del snapshot."""
    return {
        "estado": r.estado,
        "desde": r.desde,
        "hasta": r.hasta,
        "tickets": [asdict(t) for t in r.tickets],
        "trabados": [asdict(t) for t in r.trabados],
        "prs": [asdict(p) for p in r.prs],
        "costo": r.costo,
        "cuota_semana": r.cuota_semana,
        "cuota_semana_harness": r.cuota_semana_harness,
    }
