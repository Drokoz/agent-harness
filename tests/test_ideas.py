"""El agente semanal de ideas (`harness ideas`, #117) contra un mundo
scripteado.

La regla dura no vive en el prompt, vive acá: si una idea no apunta a
código que ya existe y se puede ir a leer, no se escribe; no se repite
una idea ya escrita en la vault; el tope de notas por corrida es duro;
y los campos que una nota tiene que traer (rubro, quién paga, días,
versión mínima, precio, por qué podría fallar) son validación mecánica,
no confianza.

Escenarios mínimos (del ticket): repos sin nada reutilizable, idea
duplicada contra la vault, tope alcanzado.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support
from harness.dispatch import DispatchSpec, EventLog
from harness.ideas import (MAX_IDEAS_DEFAULT, Ideas, clave_idea,
                           debe_correr, es_duplicado_idea,
                           guardar_ultima_corrida, leer_ideas_vault,
                           leer_ultima_corrida, nota_idea, nota_escribir,
                           prompt_ideas, validar_idea)

ROOT = support.ROOT

# Los "repos de verdad" del mundo scripteado: archivos con líneas reales
# para que la evidencia se pueda ir a leer.
REPO_KOKU = {"src/print-agent.ts": [
    "export function imprimirEnLan(doc) {",
    "  const socket = net.connect(IMPRESORA);",
    "  socket.write(doc);",
    "}",
    "export const SIN_NUBE = true;"]}
REPO_ERP = {"app/caja.py": [
    "class Caja:",
    "    def abrir(self):",
    "        self.billetes = inventario_inicial()",
    "    def cerrar(self):",
    "        return reconciliar(self.billetes, self.movimientos)"]}

PANE_ARRANCO = "⠐ Working...\n~/fake\n↑12k ↓2k R4k CH0.1% $0.056 1.2%/262k ...\n"


def _idea(titulo="El print-agent de koku sin nube",
          repo="koku", archivo="src/print-agent.ts", linea=2,
          rubro="Carnicerías de barrio en Santiago",
          **sobre):
    base = {"title": titulo,
            "patron": "Impresión en una impresora de la LAN sin nube, ya "
                      "andando en {}.".format(repo),
            "evidence": [{"repo": repo, "file": archivo, "line": linea}],
            "rubro": rubro,
            "paga": "El carnicero: hoy paga un PC con Windows en cada "
                    "mostrador y alguien que lo arregle",
            "falta_dias": 15,
            "version_minima": "El print-agent solo, sin comandas ni turnos",
            "precio": "CLP 800 mil por local, instalación incluida",
            "riesgo": "El carnicero no cambia el Windows si le funciona"}
    base.update(sobre)
    return base


class MundoIdeas:
    """El mundo de ideas: git, herdr y gh scripteados.

    El "agente" es el prompt: cuando llega, el mundo escribe en el
    worktree el `ideas.json` con lo que le digamos que dejó. El
    worktree es una carpeta real en el tmp; los repos contra los que se
    valida la evidencia son directorios reales también."""

    def __init__(self, ideas=None, status_extra=""):
        # None = el agente no deja ideas.json.
        self.ideas = ideas
        self.status_extra = status_extra
        self.llamadas = []
        self.worktrees = []

    def _escribir_salida(self, prompt, worktree):
        if "IDEAS SCOUT" in prompt and self.ideas is not None:
            Path(worktree, "ideas.json").write_text(
                json.dumps({"ideas": self.ideas}, ensure_ascii=False),
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
                self.worktrees.append(wt)
                return (True, "")
            if "status" in args:
                if self.worktrees and "-C" in args and \
                        args[args.index("-C") + 1] == self.worktrees[-1]:
                    wt = Path(self.worktrees[-1])
                    salida = self.status_extra
                    if (wt / "ideas.json").is_file():
                        salida = ("?? ideas.json\n" + salida).strip()
                    return (True, (salida + "\n") if salida else "")
                return (True, "")   # los repos de verdad quedan limpios
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
        return (True, "")


def leer_log(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l]


class EscenarioIdeas(unittest.TestCase):
    """Un `Ideas` contra el mundo scripteado, con tmp real para el
    worktree y los repos (la evidencia se lee del disco de verdad)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.repos = {}
        for nombre, archivos in (("koku", REPO_KOKU), ("ERP-IphoneUp", REPO_ERP)):
            base = self.d / "repos" / nombre
            base.mkdir(parents=True)
            for rel, lineas in archivos.items():
                p = base / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("\n".join(lineas) + "\n", encoding="utf-8")
            self.repos[nombre] = str(base)
        self.repos_list = [(n, p) for n, p in self.repos.items()]
        self.repo_path = self.repos_list[0][1]
        self.vault = self.d / "vault"
        self.log_path = self.d / "events.jsonl"

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
        self.tmp.cleanup()

    def correr(self, mundo, maximo=MAX_IDEAS_DEFAULT, conocidas=(),
               escribir=None):
        spec = DispatchSpec(contexto="t", kind="pi", model="qwen/qwen3.8-27b",
                            watchdog_kill_min=45)
        log = EventLog(self.log_path, "t")
        ideas = Ideas(spec, log, run_cmd=mundo.cmd, escribir_nota=escribir)
        res = ideas.ideas_corrida(self.repos_list[0][0], self.repo_path,
                                  self.repos_list, str(self.vault),
                                  maximo, list(conocidas))
        return res, leer_log(self.log_path)

    def ideas_escritas(self):
        dir = self.vault / "ideas"
        return sorted(str(f) for f in dir.glob("*.md")) if dir.is_dir() else []


# --------------------------------------------------------------------- puros
class Puros(unittest.TestCase):
    def test_ritmo_semanal(self):
        ok, _ = debe_correr("", "2026-08-27T02:00:00Z")
        self.assertTrue(ok, "primera corrida: sin sello")
        ok, motivo = debe_correr("2026-08-27T02:00:00Z", "2026-08-29T02:00:00Z")
        self.assertFalse(ok, "a 2 días no corre: es semanal, no de cada noche")
        self.assertIn("--forzar", motivo)
        ok, _ = debe_correr("2026-08-27T02:00:00Z", "2026-09-03T02:00:00Z")
        self.assertTrue(ok, "a 7 días corre")
        ok, _ = debe_correr("no-entendible", "2026-08-27T02:00:00Z")
        self.assertTrue(ok, "un sello viejo ilegible no congela el ritmo")

    def test_sello(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "estado" / "harness" / "ideas"
            self.assertEqual(leer_ultima_corrida(p), "")
            guardar_ultima_corrida(p, "2026-08-27T02:00:00Z")
            self.assertEqual(leer_ultima_corrida(p), "2026-08-27T02:00:00Z")

    def test_duplicada_por_titulo(self):
        conocidas = [("El Print-Agent de KOKU sin nube",
                      (("koku", "src/print-agent.ts", 2),))]
        self.assertEqual(es_duplicado_idea(
            "El print-agent de koku sin nube",
            [("koku", "src/print-agent.ts", 2)], conocidas),
            "El Print-Agent de KOKU sin nube")

    def test_duplicada_por_lo_que_cita(self):
        """Una nota que ya citó esa referencia: la idea es lo mismo
        vestido de otra forma."""
        conocidas = [("Otra redacción", (("koku", "src/print-agent.ts", 2),))]
        self.assertEqual(es_duplicado_idea(
            "Nueva idea", [("koku", "src/print-agent.ts", 2)], conocidas),
            "Otra redacción")
        self.assertIsNone(es_duplicado_idea(
            "Nueva idea", [("koku", "src/print-agent.ts", 3)], conocidas))

    def test_validar_sin_campos(self):
        nombres = {"koku"}
        leer = lambda r, a, l: "x"
        for campo in ("patron", "rubro", "paga", "version_minima",
                      "precio", "riesgo"):
            i = _idea()
            del i[campo]
            ok, motivo = validar_idea(i, nombres, leer)
            self.assertFalse(ok, campo)
            self.assertIn(campo, motivo)
        i = _idea(falta_dias="un esfuerzo")
        ok, motivo = validar_idea(i, nombres, leer)
        self.assertFalse(ok)
        self.assertIn("días", motivo)

    def test_validar_evidencia_que_no_se_puede_leer(self):
        tablas = {"koku": self._tmp_repo()}

        def leer(repo, archivo, linea):
            p = Path(tablas[repo]) / archivo
            if not p.is_file():
                return None
            lineas = p.read_text().splitlines()
            return lineas[linea - 1] if linea <= len(lineas) else None

        ok, motivo = validar_idea(_idea(linea=99), {"koku"}, leer)
        self.assertFalse(ok)
        self.assertIn("no se puede leer", motivo)
        ok, motivo = validar_idea(_idea(repo="otro"), {"koku"}, leer)
        self.assertFalse(ok)
        self.assertIn("inexistente", motivo)
        ok, _ = validar_idea(_idea(linea=1), {"koku"}, leer)
        self.assertTrue(ok)

    def _tmp_repo(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        p = Path(d) / "src"
        p.mkdir()
        (p / "print-agent.ts").write_text(
            "a\nb\nc\n", encoding="utf-8")
        return d

    def test_nota_trae_los_campos_del_ticket(self):
        nota = nota_idea(_idea(), "2026-08-27")
        for seccion in ("## El patrón", "## Evidencia", "## A qué rubro "
                        "le sirve", "## Quién firma el cheque", "## Qué "
                        "falta", "## La versión más chica que se puede "
                        "cobrar", "## Precio", "## Por qué podría no "
                        "funcionar"):
            self.assertIn(seccion, nota)
        self.assertIn("- `koku`:`src/print-agent.ts`:2", nota)
        self.assertIn("15 días de trabajo", nota)

    def test_leer_ideas_vault(self):
        with tempfile.TemporaryDirectory() as d:
            dir = Path(d)
            (dir / "2026-08-27-print-agent.md").write_text(
                "# El print-agent de koku sin nube\n\n"
                "## Evidencia\n\n- `koku`:`src/print-agent.ts`:2\n",
                encoding="utf-8")
            (dir / "otra.txt").write_text("# no es markdown\n")
            self.assertEqual(leer_ideas_vault(dir), [
                ("El print-agent de koku sin nube",
                 (("koku", "src/print-agent.ts", 2),))])
            self.assertEqual(leer_ideas_vault(Path(d) / "no-hay"), [])

    def test_nota_escribir_no_pisa(self):
        with tempfile.TemporaryDirectory() as d:
            i = _idea()
            p1 = nota_escribir(i, "2026-08-27", d)
            p2 = nota_escribir(_idea(), "2026-08-27", d)
            self.assertNotEqual(p1, p2, "mismo slug del día: se numera")
            self.assertTrue(Path(p2).name.endswith("-2.md"))

    def test_prompt_dice_reglas(self):
        p = prompt_ideas(3, [("koku", "/r/koku")],
                         [("Ya escrita", (("koku", "a.ts", 1),))])
        self.assertIn("IDEAS SCOUT", p)
        self.assertIn("/r/koku", p)
        self.assertIn("'Ya escrita'", p)
        self.assertIn("at most 3", p)
        self.assertEqual(p.count("\n"), 0, "una sola línea")

    def test_clave_no_colisiona(self):
        self.assertEqual(clave_idea("koku"), "koku#ideas")


# ---------------------------------------------------------------- la corrida
class LaCorrida(EscenarioIdeas):
    def test_repos_sin_nada_reutilizable(self):
        """Evidencia que no se puede ir a leer: no es una idea, no se
        escribe. Y el agente que no encontró nada: lista vacía, cero
        notas, y no es un error."""
        mundo = MundoIdeas(ideas=[
            _idea(titulo="Archivo que no existe", archivo="no/hay.ts",
                  linea=1),
            _idea(titulo="Línea fuera de rango", linea=99),
        ])
        res, eventos = self.correr(mundo)
        self.assertEqual(res["escribas"], 0)
        self.assertEqual(res["descartadas"], 2)
        self.assertEqual(self.ideas_escritas(), [])
        motivos = [e["cuerpo"] for e in eventos
                   if e["tipo"] == "idea" and "descartada" in e["cuerpo"]]
        self.assertTrue(any("no se puede leer" in m for m in motivos))
        self.assertFalse([e for e in eventos if e["tipo"] == "abandono"],
                         "descartar todo no es un abandono")

        mundo_vacia = MundoIdeas(ideas=[])
        res, eventos = self.correr(mundo_vacia)
        self.assertEqual(res["escribas"], 0)
        self.assertEqual(res["estado"], "hecho")
        self.assertFalse([e for e in eventos if e["tipo"] == "abandono"],
                         "cero ideas es un resultado válido")

    def test_idea_duplicada_contra_la_vault(self):
        """Lo que ya hay en la vault no se vuelve a escribir: ni por
        título ni por la referencia que cita."""
        dir = self.vault / "ideas"
        dir.mkdir(parents=True)
        (dir / "2026-08-20-print-agent.md").write_text(
            "# El print-agent de koku sin nube\n\n"
            "## Evidencia\n\n- `koku`:`src/print-agent.ts`:2\n",
            encoding="utf-8")
        mundo = MundoIdeas(ideas=[
            _idea(titulo="  EL PRINT-AGENT DE KOKU SIN NUBE "),
            _idea(titulo="Nueva redacción", linea=2),
            _idea(titulo="La idea de la caja", repo="ERP-IphoneUp",
                  archivo="app/caja.py", linea=4,
                  rubro="Ferreterías de La Florida"),
        ])
        res, eventos = self.correr(mundo,
                                   conocidas=leer_ideas_vault(
                                       self.vault / "ideas"))
        self.assertEqual(res["escribas"], 1)
        self.assertEqual(res["descartadas"], 2)
        escritas = self.ideas_escritas()
        self.assertEqual(len(escritas), 1)
        self.assertIn("caja", Path(escritas[0]).name)
        dups = [e["cuerpo"] for e in eventos
                if e["tipo"] == "idea" and "duplicada" in e["cuerpo"]]
        self.assertEqual(len(dups), 2)

    def test_tope_de_notas_por_corrida(self):
        """Más ideas que el tope no se escriben todas: lo que se queda
        fuera se anota, no se omite en silencio."""
        mundo = MundoIdeas(ideas=[
            _idea(titulo="Impresión LAN", linea=1),
            _idea(titulo="Cierre de caja", repo="ERP-IphoneUp",
                  archivo="app/caja.py", linea=5,
                  rubro="Farmacias de barrio"),
            _idea(titulo="Inventario serializado", repo="ERP-IphoneUp",
                  archivo="app/caja.py", linea=3,
                  rubro="Llaveros y celulares usados"),
        ])
        res, eventos = self.correr(mundo, maximo=2)
        self.assertEqual(res["escribas"], 2)
        self.assertEqual(res["descartadas"], 1)
        tope = [e["cuerpo"] for e in eventos
                if e["tipo"] == "idea" and "tope" in e["cuerpo"]]
        self.assertEqual(len(tope), 1)
        self.assertIn("Inventario serializado", tope[0])

    def test_idea_valida_se_escribe_en_la_vault(self):
        mundo = MundoIdeas(ideas=[_idea()])
        res, eventos = self.correr(mundo)
        self.assertEqual(res["escribas"], 1)
        escritas = self.ideas_escritas()
        self.assertEqual(len(escritas), 1)
        nota = Path(escritas[0]).read_text(encoding="utf-8")
        self.assertIn("# El print-agent de koku sin nube", nota)
        self.assertIn("Carnicerías de barrio", nota)
        self.assertIn("CLP 800 mil", nota)
        notas = [e for e in eventos if e["tipo"] == "nota"]
        self.assertEqual(len(notas), 1)
        self.assertTrue(notas[0]["cuerpo"].endswith(".md"))
        self.assertIn("/ideas/", notas[0]["cuerpo"])

    def test_el_arbol_no_puede_ensuciarse_fuera_de_la_salida(self):
        """El agente no toca código: un archivo modificado junto a la
        salida es abandono, no trabajo."""
        mundo = MundoIdeas(ideas=[_idea()], status_extra=" M src/a.ts")
        res, eventos = self.correr(mundo)
        self.assertEqual(res["escribas"], 0)
        self.assertTrue(any("toco archivos" in e["cuerpo"]
                            for e in eventos if e["tipo"] == "abandono"))

    def test_el_agente_sin_salida_no_deja_notas(self):
        mundo = MundoIdeas(ideas=None)
        res, eventos = self.correr(mundo)
        self.assertEqual(res["escribas"], 0)
        self.assertEqual(res["estado"], "abandonado")
        self.assertEqual(self.ideas_escritas(), [])

    def test_repos_ensuciados_despues_de_la_corrida(self):
        """El agente leyó los checkouts de verdad: si alguno quedó
        sucio, la corrida lo anota."""
        mundo = MundoIdeas(ideas=[_idea()])

        def cmd_sucio(args, cwd=None, timeout=30):
            if args[0] == "git" and "-C" in args and \
                    args[args.index("-C") + 1] == self.repos["koku"]:
                if "status" in args:
                    return (True, " M src/print-agent.ts\n")
            return mundo.cmd(args, cwd=cwd, timeout=timeout)
        spec = DispatchSpec(contexto="t", kind="pi", model="qwen/qwen3.8-27b")
        log = EventLog(self.log_path, "t")
        ideas = Ideas(spec, log, run_cmd=cmd_sucio)
        res = ideas.ideas_corrida("koku", self.repo_path, self.repos_list,
                                  str(self.vault), MAX_IDEAS_DEFAULT, [])
        eventos = leer_log(self.log_path)
        self.assertEqual(res["escribas"], 1)
        self.assertTrue(any("quedó con cambios sin commitear" in e["cuerpo"]
                            for e in eventos if e["tipo"] == "ideas"))


# --------------------------------------------------------------------- CLI
class ElCli(unittest.TestCase):
    """El modo sin adaptadores: `ideas` no existe offline, igual que
    `run` y `proponer` — un dispatcher sin mundo no es un dispatcher."""

    CONTEXTO = json.dumps({
        "default_context": "h",
        "contexts": {
            "h": {"tracker": {"kind": "github"},
                  "repos": {"root": "/no/existe", "paths": []},
                  "autonomy": "frontier",
                  "budget": {"polarity": "remaining", "provider": "none"},
                  "vault": "/no/existe/vault",
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
        p = self._run(["ideas"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("herdr", p.stderr)

    def test_max_ideas_inválido(self):
        p = self._run(["ideas", "--max-ideas", "0"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("--max-ideas", p.stderr)


if __name__ == "__main__":
    unittest.main()
