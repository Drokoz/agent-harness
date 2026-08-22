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


if __name__ == "__main__":
    unittest.main()
