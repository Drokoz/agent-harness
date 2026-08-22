"""Dibujar el snapshot. Nada más.

`render(snap)` devuelve el texto completo de la pantalla; no imprime, no decide
nada y no mira el entorno. El color entra por parámetro (`color=False` cuando la
salida no es un TTY) y los recortes de acá —cuántos issues se listan, a cuántos
caracteres se corta un título— son decisiones de dibujo, no de estado.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.snapshot import READINESS

MAX_FRONTIER = 5
MAX_BLOCKED = 3
MAX_PRS = 5


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


def bar(used, total, c, width=24):
    if not total:
        return ""
    filled = min(width, int(round(width * used / total)))
    color = c.red if used / total > 0.8 else (c.yel if used / total > 0.5 else c.grn)
    return f"{color}{'█' * filled}{c.dim}{'░' * (width - filled)}{c.off}"


def _budget(b, c, out):
    if b.state == "offline":
        out(f" {c.bold}OpenRouter{c.off}  {c.dim}sin adaptadores (HARNESS_OFFLINE=1){c.off}")
    elif b.state == "ok":
        out(f" {c.bold}OpenRouter{c.off}  {bar(b.used, b.total, c)}  "
            f"${b.left:.2f} de ${b.total:.2f}  {c.dim}≈{b.tickets} tickets{c.off}")
    else:
        out(f" {c.bold}OpenRouter{c.off}  "
            f"{c.dim}sin datos (¿key en ~/.pi/agent/models.json?){c.off}")


def _agents(a, c, out):
    if a.state == "offline":
        out(f" {c.bold}Agentes{c.off}     {c.dim}sin adaptadores (HARNESS_OFFLINE=1){c.off}")
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


def _work(repos, c, out):
    out("")
    out(f" {c.bold}Trabajo{c.off}")
    algo = False
    for r in repos:
        if not r.slug or not r.has_work:
            continue
        algo = True
        dirty = f" · {r.dirty} sin commitear" if r.dirty else ""
        out("")
        out(f"   {c.bold}{r.name}{c.off} {c.dim}{r.branch}{dirty}{c.off}")
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
    if not algo:
        out(f"   {c.dim}nada pendiente: ningún ticket ready-for-agent, "
            f"ningún PR abierto{c.off}")


def _readiness(repos, c, out):
    out("")
    out(f" {c.bold}Listo para el harness{c.off}   {c.dim}"
        + "  ".join(k for k, _, _ in READINESS) + f"{c.off}")
    for r in repos:
        marks = "   ".join(
            f"{c.grn}✓{c.off}" if r.ready[k] else f"{c.red}·{c.off}" for k, _, _ in READINESS
        )
        hint = (f"  {c.dim}falta: {r.missing[0]}{c.off}" if r.missing
                else f"  {c.grn}listo{c.off}")
        out(f"   {r.name:<16} {marks}{hint}")


def render(snap, quiet=False, color=True):
    """La pantalla entera, lista para escribir en stdout."""
    c = COLOR if color else PLAIN
    lines = [""]
    out = lines.append

    _budget(snap.budget, c, out)
    _agents(snap.agents, c, out)
    _work(snap.repos, c, out)
    if not quiet:
        _readiness(snap.repos, c, out)

    lines.append("")
    return "\n".join(lines) + "\n"
