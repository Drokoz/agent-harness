"""Tests del resumen de la mañana (ticket #7) sobre logs sintéticos.

El log es el JSONL append-only del dispatcher (formato de
`harness.dispatch.EventLog`); acá se fabrica en un tmpdir. Los tres casos
que pide el ticket: log vacío, log normal y log de un solo evento, más el
comportamiento de la marca y de `--since` (filtrar por fecha).
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import support  # noqa: F401  (pone la raíz en sys.path)

from harness.summary import (PrAbierto, construir, filtrar, guardar_marca,
                             leer_eventos, leer_marca, parse_evento, parse_fecha,
                             resumir)


def evento(timestamp, tipo, ref="?", contexto="personal", cuerpo=""):
    return json.dumps({"timestamp": timestamp, "contexto": contexto,
                       "origen": "harness", "tipo": tipo, "ref": ref,
                       "cuerpo": cuerpo}, ensure_ascii=False)


LOG_NORMAL = [
    evento("2026-08-21T23:00:00Z", "corrida", "corrida",
           cuerpo="inicio: 3 ticket(s), max 2 en paralelo"),
    evento("2026-08-22T01:10:00Z", "worktree", "ticket/3",
           cuerpo="/home/u/repo/.worktrees/agent-harness-ticket-3"),
    evento("2026-08-22T01:12:00Z", "pane", "ticket/3", cuerpo="w1:p9"),
    evento("2026-08-22T01:13:00Z", "agente", "ticket/3",
           cuerpo="agent-harness-3 (kind pi, pane w1:p9)"),
    evento("2026-08-22T01:14:00Z", "prompt", "ticket/3", cuerpo="intento 1"),
    evento("2026-08-22T03:00:00Z", "gate", "ticket/3", cuerpo="verde"),
    evento("2026-08-22T03:00:10Z", "pr", "ticket/3",
           cuerpo="PR #19 abierto (gate verde)"),
    evento("2026-08-22T03:00:20Z", "costo", "ticket/3", cuerpo="$0.0560"),
    evento("2026-08-22T03:01:00Z", "limpieza", "ticket/3",
           cuerpo="pane w1:p9 cerrado; worktree removido, rama ticket/3 quedo"),
    evento("2026-08-22T02:00:00Z", "abandono", "ticket/11",
           cuerpo="gate rojo en el worktree: sin PR"),
    evento("2026-08-22T02:05:00Z", "costo", "ticket/11", cuerpo="$0.2840"),
    evento("2026-08-22T05:00:00Z", "pr", "ticket/9",
           cuerpo="PR #20 abierto (gate verde)"),
    evento("2026-08-22T05:10:00Z", "costo", "ticket/9", cuerpo="$0.4300"),
    evento("2026-08-22T05:11:00Z", "costo", "corrida",
           cuerpo="creditos antes 25.0 / despues 24.18 / delta $0.8200"),
    evento("2026-08-22T05:11:01Z", "issue-cerrado", "#9",
           cuerpo="PR #20 merged pero el issue seguia abierto"),
    evento("2026-08-22T05:12:00Z", "corrida", "corrida",
           cuerpo="fin: 2 hecho(s), 1 abandonado(s), costo de jobs $0.77"),
]


def escribir_log(path, lineas):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lineas) + "\n", encoding="utf-8")


class TestParseo(unittest.TestCase):
    def test_linea_valida(self):
        e = parse_evento(evento("2026-08-22T01:00:00Z", "pr", "ticket/3"))
        self.assertEqual(e["tipo"], "pr")

    def test_lineas_que_no_cooperan(self):
        self.assertIsNone(parse_evento("esto no es json"))
        self.assertIsNone(parse_evento("[1, 2, 3]"))
        self.assertIsNone(parse_evento(json.dumps({"tipo": "pr"})))  # sin timestamp
        self.assertIsNone(parse_evento(json.dumps({"timestamp": "ayer a la tarde",
                                                   "tipo": "pr"})))


class TestLeerEventos(unittest.TestCase):
    def test_log_inexistente_es_vacio(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(leer_eventos(Path(tmp) / "no-existe" / "e.jsonl"), [])

    def test_lineas_rotas_se_saltan(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "e.jsonl"
            escribir_log(p, [evento("2026-08-22T01:00:00Z", "pr", "ticket/3"),
                             '{"timestamp": "cortado a mitad',
                             "   ",
                             evento("2026-08-22T02:00:00Z", "abandono", "ticket/9")])
            eventos = leer_eventos(p)
        self.assertEqual([e["tipo"] for e in eventos], ["pr", "abandono"])


class TestParseFecha(unittest.TestCase):
    def test_formatos(self):
        self.assertEqual(parse_fecha("2026-08-21"),
                         datetime(2026, 8, 21, tzinfo=timezone.utc))
        self.assertEqual(parse_fecha("2026-08-21T08:30:00Z"),
                         datetime(2026, 8, 21, 8, 30, tzinfo=timezone.utc))
        self.assertEqual(parse_fecha("2026-08-21T08:30:00+00:00"),
                         datetime(2026, 8, 21, 8, 30, tzinfo=timezone.utc))
        # Naive = UTC: así lo escribe el log.
        self.assertEqual(parse_fecha("2026-08-21T08:30:00"),
                         datetime(2026, 8, 21, 8, 30, tzinfo=timezone.utc))

    def test_inválido_es_none(self):
        for s in ("", "ayer", "31-08-2026", "2026-13-01"):
            self.assertIsNone(parse_fecha(s), s)


class TestResumir(unittest.TestCase):
    def test_log_vacio(self):
        tickets, trabados, costo = resumir([])
        self.assertEqual((tickets, trabados, costo), ([], [], 0.0))

    def test_un_solo_evento(self):
        """El caso mínimo: un solo evento de abandono cuenta como trabado y
        nada más."""
        eventos = [json.loads(evento("2026-08-22T02:00:00Z", "abandono",
                                     "ticket/11",
                                     cuerpo="agente bloqueado (aprobacion o "
                                            "pregunta pendiente)"))]
        tickets, trabados, costo = resumir(eventos)
        self.assertEqual(tickets, [])
        self.assertEqual(costo, 0.0)
        self.assertEqual(trabados[0].ref, "ticket/11")
        self.assertIn("bloqueado", trabados[0].motivo)

    def test_log_normal(self):
        eventos = [json.loads(l) for l in LOG_NORMAL]
        tickets, trabados, costo = resumir(eventos)
        self.assertEqual([t.ref for t in tickets], ["ticket/3", "ticket/9"])
        self.assertIn("PR #19", tickets[0].detalle)
        self.assertEqual([t.ref for t in trabados], ["ticket/11"])
        # Suma de los costos por job; el delta de créditos de la corrida no
        # entra (mediría el mismo gasto dos veces).
        self.assertAlmostEqual(costo, 0.0560 + 0.2840 + 0.4300)

    def test_eventos_sin_tipo_no_cuentan(self):
        eventos = [json.loads(evento("2026-08-22T01:00:00Z", "gate",
                                     "ticket/3", cuerpo="verde"))]
        self.assertEqual(resumir(eventos), ([], [], 0.0))


class TestFiltrar(unittest.TestCase):
    def test_desde_none_es_todo(self):
        eventos = [json.loads(l) for l in LOG_NORMAL]
        self.assertEqual(filtrar(eventos, None), eventos)

    def test_desde_corta_lo_anterior(self):
        eventos = [json.loads(l) for l in LOG_NORMAL]
        desde = parse_fecha("2026-08-22T04:00:00Z")
        self.assertEqual([e["tipo"] for e in filtrar(eventos, desde)],
                         ["pr", "costo", "costo", "issue-cerrado", "corrida"])

    def test_desde_incluye_el_momento_exacto(self):
        eventos = [json.loads(evento("2026-08-22T01:00:00Z", "pr", "ticket/3"))]
        self.assertEqual(len(filtrar(eventos, parse_fecha("2026-08-22T01:00:00Z"))), 1)


class TestConstruir(unittest.TestCase):
    def test_vacio_no_paso_nada(self):
        r = construir([], [])
        self.assertFalse(r.paso_algo)

    def test_normal(self):
        prs = [PrAbierto(repo="agent-harness", number=19, title="Fix"),
               PrAbierto(repo="koku", number=22, title="Migrar el schema")]
        r = construir([json.loads(l) for l in LOG_NORMAL], prs,
                      desde="2026-08-21T23:00:00Z", hasta="2026-08-22T06:00:00Z")
        self.assertTrue(r.paso_algo)
        self.assertEqual(len(r.prs), 2)
        self.assertAlmostEqual(r.costo, 0.77)


class TestMarca(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "harness" / "last_viewed"
            guardar_marca(p, lambda: "2026-08-22T08:00:00Z")
            self.assertEqual(leer_marca(p),
                             datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc))

    def test_marca_inexistente_es_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(leer_marca(Path(tmp) / "last_viewed"))


if __name__ == "__main__":
    unittest.main()
