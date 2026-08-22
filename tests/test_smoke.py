"""Test de humo: el CLI `harness` carga y sus funciones puras funcionan.

`bin/harness` no tiene extensión .py ni es un paquete, así que se carga
por su ruta de archivo, no por import normal.
"""

import importlib.util
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "bin" / "harness"


def load_harness():
    # Sin extensión .py: spec_from_file_location necesita el loader explícito.
    loader = SourceFileLoader("harness_cli", str(HARNESS))
    spec = importlib.util.spec_from_loader("harness_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSmoke(unittest.TestCase):
    def test_module_carga(self):
        mod = load_harness()
        self.assertTrue(callable(getattr(mod, "main")))
        self.assertEqual([k for k, _, _ in mod.READINESS], ["gate", "skills", "context"])

    def test_blockers_of(self):
        mod = load_harness()
        self.assertEqual(mod.blockers_of("Blocked by #3, #4"), {3, 4})
        self.assertEqual(mod.blockers_of("Blocked by: #12"), {12})
        self.assertEqual(mod.blockers_of("Blocked by none (can start)"), set())
        self.assertEqual(mod.blockers_of(None), set())
