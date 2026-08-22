"""El dispatcher (`harness run`) con el mundo de mentira: un fake `run_cmd`
con respuestas scripteadas por herdr, git y gh.

Lo que se verifica acá son las reglas que hacen seguro dejarlo corriendo:
un worktree aislado por ticket, el prompt verificado de verdad (el primero
se pierde), el gate como red, el abandono sin atascarse, la limpieza del
pane y del worktree, el log JSONL con `origen`, el costo, y que jamás sale
un comando de merge.
"""

import json
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import support
from harness import dispatch
from harness.dispatch import (DispatchSpec, Dispatcher, EventLog, Job, cosechar,
                              nombre_agente, prompt_de, una_linea, worktree_path)

ROOT = support.ROOT
HARNESS = ROOT / "bin" / "harness"

NOMBRE_VALIDO = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# La línea de estado de la TUI de pi, con contexto a 0% (prompt todavía no llega).
PANE_CERO = "⠏ Working...\n~/repo (ticket/7)\n↑1k ↓0.2k R2k CH0.0% $0.012 0.0%/262k ...\n"
# El prompt llegó: el contexto subió y hay costo acumulado.
PANE_ARRANCO = "⠏ Working...\n~/repo (ticket/7)\n↑12k ↓2k R4k CH0.1% $0.056 1.2%/262k ...\n"


class Mundo:
    """El mundo del dispatcher: run_cmd scripteado + créditos + sueño corto.

    `respuestas` es una lista de (pruebo(args), (ok, out)); gana la primera
    que aplique. Lo que no coincide cae al default por comando.
    """

    def __init__(self, pane_out=PANE_ARRANCO, wait_out=None, gate=(True, "VERDE"),
                 prs=None, credits=(None, None)):
        self.pane_out = pane_out
        self.wait_out = wait_out or '{"result":{"agent":{"agent_status":"idle"}}}'
        self.gate = gate
        self.prs = prs if prs is not None else [
            {"number": 31, "headRefName": "ticket/7"}]
        self.creditos = list(credits)
        self.n_creditos = 0
        self.respuestas = []
        self.llamadas = []
        self.lock = threading.Lock()
        self.activo = 0
        self.max_activo = 0

    def responder(self, pruebo, ok_out):
        self.respuestas.append((pruebo, ok_out))
        return self

    def cmd(self, args, cwd=None, timeout=30):
        args = list(args)
        with self.lock:
            self.llamadas.append((tuple(args), cwd, timeout))
            self.activo += 1
            self.max_activo = max(self.max_activo, self.activo)
        try:
            for pruebo, ok_out in self.respuestas:
                if pruebo(args):
                    return ok_out(args) if callable(ok_out) else ok_out
            return self._default(args, cwd)
        finally:
            with self.lock:
                self.activo -= 1

    def _default(self, args, cwd):
        a = args[0]
        if a == "git":
            if "rev-parse" in args:
                return (False, "")  # la rama no existe todavía: -b
            return (True, "")
        if a == "herdr":
            sub = args[1:3]
            if sub[:2] == ["pane", "split"]:
                return (True, '{"result":{"pane":{"pane_id":"w9:p1"}}}')
            if sub[:2] == ["pane", "read"]:
                return (True, self.pane_out)
            if sub[:2] == ["agent", "wait"]:
                return (True, self.wait_out)
            return (True, "")
        if a == "gh":
            if "pr" in args and "list" in args:
                return (True, json.dumps(self.prs))
            if "issue" in args and "close" in args:
                return (True, "")
            return (True, '{"state":"CLOSED"}')
        if a == "./scripts/gate.sh":
            return self.gate
        return (True, "")

    def credits(self):
        i = min(self.n_creditos, len(self.creditos) - 1)
        self.n_creditos += 1
        return self.creditos[i]

    def dormir(self, s):
        pass

    def llamo(self, *parte):
        return [c for c in self.llamadas
                if all(x in c[0] for x in parte)]


def log_en(tmp, nombre="ev.jsonl"):
    return EventLog(Path(tmp) / nombre, "personal",
                    reloj=lambda: "2026-08-22T12:00:00Z")


def spec(**kw):
    return DispatchSpec(contexto="personal", **kw)


def job(issue=7, repo="koku", path="/repos/koku", slug="Drokoz/koku"):
    return Job(repo=repo, repo_path=path, slug=slug, issue=issue)


def despachar(mundo, jobs, **kw):
    with tempfile.TemporaryDirectory() as tmp:
        log = log_en(tmp)
        d = Dispatcher(spec(**kw), log, mundo.cmd, credits=mundo.credits,
                       dormir=mundo.dormir)
        res = d.dispatch(jobs)
        lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
    return res, lineas


class TestPuros(unittest.TestCase):
    def test_una_linea(self):
        self.assertEqual(una_linea("a\nb"), "a b")
        self.assertEqual(una_linea("a\r\nb\r c"), "a b c")
        self.assertEqual(una_linea("  a\n  "), "a")

    def test_prompt_es_una_linea_y_lleva_las_reglas(self):
        p = prompt_de(7)
        self.assertNotIn("\n", p)
        self.assertIn("#7", p)
        self.assertIn("Closes #7", p)
        self.assertIn("./scripts/gate.sh", p)
        self.assertIn("never merge", p)

    def test_nombre_agente_es_valido_y_distingue(self):
        for n in (nombre_agente("koku", 7), nombre_agente("ERP-IphoneUp", 7),
                  nombre_agente("entrevestidos-ventas", 12345)):
            self.assertRegex(n, NOMBRE_VALIDO, n)
        self.assertNotEqual(nombre_agente("koku", 7), nombre_agente("koku", 8))
        self.assertNotEqual(nombre_agente("koku", 7), nombre_agente("koku-api", 7))

    def test_worktree_diferente_por_ticket(self):
        a = worktree_path("/repos/koku", 7)
        b = worktree_path("/repos/koku", 8)
        self.assertNotEqual(a, b)
        self.assertIn(".worktrees", a.parts)


class TestEventLog(unittest.TestCase):
    def test_lineas_completas_y_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "e.jsonl"
            reloj = lambda: "2026-08-22T12:00:00Z"
            l1 = EventLog(p, "personal", reloj=reloj)
            l1.write("worktree", "ticket/7", "/x")
            l2 = EventLog(p, "personal", reloj=lambda: "otra-timestamp")
            l2.write("gate", "ticket/7", "verde")
            lineas = [json.loads(l) for l in p.read_text().splitlines()]
            self.assertEqual(len(lineas), 2)
            for linea in lineas:
                self.assertEqual(
                    sorted(linea), ["contexto", "cuerpo", "origen", "ref",
                                    "timestamp", "tipo"])
                self.assertEqual(linea["origen"], "harness")
            self.assertEqual(lineas[0]["contexto"], "personal")
            self.assertEqual(lineas[1]["timestamp"], "otra-timestamp")


class TestCicloDeVida(unittest.TestCase):
    def test_happy_path_llega_a_hecho(self):
        m = Mundo()
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        tipos = [l["tipo"] for l in lineas]
        for esperado in ("corrida", "worktree", "pane", "agente", "prompt",
                         "gate", "pr", "costo", "limpieza"):
            self.assertIn(esperado, tipos, tipos)
        self.assertNotIn("abandono", tipos)

    def test_worktrees_aislados(self):
        m = Mundo()
        res, lineas = despachar(m, [job(7), job(8)], max_parallel=2)
        wts = [l["cuerpo"] for l in lineas if l["tipo"] == "worktree"]
        self.assertEqual(len(wts), 2)
        self.assertNotEqual(wts[0], wts[1])
        splits = m.llamo("herdr", "pane", "split")
        self.assertEqual(len(splits), 2)
        cwds = []
        for s in splits:
            a = s[0]
            self.assertIn("--cwd", a)
            cwds.append(a[a.index("--cwd") + 1])
        self.assertEqual(len(set(cwds)), 2)

    def test_nunca_merge(self):
        m = Mundo()
        despachar(m, [job()])
        merges = [c for c in m.llamadas
                  if "merge" in c[0] and "pr" in c[0]]
        self.assertEqual(merges, [])

    def test_el_prompt_se_envia_en_una_linea(self):
        m = Mundo()
        despachar(m, [job()])
        (prompt_call,) = m.llamo("herdr", "agent", "prompt")
        texto = prompt_call[0][4]  # herdr agent prompt <target> <texto> ...
        self.assertNotIn("\n", texto)
        self.assertNotIn("\r", texto)

    def test_bloqueado_se_abandona_y_sigue_el_siguiente(self):
        m = Mundo(prs=[{"number": 31, "headRefName": "ticket/7"},
                       {"number": 32, "headRefName": "ticket/8"}])
        wait_bloqueado = '{"result":{"agent":{"agent_status":"blocked"}}}'
        # el agente del ticket 7 queda bloqueado; el del 8 termina bien
        m.responder(lambda a: a[:3] == ["herdr", "agent", "wait"] and a[3] == "koku-7",
                    (True, wait_bloqueado))
        res, lineas = despachar(m, [job(7), job(8)])
        por_issue = {r.issue: r for r in res}
        self.assertEqual(por_issue[7].estado, "abandonado")
        self.assertIn("bloqueado", por_issue[7].motivo)
        self.assertEqual(por_issue[8].estado, "hecho")
        abandono = [l for l in lineas if l["tipo"] == "abandono"]
        self.assertEqual(abandono[0]["ref"], "ticket/7")

    def test_gate_rojo_no_tiene_pr(self):
        m = Mundo(gate=(False, "ROJO: unittest"))
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertIn("gate", res[0].motivo)
        self.assertNotIn("pr", [l["tipo"] for l in lineas])

    def test_terminar_sin_pr_es_abandono(self):
        m = Mundo(prs=[{"number": 31, "headRefName": "otra-rama"}])
        res, _ = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertIn("sin PR", res[0].motivo)

    def test_timeout_de_espera_es_abandono(self):
        m = Mundo()
        m.wait_out = '{"error":{"code":"timeout"},"id":"cli:agent:wait"}'
        res, _ = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertIn("timeout", res[0].motivo)

    def test_falla_el_worktree_y_el_job_no_arranca(self):
        m = Mundo().responder(
            lambda a: a[0] == "git" and "worktree" in a and "add" in a,
            (False, "fatal: no pathspec"))
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(m.llamo("herdr", "agent", "start"), [])
        self.assertEqual(m.llamo("herdr", "agent", "prompt"), [])

    def test_rama_ya_existente_no_choca(self):
        m = Mundo()
        m.responder(lambda a: "rev-parse" in a, (True, "sha\n"))
        despachar(m, [job()])
        (add,) = m.llamo("git", "worktree", "add")
        self.assertNotIn("-b", add[0])


class TestPromptPerdido(unittest.TestCase):
    def test_reintenta_hasta_que_el_contexto_sube(self):
        m = Mundo(pane_out=PANE_CERO)  # el contexto nunca sube solo

        def pane_por_intento(args):
            # cuenta los prompts enviados; el segundo hace "llegar" el trabajo
            if len(m.llamo("herdr", "agent", "prompt")) >= 2:
                return (True, PANE_ARRANCO)
            return (True, PANE_CERO)

        m.responder(lambda a: a[:3] == ["herdr", "pane", "read"], pane_por_intento)
        res, lineas = despachar(m, [job()], verify_wait_s=0.05, verify_poll_s=0.01)
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertEqual(len(m.llamo("herdr", "agent", "prompt")), 2)
        intentos = [l for l in lineas if l["tipo"] == "prompt"]
        self.assertEqual(len(intentos), 2)

    def test_si_nunca_sube_se_abandona(self):
        m = Mundo(pane_out=PANE_CERO)
        res, lineas = despachar(m, [job()], verify_wait_s=0.01,
                                verify_poll_s=0.01, verify_retries=3)
        self.assertEqual(res[0].estado, "abandonado")
        self.assertIn("0%", res[0].motivo)
        self.assertEqual(len(m.llamo("herdr", "agent", "prompt")), 3)
        self.assertIn("abandono", [l["tipo"] for l in lineas])


class TestLimpieza(unittest.TestCase):
    def test_cierra_el_pane_y_quita_el_worktree(self):
        m = Mundo()
        despachar(m, [job()])
        self.assertEqual(len(m.llamo("herdr", "pane", "close")), 1)
        removes = m.llamo("git", "worktree", "remove")
        self.assertEqual(len(removes), 1)
        self.assertNotIn("branch", removes[0][0])  # la rama queda

    def test_limpieza_tambien_si_falla_a_medio(self):
        m = Mundo(gate=(False, "ROJO"))
        despachar(m, [job()])
        self.assertEqual(len(m.llamo("herdr", "pane", "close")), 1)
        self.assertEqual(len(m.llamo("git", "worktree", "remove")), 1)


class TestCosto(unittest.TestCase):
    def test_costo_por_job_de_la_linea_de_estado(self):
        m = Mundo()
        res, lineas = despachar(m, [job()])
        self.assertAlmostEqual(res[0].costo, 0.056)
        costo = [l for l in lineas if l["tipo"] == "costo" and l["ref"] == "ticket/7"]
        self.assertIn("0.056", costo[0]["cuerpo"])

    def test_costo_de_la_corrida_por_diferencia_de_creditos(self):
        m = Mundo(credits=[100.0, 100.25])
        _, lineas = despachar(m, [job()])
        costo = [l for l in lineas if l["tipo"] == "costo" and l["ref"] == "corrida"]
        self.assertEqual(len(costo), 1)
        self.assertIn("0.25", costo[0]["cuerpo"])

    def test_sin_creditos_no_hay_evento_de_corrida(self):
        m = Mundo(credits=[None, None])
        _, lineas = despachar(m, [job()])
        self.assertNotIn("corrida", [l["ref"] for l in lineas
                                     if l["tipo"] == "costo"])


class TestParalelismo(unittest.TestCase):
    def test_el_tope_linda_el_concurrencia(self):
        import time as _t

        def espera_lenta(args):
            _t.sleep(0.05)
            return (True, '{"result":{"agent":{"agent_status":"idle"}}}')

        m = Mundo().responder(
            lambda a: a[:3] == ["herdr", "agent", "wait"], espera_lenta)
        res, _ = despachar(m, [job(i) for i in (1, 2, 3, 4)], max_parallel=2)
        self.assertEqual(len(res), 4)  # la cola entera se drena
        self.assertEqual(m.max_activo, 2)  # pero nunca más de 2 a la vez

    def test_max_configurable(self):
        m = Mundo()
        res, _ = despachar(m, [job(i) for i in (1, 2, 3)], max_parallel=1)
        self.assertEqual(m.max_activo, 1)
        self.assertEqual(len(res), 3)


class TestCosecha(unittest.TestCase):
    def test_cierra_el_issue_dejado_abierto_por_el_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = Mundo()
            m.prs = [{"number": 30, "headRefName": "ticket/7",
                      "closingIssuesReferences": [{"number": 7}]}]
            m.responder(lambda a: a[:3] == ["gh", "issue", "view"],
                        (True, '{"state":"OPEN"}'))
            log = log_en(tmp)
            cerrados = cosechar("Drokoz/koku", m.cmd, log)
            self.assertEqual(cerrados, [7])
            self.assertEqual(len(m.llamo("gh", "issue", "close", "7")), 1)
            lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
            self.assertEqual(lineas[0]["tipo"], "issue-cerrado")

    def test_no_toque_un_issue_que_ya_esta_cerrado(self):
        m = Mundo()
        m.prs = [{"number": 30, "headRefName": "ticket/7",
                  "closingIssuesReferences": [{"number": 7}]}]
        m.responder(lambda a: a[:3] == ["gh", "issue", "view"],
                    (True, '{"state":"CLOSED"}'))
        with tempfile.TemporaryDirectory() as tmp:
            cerrados = cosechar("Drokoz/koku", m.cmd, log_en(tmp))
        self.assertEqual(cerrados, [])
        self.assertEqual(m.llamo("gh", "issue", "close"), [])


class TestSnapshotPath(unittest.TestCase):
    def test_el_repo_lleva_su_path(self):
        from harness import snapshot as snap
        raw = support.raw_personal()
        raw["repos"][0]["path"] = "/repos/agent-harness"
        repos = snap.snapshot({"offline": False, "agents": None,
                               "contexts": [raw]}).contexts[0].repos
        self.assertEqual(repos[0].path, "/repos/agent-harness")


class TestCliRun(unittest.TestCase):
    """`harness run` en el CLI: el contexto manual no toma nada solo, y sin
    herdr ni adaptadores no corre."""

    DOS_CONTEXTOS = json.dumps({
        "default_context": "manual-ctx",
        "contexts": {
            "manual-ctx": {"tracker": {"kind": "github"},
                           "repos": {"root": "/no/existe", "paths": []},
                           "autonomy": "manual",
                           "budget": {"polarity": "remaining", "provider": "none"},
                           "run": {"kind": "local"}},
            "frontier-ctx": {"tracker": {"kind": "github"},
                             "repos": {"root": "/no/existe", "paths": []},
                             "autonomy": "frontier",
                             "budget": {"polarity": "remaining", "provider": "none"},
                             "run": {"kind": "local"}},
        },
    })

    def _run(self, *args, env_extra=None):
        env_extra = env_extra or {}
        with tempfile.TemporaryDirectory() as tmp:
            import os as _os
            env = dict(_os.environ)
            env["HOME"] = tmp
            env["HARNESS_CONFIG_DIR"] = tmp
            env["HARNESS_OFFLINE"] = "1"
            env["HERDR_ENV"] = ""
            env.pop("HERDR_ENV", None)
            env.update(env_extra)
            (Path(tmp) / "config.json").write_text(self.DOS_CONTEXTOS)
            return subprocess.run([str(HARNESS), *args], env=env,
                                  capture_output=True, text=True, timeout=120)

    def test_contexto_manual_no_toma_nada(self):
        p = self._run("run", "--context", "manual-ctx")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("autonomía manual", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_frontier_sin_herdr_no_corre(self):
        p = self._run("run", "--context", "frontier-ctx")
        self.assertEqual(p.returncode, 2)
        self.assertIn("herdr", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
