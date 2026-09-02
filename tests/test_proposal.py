"""El generador y el juez de trabajo nuevo (`harness proponer`, #115)
contra un mundo scripteado.

La regla dura no vive en el prompt, vive acá: el generador no abre issues
(el harness los abre, a `needs-triage`, después de verificar la evidencia
y dedupar contra abiertos Y cerrados), el juez no pone etiquetas (el
harness lo hace, y sólo con un veredicto explícito de promover), y el
default del veredicto es rechazar: cero promovidos es un resultado válido,
no un error.

Escenarios mínimos (del ticket): generador sin evidencia, duplicado contra
un cerrado, juez que rechaza todo, tope alcanzado.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support
from harness.dispatch import (BucleSpec, DispatchSpec, EventLog, Pasada, bucle)
from harness.proposal import (JUEZ_CLAUDE, JUEZ_QWEN, MAX_PROPOSITAS_DEFAULT,
                              Proponer, aplicar_tope, cuerpo_issue,
                              clave_propuesta, decidir_peldano_juez,
                              es_duplicado, issues_conocidos,
                              normalizar_titulo, parsear_veredictos,
                              validar_propuesta)

ROOT = support.ROOT

# El árbol del fake worktree: dos archivos con líneas reales para que la
# evidencia se pueda "ir a leer".
ARCHIVO_X = ["def uno():", "    return 1", "", "def dos():",
             "    return dos()", "    # bug: se llama a sí mismo", "",
             "TRES = 3", "CUATRO = 4", "CINCO = 5"]
ARCHIVO_B = ["class W:", "    def abrir(self):",
             "        self.estado = 'abierto'",
             "        # falta cerrar: el W.O. queda prendido"]

PANE_ARRANCO = "⠐ Working...\n~/fake\n↑12k ↓2k R4k CH0.1% $0.056 1.2%/262k ...\n"


class MundoProponer:
    """El mundo del proponer: git, herdr y gh scripteados.

    El "agente" es el prompt: cuando llega, mundo escribe en el worktree
    el archivo de salida (`proposals.json` / `veredictos.json`) con lo que
    le digamos que dejó. Los worktrees son reales (carpetas en el tmp):
    `_done` lee los archivos y la evidencia contra el disco de verdad."""

    def __init__(self, known_issues=None, proposals=None, veredictos=None,
                 issue_numbers=None, status_extra=""):
        # None = gh falla (sin cola de issues); lista = la cola de dedup.
        self.known_issues = known_issues
        self.proposals = proposals
        self.veredictos = veredictos
        self.issue_numbers = list(issue_numbers or [501, 502, 503])
        self.status_extra = status_extra
        self.llamadas = []
        self.creados = []      # (title, body, args) de los `gh issue create`
        self.edits = []        # args de los `gh issue edit`
        self.comments = []     # args de los `gh issue comment`
        self.worktrees = []

    # ------------------------------------------------------------- mundo
    def _escribir_salida(self, prompt, worktree):
        if "work PROPOSER" in prompt and self.proposals is not None:
            Path(worktree, "proposals.json").write_text(
                json.dumps({"proposals": self.proposals}, ensure_ascii=False),
                encoding="utf-8")
        elif "work PROPOSER" in prompt:
            pass  # el generador no dejó el archivo
        elif "JUDGE" in prompt and self.veredictos is not None:
            Path(worktree, "veredictos.json").write_text(
                self.veredictos if isinstance(self.veredictos, str)
                else json.dumps(self.veredictos, ensure_ascii=False),
                encoding="utf-8")

    def cmd(self, args, cwd=None, timeout=30):
        args = list(args)
        self.llamadas.append((tuple(args), cwd))
        a = args[0]
        if a == "git":
            if "symbolic-ref" in args:
                return (True, "refs/remotes/origin/main\n")
            if "rev-parse" in args:
                return (True, "a" * 40 + "\n")
            if "worktree" in args and "add" in args:
                wt = args[args.index("add") + 2]
                Path(wt).mkdir(parents=True, exist_ok=True)
                for rel, lineas in (("harness/x.py", ARCHIVO_X),
                                    ("backend/app.py", ARCHIVO_B)):
                    p = Path(wt) / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("\n".join(lineas) + "\n", encoding="utf-8")
                self.worktrees.append(wt)
                return (True, "")
            if "status" in args:
                salida = self.status_extra
                if self.worktrees:
                    wt = Path(self.worktrees[-1])
                    for nombre in ("proposals.json", "veredictos.json"):
                        if (wt / nombre).is_file():
                            salida = ("?? " + nombre + "\n" + salida).strip()
                return (True, (salida + "\n") if salida else "")
            return (True, "")
        if a == "herdr":
            if "pane" in args and "split" in args:
                return (True, '{"result":{"pane":{"pane_id":"w1:p1"}}}')
            if "pane" in args and "read" in args:
                return (True, PANE_ARRANCO)
            if "agent" in args and "prompt" in args:
                prompt = args[4]
                if self.worktrees:
                    self._escribir_salida(prompt, self.worktrees[-1])
                return (True,
                        '{"result":{"agent":{"agent_status":"working"}}}')
            if "agent" in args and "wait" in args:
                return (True, '{"result":{"agent":{"agent_status":"idle"}}}')
            return (True, "")
        if a == "gh":
            if "issue" in args and "list" in args:
                if self.known_issues is None:
                    return (False, "rate limited")
                return (True, json.dumps(self.known_issues))
            if "issue" in args and "create" in args:
                if not self.issue_numbers:
                    return (False, "no hay números")
                num = self.issue_numbers.pop(0)
                self.creados.append((
                    args[args.index("--title") + 1],
                    args[args.index("--body") + 1],
                    args))
                return (True, "https://github.com/Drokoz/fake/issues/{}".format(
                    num))
            if "issue" in args and "edit" in args:
                self.edits.append(args)
                return (True, "")
            if "issue" in args and "comment" in args:
                self.comments.append(args)
                return (True, "")
            return (True, "")
        return (True, "")


def leer_log(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l]


class EscenarioProponer(unittest.TestCase):
    """Un `Proponer` contra el mundo scripteado, con tmp real para los
    worktrees (la evidencia se lee del disco de verdad)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.repo_path = self.d / "repos" / "fake"
        self.repo_path.mkdir(parents=True)
        self.log_path = self.d / "events.jsonl"

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
        self.tmp.cleanup()

    def correr(self, mundo, maximo=MAX_PROPOSITAS_DEFAULT,
               peldano_juez=None):
        spec = DispatchSpec(contexto="t", kind="pi",
                            model="qwen/qwen3.8-27b")
        log = EventLog(self.log_path, "t")
        prop = Proponer(spec, log, run_cmd=mundo.cmd)
        res = prop.proponer_repo("fake", "Drokoz/fake", str(self.repo_path),
                                 maximo, peldano_juez or JUEZ_QWEN)
        return res, leer_log(self.log_path)


def _propuesta(titulo="Arreglar el W.O. que traban para siempre",
               archivo="harness/x.py", linea=5):
    return {"title": titulo,
            "problem": "El camino se llama a sí mismo y se rompe por eso.",
            "evidence": [{"file": archivo, "line": linea}],
            "acceptance": ["Que el camino no se llame a sí mismo",
                           "Un test que lo cubra"]}


# --------------------------------------------------------------------- puros
class Puros(unittest.TestCase):
    def test_normalizar_titulo(self):
        self.assertEqual(normalizar_titulo("  Arreglar   el  W.O. "),
                         "arreglar el w.o.")

    def test_duplicado_por_titulo(self):
        conocidos = [(40, "Arreglar el W.O.", "")]
        self.assertEqual(es_duplicado("  ARREGLAR EL W.O.  ",
                                      [("harness/x.py", 5)], conocidos), 40)

    def test_duplicado_por_lo_que_cita(self):
        """Un issue CERRADO que ya citó esa referencia no se vuelve a
        proponer."""
        conocidos = [(40, "Otro título", "ver backend/app.py:4")]
        self.assertEqual(es_duplicado("Nuevo título",
                                      [("backend/app.py", 4)], conocidos), 40)
        self.assertIsNone(es_duplicado("Nuevo título",
                                       [("backend/app.py", 3)], conocidos))

    def test_validar_sin_evidencia(self):
        ok, motivo = validar_propuesta(
            {"title": "t", "problem": "p", "evidence": [], "acceptance": ["a"]},
            lambda a, l: "x")
        self.assertFalse(ok)
        self.assertIn("evidencia", motivo)

    def test_validar_evidencia_que_no_se_puede_leer(self):
        def leer(archivo, linea):
            if archivo == "harness/x.py" and linea <= len(ARCHIVO_X):
                return ARCHIVO_X[linea - 1]
            return None
        ok, motivo = validar_propuesta(
            {"title": "t", "problem": "p",
             "evidence": [{"file": "harness/x.py", "line": 999}],
             "acceptance": ["a"]}, leer)
        self.assertFalse(ok)
        self.assertIn("no se puede leer", motivo)
        ok, _ = validar_propuesta(
            {"title": "t", "problem": "p",
             "evidence": [{"file": "harness/x.py", "line": 5}],
             "acceptance": ["a"]}, leer)
        self.assertTrue(ok)

    def test_validar_sin_criterios(self):
        ok, motivo = validar_propuesta(
            {"title": "t", "problem": "p",
             "evidence": [{"file": "a.py", "line": 1}], "acceptance": []},
            lambda a, l: "x")
        self.assertFalse(ok)
        self.assertIn("acceptance", motivo)

    def test_aplicar_tope(self):
        dentro, fuera = aplicar_tope([1, 2, 3], 2)
        self.assertEqual((dentro, fuera), ([1, 2], [3]))

    def test_parsear_veredictos_adversarial(self):
        """El default es rechazar: sin archivo, JSON roto, issue no
        contestado, o un `promover` que no sea `true` explícito."""
        self.assertEqual(parsear_veredictos("", [1, 2]),
                         [(1, False, "sin veredicto: el juez no dejó el archivo"),
                          (2, False, "sin veredicto: el juez no dejó el archivo")])
        self.assertEqual(
            [p for _, p, _ in parsear_veredictos("{roto", [1])], [False])
        veredictos = json.dumps({"veredictos": [
            {"issue": 1, "promover": True, "motivo": "bug con evidencia"},
            {"issue": 3, "promover": "sí", "motivo": "no es un bool"},
        ]})
        self.assertEqual(parsear_veredictos(veredictos, [1, 2, 3]),
                         [(1, True, "bug con evidencia"),
                          (2, False, "sin veredicto: el juez no lo contestó"),
                          (3, False, "no es un bool")])

    def test_cuerpo_issue_lleva_lo_que_el_ticket_pide(self):
        cuerpo = cuerpo_issue(_propuesta())
        self.assertIn("## El problema", cuerpo)
        self.assertIn("`harness/x.py`:5", cuerpo)
        self.assertIn("- [ ] Que el camino no se llame a sí mismo", cuerpo)
        self.assertIn("#115", cuerpo)

    def test_issues_conocidos_sin_gh(self):
        self.assertIsNone(issues_conocidos("o/r", lambda a: (False, "boom")))

    def test_decidir_peldano_juez(self):
        p, _ = decidir_peldano_juez(True, 1_000_000, 0, 0.25)
        self.assertEqual(p, JUEZ_QWEN)
        p, motivo = decidir_peldano_juez(False, None, 0, 0.25)
        self.assertEqual(p, JUEZ_QWEN)
        self.assertIn("sin piso", motivo)
        p, motivo = decidir_peldano_juez(False, 1_000_000, 950_000, 0.25)
        self.assertEqual(p, JUEZ_QWEN)
        self.assertIn("piso de cuota", motivo)
        p, _ = decidir_peldano_juez(False, 1_000_000, 100_000, 0.25)
        self.assertEqual(p, JUEZ_CLAUDE)


# ---------------------------------------------------------------- la corrida
class LaCorrida(EscenarioProponer):
    def test_generador_sin_evidencia_no_abre_issues(self):
        """Sin evidencia (o con evidencia que no se puede leer) el issue no
        se abre, y no hay juez que juzgar."""
        mundo = MundoProponer(known_issues=[], proposals=[
            {"title": "Sin evidencia", "problem": "p",
             "evidence": [], "acceptance": ["a"]},
            _propuesta(titulo="Archivo que no existe", archivo="no/hay.py",
                       linea=1),
            _propuesta(titulo="Línea fuera de rango", linea=999),
        ])
        res, eventos = self.correr(mundo)
        self.assertEqual(res["abiertas"], 0)
        self.assertEqual(res["descartadas"], 3)
        self.assertEqual(mundo.creados, [], "sin evidencia no se abre")
        self.assertEqual(res["juez"], "sin propuestas")
        motivos = [e["cuerpo"] for e in eventos
                   if e["tipo"] == "propuesta" and "descartada" in e["cuerpo"]]
        self.assertTrue(any("sin evidencia" in m for m in motivos))
        self.assertTrue(any("no se puede leer" in m for m in motivos))

    def test_duplicado_contra_un_cerrado_no_se_abre(self):
        """Dedup contra abiertos Y cerrados: un issue ya rechazado no se
        vuelve a proponer ni por título ni por lo que cita."""
        mundo = MundoProponer(
            known_issues=[
                {"number": 40, "title": "Arreglar el W.O. que traban para "
                                        "siempre", "body": "ver backend/app.py:4"},
                {"number": 41, "title": "Otro", "body": ""},
            ],
            proposals=[
                _propuesta(titulo="  ARREGLAR EL W.O. QUE TRABAN PARA SIEMPRE"),
                _propuesta(titulo="Nueva redacción", archivo="backend/app.py",
                           linea=4),
            ])
        res, eventos = self.correr(mundo)
        self.assertEqual(res["abiertas"], 0)
        self.assertEqual(res["descartadas"], 2)
        self.assertEqual(mundo.creados, [])
        dups = [e["cuerpo"] for e in eventos
                if e["tipo"] == "propuesta" and "duplicado" in e["cuerpo"]]
        self.assertEqual(len(dups), 2)
        self.assertTrue(all("#40" in d for d in dups))

    def test_juez_que_rechaza_todo(self):
        """Cero promovidos es un resultado válido: no hay abandono, no hay
        etiqueta, los issues quedan en needs-triage y el log lo dice."""
        mundo = MundoProponer(known_issues=[],
                              proposals=[_propuesta(),
                                         _propuesta(titulo="Otra cosa",
                                                    linea=7)],
                              veredictos={"veredictos": [
                                  {"issue": 501, "promover": False,
                                   "motivo": "un refactor que nadie pidió"},
                                  {"issue": 502, "promover": False,
                                   "motivo": "evidencia débil"},
                              ]})
        res, eventos = self.correr(mundo)
        self.assertEqual(res["abiertas"], 2)
        self.assertEqual(res["promovidas"], 0)
        self.assertEqual(res["rechazadas"], 2)
        self.assertEqual(res["juez"], "corrido")
        self.assertEqual(mundo.edits, [],
                         "sin promoción no hay ready-for-agent")
        self.assertEqual(len(mundo.comments), 2,
                         "lo que no promueve queda anotado, no tirado")
        abandonos = [e for e in eventos if e["tipo"] == "abandono"]
        self.assertEqual(abandonos, [], "cero promovidos no es un error")
        fin = [e for e in eventos if e["tipo"] == "proponer"
               and e["ref"] == "propuesta/juez"][-1]
        self.assertIn("cero promovidos", fin["cuerpo"])
        self.assertIn("0 promovido(s)", fin["cuerpo"])

    def test_tope_de_propuestas_por_corrida(self):
        """El tope duro: más propuestas que `--max-propuestas` no se abren
        todas, y las que se quedan fuera se anotan con su motivo."""
        mundo = MundoProponer(known_issues=[], proposals=[
            _propuesta(titulo="Uno", linea=1),
            _propuesta(titulo="Dos", linea=2),
            _propuesta(titulo="Tres", linea=3),
        ])
        res, eventos = self.correr(mundo, maximo=2)
        self.assertEqual(res["abiertas"], 2)
        self.assertEqual(res["descartadas"], 1)
        self.assertEqual([t for t, _, _ in mundo.creados], ["Uno", "Dos"])
        tope = [e["cuerpo"] for e in eventos
                if e["tipo"] == "propuesta" and "tope" in e["cuerpo"]]
        self.assertEqual(len(tope), 1)
        self.assertIn("Tres", tope[0])

    def test_el_juez_promueve_solo_lo_que_veredicta(self):
        """El harness (no el agente) es el que pone `ready-for-agent`, y
        sólo con veredicto explícito. Lo otro queda en needs-triage con el
        motivo."""
        mundo = MundoProponer(known_issues=[],
                              proposals=[_propuesta(),
                                         _propuesta(titulo="Otra cosa",
                                                    linea=7)],
                              veredictos={"veredictos": [
                                  {"issue": 501, "promover": True,
                                   "motivo": "bug con evidencia real"},
                                  {"issue": 502, "promover": False,
                                   "motivo": "no vale la pena"},
                              ]})
        res, eventos = self.correr(mundo)
        self.assertEqual(res["promovidas"], 1)
        self.assertEqual(res["rechazadas"], 1)
        # El generador abre a needs-triage, nunca a ready-for-agent.
        for _, _, args in mundo.creados:
            self.assertIn("needs-triage", args)
            self.assertNotIn("ready-for-agent", args)
        # La etiqueta la pone el harness, con el veredicto del juez.
        self.assertEqual(len(mundo.edits), 1)
        edit = mundo.edits[0]
        self.assertIn("501", edit)
        self.assertIn("--add-label", edit)
        self.assertIn("ready-for-agent", edit)
        self.assertIn("--remove-label", edit)
        self.assertIn("needs-triage", edit)
        rechazado = [e for e in eventos if e["tipo"] == "rechazo"]
        self.assertEqual(len(rechazado), 1)
        self.assertEqual(rechazado[0]["ref"], "#502")
        self.assertEqual(len(mundo.comments), 1)

    def test_el_arbol_no_puede_ensuciarse_fuera_de_la_salida(self):
        """El generador no toca código: un `.py` modificado junto a la
        salida es abandono, no trabajo."""
        mundo = MundoProponer(known_issues=[], proposals=[_propuesta()],
                              status_extra=" M harness/x.py")
        res, eventos = self.correr(mundo)
        self.assertEqual(res["abiertas"], 0)
        self.assertEqual(res["juez"], "sin juez (generador abandonado)")
        self.assertTrue(any("toco archivos" in e["cuerpo"]
                            for e in eventos if e["tipo"] == "abandono"))

    def test_sin_cola_de_issues_no_se_propone(self):
        """Proponer a ciegas contra el historial es fabricar duplicados:
        sin gh no hay generador."""
        mundo = MundoProponer(known_issues=None, proposals=[_propuesta()])
        res, eventos = self.correr(mundo)
        self.assertEqual(res["abiertas"], 0)
        self.assertEqual(mundo.creados, [])
        self.assertTrue(any("no se propone a ciegas" in e["cuerpo"]
                            for e in eventos if e["tipo"] == "proponer"))
        self.assertEqual([e for e in eventos if e["tipo"] == "worktree"], [],
                         "sin cola ni worktree")

    def test_la_clave_de_log_no_colisiona_con_la_de_ticket(self):
        mundo = MundoProponer(known_issues=[], proposals=[_propuesta()])
        _, eventos = self.correr(mundo)
        claves = {e["ticket"] for e in eventos if e["ticket"]}
        self.assertIn("fake#propuesta-generador", claves)
        self.assertIn("fake#501", claves)
        self.assertNotIn("fake#proponer", claves)
        self.assertEqual(clave_propuesta("fake", "juez"),
                         "fake#propuesta-juez")


# --------------------------------------------------------------------- bucle
class ElBuclePropone(unittest.TestCase):
    """La frontera vacía se gasta en proponer (#115): una vez por bucle, en
    vez de en esperar en blanco. Un fallo del proposer no tumba el bucle."""

    def _correr(self, resultados, proposer, **spec):
        with tempfile.TemporaryDirectory() as d:
            log = EventLog(Path(d) / "events.jsonl", "harness")
            estado = {"n": 0}
            fallos = []

            def pasada():
                estado["n"] += 1
                r = resultados[estado["n"] - 1]
                return Pasada(r == "trabajo", "detalle")

            def proposer_con_fallo():
                fallos.append(1)
                if proposer is not None:
                    return proposer()
                raise RuntimeError("el proposer se rompió")

            corte = bucle(pasada, BucleSpec(**spec), log,
                          proposer_vacio=proposer_con_fallo,
                          dormir=lambda s: None)
            return corte, leer_log(Path(d) / "events.jsonl"), fallos

    def test_corre_una_vez_por_bucle(self):
        llamadas = []
        corte, eventos, _ = self._correr(
            ["vacia", "vacia", "vacia"], lambda: llamadas.append(1),
            vueltas_vacias_max=3)
        self.assertEqual(len(llamadas), 1, "una vez por bucle, no por vuelta")
        self.assertIn("nada nuevo", corte)
        prop = [e for e in eventos if e["tipo"] == "proponer"]
        self.assertEqual(len(prop), 1)
        self.assertIn("frontera vacia", prop[0]["cuerpo"])

    def test_un_fallo_del_proposer_no_tumba_el_bucle(self):
        corte, eventos, fallos = self._correr(
            ["vacia", "vacia"], None, vueltas_vacias_max=2)
        self.assertEqual(fallos, [1], "intentó una sola vez")
        self.assertIn("nada nuevo", corte, "el bucle sigue y corta por el tope")
        prop = [e for e in eventos if e["tipo"] == "proponer"]
        self.assertEqual(len(prop), 1)
        self.assertIn("fallo del proposer", prop[0]["cuerpo"])

    def test_con_trabajo_no_propone(self):
        llamadas = []
        corte, _, _ = self._correr(
            ["trabajo", "trabajo"], lambda: llamadas.append(1),
            max_pasadas=2)
        self.assertEqual(llamadas, [], "la frontera no está vacía")
        self.assertIn("tope", corte)


# --------------------------------------------------------------------- CLI
class ElCli(unittest.TestCase):
    """El modo sin adaptadores: `proponer` no existe offline, igual que
    `run` — un dispatcher sin mundo no es un dispatcher."""

    CONTEXTO = json.dumps({
        "default_context": "h",
        "contexts": {
            "h": {"tracker": {"kind": "github"},
                  "repos": {"root": "/no/existe", "paths": []},
                  "autonomy": "frontier",
                  "budget": {"polarity": "remaining", "provider": "none"},
                  "run": {"kind": "local"}},
        },
    })

    def _run(self, args):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["HOME"] = tmp
            env["HARNESS_CONFIG_DIR"] = tmp
            env["HARNESS_OFFLINE"] = "1"
            env.pop("HERDR_ENV", None)
            (Path(tmp) / "config.json").write_text(self.CONTEXTO)
            return subprocess.run(
                [str(support.ROOT / "bin" / "harness"), *args], env=env,
                capture_output=True, text=True, timeout=120)

    def test_offline_no_existe(self):
        p = self._run(["proponer"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("herdr", p.stderr)

    def test_max_propuestas_inválido(self):
        p = self._run(["proponer", "--max-propuestas", "0"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("--max-propuestas", p.stderr)

    def test_piso_cuota_fuera_de_rango(self):
        p = self._run(["proponer", "--piso-cuota", "1.5"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("--piso-cuota", p.stderr)


if __name__ == "__main__":
    unittest.main()
