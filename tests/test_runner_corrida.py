"""La corrida dice con qué runner arranca, y eso manda (#174, #177).

La noche del 2026-10-18 `harness run --loop --kind claude` despachó los 49
jobs a pi/Qwen: cada job traía su peldaño de `ESCALERA` ya resuelto, el
peldaño 1 es pi, y `job.kind or spec.kind` dejaba que el peldaño ganara
siempre. Costó US$17 de OpenRouter que nadie pidió gastar.

Las reglas que se testean acá:

- `kind=claude` (el default): la escalera de la corrida es solo Claude
  (sonnet → opus). Ningún job va a pi.
- pi solo si el ticket trae `runner:cheap` Y la corrida lo permite
  (`--permitir-pi`, por defecto no).
- `kind=pi`: la escalera completa de siempre (pi → pi high → sonnet → opus).
- Un repo con `runner_min: claude` en la config nunca recibe un peldaño pi,
  ni con `runner:cheap`, ni en una corrida `kind=pi` (#177).
- Los tres sitios de `bin/harness` (implementador en `run`, implementador
  en cada pasada del `--loop`, mantenedor) pasan por `elegir_peldano`.
- La línea `peldano` del log dice el runner elegido y por qué.
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support

from harness import costo
from harness import dispatch as dispatch_mod
from harness.dispatch import DispatchSpec, Dispatcher
from harness.proposal import GENERADOR, JUEZ_CLAUDE, JUEZ_QWEN
from harness.review import Revisor
from harness.config import ConfigError, parse_config
from harness.dispatch import (ESCALERA, ESCALERA_CLAUDE, Corrida, EventLog,
                              elegir_peldano, peldano_de)

from test_dispatch import Mundo, despachar, job

HARNESS = support.ROOT / "bin" / "harness"

SONNET = ESCALERA[2]
OPUS = ESCALERA[3]


def ev(tipo, cuerpo, run_id, ticket="F7League#7", clase=None):
    return {"timestamp": "2026-10-18T02:00:00Z", "contexto": "h",
            "origen": "harness", "run_id": run_id, "ticket": ticket,
            "attempt": 1, "tipo": tipo, "ref": "ticket/7", "cuerpo": cuerpo,
            "clase": clase}


def abandono_en(runner, run_id, ticket="F7League#7", clase="modelo"):
    """Un intento que llegó a arrancar el agente (`runner`) y se abandonó."""
    return [ev("agente", "t7 (kind {}, pane w1:p1)".format(runner), run_id,
               ticket),
            ev("abandono", "gate rojo en el worktree: sin PR", run_id, ticket,
               clase=clase)]


class LaEscaleraDeLaCorrida(unittest.TestCase):
    """`elegir_peldano`: puro, sobre el historial del log."""

    def test_escalera_claude_es_solo_claude_sonnet_y_opus(self):
        self.assertEqual([p["kind"] for p in ESCALERA_CLAUDE],
                         ["claude", "claude"])
        self.assertEqual(ESCALERA_CLAUDE, (SONNET, OPUS))

    def test_el_default_de_la_corrida_es_claude_y_sin_pi(self):
        c = Corrida()
        self.assertEqual(c.kind, "claude")
        self.assertFalse(c.permitir_pi)

    def test_noche_2026_10_18_kind_claude_tres_tickets_sin_etiqueta(self):
        """El escenario exacto: --kind claude, tickets sin etiqueta, sin
        historial. Los tres jobs van a Claude; ninguno a pi."""
        corrida = Corrida(kind="claude", fuente="--kind")
        for n in (7, 8, 9):
            p, motivo = elegir_peldano([], corrida, "F7League", n)
            self.assertEqual(p["kind"], "claude", motivo)
            self.assertEqual(p, SONNET)
            self.assertIn("claude", motivo)
            self.assertIn("--kind", motivo)

    def test_kind_claude_con_runner_cheap_sin_permitir_pi_va_a_claude(self):
        corrida = Corrida(kind="claude", fuente="--kind")
        p, motivo = elegir_peldano([], corrida, "koku", 7,
                                   etiquetas=["ready-for-agent", "runner:cheap"])
        self.assertEqual(p["kind"], "claude")
        self.assertIn("runner:cheap", motivo)
        self.assertIn("ignorada", motivo)
        self.assertIn("--permitir-pi", motivo)

    def test_kind_claude_con_runner_cheap_y_permitir_pi_va_a_pi(self):
        corrida = Corrida(kind="claude", fuente="--kind", permitir_pi=True)
        p, motivo = elegir_peldano([], corrida, "koku", 7,
                                   etiquetas=["runner:cheap"])
        self.assertEqual(p, ESCALERA[0])
        self.assertIn("runner:cheap", motivo)
        self.assertIn("--permitir-pi", motivo)

    def test_permitir_pi_sin_etiqueta_no_alcanza(self):
        """--permitir-pi solo habilita: el ticket tiene que pedirlo."""
        corrida = Corrida(kind="claude", permitir_pi=True)
        p, _ = elegir_peldano([], corrida, "koku", 7, etiquetas=["ready-for-agent"])
        self.assertEqual(p["kind"], "claude")

    def test_kind_claude_con_un_abandono_previo_va_a_opus(self):
        corrida = Corrida(kind="claude")
        eventos = abandono_en("claude", "r1")
        p, motivo = elegir_peldano(eventos, corrida, "F7League", 7)
        self.assertEqual(p, OPUS, motivo)

    def test_kind_claude_nunca_pi_con_ningun_historial(self):
        corrida = Corrida(kind="claude")
        for runner in ("pi", "claude"):
            eventos = []
            for i in range(6):
                p, motivo = elegir_peldano(eventos, corrida, "F7League", 7)
                if p is not None:
                    self.assertEqual(p["kind"], "claude", motivo)
                eventos += abandono_en(runner, "r{}".format(i))

    def test_kind_claude_tras_abandonos_en_pi_arranca_en_sonnet(self):
        """Los abandonos de pi de una corrida vieja no gastan peldaños de la
        escalera Claude: un ticket que Qwen tumbó dos veces no se parkea sin
        que Claude lo haya intentado."""
        corrida = Corrida(kind="claude")
        eventos = abandono_en("pi", "r1") + abandono_en("pi", "r2")
        p, motivo = elegir_peldano(eventos, corrida, "F7League", 7)
        self.assertEqual(p, SONNET, motivo)

    def test_kind_claude_agotada_tras_sonnet_y_opus_parkea(self):
        corrida = Corrida(kind="claude")
        eventos = abandono_en("claude", "r1") + abandono_en("claude", "r2")
        p, motivo = elegir_peldano(eventos, corrida, "F7League", 7)
        self.assertIsNone(p)
        self.assertIn("agotada", motivo)

    def test_abandono_infra_no_escala_en_claude(self):
        corrida = Corrida(kind="claude")
        eventos = abandono_en("claude", "r1", clase="infra")
        p, _ = elegir_peldano(eventos, corrida, "F7League", 7)
        self.assertEqual(p, SONNET)

    def test_kind_pi_comportamiento_actual_intacto(self):
        """--kind pi: la escalera completa, peldaño por abandonos que
        escalan, sea cual sea el runner de cada abandono (como antes)."""
        corrida = Corrida(kind="pi", fuente="--kind")
        eventos = []
        for i in range(len(ESCALERA) + 1):
            p, motivo = elegir_peldano(eventos, corrida, "koku", 7)
            self.assertEqual(p, peldano_de(i), motivo)
            eventos += abandono_en(ESCALERA[min(i, 3)]["kind"],
                                   "r{}".format(i), ticket="koku#7")
        p, motivo = elegir_peldano([], corrida, "koku", 7)
        self.assertIn("--kind pi", motivo)

    def test_kind_pi_ignora_la_etiqueta(self):
        corrida = Corrida(kind="pi")
        p, _ = elegir_peldano([], corrida, "koku", 7, etiquetas=["runner:cheap"])
        self.assertEqual(p, ESCALERA[0])

    # ---------------------------------------------------- runner_min (#177)
    def test_runner_min_claude_con_cheap_y_kind_pi_va_a_claude(self):
        corrida = Corrida(kind="pi", permitir_pi=True,
                          runner_min={"F7League": "claude"})
        p, motivo = elegir_peldano([], corrida, "F7League", 7,
                                   etiquetas=["runner:cheap"])
        self.assertEqual(p["kind"], "claude", motivo)
        self.assertIn("runner_min del repo", motivo)

    def test_runner_min_claude_nunca_pi_con_historial(self):
        corrida = Corrida(kind="pi", permitir_pi=True,
                          runner_min={"F7League": "claude"})
        eventos = []
        for i in range(4):
            p, motivo = elegir_peldano(eventos, corrida, "F7League", 7,
                                       etiquetas=["runner:cheap"])
            if p is not None:
                self.assertEqual(p["kind"], "claude", motivo)
            eventos += abandono_en("claude", "r{}".format(i))

    def test_runner_min_solo_aplica_a_su_repo(self):
        corrida = Corrida(kind="pi", runner_min={"F7League": "claude"})
        p, _ = elegir_peldano([], corrida, "koku", 7)
        self.assertEqual(p, ESCALERA[0])

    def test_sin_runner_min_comportamiento_actual(self):
        corrida = Corrida(kind="pi")
        for i in range(len(ESCALERA) + 1):
            eventos = []
            for j in range(i):
                eventos += abandono_en("pi", "r{}".format(j))
            p, _ = elegir_peldano(eventos, corrida, "F7League", 7)
            self.assertEqual(p, peldano_de(i))

    # ---------------------------------------------------------- mantenedor
    def test_mantenedor_usa_la_clave_del_pr(self):
        corrida = Corrida(kind="claude")
        eventos = abandono_en("claude", "r1", ticket="koku#pr31")
        p, _ = elegir_peldano(eventos, corrida, "koku", 31, pr=True)
        self.assertEqual(p, OPUS)
        p, _ = elegir_peldano(eventos, corrida, "koku", 31)
        self.assertEqual(p, SONNET, "el abandono del PR no escala al ticket")


class LaConfig(unittest.TestCase):
    """`escalera` del contexto y `repos.runner_min` (#174, #177)."""

    def _ctx(self, **over):
        base = {"tracker": {"kind": "github"},
                "repos": {"root": "~", "paths": ["F7League"]},
                "autonomy": "frontier",
                "budget": {"polarity": "remaining", "provider": "none"},
                "run": {"kind": "local"}}
        base.update(over)
        return parse_config({"contexts": {"h": base}}).contexts[0]

    def test_sin_declarar_escalera_vacia_y_sin_runner_min(self):
        c = self._ctx()
        self.assertEqual(c.escalera, "")
        self.assertEqual(c.repos.runner_min, {})

    def test_escalera_claude_o_pi(self):
        self.assertEqual(self._ctx(escalera="claude").escalera, "claude")
        self.assertEqual(self._ctx(escalera="pi").escalera, "pi")

    def test_escalera_desconocida_es_error(self):
        with self.assertRaises(ConfigError) as cm:
            self._ctx(escalera="codex")
        self.assertIn("escalera", str(cm.exception))

    def test_runner_min_por_repo(self):
        c = self._ctx(repos={"root": "~", "paths": ["F7League"],
                             "runner_min": {"F7League": "claude"}})
        self.assertEqual(c.repos.runner_min, {"F7League": "claude"})

    def test_runner_min_invalido_es_error(self):
        for malo in ({"F7League": "qwen"}, ["F7League"], {"F7League": 1}):
            with self.assertRaises(ConfigError) as cm:
                self._ctx(repos={"root": "~", "paths": [],
                                 "runner_min": malo})
            self.assertIn("runner_min", str(cm.exception))


class LaLineaPeldanoDiceElPorQue(unittest.TestCase):
    def test_el_motivo_va_en_la_linea_peldano(self):
        p = SONNET
        j = job(kind=p["kind"], model=p["model"], extra_args=p["extra_args"])
        j.motivo_peldano = "corrida claude (--kind): escalera solo Claude"
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(dispatch_mod, "CLAUDE_JSON",
                               Path(tmp) / "claude.json"):
            _, lineas = despachar(Mundo(), [j])
        (linea,) = [l for l in lineas if l["tipo"] == "peldano"]
        self.assertTrue(linea["cuerpo"].startswith("peldaño 3 · claude sonnet"),
                        linea["cuerpo"])
        self.assertIn("corrida claude (--kind): escalera solo Claude",
                      linea["cuerpo"])

    def test_sin_motivo_la_linea_de_siempre(self):
        _, lineas = despachar(Mundo(), [job(kind="pi", model="qwen/qwen3.8-27b",
                                            extra_args=("--thinking", "medium"))])
        (linea,) = [l for l in lineas if l["tipo"] == "peldano"]
        self.assertEqual(linea["cuerpo"],
                         "peldaño 1 · pi qwen/qwen3.8-27b --thinking medium")


class _CliBase(unittest.TestCase):
    """El CLI real (`bin/harness`) con el mundo de mentira."""

    def _cfg(self, escalera=None, runner_min=None, topes=None):
        repos = {"root": str(self.d / "repos"), "paths": ["F7League"]}
        if runner_min is not None:
            repos["runner_min"] = runner_min
        context = {"tracker": {"kind": "github"}, "repos": repos,
                   "autonomy": "frontier",
                   "budget": {"polarity": "remaining", "provider": "none"},
                   "run": {"kind": "local"}}
        if escalera is not None:
            context["escalera"] = escalera
        if topes is not None:
            context["topes"] = topes
        self.d.joinpath("config.json").write_text(
            json.dumps({"default_context": "h", "contexts": {"h": context}}))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.log_path = self.d / "events.jsonl"
        self.d.joinpath("repos", "F7League").mkdir(parents=True)
        loader = SourceFileLoader("harness_cli_runner", str(HARNESS))
        spec = importlib.util.spec_from_loader("harness_cli_runner", loader)
        self.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(self.cli)

    def tearDown(self):
        self.tmp.cleanup()

    def _issue(self, n, *etiquetas):
        return {"number": n, "title": "ticket {}".format(n),
                "labels": [{"name": e} for e in ("ready-for-agent",) + etiquetas],
                "body": "Sin bloqueos."}

    def _raw(self, issues, prs=None):
        return {
            "offline": False, "agents": [],
            "contexts": [{
                "name": "h", "tracker": "github", "autonomy": "frontier",
                "vault": None, "run": "local",
                "budget": {"polarity": "remaining", "provider": "none",
                           "credits": None, "total": 0.0, "used": 0.0},
                "repos": [{
                    "name": "F7League", "tracker": "github",
                    "slug": "Drokoz/F7League", "branch": "main",
                    "default_branch": "main",
                    "readiness_source": "default-branch",
                    "status_porcelain": "",
                    "path": str(self.d / "repos" / "F7League"),
                    "exists": {"gate": True, "skills": True, "context": True},
                    "issues": issues, "prs": prs or [], "prs_merged": [],
                    "closed_count": 0,
                }],
            }],
        }

    def _correr(self, argv, issues, prs=None, conflicto=False, loop=False):
        """Corre `run` (o una pasada del loop) y devuelve
        `(jobs despachados, eventos del log)`. Las specs con que se
        despachó quedan en `self.specs`."""
        despachados = []
        self.specs = []

        def fake_dispatch(disp, jobs):
            despachados.extend(jobs)
            self.specs.append(disp.spec)
            return jobs

        args = self.cli.build_parser().parse_args(
            ["run", "--context", "h", "--log", str(self.log_path)] + argv)
        mundo = Mundo(prs=[])
        with mock.patch.object(self.cli, "OFFLINE", False), \
             mock.patch.object(self.cli, "collect",
                               return_value=self._raw(issues, prs)), \
             mock.patch.object(self.cli.adapters, "run", mundo.cmd), \
             mock.patch.object(self.cli, "mergeable_de",
                               return_value=("CONFLICTING" if conflicto
                                             else "MERGEABLE")), \
             mock.patch.object(self.cli, "cosechar", lambda *a, **k: None), \
             mock.patch.object(self.cli, "_cuota_de",
                               return_value=SimpleNamespace(
                                   permitir_claude=None)), \
             mock.patch.object(self.cli, "_filtrar_por_cuota",
                               side_effect=lambda a, c, cands, l: cands), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None,
                                             lambda *a: None)), \
             mock.patch.object(dispatch_mod.Dispatcher, "dispatch",
                               autospec=True, side_effect=fake_dispatch), \
             mock.patch.dict(os.environ, {"HERDR_ENV": "1",
                                          "HOME": str(self.d),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch("sys.stdout", new=io.StringIO()):
            ctx = self.cli.load_config().select("h")
            if loop:
                log = EventLog(self.log_path, "h")
                self.cli._una_pasada(args, ctx[0], log)
            else:
                self.assertEqual(self.cli.run_main(args, ctx), 0)
        eventos = []
        if self.log_path.exists():
            eventos = [json.loads(l) for l in
                       self.log_path.read_text().splitlines() if l]
        return despachados, eventos

    def _tickets(self, jobs):
        return [j for j in jobs if hasattr(j, "issue") and not hasattr(j, "pr")]


class LosTresSitiosDelCli(_CliBase):
    """`run` (una pasada), `run --loop` (cada pasada) y el mantenedor arman
    sus jobs con `elegir_peldano`: se corre el CLI real con el mundo de
    mentira y se miran los jobs que llegan a `Dispatcher.dispatch`."""

    def test_noche_2026_10_18_run_kind_claude(self):
        self._cfg()
        jobs, _ = self._correr(["--kind", "claude"],
                               [self._issue(n) for n in (7, 8, 9)])
        self.assertEqual(len(jobs), 3)
        self.assertEqual([j.kind for j in jobs], ["claude"] * 3)
        for j in jobs:
            self.assertIn("--kind", j.motivo_peldano)

    def test_noche_2026_10_18_loop_kind_claude(self):
        self._cfg()
        jobs, _ = self._correr(["--loop", "--kind", "claude"],
                               [self._issue(n) for n in (7, 8, 9)], loop=True)
        self.assertEqual(len(jobs), 3)
        self.assertEqual([j.kind for j in jobs], ["claude"] * 3)

    def test_sin_kind_ni_config_la_corrida_es_claude(self):
        self._cfg()
        jobs, _ = self._correr([], [self._issue(7)])
        self.assertEqual([j.kind for j in jobs], ["claude"])
        self.assertIn("por defecto", jobs[0].motivo_peldano)

    def test_la_config_escalera_pi_es_preferencia_de_la_corrida(self):
        self._cfg(escalera="pi")
        jobs, _ = self._correr([], [self._issue(7)])
        self.assertEqual([j.kind for j in jobs], ["pi"])
        self.assertIn("config", jobs[0].motivo_peldano)

    def test_el_flag_gana_sobre_la_config(self):
        self._cfg(escalera="pi")
        jobs, _ = self._correr(["--kind", "claude"], [self._issue(7)])
        self.assertEqual([j.kind for j in jobs], ["claude"])

    def test_kind_pi_escalera_completa(self):
        self._cfg()
        jobs, _ = self._correr(["--kind", "pi"], [self._issue(7)])
        self.assertEqual((jobs[0].kind, jobs[0].model, jobs[0].extra_args),
                         (ESCALERA[0]["kind"], ESCALERA[0]["model"],
                          ESCALERA[0]["extra_args"]))

    def test_runner_cheap_necesita_permitir_pi(self):
        self._cfg()
        jobs, _ = self._correr(["--kind", "claude"],
                               [self._issue(7, "runner:cheap")])
        self.assertEqual([j.kind for j in jobs], ["claude"])
        self.assertIn("ignorada", jobs[0].motivo_peldano)
        jobs, _ = self._correr(["--kind", "claude", "--permitir-pi"],
                               [self._issue(7, "runner:cheap")])
        self.assertEqual([j.kind for j in jobs], ["pi"])

    def test_runner_min_del_repo_en_la_config(self):
        self._cfg(runner_min={"F7League": "claude"})
        jobs, _ = self._correr(["--kind", "pi", "--permitir-pi"],
                               [self._issue(7, "runner:cheap")])
        self.assertEqual([j.kind for j in jobs], ["claude"])
        self.assertIn("runner_min del repo", jobs[0].motivo_peldano)

    def test_el_mantenedor_tambien_respeta_la_corrida(self):
        self._cfg()
        jobs, _ = self._correr(
            ["--kind", "claude"], [],
            prs=[{"number": 31, "title": "x", "isDraft": False,
                  "headRefName": "ticket/7"}],
            conflicto=True)
        mant = [j for j in jobs if hasattr(j, "pr")]
        self.assertEqual(len(mant), 1, jobs)
        self.assertEqual(mant[0].kind, "claude")
        self.assertTrue(mant[0].motivo_peldano)

    def test_kind_desconocido_no_se_acepta(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                self.cli.build_parser().parse_args(["run", "--kind", "codex"])

    def test_la_corrida_anota_su_runner_y_por_que(self):
        self._cfg()
        _, eventos = self._correr(["--kind", "claude"], [self._issue(7)])
        lineas = [e["cuerpo"] for e in eventos if e["tipo"] == "corrida"]
        self.assertTrue(any("runner de la corrida: claude" in l
                            and "--kind" in l for l in lineas), lineas)


class ElRevisorDeLaCorrida(unittest.TestCase):
    """El revisor de PRs (#46) tampoco es pi en una corrida Claude: con
    `--kind claude` sin `--permitir-pi`, la review corre en Claude, sonnet,
    sólo con tools de lectura."""

    def test_revisor_claude_lee_solo(self):
        llamadas = []

        def run_cmd(args, cwd=None, timeout=30):
            llamadas.append(list(args))
            if args[:3] == ["gh", "pr", "diff"]:
                return True, "diff --git a/x b/x"
            return True, "[]"

        Revisor(run_cmd, kind="claude", model="sonnet").revisar_pr("a/b", 1)
        (args,) = [a for a in llamadas if a[0] != "gh"]
        self.assertEqual(args[0], "claude")
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(args[args.index("--permission-mode") + 1], "plan")
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        for escribe in ("Bash", "Edit", "Write"):
            self.assertNotIn(escribe, args[args.index("--tools") + 1])

    def test_spec_por_defecto_es_claude_y_revisor_claude(self):
        spec = DispatchSpec(contexto="t")
        self.assertEqual(spec.kind, "claude")
        self.assertFalse(spec.revisor_pi)
        with tempfile.TemporaryDirectory() as tmp:
            d = Dispatcher(spec, EventLog(Path(tmp) / "e.jsonl", "t"),
                           Mundo().cmd)
            self.assertEqual(d._revisor().kind, "claude")
            self.assertEqual(d._revisor().model, "sonnet")

    def test_revisor_pi_solo_si_la_corrida_lo_permite(self):
        spec = DispatchSpec(contexto="t", revisor_pi=True)
        with tempfile.TemporaryDirectory() as tmp:
            d = Dispatcher(spec, EventLog(Path(tmp) / "e.jsonl", "t"),
                           Mundo().cmd)
            self.assertEqual(d._revisor().kind, "pi")


class LaSpecDeLaCorrida(_CliBase):
    """run, run --loop y maintain arman la spec con el runner de la
    corrida y `revisor_pi` según la corrida."""

    def test_spec_kind_y_revisor_en_run_y_loop(self):
        self._cfg()
        for loop in (False, True):
            argv = ["--loop"] if loop else []
            self._correr(argv + ["--kind", "claude"], [self._issue(7)],
                         loop=loop)
            self.assertEqual([(s.kind, s.revisor_pi) for s in self.specs],
                             [("claude", False)], loop)
            self._correr(argv + ["--kind", "claude", "--permitir-pi"],
                         [self._issue(7)], loop=loop)
            self.assertEqual([(s.kind, s.revisor_pi) for s in self.specs],
                             [("claude", True)], loop)
            self._correr(argv + ["--kind", "pi"], [self._issue(7)], loop=loop)
            self.assertEqual([(s.kind, s.revisor_pi) for s in self.specs],
                             [("pi", True)], loop)


class ElProponerDeLaCorrida(_CliBase):
    """El generador y el juez de `proponer` (#115) siguen la corrida: en
    una corrida Claude sin `--permitir-pi`, generador Claude (sonnet) y
    juez Claude, aunque se pida `--juez-qwen`."""

    def _proponer(self, argv, en_bucle=True):
        visto = []

        def fake(prop, repo, slug, repo_path, maximo, peldano_juez,
                 generador=GENERADOR):
            visto.append((prop.spec.kind, generador, peldano_juez))
            return {"abiertas": 0, "promovidas": 0, "rechazadas": 0,
                    "descartadas": 0, "detalle": []}

        args = self.cli.build_parser().parse_args(
            ["proponer", "--context", "h", "--log", str(self.log_path)] + argv)
        with mock.patch.object(self.cli, "collect",
                               return_value=self._raw([])), \
             mock.patch.object(self.cli.Proponer, "proponer_repo",
                               autospec=True, side_effect=fake), \
             mock.patch.object(self.cli, "_creditos_usado",
                               return_value=None), \
             mock.patch.dict(os.environ, {"HOME": str(self.d),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch("sys.stdout", new=io.StringIO()):
            ctx = self.cli.load_config().select("h")[0]
            self.cli._proponer_ctx(args, ctx, EventLog(self.log_path, "h"),
                                   en_bucle=en_bucle)
        return visto

    def test_corrida_claude_generador_y_juez_claude(self):
        self._cfg()
        for en_bucle in (True, False):
            (v,) = self._proponer(["--kind", "claude", "--juez-qwen"],
                                  en_bucle=en_bucle)
            self.assertEqual(v[1], ESCALERA_CLAUDE[0])
            self.assertEqual(v[2], JUEZ_CLAUDE)
            self.assertEqual(v[0], "claude")

    def test_sin_kind_tambien_claude(self):
        self._cfg()
        (v,) = self._proponer(["--juez-qwen"])
        self.assertEqual((v[1], v[2]), (ESCALERA_CLAUDE[0], JUEZ_CLAUDE))

    def test_corrida_pi_comportamiento_de_siempre(self):
        self._cfg()
        (v,) = self._proponer(["--kind", "pi", "--juez-qwen"])
        self.assertEqual((v[1], v[2]), (GENERADOR, JUEZ_QWEN))

    def test_runner_min_del_repo_fuerza_claude_en_proponer(self):
        self._cfg(runner_min={"F7League": "claude"})
        (v,) = self._proponer(["--kind", "pi", "--juez-qwen"])
        self.assertEqual((v[1], v[2]), (ESCALERA_CLAUDE[0], JUEZ_CLAUDE))


class NingunComandoAPi(_CliBase):
    """Regresión de la noche del 2026-10-18, de punta a punta: una corrida
    `kind=claude` entera (ticket, PR abierto y su review) más una pasada de
    proponer del `--loop` con la frontera vacía. Ningún comando registrado
    en el mundo arranca pi: ni `pi ...` ni `herdr agent start --kind pi`."""

    def _sin_pi(self, mundo):
        for args, _, _ in mundo.llamadas:
            self.assertNotEqual(args[0], "pi", args)
            for i, a in enumerate(args[:-1]):
                if a == "--kind":
                    self.assertNotEqual(args[i + 1], "pi", args)

    def test_corrida_claude_entera_sin_pi(self):
        # Con plata de sobra (#165): que el tope de gasto no tape a pi.
        self._cfg(topes={"max_usd": 100})
        mundo = Mundo(prs=[{"number": 31, "headRefName": "ticket/7"}])
        args = self.cli.build_parser().parse_args(
            ["run", "--context", "h", "--log", str(self.log_path), "-q",
             "--kind", "claude", "--sin-proponer"])
        with mock.patch.object(self.cli, "OFFLINE", False), \
             mock.patch.object(self.cli, "collect",
                               return_value=self._raw([self._issue(7)])), \
             mock.patch.object(self.cli.adapters, "run", mundo.cmd), \
             mock.patch.object(self.cli, "_cuota_de",
                               return_value=SimpleNamespace(
                                   permitir_claude=None)), \
             mock.patch.object(self.cli, "_filtrar_por_cuota",
                               side_effect=lambda a, c, cands, l: cands), \
             mock.patch.object(self.cli, "_creditos_usado",
                               return_value=None), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None,
                                             lambda *a: None)), \
             mock.patch.dict(os.environ, {"HERDR_ENV": "1",
                                          "HOME": str(self.d),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch("time.sleep"), \
             mock.patch("sys.stdout", new=io.StringIO()):
            ctxs = self.cli.load_config().select("h")
            self.assertEqual(self.cli.run_main(args, ctxs), 0)
            # La pasada del --loop con la frontera vacía: proponer.
            self.cli._proponer_ctx(args, ctxs[0],
                                   EventLog(self.log_path, "h"),
                                   en_bucle=True)
        eventos = [json.loads(l) for l in
                   self.log_path.read_text().splitlines() if l]
        self.assertTrue(any(e["tipo"] == "pr" for e in eventos),
                        "la corrida abrió el PR")
        self.assertTrue(any(e["tipo"] == "review" for e in eventos),
                        "el PR pasó por el revisor")
        self.assertTrue(mundo.llamo("herdr", "agent", "start"),
                        "arrancó algún agente")
        self._sin_pi(mundo)
        self.assertTrue(any(a[0] == "claude" for a, _, _ in mundo.llamadas),
                        "el revisor corrió en Claude")


class ElCostoDiceElPeldanoUsado(unittest.TestCase):
    """`harness costo` etiqueta cada intento con el peldaño que de verdad
    corrió (la línea `peldano`, o el runner del agente), no con el que
    habría tocado en la escalera completa."""

    def test_peldano_de_la_linea_del_log(self):
        evs = [ev("peldano", "peldaño 3 · claude sonnet --effort medium · "
                  "por qué: corrida claude (--kind): escalera solo Claude",
                  "r1")] + abandono_en("claude", "r1")
        evs += [ev("peldano", "peldaño 4 · claude opus --effort medium · "
                   "por qué: corrida claude (--kind): escalera solo Claude",
                   "r2")] + abandono_en("claude", "r2")
        t = costo.ticket_de([], evs, "F7League", 7)
        self.assertEqual([i["peldano"] for i in t["intentos"]], [3, 4])
        self.assertEqual([i["peldano_etiqueta"] for i in t["intentos"]],
                         ["peldaño 3 · claude sonnet --effort medium",
                          "peldaño 4 · claude opus --effort medium"])

    def test_sin_linea_peldano_el_runner_del_agente_manda(self):
        """Log sin línea `peldano` y agente en claude: no se lo etiqueta
        como el peldaño 1 de pi."""
        t = costo.ticket_de([], abandono_en("claude", "r1"), "F7League", 7)
        (i,) = t["intentos"]
        self.assertNotEqual(i["peldano"], 1)
        self.assertNotIn("pi", i["peldano_etiqueta"] or "")

    def test_log_viejo_de_pi_sigue_igual(self):
        evs = abandono_en("pi", "r1") + abandono_en("pi", "r2")
        t = costo.ticket_de([], evs, "F7League", 7)
        self.assertEqual([i["peldano"] for i in t["intentos"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
