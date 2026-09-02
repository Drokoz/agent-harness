"""El costo real por peldaño de la escalera (#96).

La pregunta que hay que poder contestar mirando una pantalla: ¿cuánto cuesta,
en promedio, cerrar un ticket en el peldaño 1 contra el 2 contra Sonnet, y a
partir de qué intento deja de convenir insistir?

El dato sale de cruzar dos fuentes que hasta hoy no se hablaban:

- Las **sesiones de pi** (`harness/costo_pi.py`, #90): el costo por mensaje y
  los tokens crudos, con la regla de que dos sesiones en el mismo worktree
  son dos intentos.
- El **log de eventos** (`harness/state.py`): con quién corrió cada intento
  y cuántos abandonos consumieron peldaño antes de él
  (`state.intentos_que_escalan` + `peldano_de`), y cuáles cerraron (línea
  `pr`).

El peldaño de un intento no se adivina del runner: se deriva del historial,
igual que lo hace el dispatcher — los abandonos anteriores que consumieron
peldaño (los `infra` no, por #37) son los escalones ya gastados.

Los tokens y el costo desconocido siguen la regla de `costo_pi`: el costo
desconocido es `None`, nunca `0.0`; un peldaño sin sesiones medibles no
muestra un promedio de cero, dice que no se pudo medir.

Puro: sesiones y eventos entran como las listas de dicts que dan
`costo_pi.leer_sesiones` y `summary.leer_eventos`, y no se toca disco.
`bin/harness` es el pegamento.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Dict, List

from harness import costo_pi
from harness import state
from harness.dispatch import ESCALERA, peldano_de, peldano_etiqueta
from harness.summary import parse_fecha

# Cuánto se tolera que la sesión arranque antes o termine después de la
# ventana del paso en el log: el dispatcher anota los eventos alrededor de
# la vida del agente, no dentro.
GRACE = timedelta(minutes=5)

# El `run_id` sintético de `state.pasos` para las líneas sin run_id.
_SIN_RUN = "__sin_run_id__"


# ------------------------------------------------------------------- cruces
def _clave(repo, issue):
    return "{}#{}".format(repo, issue)


def _clase_por_run(eventos, clave) -> Dict[str, str]:
    """La clase de cada abandono, por corrida: la misma regla que
    `state.intentos_que_escalan` (sin `clase` se lee como `modelo`)."""
    m: Dict[str, str] = {}
    for e in eventos:
        if e.get("ticket") == clave and e.get("tipo") == "abandono":
            m[e.get("run_id") or _SIN_RUN] = e.get("clase") or "modelo"
    return m


def _runs_cerrados(eventos, clave):
    """Las corridas que terminaron con PR (línea `pr`): el cierre que la
    tasa de éxito cuenta, igual que `state.tasas`."""
    return {e.get("run_id") or _SIN_RUN for e in eventos
            if e.get("ticket") == clave and e.get("tipo") == "pr"}


def _sesiones_del_paso(p, ses, pos, usadas) -> List[dict]:
    """Las sesiones de pi que cayeron en la ventana de este paso.

    Por posición no alcanza: un reintento de infra en el acto (#37) deja DOS
    sesiones en un MISMO paso, y posarlas una por una desfasa todo lo que
    sigue. La ventana del paso (primer y último evento de la corrida) manda,
    y varias sesiones pueden caer en la misma corrida: es lo que el
    watchdog y el reintento en el acto producen en la realidad.

    Sin ventana (pasos sin timestamps legibles) se cae a la posición:
    la `pos`-ésima sesión del ticket es la del `pos`-ésimo paso. Con ventana
    pero sin match NO hay fallback posicional: atribuir la sesión vecina al
    paso equivocado sería inventar el costo de un intento que no corrió.
    """
    if p.inicio is None or p.fin is None:
        if pos < len(ses) and id(ses[pos]) not in usadas:
            return [ses[pos]]
        return []
    inicio, fin = p.inicio - GRACE, p.fin + GRACE
    match = []
    for s in ses:
        if id(s) in usadas:
            continue
        t = parse_fecha(s["inicio"])
        if t is not None and inicio <= t <= fin:
            match.append(s)
    return match


def ticket_de(sesiones, eventos, repo, issue) -> dict:
    """El historial de un ticket, con el costo real pegado a cada intento.

    Cada intento (corrida del log, `state.pasos`) lleva: el peldaño en que
    arrancó (derivation de `intentos_que_escalan`, la misma que el
    dispatcher), el costo medido de las sesiones de pi que cayeron en su
    ventana (`None` si no se pudo medir, nunca 0.0), los tokens crudos y el
    resultado (cerró / abandono con clase / en curso)."""
    clave = _clave(repo, issue)
    pasos = state.pasos(eventos, repo, issue)
    ses = costo_pi.intentos_de(sesiones, repo, issue)
    cerrados = _runs_cerrados(eventos, clave)
    clases = _clase_por_run(eventos, clave)
    escalados = 0
    usadas: set = set()
    intentos = []
    for pos, p in enumerate(pasos):
        peldano = peldano_de(escalados)
        match = _sesiones_del_paso(p, ses, pos, usadas)
        for s in match:
            usadas.add(id(s))
        costos = [s["costo"] for s in match if s["costo"] is not None]
        tokens = None
        if match:
            tokens = {c: sum(s["tokens"][c] for s in match)
                      for c in costo_pi.CAMPOS}
        if p.run_id in cerrados:
            resultado, clase = "cerrado", None
        elif p.motivo is not None:
            resultado = "abandono"
            clase = clases.get(p.run_id, "modelo")
            if clase != "infra":
                escalados += 1
        else:
            resultado, clase = "en-curso", None
        intentos.append({
            "attempt": p.attempt,
            "run_id": p.run_id,
            "runner": p.runner,
            "peldano": (ESCALERA.index(peldano) + 1) if peldano else None,
            "peldano_etiqueta": (
                peldano_etiqueta(peldano["kind"], peldano.get("model", ""),
                                 peldano.get("extra_args", ()))
                if peldano else None),
            "costo": sum(costos) if costos else None,
            "tokens": tokens,
            "resultado": resultado,
            "clase": clase,
        })
    sin_corrida = [s["path"] for s in ses if id(s) not in usadas]
    totales = [i["costo"] for i in intentos if i["costo"] is not None]
    return {
        "ticket": clave,
        "intentos": intentos,
        "costo": sum(totales) if totales else None,
        "sesiones_sin_corrida": sin_corrida,
    }


def _tickets_conocidos(sesiones, eventos):
    """Los (repo, issue) que hay con qué mirar: los que aparecen en las
    sesiones, los que aparecen en el log. Las claves del mantenedor
    ("repo#pr<N>") no son tickets y no se meten acá."""
    vistos = {}
    for s in sesiones:
        vistos[(s["repo"], s["issue"])] = True
    for e in eventos:
        t = e.get("ticket")
        if not t:
            continue
        repo, _, n = t.rpartition("#")
        if repo and n.isdigit():
            vistos[(repo, int(n))] = True
    return sorted(vistos)


def vista(sesiones, eventos) -> dict:
    """Lo que `harness costo` muestra: por ticket y por intento, y el
    agregado por peldaño de la escalera con su tasa de éxito al lado."""
    tickets = {}
    for repo, issue in _tickets_conocidos(sesiones, eventos):
        tickets[_clave(repo, issue)] = ticket_de(sesiones, eventos, repo, issue)
    return {"tickets": tickets, "peldanos": por_peldano(tickets)}


def por_peldano(tickets) -> Dict[int, dict]:
    """El promedio que ordena la escalera (#38): costo medio, cuántos
    intentos corrieron ahí y cuántos cerraron.

    Un peldaño barato que nunca cierra no es barato: la tasa va al lado del
    costo, no en un pie de página. El promedio es sobre los intentos con
    costo MEDIDO: sin sesiones en el disco el peldaño existe (corrió,
    gastó) pero no se puede promediar, y `costo_medio` se queda en None en
    vez de inventar un cero."""
    tabla: Dict[int, dict] = {}
    for t in tickets.values():
        for i in t["intentos"]:
            n = i["peldano"]
            if n is None:
                continue
            d = tabla.setdefault(n, {"intentos": 0, "cerrados": 0,
                                      "con_costo": 0, "suma_costo": 0.0,
                                      "tokens": {c: 0 for c in costo_pi.CAMPOS}})
            d["intentos"] += 1
            if i["resultado"] == "cerrado":
                d["cerrados"] += 1
            if i["costo"] is not None:
                d["con_costo"] += 1
                d["suma_costo"] += i["costo"]
            if i["tokens"]:
                for c in costo_pi.CAMPOS:
                    d["tokens"][c] += i["tokens"][c]
    out: Dict[int, dict] = {}
    for n, d in sorted(tabla.items()):
        d = dict(d)
        d["costo_medio"] = (d["suma_costo"] / d["con_costo"]
                            if d["con_costo"] else None)
        d["tasa_exito"] = d["cerrados"] / d["intentos"]
        out[n] = d
    return out


# ----------------------------------------------------------------- pantalla
def _fmt_costo(c) -> str:
    return "${:.4f}".format(c) if c is not None else "s/m"


def _fmt_tokens(t) -> str:
    return "{}k".format(round(t / 1000.0, 1)) if t >= 1000 else str(t)


def render(v, color: bool = False) -> str:
    """La pantalla de `harness costo`. Sin datos no hay pantalla: se dice
    qué falta, en vez de imprimir una tabla vacía que se lee como éxito."""
    tickets = v["tickets"]
    peldanos = v["peldanos"]
    if not tickets:
        return ("Costo: sin datos. Hace falta una corrida con sesiones de pi "
                "y/o un log de eventos.\n")
    out = []
    a = out.append
    a("Costo real · escalera de reintentos")
    a("")
    a("  por peldaño:")
    if not peldanos:
        a("    sin intentos con peldaño (¿corrida con --kind a mano?)")
    for n, d in peldanos.items():
        a("    {} — intentos {} · cerró {} ({:.0f}%) · {} medio "
          "({}/{})".format(
              peldano_etiqueta(*_peldaño(n)), d["intentos"], d["cerrados"],
              100.0 * d["tasa_exito"], _fmt_costo(d["costo_medio"]),
              d["con_costo"], d["intentos"]))
    a("")
    a("  por ticket:")
    for clave in sorted(tickets):
        t = tickets[clave]
        a("    {}  {}".format(clave, _fmt_costo(t["costo"])))
        for i in t["intentos"]:
            det = i["peldano_etiqueta"] or "sin peldaño"
            cola = {"cerrado": "cerró",
                    "abandono": "abandono ({})".format(i["clase"]),
                    "en-curso": "en curso"}.get(i["resultado"], i["resultado"])
            tok = ""
            if i["tokens"]:
                tok = " · {}".format(_fmt_tokens(i["tokens"]["input"]))
            a("      intento {} · {} · {}{} · {}".format(
                i["attempt"], det, _fmt_costo(i["costo"]), tok, cola))
        for ruta in t["sesiones_sin_corrida"]:
            a("      (sesión sin corrida en el log: {})".format(ruta))
    a("")
    return "\n".join(out) + "\n"


def _peldaño(n):
    """(kind, model, extra_args) del peldaño `n` de `ESCALERA`."""
    return ESCALERA[n - 1]["kind"], ESCALERA[n - 1].get("model", ""), \
        ESCALERA[n - 1].get("extra_args", ())
