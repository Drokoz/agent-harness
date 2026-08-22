"""Tests de `render()`: que dibuje exactamente la pantalla de siempre.

`tests/golden/status.txt` se capturó de la salida del CLI antes de partirlo en
snapshot y render. Si este test cae, la pantalla cambió: o el cambio es
deliberado y se regenera el golden, o es un bug.
"""

import re
import unittest

import support

from harness.render import render
from harness.snapshot import Agents, Budget, Context, Snapshot, snapshot

SNAP = snapshot(support.golden_raw("personal"))
TODOS = snapshot(support.golden_raw())


def golden(name):
    return (support.GOLDEN / name).read_text()


class TestGolden(unittest.TestCase):
    def test_pantalla_completa(self):
        self.assertEqual(render(SNAP, color=False), golden("status.txt"))

    def test_pantalla_quiet(self):
        """-q se saltea la tabla de readiness y nada más."""
        self.assertEqual(render(SNAP, quiet=True, color=False), golden("status_quiet.txt"))

    def test_pantalla_de_todos_los_contextos(self):
        """`--all`: los dos contextos, uno abajo del otro, con un solo bloque de agentes."""
        salida = render(TODOS, color=False)
        self.assertEqual(salida, golden("status_all.txt"))
        self.assertEqual(salida.count("Agentes"), 1)
        self.assertEqual(salida.count("▸ personal"), 1)
        self.assertEqual(salida.count("▸ trabajo"), 1)

    def test_un_contexto_no_muestra_el_otro(self):
        salida = render(SNAP, color=False)
        self.assertNotIn("▸ trabajo", salida)
        self.assertNotIn("groceries-wl", salida)

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


class TestPolaridad(unittest.TestCase):
    """El presupuesto se lee distinto según el contexto: que no se acabe / que no sobre."""

    def linea(self, **over):
        b = Budget(state="ok", total=100.0, **over)
        b.left = b.total - b.used
        b.ratio = b.used / b.total
        snap = Snapshot(offline=False, agents=Agents(state="empty"),
                        contexts=[Context(name="x", tracker="github", autonomy="frontier",
                                          run="local", vault=None, budget=b)])
        return [l for l in render(snap, color=False).splitlines()
                if "$" in l or "presupuesto" in l][0]

    def test_remaining_lidera_con_lo_que_queda(self):
        linea = self.linea(polarity="remaining", provider="openrouter", used=30.0,
                           tickets=291)
        self.assertIn("$70.00 de $100.00 disponibles", linea)
        self.assertIn("≈291 tickets", linea)

    def test_spent_lidera_con_lo_que_se_uso(self):
        linea = self.linea(polarity="spent", provider="manual", used=30.0)
        self.assertIn("$30.00 de $100.00 usados", linea)
        self.assertIn("30% del presupuesto", linea)

    def test_los_colores_de_la_barra_se_invierten(self):
        """Gastar el 90% es rojo en personal y verde en trabajo: son objetivos opuestos."""
        from harness.render import COLOR, bar
        casi_todo = bar(90.0, 100.0, COLOR, "remaining")
        self.assertIn(COLOR.red, casi_todo)
        self.assertIn(COLOR.grn, bar(90.0, 100.0, COLOR, "spent"))
        self.assertIn(COLOR.red, bar(10.0, 100.0, COLOR, "spent"))
        self.assertIn(COLOR.grn, bar(10.0, 100.0, COLOR, "remaining"))

    def test_sin_total_no_hay_barra(self):
        from harness.render import PLAIN, bar
        self.assertEqual(bar(0.0, 0.0, PLAIN), "")

    def test_presupuesto_sin_declarar(self):
        snap = Snapshot(offline=False, agents=Agents(state="empty"),
                        contexts=[Context(name="x", tracker="github", autonomy="manual",
                                          run="ssh", vault=None,
                                          budget=Budget(state="unset"))])
        self.assertIn("no declara presupuesto", render(snap, color=False))


class TestDegradado(unittest.TestCase):
    """La pantalla se dibuja igual con adaptadores caídos: sólo falta esa sección."""

    def test_todo_caido(self):
        crudo = support.golden_raw("personal")
        crudo["contexts"][0]["budget"]["credits"] = None
        crudo["agents"] = None
        for r in crudo["contexts"][0]["repos"]:
            r["issues"] = r["prs"] = None
        salida = render(snapshot(crudo), color=False)
        self.assertIn("sin datos (¿key en ~/.pi/agent/models.json?)", salida)
        self.assertIn("fuera de herdr", salida)
        self.assertIn("nada pendiente", salida)
        self.assertIn("Listo para el harness", salida)
        self.assertIn("agent-harness", salida)  # la tabla de readiness sigue entera

    def test_gh_caido_en_un_repo_no_borra_el_otro(self):
        crudo = support.golden_raw("personal")
        crudo["contexts"][0]["repos"][0]["issues"] = None
        crudo["contexts"][0]["repos"][0]["prs"] = None
        salida = render(snapshot(crudo), color=False)
        self.assertNotIn("#12 CI: correr el gate", salida)  # la sección caída
        self.assertIn("#7 Cerrar caja", salida)             # el repo sano, intacto
        self.assertIn("OpenRouter", salida)

    def test_contexto_vacio(self):
        vacio = Snapshot(offline=False, agents=Agents(state="outside"),
                         contexts=[Context(name="x", tracker="github", autonomy="frontier",
                                           run="local", vault=None,
                                           budget=Budget(state="missing"))])
        self.assertIn("nada pendiente", render(vacio, color=False))

    def test_sin_contextos(self):
        pelado = Snapshot(offline=False, agents=Agents(state="outside"), contexts=[])
        self.assertIn("ningún contexto configurado", render(pelado, color=False))

    def test_tracker_sin_adaptador_no_se_confunde_con_nada_pendiente(self):
        salida = render(snapshot(support.golden_raw("trabajo")), color=False)
        self.assertIn("tracker jira: todavía sin adaptador", salida)
        self.assertNotIn("nada pendiente", salida)

    def test_offline(self):
        crudo = support.golden_raw("personal")
        crudo["offline"] = True
        salida = render(snapshot(crudo), color=False)
        self.assertEqual(salida.count("sin adaptadores (HARNESS_OFFLINE=1)"), 2)

    def test_offline_con_varios_contextos(self):
        """Los agentes se dicen una vez; el presupuesto, uno por contexto."""
        crudo = support.golden_raw()
        crudo["offline"] = True
        salida = render(snapshot(crudo), color=False)
        self.assertEqual(salida.count("sin adaptadores (HARNESS_OFFLINE=1)"), 3)


if __name__ == "__main__":
    unittest.main()
