"""El freno corta antes de cada job, y hay hora límite dura (#175).

Noche del 2026-10-18: el freno apareció a las 07:00, pero la pasada 2 había
arrancado a las 06:28 con 14 tickets y el bucle sólo lo miraba entre
pasadas: siguió despachando hasta las 08:28. Ahora el freno (y `--hasta`)
se miran antes de despachar CADA job; pasada `hasta + 30 min`, los agentes
vivos se cortan con commit WIP (el mismo camino que el watchdog).
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import support
from harness import dispatch
from harness.config import ConfigError, parse_config
from harness.dispatch import BucleSpec, Dispatcher, EventLog, Pasada, bucle

from test_dispatch import PANE_CERO, Mundo, job, log_en, spec
from test_smoke import load_harness_offline

TZ = dispatch.TZ_SERVIDOR
TIMEOUT = '{"error":{"code":"timeout"},"id":"cli:agent:wait"}'


def leer(log):
    return [json.loads(l) for l in log.path.read_text().splitlines() if l]


def despachar(mundo, jobs, ahora=None, **kw):
    with tempfile.TemporaryDirectory() as tmp:
        log = log_en(tmp)
        d = Dispatcher(spec(**kw), log, mundo.cmd, credits=mundo.credits,
                       dormir=mundo.dormir, ahora=ahora)
        res = d.dispatch(jobs)
        lineas = leer(log)
    return res, lineas


def cortes(lineas):
    return [l["cuerpo"] for l in lineas if l["tipo"] == "corte"]


class TestFrenoAntesDeCadaJob(unittest.TestCase):
    def test_freno_tras_el_job_2_de_5_solo_se_despachan_2(self):
        """5 jobs, max 2 en paralelo, el freno aparece cuando arranca el
        segundo: los dos que ya corrían terminan, los otros 3 no arrancan."""
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "freno"
            m = Mundo(prs=[{"number": 30 + i, "headRefName": "ticket/{}".format(i)}
                           for i in range(1, 6)])
            arranques = []

            def worktree_add(args):
                with m.lock:
                    arranques.append(args)
                    if len(arranques) == 2:
                        freno.write_text("")
                return (True, "")

            m.responder(lambda a: a[0] == "git" and "worktree" in a
                        and "add" in a, worktree_add)
            res, lineas = despachar(m, [job(i) for i in range(1, 6)],
                                    max_parallel=2, freno=str(freno))
        self.assertEqual(len(arranques), 2, "no 5: el freno corta antes")
        self.assertEqual(len(res), 2)
        self.assertTrue(all(j.estado == "hecho" for j in res),
                        [j.motivo for j in res])
        (corte,) = cortes(lineas)
        self.assertIn("freno", corte)
        self.assertIn("3 job(s) sin despachar", corte)

    def test_sin_freno_se_despacha_todo(self):
        with tempfile.TemporaryDirectory() as d:
            m = Mundo()
            res, lineas = despachar(m, [job(i) for i in range(1, 4)],
                                    max_parallel=2,
                                    freno=str(Path(d) / "no-existe"))
        self.assertEqual(len(res), 3)
        self.assertEqual(cortes(lineas), [])

    def test_el_tope_de_paralelismo_se_respeta_igual(self):
        import time as _t

        def lenta(args):
            _t.sleep(0.05)
            return (True, '{"result":{"agent":{"agent_status":"idle"}}}')

        m = Mundo().responder(lambda a: a[:3] == ["herdr", "agent", "wait"],
                              lenta)
        res, _ = despachar(m, [job(i) for i in (1, 2, 3, 4)], max_parallel=2)
        self.assertEqual(len(res), 4)
        self.assertEqual(m.max_activo, 2)


class TestHasta(unittest.TestCase):
    HASTA = datetime(2026, 10, 18, 7, 0, tzinfo=TZ)

    def test_hasta_en_el_pasado_no_despacha_nada_y_anota_el_motivo(self):
        m = Mundo()
        ahora = lambda: self.HASTA + timedelta(minutes=5)  # noqa: E731
        res, lineas = despachar(m, [job(i) for i in (1, 2, 3)],
                                hasta=self.HASTA, ahora=ahora)
        self.assertEqual(res, [])
        self.assertEqual(m.llamo("herdr", "agent", "start"), [])
        self.assertEqual(m.llamo("worktree", "add"), [])
        (corte,) = cortes(lineas)
        self.assertIn("hora límite 07:00", corte)
        self.assertIn("3 job(s) sin despachar", corte)

    def test_antes_de_hasta_se_despacha(self):
        m = Mundo()
        ahora = lambda: self.HASTA - timedelta(hours=1)  # noqa: E731
        res, lineas = despachar(m, [job()], hasta=self.HASTA, ahora=ahora)
        self.assertEqual([j.estado for j in res], ["hecho"])
        self.assertEqual(cortes(lineas), [])

    def _reloj_que_salta_en_el_wait(self, m, despues):
        """El reloj marca antes de `hasta` al despachar, y `despues` a
        partir de la primera espera del agente."""
        reloj = {"t": self.HASTA - timedelta(minutes=10)}

        def wait(args):
            reloj["t"] = despues
            return (True, TIMEOUT)

        m.responder(lambda a: a[:3] == ["herdr", "agent", "wait"], wait)
        return lambda: reloj["t"]

    def test_agente_vivo_a_hasta_mas_30_se_mata_y_queda_wip(self):
        m = Mundo()
        ahora = self._reloj_que_salta_en_el_wait(
            m, self.HASTA + timedelta(minutes=31))
        m.responder(lambda a: a[0] == "git" and "status" in a,
                    (True, " M harness/dispatch.py\n"))
        res, lineas = despachar(m, [job()], hasta=self.HASTA, ahora=ahora)
        (j,) = res
        self.assertEqual(j.estado, "abandonado")
        self.assertIn("hora límite", j.motivo)
        # Se acabó la noche, no falló el modelo: no gasta peldaño.
        self.assertEqual(j.clase_abandono, "infra")
        (matada,) = m.llamo("herdr", "agent", "send-keys")
        self.assertEqual(matada[0][-1], "ctrl+c")
        self.assertTrue(m.llamo("git", "commit"), "el commit WIP")
        self.assertIn("wip", [l["tipo"] for l in lineas])
        # Lo mató la hora, no el watchdog (un solo tick de 5 min).
        self.assertNotIn("sin progreso", j.motivo)

    def test_entre_hasta_y_la_gracia_el_agente_vivo_sigue(self):
        """Pasada la hora no se arranca nada nuevo, pero el que corre tiene
        30 minutos para terminar."""
        m = Mundo()
        reloj = {"t": self.HASTA - timedelta(minutes=10)}

        def wait(args):
            reloj["t"] = self.HASTA + timedelta(minutes=10)
            if len(m.llamo("herdr", "agent", "wait")) <= 1:
                return (True, TIMEOUT)
            return (True, '{"result":{"agent":{"agent_status":"idle"}}}')

        m.responder(lambda a: a[:3] == ["herdr", "agent", "wait"], wait)
        res, _ = despachar(m, [job()], hasta=self.HASTA,
                           ahora=lambda: reloj["t"])
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertEqual(m.llamo("herdr", "agent", "send-keys"), [])


class TestResolverHasta(unittest.TestCase):
    """`--hasta HH:MM` en America/Santiago, contra el arranque de la
    corrida: la ocurrencia más cercana (±12 h)."""

    def _r(self, h, m, hhmm, dia=18):
        return dispatch.resolver_hasta(
            datetime(2026, 10, dia, h, m, tzinfo=TZ), hhmm)

    def test_arranque_01_hasta_07_es_la_misma_manana(self):
        self.assertEqual(self._r(1, 0, "07:00"),
                         datetime(2026, 10, 18, 7, 0, tzinfo=TZ))

    def test_arranque_23_hasta_07_es_la_manana_siguiente(self):
        self.assertEqual(self._r(23, 0, "07:00"),
                         datetime(2026, 10, 19, 7, 0, tzinfo=TZ))

    def test_arranque_08_hasta_07_ya_paso(self):
        """Una hora atrás no es "mañana a las 7": la corrida no despacha."""
        self.assertEqual(self._r(8, 0, "07:00"),
                         datetime(2026, 10, 18, 7, 0, tzinfo=TZ))

    def test_arranque_desde_utc_se_lleva_a_santiago(self):
        from datetime import timezone
        inicio = datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc)  # 23:00 CLST
        self.assertEqual(dispatch.resolver_hasta(inicio, "07:00"),
                         datetime(2026, 10, 19, 7, 0, tzinfo=TZ))

    def test_formato_invalido(self):
        for malo in ("7", "25:00", "07:60", "siete", ""):
            with self.assertRaises(ValueError, msg=malo):
                dispatch.resolver_hasta(datetime(2026, 10, 18, tzinfo=TZ), malo)


class TestBucleConHasta(unittest.TestCase):
    def test_hasta_corta_entre_pasadas(self):
        hasta = datetime(2026, 10, 18, 7, 0, tzinfo=TZ)
        reloj = {"t": hasta - timedelta(minutes=30)}
        n = {"pasadas": 0}

        def pasada():
            n["pasadas"] += 1
            reloj["t"] = hasta + timedelta(minutes=1)
            return Pasada(True, "1 job(s)")

        with tempfile.TemporaryDirectory() as d:
            log = EventLog(Path(d) / "ev.jsonl", "h",
                           reloj=lambda: "2026-10-18T10:00:00Z")
            corte = bucle(pasada, BucleSpec(), log, dormir=lambda s: None,
                          hasta=hasta, ahora=lambda: reloj["t"])
        self.assertEqual(n["pasadas"], 1)
        self.assertIn("hora límite 07:00", corte)


class TestConfigHasta(unittest.TestCase):
    def _conf(self, topes):
        return {"contexts": {"p": {
            "tracker": {"kind": "github"},
            "repos": {"root": "/x", "paths": []},
            "autonomy": "frontier",
            "budget": {"polarity": "remaining", "provider": "none"},
            "run": {"kind": "local"},
            "topes": topes}}}

    def test_hasta_en_topes(self):
        t = parse_config(self._conf({"hasta": "07:00"})).contexts[0].topes
        self.assertEqual(t.hasta, "07:00")

    def test_sin_hasta_es_none(self):
        self.assertIsNone(parse_config(self._conf({})).contexts[0].topes.hasta)

    def test_hasta_invalido(self):
        with self.assertRaises(ConfigError):
            parse_config(self._conf({"hasta": "7am"}))


class TestCliHasta(unittest.TestCase):
    def setUp(self):
        self.h = load_harness_offline()

    def _ctx(self, hasta=None):
        conf = TestConfigHasta()._conf({"hasta": hasta} if hasta else {})
        return parse_config(conf).contexts[0]

    def test_flag_hasta(self):
        args = self.h.build_parser().parse_args(["run", "--hasta", "06:30"])
        self.assertEqual(args.hasta, "06:30")

    def test_la_mas_temprana_entre_cli_y_config(self):
        """De noche (--loop) gana la más temprana."""
        inicio = datetime(2026, 10, 17, 23, 0, tzinfo=TZ)
        args = self.h.build_parser().parse_args(["run", "--hasta", "06:30"])
        self.assertEqual(self.h._hasta_de(args, self._ctx("07:00"), inicio,
                                           noche=True),
                         datetime(2026, 10, 18, 6, 30, tzinfo=TZ))
        args = self.h.build_parser().parse_args(["run", "--hasta", "08:00"])
        self.assertEqual(self.h._hasta_de(args, self._ctx("07:00"), inicio,
                                           noche=True),
                         datetime(2026, 10, 18, 7, 0, tzinfo=TZ))

    def test_sin_hasta_es_none(self):
        args = self.h.build_parser().parse_args(["run"])
        self.assertIsNone(self.h._hasta_de(args, self._ctx(), datetime.now(TZ)))


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------- revisión independiente
import io  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
from unittest import mock  # noqa: E402

import test_bucle  # noqa: E402
import test_proposal  # noqa: E402
from harness.dispatch import DispatchSpec  # noqa: E402
from harness.proposal import JUEZ_QWEN, Proponer  # noqa: E402


class TestProponerConFreno(unittest.TestCase):
    """El proposer de frontera vacía también lanza agentes: el freno y la
    hora límite valen antes de cada uno (generador y juez)."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.repo_path = self.d / "repos" / "fake"
        self.repo_path.mkdir(parents=True)
        self.freno = self.d / "freno"

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _correr(self, mundo, **kw):
        spec = DispatchSpec(contexto="t", kind="pi", model="qwen/qwen3.8-27b",
                            freno=str(self.freno), **kw)
        log = EventLog(self.d / "ev.jsonl", "t")
        prop = Proponer(spec, log, run_cmd=mundo.cmd)
        res = prop.proponer_repo("fake", "Drokoz/fake", str(self.repo_path),
                                 3, JUEZ_QWEN)
        return res, leer(log)

    def test_freno_puesto_el_proposer_no_lanza_agentes(self):
        self.freno.write_text("")
        mundo = test_proposal.MundoProponer(
            known_issues=[], proposals=[test_proposal._propuesta()])
        res, eventos = self._correr(mundo)
        self.assertEqual([c for c in mundo.llamadas
                          if c[0][:3] == ("herdr", "agent", "start")], [])
        self.assertEqual([e for e in eventos if e["tipo"] == "worktree"], [])
        self.assertEqual(res["abiertas"], 0)
        self.assertTrue(any("freno" in d for d in res["detalle"]),
                        res["detalle"])
        self.assertTrue(any(e["tipo"] == "corte" and "freno" in e["cuerpo"]
                            for e in eventos), eventos)

    def test_hasta_pasada_el_proposer_no_lanza_agentes(self):
        mundo = test_proposal.MundoProponer(
            known_issues=[], proposals=[test_proposal._propuesta()])
        hasta = datetime(2026, 10, 18, 7, 0, tzinfo=TZ)
        spec = DispatchSpec(contexto="t", kind="pi", hasta=hasta)
        log = EventLog(self.d / "ev.jsonl", "t")
        prop = Proponer(spec, log, run_cmd=mundo.cmd,
                        ahora=lambda: hasta + timedelta(minutes=1))
        res = prop.proponer_repo("fake", "Drokoz/fake", str(self.repo_path),
                                 3, JUEZ_QWEN)
        self.assertEqual(res["abiertas"], 0)
        self.assertTrue(any("hora límite" in d for d in res["detalle"]),
                        res["detalle"])


class TestBucleNoProponeConFreno(unittest.TestCase):
    def test_freno_tras_la_pasada_vacia_no_corre_el_proposer(self):
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "freno"
            llamados = []

            def pasada():
                freno.write_text("")
                return Pasada(False, "nada")

            log = EventLog(Path(d) / "ev.jsonl", "h")
            corte = bucle(pasada, BucleSpec(), log, freno=str(freno),
                          dormir=lambda s: None,
                          proposer_vacio=lambda: llamados.append(1) or "x")
        self.assertEqual(llamados, [])
        self.assertIn("freno", corte)

    def test_hasta_tras_la_pasada_vacia_no_corre_el_proposer(self):
        hasta = datetime(2026, 10, 18, 7, 0, tzinfo=TZ)
        reloj = {"t": hasta - timedelta(minutes=5)}
        llamados = []

        def pasada():
            reloj["t"] = hasta + timedelta(minutes=1)
            return Pasada(False, "nada")

        with tempfile.TemporaryDirectory() as d:
            log = EventLog(Path(d) / "ev.jsonl", "h")
            corte = bucle(pasada, BucleSpec(), log, dormir=lambda s: None,
                          hasta=hasta, ahora=lambda: reloj["t"],
                          proposer_vacio=lambda: llamados.append(1) or "x")
        self.assertEqual(llamados, [])
        self.assertIn("hora límite", corte)


class TestRequeueTrasElCorte(unittest.TestCase):
    def test_el_job_recolocado_tras_el_corte_cuenta_como_sin_despachar(self):
        """Un job que perdió el prompt y vuelve a la cola cuando el freno ya
        está puesto no se pierde del conteo del corte."""
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "freno"
            # El prompt nunca llega (contexto 0%): el job se recoloca, y
            # para entonces el freno ya está puesto.
            m = Mundo(pane_out=PANE_CERO)

            def prompt(args):
                freno.write_text("")
                if "8" in args[3]:
                    import time as _t
                    _t.sleep(0.2)  # 8 vuelve a la cola DESPUÉS del corte
                return (True, '{"result":{"agent":{"agent_status":"stalled"}}}')

            m.responder(lambda a: a[:3] == ["herdr", "agent", "prompt"], prompt)
            # 7 y 8 corren; 9 espera. El freno aparece con el prompt: 9 no
            # arranca, y 7 y 8 vuelven a la cola después del corte.
            res, lineas = despachar(m, [job(7), job(8), job(9)],
                                    freno=str(freno), max_parallel=2,
                                    verify_wait_s=0.01, verify_poll_s=0.01,
                                    verify_retries=1)
            self.assertTrue(any("vuelve a la cola" in l["cuerpo"]
                                for l in lineas), "se recolocó")
        (corte,) = cortes(lineas)
        self.assertIn("3 job(s) sin despachar", corte)


class TestHastaDeConfigSoloDeNoche(unittest.TestCase):
    def setUp(self):
        self.h = load_harness_offline()
        self.ctx = TestCliHasta._ctx(None, "07:00")
        self.inicio = datetime(2026, 10, 18, 14, 0, tzinfo=TZ)

    def test_run_suelto_ignora_topes_hasta(self):
        args = self.h.build_parser().parse_args(["run"])
        self.assertIsNone(self.h._hasta_de(args, self.ctx, self.inicio))

    def test_run_suelto_con_hasta_explicito_lo_usa(self):
        args = self.h.build_parser().parse_args(["run", "--hasta", "18:00"])
        self.assertEqual(self.h._hasta_de(args, self.ctx, self.inicio),
                         datetime(2026, 10, 18, 18, 0, tzinfo=TZ))

    def test_de_noche_vale_la_config(self):
        args = self.h.build_parser().parse_args(["run", "--loop"])
        self.assertEqual(
            self.h._hasta_de(args, self.ctx, self.inicio, noche=True),
            datetime(2026, 10, 18, 7, 0, tzinfo=TZ))


class TestRunSueltoDeDiaConHastaEnConfig(unittest.TestCase):
    """config `topes.hasta` 07:00, `harness run` a las 14:00 sin --hasta:
    despacha normal. La hora límite de la config es de la noche (--loop)."""

    _raw = test_bucle.LaFronteraSeRecalcula._raw
    _issue = test_bucle.LaFronteraSeRecalcula._issue

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        conf = json.loads(test_bucle.LaFronteraSeRecalcula.CONFIG)
        conf["contexts"]["h"]["topes"] = {"hasta": "07:00", "max_usd": 50}  # #165: autoriza gasto
        # Sin créditos que medir: la tanda no se recorta por presupuesto.
        conf["contexts"]["h"]["budget"] = {"polarity": "remaining",
                                           "provider": "none"}
        self.d.joinpath("config.json").write_text(json.dumps(conf))
        self.log_path = self.d / "events.jsonl"
        self.cli = load_harness_offline()

    def tearDown(self):
        self.tmp.cleanup()

    def test_despacha_normal(self):
        mundo = Mundo(prs=[{"number": 31, "headRefName": "ticket/9"}])
        args = self.cli.build_parser().parse_args([
            "run", "--context", "h", "--log", str(self.log_path)])
        cli = self.cli
        with mock.patch.object(cli, "OFFLINE", False), \
             mock.patch.object(cli, "INICIO_CORRIDA",
                               datetime(2026, 10, 18, 14, 0, tzinfo=TZ)), \
             mock.patch.object(cli, "collect", return_value=self._raw(
                 [self._issue(9, "ready-for-agent")])), \
             mock.patch.object(cli.adapters, "run", mundo.cmd), \
             mock.patch.object(cli, "_creditos_usado", return_value=None), \
             mock.patch.object(cli, "_creditos_restantes", return_value=99.0), \
             mock.patch.object(cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None, lambda *a: None)), \
             mock.patch.dict(os.environ, {"HERDR_ENV": "1", "HOME": str(self.d),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch("time.sleep"):
            self.d.joinpath("repos", "agent-harness").mkdir(parents=True)
            contexto = cli.load_config().select("h")
            with mock.patch("sys.stdout", new=io.StringIO()):
                code = cli.run_main(args, contexto)
        eventos = test_bucle.leer_log(self.log_path)
        self.assertEqual(code, 0)
        self.assertEqual([e for e in eventos if e["tipo"] == "corte"], [])
        self.assertTrue(any(e["tipo"] == "pr" and "PR #31 abierto" in e["cuerpo"]
                            for e in eventos))


class TestLoopConHastaPasada(unittest.TestCase):
    def test_loop_con_hasta_pasada_sale_2_con_mensaje(self):
        cli = load_harness_offline()
        inicio = datetime(2026, 10, 18, 8, 0, tzinfo=TZ)
        args = cli.build_parser().parse_args(["run", "--loop", "--hasta",
                                              "07:00"])
        ctx = TestCliHasta._ctx(None)
        err = io.StringIO()
        with mock.patch.object(cli, "INICIO_CORRIDA", inicio), \
             mock.patch.object(cli, "_ahora_tz",
                               return_value=inicio + timedelta(minutes=1)), \
             mock.patch("sys.stderr", new=err):
            code = cli.run_main(args, [ctx])
        self.assertEqual(code, 2)
        self.assertIn("hora límite 07:00", err.getvalue())
