"""Tests de la cuota (ticket #39) sobre .jsonl de ejemplo en tests/fixtures.

El fixture imita el layout real de `~/.claude/projects/`: una carpeta por
proyecto con el nombre del path codificado (`/` → `-`, sin puntos), y un
`<sesion>.jsonl` por sesión. El root de la config es `/u/docs`, así que
`-u-docs--worktrees-f7league-ticket-256` es un worktree del harness,
`-u-docs-koku` un repo normal y `-otro-lugar` está fuera del root.

Los timestamps cruzan el reset de semana de propósito: viernes 2026-08-21 y
2026-08-28 son viernes, y el reset es viernes 17:00 America/Santiago
(21:00 UTC en agosto, UTC-4).
"""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import support  # noqa: F401  (pone la raíz en sys.path)

from harness.quota import (PESOS, agregar, as_dict, atribuir, codificar_path,
                           cuota_por_ventana, decodificar_uso, fuentes_desde_spec,
                           leer_fuentes, leer_sesiones, parsear_linea, pico_5h,
                           ponderar, puntos_de_ticket, render_quota,
                           resumen_evento, semana_de, semana_inicio)

HARNESS = support.ROOT / "bin" / "harness"
SESIONES = support.FIXTURES / "claude_sessions"
RAIZ = [Path("/u/docs")]
UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


CONFIG_QUOTA = json.dumps({
    "default_context": "personal",
    "contexts": {
        "personal": {"tracker": {"kind": "github"},
                     "repos": {"root": "/u/docs", "paths": []},
                     "autonomy": "frontier",
                     "budget": {"polarity": "remaining", "provider": "none"},
                     "run": {"kind": "local"}},
    },
})


@contextlib.contextmanager
def correr_quota(*args, config=CONFIG_QUOTA, offline=False, projects=SESIONES):
    """El CLI `harness quota` contra el fixture, con HOME y estado aislados.
    `projects` None = sin `--projects` (la ruta la da la config o el default)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["HOME"] = tmp
        env["HARNESS_CONFIG_DIR"] = tmp
        env["XDG_STATE_HOME"] = tmp
        env["HERDR_ENV"] = "0"
        if offline:
            env["HARNESS_OFFLINE"] = "1"
        else:
            env.pop("HARNESS_OFFLINE", None)
        (Path(tmp) / "config.json").write_text(config)
        cmd = [str(HARNESS), "quota"]
        if projects is not None:
            cmd += ["--projects", str(projects)]
        yield subprocess.run(cmd + list(args), env=env, capture_output=True,
                             text=True, timeout=120)


class TestPonderar(unittest.TestCase):
    def test_pondera_por_costo_relativo(self):
        c = {"input": 100, "cache_creation": 100, "cache_read": 100, "output": 100}
        # 100*1 + 100*1.25 + 100*0.1 + 100*5 = 100 + 125 + 10 + 500
        self.assertEqual(ponderar(c), 735.0)

    def test_cache_read_pesa_una_fraccion_del_input(self):
        """El bug del ticket #54: la suma cruda trataba `cache_read` como si
        pesara lo mismo que un `input`. Ponderado, vale un décimo."""
        solo_cache_read = ponderar({"input": 0, "cache_creation": 0,
                                    "cache_read": 1000, "output": 0})
        solo_input = ponderar({"input": 100, "cache_creation": 0,
                               "cache_read": 0, "output": 0})
        self.assertEqual(solo_cache_read, solo_input)

    def test_pesos_tienen_las_claves_del_total(self):
        self.assertEqual(set(PESOS), {"input", "cache_creation",
                                      "cache_read", "output"})


class TestDecodificarUso(unittest.TestCase):
    def test_nombres_largaos_reales(self):
        """El schema real del .jsonl de Claude Code (nombres largos)."""
        u = {"input_tokens": 2, "cache_creation_input_tokens": 56826,
             "cache_read_input_tokens": 0, "output_tokens": 332,
             "output_tokens_details": {"thinking_tokens": 100}}
        self.assertEqual(decodificar_uso(u),
                         {"input": 2, "cache_creation": 56826, "cache_read": 0,
                          "output": 332, "thinking": 100, "total": 57160})

    def test_nombres_cortos_del_ticket(self):
        u = {"input": 1, "cache_creation": 2, "cache_read": 3,
             "output": 4, "thinking_tokens": 5}
        self.assertEqual(decodificar_uso(u),
                         {"input": 1, "cache_creation": 2, "cache_read": 3,
                          "output": 4, "thinking": 5, "total": 10})

    def test_cache_creation_dict_es_suma(self):
        """El schema real trae `cache_creation` como dict por duración."""
        u = {"input_tokens": 0, "output_tokens": 0,
             "cache_creation": {"ephemeral_1h_input_tokens": 100,
                                "ephemeral_5m_input_tokens": 50}}
        self.assertEqual(decodificar_uso(u)["cache_creation"], 150)

    def test_thinking_no_suma_dos_veces(self):
        """`thinking` va dentro de `output`; el total no lo duplica."""
        u = {"input_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0, "output_tokens": 1000,
             "output_tokens_details": {"thinking_tokens": 800}}
        c = decodificar_uso(u)
        self.assertEqual(c["thinking"], 800)
        self.assertEqual(c["total"], 1000)

    def test_sin_schema(self):
        self.assertIsNone(decodificar_uso(None))
        self.assertIsNone(decodificar_uso([1, 2]))


class TestParsearLinea(unittest.TestCase):
    def test_mensaje_asistente(self):
        r = parsear_linea(
            '{"type":"assistant","timestamp":"2026-08-25T10:00:00.000Z",'
            '"message":{"model":"claude-opus-5","usage":{"input_tokens":5,'
            '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
            '"output_tokens":7}}}')
        self.assertEqual(r["modelo"], "claude-opus-5")
        self.assertEqual(r["timestamp"], dt("2026-08-25T10:00:00"))
        self.assertEqual(r["tokens"]["total"], 12)

    def test_linea_de_usuario_no_cuenta(self):
        self.assertIsNone(parsear_linea(
            '{"type":"user","timestamp":"2026-08-25T10:00:00Z",'
            '"message":{"role":"user","content":"hi"}}'))

    def test_linea_rota_no_cuenta(self):
        self.assertIsNone(parsear_linea("esto no es json"))
        self.assertIsNone(parsear_linea('{"type":"assistant"'))

    def test_sin_timestamp_no_cuenta(self):
        linea = ('{"type":"assistant","message":{"model":"m",'
                 '"usage":{"output_tokens":1}}}')
        self.assertIsNone(parsear_linea(linea))


class TestAtribuir(unittest.TestCase):
    def test_worktree_con_ticket(self):
        self.assertEqual(atribuir("-u-docs--worktrees-f7league-ticket-256", RAIZ),
                         ("f7league", 256, True))

    def test_worktree_con_ticket_corto(self):
        self.assertEqual(atribuir("-u-docs--worktrees-ah-t3", RAIZ), ("ah", 3, True))

    def test_worktree_sin_ticket(self):
        self.assertEqual(atribuir("-u-docs--worktrees-prueba-confianza", RAIZ),
                         ("prueba-confianza", None, True))

    def test_repo_normal(self):
        self.assertEqual(atribuir("-u-docs-koku", RAIZ), ("koku", None, False))

    def test_fuera_del_root(self):
        self.assertEqual(atribuir("-otro-lugar", RAIZ), ("otro-lugar", None, False))

    def test_repo_anidado_no_trunca_al_ultimo_tramo(self):
        """Ticket #54: un repo anidado bajo el root (`f7league/app/calendario`,
        el caso `entrevestidos/*` de docs/harness/config.md) no debe perder su
        proyecto real y aparecer como si `calendario` fuera uno."""
        self.assertEqual(atribuir("-u-docs-f7league-app-calendario", RAIZ),
                         ("f7league-app-calendario", None, False))

    def test_codificar_path(self):
        self.assertEqual(codificar_path("/u/docs/.worktrees"), "-u-docs--worktrees")


class TestSemanaInicio(unittest.TestCase):
    def test_el_reset_es_inclusivo(self):
        """Viernes 17:00 SCL (21:00 UTC) arranca la semana."""
        self.assertEqual(semana_inicio(dt("2026-08-28T21:00:00")),
                         dt("2026-08-28T21:00:00"))

    def test_un_minuto_antes_ca_en_la_anterior(self):
        self.assertEqual(semana_inicio(dt("2026-08-28T20:59:00")),
                         dt("2026-08-21T21:00:00"))

    def test_a_media_semana(self):
        self.assertEqual(semana_inicio(dt("2026-08-25T10:00:00")),
                         dt("2026-08-21T21:00:00"))

    def test_jueves_a_la_tarde_scl(self):
        """Jueves 16:00 SCL (20:00 UTC) todavía está en la semana anterior."""
        self.assertEqual(semana_inicio(dt("2026-08-21T20:00:00")),
                         dt("2026-08-14T21:00:00"))


class TestPico5h(unittest.TestCase):
    def test_racha_dentro_de_la_ventana(self):
        v, desde, hasta = pico_5h([(dt("2026-08-25T10:00:00"), 1000),
                                   (dt("2026-08-25T12:00:00"), 2000)])
        self.assertEqual((v, desde, hasta), (3000, dt("2026-08-25T10:00:00"),
                                             dt("2026-08-25T15:00:00")))

    def test_brecha_mayor_a_5h_no_suma(self):
        v, _, _ = pico_5h([(dt("2026-08-21T15:00:00"), 500),
                           (dt("2026-08-21T21:30:00"), 700)])
        self.assertEqual(v, 700)

    def test_vacio(self):
        self.assertIsNone(pico_5h([]))


class TestLeerSesiones(unittest.TestCase):
    def test_el_fixture(self):
        regs = leer_sesiones(SESIONES)
        self.assertEqual(len(regs), 7)
        self.assertEqual({r["dir"] for r in regs},
                         {"-u-docs--worktrees-f7league-ticket-256",
                          "-u-docs-koku", "-otro-lugar"})
        self.assertEqual({r["archivo"] for r in regs},
                         {str(SESIONES / d / f) for d, f in
                          [("-u-docs--worktrees-f7league-ticket-256", "s1.jsonl"),
                           ("-u-docs-koku", "s2.jsonl"),
                           ("-otro-lugar", "s3.jsonl")]})

    def test_directorio_inexistente(self):
        self.assertEqual(leer_sesiones("/no/existe"), [])


class TestAgregar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agg = agregar(leer_sesiones(SESIONES), RAIZ)

    def test_totales(self):
        self.assertEqual(self.agg["mensajes"], 7)
        self.assertEqual(self.agg["archivos"], 3)
        self.assertEqual(self.agg["total"],
                         {"input": 750, "cache_creation": 1500,
                          "cache_read": 2250, "output": 3500,
                          "thinking": 250, "total": 8000})

    def test_ponderado(self):
        """750*1 + 1500*1.25 + 2250*0.1 + 3500*5 = 750+1875+225+17500."""
        self.assertEqual(self.agg["ponderado"], 20350.0)
        self.assertEqual(self.agg["ponderado"], ponderar(self.agg["total"]))

    def test_por_modelo(self):
        # Los componentes crudos siguen enteros (ticket #74: `--json` no
        # pierde nada), y `ponderado` es la vista que se muestra en tabla.
        self.assertEqual(self.agg["por_modelo"]["claude-opus-5"],
                         {"input": 600, "cache_creation": 1200,
                          "cache_read": 1800, "output": 2900,
                          "thinking": 250, "total": 6500, "mensajes": 3,
                          "ponderado": 16780.0})
        self.assertEqual(self.agg["por_modelo"]["fable"]["total"], 300)
        self.assertEqual(self.agg["por_modelo"]["fable"]["ponderado"], 714.0)
        self.assertEqual(self.agg["por_modelo"]["claude-sonnet-4-20250514"]["total"],
                         1200)
        self.assertEqual(
            self.agg["por_modelo"]["claude-sonnet-4-20250514"]["ponderado"],
            2856.0)

    def test_pico_5h(self):
        p = self.agg["pico_5h"]
        self.assertEqual(p["claude-opus-5"], (3500, dt("2026-08-28T22:00:00"),
                                              dt("2026-08-29T03:00:00")))
        self.assertEqual(p["claude-sonnet-4-20250514"][0], 700)
        self.assertEqual(p["fable"], (300, dt("2026-08-22T10:00:00"),
                                      dt("2026-08-22T15:00:00")))

    def test_por_semana(self):
        self.assertEqual(self.agg["por_semana"],
                         {"2026-08-14T17:00:00-04:00":
                              {"claude-sonnet-4-20250514": 500},
                          "2026-08-21T17:00:00-04:00":
                              {"claude-opus-5": 3000,
                               "claude-sonnet-4-20250514": 700,
                               "fable": 300},
                          "2026-08-28T17:00:00-04:00":
                              {"claude-opus-5": 3500}})

    def test_por_proyecto_y_ticket(self):
        p = self.agg["por_proyecto"]
        self.assertEqual(p["f7league [harness]"],
                         {"harness": True, "input": 600, "cache_creation": 1200,
                          "cache_read": 1800, "output": 2900, "thinking": 250,
                          "total": 6500, "ponderado": 16780.0,
                          "tickets": {"256": 6500},
                          "tickets_ponderado": {"256": 16780.0}})
        self.assertEqual(p["koku"]["harness"], False)
        self.assertEqual(p["koku"]["total"], 1200)
        self.assertEqual(p["otro-lugar"]["total"], 300)

    def test_harness_vs_resto(self):
        self.assertEqual(self.agg["harness"]["total"], 6500)
        self.assertEqual(self.agg["resto"]["total"], 1500)

    def test_por_semana_cuota(self):
        """Como `por_semana`, pero ponderado y partido harness/resto: lo que
        necesita el resumen de la mañana para "cuota de la semana, cuánto es
        del harness" (ticket #47)."""
        self.assertEqual(self.agg["por_semana_cuota"], {
            "2026-08-14T17:00:00-04:00": {"harness": 0.0, "resto": 1190.0},
            "2026-08-21T17:00:00-04:00": {"harness": 7140.0, "resto": 2380.0},
            "2026-08-28T17:00:00-04:00": {"harness": 9640.0, "resto": 0.0},
        })
        total = sum(v["harness"] + v["resto"]
                   for v in self.agg["por_semana_cuota"].values())
        self.assertAlmostEqual(total, self.agg["ponderado"])

    def test_por_dia(self):
        """La evolución por noche que pide el reporte HTML (ticket #47):
        fecha calendario en America/Santiago, ponderado, harness/resto."""
        self.assertEqual(self.agg["por_dia"], {
            "2026-08-21": {"harness": 0.0, "resto": 2856.0},
            "2026-08-22": {"harness": 0.0, "resto": 714.0},
            "2026-08-25": {"harness": 7140.0, "resto": 0.0},
            "2026-08-28": {"harness": 9640.0, "resto": 0.0},
        })

    def test_por_dia_vacio_sin_registros(self):
        self.assertEqual(agregar([], RAIZ)["por_dia"], {})

    def test_por_semana_ponderado(self):
        """Ticket #74: como `por_semana` pero ponderado (la vista que se
        muestra). El crudo sigue en `por_semana`, sin tocar."""
        self.assertEqual(self.agg["por_semana_ponderado"], {
            "2026-08-14T17:00:00-04:00": {"claude-sonnet-4-20250514": 1190.0},
            "2026-08-21T17:00:00-04:00": {"claude-opus-5": 7140.0,
                                          "claude-sonnet-4-20250514": 1666.0,
                                          "fable": 714.0},
            "2026-08-28T17:00:00-04:00": {"claude-opus-5": 9640.0},
        })

    def test_por_proyecto_ponderado(self):
        p = self.agg["por_proyecto"]
        self.assertEqual(p["koku"]["ponderado"], 2856.0)
        self.assertEqual(p["otro-lugar"]["ponderado"], 714.0)

    def test_la_suma_de_los_desgloses_es_el_total_ponderado(self):
        """Ticket #74: los desgloses se muestran en la misma unidad que el
        total, así que cada suma tiene que coincidir con `ponderado` —
        no dos escalas distintas en la misma pantalla."""
        self.assertEqual(sum(d["ponderado"]
                             for d in self.agg["por_modelo"].values()),
                         self.agg["ponderado"])
        self.assertAlmostEqual(
            sum(sum(m.values())
                for m in self.agg["por_semana_ponderado"].values()),
            self.agg["ponderado"])
        self.assertAlmostEqual(
            sum(d["ponderado"]
                for d in self.agg["por_proyecto"].values()),
            self.agg["ponderado"])
        self.assertAlmostEqual(
            sum(v["harness"] + v["resto"] for v in self.agg["por_dia"].values()),
            self.agg["ponderado"])

    def test_as_dict_es_serializable(self):
        texto = json.dumps(as_dict(self.agg), sort_keys=True)
        data = json.loads(texto)
        self.assertEqual(data["total"]["total"], 8000)
        self.assertEqual(data["ponderado"], 20350.0)
        self.assertEqual(data["pico_5h"]["fable"],
                         [300, "2026-08-22T10:00:00+00:00",
                          "2026-08-22T15:00:00+00:00"])


class TestSemanaDe(unittest.TestCase):
    def setUp(self):
        self.agg = agregar(leer_sesiones(SESIONES), RAIZ)

    def test_semana_con_mensajes(self):
        self.assertEqual(semana_de(self.agg, dt("2026-08-25T00:00:00")),
                         (7140.0, 2380.0))

    def test_semana_sin_mensajes_es_cero(self):
        self.assertEqual(semana_de(self.agg, dt("2027-01-01T00:00:00")),
                         (0.0, 0.0))


class TestPuntosDeTicket(unittest.TestCase):
    """Más fino que `agregar` (que sólo suma): un punto por mensaje, para
    repartir la cuota entre los peldaños de la escalada (ver
    `harness.state.pasos`, ticket #47)."""

    def setUp(self):
        self.regs = leer_sesiones(SESIONES)

    def test_los_tres_mensajes_del_ticket(self):
        pts = puntos_de_ticket(self.regs, RAIZ, "f7league", 256)
        self.assertEqual([p for _, p in pts], [2380.0, 4760.0, 9640.0])
        self.assertEqual([t for t, _ in pts],
                         [dt("2026-08-25T10:00:00"), dt("2026-08-25T12:00:00"),
                          dt("2026-08-28T22:00:00")])

    def test_proyecto_o_ticket_que_no_matchea_da_vacio(self):
        self.assertEqual(puntos_de_ticket(self.regs, RAIZ, "f7league", 999), [])
        self.assertEqual(puntos_de_ticket(self.regs, RAIZ, "otro-proyecto", 256), [])

    def test_un_repo_normal_no_tiene_ticket_nunca(self):
        self.assertEqual(puntos_de_ticket(self.regs, RAIZ, "koku", 256), [])


class TestCuotaPorVentana(unittest.TestCase):
    def test_suma_lo_que_cae_adentro(self):
        pts = [(dt("2026-08-25T10:00:00"), 100.0), (dt("2026-08-25T12:00:00"), 200.0),
               (dt("2026-08-28T22:00:00"), 400.0)]
        self.assertEqual(cuota_por_ventana(pts, dt("2026-08-25T09:00:00"),
                                           dt("2026-08-25T13:00:00")), 300.0)

    def test_bordes_inclusive(self):
        pts = [(dt("2026-08-25T10:00:00"), 100.0)]
        self.assertEqual(cuota_por_ventana(pts, dt("2026-08-25T10:00:00"),
                                           dt("2026-08-25T10:00:00")), 100.0)

    def test_sin_borde_no_filtra_por_ese_lado(self):
        pts = [(dt("2026-08-21T00:00:00"), 10.0), (dt("2026-08-29T00:00:00"), 20.0)]
        self.assertEqual(cuota_por_ventana(pts, dt("2026-08-25T00:00:00"), None), 20.0)
        self.assertEqual(cuota_por_ventana(pts, None, dt("2026-08-25T00:00:00")), 10.0)
        self.assertEqual(cuota_por_ventana(pts, None, None), 30.0)

    def test_vacio(self):
        self.assertEqual(cuota_por_ventana([], dt("2026-08-25T00:00:00"), None), 0.0)


class TestRender(unittest.TestCase):
    def test_tabla_corta(self):
        texto = render_quota(agregar(leer_sesiones(SESIONES), RAIZ))
        self.assertIn("total ponderado 20,350", texto)
        self.assertIn("suma cruda 8,000", texto)
        self.assertIn("cache_read 2,250", texto)
        self.assertIn("claude-opus-5", texto)
        self.assertIn("fable", texto)
        self.assertIn("[harness]", texto)
        self.assertIn("ticket 256", texto)
        self.assertIn("2026-08-21", texto)
        # Ticket #74: los desgloses van ponderados, en la misma unidad que
        # el total —los crudos se quedaron abajo del total y en `--json`.
        self.assertIn("16,780", texto)      # opus ponderado
        self.assertIn("2,856", texto)       # sonnet ponderado
        self.assertIn("9,520", texto)       # semana 08-21 ponderada
        self.assertIn("9,640", texto)       # semana y día 08-28
        self.assertIn("harness 16,780", texto)
        self.assertNotIn("6,500", texto)    # el crudo de opus no está en la tabla
        # Sin constante de calibración no se inventa porcentaje: se dicen
        # los tokens y se dice que falta calibrar.
        self.assertIn("sin calibración", texto)
        self.assertNotIn("del tope", texto)
        self.assertNotIn("\033", texto)

    def test_tabla_calibrada_muestra_el_pct_del_tope(self):
        """Ticket #74: con `tope_semanal` cada corte muestra además su
        porcentaje estimado del tope semanal."""
        texto = render_quota(agregar(leer_sesiones(SESIONES), RAIZ),
                             tope_semanal=100000)
        self.assertIn("9.5% del tope", texto)   # semana 08-21: 9,520/100,000
        self.assertIn("9.6% del tope", texto)   # semana/día 08-28: 9,640
        self.assertIn("1.2% del tope", texto)   # semana 08-14: 1,190
        self.assertNotIn("sin calibración", texto)

    def test_por_dia_muestra_solo_los_ultimos_diez(self):
        """Ticket #74: el corte por día se limita a los últimos 10 días."""
        def registro(dia):
            return {"modelo": "m",
                    "timestamp": datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
                    + timedelta(days=dia),
                    "tokens": {"input": 100, "cache_creation": 0,
                               "cache_read": 0, "output": 0, "thinking": 0,
                               "total": 100},
                    "dir": "-u-docs-koku", "archivo": "a.jsonl"}
        regs = [registro(i) for i in range(12)]  # 2026-09-01 .. 2026-09-12
        texto = render_quota(agregar(regs, RAIZ))
        self.assertIn("\n    2026-09-12 ", texto)
        self.assertIn("\n    2026-09-03 ", texto)
        self.assertNotIn("\n    2026-09-02 ", texto)
        self.assertNotIn("\n    2026-09-01 ", texto)
        # El dato no se trunca: `--json` sigue trayendo los 12 días.
        self.assertEqual(len(agregar(regs, RAIZ)["por_dia"]), 12)

    def test_resumen_evento(self):
        linea = resumen_evento(agregar(leer_sesiones(SESIONES), RAIZ))
        self.assertIn("total 20350 tokens ponderados (8000 crudo)", linea)
        self.assertIn("harness=6500", linea)
        self.assertLessEqual(len(linea.split("; ")), 6)


class TestCli(unittest.TestCase):
    def test_json_es_el_agregado_crudo(self):
        with correr_quota("--json") as p:
            self.assertEqual(p.returncode, 0, p.stderr)
            data = json.loads(p.stdout)
        self.assertEqual(data["total"]["total"], 8000)
        self.assertEqual(data["ponderado"], 20350.0)
        self.assertEqual(data["por_modelo"]["fable"]["total"], 300)
        self.assertEqual(data["pico_5h"]["claude-opus-5"][0], 3500)
        self.assertEqual(data["harness"]["total"], 6500)

    def test_tabla_sin_tty(self):
        with correr_quota() as p:
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("Cuota · sesiones locales", p.stdout)
            self.assertNotIn("\033", p.stdout)

    def test_config_calibrada_dibuja_el_pct_del_tope(self):
        """Ticket #74: la constante de calibración vive en config, no en el
        código: con `quota.tope_semanal` la tabla lleva el % del tope."""
        config = json.loads(CONFIG_QUOTA)
        config["contexts"]["personal"]["quota"] = {"tope_semanal": 100000}
        with correr_quota(config=json.dumps(config)) as p:
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("9.5% del tope", p.stdout)
            self.assertNotIn("sin calibración", p.stdout)

    def test_escribe_un_evento_por_corrida(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["HOME"] = tmp
            env["HARNESS_CONFIG_DIR"] = tmp
            env["XDG_STATE_HOME"] = tmp
            env["HERDR_ENV"] = "0"
            env.pop("HARNESS_OFFLINE", None)
            (Path(tmp) / "config.json").write_text(CONFIG_QUOTA)
            p = subprocess.run([str(HARNESS), "quota",
                                "--projects", str(SESIONES)],
                               env=env, capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            log = Path(tmp) / "harness" / "events.jsonl"
            lineas = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual(len(lineas), 1)
        e = lineas[0]
        self.assertEqual(e["tipo"], "quota")
        self.assertEqual(e["origen"], "harness")
        self.assertIn("total 20350 tokens ponderados (8000 crudo)", e["cuerpo"])
        self.assertTrue(e["timestamp"].endswith("Z"))

    def test_offline_no_escribe_eventos(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["HOME"] = tmp
            env["HARNESS_CONFIG_DIR"] = tmp
            env["XDG_STATE_HOME"] = tmp
            env["HARNESS_OFFLINE"] = "1"
            (Path(tmp) / "config.json").write_text(CONFIG_QUOTA)
            p = subprocess.run([str(HARNESS), "quota",
                                "--projects", str(SESIONES)],
                               env=env, capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse((Path(tmp) / "harness").exists())

    def test_sin_sesiones_da_cero(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["HOME"] = tmp
            env["HARNESS_CONFIG_DIR"] = tmp
            env["HARNESS_OFFLINE"] = "1"
            (Path(tmp) / "config.json").write_text(CONFIG_QUOTA)
            p = subprocess.run([str(HARNESS), "quota",
                                "--projects", str(Path(tmp) / "vacio"),
                                "--json"],
                               env=env, capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout)["total"]["total"], 0)

    def test_help_documenta_las_limitaciones(self):
        p = subprocess.run([str(HARNESS), "--help"], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0)
        self.assertIn("aproximado", p.stdout)
        self.assertIn("otros dispositivos", p.stdout)
        self.assertIn("otros usuarios", p.stdout)


class TestFuentes(unittest.TestCase):
    """Multiusuario (#75): dos directorios de sesiones legibles y uno
    ilegible. El ilegible no rompe: se salta y se sabe cuál y por qué."""

    LINEA = ('{"type":"assistant","timestamp":"2026-08-25T10:00:00.000Z",'
             '"message":{"model":"claude-opus-5","usage":{"input_tokens":1,'
             '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
             '"output_tokens":1}}}')

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.a = base / "a"
        cls.b = base / "b"
        (cls.a / "-u-docs-koku").mkdir(parents=True)
        (cls.a / "-u-docs-koku" / "s.jsonl").write_text(cls.LINEA + "\n")
        (cls.b / "-otro-lugar").mkdir(parents=True)
        (cls.b / "-otro-lugar" / "s.jsonl").write_text(cls.LINEA + "\n")
        cls.inlegible = base / "inlegible"
        cls.inlegible.mkdir()
        cls.inlegible.chmod(0o000)

    @classmethod
    def tearDownClass(cls):
        cls.inlegible.chmod(0o755)
        cls._tmp.cleanup()

    def test_dos_directorios_y_uno_inlegible(self):
        regs, fuentes = leer_fuentes([self.a, self.b, self.inlegible])
        self.assertEqual(len(regs), 2)  # los legibles se leen igual
        self.assertEqual([f["ruta"] for f in fuentes],
                         [str(self.a), str(self.b), str(self.inlegible)])
        self.assertEqual([f["ok"] for f in fuentes], [True, True, False])
        self.assertIn("permiso", fuentes[2]["error"])

    def test_inexistente_no_rompe(self):
        regs, fuentes = leer_fuentes(["/no/existe-en-ningun-lado"])
        self.assertEqual(regs, [])
        self.assertFalse(fuentes[0]["ok"])
        self.assertIn("no existe", fuentes[0]["error"])

    def test_archivo_no_es_fuente(self):
        (self.a / "no-es-directorio.txt").write_text("no")
        regs, fuentes = leer_fuentes([self.a / "no-es-directorio.txt"])
        self.assertEqual(regs, [])
        self.assertFalse(fuentes[0]["ok"])
        self.assertIn("no es un directorio", fuentes[0]["error"])

    def test_ruta_repetida_cuenta_una_vez(self):
        regs, fuentes = leer_fuentes([str(self.a), str(self.a)])
        self.assertEqual(len(regs), 1)
        self.assertEqual(len(fuentes), 1)

    def test_fuentes_desde_spec(self):
        self.assertEqual(
            fuentes_desde_spec("{0}, {1} ,{0},".format(self.a, self.b)),
            [str(self.a), str(self.b)])
        self.assertEqual(fuentes_desde_spec(""), [])
        self.assertEqual(fuentes_desde_spec("  ,  "), [])


class TestRenderFuentes(unittest.TestCase):
    """Con fuentes faltantes el total se marca como piso (≥), no como
    medición (#75)."""

    @classmethod
    def setUpClass(cls):
        cls.regs = leer_sesiones(SESIONES)

    def test_completa_no_marca_piso(self):
        texto = render_quota(agregar(self.regs, RAIZ, fuentes=[
            {"ruta": str(SESIONES), "ok": True, "error": None}]))
        self.assertIn("fuentes: 1/1 leídas", texto)
        self.assertNotIn("≥", texto)

    def test_parcial_marca_el_piso_y_la_fuente_faltante(self):
        texto = render_quota(agregar(self.regs, RAIZ, fuentes=[
            {"ruta": str(SESIONES), "ok": True, "error": None},
            {"ruta": "/Users/tomasherceg/.claude/projects", "ok": False,
             "error": "no se puede acceder (permiso denegado)"}]))
        self.assertIn("fuentes: 1/2 leídas", texto)
        self.assertIn("falta /Users/tomasherceg/.claude/projects: "
                      "no se puede acceder (permiso denegado)", texto)
        self.assertIn("total ponderado ≥ 20,350", texto)
        self.assertIn("piso", texto)

    def test_sin_fuentes_el_render_no_cambia(self):
        """Los agregados viejos (sin `fuentes`) se dibujan sin piso."""
        texto = render_quota(agregar(self.regs, RAIZ))
        self.assertNotIn("≥", texto)
        self.assertNotIn("fuentes:", texto)

    def test_completa_en_el_json(self):
        agg = agregar(self.regs, RAIZ, fuentes=[
            {"ruta": str(SESIONES), "ok": True, "error": None},
            {"ruta": "/x", "ok": False, "error": "no existe"}])
        data = json.loads(json.dumps(as_dict(agg), sort_keys=True))
        self.assertFalse(data["completa"])
        self.assertEqual(data["fuentes"],
                         [{"ruta": str(SESIONES), "ok": True, "error": None},
                          {"ruta": "/x", "ok": False, "error": "no existe"}])
        # El dato medido no cambia: el piso marca la lectura, no el número.
        self.assertEqual(data["total"]["total"], 8000)

    def test_resumen_evento_marca_el_piso(self):
        linea = resumen_evento(agregar(self.regs, RAIZ, fuentes=[
            {"ruta": str(SESIONES), "ok": True, "error": None},
            {"ruta": "/x", "ok": False, "error": "no existe"}]))
        self.assertIn("piso", linea)
        self.assertIn("1 de 2", linea)


class TestCliFuentes(unittest.TestCase):
    """El CLI: `--projects` por coma y la lista declarada en config (#75)."""

    def _inlegible(self, tmp):
        d = Path(tmp) / "inlegible"
        d.mkdir()
        d.chmod(0o000)
        return d

    def test_projects_separados_por_coma(self):
        with tempfile.TemporaryDirectory() as tmp:
            inlegible = str(self._inlegible(tmp))
            with correr_quota(projects="{0},{1}".format(SESIONES, inlegible)) as p:
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("fuentes: 1/2 leídas", p.stdout)
                self.assertIn("falta {}: no se puede acceder".format(inlegible),
                              p.stdout)
                self.assertIn("total ponderado ≥", p.stdout)

    def test_json_marca_completa_falso(self):
        with tempfile.TemporaryDirectory() as tmp:
            inlegible = str(self._inlegible(tmp))
            with correr_quota("--json",
                              projects="{0},{1}".format(SESIONES, inlegible)) as p:
                self.assertEqual(p.returncode, 0, p.stderr)
                data = json.loads(p.stdout)
        self.assertFalse(data["completa"])
        faltantes = [f for f in data["fuentes"] if not f["ok"]]
        self.assertEqual(len(faltantes), 1)
        self.assertEqual(faltantes[0]["ruta"], inlegible)
        self.assertEqual(data["total"]["total"], 8000)

    def test_todas_las_fuentes_legibles_es_completa(self):
        with correr_quota("--json") as p:
            self.assertEqual(p.returncode, 0, p.stderr)
            data = json.loads(p.stdout)
        self.assertTrue(data["completa"])
        self.assertTrue(all(f["ok"] for f in data["fuentes"]))

    def test_config_declara_la_lista(self):
        """Sin `--projects`: la lista de fuentes viene de `quota.projects`."""
        config = json.loads(CONFIG_QUOTA)
        config["contexts"]["personal"]["quota"] = {
            "projects": [str(SESIONES), "/no/existe/en/ningun/lado"]}
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["HOME"] = tmp
            env["HARNESS_CONFIG_DIR"] = tmp
            env["XDG_STATE_HOME"] = tmp
            env["HARNESS_OFFLINE"] = "1"
            (Path(tmp) / "config.json").write_text(json.dumps(config))
            p = subprocess.run([str(HARNESS), "quota"], env=env,
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("fuentes: 1/2 leídas", p.stdout)
        self.assertIn("falta /no/existe/en/ningun/lado: no existe", p.stdout)
        self.assertIn("total ponderado ≥", p.stdout)

    def test_proyectos_por_coma_gana_sobre_config(self):
        config = json.loads(CONFIG_QUOTA)
        config["contexts"]["personal"]["quota"] = {
            "projects": [str(SESIONES), "/no/existe/en/ningun/lado"]}
        with correr_quota("--json", config=json.dumps(config)) as p:
            self.assertEqual(p.returncode, 0, p.stderr)
            data = json.loads(p.stdout)
        # Gana `--projects` (la SESIONES sola): no hay faltantes.
        self.assertTrue(data["completa"])
        self.assertEqual([f["ruta"] for f in data["fuentes"]], [str(SESIONES)])


if __name__ == "__main__":
    unittest.main()
