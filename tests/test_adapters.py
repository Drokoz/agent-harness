"""Tests de la capa de adaptadores: que traiga datos crudos y que no decida nada.

Ningún test acá sale a la red ni llama a `gh`/`herdr` de verdad: `run` se
reemplaza por una función que devuelve lo que devolvería el comando.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

from harness import adapters


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
            issues = adapters.gh_graphql_issues("Drokoz/agent-harness")
        por_num = {i["number"]: i for i in issues}
        self.assertEqual(sorted(por_num), [1, 4, 5, 6, 7, 8, 9, 12, 15])
        self.assertEqual(por_num[8]["blocked_by"], 2)   # dos bloqueantes abiertos
        self.assertEqual(por_num[12]["blocked_by"], 0)  # bloqueante cerrado: no cuenta
        self.assertEqual(por_num[12]["labels"], [{"name": "ready-for-agent"}])

    def test_gh_caido_es_none(self):
        with mock.patch.object(adapters, "run", fake_run({})):
            self.assertIsNone(adapters.gh_graphql_issues("o/r"))

    def test_respuesta_con_errors_es_none(self):
        malo = json.dumps({"data": {"repository": None}, "errors": [{"message": "boom"}]})
        with mock.patch.object(adapters, "run", fake_run({"gh api graphql": (True, malo)})):
            self.assertIsNone(adapters.gh_graphql_issues("o/r"))

    def test_json_roto_es_none(self):
        with mock.patch.object(adapters, "run",
                               fake_run({"gh api graphql": (True, "no soy json")})):
            self.assertIsNone(adapters.gh_graphql_issues("o/r"))

    def test_sin_slug_es_none(self):
        with mock.patch.object(adapters, "run",
                               mock.Mock(side_effect=AssertionError("llamó a gh"))):
            self.assertIsNone(adapters.gh_graphql_issues("sin-barra"))

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
            issues = adapters.gh_graphql_issues("o/r")
        self.assertEqual([i["number"] for i in issues], [1, 2])
        self.assertEqual(issues[1]["blocked_by"], 3)
        self.assertEqual(len(llamadas), 2)
        self.assertNotIn("after=", llamadas[0])
        self.assertIn("after=CUR", llamadas[1])


class TestRepos(unittest.TestCase):
    def test_lee_repos_conf_ignorando_comentarios_y_repos_que_no_estan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "existe").mkdir()
            (root / "repos.conf").write_text(
                "# un comentario\n"
                "\n"
                f"{root / 'existe'}\n"
                f"{root / 'no-existe'}\n"
                f"{root / 'existe'}  # con comentario al final\n"
            )
            repos = adapters.read_repos(root)
        self.assertEqual([p.name for p in repos], ["existe", "existe"])

    def test_repos_conf_del_repo_apunta_a_directorios(self):
        for p in adapters.read_repos(support.ROOT):
            self.assertTrue(p.is_dir())

    def test_sin_repos_conf(self):
        self.assertEqual(adapters.read_repos(Path("/no/existe")), [])

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

    def test_collect_offline_no_toca_nada_externo(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("red en modo offline")), \
                mock.patch.object(adapters, "run", fake_run({})):
            crudo = adapters.collect(support.ROOT, offline=True)
        self.assertTrue(crudo["offline"])
        self.assertIsNone(crudo["credits"])
        self.assertIsNone(crudo["agents"])
        self.assertEqual(sorted(crudo), ["agents", "credits", "offline", "repos"])


if __name__ == "__main__":
    unittest.main()
