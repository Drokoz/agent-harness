"""Tests de `render()`: que dibuje exactamente la pantalla de siempre.

`tests/golden/status.txt` se capturó de la salida del CLI antes de partirlo en
snapshot y render. Si este test cae, la pantalla cambió: o el cambio es
deliberado y se regenera el golden, o es un bug.
"""

import re
import unittest

import support

from harness.render import OFFLINE_HINT, linea_evento, render
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

    def test_bloqueos_del_guard_con_repetidos_agrupados(self):
        """(#95) El bloqueo repetido (mismo ticket, mismo motivo) aparece una
        sola vez con su conteo, y el total cuenta intentos, no líneas."""
        from harness.summary import Bloqueo
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    bloqueos=[Bloqueo(ticket="agent-harness#95",
                                      motivo="el merge es decision humana",
                                      conteo=3),
                              Bloqueo(ticket=None,
                                      motivo="reescribe historia", conteo=1)])
        salida = self.pantalla(r)
        self.assertIn("4 bloqueo(s) del guard", salida)
        self.assertIn("agent-harness#95 el merge es decision humana x3", salida)
        # Sin ticket no se inventa uno: va con "?".
        self.assertIn("· ? reescribe historia", salida)

    def test_cero_bloqueos_no_ocupan_lugar(self):
        """(#95) No gastar pantalla para decir que no pasó nada."""
        from harness.summary import Trabado
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    trabados=[Trabado(contexto="personal", ref="ticket/11",
                                      motivo="gate rojo")])
        self.assertNotIn("bloqueos del guard", self.pantalla(r))

    def test_offline(self):
        salida = self.pantalla(Resumen(estado="offline"))
        self.assertIn("Resumen", salida)
        self.assertIn(OFFLINE_HINT, salida)

    def test_cuota_de_la_semana(self):
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    cuota_semana=1000.0, cuota_semana_harness=600.0)
        salida = self.pantalla(r)
        self.assertIn("cuota de la semana", salida)
        self.assertIn("1,000", salida)
        self.assertIn("600", salida)
        self.assertIn("harness", salida)

    def test_sin_datos_de_cuota_no_muestra_la_linea(self):
        """AC #47: sin cuota la pantalla no se rompe, sigue como siempre."""
        salida = self.pantalla(Resumen(estado="ok", desde="2026-08-21T23:00:00Z"))
        self.assertNotIn("cuota de la semana", salida)

    def test_costo_por_ticket_en_las_dos_monedas(self):
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    cuota_semana=1.0, cuota_semana_harness=1.0, tickets=[
                        Ticket(contexto="personal", ref="ticket/3",
                              detalle="PR #19 abierto (gate verde)",
                              costo=0.42, cuota=12345.0)])
        salida = self.pantalla(r)
        self.assertIn("$0.42", salida)
        self.assertIn("12,345", salida)

    def test_minutos_de_agente_junto_al_costo(self):
        """AC #116: los minutos de agente se muestran al lado del costo:
        es la moneda que de verdad limita una noche."""
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    cuota_semana=1.0, cuota_semana_harness=1.0, tickets=[
                        Ticket(contexto="personal", ref="ticket/3",
                              detalle="PR #19 abierto (gate verde)",
                              costo=0.42, cuota=12345.0, minutos=320.4)])
        salida = self.pantalla(r)
        self.assertIn("320 min", salida)
        self.assertIn("$0.42", salida)

    def test_minutos_minimos_no_muestran(self):
        """Menos de un minuto no se redondea a uno: no se finge gasto de
        reloj que no hubo."""
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    cuota_semana=1.0, cuota_semana_harness=1.0, tickets=[
                        Ticket(contexto="personal", ref="ticket/3",
                              detalle="PR #19 abierto (gate verde)",
                              costo=0.42, minutos=0.4)])
        salida = self.pantalla(r)
        self.assertNotIn(" min", salida)
        self.assertIn("$0.42", salida)

    def test_sin_cuota_no_muestra_tokens_por_ticket(self):
        """Sin datos de cuota (offline), el ticket se ve como siempre: sólo
        el detalle, sin inventar una cifra de tokens."""
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z", tickets=[
            Ticket(contexto="personal", ref="ticket/3",
                  detalle="PR #19 abierto (gate verde)")])
        salida = self.pantalla(r)
        self.assertNotIn("tok", salida)

    def test_peldanos_de_un_ticket_escalado(self):
        from harness.summary import Ticket
        peldanos = [{"attempt": 1, "runner": "pi", "motivo": "gate rojo",
                    "costo": 0.05, "cuota": 0.0},
                   {"attempt": 2, "runner": "claude", "motivo": None,
                    "costo": 0.0, "cuota": 8200.0}]
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    cuota_semana=1.0, cuota_semana_harness=1.0, tickets=[
                        Ticket(contexto="personal", ref="ticket/3",
                              detalle="PR #19 abierto (gate verde)",
                              costo=0.05, cuota=8200.0, peldanos=peldanos)])
        salida = self.pantalla(r)
        # #112: el número que se ve es el de intento, no el de peldaño:
        # sin línea `peldano` en el log, el fallback es el runner.
        self.assertIn("int. 1 · pi", salida)
        self.assertIn("$0.05", salida)
        self.assertIn("int. 2 · claude", salida)
        self.assertIn("8,200", salida)

    def test_el_peldano_de_verdad_el_intento_y_el_infra(self):
        """#112: la línea dice el peldaño de verdad — la etiqueta que el
        dispatcher anotó: el índice en `ESCALERA` con su runner y su
        modelo — y el número de intento, como intento. Un intento `infra`
        no gastó peldaño: se ve distinto de uno que sí. Dos corridas con
        el mismo `attempt` (dos `run_id`) cada una muestra su peldaño y
        no se dibujan como el mismo paso."""
        from harness.summary import Ticket
        p1 = "peldaño 1 · pi qwen/qwen3.8-27b --thinking medium"
        p2 = "peldaño 2 · pi qwen/qwen3.8-27b --thinking high"
        peldanos = [
            {"attempt": 1, "runner": "pi", "motivo": "gate rojo",
             "costo": 0.05, "cuota": 0.0, "peldano": p1, "clase": "modelo"},
            {"attempt": 2, "runner": "pi", "motivo": "no arranco el agente",
             "costo": 0.0, "cuota": 0.0, "peldano": p1, "clase": "infra"},
            {"attempt": 3, "runner": "pi", "motivo": None,
             "costo": 0.0, "cuota": 8200.0, "peldano": p2, "clase": None},
        ]
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    cuota_semana=1.0, cuota_semana_harness=1.0, tickets=[
                        Ticket(contexto="personal", ref="ticket/56",
                              detalle="PR #99 abierto (gate verde)",
                              costo=0.05, cuota=8200.0, peldanos=peldanos)])
        salida = self.pantalla(r)
        self.assertIn("int. 1 · " + p1, salida)
        self.assertIn("int. 2 · " + p1, salida)
        self.assertIn("int. 3 · " + p2, salida)
        # El intento `infra` se marca, y sólo él: es el que no gastó
        # peldaño, la información que ordena la escalera.
        self.assertEqual(salida.count("no gasta peldaño"), 1)
        # El formato viejo (el número de intento pasando por peldaño)
        # ya no aparece.
        self.assertNotIn("peldaño 2 (pi)", salida)

    def test_un_solo_peldano_no_desglosa(self):
        from harness.summary import Ticket
        peldanos = [{"attempt": 1, "runner": "pi", "motivo": None,
                    "costo": 0.05, "cuota": 0.0}]
        r = Resumen(estado="ok", desde="2026-08-21T23:00:00Z",
                    cuota_semana=1.0, cuota_semana_harness=1.0, tickets=[
                        Ticket(contexto="personal", ref="ticket/3",
                              detalle="PR #19 abierto (gate verde)",
                              costo=0.05, cuota=0.0, peldanos=peldanos)])
        salida = self.pantalla(r)
        self.assertNotIn("peldaño", salida)

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

    def test_un_pr_mergeado_se_lee_como_mergeado_en_su_linea(self):
        """(#86) El cuerpo del evento dice "abierto" de cuando se abrió; si
        hoy está mergeado, la línea lo dice también — y conserva el número
        de PR, que es lo útil para ir a mirarlo."""
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-25T08:00:00Z", tickets=[
            Ticket(contexto="personal", ref="ticket/41", numero=77,
                  detalle="PR #77 abierto (gate verde)",
                  estado="mergeado", en_vivo=True),
        ])
        salida = self.pantalla(r)
        self.assertIn("1 tickets mergeados", salida)
        self.assertIn("ticket/41: PR #77 mergeado", salida)
        self.assertNotIn("PR #77 abierto", salida)

    def test_abierto_se_lee_abierto_y_cerrado_se_distingue_de_mergeado(self):
        """(#86) El estado vivo sigue siendo el de la línea: uno que sigue
        abierto se lee abierto, y uno cerrado sin mergear no se lee como
        mergeado."""
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-25T08:00:00Z", tickets=[
            Ticket(contexto="personal", ref="ticket/73", numero=79,
                  detalle="PR #79 abierto (gate verde)",
                  estado="abierto", en_vivo=True),
            Ticket(contexto="personal", ref="ticket/74", numero=80,
                  detalle="PR #80 abierto (gate verde)",
                  estado="cerrado", en_vivo=True),
        ])
        salida = self.pantalla(r)
        self.assertIn("PR #79 abierto", salida)
        self.assertIn("PR #80 cerrado", salida)
        self.assertNotIn("mergeado", [l for l in salida.splitlines()
                                      if "ticket/74" in l][0])

    def test_el_encabezado_y_el_detalle_no_pueden_discrepar(self):
        """(#86) El encabezado cuenta por `estado` reconciliado y la línea
        de cada ticket dice ese mismo estado: se compara sobre el mismo
        conjunto — por grupo, el número del encabezado es la cantidad de
        líneas de abajo, y cada línea dice el estado del grupo."""
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-25T08:00:00Z", tickets=[
            Ticket(contexto="personal", ref="ticket/41", numero=77,
                  detalle="PR #77 abierto (gate verde)",
                  estado="mergeado", en_vivo=True),
            Ticket(contexto="personal", ref="ticket/72", numero=78,
                  detalle="PR #78 abierto (gate verde)",
                  estado="mergeado", en_vivo=True),
            Ticket(contexto="personal", ref="ticket/73", numero=79,
                  detalle="PR #79 abierto (gate verde)",
                  estado="abierto", en_vivo=True),
            Ticket(contexto="personal", ref="ticket/74", numero=80,
                  detalle="PR #80 abierto (gate verde)",
                  estado="cerrado", en_vivo=True),
        ])
        salida = self.pantalla(r)
        estado_por_grupo = {"tickets mergeados": "mergeado",
                            "tickets con PR abierto": "abierto",
                            "tickets cerrados sin mergear": "cerrado"}
        grupos, actual = [], None
        for l in salida.splitlines():
            m = re.match(r"\s+. (\d+) tickets ", l)
            if m:
                actual = {"n": int(m.group(1)), "cab": l, "det": []}
                grupos.append(actual)
            elif l.lstrip().startswith("· ") and actual is not None:
                actual["det"].append(l)
        self.assertEqual(len(grupos), 3)
        for g in grupos:
            estado = next(e for et, e in estado_por_grupo.items() if et in g["cab"])
            self.assertEqual(len(g["det"]), g["n"], g["cab"])
            for d in g["det"]:
                self.assertIn(estado, d, (g["cab"], d))

    def test_sin_reconciliar_encabezado_y_detalle_dicen_lo_mismo(self):
        """(#86) Sin poder reconciliar (sin red), el encabezado y la línea
        salen del mismo dato —el log— y la línea marca que no se confirmó
        en vivo."""
        from harness.summary import Ticket
        r = Resumen(estado="ok", desde="2026-08-25T08:00:00Z", tickets=[
            Ticket(contexto="personal", ref="ticket/41", numero=77,
                  detalle="PR #77 abierto (gate verde)"),
        ])
        salida = self.pantalla(r)
        self.assertIn("1 tickets con PR abierto (gate verde)", salida)
        self.assertIn("PR #77 abierto (gate verde)", salida)
        self.assertIn("según el log", salida)

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


class TestEstadoFrontera(unittest.TestCase):
    """El ticket #43: la línea de cada ticket de la frontera muestra su estado."""

    def test_la_frontera_del_golden_tiene_pr_abierto(self):
        """koku #7 tiene un PR abierto sobre ticket/7 (la fixture lo trae)."""
        koku = SNAP.contexts[0].repos[1]
        self.assertEqual([(i.number, i.estado) for i in koku.frontier],
                         [(7, "pr-abierto"), (9, "libre")])
        ah = SNAP.contexts[0].repos[0]
        self.assertEqual([i.estado for i in ah.frontier],
                         ["libre", "libre", "libre", "libre"])

    def test_el_estado_sale_en_la_linea(self):
        lineas = render(SNAP, color=False).splitlines()
        self.assertTrue(any("#7 Cerrar caja del dia sin doble conteo  pr-abierto" in l
                            for l in lineas))
        self.assertTrue(any("#9 Exportar a CSV  libre" in l for l in lineas))

    def test_despachado_tambien_se_ve(self):
        crudo = support.golden_raw("personal")
        crudo["eventos"] = [{"timestamp": "2026-08-24T02:00:00Z",
                             "contexto": "personal", "origen": "harness",
                             "run_id": "r1", "ticket": "koku#9", "attempt": 1,
                             "tipo": "agente", "ref": "ticket/9",
                             "cuerpo": "koku-9 (kind pi, pane w9:p2)"}]
        crudo["agents"] = [{"cwd": "/x/.worktrees/koku-ticket-9"}]
        for l in render(snapshot(crudo), color=False).splitlines():
            if "#9 Exportar a CSV" in l:
                self.assertIn("despachado", l)
                return
        self.fail("falta la línea de #9")


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


def evento(**over):
    """Una línea del log JSONL, como la escribe `EventLog`."""
    linea = {"timestamp": "2026-08-26T21:41:00Z", "contexto": "personal",
             "origen": "harness", "run_id": "r1", "ticket": None,
             "attempt": None, "tipo": "corrida", "ref": "corrida",
             "cuerpo": "inicio: 3 ticket(s), max 2 en paralelo", "clase": None}
    linea.update(over)
    return linea


class TestLineaEvento(unittest.TestCase):
    """La salida en vivo de `harness run` (ticket #81): cada evento del log
    se imprime en una línea corta y legible, no el JSON crudo."""

    def test_evento_de_corrida(self):
        self.assertEqual(linea_evento(evento()),
                         "21:41  corrida  inicio: 3 ticket(s), max 2 en paralelo")

    def test_evento_de_ticket_muestra_el_numero_no_el_repo(self):
        linea = linea_evento(evento(ticket="agent-harness#64", ref="ticket/64",
                                    tipo="gate", cuerpo="verde"))
        self.assertTrue(linea.startswith("21:41  #64"), linea)
        self.assertIn("gate: verde", linea)
        self.assertNotIn("agent-harness", linea)
        self.assertNotIn("ticket/64", linea)

    def test_el_tipo_se_distingue_y_corrida_no_se_repite(self):
        linea = linea_evento(evento(ticket="koku#7", ref="ticket/7",
                                    tipo="prompt",
                                    cuerpo="intento 1: working"))
        self.assertIn("prompt: intento 1: working", linea)
        self.assertNotIn("corrida", linea)
        # `corrida` no repite su nombre en el mensaje: la columna ya dice
        # de qué se trata.
        self.assertEqual(linea_evento(evento()).count("corrida"), 1)

    def test_cuerpo_vacio_muestra_solo_el_tipo(self):
        self.assertIn("limpieza", linea_evento(evento(ticket="koku#7",
                                                       tipo="limpieza",
                                                       cuerpo="")))

    def test_no_es_json_crudo(self):
        self.assertNotIn("{", linea_evento(evento()))
        self.assertNotIn('"timestamp"', linea_evento(evento()))

    def test_cuerpo_largo_se_corta(self):
        linea = linea_evento(evento(cuerpo="x" * 300))
        self.assertLessEqual(len(linea), 120)
        self.assertTrue(linea.endswith("..."))

    def test_hora_sale_del_timestamp(self):
        self.assertTrue(linea_evento(evento(
            timestamp="2026-08-26T22:05:00Z")).startswith("22:05"))

    def test_timestamp_no_iso_no_se_rompe(self):
        self.assertIn("otra-timestamp",
                      linea_evento(evento(timestamp="otra-timestamp")))

    def test_sin_color_no_hay_escapes(self):
        """stdout no TTY: sin escapes ni caracteres de control (AC #81)."""
        for tipo, cuerpo in (("abandono", "gate rojo"), ("gate", "verde"),
                             ("corrida", "fin: 1 hecho(s)"), ("peldano", "peldaño 1")):
            linea = linea_evento(evento(tipo=tipo, cuerpo=cuerpo), color=False)
            self.assertNotIn("\033", linea)
            self.assertNotIn("\r", linea)

    def test_color_marca_el_estado(self):
        """Con TTY el veredicto se distingue: abandono rojo, gate verde y
        fin de corrida verdes, el peldaño en su propio color."""
        from harness.render import COLOR
        self.assertIn(COLOR.red, linea_evento(evento(tipo="abandono",
                                                     cuerpo="gate rojo"),
                                              color=True))
        # El bloqueo del guard es la línea que más dice de una noche (#95).
        self.assertIn(COLOR.red, linea_evento(evento(tipo="guard",
                                                     cuerpo="gh pr merge --yes -- "
                                                           "el merge es decision humana"),
                                              color=True))
        self.assertIn(COLOR.grn, linea_evento(evento(tipo="gate",
                                                     cuerpo="verde"), color=True))
        self.assertIn(COLOR.grn, linea_evento(evento(tipo="corrida",
                                                     cuerpo="fin: 1 hecho(s)"),
                                              color=True))
        self.assertIn(COLOR.cya, linea_evento(evento(tipo="peldano",
                                                     cuerpo="peldaño 1"),
                                              color=True))

    def test_el_texto_es_el_mismo_con_y_sin_color(self):
        for tipo, cuerpo in (("abandono", "gate rojo"), ("gate", "verde"),
                             ("peldano", "peldaño 1"), ("corrida", "inicio")):
            con = linea_evento(evento(tipo=tipo, cuerpo=cuerpo), color=True)
            sin = linea_evento(evento(tipo=tipo, cuerpo=cuerpo), color=False)
            self.assertEqual(re.sub(r"\033\[[0-9;]*m", "", con), sin)


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
