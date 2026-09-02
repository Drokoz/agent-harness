"""El costo real por peldaño de la escalera (#96): la cruz de las sesiones
de pi (#90) con el log de eventos (#38). Sesiones y logs sintéticos, sin
mundo.

Lo que hay que poder contestar mirando esto: cuánto cuesta en promedio
cerrar un ticket en el peldaño 1 contra el 2, y a partir de qué intento
deja de convenir insistir. La tasa de éxito va al lado del costo: un
peldaño barato que nunca cierra no es barato.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (pone la raíz del repo en sys.path)
from harness import costo, costo_pi
from test_costo_pi import escribir, msg
from test_state import ev

ROOT = support.ROOT
HARNESS = ROOT / "bin" / "harness"

K7 = "/a/.worktrees/koku-ticket-7"
K9 = "/a/.worktrees/koku-ticket-9"


def sesiones_koku7_koku9(d):
    """koku#7: el peldaño 1 falla (modelo) y el peldaño 2 cierra. koku#9: el
    primer intento muere `infra` sin prender agente (sin sesión) y el
    segundo —que en peldaño sigue estando en el 1— cierra."""
    base = Path(d)
    escribir(base, K7, "2026-08-22T12:00:30Z", [msg(costo=0.02, input=5000)])
    escribir(base, K7, "2026-08-22T13:00:15Z", [msg(costo=0.08, input=9000)])
    escribir(base, K9, "2026-08-22T11:00:30Z", [msg(costo=0.05, input=2000)])
    return costo_pi.leer_sesiones(base)


def eventos_koku7_koku9():
    return [
        # koku#9: r1 infra (no consume peldaño), r2 cierra en el peldaño 1.
        ev("agente", "ticket/9", "koku-9 (kind pi, pane w1:p1)",
           "2026-08-22T10:00:00Z", run_id="r1", ticket="koku#9", attempt=1),
        ev("abandono", "ticket/9", "no se pudo crear el worktree",
           "2026-08-22T10:05:00Z", run_id="r1", ticket="koku#9", attempt=1,
           clase="infra"),
        ev("agente", "ticket/9", "koku-9 (kind pi, pane w1:p2)",
           "2026-08-22T11:00:00Z", run_id="r2", ticket="koku#9", attempt=2),
        ev("pr", "ticket/9", "PR #40 abierto (gate verde)",
           "2026-08-22T11:20:00Z", run_id="r2", ticket="koku#9", attempt=2),
        # koku#7: r3 muere en el peldaño 1 (modelo), r4 cierra en el peldaño 2.
        ev("agente", "ticket/7", "koku-7 (kind pi, pane w2:p1)",
           "2026-08-22T12:00:00Z", run_id="r3", ticket="koku#7", attempt=1),
        ev("abandono", "ticket/7", "gate rojo en el worktree: sin PR",
           "2026-08-22T12:30:00Z", run_id="r3", ticket="koku#7", attempt=1,
           clase="modelo"),
        ev("agente", "ticket/7", "koku-7 (kind pi, pane w2:p2)",
           "2026-08-22T13:00:00Z", run_id="r4", ticket="koku#7", attempt=2),
        ev("pr", "ticket/7", "PR #41 abierto (gate verde)",
           "2026-08-22T14:00:00Z", run_id="r4", ticket="koku#7", attempt=2),
    ]


class TicketDe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ses = sesiones_koku7_koku9(self.tmp.name)
        self.evs = eventos_koku7_koku9()

    def test_cruza_intento_con_peldano_costo_y_resultado(self):
        t = costo.ticket_de(self.ses, self.evs, "koku", 7)
        i1, i2 = t["intentos"]
        self.assertEqual((i1["peldano"], i2["peldano"]), (1, 2),
                         "el abandono de modelo gastó un peldaño")
        self.assertAlmostEqual(i1["costo"], 0.02)
        self.assertAlmostEqual(i2["costo"], 0.08)
        self.assertEqual(i1["resultado"], "abandono")
        self.assertEqual(i1["clase"], "modelo")
        self.assertEqual(i2["resultado"], "cerrado")
        self.assertAlmostEqual(t["costo"], 0.10)

    def test_un_abandono_de_infra_no_gasta_peldano(self):
        """#37: los `infra` se reintentan en el acto y no escalan, igual que
        lo decide el dispatcher. El segundo intento de koku#9 corre en el
        peldaño 1 otra vez."""
        t = costo.ticket_de(self.ses, self.evs, "koku", 9)
        self.assertEqual([i["peldano"] for i in t["intentos"]], [1, 1])
        self.assertEqual(t["intentos"][0]["resultado"], "abandono")
        self.assertEqual(t["intentos"][0]["clase"], "infra")
        self.assertEqual(t["intentos"][1]["resultado"], "cerrado")

    def test_la_sesion_de_un_ticket_no_le_suda_al_otro(self):
        t7 = costo.ticket_de(self.ses, self.evs, "koku", 7)
        t9 = costo.ticket_de(self.ses, self.evs, "koku", 9)
        self.assertAlmostEqual(t7["costo"], 0.10)
        self.assertAlmostEqual(t9["costo"], 0.05)


class Ventana(unittest.TestCase):
    """El match sesión↔intento va por la ventana de la corrida, no por
    posición: dos sesiones en la misma corrida (reintento de infra en el
    acto, #37) suman a un solo intento, y una sesión de otra época no se
    pega a nada."""

    def test_dos_sesiones_en_una_corrida_suman_al_intento(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            escribir(base, "/a/.worktrees/koku-ticket-11",
                     "2026-08-22T08:00:10Z", [msg(costo=0.01, input=100)])
            escribir(base, "/a/.worktrees/koku-ticket-11",
                     "2026-08-22T08:20:00Z", [msg(costo=0.02, output=20)])
            ses = costo_pi.leer_sesiones(base)
            evs = [
                ev("agente", "ticket/11", "koku-11 (kind pi, pane w1:p1)",
                   "2026-08-22T08:00:00Z", run_id="r1", ticket="koku#11",
                   attempt=1),
                ev("abandono", "ticket/11", "gate rojo en el worktree: sin PR",
                   "2026-08-22T08:30:00Z", run_id="r1", ticket="koku#11",
                   attempt=1, clase="modelo"),
            ]
            t = costo.ticket_de(ses, evs, "koku", 11)
        i = t["intentos"][0]
        self.assertAlmostEqual(i["costo"], 0.03,
                               "las dos sesiones son del mismo intento")
        self.assertEqual(i["tokens"]["input"], 100)
        self.assertEqual(i["tokens"]["output"], 20)

    def test_sesion_fuera_de_toda_corrida_se_dice(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            escribir(base, "/a/.worktrees/koku-ticket-12",
                     "2026-08-22T06:00:00Z", [msg(costo=0.01)])
            ses = costo_pi.leer_sesiones(base)
            evs = [
                ev("agente", "ticket/12", "koku-12 (kind pi, pane w1:p1)",
                   "2026-08-22T08:00:00Z", run_id="r1", ticket="koku#12",
                   attempt=1),
            ]
            t = costo.ticket_de(ses, evs, "koku", 12)
        self.assertIsNone(t["intentos"][0]["costo"])
        self.assertEqual(len(t["sesiones_sin_corrida"]), 1)

    def test_sin_timestamps_el_match_caede_a_la_posicion(self):
        """Líneas sin timestamps legibles: no hay ventana, y una sesión por
        un paso, por orden, sigue siendo una atribución honesta."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            escribir(base, "/a/.worktrees/koku-ticket-13",
                     "2026-08-22T08:00:10Z", [msg(costo=0.01)])
            ses = costo_pi.leer_sesiones(base)
            evs = [{"tipo": "agente", "ref": "ticket/13", "run_id": "r1",
                    "ticket": "koku#13", "attempt": 1, "timestamp": "",
                    "cuerpo": "koku-13 (kind pi, pane w1:p1)"}]
            t = costo.ticket_de(ses, evs, "koku", 13)
        self.assertAlmostEqual(t["intentos"][0]["costo"], 0.01)


class PorPeldano(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.v = costo.vista(sesiones_koku7_koku9(self.tmp.name),
                             eventos_koku7_koku9())

    def test_el_peldano_barato_lleva_la_tasa_al_lado(self):
        p1 = self.v["peldanos"][1]
        self.assertEqual(p1["intentos"], 3)          # k7a1, k9a1, k9a2
        self.assertEqual(p1["cerrados"], 1)
        self.assertAlmostEqual(p1["tasa_exito"], 1 / 3)
        self.assertAlmostEqual(p1["costo_medio"], (0.02 + 0.05) / 2,
                               "promedio sobre los intentos con costo medido")
        p2 = self.v["peldanos"][2]
        self.assertEqual(p2["intentos"], 1)
        self.assertAlmostEqual(p2["tasa_exito"], 1.0)
        self.assertAlmostEqual(p2["costo_medio"], 0.08)

    def test_tokens_crudos_por_peldano(self):
        p1 = self.v["peldanos"][1]
        self.assertEqual(p1["tokens"]["input"], 5000 + 2000)
        self.assertEqual(self.v["peldanos"][2]["tokens"]["input"], 9000)

    def test_sin_sesiones_medibles_no_se_inventa_promedio_de_cero(self):
        """El intento infra de koku#9 no prendió agente: no hay sesión, no
        hay costo. Promediarlo como 0.0 haría ver el peldaño más barato de
        lo que es."""
        t = self.v["tickets"]["koku#9"]
        self.assertIsNone(t["intentos"][0]["costo"])
        self.assertIsNone(t["intentos"][0]["tokens"])
        self.assertEqual(self.v["peldanos"][1]["con_costo"], 2,
                         "el intento sin sesión no entra al promedio")

    def test_un_peldano_toda_desconocida_muestra_none(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            escribir(base, "/a/.worktrees/koku-ticket-14",
                     "2026-08-22T09:00:10Z", [msg(input=5)])  # sin cost
            ses = costo_pi.leer_sesiones(base)
            evs = [ev("agente", "ticket/14", "koku-14 (kind pi, pane w1:p1)",
                      "2026-08-22T09:00:00Z", run_id="r1", ticket="koku#14",
                      attempt=1),
                   ev("pr", "ticket/14", "PR #42 abierto (gate verde)",
                      "2026-08-22T09:20:00Z", run_id="r1", ticket="koku#14",
                      attempt=1)]
            v = costo.vista(ses, evs)
        p1 = v["peldanos"][1]
        self.assertIsNone(p1["costo_medio"])
        self.assertEqual(p1["intentos"], 1)
        self.assertAlmostEqual(p1["tasa_exito"], 1.0)


class Vista(unittest.TestCase):
    def test_descubre_tickets_del_log_y_de_las_sesiones(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            escribir(base, "/a/.worktrees/koku-ticket-20",
                     "2026-08-22T09:00:10Z", [msg(costo=0.01)])
            ses = costo_pi.leer_sesiones(base)
            evs = [ev("agente", "ticket/21", "koku-21 (kind pi, pane w1:p1)",
                      "2026-08-22T09:00:00Z", run_id="r1", ticket="koku#21",
                      attempt=1)]
            v = costo.vista(ses, evs)
        self.assertEqual(sorted(v["tickets"]), ["koku#20", "koku#21"])

    def test_el_mantenimiento_no_entraca_como_ticket(self):
        """`koku#pr5` es el PR del mantenedor (#56), no el ticket 5."""
        evs = [ev("agente", "ticket/pr5", "koku-pr5 (kind pi, pane w1:p1)",
                  "2026-08-22T09:00:00Z", run_id="r1", ticket="koku#pr5",
                  attempt=1)]
        v = costo.vista([], evs)
        self.assertEqual(v["tickets"], {})

    def test_vacio_no_inventa_peldanos(self):
        v = costo.vista([], [])
        self.assertEqual(v, {"tickets": {}, "peldanos": {}})
        self.assertIn("sin datos", costo.render(v))


class Render(unittest.TestCase):
    def test_las_preguntas_de_la_pantalla(self):
        with tempfile.TemporaryDirectory() as d:
            v = costo.vista(sesiones_koku7_koku9(d), eventos_koku7_koku9())
            texto = costo.render(v)
        for esperado in ("Costo real", "peldaño 1", "peldaño 2",
                         "koku#7", "koku#9", "33%", "cerró", "abandono"):
            self.assertIn(esperado, texto, texto)


class Cli(unittest.TestCase):
    """`harness costo` de punta a punta, sin red: sesiones y log sintéticos
    contra el CLI real."""

    def _correr(self, args, tmp):
        env = dict(os.environ)
        env.update(HOME=tmp, XDG_STATE_HOME=tmp, HARNESS_OFFLINE="1")
        return subprocess.run([str(HARNESS), *args], env=env, capture_output=True,
                              text=True, timeout=120)

    def _escenario(self, tmp):
        tmp = Path(tmp)
        base = tmp / "sessions"
        escribir(base, "/a/.worktrees/koku-ticket-7",
                 "2026-08-22T12:00:30Z", [msg(costo=0.02, input=5000)])
        log = tmp / "events.jsonl"
        lines = [ev("agente", "ticket/7", "koku-7 (kind pi, pane w1:p1)",
                    "2026-08-22T12:00:00Z", run_id="r1", ticket="koku#7",
                    attempt=1),
                 ev("pr", "ticket/7", "PR #41 abierto (gate verde)",
                    "2026-08-22T12:30:00Z", run_id="r1", ticket="koku#7",
                    attempt=1)]
        log.write_text("".join(json.dumps(e) + "\n" for e in lines))
        return str(base), str(log)

    def test_json_trae_tokens_crudos_y_peldanos(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, log = self._escenario(tmp)
            r = self._correr(["costo", "--sessions", base, "--log", log,
                              "--json"], tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        t = data["tickets"]["koku#7"]["intentos"][0]
        self.assertEqual(t["peldano"], 1)
        self.assertAlmostEqual(t["costo"], 0.02)
        self.assertEqual(t["tokens"]["input"], 5000)
        self.assertAlmostEqual(data["peldanos"]["1"]["tasa_exito"], 1.0)

    def test_pantalla_salida_cero(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, log = self._escenario(tmp)
            r = self._correr(["costo", "--sessions", base, "--log", log], tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Costo real", r.stdout)

    def test_sin_nada_es_salida_cero_y_dice_que_falta(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._correr(["costo"], tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sin datos", r.stdout)


if __name__ == "__main__":
    unittest.main()
