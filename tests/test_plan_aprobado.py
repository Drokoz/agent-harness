"""Ninguna corrida real sin plan aprobado (#176).

La noche del 2026-10-18 la corrida nocturna se programó sin que el dueño
la hubiera autorizado y gastó US$17. Nada en el harness exigía que alguien
hubiera visto qué iba a correr, con qué modelo y a qué costo.

Lo que se testea acá:

- `harness run --plan` no tiene efectos: no escribe en el tracker, no crea
  ramas, worktrees ni panes, no arranca agentes. Lista lo que se
  despacharía y deja `plan-FECHA.json` con un hash estable.
- `harness aprobar` escribe `aprobado-FECHA` (la ruta que mira
  `harness-noche`) con el hash del plan, tras mostrarlo y confirmar.
- `harness run` (también `--loop`) sin aprobación del plan vigente sale
  distinto de cero sin crear nada.
- Con `--loop`, cada pasada re-planifica: lo que el plan aprobado no cubre
  y cuesta plata no se despacha; lo de Claude sí.
- `--sin-aprobacion`: una corrida manual (sin `--loop`) en que todo es
  Claude puede correr sin aprobación; si algo paga, no.
"""

import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401  (sys.path + CLAUDE_JSON de mentira)

from harness import dispatch as dispatch_mod
from harness import plan as plan_mod
from harness.dispatch import ESCALERA, EventLog

from test_dispatch import Mundo
from test_runner_corrida import _CliBase


def _entrada(numero, kind="claude", model="sonnet", repo="F7League",
             tipo="ticket", **extra):
    e = {"repo": repo, "tipo": tipo, "numero": numero,
         "titulo": "ticket {}".format(numero), "kind": kind, "model": model,
         "extra_args": ["--effort", "medium"] if kind == "claude"
         else ["--thinking", "medium"],
         "motivo": "por qué: corrida claude (por defecto)",
         "estimado_usd": 0.0 if kind == "claude" else 0.35}
    e.update(extra)
    return e


def _plan(entradas, max_usd=5.0, hasta="07:00", kind="claude",
          permitir_pi=False, modo="loop", creado="2026-10-18T20:00:00Z"):
    return {"version": 1, "fecha": "2026-10-18", "creado": creado,
            "modo": modo,
            "contextos": [{"nombre": "h",
                           "corrida": {"kind": kind, "fuente": "--kind",
                                       "permitir_pi": permitir_pi,
                                       "runner_min": {},
                                       "usa_pi": kind == "pi" or permitir_pi},
                           "max_usd": max_usd, "hasta": hasta,
                           "freno": "", "entradas": entradas}]}


class HashDelPlan(unittest.TestCase):
    """El hash cubre lo que decide plata: tickets, runner, modelo, peldaño,
    tope y hora límite. No los timestamps, títulos ni estimados."""

    def test_estable_entre_reruns(self):
        a = _plan([_entrada(7), _entrada(8)])
        b = _plan([_entrada(8, titulo="otro título"),
                   _entrada(7, motivo="otro motivo", estimado_usd=9.9)],
                  creado="2026-10-18T22:13:00Z")
        self.assertEqual(plan_mod.hash_de(a), plan_mod.hash_de(b))

    def test_cambia_con_el_runner(self):
        a = _plan([_entrada(7)])
        b = _plan([_entrada(7, kind="pi", model=ESCALERA[0]["model"])])
        self.assertNotEqual(plan_mod.hash_de(a), plan_mod.hash_de(b))

    def test_cambia_con_el_tope(self):
        self.assertNotEqual(plan_mod.hash_de(_plan([_entrada(7)], max_usd=5)),
                            plan_mod.hash_de(_plan([_entrada(7)], max_usd=6)))

    def test_cambia_con_hasta_y_con_un_ticket_nuevo(self):
        base = plan_mod.hash_de(_plan([_entrada(7)]))
        self.assertNotEqual(base, plan_mod.hash_de(
            _plan([_entrada(7)], hasta="06:00")))
        self.assertNotEqual(base, plan_mod.hash_de(
            _plan([_entrada(7), _entrada(8)])))

    def test_cambia_con_la_corrida(self):
        self.assertNotEqual(
            plan_mod.hash_de(_plan([_entrada(7)])),
            plan_mod.hash_de(_plan([_entrada(7)], permitir_pi=True)))


class QueCubreElPlan(unittest.TestCase):

    def test_claude_siempre_cubierto(self):
        p = _plan([])
        self.assertTrue(plan_mod.cubre(p, "h", "F7League", "ticket", 99,
                                       "claude", "opus"))

    def test_pi_solo_si_esta_en_el_plan(self):
        qwen = ESCALERA[0]["model"]
        p = _plan([_entrada(7, kind="pi", model=qwen)], kind="pi")
        self.assertTrue(plan_mod.cubre(p, "h", "F7League", "ticket", 7,
                                       "pi", qwen))
        self.assertFalse(plan_mod.cubre(p, "h", "F7League", "ticket", 8,
                                        "pi", qwen))
        self.assertFalse(plan_mod.cubre(p, "otro", "F7League", "ticket", 7,
                                        "pi", qwen))
        self.assertFalse(plan_mod.cubre(p, "h", "F7League", "pr", 7,
                                        "pi", qwen))

    def test_sin_plan_nada_pagado(self):
        self.assertFalse(plan_mod.cubre(None, "h", "F7League", "ticket", 7,
                                        "pi", "x"))
        self.assertTrue(plan_mod.cubre(None, "h", "F7League", "ticket", 7,
                                       "claude", "sonnet"))

    def test_paga(self):
        self.assertFalse(plan_mod.paga(_plan([_entrada(7)])))
        self.assertTrue(plan_mod.paga(_plan([_entrada(7, kind="pi")])))
        # El revisor/proponer de una corrida que usa pi también paga.
        self.assertTrue(plan_mod.paga(_plan([_entrada(7)], permitir_pi=True)))


class ElArchivoDelPlan(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rutas_las_del_lanzador(self):
        self.assertEqual(plan_mod.ruta_aprobado(self.d, "2026-10-18"),
                         self.d / "harness" / "aprobado-2026-10-18")
        self.assertEqual(plan_mod.ruta_plan(self.d, "2026-10-18"),
                         self.d / "harness" / "plan-2026-10-18.json")

    def test_guardar_y_leer(self):
        p = plan_mod.sellar(_plan([_entrada(7)]))
        ruta = plan_mod.ruta_plan(self.d, "2026-10-18")
        plan_mod.guardar(ruta, p)
        leido = plan_mod.leer(ruta)
        self.assertEqual(leido["hash"], p["hash"])

    def test_un_plan_editado_a_mano_no_se_lee(self):
        p = plan_mod.sellar(_plan([_entrada(7)]))
        ruta = plan_mod.ruta_plan(self.d, "2026-10-18")
        plan_mod.guardar(ruta, p)
        data = json.loads(ruta.read_text())
        data["contextos"][0]["max_usd"] = 500
        ruta.write_text(json.dumps(data))
        with self.assertRaises(plan_mod.PlanInvalido):
            plan_mod.leer(ruta)

    def test_aprobacion(self):
        ruta = plan_mod.ruta_aprobado(self.d, "2026-10-18")
        self.assertIsNone(plan_mod.leer_aprobacion(ruta))
        plan_mod.escribir_aprobacion(ruta, "abc123")
        self.assertEqual(plan_mod.leer_aprobacion(ruta), "abc123")


class _Cli(_CliBase):
    """El CLI real por `main`, con el mundo de mentira y el estado en un
    directorio temporal (nunca ~/.local/state del usuario)."""

    def setUp(self):
        super().setUp()
        self.estado = self.d / "state"
        self.hoy = date.today().isoformat()
        self.issues = [self._issue(7)]
        self.despachados = []
        self.mundo = Mundo(prs=[])

    def _main(self, argv, issues=None):
        """`main(argv)` → (código, stdout, stderr). `Dispatcher.dispatch`
        es el real contra el Mundo: si algo se despachara, el Mundo vería
        el worktree y el pane."""
        issues = self.issues if issues is None else issues
        real_dispatch = dispatch_mod.Dispatcher.dispatch

        def dispatch(disp, jobs):
            self.despachados.extend(jobs)
            return real_dispatch(disp, jobs)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.cli, "OFFLINE", False), \
             mock.patch.object(self.cli, "collect",
                               side_effect=lambda *a, **k: self._raw(issues)), \
             mock.patch.object(self.cli.adapters, "run", self.mundo.cmd), \
             mock.patch.object(self.cli, "mergeable_de",
                               return_value="MERGEABLE"), \
             mock.patch.object(self.cli, "_cuota_de",
                               return_value=SimpleNamespace(
                                   permitir_claude=None)), \
             mock.patch.object(self.cli, "_filtrar_por_cuota",
                               side_effect=lambda a, c, cands, l: cands), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None,
                                             lambda *a: None)), \
             mock.patch.object(self.cli, "_creditos_usado",
                               return_value=None), \
             mock.patch.object(self.cli, "_costos_por_peldano",
                               return_value={}, create=True), \
             mock.patch.object(dispatch_mod.Dispatcher, "dispatch",
                               autospec=True, side_effect=dispatch), \
             mock.patch.dict(os.environ, {"HERDR_ENV": "1",
                                          "HOME": str(self.d),
                                          "XDG_STATE_HOME": str(self.estado),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch("time.sleep"), \
             mock.patch("sys.stdout", new=out), \
             mock.patch("sys.stderr", new=err):
            code = self.cli.main(argv + ["--context", "h",
                                         "--log", str(self.log_path)])
        return code, out.getvalue(), err.getvalue()

    def _efectos(self):
        """Los comandos del mundo que crean algo: worktrees, ramas, panes,
        agentes o escrituras en el tracker."""
        malos = []
        for args, _, _ in self.mundo.llamadas:
            txt = " ".join(args)
            if (args[0] == "herdr" or "worktree" in args or "checkout" in args
                    or "branch" in args or "push" in args
                    or (args[0] == "gh" and any(
                        a in args for a in ("close", "edit", "create",
                                            "comment", "merge")))):
                malos.append(txt)
        return malos

    def _ruta_plan(self):
        return self.estado / "harness" / "plan-{}.json".format(self.hoy)

    def _ruta_aprobado(self):
        return self.estado / "harness" / "aprobado-{}".format(self.hoy)

    def _planear_y_aprobar(self, flags=()):
        code, _, err = self._main(["run", "--plan"] + list(flags))
        self.assertEqual(code, 0, err)
        code, _, err = self._main(["aprobar", "--si"])
        self.assertEqual(code, 0, err)
        return json.loads(self._ruta_plan().read_text())


class RunSinAprobacion(_Cli):

    def test_run_sin_aprobacion_sale_distinto_de_cero_y_no_crea_nada(self):
        self._cfg()
        code, _, err = self._main(["run"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.despachados, [])
        self.assertEqual(self._efectos(), [])
        self.assertIn("harness run --plan", err)
        self.assertIn("harness aprobar", err)

    def test_loop_sin_aprobacion_tampoco(self):
        self._cfg()
        code, _, err = self._main(["run", "--loop", "--sin-proponer"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.despachados, [])
        self.assertEqual(self._efectos(), [])
        self.assertIn("harness aprobar", err)

    def test_aprobacion_de_otro_plan_no_vale(self):
        self._cfg()
        self._planear_y_aprobar()
        self.despachados.clear()
        # Aparece un ticket nuevo: el plan vigente ya no es el aprobado.
        code, _, err = self._main(["run"], issues=[self._issue(7),
                                                   self._issue(8)])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.despachados, [])
        self.assertEqual(self._efectos(), [])
        self.assertIn("cambió", err)

    def test_aprobacion_del_loop_no_vale_para_otro_modo(self):
        self._cfg()
        self._planear_y_aprobar(["--loop"])
        code, _, _ = self._main(["run"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.despachados, [])


class ElPlanNoTieneEfectos(_Cli):

    def test_plan_no_escribe_en_el_tracker_ni_crea_ramas_ni_panes(self):
        self._cfg()
        code, out, err = self._main(["run", "--plan"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.mundo.llamadas, [])
        self.assertEqual(self.despachados, [])
        self.assertFalse(self.log_path.exists(),
                         "--plan no escribe el log de eventos")
        self.assertFalse(self._ruta_aprobado().exists())
        self.assertTrue(self._ruta_plan().exists())
        # Lo que lista: repo, número, título, runner, modelo, motivo, costo.
        self.assertIn("F7League", out)
        self.assertIn("#7", out)
        self.assertIn("ticket 7", out)
        self.assertIn("claude", out)
        self.assertIn("sonnet", out)
        self.assertIn("por qué", out)
        self.assertIn("US$", out)
        self.assertIn("cuota", out)
        self.assertIn("tope", out)

    def test_plan_del_loop_no_crea_nada_y_dice_hasta_y_freno(self):
        self._cfg(topes={"hasta": "07:00"})
        freno = str(self.d / "freno")
        code, out, err = self._main(["run", "--plan", "--loop",
                                     "--freno", freno])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.mundo.llamadas, [])
        self.assertEqual(self.despachados, [])
        self.assertIn("07:00", out)
        self.assertIn(freno, out)

    def test_el_archivo_trae_hash_y_entradas(self):
        self._cfg(topes={"max_usd": 4})
        code, _, err = self._main(["run", "--plan", "--kind", "pi"])
        self.assertEqual(code, 0, err)
        p = json.loads(self._ruta_plan().read_text())
        self.assertEqual(p["hash"], plan_mod.hash_de(p))
        (e,) = p["contextos"][0]["entradas"]
        self.assertEqual((e["repo"], e["numero"], e["kind"], e["model"]),
                         ("F7League", 7, "pi", ESCALERA[0]["model"]))
        self.assertGreater(e["estimado_usd"], 0)
        self.assertEqual(p["contextos"][0]["max_usd"], 4)

    def test_hash_estable_entre_reruns_y_cambia_con_runner_y_tope(self):
        self._cfg(topes={"max_usd": 5})
        self._main(["run", "--plan"])
        h1 = json.loads(self._ruta_plan().read_text())["hash"]
        self._main(["run", "--plan"])
        h2 = json.loads(self._ruta_plan().read_text())["hash"]
        self.assertEqual(h1, h2)
        self._main(["run", "--plan", "--kind", "pi"])
        h3 = json.loads(self._ruta_plan().read_text())["hash"]
        self.assertNotEqual(h1, h3)
        self._main(["run", "--plan", "--max-usd", "1"])
        h4 = json.loads(self._ruta_plan().read_text())["hash"]
        self.assertNotEqual(h1, h4)


class Aprobar(_Cli):

    def test_aprobar_escribe_el_hash_en_la_ruta_del_lanzador(self):
        self._cfg()
        p = self._planear_y_aprobar()
        self.assertEqual(self._ruta_aprobado().read_text().strip(), p["hash"])

    def test_aprobar_sin_plan_falla(self):
        self._cfg()
        code, _, err = self._main(["aprobar", "--si"])
        self.assertNotEqual(code, 0)
        self.assertIn("harness run --plan", err)
        self.assertFalse(self._ruta_aprobado().exists())

    def test_aprobar_sin_si_y_sin_tty_no_aprueba(self):
        self._cfg()
        self._main(["run", "--plan"])
        with mock.patch("sys.stdin", new=io.StringIO("")):
            code, _, _ = self._main(["aprobar"])
        self.assertNotEqual(code, 0)
        self.assertFalse(self._ruta_aprobado().exists())

    def test_aprobar_interactivo_con_si(self):
        self._cfg()
        self._main(["run", "--plan"])
        stdin = io.StringIO("s\n")
        stdin.isatty = lambda: True
        with mock.patch("sys.stdin", new=stdin), \
             mock.patch("builtins.input", return_value="s"):
            code, out, err = self._main(["aprobar"])
        self.assertEqual(code, 0, err)
        self.assertTrue(self._ruta_aprobado().exists())

    def test_plan_aprobado_despacha(self):
        self._cfg()
        self._planear_y_aprobar()
        code, _, err = self._main(["run"])
        self.assertEqual(code, 0, err)
        self.assertEqual([j.issue for j in self._tickets(self.despachados)],
                         [7])


class ElLoopReplanifica(_Cli):
    """Aprobado el plan, la noche sigue: cada pasada re-planifica y lo
    pagado que el plan no cubre no sale."""

    def _pasada(self, argv, issues, aprobado):
        self.despachados.clear()
        self.issues = issues

        args = self.cli.build_parser().parse_args(
            ["run", "--context", "h", "--log", str(self.log_path)] + argv)
        real_dispatch = dispatch_mod.Dispatcher.dispatch

        def dispatch(disp, jobs):
            self.despachados.extend(jobs)
            return real_dispatch(disp, jobs)

        with mock.patch.object(self.cli, "OFFLINE", False), \
             mock.patch.object(self.cli, "collect",
                               return_value=self._raw(issues)), \
             mock.patch.object(self.cli.adapters, "run", self.mundo.cmd), \
             mock.patch.object(self.cli, "cosechar", lambda *a, **k: None), \
             mock.patch.object(self.cli, "_cuota_de",
                               return_value=SimpleNamespace(
                                   permitir_claude=None)), \
             mock.patch.object(self.cli, "_filtrar_por_cuota",
                               side_effect=lambda a, c, cands, l: cands), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None,
                                             lambda *a: None)), \
             mock.patch.object(self.cli, "_creditos_usado",
                               return_value=None), \
             mock.patch.object(dispatch_mod.Dispatcher, "dispatch",
                               autospec=True, side_effect=dispatch), \
             mock.patch.dict(os.environ, {"HERDR_ENV": "1",
                                          "HOME": str(self.d),
                                          "XDG_STATE_HOME": str(self.estado),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch("time.sleep"), \
             mock.patch("sys.stdout", new=io.StringIO()):
            ctx = self.cli.load_config().select("h")[0]
            self.cli._una_pasada(args, ctx, EventLog(self.log_path, "h"),
                                 aprobado=aprobado)
        return [json.loads(l) for l in
                self.log_path.read_text().splitlines() if l]

    def test_ticket_pi_nuevo_no_se_despacha(self):
        self._cfg(escalera="pi", topes={"max_usd": 5})
        plan = self._planear_y_aprobar(["--loop"])
        eventos = self._pasada(["--loop"], [self._issue(7), self._issue(8)],
                               plan)
        self.assertEqual([j.issue for j in self.despachados], [7])
        fuera = [e for e in eventos
                 if e["tipo"] == "fuera" and e["ref"] == "ticket/8"]
        self.assertTrue(fuera, eventos)
        self.assertIn("plan aprobado", fuera[0]["cuerpo"])

    def test_ticket_claude_nuevo_si_se_despacha(self):
        self._cfg(topes={"max_usd": 5})
        plan = self._planear_y_aprobar(["--loop"])
        self._pasada(["--loop"], [self._issue(7), self._issue(8)], plan)
        self.assertEqual(sorted(j.issue for j in self.despachados), [7, 8])
        self.assertEqual({j.kind for j in self.despachados}, {"claude"})

    def test_el_tope_no_sube_sobre_el_aprobado(self):
        """La config se relee en cada pasada (#163): subir topes.max_usd a
        mitad de la noche no sube el tope de una corrida aprobada."""
        self._cfg(escalera="pi", topes={"max_usd": 5})
        plan = self._planear_y_aprobar(["--loop"])
        self._cfg(escalera="pi", topes={"max_usd": 500})
        self._pasada(["--loop"], [self._issue(7)], plan)
        self.assertEqual(self.cli.TOPES_GASTO["h"].max_usd, 5)


class SinAprobacionManual(_Cli):

    def test_manual_todo_claude_corre_con_el_flag(self):
        self._cfg()
        code, _, err = self._main(["run", "--only", "7", "--sin-aprobacion"])
        self.assertEqual(code, 0, err)
        self.assertEqual([j.issue for j in self._tickets(self.despachados)],
                         [7])

    def test_manual_con_pi_se_niega_aun_con_el_flag(self):
        self._cfg(topes={"max_usd": 5})
        code, _, err = self._main(["run", "--only", "7", "--kind", "pi",
                                   "--sin-aprobacion"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.despachados, [])
        self.assertEqual(self._efectos(), [])

    def test_sin_aprobacion_no_vale_en_el_loop(self):
        self._cfg()
        code, _, _ = self._main(["run", "--loop", "--sin-aprobacion",
                                 "--sin-proponer"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.despachados, [])

    def test_only_sin_flag_tambien_pide_aprobacion(self):
        self._cfg()
        code, _, _ = self._main(["run", "--only", "7"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.despachados, [])


if __name__ == "__main__":
    unittest.main()
