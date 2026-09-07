"""El token de `gh` vencido en medio de la noche (#130).

El caso real: a las 2am el token expira y `gh` deja de contestar. Antes,
`harness status` decía "nada pendiente" y salía 0 — indistinguible de una
frontera vacía de verdad — y `run --loop` gastaba las 60 vueltas vacías
esperando algo que nunca iba a llegar.

El mundo se inyecta fallando a nivel de `adapters.run` (los `gh` devuelven
error de auth y los `git` locales contestan), no parcheando `collect`: la
distinción None (no se pudo leer) vs [] (leído, y no hay nada) tiene que
sobrevivir a la capa de adaptadores de verdad.
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

import support
from harness.dispatch import BucleSpec, EventLog, Pasada, bucle

HARNESS = support.ROOT / "bin" / "harness"

# Lo que `gh` escribe cuando el token no sirve: stderr, returncode != 0.
GH_AUTH_ERR = (False, "HTTP 401: Bad credentials (gh auth login)")

GQL_ISSUE_9 = json.dumps({"data": {"repository": {
    "cerrados": {"totalCount": 3},
    "issues": {"nodes": [
        {"number": 9, "title": "ticket 9",
         "labels": {"nodes": [{"name": "ready-for-agent"}]},
         "issueDependenciesSummary": {"blockedBy": 0}},
    ], "pageInfo": {"hasNextPage": False, "endCursor": None}},
}}})

GQL_VACIO = json.dumps({"data": {"repository": {
    "cerrados": {"totalCount": 0},
    "issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}})

CONFIG_PLANTILLA = """{{
  "default_context": "h",
  "contexts": {{"h": {{"tracker": {{"kind": "github"}},
    "repos": {{"root": "{root}", "paths": [{paths}]}},
    "autonomy": "frontier",
    "budget": {{"polarity": "remaining", "provider": "openrouter"}},
    "run": {{"kind": "local"}}}}}}
}}"""


def leer_log(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l]


def mundo(buenos=()):
    """Un `adapters.run` donde `gh` devuelve error de auth, salvo para los
    slugs de `buenos` — que trae un issue ready-for-agent #7 y un PR #31.

    El slug de cada repo sale de `git remote get-url` con su cwd, así que
    cada repo de la config tiene su `Drokoz/<nombre>` y se puede fallar
    por repo en vez de para todos (el token vencido falla para todos; un
    repo caído, sólo para él).
    """
    def run_cmd(args, cwd=None, timeout=30):
        args = list(args)
        # `collect` llama a git con `-C <path>` en vez de cwd: el slug por
        # repo sale de ahí, no del parámetro `cwd`.
        try:
            cwd = args[args.index("-C") + 1]
        except (ValueError, IndexError):
            pass
        if args[0] == "git":
            if "remote" in args:
                nombre = Path(cwd).name if cwd else "x"
                return (True, "git@github.com:Drokoz/{}.git".format(nombre))
            if "branch" in args:
                return (True, "main")
            if "status" in args:
                return (True, "")
            if "symbolic-ref" in args:
                return (True, "refs/remotes/origin/main")
            if "ls-tree" in args:
                return (True, "scripts/gate.sh\ndocs/agents/issue-tracker.md\n"
                              "CONTEXT.md\n")
            return (True, "")
        if args[0] == "gh":
            # El slug va en `-R owner/repo` (REST) o en `-F name=repo`
            # (GraphQL): resolverlo y no hacer match por subcadena.
            slug = None
            for i, a in enumerate(args):
                if a == "-R" and i + 1 < len(args):
                    slug = args[i + 1]
                elif isinstance(a, str) and a.startswith("name="):
                    slug = "Drokoz/" + a.split("=", 1)[1]
            if slug in buenos:
                if "api" in args and "graphql" in args:
                    return (True, GQL_ISSUE_9)
                if "pr" in args and "list" in args:
                    return (True, json.dumps([
                        {"number": 31, "headRefName": "ticket/9"}]))
                if "pr" in args and "view" in args:
                    return (True, '{"state":"OPEN"}')
                return (True, json.dumps(
                    [{"number": 9, "title": "ticket 9", "labels": [],
                      "body": ""}]))
            return GH_AUTH_ERR
        return (True, "")
    return run_cmd


class BaseCli(unittest.TestCase):
    """El CLI cargado de su archivo, con la config y el estado en un tmp."""

    REPOS = ("bueno", "malo")
    BUENOS_GH = ()  # qué slugs de gh contestan; por defecto ninguno (vencido)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        for nombre in self.REPOS:
            (self.d / "repos" / nombre).mkdir(parents=True)
        paths = ", ".join('"repos/{}"'.format(n) for n in self.REPOS)
        self.d.joinpath("config.json").write_text(CONFIG_PLANTILLA.format(
            root=str(self.d), paths=paths))
        self.log_path = self.d / "events.jsonl"
        loader = SourceFileLoader("harness_cli_130", str(HARNESS))
        spec = importlib.util.spec_from_loader("harness_cli_130", loader)
        self.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(self.cli)

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self, *extra):
        return self.cli.build_parser().parse_args(
            list(extra) or ["status"])

    def _patchear(self, run_cmd=None):
        """Los parches comunes: entorno aislado, mundo con gh fallando,
        y sin tocar la red de verdad (créditos) ni el disco real (cuota).
        `run_cmd` lo sobrepone si la prueba quiere un mundo a medida."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, {
            "HARNESS_CONFIG_DIR": str(self.d),
            "XDG_STATE_HOME": str(self.d / "state"),
            "HERDR_ENV": "1",
        }))
        stack.enter_context(mock.patch.object(self.cli, "OFFLINE", False))
        stack.enter_context(mock.patch.object(
            self.cli.adapters, "run", run_cmd or mundo(self.BUENOS_GH)))
        stack.enter_context(mock.patch.object(
            self.cli.adapters, "openrouter_credits", return_value=None))
        stack.enter_context(mock.patch.object(self.cli, "leer_fuentes",
                                              return_value=([], [])))
        return stack


class StatusConTrackerIlegible(BaseCli):
    """`harness status`: lo dice arriba, donde se ve, y sale distinto de 0."""

    def test_todo_ilegible_dice_y_sale_1(self):
        with self._patchear(), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = self.cli.main(["status"])
        texto = out.getvalue()
        self.assertEqual(code, 1, "un tracker ilegible no sale 0")
        # Arriba, donde se ve, y nombrando a cada repo que falló.
        self.assertIn("tracker ilegible: bueno", texto)
        self.assertIn("tracker ilegible: malo", texto)
        # No se dibuja como frontera vacía.
        self.assertNotIn("nada pendiente", texto)
        self.assertIn("no se pudo leer el tracker", texto)

    def test_el_repo_sano_no_se_tapa(self):
        self.BUENOS_GH = ("Drokoz/bueno",)
        with self._patchear(), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = self.cli.main(["status"])
        texto = out.getvalue()
        self.assertEqual(code, 1)
        # El sano con trabajo se muestra; el que falló, nombrado.
        self.assertIn("#9 ticket 9", texto)
        self.assertIn("tracker ilegible: malo", texto)
        self.assertNotIn("tracker ilegible: bueno", texto)
        self.assertNotIn("nada pendiente", texto)

    def test_frontera_vacia_de_verdad_sigue_saliendo_0(self):
        # gh responde y de verdad no hay nada: eso sigue siendo 0 y
        # "nada pendiente" — el caso nuevo no le roba el caso viejo.
        def run_cmd(args, cwd=None, timeout=30):
            if args[0] == "gh":
                if "api" in args and "graphql" in args:
                    return (True, GQL_VACIO)
                if "pr" in args and "list" in args:
                    return (True, "[]")
            return mundo(("Drokoz/bueno", "Drokoz/malo"))(
                args, cwd=cwd, timeout=timeout)
        with self._patchear(run_cmd=run_cmd), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = self.cli.main(["status"])
        self.assertEqual(code, 0)
        self.assertIn("nada pendiente", out.getvalue())


class RunConTrackerIlegible(BaseCli):
    """`harness run`: no despacha una tanda vacía: corta con el motivo."""

    def test_tanda_vacia_sobre_tracker_ilegible(self):
        args = self._args("run", "--context", "h", "--log",
                          str(self.log_path))
        with self._patchear(), \
             mock.patch.object(self.cli, "_creditos_usado",
                               return_value=None), \
             mock.patch.object(self.cli, "_creditos_restantes",
                               return_value=99.0), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None,
                                             lambda *a: None)), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = self.cli.run_main(args,
                                     self.cli.load_config().select("h"))
        self.assertEqual(code, 1, "corta, no dice 'nada en la frontera'")
        self.assertNotIn("nada en la frontera", out.getvalue())
        self.assertIn("no se pudo leer el tracker", err.getvalue())
        self.assertIn("bueno", err.getvalue())
        self.assertIn("malo", err.getvalue())
        eventos = leer_log(self.log_path)
        # El motivo llega al log de eventos, para que se vea a la mañana.
        self.assertTrue(any("tracker ilegible: bueno" in e.get("cuerpo", "")
                            for e in eventos), eventos)
        self.assertTrue(any("tracker ilegible: malo" in e.get("cuerpo", "")
                            for e in eventos), eventos)
        # No se despachó nada: ni worktree, ni pane, ni agente.
        self.assertEqual([e for e in eventos
                          if e["tipo"] in ("worktree", "pane", "agente",
                                            "prompt", "gate")], [])


class RunNoTapaALosDemas(BaseCli):
    """Un repo que falla no tapa a los demás: el sano sí se despacha."""

    BUENOS_GH = ("Drokoz/bueno",)

    def test_el_sano_se_despacha_y_se_dice_cual_fallo(self):
        # Un ticket con costo medido en el historial: sin promedio no se
        # dimensiona la tanda y nada se arranca (#68), independiente del bug.
        self.log_path.write_text(json.dumps({
            "timestamp": "2026-09-01T03:00:00Z",
            "contexto": "h", "origen": "harness", "run_id": "r0",
            "ticket": "bueno#5", "attempt": 1, "tipo": "costo",
            "ref": "ticket/5", "cuerpo": "$0.2000 (sesion de pi)",
            "clase": None}) + "\n")
        from test_dispatch import Mundo
        despachador = Mundo()  # herdr/git/gate de la corrida del repo sano
        git_collect = mundo(self.BUENOS_GH)
        # El collect no ve PRs abiertos (el issue es libre); cuando el
        # dispatcher lo pide (al verificar la corrida), el agente ya lo creó.
        n_pr_list = {"open": 0}

        def run_cmd(args, cwd=None, timeout=30):
            a = args[0]
            if a == "gh":
                line = " ".join(str(x) for x in args)
                if "Drokoz/malo" in line or "name=malo" in line:
                    return GH_AUTH_ERR
                if "api" in args and "graphql" in args:
                    return (True, GQL_ISSUE_9)
                if "pr" in args and "list" in args:
                    if "merged" in args:  # cosechar: nada merged
                        return (True, "[]")
                    n_pr_list["open"] += 1
                    if n_pr_list["open"] == 1:  # el collect
                        return (True, "[]")
                    return (True, json.dumps([
                        {"number": 31, "headRefName": "ticket/9"}]))
                if "pr" in args and "view" in args:
                    if "files" in args:
                        return (True, json.dumps(
                            [{"path": p} for p in despachador.pr_files]))
                    return (True, json.dumps({
                        "headRefOid": despachador.pr_head_oid}))
                return (True, "")
            if a == "herdr" or a == "./scripts/gate.sh" or (
                    a == "git" and ("rev-list" in args or "rev-parse" in args)):
                return despachador.cmd(args, cwd=cwd, timeout=timeout)
            return git_collect(args, cwd=cwd, timeout=timeout)

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
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = self.cli.run_main(args,
                                     self.cli.load_config().select("h"))
        texto = out.getvalue()
        self.assertEqual(code, 0)
        # Se dice cuál falló...
        self.assertIn("no se pudo leer el tracker", texto)
        self.assertIn("malo", texto)
        # ...y el sano se despachó de todos modos.
        self.assertIn("#9/bueno", texto)
        eventos = leer_log(self.log_path)
        self.assertTrue(any(e["tipo"] == "pr" and "PR #31 abierto" in e["cuerpo"]
                            for e in eventos), eventos)
        self.assertTrue(any("tracker ilegible: malo" in e.get("cuerpo", "")
                            for e in eventos), eventos)
        # El repo falló no entró a la cola: nada en el log apunta a "malo".
        self.assertEqual([e for e in eventos
                          if "malo#" in str(e.get("ticket", ""))], [])


class LoopConTrackerIlegible(BaseCli):
    """`run --loop`: no gasta vueltas vacías: corta en la primera pasada."""

    def test_corta_en_la_primera_pasada(self):
        args = self._args("run", "--context", "h", "--loop",
                          "--log", str(self.log_path),
                          "--piso", "3", "--max-pasadas", "9",
                          "--sin-proponer")
        with self._patchear(), \
             mock.patch.object(self.cli, "_creditos_usado",
                               return_value=None), \
             mock.patch.object(self.cli, "_creditos_restantes",
                               return_value=99.0), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None,
                                             lambda *a: None)), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("time.sleep"):
            code = self.cli.run_main(args,
                                     self.cli.load_config().select("h"))
        texto = out.getvalue()
        eventos = leer_log(self.log_path)
        self.assertEqual(code, 1)
        # Una sola pasada, no las 60 vueltas vacías de la noche.
        self.assertEqual([e for e in eventos if e["tipo"] == "pasada"], [])
        cortes = [e for e in eventos
                  if e["tipo"] == "bucle" and "corte" in e["cuerpo"]]
        self.assertEqual(len(cortes), 1)
        # El motivo queda en el log de eventos, con el repo nombrado.
        self.assertIn("tracker ilegible", cortes[0]["cuerpo"])
        self.assertIn("bueno", cortes[0]["cuerpo"])
        self.assertIn("malo", cortes[0]["cuerpo"])
        # No propone contra un tracker que no se puede leer, no espera más.
        self.assertEqual([e for e in eventos if e["tipo"] == "proponer"], [])
        self.assertIn("corte: tracker ilegible", texto)


class ElBucleCortaConPasadaCorte(unittest.TestCase):
    """El contrato del bucle: una pasada con `corte` corta en la primera."""

    def test_corte_de_la_pasada_gana_en_la_primera(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "events.jsonl"
            log = EventLog(log_path, "harness")
            n = {"p": 0}

            def pasada():
                n["p"] += 1
                return Pasada(False, "nada en la frontera", log=log,
                              corte="tracker ilegible: koku")

            corte = bucle(pasada, BucleSpec(), log,
                          dormir=lambda s: None)
            self.assertEqual(n["p"], 1, "corta en la primera pasada")
            self.assertTrue(corte.startswith("tracker ilegible"))
            eventos = [json.loads(l) for l in
                       Path(log_path).read_text().splitlines() if l]
            self.assertTrue(any(e["tipo"] == "bucle"
                                and "corte: tracker ilegible" in e["cuerpo"]
                                for e in eventos), eventos)


if __name__ == "__main__":
    unittest.main()
