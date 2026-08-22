"""Vista transversal de decisiones: ADRs por repo + notas de la vault.

Cubre la lógica pura (`harness.decisions`) y el disco (`adr_files`,
`vault_decision_files`). El cableado de punta a punta vive en test_cli.py.
"""

import tempfile
import unittest
from pathlib import Path

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
