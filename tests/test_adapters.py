"""Tests de la capa de adaptadores: que traiga datos crudos y que no decida nada.

Ningún test acá sale a la red ni llama a `gh`/`herdr` de verdad: `run` se
reemplaza por una función que devuelve lo que devolvería el comando.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

from harness import adapters
from harness.config import ConfigError, Repos, default_config


def fake_run(respuestas):
    """Un `run` que contesta según el comando, y registra qué se le pidió."""
    llamadas = []

    def _run(args, cwd=None, timeout=30):
        llamadas.append(list(args))
        for prefijo, salida in respuestas.items():
            if " ".join(args).startswith(prefijo):
                return salida
        return False, ""

    _run.llamadas = llamadas
    return _run


class TestRun(unittest.TestCase):
    def test_comando_inexistente_no_levanta(self):
        self.assertEqual(adapters.run(["no-existe-este-comando-42"]), (False, ""))

    def test_codigo_de_salida_distinto_de_cero(self):
        self.assertEqual(adapters.run(["sh", "-c", "echo hola; exit 3"]), (False, "hola"))

    def test_devuelve_stdout_pelado(self):
        self.assertEqual(adapters.run(["sh", "-c", "echo  hola  "]), (True, "hola"))


class TestGhJson(unittest.TestCase):
    def test_parsea(self):
        run = fake_run({"gh issue": (True, json.dumps([{"number": 1}]))})
        with mock.patch.object(adapters, "run", run):
            self.assertEqual(adapters.gh_json("o/r", ["issue", "list"]), [{"number": 1}])
        self.assertEqual(run.llamadas[0][-2:], ["-R", "o/r"])

    def test_gh_caido_es_none(self):
        with mock.patch.object(adapters, "run", fake_run({})):
            self.assertIsNone(adapters.gh_json("o/r", ["issue", "list"]))

    def test_json_roto_es_none(self):
        run = fake_run({"gh": (True, "no soy json")})
        with mock.patch.object(adapters, "run", run):
            self.assertIsNone(adapters.gh_json("o/r", ["pr", "list"]))


class TestHerdr(unittest.TestCase):
    def test_fuera_de_herdr_es_none(self):
        with mock.patch.dict("os.environ", {"HERDR_ENV": "0"}):
            self.assertIsNone(adapters.herdr_agents())

    def test_offline_es_none(self):
        with mock.patch.dict("os.environ", {"HERDR_ENV": "1"}), \
                mock.patch.object(adapters, "run",
                                  mock.Mock(side_effect=AssertionError("llamó a herdr"))):
            self.assertIsNone(adapters.herdr_agents(offline=True))

    def test_desenvuelve_el_result(self):
        crudo = json.dumps({"id": "cli:agent:list", "result": {"agents": [{"agent": "claude"}]}})
        with mock.patch.dict("os.environ", {"HERDR_ENV": "1"}), \
                mock.patch.object(adapters, "run", fake_run({"herdr": (True, crudo)})):
            self.assertEqual(adapters.herdr_agents(), [{"agent": "claude"}])

    def test_herdr_caido_es_lista_vacia(self):
        """Distinto de None: estamos en una sesión, sólo que no contestó."""
        with mock.patch.dict("os.environ", {"HERDR_ENV": "1"}), \
                mock.patch.object(adapters, "run", fake_run({})):
            self.assertEqual(adapters.herdr_agents(), [])


class TestOpenRouter(unittest.TestCase):
    def test_offline_ni_abre_el_archivo(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("llamada de red en modo offline")):
            self.assertIsNone(adapters.openrouter_credits(offline=True))

    def test_sin_key_es_none(self):
        with mock.patch.object(adapters, "PI_MODELS", Path("/no/existe/models.json")):
            self.assertIsNone(adapters.openrouter_credits())


class TestGhGraphqlIssues(unittest.TestCase):
    def test_trae_dependencias_nativas(self):
        fixture = json.dumps(support.fixture("gh_issue_graphql.json"))
        run = fake_run({"gh api graphql": (True, fixture)})
        with mock.patch.object(adapters, "run", run):
            issues, cerrados = adapters.gh_graphql_issues("Drokoz/agent-harness")
        por_num = {i["number"]: i for i in issues}
        self.assertIsInstance(cerrados, (int, type(None)))
        self.assertEqual(sorted(por_num), [1, 4, 5, 6, 7, 8, 9, 12, 15])
        self.assertEqual(por_num[8]["blocked_by"], 2)   # dos bloqueantes abiertos
        self.assertEqual(por_num[12]["blocked_by"], 0)  # bloqueante cerrado: no cuenta
        self.assertEqual(por_num[12]["labels"], [{"name": "ready-for-agent"}])

    def test_gh_caido_es_none(self):
        with mock.patch.object(adapters, "run", fake_run({})):
            self.assertEqual(adapters.gh_graphql_issues("o/r"), (None, None))

    def test_respuesta_con_errors_es_none(self):
        malo = json.dumps({"data": {"repository": None}, "errors": [{"message": "boom"}]})
        with mock.patch.object(adapters, "run", fake_run({"gh api graphql": (True, malo)})):
            self.assertEqual(adapters.gh_graphql_issues("o/r"), (None, None))

    def test_json_roto_es_none(self):
        with mock.patch.object(adapters, "run",
                               fake_run({"gh api graphql": (True, "no soy json")})):
            self.assertEqual(adapters.gh_graphql_issues("o/r"), (None, None))

    def test_sin_slug_es_none(self):
        with mock.patch.object(adapters, "run",
                               mock.Mock(side_effect=AssertionError("llamó a gh"))):
            self.assertEqual(adapters.gh_graphql_issues("sin-barra"), (None, None))

    def test_pagina_hasta_dos_paginas(self):
        p1 = {"data": {"repository": {"issues": {
            "nodes": [{"number": 1, "issueDependenciesSummary": {"blockedBy": 0}}],
            "pageInfo": {"hasNextPage": True, "endCursor": "CUR"}}}}}
        p2 = {"data": {"repository": {"issues": {
            "nodes": [{"number": 2, "issueDependenciesSummary": {"blockedBy": 3}}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        llamadas = []

        def _run(args, cwd=None, timeout=30):
            llamadas.append(" ".join(args))
            return True, json.dumps(p1 if len(llamadas) == 1 else p2)

        with mock.patch.object(adapters, "run", _run):
            issues, _ = adapters.gh_graphql_issues("o/r")
        self.assertEqual([i["number"] for i in issues], [1, 2])
        self.assertEqual(issues[1]["blocked_by"], 3)
        self.assertEqual(len(llamadas), 2)
        self.assertNotIn("after=", llamadas[0])
        self.assertIn("after=CUR", llamadas[1])

    def test_cerrados_se_leen_una_vez_y_sobreviven_a_la_paginacion(self):
        """El totalCount viene en la primera página; la segunda no lo repite y no
        debe borrarlo — si se pisara, un repo paginado reportaría 0 cerrados."""
        p1 = {"data": {"repository": {
            "cerrados": {"totalCount": 42},
            "issues": {"nodes": [{"number": 1}],
                       "pageInfo": {"hasNextPage": True, "endCursor": "CUR"}}}}}
        p2 = {"data": {"repository": {
            "issues": {"nodes": [{"number": 2}],
                       "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        n = []

        def _run(args, cwd=None, timeout=30):
            n.append(1)
            return True, json.dumps(p1 if len(n) == 1 else p2)

        with mock.patch.object(adapters, "run", _run):
            issues, cerrados = adapters.gh_graphql_issues("o/r")
        self.assertEqual(cerrados, 42)
        self.assertEqual([i["number"] for i in issues], [1, 2])


class TestConfigEnDisco(unittest.TestCase):
    def test_sin_archivo_es_la_config_de_arranque(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"HARNESS_CONFIG_DIR": tmp}):
                self.assertEqual(adapters.load_config(), default_config())

    def test_config_path_respeta_la_variable(self):
        with mock.patch.dict("os.environ", {"HARNESS_CONFIG_DIR": "/x/y"}):
            self.assertEqual(adapters.config_path(), Path("/x/y/config.json"))

    def test_config_path_por_defecto(self):
        entorno = {k: v for k, v in __import__("os").environ.items()
                   if k not in ("HARNESS_CONFIG_DIR", "XDG_CONFIG_HOME")}
        with mock.patch.dict("os.environ", entorno, clear=True):
            self.assertEqual(adapters.config_path(),
                             Path.home() / ".config" / "harness" / "config.json")

    def test_lee_la_del_usuario(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "config.json"
            conf.write_text(json.dumps({"contexts": {"trabajo": {
                "tracker": {"kind": "github"},
                "repos": {"root": tmp, "paths": []},
                "autonomy": "manual",
                "budget": {"polarity": "spent", "provider": "manual", "total": 200.0},
                "run": {"kind": "local"}}}}))
            cfg = adapters.load_config(conf)
        self.assertEqual(cfg.names, ["trabajo"])

    def test_json_roto_es_un_error_legible(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "config.json"
            conf.write_text("{no soy json")
            with self.assertRaises(ConfigError) as e:
                adapters.load_config(conf)
        self.assertIn("JSON inválido", str(e.exception))
        self.assertIn(str(conf), str(e.exception))

    def test_config_invalida_nombra_el_archivo(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "config.json"
            conf.write_text(json.dumps({"contexts": {"x": {}}}))
            with self.assertRaises(ConfigError) as e:
                adapters.load_config(conf)
        self.assertIn('contexto "x"', str(e.exception))


class TestRepos(unittest.TestCase):
    def test_ignora_los_que_no_estan(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "existe").mkdir()
            repos = adapters.repo_paths(Repos(root=tmp, paths=["existe", "no-existe"]))
        self.assertEqual([p.name for p in repos], ["existe"])

    def test_path_absoluto_no_cuelga_del_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "suelto").mkdir()
            repos = adapters.repo_paths(
                Repos(root="/no/existe", paths=[str(Path(tmp) / "suelto")]))
        self.assertEqual([p.name for p in repos], ["suelto"])

    def test_estrella_expande_los_repos_anidados(self):
        """El caso entrevestidos: una carpeta contenedora con un repo por subproyecto."""
        with tempfile.TemporaryDirectory() as tmp:
            for sub in ("ENTREVESTIDOS-BACK", "ENTREVESTIDOS-FRONT"):
                (Path(tmp) / "entrevestidos" / sub / ".git").mkdir(parents=True)
            (Path(tmp) / "entrevestidos" / "docs").mkdir()  # no es un repo
            repos = adapters.repo_paths(Repos(root=tmp, paths=["entrevestidos/*"]))
        self.assertEqual([p.name for p in repos],
                         ["ENTREVESTIDOS-BACK", "ENTREVESTIDOS-FRONT"])

    def test_la_carpeta_contenedora_y_sus_anidados_conviven(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ev" / ".git").mkdir(parents=True)
            (Path(tmp) / "ev" / "back" / ".git").mkdir(parents=True)
            repos = adapters.repo_paths(Repos(root=tmp, paths=["ev", "ev/*"]))
        self.assertEqual([p.name for p in repos], ["ev", "back"])

    def test_sin_duplicados(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "koku").mkdir()
            repos = adapters.repo_paths(Repos(root=tmp, paths=["koku", "koku"]))
        self.assertEqual(len(repos), 1)

    def test_estrella_sobre_una_carpeta_que_no_existe(self):
        self.assertEqual(adapters.repo_paths(Repos(root="/no/existe", paths=["x/*"])), [])

    def test_los_repos_de_la_config_por_defecto_son_directorios(self):
        for p in adapters.repo_paths(default_config().contexts[0].repos):
            self.assertTrue(p.is_dir())

    def test_collect_repo_trae_crudo(self):
        """El adaptador no interpreta: issues normalizados a la forma que lee
        snapshot. Aquí GraphQL cae y el fallback REST deja `blocked_by: None`."""
        issues = adapters.normalize_issues(support.gh_issues())
        run = fake_run({
            "git -C /x/koku branch": (True, "main"),
            "git -C /x/koku status": (True, " M a.py"),
            "git -C /x/koku remote": (True, "git@github.com:Drokoz/koku.git"),
            "gh issue": (True, json.dumps(support.gh_issues())),
            "gh pr": (True, "[]"),
        })
        with mock.patch.object(adapters, "run", run):
            raw = adapters.collect_repo(Path("/x/koku"))
        self.assertEqual(raw["slug"], "Drokoz/koku")
        self.assertEqual(raw["branch"], "main")
        self.assertEqual(raw["status_porcelain"], " M a.py")
        self.assertEqual(raw["issues"], issues)
        self.assertTrue(all(i["blocked_by"] is None for i in raw["issues"]))

    def test_collect_repo_con_dependencias_nativas(self):
        """Si GraphQL contesta, issues salen de ahí y no se llama al REST de issues."""
        g = json.dumps(support.fixture("gh_issue_graphql.json"))
        run = fake_run({
            "git -C /x/koku branch": (True, "main"),
            "git -C /x/koku status": (True, ""),
            "git -C /x/koku remote": (True, "git@github.com:Drokoz/koku.git"),
            "gh api graphql": (True, g),
            "gh pr": (True, "[]"),
        })
        with mock.patch.object(adapters, "run", run):
            raw = adapters.collect_repo(Path("/x/koku"))
        self.assertTrue(all(i["blocked_by"] is not None for i in raw["issues"]))
        self.assertFalse(any(" ".join(c).startswith("gh issue") for c in run.llamadas),
                         run.llamadas)
        self.assertEqual(raw["prs"], [])
        # nada de frontera/bloqueados/dirty: eso lo decide snapshot()
        self.assertEqual(set(raw) & {"frontier", "blocked", "dirty", "triage"}, set())

    def test_collect_repo_sin_remote_no_llama_a_gh(self):
        run = fake_run({"git -C /x/local branch": (True, "main")})
        with mock.patch.object(adapters, "run", run):
            raw = adapters.collect_repo(Path("/x/local"))
        self.assertIsNone(raw["slug"])
        self.assertIsNone(raw["issues"])
        self.assertTrue(all(c[0] == "git" for c in run.llamadas), run.llamadas)

    def test_collect_repo_offline_no_llama_a_gh(self):
        run = fake_run({
            "git -C /x/koku branch": (True, "main"),
            "git -C /x/koku remote": (True, "https://github.com/Drokoz/koku"),
        })
        with mock.patch.object(adapters, "run", run):
            raw = adapters.collect_repo(Path("/x/koku"), offline=True)
        self.assertEqual(raw["slug"], "Drokoz/koku")
        self.assertIsNone(raw["issues"])
        self.assertTrue(all(c[0] == "git" for c in run.llamadas), run.llamadas)

    def test_tracker_que_no_es_github_no_llama_a_gh(self):
        run = fake_run({
            "git -C /x/wl branch": (True, "main"),
            "git -C /x/wl remote": (True, "https://github.com/wl/groceries"),
        })
        with mock.patch.object(adapters, "run", run):
            raw = adapters.collect_repo(Path("/x/wl"), tracker="jira")
        self.assertEqual(raw["tracker"], "jira")
        self.assertIsNone(raw["issues"])
        self.assertTrue(all(c[0] == "git" for c in run.llamadas), run.llamadas)


class TestCollect(unittest.TestCase):
    def test_offline_no_toca_nada_externo(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("red en modo offline")), \
                mock.patch.object(adapters, "run", fake_run({})):
            crudo = adapters.collect(default_config().contexts, offline=True)
        self.assertTrue(crudo["offline"])
        self.assertIsNone(crudo["agents"])
        self.assertEqual(sorted(crudo), ["agents", "contexts", "offline"])
        self.assertEqual([c["name"] for c in crudo["contexts"]], ["personal"])
        self.assertIsNone(crudo["contexts"][0]["budget"]["credits"])

    def test_un_crudo_por_contexto_con_su_declaracion(self):
        cfg = support.config_dos_contextos()
        with mock.patch.object(adapters, "run", fake_run({})):
            crudo = adapters.collect(cfg.contexts, offline=True)
        personal, trabajo = crudo["contexts"]
        self.assertEqual(personal["name"], "personal")
        self.assertEqual((trabajo["tracker"], trabajo["autonomy"], trabajo["run"]),
                         ("jira", "manual", "ssh"))
        self.assertEqual(trabajo["vault"], "~/wl-devlead-vault")
        self.assertEqual(trabajo["budget"]["polarity"], "spent")

    def test_el_token_del_tracker_no_sale_en_lo_crudo(self):
        cfg = support.config_dos_contextos()
        with mock.patch.object(adapters, "run", fake_run({})):
            crudo = adapters.collect(cfg.contexts, offline=True)
        self.assertNotIn("secreto-de-jira", json.dumps(crudo))

    def test_los_credits_se_piden_una_sola_vez(self):
        cfg = support.config_dos_contextos()
        llamadas = []

        def fake_credits(offline=False):
            llamadas.append(offline)
            return {"total_credits": 25.0, "total_usage": 1.0}

        with mock.patch.object(adapters, "openrouter_credits", fake_credits), \
                mock.patch.object(adapters, "run", fake_run({})):
            crudo = adapters.collect(cfg.contexts)
        self.assertEqual(len(llamadas), 1)
        self.assertIsNotNone(crudo["contexts"][0]["budget"]["credits"])
        # el contexto que no usa OpenRouter no se lleva los créditos ajenos
        self.assertIsNone(crudo["contexts"][1]["budget"]["credits"])

    def test_sin_contextos_openrouter_no_se_pide_nada(self):
        cfg = support.config_dos_contextos()
        solo_trabajo = [c for c in cfg.contexts if c.name == "trabajo"]
        with mock.patch.object(adapters, "openrouter_credits",
                               mock.Mock(side_effect=AssertionError("pidió créditos"))), \
                mock.patch.object(adapters, "run", fake_run({})):
            adapters.collect(solo_trabajo)


if __name__ == "__main__":
    unittest.main()


class TestRunCapturaElError(unittest.TestCase):
    """`run` tiene que devolver el stderr cuando el comando falla.

    Git escribe sus errores en stderr. Devolver sólo stdout deja el motivo del
    fallo en la nada: el 2026-08-23 el dispatcher abandonó quince tickets y el
    log decía literalmente "fallo: ", sin una palabra de por qué.
    """

    def test_el_motivo_del_fallo_no_se_pierde(self):
        ok, out = adapters.run(
            [sys.executable, "-c",
             "import sys; sys.stderr.write('esto explica el fallo'); sys.exit(1)"])
        self.assertFalse(ok)
        self.assertIn("esto explica el fallo", out)

    def test_cuando_sale_bien_devuelve_el_stdout_limpio(self):
        ok, out = adapters.run([sys.executable, "-c", "print('la salida')"])
        self.assertTrue(ok)
        self.assertEqual(out, "la salida")
