"""El costo real de un ticket, leído de las sesiones de pi (ticket #90).

Hasta acá el costo por job salía de `Dispatcher._repartir_costo`: restar el
crédito de OpenRouter de antes y después de la corrida y repartir el delta
entre los jobs que prendieron pane. Eso es un reparto, no una medición. Se
contamina con cualquier otra cosa que use la misma API key mientras corre la
tanda, y no existe si la corrida se cae antes de leer el crédito final.

pi ya guarda el dato exacto. Cada mensaje de asistente de
`~/.pi/agent/sessions/<carpeta>/*.jsonl` trae `message.usage.cost.total`, y la
primera línea del archivo (`type: "session"`) trae el `cwd` —el worktree, que
codifica el ticket. Sumando por sesión sale el costo real, sin API, sin
reparto y sin depender de que la corrida termine bien.

Dos sesiones en el mismo worktree son **dos intentos**, no una suma anónima:
es la unidad que necesita la escalera (#38) para contestar cuándo deja de
convenir insistir con Qwen.

Un costo desconocido es `None`, nunca `0.0`: un cero ahí sería indistinguible
de un gasto real de cero (la misma regla que `_repartir_costo`).

Acá vive la lógica pura; el único que toca el disco es `leer_sesiones`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Dónde pi guarda las sesiones, una carpeta por cwd.
SESIONES = "~/.pi/agent/sessions"

# El worktree de un ticket: `.../.worktrees/<repo>-ticket-<n>`, que es lo que
# arma `dispatch.worktree_path`. El `.+?` es perezoso a propósito: un repo con
# guiones (`agent-harness`) se parte mal con uno codicioso.
RUTA_TICKET = re.compile(r"[-.]worktrees[-/](.+?)-ticket-(\d+)(?:[-/]|$)")

# Los contadores que pi reporta por mensaje. `totalTokens` no entra: es la
# suma que hace pi, y sumar sumas re-cuenta.
CAMPOS = ("input", "output", "cacheRead", "cacheWrite", "reasoning")

# Por qué se cortó la sesión (#98). El dispatcher sólo ve el síntoma —"el
# agente terminó sin PR abierto"— y lo clasifica `modelo`. La causa la guarda
# pi en `stopReason` + `errorMessage`, y las tres clases de #37 tienen
# consecuencias distintas: `infra` se reintenta gratis, `modelo` quema un
# peldaño de la escalera, `humano` sale de la frontera.
#
# El 2026-08-24, entre las 11:48 y las 11:55, cinco agentes murieron con
# "Connection error.": no fueron cinco fracasos del modelo, fue un corte de red.

# Reintentar no lo arregla: hace falta que una persona ponga plata o arregle
# la cuenta. Se mira antes que la familia de infra, que es más ancha.
HUMANO = ("402", "out of credits", "insufficient credit", "insufficient_quota",
          "billing", "payment required", "subscription", "add credits")

# A este no lo mató la red: lo matamos nosotros. `terminated` es el watchdog
# (#41) cortando un agente que dejó de avanzar, y un agente trabado es una
# falla del modelo, no de la infraestructura. Llamarlo `infra` APAGA la
# escalera: la noche del 2026-08-26 los nueve abandonos salieron `infra`,
# `escalados` quedó en 0 para todos, y #56 corrió cuatro veces en el peldaño
# más barato por US$2,58 sin escalar nunca.
MATADO = ("terminated", "killed", "sigterm", "sigkill", "aborted")


def ticket_de_cwd(cwd) -> Optional[Tuple[str, int]]:
    """`(repo, issue)` si esa ruta es el worktree de un ticket; si no, None."""
    m = RUTA_TICKET.search(str(cwd or ""))
    return (m.group(1), int(m.group(2))) if m else None


def _tokens_vacios() -> Dict[str, int]:
    return {c: 0 for c in CAMPOS}


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def parsear_sesion(path) -> Optional[dict]:
    """Una sesión del disco: cuánto costó, cuántos tokens y de qué ticket es.

    Una línea rota no tira la sesión entera: los `.jsonl` se escriben mientras
    el agente trabaja y la última línea puede estar cortada por la mitad."""
    path = Path(path)
    cwd = ""
    inicio = ""
    modelo = ""
    stop = ""
    error = ""
    costo = None
    tokens = _tokens_vacios()
    mensajes = 0
    try:
        texto = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        try:
            e = json.loads(linea)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "session":
            cwd = e.get("cwd") or ""
            inicio = e.get("timestamp") or ""
            continue
        m = e.get("message")
        if not isinstance(m, dict):
            continue
        uso = m.get("usage")
        if not isinstance(uso, dict):
            continue
        mensajes += 1
        modelo = m.get("model") or modelo
        # La última salida es la que cuenta: una sesión puede recuperarse de
        # un error y seguir, y entonces ese error no la explica.
        if m.get("stopReason"):
            stop = m["stopReason"]
            error = str(m.get("errorMessage") or "")
        for c in CAMPOS:
            tokens[c] += int(_num(uso.get(c)))
        precio = uso.get("cost")
        if isinstance(precio, dict) and "total" in precio:
            costo = _num(precio["total"]) + (costo or 0.0)
    # El cwd de la cabecera es la fuente; el nombre de la carpeta es el plan B
    # (una sesión sin cabecera legible sigue diciendo de quién es).
    tk = ticket_de_cwd(cwd) or ticket_de_cwd(path.parent.name)
    if not tk:
        return None
    return {
        "repo": tk[0],
        "issue": tk[1],
        "ticket": "{}#{}".format(tk[0], tk[1]),
        "path": str(path),
        "cwd": cwd,
        "inicio": inicio or _inicio_del_nombre(path.name),
        "modelo": modelo,
        "salida": {"stop": stop, "error": error},
        "costo": costo,
        "tokens": tokens,
        "mensajes": mensajes,
    }


def _inicio_del_nombre(nombre: str) -> str:
    """pi nombra `2026-08-25T02-57-00-927Z_<uuid>.jsonl`. Sirve de plan B para
    ordenar intentos cuando la cabecera no se pudo leer."""
    m = re.match(r"(\d{4}-\d\d-\d\d)T(\d\d)-(\d\d)-(\d\d)-(\d+)Z_", nombre)
    if not m:
        return ""
    return "{}T{}:{}:{}.{}Z".format(*m.groups())


def leer_sesiones(base=SESIONES) -> List[dict]:
    """Todas las sesiones de pi que son de un ticket. El único que toca disco."""
    raiz = Path(base).expanduser()
    if not raiz.is_dir():
        return []
    out = []
    for path in sorted(raiz.glob("*/*.jsonl")):
        s = parsear_sesion(path)
        if s:
            out.append(s)
    return out


def _comparable(ts) -> str:
    """El `desde` viene del reloj del EventLog, que no lleva milisegundos, y
    pi sí los escribe. Comparando texto crudo, `...:29.500Z` es MENOR que
    `...:29Z` —el punto ordena antes que la Z— y una sesión de esta corrida
    se perdería por medio segundo."""
    return re.sub(r"\.\d+(?=Z?$)", "", str(ts or ""))


def intentos_de(sesiones, repo, issue, desde=None) -> List[dict]:
    """Las sesiones de ese ticket, ordenadas por cuándo arrancaron.

    `desde` (timestamp ISO) recorta a los intentos de esta corrida: sin eso,
    un ticket que ya se intentó anoche arrastra el costo de anoche."""
    corte = _comparable(desde)
    out = [s for s in sesiones
           if s["repo"] == repo and s["issue"] == int(issue)
           and (not corte or _comparable(s["inicio"]) >= corte)]
    return sorted(out, key=lambda s: s["inicio"])


def costo_de(sesiones, repo, issue, desde=None) -> Optional[float]:
    """El costo real del ticket, o None si no hay nada medible en el disco."""
    conocidos = [s["costo"] for s in intentos_de(sesiones, repo, issue, desde)
                 if s["costo"] is not None]
    return sum(conocidos) if conocidos else None


def salida_de(sesiones, repo, issue, desde=None) -> Optional[dict]:
    """Cómo terminó el ÚLTIMO intento de ese ticket. Los anteriores ya se
    contaron en su momento; el que explica este abandono es el de ahora."""
    ints = intentos_de(sesiones, repo, issue, desde)
    return ints[-1]["salida"] if ints else None


def clasificar_salida(salida) -> Tuple[Optional[str], str]:
    """`(clase, detalle)` según cómo cortó pi, en las clases de #37.

    Sin opinión (`None`) cuando la sesión terminó bien: si pi no se cayó, el
    que sabe por qué no hubo PR es el dispatcher, y su clasificación queda."""
    if not isinstance(salida, dict):
        return (None, "")
    stop = (salida.get("stop") or "").strip()
    error = (salida.get("error") or "").strip()
    if stop == "error":
        bajo = error.lower()
        if any(t in bajo for t in HUMANO):
            clase = "humano"
        elif any(t in bajo for t in MATADO):
            clase = "modelo"
        else:
            clase = "infra"
        return (clase, error or "pi cortó con error, sin mensaje")
    if stop == "length":
        return ("modelo", "se quedó sin contexto (stopReason: length)")
    return (None, "")


def por_ticket(sesiones) -> Dict[str, dict]:
    """Costo, intentos y tokens por ticket, para el reporte."""
    agg: Dict[str, dict] = {}
    for s in sesiones:
        d = agg.setdefault(s["ticket"], {
            "repo": s["repo"], "issue": s["issue"], "costo": None,
            "intentos": 0, "tokens": _tokens_vacios(), "modelos": [],
        })
        d["intentos"] += 1
        if s["costo"] is not None:
            d["costo"] = s["costo"] + (d["costo"] or 0.0)
        for c in CAMPOS:
            d["tokens"][c] += s["tokens"][c]
        if s["modelo"] and s["modelo"] not in d["modelos"]:
            d["modelos"].append(s["modelo"])
    return agg
