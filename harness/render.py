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

import re
from dataclasses import dataclass

from harness.snapshot import READINESS

MAX_FRONTIER = 5
MAX_BLOCKED = 3
MAX_PRS = 5

# De qué color se dibuja cada estado de la frontera (ticket #43): lo que se
# puede tomar está en verde; lo que ya está en manos de alguien, en su color.
ESTADO_COLOR = {"libre": "grn", "despachado": "cya", "pr-abierto": "yel",
                "mergeado": "grn", "parkeado": "dim"}

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


def _tok(n):
    return f"{round(n or 0):,}"


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
    # La cuota de la semana no es del período (ver Resumen.cuota_semana): se
    # muestra aunque no haya pasado nada más, y no se muestra si no se pudo
    # calcular (offline, #47) en vez de fingir un dato.
    if r.cuota_semana is not None:
        out(f"   cuota de la semana: {_tok(r.cuota_semana)} tokens "
            f"({_tok(r.cuota_semana_harness)} del harness)")
    if not r.paso_algo:
        out(f"   {c.dim}no pasó nada{c.off}")
        return
    if r.tickets:
        _tickets(r.tickets, c, out)
    if r.trabados:
        out(f"   {c.red}⊘{c.off} {len(r.trabados)} trabado(s)")
        for t in r.trabados:
            out(f"     · {t.contexto} {t.ref}: {t.motivo[:72]}")
    if r.bloqueos:
        # Lo que más dice de una noche (#95): un agente que volvió a
        # intentar lo que el guard le bloqueó es una señal sobre el
        # prompt, el modelo o el ticket. Repetidos (mismo ticket, mismo
        # motivo) van agrupados con su conteo, no repetidos.
        total = sum(b.conteo for b in r.bloqueos)
        out(f"   {c.red}⛔{c.off} {total} bloqueo(s) del guard")
        for b in r.bloqueos:
            x = f" {c.dim}x{b.conteo}{c.off}" if b.conteo > 1 else ""
            out(f"     · {b.ticket or '?'} {b.motivo[:72]}{x}")
    if r.prs:
        out(f"   {c.yel}◌{c.off} {len(r.prs)} PR abierto(s) esperando review")
        for p in r.prs:
            out(f"     · {p.repo} #{p.number} {p.title[:60]}")
    if r.costo > 0:
        out(f"   costo del período: ${r.costo:.2f}")


def _revision(t, c, out):
    """El veredicto del revisor barato junto a su PR (#46): la línea que le
    dice a la persona qué mirar primero. Tres estados: hallazgos (uno por
    línea, la más grave primero — ya vienen ordenados por el revisor),
    "sin hallazgos" (también es información: un PR sin hallazgos se marca
    como tal) y fallida (sin veredicto, con su motivo)."""
    r = t.revision
    if r is None:
        return
    if r.estado == "fallida":
        out(f"       {c.dim}revisión fallida: {r.motivo}{c.off}")
        return
    if not r.hallazgos:
        out(f"       {c.dim}revisión: sin hallazgos{c.off}")
        return
    for h in r.hallazgos:
        color = {"alta": c.red, "media": c.yel}.get(h.gravedad, c.dim)
        out(f"       {color}{h.linea}{c.off}")


def _gasto_ticket(t, c):
    """Lo que un ticket costó en las monedas que midemos (#47, #116):
    minutos de agente (la que de verdad limita una noche), dólares de
    OpenRouter y tokens de Claude ponderados, cuando hay algo que
    mostrar. Vacío si el ticket no gastó nada en ninguna de las tres —
    o si no se cruzó contra la cuota (offline: `cuota` queda en 0.0)."""
    partes = []
    if t.minutos >= 1:
        partes.append(f"{round(t.minutos)} min")
    if t.costo > 0:
        partes.append(f"${t.costo:.2f}")
    if t.cuota > 0:
        partes.append(f"{_tok(t.cuota)} tok")
    if not partes:
        return ""
    return f"  {c.dim}" + " · ".join(partes) + c.off


def _peldanos(t, c, out):
    """El desglose por intento de un ticket escalado (#38, #47, #112): sólo
    tiene sentido mostrarlo cuando hubo más de un intento — uno solo no
    "escaló", y repetir el mismo número no agrega nada.

    Cada línea es una corrida: el intento, como intento (no es el peldaño),
    el peldaño de verdad —la etiqueta que el dispatcher anotó en su línea
    `peldano`: índice en `ESCALERA` con runner y modelo— y su gasto. Un
    intento que no gastó peldaño (abandono `infra`, #37) se marca: es la
    información que ordena la escalera. Dos corridas que comparten intento
    (dos `run_id` distintos) se dibujan igual, con su peldaño y su runner.
    """
    if len(t.peldanos) <= 1:
        return
    for p in t.peldanos:
        partes = []
        if p.get("costo"):
            partes.append(f"${p['costo']:.2f}")
        if p.get("cuota"):
            partes.append(f"{_tok(p['cuota'])} tok")
        gasto = " · ".join(partes) if partes else "sin medir"
        motivo = f" — {p['motivo'][:50]}" if p.get("motivo") else ""
        # Sin línea `peldano` en el log (corrida vieja, fallback de la
        # corrida) no hay peldaño que mostrar: el runner, o ni eso.
        peldano = p.get("peldano") or p.get("runner") or "?"
        nota = " (no gasta peldaño)" if p.get("clase") == "infra" else ""
        out(f"       {c.dim}int. {p.get('attempt') or '?'} · {peldano}: "
            f"{gasto}{motivo}{nota}{c.off}")


def _detalle_ticket(t):
    """Lo que la línea dice del PR (#86): cuando el estado se confirmó en
    vivo, el estado reconciliado y el número — el cuerpo del evento es de
    cuando se abrió y se queda atrás (un PR mergeado sigue diciendo "abierto"
    en el log, y el encabezado ya dice mergeados). Sin confirmar, el cuerpo
    del log, que la línea marca "según el log" de todos modos."""
    if t.en_vivo and t.numero is not None:
        return f"PR #{t.numero} {t.estado}"
    return t.detalle


def _tickets(tickets, c, out):
    """Los tickets del período, agrupados por el estado en vivo del PR: el
    merge es humano y no deja evento, así que "abierto" es sólo lo que decía
    el log en su momento. Un ticket sin reconciliar (sin red, sin slug) se
    marca como tal en vez de fingir que se confirmó."""
    grupos = (
        ("mergeado", c.grn, "✓", "tickets mergeados"),
        ("abierto", c.yel, "◌", "tickets con PR abierto (gate verde)"),
        ("cerrado", c.red, "✗", "tickets cerrados sin mergear"),
    )
    for estado, color, icono, etiqueta in grupos:
        del_estado = [t for t in tickets if t.estado == estado]
        if not del_estado:
            continue
        out(f"   {color}{icono}{c.off} {len(del_estado)} {etiqueta}")
        for t in del_estado:
            marca = "" if t.en_vivo else f" {c.dim}(según el log, sin confirmar){c.off}"
            out(f"     · {t.contexto} {t.ref}: {_detalle_ticket(t)}{marca}{_gasto_ticket(t, c)}")
            _revision(t, c, out)
            _peldanos(t, c, out)


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
            esc = getattr(c, ESTADO_COLOR.get(i.estado, ""), "")
            out(f"     {c.grn}▸{c.off} #{i.number} {i.title[:44]}  "
                f"{esc}{i.estado or 'libre'}{c.off}")
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


# ----------------------------------------------------------------- eventos
# La hora corta de un timestamp ISO ("…T21:41:00Z" → "21:41"); un reloj de
# test no es ISO y en ese caso se muestra el timestamp entero, no un invento.
_HORA_RE = re.compile(r"T(\d{2}:\d{2})")


def _hora_evento(ts):
    m = _HORA_RE.search(str(ts or ""))
    return m.group(1) if m else str(ts or "")


def linea_evento(linea, color=True):
    """Una línea corta y legible para un evento del log de `harness run`
    (ticket #81): la hora, el ticket (o `corrida`, si la línea no es de un
    ticket) y qué pasó. No es el JSON crudo — eso queda en el JSONL — sino
    lo que se mira mientras la corrida va. `color=False` (salida no TTY) no
    lleva escapes ni caracteres de control: un log redirigido a archivo
    tiene que quedar legible como está.

    `linea` es el dict que escribe `EventLog.write`; los campos faltantes
    no rompen (líneas viejas del JSONL, que no traían todas las claves).
    """
    c = COLOR if color else PLAIN
    ticket = linea.get("ticket")
    ref = ("#{}".format(str(ticket).rsplit("#", 1)[-1]) if ticket
           else str(linea.get("ref") or ""))
    tipo = str(linea.get("tipo") or "")
    cuerpo = str(linea.get("cuerpo") or "").strip()
    # `corrida` no repite su nombre: la columna ya dice de qué se trata.
    msg = cuerpo if tipo == "corrida" else (tipo + (": " + cuerpo if cuerpo else ""))
    if len(msg) > 100:
        msg = msg[:97] + "..."
    col = ""
    if tipo in ("abandono", "guard"):
        col = c.red
    elif tipo == "peldano":
        col = c.cya
    elif (tipo == "corrida" and cuerpo.startswith("fin")) or tipo == "pr" \
            or (tipo == "gate" and cuerpo.startswith("verde")):
        col = c.grn
    return "{}  {:<7}  {}{}{}".format(_hora_evento(linea.get("timestamp")),
                                      ref, col, msg,
                                      c.off if col else "")
