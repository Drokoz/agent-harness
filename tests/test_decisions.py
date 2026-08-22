"""Vista transversal de decisiones: ADRs por repo + notas de la vault.

Cubre la lógica pura (`harness.decisions`), el disco (`adr_files`,
`vault_decision_files`) y el cableado de punta a punta del comando
`harness decisions`.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import adapters

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "bin" / "harness"
from harness.decisions import (adr_identity, as_list, decisions_for, note_identity,
                               render_decisions)


class TestIdentities(unittest.TestCase):
    def test_adr_con_numero_y_h1(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "0001-event-sourced-orders.md")
            p.write_text("# Event-sourced orders\n\nCuerpo.\n")
            self.assertEqual(adr_identity(p), ("0001", "Event-sourced orders"))

    def test_adr_sin_h1_cae_al_nombre(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "0002-postgres-for-write-model.md")
            p.write_text("Cuerpo sin título.\n")
            self.assertEqual(adr_identity(p),
                             ("0002", "postgres for write model"))

    def test_adr_sin_numero(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "legacy-decision.md")
            p.write_text("Sin nada.\n")
            self.assertEqual(adr_identity(p), ("legacy-decision", "legacy decision"))

    def test_nota_con_fecha_y_h1(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "2026-08-21-cambiar-de-banco.md")
            p.write_text("# Cambiar de banco\n")
            self.assertEqual(note_identity(p),
                             ("2026-08-21-cambiar-de-banco", "Cambiar de banco"))

    def test_nota_sin_h1_mantiene_el_nombre_entero(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "2026-09-01-otra-nota.md")
            p.write_text("Sin título.\n")
            self.assertEqual(note_identity(p),
                             ("2026-09-01-otra-nota", "2026-09-01-otra-nota"))

    def test_archivo_inlegible_no_levanta(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(adr_identity(Path(tmp, "no-existe.md")),
                             ("no-existe", "no existe"))


class TestDecisionsFor(unittest.TestCase):
    def test_adrs_y_notas_juntos(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            adr = t / "a.md"
            nota = t / "b.md"
            adr.write_text("# Uno\n")
            nota.write_text("# Nota\n")
            ds = decisions_for("personal", [("koku", [adr])], [nota])
        self.assertEqual(len(ds), 2)
        self.assertEqual((ds[0].source, ds[0].id, ds[0].title),
                         ("repo:koku", "a", "Uno"))
        self.assertEqual((ds[1].source, ds[1].id, ds[1].title),
                         ("vault:decisiones", "b", "Nota"))
        self.assertEqual(ds[0].context, "personal")

    def test_contexto_sin_vault_no_rompe(self):
        """AC: un contexto sin vault sólo muestra sus ADRs."""
        with tempfile.TemporaryDirectory() as tmp:
            adr = Path(tmp, "0001-x.md")
            adr.write_text("# X\n")
            ds = decisions_for("trabajo", [("groceries", [adr])], None)
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0].source, "repo:groceries")

    def test_contexto_sin_nada(self):
        self.assertEqual(decisions_for("vacio", [], None), [])


class TestAsList(unittest.TestCase):
    def test_forma_json(self):
        self.assertEqual(as_list(decisions_for("vacio", [], None)), [])
        with tempfile.TemporaryDirectory() as tmp:
            adr = Path(tmp, "0001-y.md")
            adr.write_text("# Y\n")
            [d] = decisions_for("personal", [("koku", [adr])], None)
        self.assertEqual(as_list([d]), [{
            "context": "personal", "source": "repo:koku",
            "id": "0001", "title": "Y", "path": str(adr),
        }])


class TestRender(unittest.TestCase):
    def test_fuente_de_cada_resultado_se_ve(self):
        """AC: cada resultado dice qué repo o qué carpeta de la vault."""
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            adr = t / "0001-x.md"
            nota = t / "2026-08-21-y.md"
            adr.write_text("# X\n")
            nota.write_text("# Y\n")
            ds = decisions_for("personal", [("koku", [adr])], [nota])
            texto = render_decisions([("personal", ds)], color=False)
        self.assertIn("koku/docs/adr", texto)
        self.assertIn("vault/decisiones", texto)
        self.assertIn("0001", texto)
        self.assertIn("X", texto)
        self.assertIn("2026-08-21-y", texto)
        self.assertIn("Y", texto)

    def test_sin_tty_sin_ansi(self):
        texto = render_decisions([("vacio", [])], color=False)
        self.assertNotIn("\033", texto)
        self.assertIn("sin decisiones todavía", texto)


class TestAdapters(unittest.TestCase):
    def test_adr_files_ordenados(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "docs" / "adr").mkdir(parents=True)
            (t / "docs" / "adr" / "0002-b.md").write_text("x")
            (t / "docs" / "adr" / "0001-a.md").write_text("x")
            (t / "docs" / "adr" / "README.txt").write_text("x")
            self.assertEqual(
                [f.name for f in adapters.adr_files(t)],
                ["0001-a.md", "0002-b.md"])

    def test_adr_files_sin_carpeta(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(adapters.adr_files(Path(tmp)), [])

    def test_memoria_de_claude_no_entra(self):
        """AC: la memoria de Claude no se toca ni se fusiona."""
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "docs" / "adr").mkdir(parents=True)
            (t / "CLAUDE.md").write_text("# Memoria de Claude\n")
            (t / "AGENTS.md").write_text("cosas")
            (t / "docs" / "adr" / "0001-a.md").write_text("x")
            self.assertEqual([f.name for f in adapters.adr_files(t)], ["0001-a.md"])

    def test_vault_decision_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "decisiones").mkdir()
            (t / "diario").mkdir()  # otra carpeta de la taxonomía: no entra
            (t / "decisiones" / "b.md").write_text("x")
            (t / "decisiones" / "a.md").write_text("x")
            (t / "diario" / "c.md").write_text("x")
            self.assertEqual(
                [f.name for f in adapters.vault_decision_files(str(t))],
                ["a.md", "b.md"])

    def test_vault_sin_declarar_ni_sin_carpeta(self):
        self.assertEqual(adapters.vault_decision_files(None), [])
        self.assertEqual(adapters.vault_decision_files("~/no-existe/vault"), [])


class TestCli(unittest.TestCase):
    """Punta a punta: el comando `harness decisions` sobre un escenario en disco."""

    def _escenario(self, tmp):
        t = Path(tmp)
        koku = t / "koku"
        (koku / "docs" / "adr").mkdir(parents=True)
        (koku / "CLAUDE.md").write_text("# Memoria de Claude\n")
        (koku / "docs" / "adr" / "0003-almacen-event-sourced.md").write_text(
            "# Almacen event-sourced\n")
        gro = t / "groceries-wl"
        (gro / "docs" / "adr").mkdir(parents=True)
        (gro / "docs" / "adr" / "0001-kiosco.md").write_text("# Kiosco\n")
        vault = t / "vault-personal"
        (vault / "decisiones").mkdir(parents=True)
        (vault / "diario").mkdir(parents=True)
        (vault / "decisiones" / "2026-08-21-banco.md").write_text("# De banco\n")
        (vault / "diario" / "hoy.md").write_text("# Hoy\n")
        return {
            "default_context": "personal",
            "contexts": {
                "personal": {
                    "tracker": {"kind": "github"},
                    "repos": {"root": str(t), "paths": ["koku"]},
                    "autonomy": "frontier",
                    "budget": {"polarity": "remaining", "provider": "none"},
                    "vault": str(vault),
                    "run": {"kind": "local"},
                },
                "trabajo": {  # sin vault: no debe romper
                    "tracker": {"kind": "jira", "url": "https://x.atlassian.net",
                                "project": "GRO"},
                    "repos": {"root": str(t), "paths": ["groceries-wl"]},
                    "autonomy": "manual",
                    "budget": {"polarity": "spent", "provider": "manual",
                               "total": 100.0, "used": 10.0},
                    "run": {"kind": "ssh", "host": "wl@localhost"},
                },
            },
        }

    def _correr(self, args, config):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "config.json").write_text(json.dumps(config))
            env = dict(os.environ)
            env.update(HOME=str(tmp), HARNESS_CONFIG_DIR=str(tmp),
                       HARNESS_OFFLINE="1")
            return subprocess.run([str(HARNESS), *args], env=env,
                                  capture_output=True, text=True, timeout=60)

    def test_decisions_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._correr(["decisions", "--all", "--json"], self._escenario(tmp))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        ds = json.loads(p.stdout)["decisions"]
        self.assertEqual(
            [(d["context"], d["source"], d["id"]) for d in ds],
            [("personal", "repo:koku", "0003"),
             ("personal", "vault:decisiones", "2026-08-21-banco"),
             ("trabajo", "repo:groceries-wl", "0001")])
        titulos = {d["id"]: d["title"] for d in ds}
        self.assertEqual(titulos["0003"], "Almacen event-sourced")
        self.assertEqual(titulos["2026-08-21-banco"], "De banco")
        for d in ds:  # la memoria de Claude y el diario no entran
            self.assertNotIn("CLAUDE", d["path"])
            self.assertNotIn("diario", d["path"])

    def test_decisions_pantalla(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._correr(["decisions"], self._escenario(tmp))  # sólo el default
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Decisiones", p.stdout)
        self.assertIn("koku/docs/adr", p.stdout)
        self.assertIn("vault/decisiones", p.stdout)
        self.assertNotIn("\033", p.stdout)

    def test_decisions_sin_vault_no_rompe(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._correr(["decisions", "--context", "trabajo"],
                             self._escenario(tmp))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0001", p.stdout)
        self.assertIn("Kiosco", p.stdout)
        self.assertNotIn("vault/decisiones", p.stdout)
