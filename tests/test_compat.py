"""Compatibilidad: el código apunta a Python 3.9 y no usa nada fuera de la stdlib.

Son tests para que el gate los corra siempre con el resto de la suite. Un
archivo Python es cualquier *.py del repo más cualquier script con shebang
python (así entra `bin/harness`, que no tiene extensión .py).
"""

import ast
import importlib.util
import sys
import sysconfig
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_PARTS = {".git", "__pycache__", ".harness"}


def python_files():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or any(part in SKIP_PARTS for part in p.parts):
            continue
        if p.suffix == ".py":
            yield p
            continue
        try:
            with p.open("rb") as f:
                first = f.readline()
        except OSError:
            continue
        if first.startswith(b"#!") and b"python" in first:
            yield p


def imported_top_levels(source):
    """Top-level de todo lo que un fuente importa: `import a.b`, `from a.b import c`
    e `importlib.import_module("a.b")` con literal."""
    mods = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            mods.add(node.args[0].value.split(".")[0])
    return mods


def is_local(name):
    """Un módulo propio del repo (tests que se importan entre sí, etc.)."""
    for base in (ROOT, ROOT / "tests", ROOT / "bin", ROOT / "scripts"):
        if (base / f"{name}.py").is_file() or (base / name / "__init__.py").is_file():
            return True
    return False


def is_stdlib(name):
    """Un módulo es stdlib si vive en el árbol de la stdlib del intérprete actual
    (o es builtin/frozen). No lo es si vive en site-packages o no existe."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError, AttributeError):
        return False
    if spec is None:
        return False
    bases = tuple(b for b in (sysconfig.get_path("stdlib"),
                              sysconfig.get_path("platstdlib")) if b)
    origin = spec.origin
    if origin in ("built-in", "frozen"):
        return True
    if origin:
        return any(origin.startswith(base) for base in bases)
    # Namespace package: se decide por sus rutas.
    locs = spec.submodule_search_locations or []
    return any(str(loc).startswith(base) for loc in locs for base in bases)


class TestCompat(unittest.TestCase):
    def test_archivos_python_existen(self):
        self.assertGreaterEqual(
            len(list(python_files())), 1, "no se encontró ningún archivo Python"
        )

    def test_compilan_en_python_39(self):
        for p in python_files():
            with self.subTest(file=str(p)):
                compile(p.read_text(), str(p), "exec")

    def test_interprete_es_39(self):
        self.assertEqual(
            sys.version_info[:2], (3, 9),
            "el gate debe correr sobre Python 3.9 para que el chequeo de "
            "sintaxis y de stdlib valga",
        )

    def test_solo_stdlib(self):
        for p in python_files():
            source = p.read_text()
            for mod in sorted(imported_top_levels(source)):
                with self.subTest(file=str(p), module=mod):
                    self.assertTrue(is_stdlib(mod) or is_local(mod),
                                    f"import fuera de la stdlib: {mod}")
