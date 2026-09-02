"""El reporte HTML: que dibuje el snapshot y que no se rompa con datos incompletos.

Se testea el comportamiento observable —qué aparece en la página— y no cómo está
armado el HTML por dentro, que va a cambiar.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.report import render_html  # noqa: E402


def snap(**over):
    base = {
        "version": 1,
        "offline": False,
        "agents": {"state": "ok", "items": [
            {"who": "t6", "agent": "pi", "status": "working", "repo": "ah-t6", "pane": "w4:p1"},
            {"who": "viejo", "agent": "claude", "status": "blocked", "repo": "koku", "pane": "w1:p2"},
        ]},
        "resumen": {"estado": "ok", "desde": "2026-08-22T10:00:00Z",
                    "hasta": "2026-08-22T18:00:00Z", "costo": 1.25,
                    "tickets": [{"repo": "agent-harness", "number": 2, "title": "Gate"}],
                    "prs": [{"repo": "koku", "number": 38, "title": "Gate de koku"}],
                    "trabados": []},
        "contexts": [{
            "name": "personal", "tracker": "github", "autonomy": "frontier", "run": "local",
            "budget": {"state": "ok", "polarity": "remaining", "provider": "openrouter",
                       "total": 10.0, "used": 4.0, "left": 6.0, "ratio": 0.4, "tickets": 25},
            "repos": [{
                "name": "agent-harness", "branch": "main", "dirty": 0, "slug": "d/ah",
                "tracker": "github", "closed_count": 9,
                "ready": {"gate": True, "skills": True, "context": False},
                "frontier": [{"number": 5, "title": "Adaptador de Jira"}],
                "blocked": [{"number": 8, "title": "Delegación remota"}],
                "triage": [], "prs": [],
            }],
        }],
    }
    base.update(over)
    return base


class TestReport(unittest.TestCase):
    def test_dibuja_lo_esencial(self):
        h = render_html(snap())
        for esperado in ("Reporte del Harness", "personal", "agent-harness",
                         "Adaptador de Jira", "Delegación remota", "US$6.00"):
            self.assertIn(esperado, h, f"falta {esperado!r} en la página")

    def test_completitud_sale_del_conteo_de_cerrados(self):
        h = render_html(snap())
        # 9 cerrados, 2 abiertos (1 frontera + 1 bloqueado) → 82%
        self.assertIn("9 cerrados", h)
        self.assertIn("2 abiertos", h)
        self.assertRegex(h, r"8[12]% completo")

    def test_sin_cerrados_no_inventa_porcentaje(self):
        s = snap()
        s["contexts"][0]["repos"][0]["closed_count"] = None
        h = render_html(s)
        self.assertIn("Sin datos del tracker", h)
        self.assertNotIn("% completo", h)

    def test_los_bloqueados_aparecen_como_bloqueados(self):
        h = render_html(snap())
        self.assertIn("Puede tomarse ahora", h)
        self.assertIn("Bloqueado", h)

    def test_presupuesto_gastado_invierte_la_lectura(self):
        s = snap()
        s["contexts"][0]["budget"].update({"polarity": "spend", "used": 120.0,
                                           "total": 400.0, "ratio": 0.3})
        h = render_html(s)
        self.assertIn("US$120.00 usados", h)
        self.assertIn("que hay que gastar", h)

    def test_snapshot_degradado_no_rompe(self):
        """Sin presupuesto, sin resumen, sin agentes: la página sale igual."""
        s = snap(resumen={"estado": "vacio"}, agents={"state": "outside", "items": []})
        s["contexts"][0]["budget"] = {"state": "missing"}
        h = render_html(s)
        self.assertIn("Reporte del Harness", h)
        self.assertIn("sin credencial", h)
        self.assertIn("Fuera de herdr", h)

    def test_sin_contextos_no_explota(self):
        h = render_html({"version": 1, "contexts": [], "agents": {}, "resumen": {}})
        self.assertIn("Reporte del Harness", h)

    def test_escapa_el_html_de_los_titulos(self):
        s = snap()
        s["contexts"][0]["repos"][0]["frontier"] = [
            {"number": 1, "title": '<script>alert("x")</script>'}]
        h = render_html(s)
        self.assertNotIn("<script>alert", h)
        self.assertIn("&lt;script&gt;", h)

    def test_es_autocontenido(self):
        """Sólo Google Fonts puede salir a la red: es lo único que la CSP admite."""
        h = render_html(snap())
        externos = re.findall(r'(?:src|href)="(https?://[^"]+)"', h)
        for url in externos:
            self.assertTrue(url.startswith("https://fonts.g"),
                            f"recurso externo no permitido: {url}")

    def test_las_dos_temas_estan_definidos(self):
        h = render_html(snap())
        self.assertIn("prefers-color-scheme:dark", h)
        self.assertIn('[data-theme="dark"]', h)


class TestCuota(unittest.TestCase):
    """La cuota en el reporte HTML (#47): la semana en el resumen, el costo
    por ticket en las dos monedas, los peldaños de un ticket escalado, y la
    evolución por noche en su propia sección."""

    def test_semana_en_el_resumen(self):
        s = snap()
        s["resumen"]["cuota_semana"] = 12345.0
        s["resumen"]["cuota_semana_harness"] = 6000.0
        h = render_html(s)
        self.assertIn("Cuota de la semana", h)
        self.assertIn("12,345", h)
        self.assertIn("6,000", h)

    def test_sin_cuota_semanal_no_muestra_el_kpi(self):
        h = render_html(snap())
        self.assertNotIn("Cuota de la semana", h)

    def test_costo_por_ticket_en_las_dos_monedas(self):
        s = snap()
        s["resumen"]["tickets"] = [{"repo": "agent-harness", "number": 2,
                                    "title": "Gate", "costo": 0.42, "cuota": 12345.0}]
        h = render_html(s)
        self.assertIn("US$0.42", h)
        self.assertIn("12,345 tok", h)

    def test_ticket_sin_gasto_no_muestra_sufijo_vacio(self):
        s = snap()
        s["resumen"]["tickets"] = [{"repo": "agent-harness", "number": 2, "title": "Gate"}]
        h = render_html(s)
        self.assertIn("agent-harness #2", h)

    def test_peldanos_de_un_ticket_escalado(self):
        s = snap()
        s["resumen"]["tickets"] = [{
            "repo": "agent-harness", "number": 2, "title": "Gate",
            "costo": 0.05, "cuota": 8200.0,
            "peldanos": [
                {"attempt": 1, "runner": "pi", "costo": 0.05, "cuota": 0.0,
                 "peldano": "peldaño 1 · pi qwen/qwen3.8-27b --thinking medium",
                 "clase": "modelo"},
                {"attempt": 2, "runner": "pi", "costo": 0.0, "cuota": 0.0,
                 "peldano": "peldaño 1 · pi qwen/qwen3.8-27b --thinking medium",
                 "clase": "infra"},
                {"attempt": 3, "runner": "pi", "costo": 0.0, "cuota": 8200.0,
                 "peldano": "peldaño 2 · pi qwen/qwen3.8-27b --thinking high",
                 "clase": None}],
        }]
        h = render_html(s)
        # #112: el peldaño de verdad (la etiqueta del dispatcher) y el
        # intento, como intento; el intento `infra` se marca.
        self.assertIn("int. 1 · peldaño 1 · pi qwen/qwen3.8-27b --thinking medium", h)
        self.assertIn("int. 2 · peldaño 1 · pi qwen/qwen3.8-27b --thinking medium", h)
        self.assertIn("int. 3 · peldaño 2 · pi qwen/qwen3.8-27b --thinking high", h)
        self.assertEqual(h.count("no gasta peldaño"), 1)
        self.assertIn("8,200 tok", h)

    def test_un_solo_peldano_no_desglosa(self):
        s = snap()
        s["resumen"]["tickets"] = [{
            "repo": "agent-harness", "number": 2, "title": "Gate", "costo": 0.05,
            "peldanos": [{"attempt": 1, "runner": "pi", "costo": 0.05, "cuota": 0.0}],
        }]
        h = render_html(s)
        self.assertNotIn("peldaño", h)

    def test_evolucion_por_noche(self):
        s = snap(cuota={"estado": "ok", "por_dia": {
            "2026-08-21": {"harness": 0.0, "resto": 2856.0},
            "2026-08-28": {"harness": 9640.0, "resto": 0.0},
        }})
        h = render_html(s)
        self.assertIn("2026-08-21", h)
        self.assertIn("2026-08-28", h)
        self.assertIn("9,640", h)

    def test_sin_cuota_no_rompe(self):
        """AC #47: sin datos de cuota la página no se rompe, dice que no
        hay y sigue."""
        h = render_html(snap(cuota={"estado": "offline"}))
        self.assertIn("Reporte del Harness", h)
        self.assertIn("Sin datos de cuota", h)

    def test_cuota_ausente_no_rompe(self):
        h = render_html(snap())
        self.assertIn("Reporte del Harness", h)


class TestBloqueos(unittest.TestCase):
    """Los bloqueos del guard en el reporte (#95): entran al resumen como
    el resto, y con cero bloqueos no se ocupa espacio para decir que no
    pasó nada."""

    def test_bloqueos_entran_al_reporte(self):
        s = snap()
        s["resumen"]["bloqueos"] = [
            {"ticket": "agent-harness#95", "motivo": "el merge es decision humana",
             "conteo": 3},
            {"ticket": None, "motivo": "reescribe historia", "conteo": 1},
        ]
        h = render_html(s)
        # KPI (total en intentos, no en líneas) + sección con el detalle.
        self.assertEqual(h.count("Bloqueos del guard"), 2)
        # El KPI cuenta intentos (3+1), no líneas de log.
        self.assertIn('<span class="kpi-n is-human">4</span>', h)
        self.assertIn("agent-harness#95", h)
        self.assertIn("el merge es decision humana", h)
        # El repetido sale una sola vez, con su conteo.
        self.assertIn("x3", h)
        self.assertEqual(h.count("el merge es decision humana"), 1)

    def test_cero_bloqueos_no_ocupan_lugar(self):
        self.assertNotIn("Bloqueos del guard", render_html(snap()))


if __name__ == "__main__":
    unittest.main()


class TestLoCerradoSeLee(unittest.TestCase):
    """Los tickets del resumen no traen repo/number/title.

    El dispatcher los anota como {ref, detalle} —"ticket/69", "PR #87 abierto
    (gate verde)"— y el reporte los dibujaba buscando claves que no existen, así
    que la lista de "Cerrados" salía con viñetas vacías y un "#" suelto.
    """

    def test_dibuja_ref_y_detalle_cuando_no_hay_repo_ni_titulo(self):
        s = snap()
        s["resumen"]["tickets"] = [
            {"contexto": "noche", "ref": "ticket/69",
             "detalle": "PR #87 abierto (gate verde)"}]
        h = render_html(s)
        self.assertIn("ticket/69", h)
        self.assertIn("PR #87 abierto", h)

    def test_sigue_dibujando_repo_y_titulo_cuando_si_estan(self):
        h = render_html(snap())
        self.assertIn("agent-harness #2", h)
        self.assertIn("Gate", h)

    def test_no_deja_un_numeral_suelto_sin_numero(self):
        s = snap()
        s["resumen"]["tickets"] = [{"ref": "ticket/69", "detalle": "algo"}]
        h = render_html(s)
        self.assertNotIn('">  #</span>', h)
        self.assertNotIn('"> #</span>', h)
