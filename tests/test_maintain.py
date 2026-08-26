"""El mantenedor de conflictos (`harness maintain`, #56) contra un mundo
scripteado.

El segundo tipo de job: sale de los PRs abiertos (no de la frontera de
issues), lleva el prompt de resolver (no de implementar), termina cuando
el PR queda mergeable con el gate verde sobre la rama resuelta, y la regla
dura: ninguna resolución que descarta trabajo se acepta. La clave de log
del PR ("repo#pr<N>") es distinguible de la del ticket ("repo#<N>") para
que los abandonos no se escalen entre las dos colas.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

import support
from harness import dispatch, state
from harness.dispatch import (DispatchSpec, EventLog, ESCALERA, peldano_de)
from harness.maintain import (Baseline, CONFLICTING, MantenJob, Mantener,
                              es_test, mergeable_de, prs_no_mergeables,
                              preserva, prompt_de_resolucion)

ROOT = support.ROOT
HARNESS = ROOT / "bin" / "harness"

PANE_ARRANCO = "⠏ Working...\n~/repo (fix/colision)\n↑12k ↓2k R4k CH0.1% $0.056 1.2%/262k ...\n"


# --------------------------------------------------------------------- mundo
class MundoMantener:
    """El mundo del mantenedor: git, herdr y gh scripteados.

    `resuelto` marca que el agente trabajó: hasta entonces el PR es
    CONFLICTING y sus archivos son la baseline; después, lo que le
    digamos que dejó (`mergeable_despues`, `resuelto_files`)."""

    def __init__(self, prs=None, baseline_files=None, resuelto_files=None,
                 mergeable_despues="MERGEABLE", gate=(True, "VERDE"),
                 head_oid="a" * 40, pr_head_oid=None, pr_del_ticket=None):
        self.prs = prs if prs is not None else [
            {"number": 31, "headRefName": "fix/colision", "title": "p"}]
        self.baseline_files = baseline_files if baseline_files is not None else [
            {"path": "harness/x.py", "changeType": "MODIFIED"},
            {"path": "tests/test_x.py", "changeType": "ADDED"}]
        self.resuelto_files = (list(self.baseline_files) if resuelto_files is None
                               else resuelto_files)
        self.mergeable_despues = mergeable_despues
        self.gate = gate
        self.head_oid = head_oid
        self.pr_head_oid = pr_head_oid if pr_head_oid is not None else head_oid
        # El PR que el agente del TICKET deja abierto (flujo de `harness run`).
        self.pr_del_ticket = pr_del_ticket
        self.resuelto = False
        self.respuestas = []
        self.llamadas = []

    def responder(self, pruebo, ok_out):
        self.respuestas.append((pruebo, ok_out))
        return self

    def cmd(self, args, cwd=None, timeout=30):
        args = list(args)
        self.llamadas.append((tuple(args), cwd, timeout))
        for pruebo, ok_out in self.respuestas:
            if pruebo(args):
                return ok_out(args) if callable(ok_out) else ok_out
        return self._default(args)

    def _default(self, args):
        a = args[0]
        if a == "git":
            if "rev-list" in args:
                return (True, "1\n")
            if "rev-parse" in args:
                if args[-1] == "HEAD":
                    return (True, self.head_oid + "\n")
                if args[-1].startswith("refs/heads/"):
                    return (True, self.head_oid + "\n")  # la rama existe en lo local
                return (False, "")
            return (True, "")
        if a == "herdr":
            sub = args[1:3]
            if sub[:2] == ["pane", "split"]:
                return (True, '{"result":{"pane":{"pane_id":"w9:p2"}}}')
            if sub[:2] == ["pane", "read"]:
                return (True, PANE_ARRANCO)
            if sub[:2] == ["agent", "prompt"]:
                self.resuelto = True  # el prompt llegó: el agente trabajó
                return (True, '{"result":{"agent":{"agent_status":"working"}}}')
            if sub[:2] == ["agent", "wait"]:
                return (True, '{"result":{"agent":{"agent_status":"idle"}}}')
            if sub[:2] == ["agent", "get"]:
                return (True, '{"result":{"agent":{"agent_status":"working"}}}')
            return (True, "")
        if a == "gh":
            if "pr" in args and "list" in args:
                if "--state" in args and "merged" in args:
                    return (True, "[]")
                if self.pr_del_ticket:
                    return (True, json.dumps(self.pr_del_ticket))
                return (True, "[]")
            if "pr" in args and "view" in args:
                if "mergeable" in args:
                    v = (self.mergeable_despues if self.resuelto else CONFLICTING)
                    return (True, json.dumps({"mergeable": v}))
                if "files" in args:
                    files = (self.baseline_files if not self.resuelto
                             else self.resuelto_files)
                    return (True, json.dumps(files))
                if "headRefOid" in args:
                    return (True, json.dumps({"headRefOid": self.pr_head_oid}))
                return (True, "{}")
            if "pr" in args and "comment" in args:
                return (True, "")
            return (True, "")
        if a == "./scripts/gate.sh":
            return self.gate
        return (True, "")

    def llamo(self, *parte):
        return [c for c in self.llamadas if all(x in c[0] for x in parte)]


def log_en(tmp, nombre="ev.jsonl"):
    return EventLog(Path(tmp) / nombre, "harness",
                    reloj=lambda: "2026-08-26T12:00:00Z")


def job_mantener(**kw):
    base = dict(repo="koku", repo_path="/repos/koku", slug="Drokoz/koku",
                pr=31, branch="fix/colision", base="main",
                kind="pi", model="qwen/qwen3.8-27b")
    base.update(kw)
    return MantenJob(**base)


def mantener(mundo, job=None, **kw):
    """Corre `Mantener.dispatch` con un solo job; devuelve (jobs, lineas)."""
    with tempfile.TemporaryDirectory() as tmp:
        log = log_en(tmp)
        m = Mantener(DispatchSpec(contexto="harness", **kw), log, mundo.cmd,
                     credits=lambda: None, dormir=lambda s: None)
        res = m.dispatch([job or job_mantener()])
        lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
    return res, lineas


# ------------------------------------------------------------------- puros
class TestMergeable(unittest.TestCase):
    def test_mergeable_de_lee_el_veredicto(self):
        out = (True, json.dumps({"mergeable": "CONFLICTING"}))
        self.assertEqual(mergeable_de("o/r", 5, lambda args: out), "CONFLICTING")

    def test_mergeable_de_sin_gh_da_none(self):
        self.assertIsNone(mergeable_de("o/r", 5, lambda args: (False, "")))

    def test_mergeable_de_json_roto_da_none(self):
        self.assertIsNone(mergeable_de("o/r", 5, lambda args: (True, "esto no es json")))


class TestPrsNoMergeables(unittest.TestCase):
    PRS = [{"number": 31, "headRefName": "fix/a", "title": "a"},
           {"number": 32, "headRefName": "fix/b", "title": "b"},
           {"number": 33, "headRefName": "fix/c", "title": "c"}]

    def test_solo_los_conflicting_y_por_pr_view(self):
        """La fuente es `gh pr view --json mergeable` por PR (#56):
        MERGEABLE y UNKNOWN no entran a la cola (UNKNOWN = GitHub todavía
        lo calcula; la próxima pasada lo encuentra)."""
        veredictos = {"31": "CONFLICTING", "32": "MERGEABLE", "33": "UNKNOWN"}

        def run(args):
            if "list" in args:
                return (True, json.dumps(self.PRS))
            if "view" in args and "mergeable" in args:
                num = args[args.index("view") + 1]
                return (True, json.dumps({"mergeable": veredictos[num]}))
            return (True, "")

        malos = prs_no_mergeables("o/r", run)
        self.assertEqual([p["number"] for p in malos], [31])

    def test_sin_gh_no_hay_prs(self):
        self.assertEqual(prs_no_mergeables("o/r", lambda args: (False, "")), [])

    def test_list_json_roto_no_hay_prs(self):
        self.assertEqual(prs_no_mergeables("o/r", lambda args: (True, "roto")), [])

    def test_pr_snumero_no_entra(self):
        def run(args):
            if "list" in args:
                return (True, json.dumps([{"headRefName": "x"},
                                          {"number": 9, "headRefName": "y"}]))
            return (True, json.dumps({"mergeable": "CONFLICTING"}))
        malos = prs_no_mergeables("o/r", run)
        self.assertEqual([p["number"] for p in malos], [9])


class TestEsTest(unittest.TestCase):
    def test_rutas_de_tests(self):
        for r in ("tests/test_x.py", "test/foo.py", "backend/tests/x_test.go",
                  "test_x.py", "src/x_test.go", "js/app.test.ts",
                  "js/app.spec.js"):
            self.assertTrue(es_test(r), r)

    def test_rutas_que_no_son_tests(self):
        for r in ("harness/x.py", "src/app.ts", "docs/a.md", "test",
                  "x.testing.js", "", "  "):
            self.assertFalse(es_test(r), r)


class TestBaseline(unittest.TestCase):
    def test_de_pr_separa_archivos_y_tests(self):
        b = Baseline.de_pr([
            {"path": "harness/x.py", "changeType": "MODIFIED"},
            {"path": "tests/test_x.py", "changeType": "ADDED"},
            {"path": "src/main.ts", "changeType": "MODIFIED"},
            {"changeType": "ADDED"}])  # sin path: no cuenta
        self.assertEqual(b.archivos, frozenset(
            {"harness/x.py", "tests/test_x.py", "src/main.ts"}))
        self.assertEqual(b.tests, frozenset({"tests/test_x.py"}))


class TestPreserva(unittest.TestCase):
    """La parte difícil (#56): qué cuenta como resuelto. Una resolución
    `--ours`/`--theirs` pasa el gate y tira trabajo en silencio: verde
    no alcanza."""

    def _b(self):
        return Baseline.de_pr([
            {"path": "harness/x.py"}, {"path": "tests/test_x.py"}])

    def test_mismo_set_pasa(self):
        ok, _ = preserva(self._b(), ["tests/test_x.py", "harness/x.py"])
        self.assertTrue(ok)

    def test_archivos_nuevos_pasan(self):
        """Resolver un conflicto puede tocar MÁS archivos, no menos."""
        ok, _ = preserva(self._b(), ["harness/x.py", "tests/test_x.py",
                                     "harness/nuevo.py"])
        self.assertTrue(ok)

    def test_archivo_perdido_no_pasa(self):
        ok, motivo = preserva(self._b(), ["tests/test_x.py"])
        self.assertFalse(ok)
        self.assertIn("harness/x.py", motivo)

    def test_test_perdido_no_pasa_y_el_motivo_lo_dice(self):
        ok, motivo = preserva(self._b(), ["harness/x.py"])
        self.assertFalse(ok)
        self.assertIn("test", motivo.lower())
        self.assertIn("tests/test_x.py", motivo)

    def test_sin_baseline_no_se_puede_verificar(self):
        ok, motivo = preserva(None, ["harness/x.py"])
        self.assertFalse(ok)
        self.assertTrue(motivo)


class TestPromptDeResolucion(unittest.TestCase):
    def test_es_una_linea(self):
        p = prompt_de_resolucion(31, "fix/colision", "main")
        self.assertNotIn("\n", p)

    def test_es_resolver_no_implementar(self):
        p = prompt_de_resolucion(31, "fix/colision", "main").lower()
        self.assertIn("resolve", p)
        self.assertIn("not an implement job", p)
        self.assertIn("pr #31", p)
        self.assertIn("fix/colision", p)
        self.assertIn("main", p)

    def test_lleva_las_reglas_duraderas(self):
        p = prompt_de_resolucion(31, "fix/colision", "main").lower()
        self.assertIn("never merge", p)
        self.assertIn("force-push", p)
        self.assertIn("./scripts/gate.sh", p)
        self.assertIn("push", p)

    def test_prohíbe_descartar_trabajo(self):
        p = prompt_de_resolucion(31, "fix/colision", "main").lower()
        self.assertIn("discard", p)
        self.assertIn("rejected", p)


# -------------------------------------------------------------------- state
class TestClavesDeLog(unittest.TestCase):
    """La clave del PR es distinguible de la del ticket (#56, y la misma
    lección de #72: dos colas que comparten número no se contaminan)."""

    def test_clave_del_pr_no_es_clave_de_ticket(self):
        self.assertEqual(state.clave_mantenimiento("koku", 31), "koku#pr31")
        self.assertNotEqual(state.clave_mantenimiento("koku", 31),
                            "koku#31")

    def test_abandono_de_pr_no_escalal_al_ticket(self):
        evs = [{"tipo": "abandono", "ticket": "koku#pr31", "run_id": "r1",
                "clase": "modelo"}]
        self.assertEqual(state.intentos_que_escalan(evs, "koku", 31), 0)
        self.assertEqual(state.intentos_pr_que_escalan(evs, "koku", 31), 1)

    def test_abandono_de_ticket_no_escalal_al_pr(self):
        evs = [{"tipo": "abandono", "ticket": "koku#31", "run_id": "r1",
                "clase": "modelo"}]
        self.assertEqual(state.intentos_pr_que_escalan(evs, "koku", 31), 0)
        self.assertEqual(state.intentos_que_escalan(evs, "koku", 31), 1)

    def test_infra_no_consume_peldano_en_el_pr(self):
        evs = [{"tipo": "abandono", "ticket": "koku#pr31", "run_id": "r1",
                "clase": "infra"},
               {"tipo": "abandono", "ticket": "koku#pr31", "run_id": "r2",
                "clase": "modelo"}]
        self.assertEqual(state.intentos_pr_que_escalan(evs, "koku", 31), 1)

    def test_intentos_pr_cuenta_corridas(self):
        evs = [{"tipo": "mantenimiento", "ticket": "koku#pr31", "run_id": "r1"},
               {"tipo": "abandono", "ticket": "koku#pr31", "run_id": "r1"},
               {"tipo": "mantenimiento", "ticket": "koku#pr31", "run_id": "r2"}]
        self.assertEqual(state.intentos_pr(evs, "koku", 31), 2)

    def test_motivos_de_abandono_pr(self):
        evs = [{"tipo": "abandono", "ticket": "koku#pr31", "run_id": "r1",
                "cuerpo": "gate rojo"},
               {"tipo": "abandono", "ticket": "koku#31", "run_id": "r1",
                "cuerpo": "otro"}]
        self.assertEqual(state.motivos_de_abandono_pr(evs, "koku", 31),
                         ["gate rojo"])


class TestEscalera(unittest.TestCase):
    """`runner:cheap` con escalada (#56): arranca en el peldaño barato
    (pi/Qwen) y escala como cualquier ticket, leyendo el log."""

    def test_el_pr_sin_historial_arranca_en_el_peldano_barato(self):
        self.assertEqual(peldano_de(state.intentos_pr_que_escalan([], "koku", 31)),
                         ESCALERA[0])
        self.assertEqual(peldano_de(0)["kind"], "pi")

    def test_dos_abandonos_escalados_llevan_al_peldano_3(self):
        evs = [{"tipo": "abandono", "ticket": "koku#pr31", "run_id": "r1",
                "clase": "modelo"},
               {"tipo": "abandono", "ticket": "koku#pr31", "run_id": "r2",
                "clase": "modelo"}]
        self.assertEqual(peldano_de(state.intentos_pr_que_escalan(evs, "koku", 31)),
                         ESCALERA[2])
        self.assertEqual(ESCALERA[2]["kind"], "claude")

    def test_cuatro_abandonos_agotan_la_escalera(self):
        evs = [{"tipo": "abandono", "ticket": "koku#pr31", "run_id": "r{}".format(i),
                "clase": "modelo"} for i in range(1, 5)]
        self.assertIsNone(
            peldano_de(state.intentos_pr_que_escalan(evs, "koku", 31)))


# ------------------------------------------------------------ ciclo de vida
class TestMantenerMundo(unittest.TestCase):
    def test_camino_happy_resuelve_y_deja_el_pr_mergeable(self):
        mundo = MundoMantener()
        res, lineas = mantener(mundo)
        (job,) = res
        self.assertEqual(job.estado, "resuelto")
        self.assertEqual(job.clase_abandono, "")
        # El log es un tipo propio, con la clave del PR, no del ticket.
        self.assertIn("mantenimiento", [l["tipo"] for l in lineas])
        self.assertTrue(all(l["ticket"] in (None, "koku#pr31") for l in lineas))
        self.assertFalse(any(l["tipo"] == "abandono" for l in lineas))
        (fin,) = [l for l in lineas if l["tipo"] == "mantenimiento"
                  and l["cuerpo"].startswith("PR #31 mergeable")]
        self.assertEqual(fin["ref"], "pr/31")
        # El gate corrió en el worktree del PR, por el dispatcher.
        self.assertTrue(any(c[0][0] == "./scripts/gate.sh" and
                            c[1] and "koku-ticket-31" in c[1]
                            for c in mundo.llamadas))
        # El prompt fue el de resolver, no el de implementar.
        (prompt,) = mundo.llamo("herdr", "agent", "prompt")
        texto = prompt[0][4]
        self.assertIn("resolve", texto.lower())
        self.assertIn("fix/colision", texto)
        # El worktree usó la rama que ya existe del PR, sin -b.
        (add,) = [c for c in mundo.llamo("git", "worktree", "add")
                  if any("koku-ticket-31" in a for a in c[0])]
        self.assertNotIn("-b", add[0])
        self.assertIn("fix/colision", add[0])
        # Limpieza: pane cerrado, worktree fuera, rama queda.
        self.assertTrue(mundo.llamo("herdr", "pane", "close"))
        self.assertTrue(mundo.llamo("git", "worktree", "remove"))
        # El dispatcher jamás mergea ni force-pushea.
        self.assertFalse(any("merge" == a for c in mundo.llamadas for a in c[0]))
        self.assertFalse(any("push" == a for c in mundo.llamadas for a in c[0]))

    def test_el_dispatcher_confirma_mergeable_por_api(self):
        """El verde del gate no alcanza: el PR tiene que quedar mergeable
        según GitHub, verificado con `gh pr view --json mergeable`."""
        mundo = MundoMantener(mergeable_despues="CONFLICTING")
        res, _ = mantener(mundo)
        (job,) = res
        self.assertEqual(job.estado, "abandonado")
        self.assertIn("mergeable", job.motivo)
        self.assertEqual(job.clase_abandono, "modelo")

    def test_resolucion_que_tira_archivo_no_se_acepta(self):
        """La resolución `--theirs`: pasa el gate y descarta el trabajo
        del PR en silencio. Se descarta la resolución y el PR queda
        anotado para humano."""
        mundo = MundoMantener(resuelto_files=[{"path": "tests/test_x.py"}])
        res, lineas = mantener(mundo)
        (job,) = res
        self.assertEqual(job.estado, "abandonado")
        self.assertIn("harness/x.py", job.motivo)
        self.assertEqual(job.clase_abandono, "modelo")
        # Anotado para humano: comentario en el PR, y la línea lo dice.
        (comentario,) = mundo.llamo("gh", "pr", "comment")
        self.assertIn("humano", " ".join(a for a in comentario[0] if isinstance(a, str)).lower())
        self.assertTrue(any(l["tipo"] == "mantenimiento" and "humano" in l["cuerpo"]
                            for l in lineas))

    def test_resolucion_que_tira_el_test_no_se_acepta(self):
        mundo = MundoMantener(resuelto_files=[{"path": "harness/x.py"}])
        res, _ = mantener(mundo)
        (job,) = res
        self.assertEqual(job.estado, "abandonado")
        self.assertIn("tests/test_x.py", job.motivo)

    def test_head_del_pr_no_coincide(self):
        """El gate midió el HEAD del worktree; si el PR apunta a otro
        commit, el verde no aplica a lo que va a mergear."""
        mundo = MundoMantener(pr_head_oid="b" * 40)
        res, _ = mantener(mundo)
        (job,) = res
        self.assertEqual(job.estado, "abandonado")
        self.assertIn("head", job.motivo.lower())

    def test_gate_rojo_no_resuelve_nada(self):
        mundo = MundoMantener(gate=(False, "ROJO: unittest"))
        res, _ = mantener(mundo)
        (job,) = res
        self.assertEqual(job.estado, "abandonado")
        self.assertIn("gate rojo", job.motivo)

    def test_sin_baseline_no_se_despacha(self):
        """No poder leer el diff del PR antes de resolver es una falla
        del mundo (infra, no consume peldaño), y no se despacha a ciegas:
        sin baseline no hay contra qué verificar la resolución."""
        mundo = MundoMantener()
        mundo.responder(
            lambda a: "gh" in a and "files" in a and "view" in a,
            (False, "gh se cayó"))
        res, lineas = mantener(mundo)
        (job,) = res
        self.assertEqual(job.estado, "abandonado")
        self.assertEqual(job.clase_abandono, "infra")
        self.assertEqual(job.costo, 0.0)
        self.assertFalse(mundo.llamo("herdr", "agent", "start"))

    def test_claves_de_log_y_campo_ticket(self):
        _, lineas = mantener(MundoMantener())
        self.assertTrue(any(l["tipo"] == "mantenimiento" and l["ticket"] == "koku#pr31"
                            for l in lineas))


class TestParkear(unittest.TestCase):
    def test_escalera_agotada_anota_el_pr_para_humano(self):
        mundo = MundoMantener()
        with tempfile.TemporaryDirectory() as tmp:
            log = log_en(tmp)
            m = Mantener(DispatchSpec(contexto="harness"), log, mundo.cmd,
                         credits=lambda: None, dormir=lambda s: None)
            m.parkear(job_mantener(), "pr/31", ["gate rojo", "sin PR"])
            lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
        (comentario,) = mundo.llamo("gh", "pr", "comment")
        texto = " ".join(a for a in comentario[0] if isinstance(a, str))
        self.assertIn("humano", texto.lower())
        self.assertIn("gate rojo", texto)
        # No se despachó nada: ni worktree ni agente.
        self.assertFalse(mundo.llamo("git", "worktree", "add"))
        self.assertFalse(mundo.llamo("herdr", "agent", "start"))
        self.assertTrue(any(l["tipo"] == "mantenimiento" and "agotada" in l["cuerpo"]
                            for l in lineas))


# --------------------------------------------------------------------- CLI
def load_harness():
    loader = SourceFileLoader("harness_cli_mant", str(HARNESS))
    spec = importlib.util.spec_from_loader("harness_cli_mant", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCliMaintain(unittest.TestCase):
    """El cableado del CLI, con el mundo de mentira inyectado en los
    adaptadores: el mantenimiento corre antes que los tickets nuevos."""

    CONFIG = json.dumps({
        "default_context": "harness",
        "contexts": {
            "harness": {"tracker": {"kind": "github"},
                        "repos": {"root": "/repos", "paths": ["koku"]},
                        "autonomy": "frontier",
                        "budget": {"polarity": "remaining", "provider": "none"},
                        "run": {"kind": "local"}},
            "manual-ctx": {"tracker": {"kind": "github"},
                           "repos": {"root": "/no/existe", "paths": []},
                           "autonomy": "manual",
                           "budget": {"polarity": "remaining", "provider": "none"},
                           "run": {"kind": "local"}},
        },
    })

    CRUDO = {
        "offline": False,
        "agents": [],
        "contexts": [{
            "name": "harness", "tracker": "github", "autonomy": "frontier",
            "vault": None, "run": "local",
            "budget": {"polarity": "remaining", "provider": "none",
                       "credits": None, "total": 0.0, "used": 0.0},
            "repos": [{
                "name": "koku", "tracker": "github", "slug": "Drokoz/koku",
                "branch": "main", "default_branch": "main", "path": "/repos/koku",
                "status_porcelain": "",
                "exists": {"gate": True, "skills": True, "context": True},
                "issues": [{"number": 7, "title": "t", "body": "",
                            "labels": [{"name": "ready-for-agent"}],
                            "blocked_by": 0}],
                "prs": [{"number": 31, "title": "p", "isDraft": False,
                         "headRefName": "fix/colision"}],
                "prs_merged": [],
            }],
        }],
    }

    def _mundo(self):
        return MundoMantener(pr_del_ticket=[{"number": 42,
                                             "headRefName": "ticket/7"}])

    def _correr(self, mod, mundo, argv, tmp):
        env = {"HERDR_ENV": "1", "HOME": tmp, "HARNESS_CONFIG_DIR": tmp}
        with mock.patch.object(mod, "collect",
                               lambda ctxs, offline=False, workers=8:
                               json.loads(json.dumps(self.CRUDO)),
                               create=True), \
                mock.patch.object(mod.adapters, "run", mundo.cmd), \
                mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = mod.main(argv)
        return rc, out.getvalue(), mundo

    def _modulo(self, tmp):
        (Path(tmp) / "config.json").write_text(self.CONFIG)
        return load_harness()

    def test_run_mantiene_antes_que_despachar_tickets(self):
        """Con un PR no mergeable, `harness run` resuelve el conflicto y
        recien después despacha el ticket de la frontera (#56)."""
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._modulo(tmp)
            mundo = self._mundo()
            rc, out, mundo = self._correr(
                mod, mundo,
                ["run", "--context", "harness", "--log", str(Path(tmp) / "ev.jsonl")],
                tmp)
            self.assertEqual(rc, 0, out)
            add_mant = [i for i, c in enumerate(mundo.llamadas)
                        if "worktree" in c[0] and "add" in c[0]
                        and any("koku-ticket-31" in a for a in c[0])]
            add_ticket = [i for i, c in enumerate(mundo.llamadas)
                          if "worktree" in c[0] and "add" in c[0]
                          and any("koku-ticket-7" in a for a in c[0])]
            self.assertTrue(add_mant, "no se creó el worktree del mantenimiento")
            self.assertTrue(add_ticket, "no se creó el worktree del ticket")
            self.assertLess(add_mant[0], add_ticket[0],
                            "el mantenimiento tiene que correr antes que el ticket")
            self.assertIn("PR #31/koku", out)
            self.assertIn("#7/koku", out)

    def test_maintain_solo_mantiene(self):
        """`harness maintain` toca los PRs no mergeables y nada de la
        frontera: no crea el worktree del ticket."""
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._modulo(tmp)
            mundo = self._mundo()
            rc, out, mundo = self._correr(
                mod, mundo,
                ["maintain", "--context", "harness",
                 "--log", str(Path(tmp) / "ev.jsonl")], tmp)
            self.assertEqual(rc, 0, out)
            self.assertIn("PR #31/koku", out)
            self.assertFalse(any("worktree" in c[0] and "add" in c[0]
                                 and any("koku-ticket-7" in a for a in c[0])
                                 for c in mundo.llamadas))

    def test_maintain_sin_conflictos_no_hace_nada(self):
        mundo = MundoMantener(mergeable_despues="MERGEABLE")
        # Sin agente trabajando el PR sigue MERGEABLE: nada que mantener.
        mundo.resuelto = True
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._modulo(tmp)
            rc, out, mundo = self._correr(
                mod, mundo,
                ["maintain", "--context", "harness",
                 "--log", str(Path(tmp) / "ev.jsonl")], tmp)
            self.assertEqual(rc, 0, out)
            self.assertIn("nada que mantener", out)
            self.assertFalse(mundo.llamo("herdr", "agent", "start"))

    def test_contexto_manual_no_toma_nada(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._modulo(tmp)
            mundo = self._mundo()
            rc, out, _ = self._correr(
                mod, mundo,
                ["maintain", "--context", "manual-ctx",
                 "--log", str(Path(tmp) / "ev.jsonl")], tmp)
            self.assertEqual(rc, 0, out)
            self.assertIn("autonomía manual", out)

    def test_sin_herdr_no_corre(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text(self.CONFIG)
            mod = load_harness()
            env = {"HERDR_ENV": "", "HOME": tmp, "HARNESS_CONFIG_DIR": tmp}
            with mock.patch.dict(os.environ, env, clear=False), \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                rc = mod.main(["maintain", "--context", "harness"])
            self.assertEqual(rc, 2)
            self.assertIn("herdr", err.getvalue())


if __name__ == "__main__":
    unittest.main()
