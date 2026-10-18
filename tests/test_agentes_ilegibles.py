"""`herdr agent list` que falla dentro de herdr (#178, regla 2 de #152).

Una fuente que no se pudo leer es ilegible, no vacía: `None` (no se pudo
leer) no es `[]` (leído, y nadie trabajando). Antes, un herdr caído en medio
de la sesión se dibujaba "ninguno trabajando", `status` salía 0 y
`estado_frontier` marcaba `libre` un ticket en curso — y `run` lo
re-despachaba, gastando un intento.

Fuera de herdr (sin `HERDR_ENV`) sigue siendo otro estado: "fuera de herdr".
"""

import io
import json
import subprocess
import unittest
from unittest import mock

import support  # noqa: F401  (arma el sys.path)
from harness import adapters
from harness.render import render
from harness.report import render_html
from harness.snapshot import as_dict, snapshot
from test_snapshot import issue, raw, repo_raw
from test_tracker_ilegible import BaseCli, leer_log, mundo

HERDR_CAIDO = (False, "herdr: no server running")


def con_herdr(respuesta):
    """Un `adapters.run` donde sólo `herdr` contesta, con `respuesta`."""
    def _run(args, cwd=None, timeout=30):
        if args[0] == "herdr":
            return respuesta
        return (False, "")
    return _run


class Adaptador(unittest.TestCase):
    def _agentes(self, respuesta):
        with mock.patch.dict("os.environ", {"HERDR_ENV": "1"}), \
                mock.patch.object(adapters, "run", con_herdr(respuesta)):
            return adapters.herdr_agents()

    def test_exit_distinto_de_cero_es_none(self):
        self.assertIsNone(self._agentes(HERDR_CAIDO))

    def test_salida_que_no_es_json_es_none(self):
        self.assertIsNone(self._agentes((True, "esto no es json")))

    def test_json_con_forma_desconocida_es_none(self):
        self.assertIsNone(self._agentes((True, json.dumps({"result": "x"}))))
        self.assertIsNone(self._agentes((True, json.dumps([1, 2]))))

    def test_binario_ausente_es_none(self):
        with mock.patch.dict("os.environ", {"HERDR_ENV": "1"}), \
                mock.patch("subprocess.run",
                           side_effect=FileNotFoundError("herdr")):
            self.assertIsNone(adapters.herdr_agents())

    def test_leida_y_vacia_sigue_siendo_lista_vacia(self):
        vacia = json.dumps({"result": {"agents": []}})
        self.assertEqual(self._agentes((True, vacia)), [])

    def test_collect_dice_si_estamos_en_herdr(self):
        """`agents: None` sólo no alcanza: el crudo dice si había sesión."""
        from harness.config import default_config
        ctxs = default_config().contexts
        with mock.patch.dict("os.environ", {"HERDR_ENV": "1"}), \
                mock.patch.object(adapters, "run", con_herdr(HERDR_CAIDO)), \
                mock.patch.object(adapters, "openrouter_credits",
                                  return_value=None):
            crudo = adapters.collect(ctxs)
        self.assertIsNone(crudo["agents"])
        self.assertTrue(crudo["herdr"])
        with mock.patch.dict("os.environ", {"HERDR_ENV": "0"}), \
                mock.patch.object(adapters, "run", con_herdr(HERDR_CAIDO)), \
                mock.patch.object(adapters, "openrouter_credits",
                                  return_value=None):
            crudo = adapters.collect(ctxs)
        self.assertIsNone(crudo["agents"])
        self.assertFalse(crudo["herdr"])


class Snapshot(unittest.TestCase):
    def test_en_herdr_sin_lista_es_ilegible(self):
        crudo = raw(agents=None)
        crudo["herdr"] = True
        self.assertEqual(snapshot(crudo).agents.state, "ilegible")

    def test_fuera_de_herdr_sigue_siendo_outside(self):
        crudo = raw(agents=None)
        crudo["herdr"] = False
        self.assertEqual(snapshot(crudo).agents.state, "outside")
        self.assertEqual(snapshot(raw(agents=None)).agents.state, "outside")

    def test_offline_gana(self):
        crudo = raw(offline=True, agents=None)
        crudo["herdr"] = True
        self.assertEqual(snapshot(crudo).agents.state, "offline")

    def _repo(self, herdr, agentes):
        crudo = raw(agents=agentes, repos=[repo_raw(issues=[issue(7)])])
        crudo["herdr"] = herdr
        crudo["eventos"] = [{
            "timestamp": "2026-08-24T02:00:00Z", "contexto": "personal",
            "origen": "harness", "run_id": "r1", "ticket": "koku#7",
            "attempt": 1, "tipo": "agente", "ref": "ticket/7",
            "cuerpo": "koku-7 (kind pi, pane w9:p1)"}]
        return snapshot(crudo).contexts[0].repos[0]

    def test_en_curso_no_vuelve_a_libre_con_lista_ilegible(self):
        """Sin saber quién vive, el despachado no se da por muerto."""
        r = self._repo(True, None)
        self.assertEqual([(i.number, i.estado) for i in r.frontier],
                         [(7, "despachado")])

    def test_lista_leida_y_vacia_si_lo_libera(self):
        r = self._repo(True, [])
        self.assertEqual([(i.number, i.estado) for i in r.frontier],
                         [(7, "libre")])

    def test_render_dice_ilegible(self):
        crudo = raw(agents=None)
        crudo["herdr"] = True
        salida = render(snapshot(crudo), color=False)
        linea = [l for l in salida.splitlines() if "Agentes" in l][0]
        self.assertIn("ilegible", linea)
        self.assertNotIn("ninguno trabajando", salida)
        self.assertNotIn("fuera de herdr", salida)

    def test_reporte_html_dice_ilegible(self):
        crudo = raw(agents=None)
        crudo["herdr"] = True
        data = as_dict(snapshot(crudo))
        data.update(resumen={}, cuota=None)
        h = render_html(data)
        self.assertIn("ilegible", h)
        self.assertNotIn("Ningún agente trabajando", h)


def mundo_herdr_caido(buenos):
    base = mundo(buenos)

    def run_cmd(args, cwd=None, timeout=30):
        if args[0] == "herdr":
            return HERDR_CAIDO
        return base(args, cwd=cwd, timeout=timeout)
    return run_cmd


class StatusConAgentesIlegibles(BaseCli):
    REPOS = ("bueno",)
    BUENOS_GH = ("Drokoz/bueno",)

    def test_dice_ilegible_y_sale_1(self):
        with self._patchear(run_cmd=mundo_herdr_caido(self.BUENOS_GH)), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = self.cli.main(["status"])
        texto = out.getvalue()
        self.assertEqual(code, 1, "una lista de agentes ilegible no sale 0")
        linea = [l for l in texto.splitlines() if "Agentes" in l][0]
        self.assertIn("ilegible", linea)
        self.assertNotIn("ninguno trabajando", texto)


class RunConAgentesIlegibles(BaseCli):
    """`run` no re-despacha lo que estaba en curso si no sabe quién vive."""

    REPOS = ("bueno",)
    BUENOS_GH = ("Drokoz/bueno",)

    def test_no_re_despacha_el_ticket_en_curso(self):
        base = {"contexto": "h", "origen": "harness", "run_id": "r0",
                "ticket": "bueno#9", "attempt": 1, "ref": "ticket/9",
                "clase": None}
        previos = [
            # Un costo medido: sin promedio la tanda no se dimensiona (#68)
            # y nada se despacharía por otro motivo.
            dict(base, timestamp="2026-09-01T03:00:00Z", ticket="bueno#5",
                 ref="ticket/5", tipo="costo", cuerpo="$0.2000 (sesion de pi)"),
            # El ticket #9 en curso: su agente arrancó.
            dict(base, timestamp="2026-09-01T04:00:00Z", tipo="agente",
                 cuerpo="bueno-9 (kind pi, pane w9:p1)"),
        ]
        self.log_path.write_text(
            "".join(json.dumps(e) + "\n" for e in previos))

        def run_cmd(args, cwd=None, timeout=30):
            if args[0] == "gh" and "pr" in args and "list" in args:
                return (True, "[]")  # sin PR: sólo el agente lo tiene
            return mundo_herdr_caido(self.BUENOS_GH)(args, cwd=cwd,
                                                     timeout=timeout)

        args = self._args("run", "--context", "h", "--log",
                          str(self.log_path))
        with self._patchear(run_cmd=run_cmd), \
             mock.patch.object(self.cli, "_creditos_usado",
                               return_value=None), \
             mock.patch.object(self.cli, "_creditos_restantes",
                               return_value=99.0), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None,
                                             lambda *a: None)), \
             mock.patch("sys.stdout", new_callable=io.StringIO), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            self.cli.run_main(args, self.cli.load_config().select("h"))
        nuevos = leer_log(self.log_path)[len(previos):]
        self.assertEqual([e for e in nuevos
                          if e["tipo"] in ("worktree", "pane", "agente",
                                            "prompt", "gate")], [], nuevos)


if __name__ == "__main__":
    unittest.main()
