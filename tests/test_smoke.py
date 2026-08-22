"""Test de humo: el CLI `harness` carga, sus funciones puras funcionan y
`harness status` corre de punta a punta en modo sin adaptadores (sin red).

`bin/harness` no tiene extensión .py ni es un paquete, así que se carga
por su ruta de archivo, no por import normal.
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "bin" / "harness"


def load_harness():
    # Sin extensión .py: spec_from_file_location necesita el loader explícito.
    loader = SourceFileLoader("harness_cli", str(HARNESS))
    spec = importlib.util.spec_from_loader("harness_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_harness_offline():
    """Carga el CLI con HARNESS_OFFLINE=1 (el flag se lee al importar)."""
    old = os.environ.get("HARNESS_OFFLINE")
    os.environ["HARNESS_OFFLINE"] = "1"
    try:
        return load_harness()
    finally:
        if old is None:
            del os.environ["HARNESS_OFFLINE"]
        else:
            os.environ["HARNESS_OFFLINE"] = old


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

    def test_status_offline_no_toca_adaptadores(self):
        """En modo offline, main() no llama a gh ni herdr ni abre sockets."""
        mod = load_harness_offline()
        calls = []

        def fake_run(args, cwd=None, timeout=30):
            calls.append(args[0] if args else None)
            if args and args[0] == "git" and "remote" in args:
                return True, "https://github.com/Drokoz/agent-harness.git"
            return False, ""

        buf = io.StringIO()
        with mock.patch.object(mod, "run", fake_run), \
                mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("llamada de red en modo offline")), \
                mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                mock.patch.object(sys, "argv", ["harness", "status"]), \
                contextlib.redirect_stdout(buf):
            mod.main()
        self.assertTrue(all(c == "git" for c in calls),
                        f"adaptador externo en modo offline: {sorted(set(calls))}")
        self.assertIn("sin adaptadores", buf.getvalue())

    def test_status_offline_punta_a_punta(self):
        """El script corre completo sin red ni credenciales (HOME vacía)."""
        env = dict(os.environ)
        env["HARNESS_OFFLINE"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            env["HOME"] = tmp
            p = subprocess.run([str(HARNESS), "status"], env=env,
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("sin adaptadores", p.stdout)
