"""El costo real de un ticket, leído de las sesiones de pi (#90).

Hasta ahora el costo por job salía de repartir el delta de créditos de
OpenRouter entre los jobs que prendieron pane. Eso es un reparto, no una
medición: se contamina con cualquier otra cosa que use la misma key y no
existe si la corrida se cae antes de leer el crédito final.

pi ya guarda el número exacto por mensaje.
"""

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from harness import costo_pi

WT = "/Users/x/Documents/Github/.worktrees/agent-harness-ticket-74"


def msg(costo=None, ts="2026-08-25T02:57:19.899Z", **tokens):
    """Un mensaje de asistente con su `usage`, como lo escribe pi."""
    uso = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
           "reasoning": 0}
    uso.update(tokens)
    if costo is not None:
        uso["cost"] = {"total": costo}
    return {"type": "message", "timestamp": ts,
            "message": {"role": "assistant", "model": "qwen/qwen3.8-27b",
                        "usage": uso, "content": []}}


def escribir(base, cwd, inicio, lineas):
    """Una sesión en el disco, con el nombre de carpeta y de archivo que usa pi."""
    carpeta = base / ("--" + cwd.strip("/").replace("/", "-") + "--")
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = inicio.replace(":", "-").replace(".", "-") + "_sesion.jsonl"
    cabecera = {"type": "session", "version": 3, "id": "s-" + inicio,
                "timestamp": inicio, "cwd": cwd}
    with (carpeta / nombre).open("w", encoding="utf-8") as f:
        for l in [cabecera] + lineas:
            f.write(json.dumps(l) + "\n")
    return carpeta / nombre


class TicketDelCwd(unittest.TestCase):
    def test_el_worktree_del_harness_dice_repo_y_numero(self):
        self.assertEqual(costo_pi.ticket_de_cwd(WT), ("agent-harness", 74))
        self.assertEqual(costo_pi.ticket_de_cwd(WT + "/harness"),
                         ("agent-harness", 74))

    def test_un_repo_con_guiones_no_se_parte_mal(self):
        self.assertEqual(
            costo_pi.ticket_de_cwd("/a/.worktrees/mi-repo-largo-ticket-7"),
            ("mi-repo-largo", 7))

    def test_lo_que_no_es_un_worktree_de_ticket_no_es_de_nadie(self):
        for p in ["/Users/x/Documents/Github/agent-harness", "/Users/x", "",
                  "/a/.worktrees/sin-numero"]:
            self.assertIsNone(costo_pi.ticket_de_cwd(p), p)


class ParsearSesion(unittest.TestCase):
    def test_suma_el_costo_y_los_tokens_de_cada_mensaje(self):
        with tempfile.TemporaryDirectory() as d:
            p = escribir(Path(d), WT, "2026-08-25T02:57:00.927Z", [
                msg(costo=0.0012, input=100, output=10, cacheRead=1000),
                msg(costo=0.0030, input=200, output=20, reasoning=5),
            ])
            s = costo_pi.parsear_sesion(p)
        self.assertAlmostEqual(s["costo"], 0.0042)
        self.assertEqual(s["tokens"]["input"], 300)
        self.assertEqual(s["tokens"]["output"], 30)
        self.assertEqual(s["tokens"]["cacheRead"], 1000)
        self.assertEqual(s["tokens"]["reasoning"], 5)
        self.assertEqual(s["mensajes"], 2)
        self.assertEqual(s["modelo"], "qwen/qwen3.8-27b")

    def test_una_sesion_sin_usage_no_cuesta_cero_cuesta_desconocido(self):
        """Un cero acá sería indistinguible de un gasto real de cero."""
        with tempfile.TemporaryDirectory() as d:
            p = escribir(Path(d), WT, "2026-08-25T02:57:00.927Z", [])
            s = costo_pi.parsear_sesion(p)
        self.assertIsNone(s["costo"])
        self.assertEqual(s["mensajes"], 0)

    def test_un_usage_sin_cost_tampoco_inventa_un_cero(self):
        with tempfile.TemporaryDirectory() as d:
            p = escribir(Path(d), WT, "2026-08-25T02:57:00.927Z",
                         [msg(input=10)])
            s = costo_pi.parsear_sesion(p)
        self.assertIsNone(s["costo"])
        self.assertEqual(s["tokens"]["input"], 10)

    def test_una_linea_rota_no_tira_la_sesion_entera(self):
        with tempfile.TemporaryDirectory() as d:
            p = escribir(Path(d), WT, "2026-08-25T02:57:00.927Z",
                         [msg(costo=0.5)])
            with p.open("a", encoding="utf-8") as f:
                f.write("{esto no es json\n")
            s = costo_pi.parsear_sesion(p)
        self.assertAlmostEqual(s["costo"], 0.5)


class LeerSesiones(unittest.TestCase):
    def test_una_carpeta_que_no_existe_no_rompe(self):
        self.assertEqual(costo_pi.leer_sesiones("/no/existe/en/ningun/lado"), [])

    def test_dos_sesiones_en_el_mismo_worktree_son_dos_intentos(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            escribir(base, WT, "2026-08-25T02:00:00.000Z", [msg(costo=0.10)])
            escribir(base, WT, "2026-08-25T05:00:00.000Z", [msg(costo=0.40)])
            ses = costo_pi.leer_sesiones(base)
            intentos = costo_pi.intentos_de(ses, "agent-harness", 74)
        self.assertEqual(len(intentos), 2)
        self.assertEqual([round(i["costo"], 2) for i in intentos], [0.10, 0.40],
                         "tienen que venir ordenados por cuando arrancaron")
        self.assertAlmostEqual(costo_pi.costo_de(ses, "agent-harness", 74), 0.50)

    def test_desde_recorta_a_los_intentos_de_esta_corrida(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            escribir(base, WT, "2026-08-25T02:00:00.000Z", [msg(costo=0.10)])
            escribir(base, WT, "2026-08-25T05:00:00.000Z", [msg(costo=0.40)])
            ses = costo_pi.leer_sesiones(base)
            c = costo_pi.costo_de(ses, "agent-harness", 74,
                                  desde="2026-08-25T04:00:00Z")
        self.assertAlmostEqual(c, 0.40, msg="lo de antes de la corrida no es de esta corrida")

    def test_los_milisegundos_no_dejan_afuera_una_sesion_de_esta_corrida(self):
        """`desde` viene del reloj del EventLog, que no lleva milisegundos, y
        pi sí. Comparando texto crudo, "…:29.500Z" es MENOR que "…:29Z" (el
        punto ordena antes que la Z) y la sesión se perdería."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            escribir(base, WT, "2026-08-25T03:15:29.500Z", [msg(costo=0.70)])
            ses = costo_pi.leer_sesiones(base)
            c = costo_pi.costo_de(ses, "agent-harness", 74,
                                  desde="2026-08-25T03:15:29Z")
        self.assertAlmostEqual(c, 0.70)

    def test_sin_sesion_en_el_disco_el_costo_es_desconocido(self):
        self.assertIsNone(costo_pi.costo_de([], "agent-harness", 74))

    def test_una_carpeta_que_no_es_de_un_ticket_se_ignora(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            escribir(base, "/Users/x/Documents/Github/agent-harness",
                     "2026-08-25T02:00:00.000Z", [msg(costo=9.99)])
            ses = costo_pi.leer_sesiones(base)
        self.assertEqual(ses, [], "la sesión del humano no es de ningún ticket")

    def test_por_ticket_agrega_costo_e_intentos(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            escribir(base, WT, "2026-08-25T02:00:00.000Z", [msg(costo=0.10, input=5)])
            escribir(base, WT, "2026-08-25T05:00:00.000Z", [msg(costo=0.40, input=7)])
            escribir(base, "/a/.worktrees/otro-repo-ticket-3",
                     "2026-08-25T03:00:00.000Z", [msg(costo=1.00)])
            agg = costo_pi.por_ticket(costo_pi.leer_sesiones(base))
        self.assertAlmostEqual(agg["agent-harness#74"]["costo"], 0.50)
        self.assertEqual(agg["agent-harness#74"]["intentos"], 2)
        self.assertEqual(agg["agent-harness#74"]["tokens"]["input"], 12)
        self.assertAlmostEqual(agg["otro-repo#3"]["costo"], 1.00)


if __name__ == "__main__":
    unittest.main()
