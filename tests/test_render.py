"""Tests de `render()`: que dibuje exactamente la pantalla de siempre.

`tests/golden/status.txt` se capturó de la salida del CLI antes de partirlo en
snapshot y render. Si este test cae, la pantalla cambió: o el cambio es
deliberado y se regenera el golden, o es un bug.
"""

import re
import unittest

import support

from harness.render import OFFLINE_HINT, render
from harness.snapshot import Agents, Budget, Context, Snapshot, snapshot
from harness.summary import PrAbierto, Resumen

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


class TestReadinessRamaPorDefecto(unittest.TestCase):
    """El working tree en una rama distinta a la por defecto se nota en la
    tabla de readiness, y el fallback al working tree también."""

    def linea(self, **over):
        """La línea de readiness de agent-harness (en el golden, ticket/3)."""
        crudo = support.golden_raw("personal")
        crudo["contexts"][0]["repos"][0].update(over)
        for l in render(snapshot(crudo), color=False).splitlines():
            if l.startswith("   agent-harness ") and "✓" in l:
                return l
        self.fail("falta la línea de readiness de agent-harness")

    def test_rama_distinta_de_la_por_defecto_se_nota(self):
        linea = self.linea(default_branch="main", readiness_source="default-branch")
        self.assertIn("(rama: ticket/3, readiness: main)", linea)

    def test_en_la_rama_por_defecto_no_hay_nota(self):
        linea = self.linea(branch="main", default_branch="main",
                           readiness_source="default-branch")
        self.assertNotIn("readiness:", linea)

    def test_fallback_al_working_tree_se_nota(self):
        linea = self.linea(default_branch="main", readiness_source="working-tree")
        self.assertIn("(readiness: working tree)", linea)

    def test_sin_rama_por_defecto_no_hay_nota(self):
        """Repo sin remote: sigue funcionando como hoy, sin anotaciones."""
        linea = self.linea(default_branch=None, readiness_source="working-tree")
        self.assertNotIn("readiness", linea)


class TestResumen(unittest.TestCase):
    """El resumen de la mañana (ticket #7): arriba de todo, con lo accionable abajo."""

    def pantalla(self, resumen):
        return render(SNAP, resumen=resumen, color=False)

    def test_el_resumen_queda_arriba_de_todo(self):
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    tickets=[Ticket(contexto="personal", ref="ticket/3",
                                    detalle="PR #19 abierto (gate verde)")])
        salida = self.pantalla(r)
        self.assertLess(salida.index("Resumen"), salida.index("Agentes"))
        self.assertIn("Trabajo", salida)  # lo accionable sigue ahí, abajo

    def test_log_vacio_dice_que_no_paso_nada(self):
        salida = self.pantalla(Resumen(estado="ok", desde="2026-08-21T23:00:00Z"))
        self.assertIn("no pasó nada", salida)
        self.assertIn("Agentes", salida)  # el resto de la pantalla se dibuja igual

    def test_sin_marca_es_desde_el_principio(self):
        self.assertIn("desde el principio", self.pantalla(Resumen(estado="ok")))

    def test_un_solo_evento(self):
        from harness.summary import Trabado
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    trabados=[Trabado(contexto="personal", ref="ticket/11",
                                      motivo="agente bloqueado (aprobacion o pregunta)")])
        salida = self.pantalla(r)
        self.assertIn("1 trabado(s)", salida)
        self.assertIn("ticket/11", salida)
        self.assertNotIn("tickets con PR", salida)
        self.assertNotIn("esperando review", salida)

    def test_secciones_completas(self):
        from harness.summary import Ticket, Trabado
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    tickets=[Ticket(contexto="personal", ref="ticket/3",
                                    detalle="PR #19 abierto (gate verde)")],
                    trabados=[Trabado(contexto="personal", ref="ticket/11",
                                      motivo="gate rojo en el worktree: sin PR")],
                    prs=[PrAbierto(repo="agent-harness", number=19, title="Fix")],
                    costo=0.77)
        salida = self.pantalla(r)
        self.assertIn("1 tickets con PR abierto (gate verde)", salida)
        self.assertIn("1 trabado(s)", salida)
        self.assertIn("gate rojo en el worktree: sin PR", salida)
        self.assertIn("1 PR abierto(s) esperando review", salida)
        self.assertIn("· agent-harness #19 Fix", salida)
        self.assertIn("costo del período: $0.77", salida)

    def test_offline(self):
        salida = self.pantalla(Resumen(estado="offline"))
        self.assertIn("Resumen", salida)
        self.assertIn(OFFLINE_HINT, salida)

    def test_mergeado_y_cerrado_son_grupos_distintos(self):
        """Un PR mergeado y uno cerrado sin mergear son resultados opuestos:
        no pueden compartir línea (ticket #55)."""
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z", tickets=[
            Ticket(contexto="personal", ref="ticket/3",
                  detalle="PR #19 abierto (gate verde)", estado="mergeado", en_vivo=True),
            Ticket(contexto="personal", ref="ticket/9",
                  detalle="PR #20 abierto (gate verde)", estado="cerrado", en_vivo=True),
        ])
        salida = self.pantalla(r)
        self.assertIn("1 tickets mergeados", salida)
        self.assertIn("1 tickets cerrados sin mergear", salida)
        self.assertNotIn("tickets con PR abierto (gate verde)", salida)

    def test_sin_confirmar_en_vivo_se_marca_como_tal(self):
        """Sin red, el ticket se muestra con lo que dice el log, pero
        distinguido de un estado confirmado en vivo (ticket #55)."""
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z", tickets=[
            Ticket(contexto="personal", ref="ticket/3",
                  detalle="PR #19 abierto (gate verde)"),
        ])
        salida = self.pantalla(r)
        self.assertIn("según el log", salida)

    def test_confirmado_en_vivo_no_lleva_la_marca(self):
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z", tickets=[
            Ticket(contexto="personal", ref="ticket/3",
                  detalle="PR #19 abierto (gate verde)", en_vivo=True),
        ])
        salida = self.pantalla(r)
        self.assertNotIn("según el log", salida)


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
