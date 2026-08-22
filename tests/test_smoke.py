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
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
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


@contextlib.contextmanager
def correr(*args, config=None):
    """Corre el CLI de verdad, con HOME vacía y la config que le pasemos."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["HOME"] = tmp
        env["HARNESS_OFFLINE"] = "1"
        env["HARNESS_CONFIG_DIR"] = tmp
        if config is not None:
            (Path(tmp) / "config.json").write_text(config)
        yield subprocess.run([str(HARNESS), *args], env=env, capture_output=True,
                             text=True, timeout=120)


DOS_CONTEXTOS = json.dumps({
    "default_context": "personal",
    "contexts": {
        "personal": {"tracker": {"kind": "github"},
                     "repos": {"root": "/no/existe", "paths": []},
                     "autonomy": "frontier",
                     "budget": {"polarity": "remaining", "provider": "openrouter"},
                     "run": {"kind": "local"}},
        "trabajo": {"tracker": {"kind": "jira", "url": "https://x.atlassian.net",
                                "project": "GRO", "token": "secreto-de-jira"},
                    "repos": {"root": "/no/existe", "paths": []},
                    "autonomy": "manual",
                    "budget": {"polarity": "spent", "provider": "manual", "total": 200.0,
                               "used": 128.4},
                    "run": {"kind": "ssh", "host": "wl@localhost"}},
    },
})


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


class TestContextos(unittest.TestCase):
    def test_por_defecto_muestra_solo_el_contexto_por_defecto(self):
        with correr("status", config=DOS_CONTEXTOS) as p:
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("▸ personal", p.stdout)
            self.assertNotIn("▸ trabajo", p.stdout)

    def test_context_elige_uno(self):
        with correr("status", "--context", "trabajo", config=DOS_CONTEXTOS) as p:
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("▸ trabajo", p.stdout)
            self.assertNotIn("▸ personal", p.stdout)

    def test_all_los_muestra_todos(self):
        with correr("status", "--all", config=DOS_CONTEXTOS) as p:
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("▸ personal", p.stdout)
            self.assertIn("▸ trabajo", p.stdout)

    def test_sin_config_arranca_igual(self):
        """Sin ~/.config/harness/config.json el harness sigue saliendo 0."""
        with correr("status") as p:
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("▸ personal", p.stdout)


class TestJson(unittest.TestCase):
    def test_es_parseable_y_trae_el_snapshot_entero(self):
        with correr("status", "--json", "--all", config=DOS_CONTEXTOS) as p:
            self.assertEqual(p.returncode, 0, p.stderr)
            data = json.loads(p.stdout)
        self.assertEqual([c["name"] for c in data["contexts"]], ["personal", "trabajo"])
        self.assertEqual(data["contexts"][1]["budget"]["polarity"], "spent")
        self.assertIn("version", data)
        self.assertIn("agents", data)

    def test_no_dibuja_la_pantalla(self):
        with correr("status", "--json", config=DOS_CONTEXTOS) as p:
            self.assertNotIn("\033", p.stdout)
            self.assertNotIn("Listo para el harness", p.stdout)

    def test_no_filtra_el_token_del_tracker(self):
        with correr("status", "--json", "--all", config=DOS_CONTEXTOS) as p:
            self.assertNotIn("secreto-de-jira", p.stdout)


class TestErrores(unittest.TestCase):
    """Una config mal escrita da una línea de error, no un stack trace."""

    def esperar_error(self, *args, **kw):
        with correr(*args, **kw) as p:
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual(p.stdout, "")
            self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
            return p.stderr

    def test_contexto_que_no_existe(self):
        err = self.esperar_error("status", "--context", "laboral", config=DOS_CONTEXTOS)
        self.assertIn("laboral", err)
        self.assertIn("personal, trabajo", err)

    def test_json_roto(self):
        err = self.esperar_error("status", config="{no soy json")
        self.assertIn("JSON inválido", err)

    def test_campo_faltante(self):
        roto = json.dumps({"contexts": {"personal": {"tracker": {"kind": "github"}}}})
        err = self.esperar_error("status", config=roto)
        self.assertIn('contexto "personal"', err)
        self.assertIn('falta "repos"', err)

    def test_valor_que_no_existe(self):
        malo = json.loads(DOS_CONTEXTOS)
        malo["contexts"]["personal"]["autonomy"] = "automatico"
        err = self.esperar_error("status", config=json.dumps(malo))
        self.assertIn("frontier, manual", err)

    def test_comando_desconocido(self):
        with correr("volar") as p:
            self.assertNotEqual(p.returncode, 0)
            self.assertNotIn("Traceback", p.stderr)


class TestParser(unittest.TestCase):
    def test_defaults(self):
        args = load_harness().build_parser().parse_args([])
        self.assertEqual(args.command, "status")
        self.assertIsNone(args.context)
        self.assertFalse(args.todos or args.quiet or args.como_json)

    def test_todas_las_banderas(self):
        args = load_harness().build_parser().parse_args(
            ["status", "--context", "trabajo", "--all", "-q", "--json"])
        self.assertEqual(args.context, "trabajo")
        self.assertTrue(args.todos and args.quiet and args.como_json)


if __name__ == "__main__":
    unittest.main()
