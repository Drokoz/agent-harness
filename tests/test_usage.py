"""`harness usage`: la cuota REAL de Claude, leída de `/usage` por herdr.

Tres piezas, testeadas por separado:

- el parser (`parsear_usage`, puro): texto de pantalla -> buckets con su %
  y su reset como datetime con zona. La mayor parte de los tests: el texto
  de `/usage` no tiene contrato, y lo único que lo protege son fixtures
  capturadas de verdad (tests/fixtures/usage_*.txt). Nunca inventa un
  número: lo que no entiende es un `UsageIlegible` con una línea legible.
- la captura (`capturar`), con un run_cmd falso como el de test_dispatch:
  abre un pane, arranca claude, manda `/usage`, lee y SIEMPRE cierra.
- la política (`decidir`, `aplicar_cuota`, `Cuota`): qué hace `harness run`
  con esos números — esperar el reset de la sesión de 5h, cuánto Claude
  lanzar según la semana, y el fallback a la estimación de `harness quota`.
"""

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import support
from harness import usage
from harness.config import ConfigError, parse_config
from harness.dispatch import Job
from harness.proposal import JUEZ_CLAUDE, JUEZ_QWEN, decidir_peldano_juez_usage
from harness.usage import (Bucket, Cuota, DecisionCuota, EstadoCuota,
                           PoliticaCuota, Usage, UsageIlegible, aplicar_cuota,
                           capturar, decidir, pane_base_de, parsear_reset,
                           parsear_usage, proxima_hora_limite)

SCL = ZoneInfo("America/Santiago")
HARNESS = support.ROOT / "bin" / "harness"
HEADER_SEMANA_T = "Current week (all models)"

# El sábado 17 de octubre de 2026 a las 20:00 en Santiago: el momento de la
# captura del homeserver (el reset de la sesión, 1:10am, es de mañana).
AHORA = datetime(2026, 10, 17, 20, 0, tzinfo=SCL)


def fixture(nombre):
    return (support.FIXTURES / nombre).read_text()


def scl(*a):
    return datetime(*a, tzinfo=SCL)


# ------------------------------------------------------------------ parser
class TestParser(unittest.TestCase):
    def test_la_captura_del_homeserver(self):
        u = parsear_usage(fixture("usage_homeserver.txt"), AHORA)
        self.assertEqual(u.sesion.pct, 17)
        self.assertEqual(u.sesion.reset, scl(2026, 10, 18, 1, 10))
        self.assertEqual(u.semana.pct, 6)
        self.assertEqual(u.semana.reset, scl(2026, 10, 23, 18, 0))
        self.assertEqual([(b.nombre, b.pct, b.reset) for b in u.por_modelo],
                         [("Fable", 0, scl(2026, 10, 23, 18, 0))])

    def test_los_resets_tienen_la_zona_de_santiago(self):
        u = parsear_usage(fixture("usage_homeserver.txt"), AHORA)
        self.assertEqual(u.sesion.reset.utcoffset(), timedelta(hours=-3))
        self.assertEqual(str(u.sesion.reset.tzinfo), "America/Santiago")

    def test_ignora_los_bloques_de_arriba(self):
        # "Session" trae su propio "Usage: 0 input..." y el footprint de
        # plugins sus números: nada de eso es la cuota.
        u = parsear_usage(fixture("usage_refrescando.txt"), AHORA)
        self.assertEqual(u.sesion.pct, 18)
        self.assertEqual(u.semana.pct, 6)
        self.assertEqual(u.por_modelo, [])

    def test_ignora_los_porcentajes_de_la_seccion_local(self):
        # "92% of your usage was at >150k context" no es un bucket.
        u = parsear_usage(fixture("usage_completo.txt"), AHORA)
        self.assertEqual((u.sesion.pct, u.semana.pct), (18, 6))
        self.assertEqual(u.sesion.reset, scl(2026, 10, 18, 1, 9))
        self.assertEqual(u.semana.reset, scl(2026, 10, 23, 17, 59))
        self.assertEqual([b.nombre for b in u.por_modelo], ["Fable"])

    def test_sin_la_semana_no_inventa_nada(self):
        texto = "   Current session\n   ██   17% used\n   Resets 1:10am\n"
        with self.assertRaises(UsageIlegible) as cm:
            parsear_usage(texto, AHORA)
        self.assertIn("Current week (all models)", str(cm.exception))

    def test_texto_vacio_o_ajeno_es_ilegible(self):
        for texto in ("", "bash: claude: command not found\n"):
            with self.subTest(texto=texto), self.assertRaises(UsageIlegible) as cm:
                parsear_usage(texto, AHORA)
            self.assertIn("Current session", str(cm.exception))

    def test_un_bucket_sin_porcentaje_es_ilegible(self):
        texto = fixture("usage_homeserver.txt").replace("6% used", "loading")
        with self.assertRaises(UsageIlegible) as cm:
            parsear_usage(texto, AHORA)
        self.assertIn("Current week (all models)", str(cm.exception))
        self.assertIn("% used", str(cm.exception))

    def test_un_reset_que_no_se_entiende_es_ilegible(self):
        texto = fixture("usage_homeserver.txt").replace(
            "Resets 1:10am (America/Santiago)", "Resets whenever it feels like")
        with self.assertRaises(UsageIlegible) as cm:
            parsear_usage(texto, AHORA)
        self.assertIn("Resets whenever it feels like", str(cm.exception))

    def test_sin_linea_de_reset_el_reset_es_none(self):
        texto = fixture("usage_homeserver.txt").replace(
            "   Resets 1:10am (America/Santiago)\n", "")
        u = parsear_usage(texto, AHORA)
        self.assertEqual(u.sesion.pct, 17)
        self.assertIsNone(u.sesion.reset)

    def test_porcentaje_con_decimales_y_cien(self):
        texto = (fixture("usage_homeserver.txt")
                 .replace("17% used", "100% used")
                 .replace("6% used", "6.5% used"))
        u = parsear_usage(texto, AHORA)
        self.assertEqual((u.sesion.pct, u.semana.pct), (100, 6.5))

    def test_lineas_envueltas_con_espacios_al_final(self):
        texto = "\n".join(l + "   " for l in
                          fixture("usage_homeserver.txt").splitlines())
        self.assertEqual(parsear_usage(texto, AHORA).semana.pct, 6)


class TestReset(unittest.TestCase):
    def test_hora_de_hoy_o_de_mañana(self):
        self.assertEqual(parsear_reset("Resets 11pm (America/Santiago)", AHORA),
                         scl(2026, 10, 17, 23, 0))
        self.assertEqual(parsear_reset("Resets 1:10am (America/Santiago)", AHORA),
                         scl(2026, 10, 18, 1, 10))

    def test_sin_minutos_y_sin_zona(self):
        # Sin zona: la del contexto (America/Santiago).
        self.assertEqual(parsear_reset("Resets 6pm", scl(2026, 10, 17, 9, 0)),
                         scl(2026, 10, 17, 18, 0))

    def test_en_relativo(self):
        self.assertEqual(parsear_reset("Resets in 2h 15m", AHORA),
                         AHORA + timedelta(hours=2, minutes=15))
        self.assertEqual(parsear_reset("Resets in 45m", AHORA),
                         AHORA + timedelta(minutes=45))
        self.assertEqual(parsear_reset("Resets in 3d 4h", AHORA),
                         AHORA + timedelta(days=3, hours=4))

    def test_fecha_con_at(self):
        self.assertEqual(parsear_reset("Resets Oct 23 at 6pm", AHORA),
                         scl(2026, 10, 23, 18, 0))

    def test_veinticuatro_horas(self):
        self.assertEqual(parsear_reset("Resets 18:30", scl(2026, 10, 17, 9, 0)),
                         scl(2026, 10, 17, 18, 30))

    def test_otra_zona_se_convierte_a_santiago(self):
        r = parsear_reset("Resets 9pm (UTC)", scl(2026, 10, 17, 9, 0))
        self.assertEqual(r, scl(2026, 10, 17, 18, 0))
        self.assertEqual(str(r.tzinfo), "America/Santiago")

    def test_la_fecha_de_enero_en_diciembre_es_del_año_que_viene(self):
        self.assertEqual(parsear_reset("Resets Jan 2, 6pm",
                                       scl(2026, 12, 30, 12, 0)),
                         scl(2027, 1, 2, 18, 0))

    def test_lo_que_no_se_entiende_falla_con_la_linea(self):
        for linea in ("Resets soon", "Resets 25:00", "Resets 13pm",
                      "Resets Foo 23, 6pm", "Resets 6pm (Mars/Olympus)",
                      "Resets 6"):
            with self.subTest(linea=linea), self.assertRaises(UsageIlegible) as cm:
                parsear_reset(linea, AHORA)
            self.assertIn(linea, str(cm.exception))


# ----------------------------------------------------------------- captura
class Herdr:
    """run_cmd falso para la captura: respuestas por subcomando de herdr."""

    def __init__(self, lecturas=None, listo=(True, '{"result":{"type":"output_matched"}}'),
                 semana=(True, '{"result":{"type":"output_matched"}}'),
                 split=(True, '{"result":{"pane":{"pane_id":"w1:p7"}}}'),
                 close=(True, '{"result":{"type":"ok"}}'),
                 panes=None):
        self.lecturas = list(lecturas if lecturas is not None
                             else ["❯\n  ⏵⏵ auto mode on (shift+tab to cycle)",
                                   fixture("usage_homeserver.txt")])
        self.listo, self.semana, self.split, self.close = listo, semana, split, close
        self.panes = panes
        self.llamadas = []

    def __call__(self, args, cwd=None, timeout=30):
        args = list(args)
        self.llamadas.append(args)
        sub = args[1:3]
        if sub == ["pane", "split"]:
            return self.split
        if sub == ["pane", "list"]:
            return (True, json.dumps({"result": {"panes": self.panes or []}}))
        if sub == ["pane", "wait-output"]:
            return self.semana if "Current week" in args else self.listo
        if sub == ["pane", "read"]:
            if len(self.lecturas) > 1:
                return (True, self.lecturas.pop(0))
            return (True, self.lecturas[0])
        if sub == ["pane", "close"]:
            return self.close
        return (True, "")

    def subcomandos(self):
        return [" ".join(a[1:3]) for a in self.llamadas]


class TestCaptura(unittest.TestCase):
    def test_el_camino_feliz_y_el_orden(self):
        h = Herdr()
        texto, avisos = capturar(h, pane_base="w1:p1", cwd="/repos",
                                 dormir=lambda s: None)
        self.assertIn("Current week (all models)", texto)
        self.assertEqual(avisos, [])
        self.assertEqual(h.llamadas[0],
                         ["herdr", "pane", "split", "--pane", "w1:p1",
                          "--direction", "right", "--cwd", "/repos",
                          "--no-focus"])
        self.assertIn(["herdr", "pane", "run", "w1:p7", "claude"], h.llamadas)
        self.assertIn(["herdr", "pane", "send-text", "w1:p7", "/usage"], h.llamadas)
        self.assertIn(["herdr", "pane", "send-keys", "w1:p7", "Enter"], h.llamadas)
        esperas = [a for a in h.llamadas if a[1:3] == ["pane", "wait-output"]]
        self.assertTrue(any("Current week" in a for a in esperas),
                        "se espera la salida con wait-output, no con sleeps fijos")
        # Siempre se cierra, y es lo último.
        self.assertEqual(h.llamadas[-1], ["herdr", "pane", "close", "w1:p7"])
        self.assertIn(["herdr", "pane", "send-keys", "w1:p7", "esc"], h.llamadas)

    def test_sin_pane_base_parte_del_actual(self):
        h = Herdr()
        capturar(h, pane_base=None, cwd="/repos", dormir=lambda s: None)
        self.assertIn("--current", h.llamadas[0])

    def test_espera_a_que_termine_de_refrescar(self):
        h = Herdr(lecturas=["❯ shift+tab", fixture("usage_refrescando.txt"),
                            fixture("usage_completo.txt")])
        dormidas = []
        texto, _ = capturar(h, pane_base="w1:p1", cwd="/r", dormir=dormidas.append)
        self.assertNotIn("Refreshing", texto)
        self.assertIn("Current week (Fable)", texto)
        self.assertTrue(dormidas)

    def test_si_el_primer_read_no_trae_los_buckets_lee_de_nuevo(self):
        # Visto en legion: wait-output ya vio "Current week" pero el read
        # inmediato devolvió la pantalla de antes.
        h = Herdr(lecturas=["❯ shift+tab", "❯ /usage", fixture("usage_completo.txt")])
        texto, _ = capturar(h, pane_base="w1:p1", cwd="/r", dormir=lambda s: None)
        self.assertIn("Current session", texto)

    def test_si_nunca_termina_de_refrescar_lo_avisa(self):
        h = Herdr(lecturas=["❯ shift+tab", fixture("usage_refrescando.txt")])
        texto, avisos = capturar(h, pane_base="w1:p1", cwd="/r",
                                 dormir=lambda s: None)
        self.assertIn("Current week (all models)", texto)
        self.assertTrue(any("Refreshing" in a for a in avisos))

    def test_claude_no_arranca_falla_y_cierra_igual(self):
        h = Herdr(listo=(False, '{"error":{"code":"timeout"}}'))
        with self.assertRaises(UsageIlegible) as cm:
            capturar(h, pane_base="w1:p1", cwd="/r", dormir=lambda s: None)
        self.assertIn("claude", str(cm.exception).lower())
        self.assertEqual(h.llamadas[-1], ["herdr", "pane", "close", "w1:p7"])

    def test_usage_no_aparece_falla_y_cierra_igual(self):
        h = Herdr(semana=(False, '{"error":{"code":"timeout"}}'))
        with self.assertRaises(UsageIlegible) as cm:
            capturar(h, pane_base="w1:p1", cwd="/r", dormir=lambda s: None)
        self.assertIn("Current week", str(cm.exception))
        self.assertEqual(h.llamadas[-1], ["herdr", "pane", "close", "w1:p7"])

    def test_la_carpeta_sin_confianza_falla_explicito(self):
        h = Herdr(lecturas=["Do you trust the files in this folder?\n❯ 1. Yes"])
        with self.assertRaises(UsageIlegible) as cm:
            capturar(h, pane_base="w1:p1", cwd="/r", dormir=lambda s: None)
        self.assertIn("confi", str(cm.exception))
        self.assertNotIn(["herdr", "pane", "send-text", "w1:p7", "/usage"],
                         h.llamadas)
        self.assertEqual(h.llamadas[-1], ["herdr", "pane", "close", "w1:p7"])

    def test_sin_pane_no_hay_nada_que_cerrar(self):
        h = Herdr(split=(False, "no server"))
        with self.assertRaises(UsageIlegible) as cm:
            capturar(h, pane_base="w1:p1", cwd="/r", dormir=lambda s: None)
        self.assertIn("pane", str(cm.exception))
        self.assertNotIn("pane close", h.subcomandos())

    def test_un_close_que_falla_queda_avisado(self):
        h = Herdr(close=(False, "pane not found"))
        _, avisos = capturar(h, pane_base="w1:p1", cwd="/r", dormir=lambda s: None)
        self.assertTrue(any("w1:p7" in a for a in avisos))

    def test_una_excepcion_del_run_cmd_tambien_cierra(self):
        h = Herdr()

        def explota(args, cwd=None, timeout=30):
            if args[1:3] == ["pane", "send-text"]:
                raise RuntimeError("se cayó el socket")
            return h(args, cwd=cwd, timeout=timeout)
        with self.assertRaises(RuntimeError):
            capturar(explota, pane_base="w1:p1", cwd="/r", dormir=lambda s: None)
        self.assertEqual(h.llamadas[-1], ["herdr", "pane", "close", "w1:p7"])


class TestPaneBase(unittest.TestCase):
    def test_dentro_de_herdr_el_propio(self):
        self.assertEqual(pane_base_de(Herdr(), {"HERDR_PANE_ID": "w3:p2"}), "w3:p2")

    def test_fuera_de_herdr_el_enfocado(self):
        h = Herdr(panes=[{"pane_id": "w2:p1", "focused": False},
                         {"pane_id": "w1:p1", "focused": True}])
        self.assertEqual(pane_base_de(h, {}), "w1:p1")

    def test_fuera_de_herdr_sin_enfocado_el_primero(self):
        h = Herdr(panes=[{"pane_id": "w2:p1"}, {"pane_id": "w1:p1"}])
        self.assertEqual(pane_base_de(h, {}), "w2:p1")

    def test_sin_server_de_herdr_es_ilegible(self):
        def sin_server(args, cwd=None, timeout=30):
            return (False, "could not connect to herdr server")
        with self.assertRaises(UsageIlegible) as cm:
            pane_base_de(sin_server, {})
        self.assertIn("herdr", str(cm.exception))


# ---------------------------------------------------------------- política
def estado(sesion=10, sesion_reset=None, semana=10, semana_reset=None,
           fuente="usage"):
    return EstadoCuota(fuente=fuente, sesion_pct=sesion,
                       sesion_reset=sesion_reset or AHORA + timedelta(hours=3),
                       semana_pct=semana,
                       semana_reset=semana_reset or AHORA + timedelta(days=6))


POL = PoliticaCuota()  # los defaults documentados


class TestPolitica(unittest.TestCase):
    def test_los_defaults(self):
        self.assertEqual((POL.umbral_sesion, POL.reserva_diaria,
                          POL.costo_ticket, POL.hora_limite),
                         (0.90, 0.08, 0.02, "07:00"))

    def test_proxima_hora_limite(self):
        self.assertEqual(proxima_hora_limite(scl(2026, 10, 17, 23, 0), "07:00"),
                         scl(2026, 10, 18, 7, 0))
        self.assertEqual(proxima_hora_limite(scl(2026, 10, 18, 6, 0), "07:00"),
                         scl(2026, 10, 18, 7, 0))

    def test_sesion_llena_espera_hasta_el_reset(self):
        reset = scl(2026, 10, 18, 1, 10)
        d = decidir(estado(sesion=93, sesion_reset=reset), AHORA, POL)
        self.assertFalse(d.claude)
        self.assertEqual(d.esperar_hasta, reset)
        self.assertIn("93%", d.motivo)

    def test_sesion_llena_que_resetea_despues_de_la_hora_limite(self):
        reset = scl(2026, 10, 18, 8, 0)
        d = decidir(estado(sesion=95, sesion_reset=reset), AHORA, POL)
        self.assertFalse(d.claude)
        self.assertIsNone(d.esperar_hasta)
        self.assertIn("hora límite", d.motivo)

    def test_el_umbral_es_configurable(self):
        pol = PoliticaCuota(umbral_sesion=0.5)
        self.assertFalse(decidir(estado(sesion=60), AHORA, pol).claude)
        self.assertTrue(decidir(estado(sesion=60), AHORA, POL).claude)

    def test_semana_sin_holgura_no_lanza_claude(self):
        # 70% usado, 6 días al reset: la reserva (48%) se come el 30%.
        d = decidir(estado(semana=70), AHORA, POL)
        self.assertFalse(d.claude)
        self.assertEqual(d.max_claude, 0)
        self.assertIsNone(d.esperar_hasta)

    def test_mas_agresivo_cerca_del_reset(self):
        lejos = decidir(estado(semana=30, semana_reset=AHORA + timedelta(days=6)),
                        AHORA, POL)
        cerca = decidir(estado(semana=30, semana_reset=AHORA + timedelta(hours=12)),
                        AHORA, POL)
        self.assertGreater(cerca.max_claude, lejos.max_claude)

    def test_mas_agresivo_con_mas_cuota_sin_usar(self):
        poco = decidir(estado(semana=40, semana_reset=AHORA + timedelta(days=2)),
                       AHORA, POL)
        mucho = decidir(estado(semana=10, semana_reset=AHORA + timedelta(days=2)),
                        AHORA, POL)
        self.assertGreater(mucho.max_claude, poco.max_claude)

    def test_la_cuenta_de_la_holgura(self):
        # 20% usado, 2 días: queda 80%, reserva 16%, holgura 64% -> 32 tickets.
        d = decidir(estado(semana=20, semana_reset=AHORA + timedelta(days=2)),
                    AHORA, POL)
        self.assertTrue(d.claude)
        self.assertEqual(d.max_claude, 32)

    def test_sin_dato_de_la_semana_no_hay_tope(self):
        d = decidir(EstadoCuota(fuente="sin dato"), AHORA, POL)
        self.assertTrue(d.claude)
        self.assertIsNone(d.max_claude)

    def test_la_estimacion_sin_sesion_igual_mira_la_semana(self):
        d = decidir(EstadoCuota(fuente="estimacion", semana_pct=95,
                                semana_reset=AHORA + timedelta(days=3)),
                    AHORA, POL)
        self.assertFalse(d.claude)


def job(issue, kind):
    return Job(repo="r", repo_path="/r", slug="o/r", issue=issue, kind=kind)


class CuotaFija:
    """Una Cuota de mentira: devuelve las decisiones en orden."""

    def __init__(self, *decisiones):
        self.decisiones = list(decisiones)
        self.consultas = 0

    def decidir(self):
        self.consultas += 1
        if len(self.decisiones) > 1:
            return self.decisiones.pop(0)
        return self.decisiones[0]


LIBRE = DecisionCuota(claude=True, max_claude=None, esperar_hasta=None,
                      motivo="semana al 10%")


class TestAplicarCuota(unittest.TestCase):
    def setUp(self):
        self.reloj = [AHORA]
        self.dormidas = []
        self.notas = []

    def ahora(self):
        return self.reloj[0]

    def dormir(self, s):
        self.dormidas.append(s)
        self.reloj[0] += timedelta(seconds=s)

    def aplicar(self, candidatos, cuota, freno=None):
        return aplicar_cuota(candidatos, cuota, ahora=self.ahora,
                             dormir=self.dormir, anotar=self.notas.append,
                             freno=freno)

    def test_sin_claude_no_se_consulta(self):
        cuota = CuotaFija(LIBRE)
        entran, fuera = self.aplicar([job(1, "pi")], cuota)
        self.assertEqual([j.issue for j in entran], [1])
        self.assertEqual(cuota.consultas, 0)

    def test_tope_de_tickets_de_claude(self):
        cuota = CuotaFija(DecisionCuota(True, 1, None, "holgura 3%"))
        entran, fuera = self.aplicar([job(1, "claude"), job(2, "pi"),
                                      job(3, "claude")], cuota)
        self.assertEqual([j.issue for j in entran], [1, 2])
        self.assertEqual([j.issue for j, _ in fuera], [3])
        self.assertIn("cuota de Claude", fuera[0][1])

    def test_sesion_llena_con_qwen_no_espera(self):
        llena = DecisionCuota(False, 0, AHORA + timedelta(hours=2), "sesión al 95%")
        entran, fuera = self.aplicar([job(1, "claude"), job(2, "pi")],
                                     CuotaFija(llena))
        self.assertEqual([j.issue for j in entran], [2])
        self.assertEqual([j.issue for j, _ in fuera], [1])
        self.assertEqual(self.dormidas, [])

    def test_sesion_llena_solo_claude_espera_el_reset_y_sigue(self):
        reset = AHORA + timedelta(hours=2)
        cuota = CuotaFija(DecisionCuota(False, 0, reset, "sesión al 95%"), LIBRE)
        entran, fuera = self.aplicar([job(1, "claude")], cuota)
        self.assertEqual([j.issue for j in entran], [1])
        self.assertEqual(fuera, [])
        self.assertGreaterEqual(self.reloj[0], reset)
        # Por tramos (para atender el freno), no un sleep de dos horas.
        self.assertGreater(len(self.dormidas), 1)
        self.assertTrue(any("espero" in n for n in self.notas))

    def test_reset_despues_de_la_hora_limite_no_espera(self):
        cuota = CuotaFija(DecisionCuota(False, 0, None,
                                        "resetea después de la hora límite"))
        entran, fuera = self.aplicar([job(1, "claude")], cuota)
        self.assertEqual(entran, [])
        self.assertEqual(self.dormidas, [])
        self.assertIn("hora límite", fuera[0][1])

    def test_el_freno_corta_la_espera(self):
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "freno"
            reset = AHORA + timedelta(hours=3)
            cuota = CuotaFija(DecisionCuota(False, 0, reset, "sesión al 95%"))

            def dormir(s):
                self.dormir(s)
                freno.write_text("")
            entran, fuera = aplicar_cuota([job(1, "claude")], cuota,
                                          ahora=self.ahora, dormir=dormir,
                                          anotar=self.notas.append,
                                          freno=str(freno))
        self.assertEqual(entran, [])
        self.assertEqual(len(self.dormidas), 1)
        self.assertEqual([j.issue for j, _ in fuera], [1])


# ---------------------------------------------------------- Cuota (fuentes)
USAGE = Usage(sesion=Bucket("Current session", 17, AHORA + timedelta(hours=5)),
              semana=Bucket("Current week (all models)", 6,
                            AHORA + timedelta(days=6)),
              por_modelo=[])


class TestCuota(unittest.TestCase):
    def test_con_usage_la_fuente_es_usage(self):
        notas = []
        c = Cuota(leer_usage=lambda: USAGE, estimar=lambda: None,
                  politica=POL, ahora=lambda: AHORA, anotar=notas.append)
        e = c.estado()
        self.assertEqual((e.fuente, e.sesion_pct, e.semana_pct), ("usage", 17, 6))
        self.assertTrue(any("/usage" in n for n in notas))

    def test_si_usage_falla_usa_la_estimacion_y_lo_dice(self):
        notas = []

        def falla():
            raise UsageIlegible("no encuentro 'Current session'")
        estimado = EstadoCuota(fuente="estimacion", semana_pct=40,
                               semana_reset=AHORA + timedelta(days=2))
        c = Cuota(leer_usage=falla, estimar=lambda: estimado, politica=POL,
                  ahora=lambda: AHORA, anotar=notas.append)
        e = c.estado()
        self.assertEqual(e.fuente, "estimacion")
        self.assertIsNone(e.sesion_pct)
        nota = " ".join(notas)
        self.assertIn("no encuentro 'Current session'", nota)
        self.assertIn("estimación", nota)

    def test_sin_usage_ni_estimacion_es_sin_dato(self):
        def falla():
            raise UsageIlegible("herdr no contesta")
        c = Cuota(leer_usage=falla, estimar=lambda: None, politica=POL,
                  ahora=lambda: AHORA, anotar=lambda s: None)
        self.assertEqual(c.estado().fuente, "sin dato")
        self.assertTrue(c.decidir().claude)

    def test_permitir_claude_es_la_costura_del_dispatcher(self):
        llena = Usage(sesion=Bucket("Current session", 96,
                                    AHORA + timedelta(hours=1)),
                      semana=USAGE.semana, por_modelo=[])
        c = Cuota(leer_usage=lambda: llena, estimar=lambda: None, politica=POL,
                  ahora=lambda: AHORA, anotar=lambda s: None)
        ok, motivo = c.permitir_claude()
        self.assertFalse(ok)
        self.assertIn("96%", motivo)


# ------------------------------------------------- statusline (rate_limits)
# Lo que Claude Code le pasa por stdin al comando de statusline (2.1.296,
# capturado en legion): `rate_limits` aparece después de la primera
# respuesta del modelo y no trae el bucket por modelo.
STDIN_STATUSLINE = {
    "session_id": "abc-123", "cwd": "/home/drokoz/Documents/Github",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
    "version": "2.1.296",
    "rate_limits": {"five_hour": {"used_percentage": 22,
                                  "resets_at": 1792296600},
                    "seven_day": {"used_percentage": 6,
                                  "resets_at": 1792789200}},
}


class _ResetsFrescos(unittest.TestCase):
    """Los `resets_at` de `STDIN_STATUSLINE` son epochs fijos del día del
    captura: pasados ellos, un archivo escrito AHORA se descarta como "de
    antes del reset" y la suite se rompe sola (time bomb). Cada test que
    compara contra el `now` real se corre con los resets recalculados de
    forma relativa (siempre a futuro): lo que se prueba es la lógica de
    frescura, no la fecha de la fixture."""

    @classmethod
    def setUpClass(cls):
        ahora = datetime.now(timezone.utc)
        cls.sesion = int((ahora + timedelta(hours=4, minutes=48)).timestamp())
        cls.semana = int((ahora + timedelta(days=4, hours=23)).timestamp())
        cls._patch = mock.patch.dict(
            STDIN_STATUSLINE["rate_limits"],
            {"five_hour": {"used_percentage": 22, "resets_at": cls.sesion},
             "seven_day": {"used_percentage": 6, "resets_at": cls.semana}})
        cls._patch.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()
        super().tearDownClass()


class TestStatusline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "harness" / "rate_limits.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_escribe_rate_limits_con_captura_y_sesion(self):
        self.assertTrue(usage.escribir_rate_limits(
            dict(STDIN_STATUSLINE), self.ruta, AHORA))
        data = json.loads(self.ruta.read_text())
        self.assertEqual(data["rate_limits"], STDIN_STATUSLINE["rate_limits"])
        self.assertEqual(data["capturado"], AHORA.isoformat())
        self.assertEqual(data["session_id"], "abc-123")
        # Escritura atómica: no queda el temporal al lado.
        self.assertEqual([p.name for p in self.ruta.parent.iterdir()],
                         ["rate_limits.json"])

    def test_sin_rate_limits_no_pisa_el_archivo_bueno(self):
        usage.escribir_rate_limits(dict(STDIN_STATUSLINE), self.ruta, AHORA)
        antes = self.ruta.read_text()
        sin = {k: v for k, v in STDIN_STATUSLINE.items() if k != "rate_limits"}
        self.assertFalse(usage.escribir_rate_limits(sin, self.ruta, AHORA))
        self.assertEqual(self.ruta.read_text(), antes)

    def test_lee_el_archivo_fresco(self):
        usage.escribir_rate_limits(dict(STDIN_STATUSLINE), self.ruta, AHORA)
        u, motivo = usage.leer_rate_limits(self.ruta, AHORA + timedelta(minutes=3),
                                           timedelta(minutes=10))
        self.assertEqual(motivo, "")
        self.assertEqual((u.sesion.pct, u.semana.pct), (22, 6))
        self.assertEqual(u.sesion.reset,
                         datetime.fromtimestamp(1792296600, SCL))
        self.assertEqual(u.semana.reset,
                         datetime.fromtimestamp(1792789200, SCL))
        self.assertEqual(str(u.sesion.reset.tzinfo), "America/Santiago")
        self.assertEqual(u.sesion.fuente, "statusline")
        self.assertEqual(u.sesion.leido, AHORA)
        self.assertEqual(u.por_modelo, [])

    def test_viejo_no_se_usa(self):
        usage.escribir_rate_limits(dict(STDIN_STATUSLINE), self.ruta, AHORA)
        u, motivo = usage.leer_rate_limits(self.ruta, AHORA + timedelta(minutes=11),
                                           timedelta(minutes=10))
        self.assertIsNone(u)
        self.assertIn("11 min", motivo)

    def test_sin_archivo_o_roto(self):
        u, motivo = usage.leer_rate_limits(self.ruta, AHORA, timedelta(minutes=10))
        self.assertIsNone(u)
        self.assertIn("no existe", motivo)
        self.ruta.parent.mkdir(parents=True)
        for roto in ("{", '{"capturado": "x"}', json.dumps({
                "capturado": AHORA.isoformat(),
                "rate_limits": {"five_hour": {"used_percentage": "mucho"}}})):
            self.ruta.write_text(roto)
            with self.subTest(roto=roto):
                u, motivo = usage.leer_rate_limits(self.ruta, AHORA,
                                                   timedelta(minutes=10))
                self.assertIsNone(u)
                self.assertTrue(motivo)


class TestEleccionDeFuente(unittest.TestCase):
    def setUp(self):
        self.fresca = Usage(Bucket("Current session", 22, AHORA, "statusline", AHORA),
                            Bucket("Current week (all models)", 6, AHORA,
                                   "statusline", AHORA))
        self.capturada = parsear_usage(fixture("usage_homeserver.txt"), AHORA)
        self.capturas = 0

    def capturar(self):
        self.capturas += 1
        return self.capturada

    def test_statusline_fresca_gana_y_no_abre_pane(self):
        u = usage.leer_cuota(lambda: (self.fresca, ""), self.capturar)
        self.assertIs(u, self.fresca)
        self.assertEqual(self.capturas, 0)

    def test_sin_statusline_captura_usage(self):
        u = usage.leer_cuota(lambda: (None, "viejo: 30 min"), self.capturar)
        self.assertIs(u, self.capturada)
        self.assertEqual(self.capturas, 1)

    def test_forzar_usage(self):
        u = usage.leer_cuota(lambda: (self.fresca, ""), self.capturar,
                             fuente="usage")
        self.assertIs(u, self.capturada)

    def test_solo_statusline_sin_archivo_es_ilegible(self):
        with self.assertRaises(UsageIlegible) as cm:
            usage.leer_cuota(lambda: (None, "no existe"), self.capturar,
                             fuente="statusline")
        self.assertIn("no existe", str(cm.exception))
        self.assertEqual(self.capturas, 0)

    def test_las_dos_fallan_dice_las_dos(self):
        def falla():
            raise UsageIlegible("claude no arrancó")
        with self.assertRaises(UsageIlegible) as cm:
            usage.leer_cuota(lambda: (None, "viejo: 30 min"), falla)
        self.assertIn("viejo: 30 min", str(cm.exception))
        self.assertIn("claude no arrancó", str(cm.exception))

    def test_as_dict_dice_fuente_y_antiguedad_por_dato(self):
        d = usage.as_dict(self.fresca, AHORA + timedelta(seconds=90))
        self.assertEqual(d["fuente"], "statusline")
        self.assertEqual(d["sesion"]["fuente"], "statusline")
        self.assertEqual(d["sesion"]["edad_s"], 90)
        self.assertEqual(d["sesion"]["leido"], AHORA.isoformat())


class TestScriptStatusline(unittest.TestCase):
    """`scripts/statusline-harness`: el comando de statusline de las
    sesiones de Claude del harness. Lee el JSON de stdin, anota la cuota y
    imprime una línea corta; nunca falla (una statusline rota no puede
    tumbar la sesión)."""

    SCRIPT = support.ROOT / "scripts" / "statusline-harness"

    def correr(self, entrada, state):
        env = dict(os.environ, XDG_STATE_HOME=state)
        return subprocess.run([str(self.SCRIPT)], input=entrada, env=env,
                              capture_output=True, text=True, timeout=30)

    def test_anota_y_muestra(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.correr(json.dumps(STDIN_STATUSLINE), d)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("22%", p.stdout)
            data = json.loads((Path(d) / "harness" / "rate_limits.json").read_text())
            self.assertEqual(data["rate_limits"]["seven_day"]["used_percentage"], 6)

    def test_entrada_basura_no_falla(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.correr("no es json", d)
            self.assertEqual(p.returncode, 0)
            self.assertFalse((Path(d) / "harness" / "rate_limits.json").exists())


# -------------------------------------------------------------- juez (#115)
class TestJuezConUsage(unittest.TestCase):
    def test_semana_con_cuota_corre_en_claude(self):
        p, motivo = decidir_peldano_juez_usage(False, 20, 10, 0.25, 0.90)
        self.assertEqual(p, JUEZ_CLAUDE)
        self.assertIn("/usage", motivo)

    def test_bajo_el_piso_corre_en_qwen(self):
        p, _ = decidir_peldano_juez_usage(False, 80, 10, 0.25, 0.90)
        self.assertEqual(p, JUEZ_QWEN)

    def test_sesion_llena_corre_en_qwen(self):
        p, motivo = decidir_peldano_juez_usage(False, 20, 95, 0.25, 0.90)
        self.assertEqual(p, JUEZ_QWEN)
        self.assertIn("5h", motivo)

    def test_forzado(self):
        p, _ = decidir_peldano_juez_usage(True, 0, 0, 0.25, 0.90)
        self.assertEqual(p, JUEZ_QWEN)


# ------------------------------------------------------------------ config
def config_con(quota):
    return parse_config({"default_context": "p", "contexts": {"p": {
        "tracker": {"kind": "github"},
        "repos": {"root": "~/r", "paths": ["a"]},
        "autonomy": "frontier",
        "budget": {"polarity": "remaining", "provider": "none"},
        "run": {"kind": "local"},
        "quota": quota}}})


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        q = config_con({}).contexts[0].quota
        self.assertEqual(q.politica(), PoliticaCuota())

    def test_valores_propios(self):
        q = config_con({"umbral_sesion": 0.8, "reserva_diaria": 0.05,
                        "costo_ticket": 0.03, "hora_limite": "06:30"}
                       ).contexts[0].quota
        self.assertEqual(q.politica(), PoliticaCuota(0.8, 0.05, 0.03, "06:30"))

    def test_edad_maxima_de_la_statusline(self):
        self.assertEqual(config_con({}).contexts[0].quota.statusline_max_edad_min, 10)
        q = config_con({"statusline_max_edad_min": 3}).contexts[0].quota
        self.assertEqual(q.statusline_max_edad_min, 3)
        with self.assertRaises(ConfigError):
            config_con({"statusline_max_edad_min": 0})

    def test_valores_invalidos(self):
        for malo in ({"umbral_sesion": 0}, {"umbral_sesion": 1.5},
                     {"reserva_diaria": -0.1}, {"costo_ticket": 0},
                     {"hora_limite": "7am"}, {"hora_limite": "25:00"}):
            with self.subTest(malo=malo), self.assertRaises(ConfigError):
                config_con(malo)


# --------------------------------------------------------------------- CLI
class TestCli(_ResetsFrescos, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        loader = SourceFileLoader("harness_cli_usage", str(HARNESS))
        spec = importlib.util.spec_from_loader("harness_cli_usage", loader)
        self.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(self.cli)
        self.env = {"HOME": str(self.d), "HARNESS_CONFIG_DIR": str(self.d),
                    "XDG_STATE_HOME": str(self.d / "state")}
        # #164: sin config el CLI se detiene; `usage` no depende de los
        # contextos, pero la config tiene que estar en el disco.
        # `provider: openrouter` es el de la config de arranque que
        # heredaban estos tests: trae el `max_edad` default de la statusline.
        (self.d / "config.json").write_text(json.dumps(
            {"default_context": "h", "contexts": {"h": {
                "tracker": {"kind": "github"},
                "repos": {"root": str(self.d / "repos"), "paths": []},
                "autonomy": "frontier",
                "budget": {"polarity": "remaining", "provider": "openrouter"},
                "run": {"kind": "local"}}}}))

    def tearDown(self):
        self.tmp.cleanup()

    def correr(self, argv, leer):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.cli, "OFFLINE", False), \
             mock.patch.object(self.cli, "_capturar_usage", leer), \
             mock.patch.dict(os.environ, self.env), \
             mock.patch("sys.stdout", new=out), mock.patch("sys.stderr", new=err):
            code = self.cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_usage_json(self):
        u = parsear_usage(fixture("usage_homeserver.txt"), AHORA)
        code, out, _ = self.correr(["usage", "--json"], lambda *a, **k: u)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["fuente"], "usage")
        self.assertEqual(data["sesion"]["pct"], 17)
        self.assertEqual(data["sesion"]["reset"], "2026-10-18T01:10:00-03:00")
        # De qué fuente vino cada dato y cuándo se leyó.
        self.assertEqual(data["sesion"]["fuente"], "usage")
        self.assertEqual(data["sesion"]["leido"], AHORA.isoformat())
        self.assertIn("edad_s", data["sesion"])
        self.assertEqual(data["semana"]["pct"], 6)
        self.assertEqual(data["por_modelo"]["Fable"]["reset"],
                         "2026-10-23T18:00:00-03:00")

    def test_usage_texto(self):
        u = parsear_usage(fixture("usage_homeserver.txt"), AHORA)
        code, out, _ = self.correr(["usage"], lambda *a, **k: u)
        self.assertEqual(code, 0)
        self.assertIn("17%", out)
        self.assertIn("Fable", out)

    def test_usage_ilegible_sale_1_con_el_motivo(self):
        def falla(*a, **k):
            raise UsageIlegible("no encuentro 'Current session'")
        code, out, err = self.correr(["usage", "--json"], falla)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("no encuentro 'Current session'", err)

    def test_con_statusline_fresca_no_abre_pane(self):
        ruta = self.d / "state" / "harness" / "rate_limits.json"
        # El reloj de la corrida es el `AHORA` fijo, no el de la máquina:
        # el reset de la sesión de la statusline (1:10am del 18 de octubre)
        # es de mañana para `AHORA` (sábado 17, 20:00), así que el dato
        # vale. Con el reloj real, pasados los 180 días del reset, el
        # archivo era "de antes del reset" y el test moría solo.
        self.assertTrue(usage.escribir_rate_limits(
            dict(STDIN_STATUSLINE), ruta, AHORA - timedelta(minutes=2)))

        class reloj_fijo(datetime):
            @classmethod
            def now(cls, tz=None):
                return (AHORA.astimezone(tz) if tz
                        else AHORA.replace(tzinfo=None))

        def no_capturar(*a, **k):
            raise AssertionError("con la statusline fresca no se abre pane")
        with mock.patch.object(self.cli, "datetime", reloj_fijo):
            code, out, _ = self.correr(["usage", "--json"], no_capturar)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["fuente"], "statusline")
        self.assertEqual(data["sesion"]["pct"], 22)
        self.assertEqual(data["sesion"]["fuente"], "statusline")
        self.assertGreaterEqual(data["sesion"]["edad_s"], 100)
        self.assertEqual(data["por_modelo"], {})

    def test_fuente_usage_fuerza_el_pane(self):
        ruta = self.d / "state" / "harness" / "rate_limits.json"
        usage.escribir_rate_limits(dict(STDIN_STATUSLINE), ruta, datetime.now(SCL))
        u = parsear_usage(fixture("usage_homeserver.txt"), AHORA)
        code, out, _ = self.correr(["usage", "--json", "--fuente", "usage"],
                                   lambda *a, **k: u)
        self.assertEqual(json.loads(out)["fuente"], "usage")
        self.assertIn("Fable", json.loads(out)["por_modelo"])

    def test_usage_offline_no_corre(self):
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(self.cli, "OFFLINE", True), \
             mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(self.cli.main(["usage"]), 2)
        self.assertIn("herdr", err.getvalue())


class TestRunConCuota(unittest.TestCase):
    """`harness run` con un ticket en el peldaño 3 (Claude): la cuota decide."""

    def setUp(self):
        from test_bucle import leer_log  # noqa: F401  (el mismo lector)
        self.leer_log = leer_log
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.log_path = self.d / "events.jsonl"
        loader = SourceFileLoader("harness_cli_run_cuota", str(HARNESS))
        spec = importlib.util.spec_from_loader("harness_cli_run_cuota", loader)
        self.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(self.cli)
        # Dos abandonos que escalan: #7 va al peldaño 3 (claude sonnet).
        lineas = [json.dumps({
            "timestamp": "2026-08-20T02:0{}:00Z".format(i),
            "contexto": "h", "origen": "harness", "run_id": "r{}".format(i),
            "ticket": "agent-harness#7", "attempt": i, "tipo": "abandono",
            "ref": "ticket/7", "cuerpo": "gate rojo", "clase": "modelo"})
            for i in (1, 2)]
        self.log_path.write_text("\n".join(lineas) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _raw(self):
        return {"offline": False, "agents": [], "contexts": [{
            "name": "h", "tracker": "github", "autonomy": "frontier",
            "vault": None, "run": "local",
            "budget": {"polarity": "remaining", "provider": "none",
                       "credits": None, "total": 0.0, "used": 0.0},
            "repos": [{
                "name": "agent-harness", "tracker": "github",
                "slug": "Drokoz/agent-harness", "branch": "main",
                "default_branch": "main", "readiness_source": "default-branch",
                "status_porcelain": "",
                "path": str(self.d / "repos" / "agent-harness"),
                "exists": {"gate": True, "skills": True, "context": True},
                "issues": [{"number": 7, "title": "t", "body": "Sin bloqueos.",
                            "labels": [{"name": "ready-for-agent"}]}],
                "prs": [], "prs_merged": [], "closed_count": 0}]}]}

    def correr(self, usage_de):
        from test_dispatch import Mundo
        mundo = Mundo(prs=[{"number": 31, "headRefName": "ticket/7"}])
        (self.d / "config.json").write_text(json.dumps({
            "default_context": "h", "contexts": {"h": {
                "tracker": {"kind": "github"},
                "repos": {"root": str(self.d / "repos"), "paths": ["agent-harness"]},
                "autonomy": "frontier",
                "budget": {"polarity": "remaining", "provider": "none"},
                "run": {"kind": "local"}}}}))
        (self.d / "repos" / "agent-harness").mkdir(parents=True)
        args = self.cli.build_parser().parse_args(
            ["run", "--context", "h", "--log", str(self.log_path), "-q"])
        with mock.patch.object(self.cli, "OFFLINE", False), \
             mock.patch.object(self.cli, "collect", return_value=self._raw()), \
             mock.patch.object(self.cli.adapters, "run", mundo.cmd), \
             mock.patch.object(self.cli, "_capturar_usage", usage_de), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None, lambda *a: None)), \
             mock.patch.dict(os.environ, {"HERDR_ENV": "1", "HOME": str(self.d),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch("time.sleep"), \
             mock.patch("sys.stdout", new=io.StringIO()) as out:
            contexto = self.cli.load_config().select("h")
            code = self.cli.run_main(args, contexto)
        return code, out.getvalue(), self.leer_log(self.log_path), mundo

    def test_semana_sin_holgura_deja_fuera_al_ticket_de_claude(self):
        ahora = datetime.now(SCL)
        u = Usage(sesion=Bucket("Current session", 10, ahora + timedelta(hours=4)),
                  semana=Bucket("Current week (all models)", 90,
                                ahora + timedelta(days=5)),
                  por_modelo=[])
        code, out, eventos, mundo = self.correr(lambda *a, **k: u)
        self.assertEqual(code, 0)
        fuera = [e for e in eventos if e["tipo"] == "fuera"]
        self.assertEqual(len(fuera), 1)
        self.assertIn("cuota de Claude", fuera[0]["cuerpo"])
        self.assertTrue(any(e["tipo"] == "cuota" and "/usage" in e["cuerpo"]
                            for e in eventos))
        self.assertFalse(any(e["tipo"] == "worktree" for e in eventos))

    def test_usage_ilegible_cae_a_la_estimacion_y_lo_anota(self):
        def falla(*a, **k):
            raise UsageIlegible("no encuentro 'Current session'")
        code, out, eventos, mundo = self.correr(falla)
        cuotas = [e["cuerpo"] for e in eventos if e["tipo"] == "cuota"]
        self.assertTrue(any("no encuentro 'Current session'" in c
                            and "estimación" in c for c in cuotas), cuotas)


# ------------------------------------- #159 revisión 1: reset ya pasado
def statusline_en(ruta, capturado, sesion_pct, sesion_reset, semana_pct=10,
                  semana_reset=None):
    """Un archivo de statusline capturado a `capturado` con esos datos."""
    semana_reset = semana_reset or capturado + timedelta(days=5)
    usage.escribir_rate_limits(
        {"session_id": "s", "rate_limits": {
            "five_hour": {"used_percentage": sesion_pct,
                          "resets_at": int(sesion_reset.timestamp())},
            "seven_day": {"used_percentage": semana_pct,
                          "resets_at": int(semana_reset.timestamp())}}},
        ruta, capturado)


class TestResetPasado(unittest.TestCase):
    """Revisión de #159 (ALTA): después del reset de la sesión de 5h, la
    statusline seguía "fresca" con el % de antes del reset, y Claude
    quedaba fuera toda la noche."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "harness" / "rate_limits.json"
        self.reloj = [scl(2026, 10, 18, 2, 0)]
        self.dormidas = []
        self.capturas = 0

    def tearDown(self):
        self.tmp.cleanup()

    def ahora(self):
        return self.reloj[0]

    def dormir(self, s):
        self.dormidas.append(s)
        self.reloj[0] += timedelta(seconds=s)

    def capturar(self):
        # /usage de verdad, después del reset: la sesión volvió a empezar.
        self.capturas += 1
        ahora = self.ahora()
        return Usage(Bucket("Current session", 3, scl(2026, 10, 18, 7, 5),
                            "usage", ahora),
                     Bucket(HEADER_SEMANA_T, 11, scl(2026, 10, 23, 18, 0),
                            "usage", ahora))

    def cuota(self):
        return Cuota(
            leer_usage=lambda: usage.leer_cuota(
                lambda: usage.leer_rate_limits(self.ruta, self.ahora(),
                                               timedelta(minutes=10)),
                self.capturar),
            estimar=lambda: None, politica=POL, ahora=self.ahora,
            anotar=lambda s: None)

    def test_la_statusline_con_el_reset_pasado_no_vale(self):
        statusline_en(self.ruta, scl(2026, 10, 18, 2, 0), 95,
                      scl(2026, 10, 18, 2, 5))
        u, motivo = usage.leer_rate_limits(self.ruta, scl(2026, 10, 18, 2, 6),
                                           timedelta(minutes=10))
        self.assertIsNone(u)
        self.assertIn("ya pasó", motivo)

    def test_decidir_ignora_una_sesion_cuyo_reset_ya_paso(self):
        e = estado(sesion=95, sesion_reset=AHORA - timedelta(minutes=1))
        self.assertTrue(decidir(e, AHORA, POL).claude)

    def test_espera_el_reset_y_relanza_con_una_fuente_valida(self):
        statusline_en(self.ruta, scl(2026, 10, 18, 2, 0), 95,
                      scl(2026, 10, 18, 2, 5))
        cuota = self.cuota()
        entran, fuera = aplicar_cuota([job(1, "claude"), job(2, "claude")],
                                      cuota, ahora=self.ahora,
                                      dormir=self.dormir, anotar=lambda s: None)
        self.assertEqual([j.issue for j in entran], [1, 2])
        self.assertEqual(fuera, [])
        # Esperó el reset más un margen, y la nueva lectura fue /usage.
        self.assertGreaterEqual(self.reloj[0], scl(2026, 10, 18, 2, 6))
        self.assertEqual(self.capturas, 1)
        # Y el chequeo por ticket ve lo mismo.
        self.assertTrue(cuota.permitir_claude()[0])


# ------------------------------- #159 revisión 2: una sola captura a la vez
class TestCacheDeCuota(unittest.TestCase):
    """Revisión de #159 (MEDIA): cada ticket de Claude en paralelo volvía a
    capturar /usage (N panes y N `claude` a la vez)."""

    def setUp(self):
        self.reloj = [AHORA]
        self.lecturas = 0
        self.lock = threading.Lock()

    def leer(self):
        with self.lock:
            self.lecturas += 1
        time.sleep(0.05)  # una captura tarda: los hilos se pisan
        return USAGE

    def cuota(self):
        return Cuota(leer_usage=self.leer, estimar=lambda: None, politica=POL,
                     ahora=lambda: self.reloj[0], anotar=lambda s: None)

    def test_varios_hilos_una_sola_captura(self):
        c = self.cuota()
        hilos = [threading.Thread(target=c.permitir_claude) for _ in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(self.lecturas, 1)

    def test_el_cache_dura_poco(self):
        c = self.cuota()
        c.estado()
        self.reloj[0] += timedelta(minutes=1)
        c.estado()
        self.assertEqual(self.lecturas, 1)
        self.reloj[0] += timedelta(minutes=2)
        c.estado()
        self.assertEqual(self.lecturas, 2)

    def test_pasar_un_reset_invalida_el_cache(self):
        u = Usage(Bucket("Current session", 95, AHORA + timedelta(seconds=30)),
                  USAGE.semana)
        c = Cuota(leer_usage=lambda: (setattr(self, "lecturas",
                                              self.lecturas + 1), u)[1],
                  estimar=lambda: None, politica=POL,
                  ahora=lambda: self.reloj[0], anotar=lambda s: None)
        c.estado()
        self.reloj[0] += timedelta(seconds=45)
        c.estado()
        self.assertEqual(self.lecturas, 2)


# ------------------------------- #159 revisión 3: la fuente real en el log
class TestFuenteEnElEstado(unittest.TestCase):
    def test_un_dato_de_la_statusline_no_se_anota_como_usage(self):
        sl = Usage(Bucket("Current session", 22, AHORA, "statusline", AHORA),
                   Bucket(HEADER_SEMANA_T, 6, AHORA, "statusline", AHORA))
        notas = []
        c = Cuota(leer_usage=lambda: sl, estimar=lambda: None, politica=POL,
                  ahora=lambda: AHORA, anotar=notas.append)
        self.assertEqual(c.estado().fuente, "statusline")
        self.assertTrue(notas[0].startswith("statusline:"), notas)


# ----------------------------- #159 revisión 4: capturado en el futuro
class TestCapturadoEnElFuturo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "harness" / "rate_limits.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_del_futuro_no_cuenta_como_fresco(self):
        usage.escribir_rate_limits(dict(STDIN_STATUSLINE), self.ruta,
                                   AHORA + timedelta(minutes=5))
        u, motivo = usage.leer_rate_limits(self.ruta, AHORA, timedelta(minutes=10))
        self.assertIsNone(u)
        self.assertIn("futuro", motivo)

    def test_un_desajuste_chico_de_reloj_se_tolera(self):
        usage.escribir_rate_limits(dict(STDIN_STATUSLINE), self.ruta,
                                   AHORA + timedelta(seconds=30))
        u, _ = usage.leer_rate_limits(self.ruta, AHORA, timedelta(minutes=10))
        self.assertIsNotNone(u)


# ------------------- #159 revisión 5: una sesión vieja no pisa a una nueva
class TestNoPisarConDatosViejos(_ResetsFrescos, unittest.TestCase):
    def test_un_reset_anterior_al_guardado_no_reemplaza(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "harness" / "rate_limits.json"
            nuevo = dict(STDIN_STATUSLINE)
            self.assertTrue(usage.escribir_rate_limits(nuevo, ruta, AHORA))
            antes = ruta.read_text()
            viejo = json.loads(json.dumps(STDIN_STATUSLINE))
            viejo["rate_limits"]["five_hour"]["resets_at"] -= 5 * 3600
            self.assertFalse(usage.escribir_rate_limits(
                viejo, ruta, AHORA + timedelta(minutes=1)))
            self.assertEqual(ruta.read_text(), antes)


# --------------------- #159 revisión 7: la hora límite es la de la corrida
class TestHoraLimiteDeLaCorrida(unittest.TestCase):
    def test_limite_de_la_corrida(self):
        self.assertEqual(usage.limite_de_corrida(scl(2026, 10, 17, 23, 0), "07:00"),
                         scl(2026, 10, 18, 7, 0))
        # Arrancó de día, pasada la hora límite: la de mañana.
        self.assertEqual(usage.limite_de_corrida(scl(2026, 10, 18, 8, 0), "07:00"),
                         scl(2026, 10, 19, 7, 0))

    def test_pasada_la_hora_limite_no_se_espera(self):
        # La corrida arrancó a las 23:00; son las 09:00 y la sesión está llena.
        limite = usage.limite_de_corrida(scl(2026, 10, 17, 23, 0), "07:00")
        ahora = scl(2026, 10, 18, 9, 0)
        e = estado(sesion=95, sesion_reset=ahora + timedelta(hours=1))
        d = decidir(e, ahora, POL, limite=limite)
        self.assertFalse(d.claude)
        self.assertIsNone(d.esperar_hasta)
        self.assertIn("hora límite", d.motivo)

    def test_la_cuota_usa_el_inicio_de_la_corrida(self):
        ahora = [scl(2026, 10, 18, 9, 0)]
        llena = Usage(Bucket("Current session", 95, ahora[0] + timedelta(hours=1)),
                      USAGE.semana)
        c = Cuota(leer_usage=lambda: llena, estimar=lambda: None, politica=POL,
                  ahora=lambda: ahora[0], anotar=lambda s: None,
                  inicio=scl(2026, 10, 17, 23, 0))
        self.assertIsNone(c.decidir().esperar_hasta)


if __name__ == "__main__":
    unittest.main()
