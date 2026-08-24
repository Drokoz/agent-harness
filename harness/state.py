"""El estado del historial: un reductor puro sobre el log de eventos.

El log de `harness.dispatch` ya es un event store append-only, pero hasta
hoy sólo se escribe: sin leerlo no hay forma de saber que un ticket ya
falló dos veces ni con qué runner. Este módulo lo lee y contesta las
preguntas sobre el historial: `de_ticket` (qué le pasó a un ticket),
`tasas` (éxito/abandono por runner y por repo), `duracion_media`
(minutos por ticket cerrado, por runner) e `intentos_que_escalan`
(cuántos abandonos consumen un peldaño de la escalera de reintentos,
#38); y da el estado de la frontera (`estado_frontier`, ticket #43): qué
tickets fueron despachados, y cuáles de esa marca siguen vivos (con
agente en el worktree) y cuáles no.

Puro: los eventos entran como la lista de dicts que da
`harness.summary.leer_eventos`, y no se toca nada más — ni disco, ni red,
ni herdr, ni git. Los tests corren con dicts construidos a mano.

Esquema de línea: cada evento lleva `run_id` (una corrida del
dispatcher), `ticket` ("repo#issue"; None en las líneas que no son de un
ticket, p.ej. `corrida`) y `attempt` (el intento global del ticket: el
primer intento vale 1). Las líneas viejas, escritas antes de estas
claves, se leen igual: se reconocen por `ref` ("ticket/N"), no tienen
forma de distinguir el repo, y todas cuentan como un solo intento.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional

from harness.summary import COSTO_JOB_RE, parse_fecha

# Corrida sintética para las líneas viejas (sin `run_id`): todas entran
# en la misma, así que un ticket viejo cuenta como un intento.
_SIN_RUN = "__sin_run_id__"

# El cuerpo de la línea "agente" lleva el kind del agente —el runner del
# ticket—: "koku-7 (kind pi, pane w9:p1)".
KIND_RE = re.compile(r"kind ([A-Za-z0-9_.-]+),")


# ------------------------------------------------------------------- claves
def _ticket_clave(repo, issue):
    return "{}#{}".format(repo, issue)


def _clave_de(e):
    """(run_id, ticket) del evento para agrupar, o None si no es de ticket.

    Las líneas nuevas traen el ticket en `ticket`; las viejas, en `ref`
    ("ticket/N"), que además no permite saber el repo.
    """
    t = e.get("ticket")
    if t is None:
        ref = str(e.get("ref", ""))
        if not ref.startswith("ticket/"):
            return None
        t = ref
    return (e.get("run_id") or _SIN_RUN, t)


def _es_de(e, repo, issue):
    """¿Este evento es del ticket (repo, issue)?

    Con las líneas viejas, que no traen repo, dos repos que compartan el
    número de issue se mezclan: es la limitación del esquema anterior, no
    del reductor.
    """
    if "ticket" in e:
        return e.get("ticket") == _ticket_clave(repo, issue)
    return e.get("ref") == "ticket/{}".format(issue)


def _runners(eventos):
    """{(run_id, ticket): runner} desde las líneas `agente` del log."""
    m = {}
    for e in eventos:
        if e.get("tipo") != "agente":
            continue
        mkt = KIND_RE.search(str(e.get("cuerpo", "")))
        clave = _clave_de(e)
        if mkt and clave is not None:
            m[clave] = mkt.group(1)
    return m


# ------------------------------------------------------------------- reductores
def intentos(eventos, repo, issue):
    """Cuántas corridas intentaron este ticket: un `run_id` es una corrida.

    Las líneas viejas (sin `run_id`) entran todas en la misma corrida, y
    cuentan como intento 1. El dispatcher usa esto para numerar el
    siguiente intento antes de despachar."""
    runs = set()
    for e in eventos:
        if _es_de(e, repo, issue):
            runs.add(e.get("run_id") or _SIN_RUN)
    return len(runs)


@dataclass
class EstadoTicket:
    """Lo que el historial dice de un ticket.

    `duracion_min` es la duración acumulada: por corrida, desde su primer
    evento hasta el último. `costo` es el costo acumulado de los jobs
    (líneas `costo` "$0.0560"); el delta de créditos de la corrida no se
    suma: mide el mismo gasto por otra vía.
    """

    intentos: int = 0
    ultimo_motivo: Optional[str] = None
    ultimo_runner: Optional[str] = None
    duracion_min: float = 0.0
    costo: float = 0.0


def de_ticket(eventos, repo, issue):
    """El estado acumulado de un ticket en el historial: cuántos intentos,
    con qué motivo murió el último, con qué runner, cuánto duró en total y
    cuánto costó."""
    ultimo_motivo = None
    ultimo_runner = None
    costo = 0.0
    por_run = {}
    for e in eventos:
        if not _es_de(e, repo, issue):
            continue
        tipo, cuerpo = e.get("tipo"), str(e.get("cuerpo", ""))
        if tipo == "abandono":
            ultimo_motivo = cuerpo
        elif tipo == "agente":
            mkt = KIND_RE.search(cuerpo)
            if mkt:
                ultimo_runner = mkt.group(1)
        elif tipo == "costo":
            m = COSTO_JOB_RE.match(cuerpo.strip())
            if m:
                costo += float(m.group(1))
        dt = parse_fecha(str(e.get("timestamp", "")))
        if dt is not None:
            run = e.get("run_id") or _SIN_RUN
            a, b = por_run.get(run, (dt, dt))
            por_run[run] = (min(a, dt), max(b, dt))
    duracion = sum((fin - ini).total_seconds() / 60.0
                   for ini, fin in por_run.values())
    return EstadoTicket(intentos=intentos(eventos, repo, issue),
                        ultimo_motivo=ultimo_motivo,
                        ultimo_runner=ultimo_runner,
                        duracion_min=duracion,
                        costo=costo)


def intentos_que_escalan(eventos, repo, issue):
    """Cuántos intentos de este ticket consumen un peldaño de la escalera
    de reintentos (#38): los que terminan en abandono, salvo los
    clasificados `infra` -- esos se reintentan en el acto (#37) y no
    cuentan. Una corrida sin línea `abandono` (todavía en curso, o
    cerrada con `pr`) tampoco cuenta: nada que escalar.

    La clase de una línea vieja (sin `clase`, escrita antes de #37) se
    lee como `modelo`: el default conservador es el que gasta, no el
    que reintenta gratis.
    """
    clase_por_run = {}
    for e in eventos:
        if not _es_de(e, repo, issue) or e.get("tipo") != "abandono":
            continue
        run = e.get("run_id") or _SIN_RUN
        clase_por_run[run] = e.get("clase") or "modelo"
    return sum(1 for clase in clase_por_run.values() if clase != "infra")


def motivos_de_abandono(eventos, repo, issue):
    """Los motivos de cada abandono de este ticket (línea `abandono`), en el
    orden del historial: lo que `Dispatcher.parkear` comenta al agotar la
    escalera (#38), para que un humano no tenga que releer el log."""
    return [str(e.get("cuerpo", "")) for e in eventos
           if _es_de(e, repo, issue) and e.get("tipo") == "abandono"]


def ultimo_gate_rojo(eventos, repo, issue):
    """La cola del gate rojo más reciente de este ticket (sin el prefijo
    "rojo:"), o None si no hubo ninguno: lo que el peldaño 2 de la escalera
    (#38) inyecta en el prompt del intento siguiente. Un abandono que nunca
    llegó a correr el gate (árbol sucio, timeout, sin PR) no deja rastro
    acá, y el peldaño 2 sigue sin la cola -- `prompt_de` ya sabe omitirla."""
    ultimo = None
    for e in eventos:
        if not _es_de(e, repo, issue) or e.get("tipo") != "gate":
            continue
        cuerpo = str(e.get("cuerpo", ""))
        if cuerpo.startswith("rojo:"):
            ultimo = cuerpo[len("rojo:"):].strip()
    return ultimo


def _agregar(tabla, grupo, clave):
    c = tabla.setdefault(grupo, {"exito": 0, "abandono": 0})
    c[clave] += 1


def tasas(eventos):
    """Éxito (líneas `pr`) y abandono (líneas `abandono`), por runner y por
    repo.

    El runner del evento es el de su corrida y ticket (el kind de la línea
    `agente`); sin forma de saberlo, va en "desconocido". El repo sale de
    `ticket` ("repo#issue"): las líneas viejas no lo traen, así que entran
    en `por_runner` y no en `por_repo`.
    """
    runners = _runners(eventos)
    por_runner: Dict[str, Dict[str, int]] = {}
    por_repo: Dict[str, Dict[str, int]] = {}
    for e in eventos:
        if e.get("tipo") not in ("pr", "abandono"):
            continue
        clave = "exito" if e["tipo"] == "pr" else "abandono"
        k = _clave_de(e)
        _agregar(por_runner, runners.get(k, "desconocido"), clave)
        if k is not None and not k[1].startswith("ticket/"):
            _agregar(por_repo, k[1].rsplit("#", 1)[0], clave)
    return {"por_runner": por_runner, "por_repo": por_repo}


def duracion_media(eventos, runner):
    """Minutos por ticket cerrado (línea `pr`) corrido por `runner`.

    La duración de un cierre es desde el primer evento de esa corrida del
    ticket hasta la línea `pr`. Sin cierres del runner: 0.0.
    """
    runners = _runners(eventos)
    primero = {}
    for e in eventos:
        k = _clave_de(e)
        if k is None:
            continue
        dt = parse_fecha(str(e.get("timestamp", "")))
        if dt is not None and k not in primero:
            primero[k] = dt
    durs = []
    for e in eventos:
        if e.get("tipo") != "pr":
            continue
        k = _clave_de(e)
        if k is None or runners.get(k) != runner:
            continue
        fin = parse_fecha(str(e.get("timestamp", "")))
        ini = primero.get(k)
        if fin is not None and ini is not None:
            durs.append((fin - ini).total_seconds() / 60.0)
    return sum(durs) / len(durs) if durs else 0.0


# ------------------------------------------------------------------ frontera
# Estados de un ticket de la frontera (ticket #43): la frontera es un estado,
# no un booleano. `libre` es el único que se despacha.
LIBRE = "libre"
DESPACHADO = "despachado"
PR_ABIERTO = "pr-abierto"
MERGEADO = "mergeado"
PARKEADO = "parkeado"

# Etiquetas del triage que parcean un ticket: un humano lo toma, la flota no.
# `ready-for-human` es la que el dispatcher marca cuando el PR no se acepta;
# `wontfix` es el "no se hace" explícito. Un ticket parkeado no vuelve, aunque
# siga abierto.
PARKEADOS = ("ready-for-human", "wontfix")


def rama_de(issue):
    """La rama que el dispatcher crea para un ticket: `ticket/<n>`."""
    return "ticket/{}".format(issue)


def despachado(eventos, repo, issue):
    """¿El log registra que un agente ARRANCÓ sobre este ticket?

    La marca es la línea `agente` exitosa (la que trae el kind, `KIND_RE`):
    "koku-7 (kind pi, pane w9:p1)". Las fallas de arranque ("fallo: ...") no
    cuentan: sobre ese ticket nunca hubo trabajo.
    """
    for e in eventos or []:
        if (e.get("tipo") == "agente" and _es_de(e, repo, issue)
                and KIND_RE.search(str(e.get("cuerpo", "")))):
            return True
    return False


def agente_vivo(agentes, repo, issue):
    """¿Un agente de herdr vive ahora en el worktree del ticket?

    El worktree de un ticket se llama `<repo>-ticket-<n>`
    (`harness.dispatch.worktree_path`), y herdr trae el cwd de cada agente:
    no hay que adivinar por nombres.
    """
    objetivo = "{}-ticket-{}".format(repo, issue)
    for a in agentes or []:
        if not isinstance(a, dict):
            continue
        cwd = str(a.get("cwd") or "").rstrip("/")
        if cwd.rsplit("/", 1)[-1] == objetivo:
            return True
    return False


def estado_frontier(labels, despachado_en_log, vivo, pr_abierto, pr_merged):
    """El estado de un ticket de la frontera.

    `parkeado` es definitivo (un humano lo tiene); `pr-abierto` y `mergeado`
    salen de los PRs sobre `ticket/<n>`; `despachado` sale del log de eventos
    y de un agente vivo — despachado en una corrida muerta (sin PR y sin
    agente) no queda colgado: vuelve a `libre`."""
    if set(labels or ()) & set(PARKEADOS):
        return PARKEADO
    if pr_abierto:
        return PR_ABIERTO
    if pr_merged:
        return MERGEADO
    if despachado_en_log and vivo:
        return DESPACHADO
    return LIBRE
