"""Test de humo: el CLI `harness` carga, pega las tres capas y corre de punta a
punta en modo sin adaptadores (sin red).

La lógica se testea en test_snapshot.py y el dibujo en test_render.py; acá sólo
se verifica el cableado adapters -> snapshot -> render.

`bin/harness` no tiene extensión .py ni es un paquete, así que se carga por su
ruta de archivo, no por import normal.
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
from unittest import mock

import support

from harness import adapters

ROOT = support.ROOT
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
        # el CLI es pegamento: no define lógica propia
        for capa in ("collect", "snapshot", "render"):
            self.assertTrue(callable(getattr(mod, capa)), capa)

    def test_readiness_es_la_del_snapshot(self):
        from harness.snapshot import READINESS
        self.assertEqual([k for k, _, _ in READINESS], ["gate", "skills", "context"])

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
        with mock.patch.object(adapters, "run", fake_run), \
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

    def test_sin_tty_sin_colores(self):
        """Con la salida redirigida (no es un TTY) no salen escapes ANSI."""
        env = dict(os.environ)
        env["HARNESS_OFFLINE"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            env["HOME"] = tmp
            p = subprocess.run([str(HARNESS), "status"], env=env,
                               capture_output=True, text=True, timeout=120)
        self.assertNotIn("\033", p.stdout)


if __name__ == "__main__":
    unittest.main()
