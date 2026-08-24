"""Dibujar el snapshot. Nada más.

`render(snap)` devuelve el texto completo de la pantalla; no imprime, no decide
nada y no mira el entorno. El color entra por parámetro (`color=False` cuando la
salida no es un TTY) y los recortes de acá —cuántos issues se listan, a cuántos
caracteres se corta un título— son decisiones de dibujo, no de estado. Lo que no
entra en la pantalla sí sale entero por `--json`.

Los agentes son de la máquina, así que se dibujan una sola vez arriba. Todo lo
demás es por contexto: cada uno con su presupuesto, su trabajo y su readiness.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.snapshot import READINESS

MAX_FRONTIER = 5
MAX_BLOCKED = 3
MAX_PRS = 5

OFFLINE_HINT = "sin adaptadores (HARNESS_OFFLINE=1)"
PROVIDERS = {"openrouter": "OpenRouter", "manual": "Presupuesto", "none": "Presupuesto"}


@dataclass
class Palette:
    bold: str = ""
    dim: str = ""
    red: str = ""
    grn: str = ""
    yel: str = ""
    cya: str = ""
    off: str = ""


COLOR = Palette(bold="\033[1m", dim="\033[2m", red="\033[31m", grn="\033[32m",
                yel="\033[33m", cya="\033[36m", off="\033[0m")
PLAIN = Palette()


def bar(used, total, c, polarity="remaining", width=24):
    """La barra, coloreada según la polaridad del contexto.

    Con `remaining` lo bueno es que quede plata; con `spent`, que se haya gastado.
    Es la misma barra: lo que cambia es de qué lado está el rojo.
    """
    if not total:
        return ""
    ratio = min(1.0, max(0.0, used / total))
    bien = ratio if polarity == "spent" else 1 - ratio
    color = c.red if bien < 0.2 else (c.yel if bien < 0.5 else c.grn)
    filled = min(width, int(round(width * ratio)))
    return f"{color}{'█' * filled}{c.dim}{'░' * (width - filled)}{c.off}"


def _resumen(r, c, out):
    """El resumen de la mañana, arriba de todo: qué pasó desde la última
    vez que se miró, antes de lo accionable."""
    if r.estado == "offline":
        out("")
        out(f" {c.bold}Resumen{c.off}     {c.dim}{OFFLINE_HINT}{c.off}")
        return
    desde = f"desde {r.desde}" if r.desde else "desde el principio"
    out("")
    out(f" {c.bold}Resumen{c.off}     {c.dim}{desde}{c.off}")
    if not r.paso_algo:
        out(f"   {c.dim}no pasó nada{c.off}")
        return
    if r.tickets:
        out(f"   {c.grn}✓{c.off} {len(r.tickets)} tickets con PR abierto (gate verde)")
        for t in r.tickets:
            out(f"     · {t.contexto} {t.ref}: {t.detalle}")
    if r.trabados:
        out(f"   {c.red}⊘{c.off} {len(r.trabados)} trabado(s)")
        for t in r.trabados:
            out(f"     · {t.contexto} {t.ref}: {t.motivo[:72]}")
    if r.prs:
        out(f"   {c.yel}◌{c.off} {len(r.prs)} PR abierto(s) esperando review")
        for p in r.prs:
            out(f"     · {p.repo} #{p.number} {p.title[:60]}")
    if r.costo > 0:
        out(f"   costo del período: ${r.costo:.2f}")


def _budget(b, c, out):
    etiqueta = PROVIDERS.get(b.provider, "Presupuesto")
    label = f" {c.bold}{etiqueta}{c.off}{' ' * max(1, 12 - len(etiqueta))}"
    if b.state == "offline":
        out(f"{label}{c.dim}{OFFLINE_HINT}{c.off}")
    elif b.state == "unset":
        out(f"{label}{c.dim}el contexto no declara presupuesto{c.off}")
    elif b.state == "missing":
        donde = ("¿key en ~/.pi/agent/models.json?" if b.provider == "openrouter"
                 else "revisá total y used en la config")
        out(f"{label}{c.dim}sin datos ({donde}){c.off}")
    elif b.polarity == "spent":
        # Trabajo: el presupuesto es de la empresa y dejarlo sin usar es perderlo.
        out(f"{label}{bar(b.used, b.total, c, b.polarity)}  "
            f"${b.used:.2f} de ${b.total:.2f} usados  "
            f"{c.dim}{b.ratio * 100:.0f}% del presupuesto{c.off}")
    else:
        # Personal: el recurso escaso es la plata, así que lo que importa es lo que queda.
        out(f"{label}{bar(b.used, b.total, c, b.polarity)}  "
            f"${b.left:.2f} de ${b.total:.2f} disponibles  "
            f"{c.dim}≈{b.tickets} tickets{c.off}")


def _agents(a, c, out):
    if a.state == "offline":
        out(f" {c.bold}Agentes{c.off}     {c.dim}{OFFLINE_HINT}{c.off}")
        return
    if a.state == "outside":
        out(f" {c.bold}Agentes{c.off}     "
            f"{c.dim}fuera de herdr — corré esto dentro de una sesión{c.off}")
        return
    if a.state == "empty":
        out(f" {c.bold}Agentes{c.off}     {c.dim}ninguno trabajando{c.off}")
        return
    out(f" {c.bold}Agentes{c.off}")
    for ag in a.items:
        color = {"working": c.cya, "blocked": c.red, "idle": c.dim,
                 "done": c.grn}.get(ag.status, "")
        out(f"   {color}●{c.off} {ag.who[:34]:<34} {c.dim}{ag.agent:<7}{c.off} "
            f"{color}{ag.status:<8}{c.off} {c.dim}{ag.repo} {ag.pane}{c.off}")


def _header(ctx, c, out):
    detalle = f"{ctx.tracker} · {ctx.autonomy} · {ctx.run}"
    if ctx.vault:
        detalle += f" · vault {ctx.vault}"
    out("")
    out(f" {c.bold}▸ {ctx.name}{c.off}  {c.dim}{detalle}{c.off}")


def _work(ctx, c, out):
    out("")
    out(f" {c.bold}Trabajo{c.off}")
    algo = False
    for r in ctx.repos:
        if not r.slug or not r.has_work:
            continue
        algo = True
        dirty = f" · {r.dirty} sin commitear" if r.dirty else ""
        out("")
        # De dónde salió la frontera de este repo: se ve, no se adivina.
        src = {"native": " (frontera: dependencias nativas)",
               "body": " (frontera: parseo del body)"}.get(r.frontier_source, "")
        out(f"   {c.bold}{r.name}{c.off} {c.dim}{r.branch}{dirty}{src}{c.off}")
        for i in r.frontier[:MAX_FRONTIER]:
            out(f"     {c.grn}▸{c.off} #{i.number} {i.title[:64]}")
        if len(r.frontier) > MAX_FRONTIER:
            out(f"     {c.dim}… y {len(r.frontier) - MAX_FRONTIER} más "
                f"en la frontera{c.off}")
        for i in r.blocked[:MAX_BLOCKED]:
            out(f"     {c.dim}⊘ #{i.number} {i.title[:60]} (bloqueado){c.off}")
        for p in r.prs[:MAX_PRS]:
            mark = f"{c.dim}draft{c.off}" if p.draft else f"{c.yel}review{c.off}"
            out(f"     {mark} #{p.number} {p.title[:60]}")
        if r.triage:
            out(f"     {c.dim}{len(r.triage)} sin triage{c.off}")
    if algo:
        return
    if ctx.tracker != "github":
        out(f"   {c.dim}tracker {ctx.tracker}: todavía sin adaptador, "
            f"no hay tickets que mostrar{c.off}")
    else:
        out(f"   {c.dim}nada pendiente: ningún ticket ready-for-agent, "
            f"ningún PR abierto{c.off}")


def _readiness(repos, c, out):
    if not repos:
        return
    out("")
    out(f" {c.bold}Listo para el harness{c.off}   {c.dim}"
        + "  ".join(k for k, _, _ in READINESS) + f"{c.off}")
    for r in repos:
        marks = "   ".join(
            f"{c.grn}✓{c.off}" if r.ready[k] else f"{c.red}·{c.off}" for k, _, _ in READINESS
        )
        hint = (f"  {c.dim}falta: {r.missing[0]}{c.off}" if r.missing
                else f"  {c.grn}listo{c.off}")
        # La readiness se mira en la rama por defecto; si el working tree está
        # en otra rama (o la mirada cayó al working tree), se nota en la línea.
        nota = ""
        if r.readiness_source == "default-branch":
            if r.default_branch and r.branch not in ("?", r.default_branch):
                nota = f"  {c.dim}(rama: {r.branch}, readiness: {r.default_branch}){c.off}"
        elif r.default_branch:
            nota = f"  {c.dim}(readiness: working tree){c.off}"
        out(f"   {r.name:<22} {marks}{hint}{nota}")


def render(snap, quiet=False, color=True, resumen=None):
    """La pantalla entera, lista para escribir en stdout.

    `resumen` es el resumen de la mañana (ver `harness.summary`); cuando se
    pasa, se dibuja arriba de todo. Sin él la pantalla es la de siempre.
    """
    c = COLOR if color else PLAIN
    lines = [""]
    out = lines.append

    if resumen is not None:
        _resumen(resumen, c, out)
    _agents(snap.agents, c, out)
    if not snap.contexts:
        out("")
        out(f" {c.dim}ningún contexto configurado{c.off}")
    for ctx in snap.contexts:
        _header(ctx, c, out)
        _budget(ctx.budget, c, out)
        _work(ctx, c, out)
        if not quiet:
            _readiness(ctx.repos, c, out)

    lines.append("")
    return "\n".join(lines) + "\n"
