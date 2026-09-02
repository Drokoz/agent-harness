"""Reporte HTML del snapshot: una página autocontenida para mirar el estado.

Consume el mismo dict que emite `harness status --json` — el contrato que el spec
fijó entre el núcleo y cualquier frontend. No importa nada de `snapshot` ni de
`render`: si mañana la web se escribe en otro lenguaje, consume este mismo JSON.

La página no depende de nada externo salvo Google Fonts, y funciona igual en tema
claro y oscuro. Se puede abrir del disco o publicar como Artifact.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

VERSION = 1


def _esc(x):
    return html.escape(str(x if x is not None else ""), quote=True)


def _fecha(iso):
    """ISO 8601 a algo legible. Devuelve el crudo si no parsea: nunca romper por una fecha."""
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone()
        meses = ("ene", "feb", "mar", "abr", "may", "jun",
                 "jul", "ago", "sep", "oct", "nov", "dic")
        return f"{d.day} {meses[d.month - 1]} {d:%H:%M}"
    except (ValueError, TypeError, IndexError):
        return str(iso)


# --------------------------------------------------------------------- piezas
def _barra(ratio, tono="machine"):
    """Barra de progreso de 0 a 1. `ratio` fuera de rango se recorta, no explota."""
    try:
        pct = max(0.0, min(1.0, float(ratio))) * 100
    except (TypeError, ValueError):
        pct = 0.0
    return (f'<div class="bar"><span class="bar-fill is-{tono}" '
            f'style="width:{pct:.1f}%"></span></div>')


def _presupuesto(b):
    if not b or b.get("state") != "ok":
        estado = {"offline": "sin adaptadores", "missing": "sin credencial",
                  "unset": "sin configurar"}.get((b or {}).get("state"), "sin datos")
        return f'<p class="muted">Presupuesto: {_esc(estado)}.</p>'

    usado, total = b.get("used") or 0, b.get("total") or 0
    left, tickets = b.get("left") or 0, b.get("tickets")
    # La polaridad cambia qué es "bien": en personal que no se acabe, en trabajo que
    # no sobre a fin de mes. Son objetivos opuestos y la página tiene que decirlo.
    if b.get("polarity") == "spend":
        titular, sub = f"US${usado:.2f} usados", f"de US${total:.2f} que hay que gastar"
        tono = "human" if (b.get("ratio") or 0) < 0.5 else "machine"
        ratio = b.get("ratio") or 0
    else:
        titular = f"US${left:.2f} disponibles"
        sub = f"de US${total:.2f}" + (f" · alcanza para ~{tickets} tickets" if tickets else "")
        ratio = 1 - (b.get("ratio") or 0)
        tono = "machine" if ratio > 0.25 else "human"
    return (f'<div class="budget"><p class="budget-n">{_esc(titular)}</p>'
            f'{_barra(ratio, tono)}<p class="muted">{_esc(sub)}</p></div>')


def _ref(item):
    """Cómo se nombra una línea del resumen.

    Los PRs vienen con repo y número; los tickets que anota el dispatcher vienen
    con `ref` ("ticket/69") y nada más. Antes se pedían siempre repo y number, y
    los tickets salían como un "#" suelto al lado de una viñeta vacía.
    """
    repo, num = item.get("repo"), item.get("number")
    if repo and num is not None:
        return "{} #{}".format(repo, num)
    if num is not None:
        return "#{}".format(num)
    return item.get("ref") or ""


def _texto(item):
    """El título si lo hay; si no, lo que el dispatcher dejó dicho."""
    return item.get("title") or item.get("detalle") or ""


def _gasto_texto(d):
    """El gasto de un ticket o un peldaño en las dos monedas (#47): sólo
    las que tengan algo que mostrar, `d` es un dict suelto (ticket, PR o
    peldaño) así que un PR sin `costo`/`cuota` no muestra nada."""
    partes = []
    if d.get("costo"):
        partes.append(f'US${d["costo"]:.2f}')
    if d.get("cuota"):
        partes.append(f'{round(d["cuota"]):,} tok')
    return " · ".join(partes)


def _fila_resumen(i, marca):
    """Una fila de "Cerrados"/"Esperando review", con su gasto si lo trae, y
    los peldaños de la escalada como filas propias cuando hubo más de un
    intento (ticket que escaló, #47)."""
    gasto = _gasto_texto(i)
    sufijo = f' <span class="muted small">{_esc(gasto)}</span>' if gasto else ""
    fila = (f'<li><span class="mark is-{marca}"></span>'
           f'<span class="ref">{_esc(_ref(i))}</span> '
           f'{_esc(_texto(i)[:88])}{sufijo}</li>')
    peldanos = i.get("peldanos") or []
    if len(peldanos) > 1:
        for p in peldanos:
            g = _gasto_texto(p) or "sin medir"
            fila += (f'<li class="muted small peldano">peldaño {_esc(p.get("attempt"))} '
                    f'({_esc(p.get("runner") or "?")}): {_esc(g)}</li>')
    return fila


def _resumen(r):
    if not r or r.get("estado") != "ok":
        return '<p class="muted">Sin log todavía: nada que resumir.</p>'
    tickets, prs = r.get("tickets") or [], r.get("prs") or []
    trabados, costo = r.get("trabados") or [], r.get("costo") or 0
    bloqueos = r.get("bloqueos") or []

    cifras = [("Tickets cerrados", len(tickets), "machine"),
              ("PRs esperando review", len(prs), "human" if prs else "machine"),
              ("Agentes trabados", len(trabados), "human" if trabados else "machine"),
              ("Costo del período", f"US${costo:.2f}", "machine")]
    # Con cero bloqueos no se ocupa espacio para decir que no pasó nada (#95).
    if bloqueos:
        cifras.append(("Bloqueos del guard",
                       sum(b.get("conteo") or 1 for b in bloqueos), "human"))
    cuota_semana = r.get("cuota_semana")
    if cuota_semana is not None:
        harness = r.get("cuota_semana_harness") or 0
        cifras.append(("Cuota de la semana",
                       f"{round(cuota_semana):,} tok ({round(harness):,} harness)",
                       "machine"))
    kpis = "".join(
        f'<div class="kpi"><span class="kpi-n is-{t}">{_esc(v)}</span>'
        f'<span class="kpi-l">{_esc(l)}</span></div>' for l, v, t in cifras)

    detalle = ""
    for titulo, items, marca in (("Cerrados", tickets, "done"), ("Esperando review", prs, "wait")):
        if not items:
            continue
        filas = "".join(_fila_resumen(i, marca) for i in items[:8])
        extra = (f'<li class="muted">… y {len(items)-8} más</li>' if len(items) > 8 else "")
        detalle += f'<h3>{_esc(titulo)}</h3><ul class="list">{filas}{extra}</ul>'

    # Lo que más dice de una noche: los intentos que el guard rechazó, ya
    # agrupados por (ticket, motivo) en `summary.bloqueos_de` (#95).
    if bloqueos:
        filas = "".join(
            f'<li><span class="mark is-block"></span>'
            f'<span class="ref">{_esc(b.get("ticket") or "?")}</span> '
            f'{_esc(str(b.get("motivo") or "")[:88])} '
            f'<span class="muted small">{"x" + str(b.get("conteo") or 1) if (b.get("conteo") or 1) > 1 else ""}</span></li>'
            for b in bloqueos[:8])
        extra = (f'<li class="muted">… y {len(bloqueos)-8} más</li>' if len(bloqueos) > 8 else "")
        detalle += f'<h3>Bloqueos del guard</h3><ul class="list">{filas}{extra}</ul>'

    ventana = f'{_fecha(r.get("desde"))} → {_fecha(r.get("hasta"))}'
    return (f'<p class="muted window">{_esc(ventana)}</p>'
            f'<div class="kpis">{kpis}</div>{detalle}')


def _cuota_seccion(cuota):
    """La evolución por noche (#47): las últimas dos semanas de
    `cuota["por_dia"]`, harness vs resto, con una barra relativa al pico.
    Sin datos de cuota (offline, o la clave ni está) la sección lo dice y
    sigue: no rompe la página."""
    if not cuota or cuota.get("estado") != "ok":
        return '<p class="muted">Sin datos de cuota.</p>'
    por_dia = cuota.get("por_dia") or {}
    if not por_dia:
        return '<p class="muted">Sin datos de cuota todavía.</p>'
    dias = sorted(por_dia)[-14:]
    totales = {d: (por_dia[d].get("harness") or 0) + (por_dia[d].get("resto") or 0)
              for d in dias}
    pico = max(totales.values(), default=0) or 1
    filas = "".join(
        f'<li class="noche"><span class="mono muted small">{_esc(d)}</span>'
        f'{_barra(totales[d] / pico)}'
        f'<span class="muted small">{round(totales[d]):,} tok '
        f'({round(por_dia[d].get("harness") or 0):,} harness)</span></li>'
        for d in dias)
    return f'<ul class="list noches">{filas}</ul>'


def _agentes(a):
    items = (a or {}).get("items") or []
    if (a or {}).get("state") == "outside":
        return '<p class="muted">Fuera de herdr: no hay sesión que inspeccionar.</p>'
    if not items:
        return '<p class="muted">Ningún agente trabajando.</p>'
    orden = {"blocked": 0, "working": 1, "done": 2, "idle": 3}
    filas = "".join(
        f'<tr><td><span class="dot is-{_esc(x.get("status","?"))}"></span>'
        f'{_esc((x.get("who") or "—")[:44])}</td>'
        f'<td class="mono">{_esc(x.get("agent",""))}</td>'
        f'<td><span class="pill is-{_esc(x.get("status","?"))}">{_esc(x.get("status",""))}</span></td>'
        f'<td class="mono muted">{_esc(x.get("repo",""))}</td></tr>'
        for x in sorted(items, key=lambda x: orden.get(x.get("status"), 9)))
    return f'<div class="scroll"><table class="agents"><tbody>{filas}</tbody></table></div>'


def _repo(r):
    abiertos = len(r.get("frontier") or []) + len(r.get("blocked") or [])
    cerrados = r.get("closed_count")
    ready = r.get("ready") or {}

    chips = "".join(
        f'<span class="chip {"is-on" if ready.get(k) else "is-off"}">{_esc(n)}</span>'
        for k, n in (("gate", "gate"), ("skills", "skills"), ("context", "CONTEXT.md")))

    # Completitud: sólo tiene sentido si el tracker respondió y hay historia.
    if cerrados is None:
        completitud = '<p class="muted small">Sin datos del tracker.</p>'
    elif cerrados + abiertos == 0:
        completitud = '<p class="muted small">Sin issues todavía.</p>'
    else:
        ratio = cerrados / (cerrados + abiertos)
        completitud = (f'{_barra(ratio)}<p class="muted small">'
                       f'{cerrados} cerrados · {abiertos} abiertos '
                       f'· {ratio*100:.0f}% completo</p>')

    tareas = ""
    grupos = (("Puede tomarse ahora", r.get("frontier") or [], "go"),
              ("Bloqueado", r.get("blocked") or [], "block"),
              ("Sin triage", r.get("triage") or [], "triage"))
    for titulo, items, marca in grupos:
        if not items:
            continue
        filas = "".join(
            f'<li><span class="mark is-{marca}"></span>'
            f'<span class="ref">#{_esc(i.get("number",""))}</span> '
            f'{_esc((i.get("title") or "")[:78])}</li>' for i in items[:6])
        extra = f'<li class="muted">… y {len(items)-6} más</li>' if len(items) > 6 else ""
        tareas += f'<p class="group">{_esc(titulo)}</p><ul class="list">{filas}{extra}</ul>'

    prs = r.get("prs") or []
    if prs:
        filas = "".join(
            f'<li><span class="mark is-{"draft" if p.get("isDraft") else "wait"}"></span>'
            f'<span class="ref">#{_esc(p.get("number",""))}</span> '
            f'{_esc((p.get("title") or "")[:78])}</li>' for p in prs[:6])
        tareas += f'<p class="group">PRs abiertos</p><ul class="list">{filas}</ul>'

    if not tareas:
        tareas = '<p class="muted small">Nada pendiente.</p>'

    sucio = (f' · <span class="warn">{r["dirty"]} sin commitear</span>'
             if r.get("dirty") else "")
    return (f'<article class="repo"><header class="repo-h">'
            f'<h3>{_esc(r.get("name","?"))}</h3>'
            f'<p class="mono muted small">{_esc(r.get("branch","?"))}{sucio}</p>'
            f'<div class="chips">{chips}</div></header>'
            f'<div class="repo-b">{completitud}{tareas}</div></article>')


# ----------------------------------------------------------------------- CSS
CSS = """
:root{--ground:#F2F4F6;--surface:#FFF;--sunken:#E8EBEF;--ink:#191E25;--ink-soft:#48525E;
--muted:#6E7985;--rule:#D5DBE2;--rule-soft:#E4E9EE;--machine:#1B6B85;--human:#A65D1F;
--warn:#A65D1F;--shadow:0 1px 2px rgba(25,30,37,.06),0 8px 24px -14px rgba(25,30,37,.2)}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#101419;
--surface:#171C23;--sunken:#1E242C;--ink:#E4E8ED;--ink-soft:#B0BAC5;--muted:#838E9A;
--rule:#2B333D;--rule-soft:#232A33;--machine:#63BAD4;--human:#E0A263;--warn:#E0A263;
--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -14px rgba(0,0,0,.6)}}
:root[data-theme="dark"]{--ground:#101419;--surface:#171C23;--sunken:#1E242C;--ink:#E4E8ED;
--ink-soft:#B0BAC5;--muted:#838E9A;--rule:#2B333D;--rule-soft:#232A33;--machine:#63BAD4;
--human:#E0A263;--warn:#E0A263;--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -14px rgba(0,0,0,.6)}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);margin:0;padding:0 20px 80px;
font:16px/1.6 "Source Serif 4",Georgia,serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:940px;margin:0 auto}
h1,h2,h3,.ui,.mono,.chip,.pill,.kpi-l,.group,th{font-family:Archivo,"Helvetica Neue",Arial,sans-serif}
.mono,.ref,.window{font-family:"IBM Plex Mono",ui-monospace,monospace}
header.top{padding:64px 0 30px}
.eyebrow{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.14em;
text-transform:uppercase;color:var(--muted);margin:0 0 14px}
h1{font-size:clamp(30px,5.5vw,44px);font-weight:700;letter-spacing:-.022em;line-height:1.07;
margin:0 0 12px;text-wrap:balance}
section{padding:36px 0;border-top:1px solid var(--rule-soft)}
h2{font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
margin:0 0 20px;padding-bottom:9px;border-bottom:1px solid var(--rule)}
h3{font-size:17px;font-weight:600;letter-spacing:-.01em;margin:18px 0 8px}
p{margin:0 0 12px}.muted{color:var(--muted)}.small{font-size:14px}.warn{color:var(--warn)}
.window{font-size:12.5px;letter-spacing:.02em}
.bar{height:6px;border-radius:3px;background:var(--sunken);overflow:hidden;margin:6px 0}
.bar-fill{display:block;height:100%;border-radius:3px}
.bar-fill.is-machine{background:var(--machine)}.bar-fill.is-human{background:var(--human)}
.budget{background:var(--surface);border:1px solid var(--rule);border-radius:6px;
padding:20px 22px;box-shadow:var(--shadow);max-width:440px}
.budget-n{font-family:Archivo,sans-serif;font-size:24px;font-weight:600;margin:0 0 4px;
font-variant-numeric:tabular-nums}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
background:var(--rule);border:1px solid var(--rule);border-radius:6px;overflow:hidden;margin:0 0 8px}
.kpi{background:var(--surface);padding:16px 18px;display:flex;flex-direction:column;gap:3px}
.kpi-n{font-family:Archivo,sans-serif;font-size:26px;font-weight:600;line-height:1;
font-variant-numeric:tabular-nums}
.kpi-n.is-machine{color:var(--machine)}.kpi-n.is-human{color:var(--human)}
.kpi-l{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
ul.list{list-style:none;padding:0;margin:0 0 14px}
ul.list li{display:flex;gap:9px;align-items:baseline;padding:5px 0;font-size:15px;
border-bottom:1px solid var(--rule-soft)}
ul.list li:last-child{border-bottom:none}
li.noche{display:flex;align-items:center;gap:10px}
li.noche .bar{flex:1;margin:0}
li.peldano{padding-left:16px;font-family:"IBM Plex Mono",monospace}
.ref{font-size:12.5px;color:var(--muted);flex:none}
.mark{width:7px;height:7px;border-radius:50%;flex:none;transform:translateY(-1px);background:var(--muted)}
.mark.is-go,.mark.is-done{background:var(--machine)}
.mark.is-wait,.mark.is-triage{background:var(--human)}
.mark.is-block,.mark.is-draft{background:var(--muted);opacity:.5}
.group{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:16px 0 6px}
.repos{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px}
.repo{background:var(--surface);border:1px solid var(--rule);border-radius:6px;
box-shadow:var(--shadow);overflow:hidden}
.repo-h{padding:16px 20px 14px;border-bottom:1px solid var(--rule-soft)}
.repo-h h3{margin:0 0 3px;font-size:18px}
.repo-b{padding:14px 20px 18px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;padding:3px 8px;border-radius:3px}
.chip.is-on{background:var(--machine);color:var(--surface)}
.chip.is-off{background:var(--sunken);color:var(--muted)}
.scroll{overflow-x:auto}
table.agents{border-collapse:collapse;width:100%;font-family:Archivo,sans-serif;font-size:14.5px}
table.agents td{padding:9px 14px 9px 0;border-bottom:1px solid var(--rule-soft)}
table.agents tr:last-child td{border-bottom:none}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:9px;background:var(--muted)}
.dot.is-working{background:var(--machine)}.dot.is-blocked{background:var(--human)}
.pill{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;padding:3px 8px;
border-radius:3px;background:var(--sunken);color:var(--muted);white-space:nowrap}
.pill.is-working{background:var(--machine);color:var(--surface)}
.pill.is-blocked{background:var(--human);color:var(--surface)}
footer{margin-top:44px;padding-top:20px;border-top:1px solid var(--rule);
font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--muted);
display:flex;flex-wrap:wrap;gap:8px 22px}
"""


def render_html(snap, generado=None):
    """El snapshot (el mismo dict de `--json`) a una página autocontenida."""
    ctxs = snap.get("contexts") or []
    cuando = generado or datetime.now(timezone.utc).astimezone()
    sello = cuando.strftime("%d/%m/%Y %H:%M") if hasattr(cuando, "strftime") else str(cuando)

    secciones = []
    for c in ctxs:
        repos = c.get("repos") or []
        cards = "".join(_repo(r) for r in repos)
        # tracker y run pueden venir como string o como dict según de dónde salga el
        # snapshot; la página no debe romperse por eso.
        def _kind(v):
            return v.get("kind") if isinstance(v, dict) else v
        meta = " · ".join(str(x) for x in (
            _kind(c.get("tracker")), c.get("autonomy"), _kind(c.get("run"))) if x)
        secciones.append(
            f'<section><h2>{_esc(c.get("name","contexto"))}'
            f'<span class="muted"> — {_esc(meta)}</span></h2>'
            f'{_presupuesto(c.get("budget"))}'
            f'<div class="repos" style="margin-top:22px">{cards}</div></section>')

    total_repos = sum(len(c.get("repos") or []) for c in ctxs)
    con_gate = sum(1 for c in ctxs for r in (c.get("repos") or [])
                   if (r.get("ready") or {}).get("gate"))

    return f"""<title>Reporte del Harness</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header class="top">
  <p class="eyebrow">Generado el {_esc(sello)}</p>
  <h1>Reporte del Harness</h1>
</header>

<section><h2>Qué pasó</h2>{_resumen(snap.get("resumen"))}</section>

<section><h2>Cuota por noche</h2>{_cuota_seccion(snap.get("cuota"))}</section>

<section><h2>Agentes</h2>{_agentes(snap.get("agents"))}</section>

{"".join(secciones)}

<footer>
  <span>{total_repos} repos · {con_gate} con gate</span>
  <span>harness report v{VERSION}</span>
</footer>
</div>
"""


def render_from_json(texto):
    """Atajo para pipear: `harness status --json | ...`."""
    return render_html(json.loads(texto))
