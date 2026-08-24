"""El reductor de estado (`harness/state.py`): puro, sobre la lista de
eventos que da `harness.summary.leer_eventos`. Sin herdr, sin git, sin
red: los eventos son dicts que se construyen acá.

Cubre el esquema nuevo del log —`run_id`, `ticket` y `attempt`— y las
líneas viejas, escritas antes de esas claves, que tienen que leerse sin
romper y cuentan como intento 1.
"""

import unittest

import support  # noqa: F401  (pone la raíz del repo en sys.path)
from harness import state


def ev(tipo, ref, cuerpo, ts, run_id="r1", ticket="koku#7", attempt=1,
       clase=None):
    """Una línea del esquema nuevo."""
    return {"timestamp": ts, "contexto": "personal", "origen": "harness",
            "run_id": run_id, "ticket": ticket, "attempt": attempt,
            "tipo": tipo, "ref": ref, "cuerpo": cuerpo, "clase": clase}


def vieja(tipo, ref, cuerpo, ts):
    """Una línea del esquema anterior: sin run_id, ticket ni attempt."""
    return {"timestamp": ts, "contexto": "personal", "origen": "harness",
            "tipo": tipo, "ref": ref, "cuerpo": cuerpo}


class TestIntentos(unittest.TestCase):
    def test_lineas_viejas_cuentan_como_un_intento(self):
        evs = [vieja("worktree", "ticket/7", "/x", "2026-08-22T12:00:00Z"),
               vieja("agente", "ticket/7", "koku-7 (kind pi, pane w9:p1)",
                     "2026-08-22T12:01:00Z"),
               vieja("gate", "ticket/7", "rojo: unittest",
                     "2026-08-22T12:10:00Z"),
               vieja("abandono", "ticket/7", "gate rojo en el worktree: sin PR",
                     "2026-08-22T12:10:00Z")]
        self.assertEqual(state.intentos(evs, "koku", 7), 1)

    def test_un_run_id_distinto_es_otro_intento(self):
        evs = [ev("gate", "ticket/7", "rojo", "2026-08-22T12:00:00Z",
                  run_id="r1"),
               ev("gate", "ticket/7", "rojo", "2026-08-22T13:00:00Z",
                  run_id="r2"),
               ev("gate", "ticket/7", "verde", "2026-08-22T13:05:00Z",
                  run_id="r2")]
        self.assertEqual(state.intentos(evs, "koku", 7), 2)

    def test_lineas_que_no_son_del_ticket_no_cuentan(self):
        evs = [ev("corrida", "corrida", "inicio: 1 ticket(s)",
                  "2026-08-22T12:00:00Z", ticket=None, attempt=None),
               ev("gate", "ticket/8", "rojo", "2026-08-22T12:00:00Z",
                  ticket="koku#8")]
        self.assertEqual(state.intentos(evs, "koku", 7), 0)

    def test_vacio(self):
        self.assertEqual(state.intentos([], "koku", 7), 0)


class TestDeTicket(unittest.TestCase):
    def historial(self):
        """Dos intentos: el primero muere con gate rojo (pi), el segundo
        cierra (claude)."""
        return [
            ev("worktree", "ticket/7", "/x", "2026-08-22T12:00:00Z",
               run_id="r1"),
            ev("agente", "ticket/7", "koku-7 (kind pi, pane w9:p1)",
               "2026-08-22T12:01:00Z", run_id="r1"),
            ev("gate", "ticket/7", "rojo: unittest", "2026-08-22T12:10:00Z",
               run_id="r1"),
            ev("abandono", "ticket/7", "gate rojo en el worktree: sin PR",
               "2026-08-22T12:10:00Z", run_id="r1"),
            ev("costo", "ticket/7", "$0.0500", "2026-08-22T12:11:00Z",
               run_id="r1"),
            ev("worktree", "ticket/7", "/x", "2026-08-23T13:00:00Z",
               run_id="r2"),
            ev("agente", "ticket/7", "koku-7 (kind claude, pane w9:p2)",
               "2026-08-23T13:01:00Z", run_id="r2"),
            ev("gate", "ticket/7", "verde", "2026-08-23T13:20:00Z",
               run_id="r2"),
            ev("pr", "ticket/7", "PR #35 abierto (gate verde)",
               "2026-08-23T13:20:00Z", run_id="r2"),
            ev("costo", "ticket/7", "$0.1000", "2026-08-23T13:21:00Z",
               run_id="r2"),
        ]

    def test_campo_a_campo(self):
        s = state.de_ticket(self.historial(), "koku", 7)
        self.assertEqual(s.intentos, 2)
        self.assertEqual(s.ultimo_motivo, "gate rojo en el worktree: sin PR")
        self.assertEqual(s.ultimo_runner, "claude")
        # r1: 12:00 -> 12:11 = 11 min; r2: 13:00 -> 13:21 = 21 min
        self.assertEqual(s.duracion_min, 32.0)
        self.assertAlmostEqual(s.costo, 0.15)

    def test_sin_eventos(self):
        s = state.de_ticket([], "koku", 7)
        self.assertEqual(s.intentos, 0)
        self.assertIsNone(s.ultimo_motivo)
        self.assertIsNone(s.ultimo_runner)
        self.assertEqual(s.duracion_min, 0.0)
        self.assertEqual(s.costo, 0.0)

    def test_no_se_mecha_con_otro_repo(self):
        otros = [ev("abandono", "ticket/7", "gate rojo",
                    "2026-08-22T12:00:00Z", run_id="r9", ticket="otro#7")]
        s = state.de_ticket(otros, "koku", 7)
        self.assertEqual(s.intentos, 0)
        self.assertIsNone(s.ultimo_motivo)

    def test_lineas_viejas_entran(self):
        evs = [vieja("abandono", "ticket/7",
                     "timeout de espera (3600000 ms)",
                     "2026-08-22T12:10:00Z")]
        s = state.de_ticket(evs, "koku", 7)
        self.assertEqual(s.intentos, 1)
        self.assertEqual(s.ultimo_motivo, "timeout de espera (3600000 ms)")
        self.assertIsNone(s.ultimo_runner)


class TestTasas(unittest.TestCase):
    def test_por_runner_y_por_repo(self):
        evs = [
            ev("agente", "ticket/7", "koku-7 (kind pi, pane p1)",
               "2026-08-22T12:00:00Z", run_id="r1", ticket="koku#7"),
            ev("pr", "ticket/7", "PR #35 abierto (gate verde)",
               "2026-08-22T12:30:00Z", run_id="r1", ticket="koku#7"),
            ev("agente", "ticket/8", "koku-8 (kind pi, pane p2)",
               "2026-08-22T12:00:00Z", run_id="r1", ticket="koku#8"),
            ev("abandono", "ticket/8", "agente bloqueado",
               "2026-08-22T13:00:00Z", run_id="r1", ticket="koku#8"),
            ev("agente", "ticket/9", "otro-9 (kind claude, pane p3)",
               "2026-08-22T12:00:00Z", run_id="r1", ticket="otro#9"),
            ev("pr", "ticket/9", "PR #36 abierto (gate verde)",
               "2026-08-22T12:20:00Z", run_id="r1", ticket="otro#9"),
        ]
        t = state.tasas(evs)
        self.assertEqual(t["por_runner"],
                         {"pi": {"exito": 1, "abandono": 1},
                          "claude": {"exito": 1, "abandono": 0}})
        self.assertEqual(t["por_repo"],
                         {"koku": {"exito": 1, "abandono": 1},
                          "otro": {"exito": 1, "abandono": 0}})

    def test_linea_vieja_entra_por_runner_no_por_repo(self):
        """Las líneas viejas no traen repo en el ticket: cuentan en
        `por_runner` (el kind está en la línea `agente`) y no en
        `por_repo`."""
        evs = [
            vieja("agente", "ticket/7", "koku-7 (kind pi, pane p1)",
                  "2026-08-22T12:00:00Z"),
            vieja("abandono", "ticket/7", "gate rojo en el worktree: sin PR",
                  "2026-08-22T12:30:00Z"),
        ]
        t = state.tasas(evs)
        self.assertEqual(t["por_runner"], {"pi": {"exito": 0, "abandono": 1}})
        self.assertEqual(t["por_repo"], {})

    def test_vacia(self):
        self.assertEqual(state.tasas([]), {"por_runner": {}, "por_repo": {}})


class TestDuracionMedia(unittest.TestCase):
    def test_minutos_por_ticket_cerrado(self):
        evs = [
            ev("worktree", "ticket/7", "/x", "2026-08-22T12:00:00Z",
               run_id="r1", ticket="koku#7"),
            ev("agente", "ticket/7", "koku-7 (kind pi, pane p1)",
               "2026-08-22T12:01:00Z", run_id="r1", ticket="koku#7"),
            ev("pr", "ticket/7", "PR #35 abierto (gate verde)",
               "2026-08-22T12:20:00Z", run_id="r1", ticket="koku#7"),
            ev("worktree", "ticket/8", "/x", "2026-08-22T12:00:00Z",
               run_id="r1", ticket="koku#8"),
            ev("agente", "ticket/8", "koku-8 (kind pi, pane p2)",
               "2026-08-22T12:01:00Z", run_id="r1", ticket="koku#8"),
            ev("pr", "ticket/8", "PR #37 abierto (gate verde)",
               "2026-08-22T12:40:00Z", run_id="r1", ticket="koku#8"),
            ev("worktree", "ticket/9", "/x", "2026-08-22T14:00:00Z",
               run_id="r1", ticket="otro#9"),
            ev("agente", "ticket/9", "otro-9 (kind claude, pane p3)",
               "2026-08-22T14:01:00Z", run_id="r1", ticket="otro#9"),
            ev("pr", "ticket/9", "PR #38 abierto (gate verde)",
               "2026-08-22T14:11:00Z", run_id="r1", ticket="otro#9"),
        ]
        self.assertEqual(state.duracion_media(evs, "pi"), 30.0)   # (20+40)/2
        # claude: 14:00 (primer evento) -> 14:11 (pr) = 11 min
        self.assertEqual(state.duracion_media(evs, "claude"), 11.0)
        self.assertEqual(state.duracion_media(evs, "codex"), 0.0)


class TestIntentosQueEscalan(unittest.TestCase):
    """Cuántos intentos de un ticket consumen un peldaño de la escalera de
    reintentos (#38): todos los que terminan en abandono, salvo los
    clasificados `infra` -- esos se reintentan en el acto (#37) y no
    cuentan."""

    def test_infra_no_escala_pero_modelo_si(self):
        evs = [
            ev("abandono", "ticket/7", "no se pudo crear el worktree",
               "2026-08-22T12:00:00Z", run_id="r1", clase="infra"),
            ev("abandono", "ticket/7", "gate rojo en el worktree: sin PR",
               "2026-08-23T12:00:00Z", run_id="r2", clase="modelo"),
        ]
        self.assertEqual(state.intentos_que_escalan(evs, "koku", 7), 1)

    def test_humano_tambien_escala(self):
        evs = [ev("abandono", "ticket/7",
                  "agente bloqueado (aprobacion o pregunta pendiente)",
                  "2026-08-22T12:00:00Z", run_id="r1", clase="humano")]
        self.assertEqual(state.intentos_que_escalan(evs, "koku", 7), 1)

    def test_lineas_viejas_sin_clase_cuentan_como_modelo(self):
        """El default conservador: una causa desconocida gasta peldaño,
        no lo reintenta gratis."""
        evs = [vieja("abandono", "ticket/7",
                     "gate rojo en el worktree: sin PR",
                     "2026-08-22T12:00:00Z")]
        self.assertEqual(state.intentos_que_escalan(evs, "koku", 7), 1)

    def test_una_corrida_sin_abandono_no_escala(self):
        evs = [ev("pr", "ticket/7", "PR #35 abierto (gate verde)",
                  "2026-08-22T12:00:00Z", run_id="r1")]
        self.assertEqual(state.intentos_que_escalan(evs, "koku", 7), 0)

    def test_vacio(self):
        self.assertEqual(state.intentos_que_escalan([], "koku", 7), 0)

    def test_no_se_mecha_con_otro_repo(self):
        evs = [ev("abandono", "ticket/7", "gate rojo", "2026-08-22T12:00:00Z",
                  run_id="r9", ticket="otro#7", clase="modelo")]
        self.assertEqual(state.intentos_que_escalan(evs, "koku", 7), 0)


if __name__ == "__main__":
    unittest.main()
