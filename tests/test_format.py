"""Formato mínimo, sin dependencias: sin tabuladores, sin espacio al final de
línea y un solo newline al final del archivo. Aplica a los archivos Python
del repo (incluido `bin/harness`)."""

import unittest

from test_compat import python_files


class TestFormat(unittest.TestCase):
    def test_blanqueos_limpios(self):
        for p in python_files():
            text = p.read_text()
            with self.subTest(file=str(p)):
                for i, line in enumerate(text.splitlines(), 1):
                    self.assertEqual(line, line.rstrip(),
                                     f"línea {i}: espacio al final")
                    self.assertNotIn("\t", line, f"línea {i}: tabulador")
                self.assertTrue(text.endswith("\n"), "no termina en newline")
                self.assertFalse(text.endswith("\n\n"), "newline doble al final")
