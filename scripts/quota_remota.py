#!/usr/bin/env python3
"""El consumo de Claude de ESTE usuario del sistema, sólo en números.

Autocontenido (stdlib) para correrlo en otro usuario de la máquina sin
instalar el harness:

    ssh tomasherceg@192.168.1.22 'python3 -' < scripts/quota_remota.py

Lee `~/.claude/projects/**/*.jsonl` y no imprime prompts, rutas ni
contenido: sólo tokens ponderados por semana (reset vie 17:00
America/Santiago), por día y por modelo. Pondera igual que
`harness/quota.py` (`PESOS`).
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Santiago")
PESOS = {"input_tokens": 1.0, "cache_creation_input_tokens": 1.25,
         "cache_read_input_tokens": 0.1, "output_tokens": 5.0}
DIAS = 35


def semana_de(t):
    """El viernes 17:00 en que arrancó la semana de cuota de `t`."""
    viernes = t - timedelta(days=(t.weekday() - 4) % 7)
    inicio = viernes.replace(hour=17, minute=0, second=0, microsecond=0)
    if inicio > t:
        inicio -= timedelta(days=7)
    return inicio.date()


def main():
    desde = datetime.now(TZ) - timedelta(days=DIAS)
    semana, dia, modelo, franja = (defaultdict(float) for _ in range(4))
    for f in Path("~/.claude/projects").expanduser().rglob("*.jsonl"):
        try:
            lineas = f.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with lineas:
            for linea in lineas:
                try:
                    r = json.loads(linea)
                    msg = r.get("message") or {}
                    usage = msg.get("usage")
                    ts = r.get("timestamp")
                    if not usage or not ts:
                        continue
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)
                except (ValueError, AttributeError, TypeError):
                    continue
                if t < desde:
                    continue
                p = sum((usage.get(k) or 0) * w for k, w in PESOS.items())
                semana[semana_de(t)] += p
                dia[t.date()] += p
                modelo[msg.get("model", "?")] += p
                habil = t.weekday() < 5 and 9 <= t.hour < 18
                franja["habil 9-18" if habil else "resto"] += p

    print("por semana (ponderado, reset vie 17:00):")
    for k in sorted(semana, reverse=True):
        print("  {}  {:>15,.0f}".format(k, semana[k]))
    print("por dia:")
    for k in sorted(dia, reverse=True):
        print("  {} {}  {:>15,.0f}".format(k, k.strftime("%a"), dia[k]))
    print("por modelo:")
    for k, v in sorted(modelo.items(), key=lambda kv: -kv[1]):
        print("  {:<24} {:>15,.0f}".format(k, v))
    print("franja:")
    for k, v in franja.items():
        print("  {:<12} {:>15,.0f}".format(k, v))


if __name__ == "__main__":
    main()
