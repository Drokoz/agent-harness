"""Ninguna corrida real sin plan aprobado (#176).

La noche del 2026-10-18 la corrida nocturna se programó sin que el dueño la
hubiera autorizado y gastó US$17 de un saldo que no estaba destinado a eso.
Nada exigía que alguien hubiera visto qué iba a correr, con qué modelo y a
qué costo.

El plan es lo que una corrida despacharía, calculado sin efectos
(`harness run --plan`): por ticket, repo, número, runner, modelo, peldaño,
el porqué de la elección (`elegir_peldano`) y el costo estimado. Se guarda
en `<estado>/harness/plan-FECHA.json` con un hash; `harness aprobar` deja
`<estado>/harness/aprobado-FECHA` con ese hash adentro (la misma ruta que
mira el lanzador `harness-noche`), y `harness run` no arranca si el plan
que calcula al arrancar no tiene ese hash.

El hash cubre lo que decide plata: el modo (`loop` o `una`), por contexto
la corrida (runner, `--permitir-pi`, `runner_min`), el tope de gasto, la
hora límite, y las entradas (repo, tipo, número, runner, modelo, args del
peldaño). No cubre timestamps, títulos, motivos ni estimados: el costo
medio del historial se mueve con cada job, y eso no puede invalidar la
aprobación a mitad de la noche (el tope sí está cubierto, y es el techo).

Puro salvo `guardar`/`leer`/`escribir_aprobacion`/`leer_aprobacion`, que
tocan sólo las rutas que reciben. `bin/harness` es el pegamento.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

VERSION = 1

# Los runners que no gastan plata: corren contra la cuota de la suscripción
# de Claude, no contra la key de OpenRouter (`dispatch.RUNNERS_SIN_CREDITOS`).
SIN_PLATA = ("claude",)


class PlanInvalido(Exception):
    """El archivo del plan no existe, no se lee, o su hash no coincide con
    su contenido (alguien lo editó a mano)."""


# -------------------------------------------------------------------- rutas
def ruta_plan(dir_estado, fecha):
    """`<estado>/harness/plan-FECHA.json`."""
    return Path(dir_estado) / "harness" / "plan-{}.json".format(fecha)


def ruta_aprobado(dir_estado, fecha):
    """`<estado>/harness/aprobado-FECHA`: la ruta exacta que mira
    `harness-noche` (`~/.local/state/harness/aprobado-$(date +%F)`)."""
    return Path(dir_estado) / "harness" / "aprobado-{}".format(fecha)


# --------------------------------------------------------------------- hash
def _clave_entrada(e):
    return [str(e["repo"]), str(e.get("tipo") or "ticket"), int(e["numero"]),
            str(e["kind"]), str(e.get("model") or ""),
            [str(a) for a in (e.get("extra_args") or ())]]


def _usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    return "{:.2f}".format(v) if math.isfinite(v) else "?"


def contenido(plan):
    """Lo que el hash cubre: lo que decide plata, ordenado, sin timestamps,
    títulos, motivos ni estimados."""
    ctxs = []
    for c in plan.get("contextos") or ():
        corrida = c.get("corrida") or {}
        ctxs.append({
            "nombre": str(c.get("nombre")),
            "corrida": {
                "kind": str(corrida.get("kind") or ""),
                "permitir_pi": bool(corrida.get("permitir_pi")),
                "runner_min": sorted(
                    [str(k), str(v)]
                    for k, v in (corrida.get("runner_min") or {}).items()),
            },
            "max_usd": _usd(c.get("max_usd")),
            "hasta": str(c.get("hasta") or ""),
            "entradas": sorted(_clave_entrada(e)
                               for e in c.get("entradas") or ()),
        })
    ctxs.sort(key=lambda c: c["nombre"])
    return {"version": VERSION, "modo": str(plan.get("modo") or ""),
            "contextos": ctxs}


def hash_de(plan):
    """sha256 del contenido canónico: estable entre reruns, distinto si
    cambia un ticket, el runner, el tope o la hora límite."""
    texto = json.dumps(contenido(plan), sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def sellar(plan):
    """Una copia del plan con su `hash`."""
    p = dict(plan)
    p["hash"] = hash_de(p)
    return p


# ------------------------------------------------------------ lo que cubre
def _contextos(plan, nombre):
    return [c for c in (plan or {}).get("contextos") or ()
            if c.get("nombre") == nombre]


def cubre(plan, contexto, repo, tipo, numero, kind, model):
    """¿Puede salir este job bajo el plan aprobado?

    Claude siempre: no gasta plata, gasta cuota (que tiene sus propios
    frenos). Un job pagado, sólo si el plan lo trae con el mismo repo,
    tipo, número, runner y modelo: un ticket pi que apareció después de
    aprobar no sale. El peldaño de thinking (pi → pi high) no cambia el
    runner ni el modelo: la escalada de un ticket aprobado sigue cubierta.
    Sin plan (`None`), nada pagado."""
    if kind in SIN_PLATA:
        return True
    for c in _contextos(plan, contexto):
        for e in c.get("entradas") or ():
            if (e.get("repo") == repo
                    and (e.get("tipo") or "ticket") == tipo
                    and int(e.get("numero")) == int(numero)
                    and e.get("kind") == kind
                    and (e.get("model") or "") == (model or "")):
                return True
    return False


def techo_usd(plan, contexto):
    """El tope de gasto que el plan aprobó para `contexto` (None si el plan
    no lo trae): la config se relee cada pasada, y subirla a mitad de la
    noche no sube el tope de una corrida aprobada."""
    for c in _contextos(plan, contexto):
        try:
            return float(c.get("max_usd"))
        except (TypeError, ValueError):
            return 0.0
    return None


def pagadas(plan):
    """Las entradas que gastan plata, como (contexto, entrada)."""
    return [(c.get("nombre"), e) for c in (plan or {}).get("contextos") or ()
            for e in c.get("entradas") or ()
            if e.get("kind") not in SIN_PLATA]


def paga(plan):
    """¿Algo de este plan puede gastar plata? Un job pagado, o una corrida
    que usa pi fuera de la escalera (revisor de PRs, proponer)."""
    if pagadas(plan):
        return True
    return any((c.get("corrida") or {}).get("usa_pi")
               for c in (plan or {}).get("contextos") or ())


def sin_plata(plan):
    """El plan de una corrida manual `--sin-aprobacion`: sólo Claude, con
    tope de gasto US$0 en todos sus contextos."""
    p = dict(plan)
    p["contextos"] = [dict(c, max_usd=0.0, entradas=[
        e for e in c.get("entradas") or () if e.get("kind") in SIN_PLATA])
        for c in plan.get("contextos") or ()]
    return p


def diferencias(aprobado, vigente):
    """Lo que cambió entre dos planes, en líneas cortas, para decirle al
    dueño por qué su aprobación ya no vale."""
    def claves(p):
        out = set()
        for c in (p or {}).get("contextos") or ():
            for e in c.get("entradas") or ():
                out.add((c.get("nombre"), e.get("repo"),
                         e.get("tipo") or "ticket", int(e.get("numero")),
                         e.get("kind"), e.get("model") or ""))
        return out
    a, v = claves(aprobado), claves(vigente)
    lineas = []
    for k in sorted(v - a, key=str):
        lineas.append("+ {}: {} #{} ({}) en {} {}".format(*k[:2], k[3], k[2],
                                                           k[4], k[5]))
    for k in sorted(a - v, key=str):
        lineas.append("- {}: {} #{} ({}) en {} {}".format(*k[:2], k[3], k[2],
                                                           k[4], k[5]))
    ca = {c.get("nombre"): c for c in (aprobado or {}).get("contextos") or ()}
    for c in (vigente or {}).get("contextos") or ():
        viejo = ca.get(c.get("nombre"))
        if viejo is None:
            continue
        for campo in ("max_usd", "hasta"):
            if contenido({"contextos": [viejo]})["contextos"][0][campo] != \
                    contenido({"contextos": [c]})["contextos"][0][campo]:
                lineas.append("{}: {} {} → {}".format(
                    c.get("nombre"), campo, viejo.get(campo) or "—",
                    c.get(campo) or "—"))
        if contenido({"contextos": [viejo]})["contextos"][0]["corrida"] != \
                contenido({"contextos": [c]})["contextos"][0]["corrida"]:
            lineas.append("{}: la corrida cambió (runner / --permitir-pi / "
                          "runner_min)".format(c.get("nombre")))
    if (aprobado or {}).get("modo") != (vigente or {}).get("modo"):
        lineas.append("modo {} → {}".format((aprobado or {}).get("modo"),
                                            (vigente or {}).get("modo")))
    return lineas


# ------------------------------------------------------------------- disco
def guardar(ruta, plan):
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_name(ruta.name + ".tmp")
    tmp.write_text(json.dumps(plan, indent=2, ensure_ascii=False,
                              sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(ruta)


def leer(ruta):
    """El plan guardado, verificado: su `hash` tiene que ser el de su
    contenido. Si no, PlanInvalido: un plan editado a mano no se aprueba."""
    ruta = Path(ruta)
    try:
        plan = json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PlanInvalido("no existe {}".format(ruta))
    except (OSError, ValueError) as e:
        raise PlanInvalido("no se pudo leer {}: {}".format(ruta, e))
    if not isinstance(plan, dict) or plan.get("hash") != hash_de(plan):
        raise PlanInvalido("{}: el hash no coincide con el contenido (¿se "
                           "editó a mano?)".format(ruta))
    return plan


def escribir_aprobacion(ruta, h):
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(h + "\n", encoding="utf-8")


def leer_aprobacion(ruta):
    """El hash aprobado, o None si no hay aprobación (o no se lee). Un
    `touch` vacío (la aprobación vieja del lanzador) no aprueba nada."""
    try:
        h = Path(ruta).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return h or None


# ----------------------------------------------------------------- pantalla
def _fmt_usd(v):
    return "US${:.2f}".format(float(v or 0.0))


def render(plan):
    """El plan para leer antes de aprobar."""
    out = []
    modo = plan.get("modo")
    out.append("plan de la corrida {} · {} · hash {}".format(
        plan.get("fecha", ""),
        "run --loop" if modo == "loop" else "run (una pasada)",
        (plan.get("hash") or hash_de(plan))[:12]))
    total = 0.0
    n_pagados = n_claude = 0
    for c in plan.get("contextos") or ():
        corrida = c.get("corrida") or {}
        cab = ["contexto {}".format(c.get("nombre")),
               "corrida {} ({})".format(corrida.get("kind"),
                                        corrida.get("fuente", "")),
               "runner:cheap {}".format("permitido (--permitir-pi)"
                                        if corrida.get("permitir_pi")
                                        else "ignorado"),
               "tope de gasto {}".format(_fmt_usd(c.get("max_usd")))]
        if c.get("hasta"):
            cab.append("hasta {}".format(c["hasta"]))
        if modo == "loop":
            cab.append("freno {}".format(c.get("freno") or "(sin --freno)"))
        out.append("")
        out.append(" · ".join(cab))
        for nota in c.get("notas") or ():
            out.append("  ({})".format(nota))
        entradas = c.get("entradas") or ()
        if not entradas:
            out.append("  nada que despachar")
        sub = 0.0
        for e in entradas:
            pagado = e.get("kind") not in SIN_PLATA
            est = float(e.get("estimado_usd") or 0.0)
            if pagado:
                n_pagados += 1
                sub += est
                costo = "~{} ({})".format(_fmt_usd(est),
                                          e.get("estimado_fuente", ""))
            else:
                n_claude += 1
                costo = "US$0.00 en plata (gasta cuota de la suscripción " \
                        "de Claude)"
            out.append("  {} {} #{}  {}".format(
                e.get("repo"), "PR" if e.get("tipo") == "pr" else "ticket",
                e.get("numero"), e.get("titulo") or ""))
            out.append("      {} · {}".format(e.get("peldano") or "{} {}".format(
                e.get("kind"), e.get("model")), costo))
            if e.get("motivo"):
                out.append("      {}".format(e["motivo"]))
        total += sub
        tope = float(c.get("max_usd") or 0.0)
        if sub > tope:
            out.append("  el tope corta antes: estimado {} > tope {}: lo que "
                       "no quepa no se despacha".format(_fmt_usd(sub),
                                                        _fmt_usd(tope)))
    out.append("")
    out.append("total estimado: {} en {} job(s) pagado(s); {} job(s) de "
               "Claude: US$0 en plata, gastan cuota (harness usage / "
               "harness quota)".format(_fmt_usd(total), n_pagados, n_claude))
    topes = ", ".join("{} {}".format(c.get("nombre"), _fmt_usd(c.get("max_usd")))
                      for c in plan.get("contextos") or ())
    out.append("tope que lo corta: {} (topes.max_usd / --max-usd)".format(
        topes or "—"))
    if any((c.get("corrida") or {}).get("usa_pi")
           for c in plan.get("contextos") or ()):
        out.append("ojo: la corrida usa pi también para el revisor de PRs y "
                   "proponer; entra en el mismo tope")
    if modo == "loop":
        out.append("con --loop cada pasada re-planifica: sólo sale lo que "
                   "este plan cubre, más tickets nuevos que vayan a Claude; "
                   "un ticket pagado nuevo no sale")
    return "\n".join(out) + "\n"
