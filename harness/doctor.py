"""`harness doctor`: la salud de la máquina donde corre la flota (#126).

Lo que el harness necesita pero que vive fuera del repo —hoy: el routing de
OpenRouter de `~/.pi/agent/models.json`— se mira aquí, con su evidencia. La
verificación pura vive en `harness/routing.py`; este módulo cruza la
declaración contra el disco y contra la evidencia del log (los
`errorMessage` que `costo_pi` ya guarda de cada sesión), y `aplicar` es la
forma de ponerlo sin editar JSON a mano.
"""

from __future__ import annotations

from harness import costo_pi, routing


def diagnostico(declaracion, models, sesiones=()) -> dict:
    """El estado del routing más la evidencia del log, en un dict.

    `estado`/`detalle`: de `routing.estado` (ok | ausente | distinto |
    sin-config). `evidencia`: qué proveedores de atrás aparecen en los
    `errorMessage` de las sesiones y cuántas veces. Los cruces dicen lo que
    la lista declarada se está perdiendo (`sin_declarar`) y qué declarado no
    tiene evidencia todavía (`sin_evidencia`, informativo: puede que la
    evidencia sea de antes de las sesiones que se leyeron).
    """
    estado, detalle = routing.estado(declaracion, models)
    esperado = ((declaracion or {}).get("compat") or {}).get("openRouterRouting") or {}
    ignore = {str(x).lower() for x in esperado.get("ignore") or []}
    evidencia = costo_pi.proveedores_con_error(list(sesiones or ()))
    provider = (declaracion or {}).get("provider") or "openrouter"
    return {
        "estado": estado,
        "detalle": detalle,
        "provider": provider,
        "esperado": esperado,
        "real": routing.routing_de(models, provider),
        "ignore": sorted(ignore),
        "evidencia": dict(evidencia),
        "sin_declarar": sorted(set(evidencia) - ignore),
        "sin_evidencia": sorted(ignore - set(evidencia)),
    }


def _routing_linea(routing_dict, prefijo):
    if not routing_dict:
        return prefijo + "(sin routing)"
    partes = []
    for k, v in sorted(routing_dict.items()):
        partes.append("{}={}".format(k,
                                     ",".join(v) if isinstance(v, list) else v))
    return prefijo + " ".join(partes)


def render(d, color=True) -> str:
    """La pantalla de `harness doctor`. `color=False` (no TTY) no lleva
    escapes: la salida tiene que quedar legible redirigida a archivo."""
    if color:
        grn, red, yel, dim, bold, off = ("\033[32m", "\033[31m", "\033[33m",
                                         "\033[2m", "\033[1m", "\033[0m")
    else:
        grn = red = yel = dim = bold = off = ""
    lines = []
    out = lines.append
    estado = d["estado"]
    label = f" {bold}Routing OpenRouter{off}"
    if estado == "ok":
        out(f"{label}  {grn}✓ ok{off}")
    elif estado == "ausente":
        out(f"{label}  {red}· ausente{off}  {dim}{d['detalle']} — harness doctor --fix{off}")
    elif estado == "distinto":
        out(f"{label}  {yel}· distinto al esperado{off}  {dim}{d['detalle']} — harness doctor --fix{off}")
    else:
        out(f"{label}  {dim}{d['estado']}: {d['detalle']}{off}")
    out(f"  {dim}esperado{off}  {_routing_linea(d['esperado'], '')}")
    out(f"  {dim}real{off}      {_routing_linea(d['real'], '')}")
    ev = d["evidencia"]
    if ev:
        out("  {}evidencia del log{}  {}".format(
            dim, off,
            ", ".join("{} ({})".format(p, n) for p, n in sorted(ev.items()))))
    if d["sin_declarar"]:
        out("  {}evidencia sin declarar: {} — revisar y sumarlo a "
            "harness/routing.json{}".format(yel, ", ".join(d["sin_declarar"]), off))
    if d["sin_evidencia"]:
        out("  {}declarado sin evidencia en las sesiones leídas: "
            "{}{}".format(dim, ", ".join(d["sin_evidencia"]), off))
    return "\n".join(lines) + "\n"
