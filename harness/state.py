"""El estado del historial: un reductor puro sobre el log de eventos.

El log de `harness.dispatch` ya es un event store append-only, pero hasta
hoy sólo se escribe: sin leerlo no hay forma de saber que un ticket ya
falló dos veces ni con qué runner. Este módulo lo lee y contesta las
preguntas sobre el historial: `de_ticket` (qué le pasó a un ticket),
`tasas` (éxito/abandono por runner y por repo), `duracion_media`
(minutos por ticket cerrado, por runner) e `intentos_que_escalan`
(cuántos abandonos consumen un peldaño de la escalera de reintentos,
#38); `costo_promedio` (el costo medio por ticket, con el que el
presupuesto dimensiona la tanda, #68); y da el estado de la frontera (`estado_frontier`, ticket #43): qué
tickets fueron despachados, y cuáles de esa marca siguen vivos (con
agente en el worktree) y cuáles no.

Puro: los eventos entran como la lista de dicts que da
`harness.summary.leer_eventos`, y no se toca nada más — ni disco, ni red,
ni herdr, ni git. Los tests corren con dicts construidos a mano.

Esquema de línea: cada evento lleva `run_id` (una corrida del
dispatcher), `ticket` ("repo#issue"; None en las líneas que no son de un
ticket, p.ej. `corrida`) y `attempt` (el intento global del ticket: el
primer intento vale 1). Las líneas viejas, escritas antes de estas
claves, se leen sin romper, pero sin `ticket` no se pueden atribuir a
un repo: el `ref` ("ticket/N") es idéntico en todos los repos, y dos
repos que compartan el número de issue se contaminarían el conteo de
intentos y la escalera (#72). Así que no cuentan para ningún repo: se
pierde la historia anterior a #35, que es el trueque correcto — un
intento mal atribuido hace escalar de más y gastar cuota.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from harness.summary import COSTO_JOB_RE, parse_fecha

# Corrida sintética para las líneas que no traen `run_id` (en el
# esquema nuevo esto no debería pasar): se agrupan juntas.
_SIN_RUN = "__sin_run_id__"

# El cuerpo de la línea "agente" lleva el kind del agente —el runner del
# ticket—: "koku-7 (kind pi, pane w9:p1)".
KIND_RE = re.compile(r"kind ([A-Za-z0-9_.-]+),")


# ------------------------------------------------------------------- claves
def _ticket_clave(repo, issue):
    return "{}#{}".format(repo, issue)


def _clave_de(e):
    """(run_id, ticket) del evento para agrupar, o None si no se puede
    atribuir: líneas que no son de ticket (p.ej. `corrida`) y líneas
    viejas (sin campo `ticket`, cuyo `ref` no distingue el repo, #72).
    """
    t = e.get("ticket")
    if t is None:
        return None
    return (e.get("run_id") or _SIN_RUN, t)


def _es_de(e, repo, issue):
    """¿Este evento es del ticket (repo, issue)?

    La única clave confiable es `ticket` ("repo#issue"). Las líneas
    viejas no la traen y el `ref` ("ticket/N") es idéntico en todos los
    repos, así que no se pueden atribuir a ningún repo y no cuentan
    para ninguno (#72).
    """
    return e.get("ticket") == _ticket_clave(repo, issue)


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

    Las líneas viejas (sin `ticket`) no cuentan para ningún repo: no se
    pueden atribuir (#72). El dispatcher usa esto para numerar el
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


@dataclass
class Peldano:
    """Un intento (`run_id`) de un ticket escalado: el runner, lo que gastó
    en dólares (líneas `costo`) y la ventana de tiempo del intento.

    `cuota` (tokens ponderados de Claude) queda en None acá: `state` es
    puro sobre el log y no sabe de `harness.quota` (sesiones locales); el
    CLI cruza `inicio`/`fin` contra `quota.cuota_por_ventana` y lo llena
    (#47, ver `bin/harness`).
    """

    attempt: int
    run_id: str
    runner: Optional[str] = None
    motivo: Optional[str] = None
    costo: float = 0.0
    inicio: Optional[datetime] = None
    fin: Optional[datetime] = None
    cuota: Optional[float] = None


def pasos(eventos, repo, issue):
    """Los peldaños de este ticket, uno por corrida (`run_id`), en el orden
    en que aparecen en el log. Cada uno trae su runner, su costo en
    dólares acumulado y la ventana de tiempo de sus eventos —lo que hace
    falta para repartir la cuota entre peldaños (ver `Peldano`)."""
    por_run: Dict[str, dict] = {}
    orden = []
    for e in eventos:
        if not _es_de(e, repo, issue):
            continue
        run = e.get("run_id") or _SIN_RUN
        if run not in por_run:
            por_run[run] = {"attempt": e.get("attempt") or 1, "runner": None,
                            "motivo": None, "costo": 0.0, "inicio": None, "fin": None}
            orden.append(run)
        acc = por_run[run]
        if e.get("attempt") is not None:
            acc["attempt"] = e["attempt"]
        tipo, cuerpo = e.get("tipo"), str(e.get("cuerpo", ""))
        if tipo == "agente":
            mkt = KIND_RE.search(cuerpo)
            if mkt:
                acc["runner"] = mkt.group(1)
        elif tipo == "abandono":
            acc["motivo"] = cuerpo
        elif tipo == "costo":
            m = COSTO_JOB_RE.match(cuerpo.strip())
            if m:
                acc["costo"] += float(m.group(1))
        dt = parse_fecha(str(e.get("timestamp", "")))
        if dt is not None:
            acc["inicio"] = dt if acc["inicio"] is None else min(acc["inicio"], dt)
            acc["fin"] = dt if acc["fin"] is None else max(acc["fin"], dt)
    return [Peldano(attempt=por_run[r]["attempt"], run_id=r, runner=por_run[r]["runner"],
                    motivo=por_run[r]["motivo"], costo=por_run[r]["costo"],
                    inicio=por_run[r]["inicio"], fin=por_run[r]["fin"])
           for r in sorted(orden, key=lambda r: (por_run[r]["attempt"], r))]


def costo_promedio(eventos):
    """El costo medio por ticket del historial (#68): con qué cifra el
    presupuesto dimensiona la tanda.

    Un ticket puede tener varias líneas `costo` (varios intentos, varios
    peldaños): su costo es la suma, y el promedio es sobre tickets, no
    sobre líneas. Dos filtros, por el lado correcto:

    - Sin campo `ticket` no se puede atribuir (línea `corrida`, o línea
      vieja de antes de #35) y no cuenta en ningún lado (#72): la línea
      `costo` de la corrida mide el delta de créditos, no un ticket.
    - Un ticket que en total no gastó (0.0, `de_ticket`) nunca prendió
      agente y su cero diluiría el promedio para abajo: la tanda crecería
      más de lo que la plata alcanza.

    Sin tickets con costo medido: None — no hay historia con qué
    dimensionar, y es el llamador el que dice qué se hace con eso."""
    por_ticket = {}
    for e in eventos:
        if e.get("tipo") != "costo":
            continue
        ticket = e.get("ticket")
        if ticket is None:
            continue
        m = COSTO_JOB_RE.match(str(e.get("cuerpo", "")).strip())
        if not m:
            continue
        por_ticket[ticket] = por_ticket.get(ticket, 0.0) + float(m.group(1))
    gastos = [c for c in por_ticket.values() if c > 0]
    return sum(gastos) / len(gastos) if gastos else None


def intentos_que_escalan(eventos, repo, issue):
    """Cuántos intentos de este ticket consumen un peldaño de la escalera
    de reintentos (#38): los que terminan en abandono, salvo los
    clasificados `infra` -- esos se reintentan en el acto (#37) y no
    cuentan. Una corrida sin línea `abandono` (todavía en curso, o
    cerrada con `pr`) tampoco cuenta: nada que escalar.

    La clase de una línea sin `clase` (escrita entre #35 y #37) se
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
    `ticket` ("repo#issue"); las líneas sin `ticket` no se pueden atribuir
    a ningún repo, así que no cuentan en ningún lado (#72).
    """
    runners = _runners(eventos)
    por_runner: Dict[str, Dict[str, int]] = {}
    por_repo: Dict[str, Dict[str, int]] = {}
    for e in eventos:
        if e.get("tipo") not in ("pr", "abandono"):
            continue
        clave = "exito" if e["tipo"] == "pr" else "abandono"
        k = _clave_de(e)
        if k is None:
            continue
        _agregar(por_runner, runners.get(k, "desconocido"), clave)
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
