"""Tests de `render()`: que dibuje exactamente la pantalla de siempre.

`tests/golden/status.txt` se capturó de la salida del CLI antes de partirlo en
snapshot y render. Si este test cae, la pantalla cambió: o el cambio es
deliberado y se regenera el golden, o es un bug.
"""

import re
import unittest

import support

from harness.render import render
from harness.snapshot import Agents, Budget, Snapshot, snapshot

SNAP = snapshot(support.golden_raw())


def golden(name):
    return (support.GOLDEN / name).read_text()


class TestGolden(unittest.TestCase):
    def test_pantalla_completa(self):
        self.assertEqual(render(SNAP, color=False), golden("status.txt"))

    def test_pantalla_quiet(self):
        """-q se saltea la tabla de readiness y nada más."""
        self.assertEqual(render(SNAP, quiet=True, color=False), golden("status_quiet.txt"))

    def test_render_no_depende_del_orden_de_llamada(self):
        self.assertEqual(render(SNAP, color=False), render(SNAP, color=False))


class TestColor(unittest.TestCase):
    def test_sin_color_no_hay_escapes(self):
        """Cuando la salida no es un TTY el CLI pasa color=False."""
        self.assertNotIn("\033", render(SNAP, color=False))

    def test_con_color_hay_escapes(self):
        salida = render(SNAP, color=True)
        self.assertIn("\033[1m", salida)
        self.assertIn("\033[0m", salida)

    def test_el_texto_es_el_mismo_con_y_sin_color(self):
        limpio = re.sub(r"\033\[[0-9;]*m", "", render(SNAP, color=True))
        self.assertEqual(limpio, render(SNAP, color=False))


class TestDegradado(unittest.TestCase):
    """La pantalla se dibuja igual con adaptadores caídos: sólo falta esa sección."""

    def test_todo_caido(self):
        crudo = support.golden_raw()
        crudo["credits"] = None
        crudo["agents"] = None
        for r in crudo["repos"]:
            r["issues"] = r["prs"] = None
        salida = render(snapshot(crudo), color=False)
        self.assertIn("sin datos (¿key en ~/.pi/agent/models.json?)", salida)
        self.assertIn("fuera de herdr", salida)
        self.assertIn("nada pendiente", salida)
        self.assertIn("Listo para el harness", salida)
        self.assertIn("agent-harness", salida)  # la tabla de readiness sigue entera

    def test_gh_caido_en_un_repo_no_borra_el_otro(self):
        crudo = support.golden_raw()
        crudo["repos"][0]["issues"] = crudo["repos"][0]["prs"] = None
        salida = render(snapshot(crudo), color=False)
        self.assertNotIn("#12 CI: correr el gate", salida)  # la sección caída
        self.assertIn("#7 Cerrar caja", salida)             # el repo sano, intacto
        self.assertIn("OpenRouter", salida)

    def test_snapshot_vacio(self):
        vacio = Snapshot(offline=False, budget=Budget(state="missing"),
                         agents=Agents(state="outside"), repos=[])
        self.assertIn("nada pendiente", render(vacio, color=False))

    def test_offline(self):
        crudo = support.golden_raw()
        crudo["offline"] = True
        salida = render(snapshot(crudo), color=False)
        self.assertEqual(salida.count("sin adaptadores (HARNESS_OFFLINE=1)"), 2)


if __name__ == "__main__":
    unittest.main()
