"""El routing de OpenRouter (#126): la declaración del repo, el estado contra
una models.json sintética, la evidencia del log, `doctor` y la línea de
`status`.

El caso que motivó todo: la noche del 2026-09-01 OpenRouter eligió Reka, Reka
limitó, y la flota se apagó seis veces en la misma línea. El arreglo vivía en
`~/.pi/agent/models.json`, editado a mano: no versionado, no testeado. Acá se
testea contra configs sintéticas: routing ausente, distinto al esperado y
correcto.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support

from harness import costo_pi, doctor, routing, snapshot
from harness.render import render

ROOT = support.ROOT
HARNESS = ROOT / "bin" / "harness"

DECL = {"provider": "openrouter",
        "compat": {"openRouterRouting": {"allow_fallbacks": True,
                                         "ignore": ["reka"]}}}
MODELS_OK = {"providers": {"openrouter": {"apiKey": "sk-or-fake",
                                          "compat": {"openRouterRouting":
                                                     {"allow_fallbacks": True,
                                                      "ignore": ["reka"]}}}}}
SIN_MODELS = None


def models_cambiando(**routing_over):
    """Una models.json con el routing tocado en las keys pedidas."""
    m = copy.deepcopy(MODELS_OK)
    real = m["providers"]["openrouter"]["compat"]["openRouterRouting"]
    for k, v in routing_over.items():
        if v is None:
            real.pop(k, None)
        else:
            real[k] = v
    return m


def sesion_con_error(mensaje):
    return {"salida": {"stop": "error", "error": mensaje}}


class TestEstado(unittest.TestCase):
    """El veredicto puro, contra configs sintéticas (el corazón de #126)."""

    def test_routing_correcto(self):
        self.assertEqual(routing.estado(DECL, MODELS_OK), ("ok", ""))

    def test_routing_ausente(self):
        m = copy.deepcopy(MODELS_OK)
        del m["providers"]["openrouter"]["compat"]
        self.assertEqual(routing.estado(DECL, m)[0], "ausente")

    def test_routing_ausente_sin_provider(self):
        self.assertEqual(routing.estado(DECL, {"providers": {}})[0], "ausente")

    def test_sin_models_json(self):
        estado, detalle = routing.estado(DECL, SIN_MODELS)
        self.assertEqual(estado, "sin-config")
        self.assertIn("models.json", detalle)

    def test_routing_distinto_ignore(self):
        estado, detalle = routing.estado(
            DECL, models_cambiando(ignore=["reka", "otro"]))
        self.assertEqual(estado, "distinto")
        self.assertIn("ignore", detalle)

    def test_routing_distinto_allow_fallbacks(self):
        estado, _ = routing.estado(DECL, models_cambiando(allow_fallbacks=False))
        self.assertEqual(estado, "distinto")

    def test_routing_distinto_falta_key(self):
        estado, detalle = routing.estado(DECL, models_cambiando(ignore=None))
        self.assertEqual(estado, "distinto")
        self.assertIn("ignore", detalle)

    def test_routing_extra_no_invalida(self):
        """Lo esperado está puesto aunque lo real tenga algo más."""
        m = models_cambiando(zdr=True)
        self.assertEqual(routing.estado(DECL, m)[0], "ok")

    def test_declaracion_sin_compat(self):
        self.assertEqual(routing.estado({"provider": "openrouter"},
                                        MODELS_OK)[0], "distinto")


class TestAplicar(unittest.TestCase):
    """`doctor --fix`: el routing se aplica sin tocar el resto del archivo."""

    def _aplicar_en(self, contenido):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            if contenido is not None:
                path.write_text(contenido, encoding="utf-8")
            ok, msg = routing.aplicar(DECL, path=str(path))
            data = None
            if ok and path.exists():
                data = json.loads(path.read_text())
            return ok, msg, data

    def test_crea_el_routing_preservando_el_resto(self):
        base = json.dumps({"providers": {"openrouter": {"apiKey": "sk-or-fake"},
                                         "otra-provider": {"baseUrl": "x"}}})
        ok, msg, data = self._aplicar_en(base)
        self.assertTrue(ok, msg)
        self.assertEqual(data["providers"]["openrouter"]["apiKey"], "sk-or-fake")
        self.assertEqual(data["providers"]["otra-provider"], {"baseUrl": "x"})
        self.assertEqual(data["providers"]["openrouter"]["compat"]["openRouterRouting"],
                         DECL["compat"]["openRouterRouting"])

    def test_idempotente(self):
        ok, _, data = self._aplicar_en(json.dumps(MODELS_OK))
        self.assertTrue(ok)
        self.assertEqual(routing.estado(DECL, data), ("ok", ""))
        ok2, _, data2 = self._aplicar_en(json.dumps(data))
        self.assertTrue(ok2)
        self.assertEqual(data2, data)

    def test_sobrescribe_lo_distinto(self):
        base = json.dumps(models_cambiando(ignore=["reka", "otro"]))
        ok, _, data = self._aplicar_en(base)
        self.assertTrue(ok)
        self.assertEqual(routing.estado(DECL, data), ("ok", ""))

    def test_sin_archivo_no_inventa(self):
        ok, msg, data = self._aplicar_en(None)
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("no hay", msg)

    def test_json_roto_no_se_escribe(self):
        ok, msg, data = self._aplicar_en("{no soy json")
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("JSON", msg)


class TestEvidencia(unittest.TestCase):
    """La lista de proveedores a evitar sale del log, no de la corazonada."""

    def test_extrae_al_proveedor_del_error(self):
        sesiones = [sesion_con_error("Upstream error from Reka: Too many requests."),
                    sesion_con_error("Upstream error from Reka: Too many requests.")]
        self.assertEqual(costo_pi.proveedores_con_error(sesiones), {"reka": 2})

    def test_varios_proveedores(self):
        sesiones = [sesion_con_error("Upstream error from Reka: Too many requests."),
                    sesion_con_error("Upstream error from Alibaba: Internal error.")]
        self.assertEqual(costo_pi.proveedores_con_error(sesiones),
                         {"reka": 1, "alibaba": 1})

    def test_sesiones_bien_no_apanan(self):
        self.assertEqual(costo_pi.proveedores_con_error(
            [{"salida": {"stop": "end_turn", "error": ""}}]), {})

    def test_error_sin_proveedor_reconocible(self):
        self.assertEqual(costo_pi.proveedores_con_error(
            [sesion_con_error("Connection error.")]), {})


class TestDoctor(unittest.TestCase):
    """El cruce: declaración + disco + evidencia."""

    def test_ok_con_evidencia_cobierta(self):
        d = doctor.diagnostico(DECL, MODELS_OK,
                               [sesion_con_error("Upstream error from Reka: x.")])
        self.assertEqual(d["estado"], "ok")
        self.assertEqual(d["evidencia"], {"reka": 1})
        self.assertEqual(d["sin_declarar"], [])
        self.assertEqual(d["sin_evidencia"], [])

    def test_evidencia_sin_declarar(self):
        d = doctor.diagnostico(DECL, MODELS_OK,
                               [sesion_con_error("Upstream error from X: x.")])
        self.assertEqual(d["sin_declarar"], ["x"])

    def test_ausente(self):
        m = copy.deepcopy(MODELS_OK)
        del m["providers"]["openrouter"]["compat"]
        d = doctor.diagnostico(DECL, m)
        self.assertEqual(d["estado"], "ausente")
        self.assertIsNone(d["real"])

    def test_render_sin_color(self):
        d = doctor.diagnostico(DECL, MODELS_OK)
        texto = doctor.render(d, color=False)
        self.assertNotIn("\033", texto)
        self.assertIn("✓ ok", texto)
        self.assertIn("esperado", texto)
        self.assertIn("real", texto)

    def test_render_ausente_sugiere_fix(self):
        m = copy.deepcopy(MODELS_OK)
        del m["providers"]["openrouter"]["compat"]
        texto = doctor.render(doctor.diagnostico(DECL, m), color=False)
        self.assertIn("ausente", texto)
        self.assertIn("harness doctor --fix", texto)


class TestStatus(unittest.TestCase):
    """`status` dice si el routing esperado está puesto, junto a la
    readiness."""

    def linea_routing(self, raw):
        for l in render(snapshot.snapshot(raw), color=False).splitlines():
            if l.startswith(" Routing"):
                return l
        return None

    def test_ok(self):
        raw = support.golden_raw("personal")
        self.assertIn("✓ ok", self.linea_routing(raw))
        self.assertIn("ignore: reka", self.linea_routing(raw))

    def test_ausente(self):
        raw = support.golden_raw("personal")
        raw["routing"]["models"] = {"providers": {"openrouter": {}}}
        self.assertIn("ausente", self.linea_routing(raw))
        self.assertIn("harness doctor --fix", self.linea_routing(raw))

    def test_distinto(self):
        raw = support.golden_raw("personal")
        raw["routing"]["models"] = models_cambiando(allow_fallbacks=False)
        self.assertIn("distinto al esperado", self.linea_routing(raw))

    def test_sin_config(self):
        raw = support.golden_raw("personal")
        raw["routing"]["models"] = None
        self.assertIn("models.json", self.linea_routing(raw))

    def test_sin_declaracion(self):
        raw = support.golden_raw("personal")
        raw["routing"]["declaracion"] = None
        self.assertIn("sin routing declarado", self.linea_routing(raw))

    def test_salida_json_lo_trae(self):
        snap = snapshot.snapshot(support.golden_raw("personal"))
        self.assertEqual(snapshot.as_dict(snap)["routing"]["state"], "ok")


def _run_cli(*args, home):
    env = dict(os.environ)
    env["HOME"] = home
    env.pop("HARNESS_OFFLINE", None)
    return subprocess.run([str(HARNESS), *args], env=env, capture_output=True,
                          text=True, timeout=120)


class TestDoctorCli(unittest.TestCase):
    """El comando de punta a punta, con una HOME sintética."""

    def _home(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        self.addCleanup(tmp.cleanup)
        (home / ".pi" / "agent").mkdir(parents=True)
        return home

    def test_sin_models_json_salida_1(self):
        home = self._home()
        p = _run_cli("doctor", home=str(home))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("sin-config", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_routing_ok_salida_0(self):
        home = self._home()
        (home / ".pi/agent/models.json").write_text(json.dumps(MODELS_OK))
        p = _run_cli("doctor", home=str(home))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("✓ ok", p.stdout)

    def test_distinto_salida_1_y_fix_salida_0(self):
        home = self._home()
        models = home / ".pi/agent/models.json"
        base = copy.deepcopy(MODELS_OK)
        base["providers"]["openrouter"]["compat"]["openRouterRouting"]["ignore"] = \
            ["reka", "otro"]
        models.write_text(json.dumps(base))
        p = _run_cli("doctor", home=str(home))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("distinto al esperado", p.stdout)
        p = _run_cli("doctor", "--fix", home=str(home))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        data = json.loads(models.read_text())
        self.assertEqual(routing.estado(DECL, data), ("ok", ""))
        # El resto del archivo se preservó.
        self.assertEqual(data["providers"]["openrouter"]["apiKey"], "sk-or-fake")

    def test_fix_sin_archivo_salida_2(self):
        home = self._home()
        p = _run_cli("doctor", "--fix", home=str(home))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse((home / ".pi/agent/models.json").exists())


if __name__ == "__main__":
    unittest.main()
