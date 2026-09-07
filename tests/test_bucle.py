"""El modo `--loop` (#88): la pasada se repite hasta que corta algo.

Ningún bucle sin límite: el corte es siempre por el piso de presupuesto,
el tope de pasadas con trabajo, el archivo de freno, o la espera agotada
con la frontera vacía. El mundo y el reloj se inyectan: el bucle se testea
sin esperar de verdad, y cada pasada queda en el log con su número y el
corte con su motivo.

Aquí se testea también lo que el bucle de `noche.sh` no podía: la frontera
se recalcula entre pasadas (un ticket que se parkeó deja de entrar, y uno
que se destrabó entra), corriendo la `run_main` de verdad con el mundo de
mentira.
"""

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

import support
from harness.dispatch import BucleSpec, EventLog, Pasada, bucle

from test_dispatch import Mundo

HARNESS = support.ROOT / "bin" / "harness"


def leer_log(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l]


def correr(resultados, creditos=None, al_largo=None, freno=None,
           vigilar=None, reloj=None, **spec):
    """Corre el bucle con un mundo de mentira. Devuelve
    `(corte, eventos, dormidos, nº de pasadas)`.

    `resultados` es la lista de qué encuentra cada pasada ("trabajo" o
    "vacia"); más allá del final se repite la última. `creditos` es la
    lista de lecturas de crédito (una por iteración del bucle; `None` =
    sin medir); `al_largo(i)` es un efecto de lado en la pasada i;
    `dormir` es un registro, no una espera; `vigilar` es el "¿cambió
    algo?" barato de #114 (False = no barrer); `reloj` fija el reloj del
    log para los "desde las HH:MM".
    """
    with tempfile.TemporaryDirectory() as d:
        log_path = Path(d) / "events.jsonl"
        log = EventLog(log_path, "harness", reloj=reloj)
        dormidos = []
        estado = {"n": 0}

        def pasada():
            i = estado["n"]
            estado["n"] += 1
            if al_largo:
                al_largo(i)
            r = resultados[i] if i < len(resultados) else \
                (resultados[-1] if resultados else "vacia")
            if r == "trabajo":
                return Pasada(True, "1 job(s)")
            return Pasada(False, "nada en la frontera")

        creditos_fn = None
        if creditos is not None:
            i = {"n": 0}

            def creditos_fn():
                v = creditos[i["n"]] if i["n"] < len(creditos) else creditos[-1]
                i["n"] += 1
                return None if v is None else float(v)

        spec_obj = BucleSpec(**spec) if spec else BucleSpec()
        corte = bucle(pasada, spec_obj, log, creditos=creditos_fn, freno=freno,
                      dormir=dormidos.append, vigilar=vigilar)
        return corte, leer_log(log_path), dormidos, estado["n"]


class ElBucle(unittest.TestCase):
    def test_corta_por_tope_de_pasadas_con_trabajo(self):
        corte, _, dormidos, n = correr(["trabajo"], max_pasadas=3)
        self.assertEqual(n, 3, "nunca un bucle sin límite")
        self.assertIn("tope", corte)
        self.assertEqual(dormidos, [60.0] * 3)

    def test_frontera_vacia_espera_en_vez_de_cortar(self):
        """#101: la frontera vacía no es el fin de la noche, es una espera.
        #114: la espera se alarga sola, 300s doblado por vuelta."""
        corte, _, dormidos, n = correr(["vacia"], vueltas_vacias_max=3)
        self.assertEqual(n, 3)
        self.assertIn("nada nuevo", corte)
        self.assertEqual(dormidos, [300.0, 600.0, 1200.0],
                         "la espera vacía es la larga y sube sola")

    def test_una_pasada_vacia_no_gasta_turno_de_trabajo(self):
        corte, _, _, n = correr(["vacia"], max_pasadas=1, vueltas_vacias_max=4)
        self.assertEqual(n, 4, "las vacías cuentan contra su propio tope")
        self.assertNotIn("tope", corte)
        self.assertIn("nada nuevo", corte)

    def test_el_trabajo_reinicia_el_contador_de_vacias(self):
        corte, _, _, n = correr(
            ["vacia", "vacia", "trabajo", "vacia", "vacia", "vacia"],
            max_pasadas=5, vueltas_vacias_max=3)
        self.assertEqual(n, 6)
        self.assertIn("nada nuevo", corte)

    def test_el_reloj_es_el_inyectado(self):
        """Trabajo -> espera corta; vacía -> espera larga. Sin reloj real."""
        corte, _, dormidos, n = correr(
            ["trabajo", "vacia"], max_pasadas=2, vueltas_vacias_max=1)
        self.assertEqual(n, 2)
        self.assertEqual(dormidos, [60.0, 300.0])

    def test_piso_bajo_no_arranca(self):
        corte, _, dormidos, n = correr(["trabajo"], creditos=[2.5], piso=3.0)
        self.assertEqual(n, 0)
        self.assertEqual(dormidos, [])
        self.assertIn("piso", corte)

    def test_piso_corta_entre_pasadas(self):
        corte, _, dormidos, n = correr(
            ["trabajo"], creditos=[99.0, 2.5], piso=3.0)
        self.assertEqual(n, 1)
        self.assertIn("piso", corte)
        self.assertIn("2.50", corte)

    def test_piso_corta_tambien_mientras_espera(self):
        corte, _, _, n = correr(
            ["vacia"], creditos=[99.0, 2.5], piso=3.0, vueltas_vacias_max=9)
        self.assertEqual(n, 1)
        self.assertIn("piso", corte)
        self.assertNotIn("nada nuevo", corte)

    def test_creditos_sin_medir_no_corta_por_piso(self):
        """Sin crédito para medir, el piso no corta: los demás cortes sí."""
        corte, _, _, n = correr(["vacia"], creditos=[None], vueltas_vacias_max=2)
        self.assertEqual(n, 2)
        self.assertIn("nada nuevo", corte)

    def test_freno_corta_entre_pasadas(self):
        """El freno aparece mientras termina la pasada 1: se atiende antes de
        la 2, no en medio de ninguna."""
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "freno"

            def al_largo(i):
                if i == 0:
                    freno.write_text("")

            corte, _, dormidos, n = correr(
                ["vacia"], freno=str(freno), al_largo=al_largo,
                vueltas_vacias_max=9)
        self.assertEqual(n, 1)
        self.assertIn("freno", corte)
        self.assertIn(str(freno), corte)

    def test_freno_puesto_al_arrancar_no_corre_nada(self):
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "freno"
            freno.write_text("")
            corte, _, dormidos, n = correr(["trabajo"], freno=str(freno))
        self.assertEqual(n, 0)
        self.assertEqual(dormidos, [])
        self.assertIn("freno", corte)

    def test_cada_pasada_con_trabajo_queda_en_el_log_con_su_numero(self):
        corte, eventos, _, n = correr(
            ["trabajo", "vacia"], max_pasadas=2, vueltas_vacias_max=2)
        self.assertEqual(n, 3)
        por_pasada = [e for e in eventos if e["tipo"] == "pasada"]
        # #114: la pasada vacía no deja línea de `pasada`: su racha queda
        # en eventos de `espera` (una por subida de la espera).
        self.assertEqual([e["ref"] for e in por_pasada], ["bucle/1"])
        self.assertIn("pasada 1", por_pasada[0]["cuerpo"])
        esperas = [e for e in eventos if e["tipo"] == "espera"]
        self.assertEqual(len(esperas), 2, "una por subida de la espera")
        self.assertEqual(esperas[0]["ref"], "bucle")

    def test_la_espera_vacia_se_alarga_sola_con_techo(self):
        """#114: 5m, 10m, 20m, 30m y más 30m; el techo es configurable."""
        corte, _, dormidos, n = correr(["vacia"], vueltas_vacias_max=6)
        self.assertEqual(n, 6)
        self.assertEqual(dormidos, [300.0, 600.0, 1200.0, 1800.0, 1800.0,
                                    1800.0])
        corte, _, dormidos, n = correr(
            ["vacia"], vueltas_vacias_max=4, espera_vacia=60.0,
            espera_vacia_max=90.0)
        self.assertEqual(dormidos, [60.0, 90.0, 90.0, 90.0])

    def test_el_trabajo_corta_el_backoff(self):
        """#114: la racha vacía se reinicia con trabajo: la espera baja."""
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "freno"

            def al_largo(i):
                if i == 3:
                    freno.write_text("")
            corte, _, dormidos, n = correr(
                ["vacia", "vacia", "trabajo", "vacia"], max_pasadas=5,
                vueltas_vacias_max=9, freno=str(freno), al_largo=al_largo)
            self.assertEqual(n, 4)
            self.assertEqual(dormidos, [300.0, 600.0, 60.0, 300.0])

    def test_las_vueltas_vacias_se_colapsan_en_pocas_lineas(self):
        """#114: 55 vueltas sin novedades no son 55 líneas: una por cada
        subida de la espera, con la hora en que empezó la racha."""
        corte, eventos, dormidos, n = correr(
            ["vacia"], vueltas_vacias_max=55,
            reloj=lambda: "2026-09-06T02:36:37Z")
        self.assertEqual(n, 55)
        esperas = [e for e in eventos if e["tipo"] == "espera"]
        self.assertEqual(len(esperas), 4,
                         "300s, 600s, 1200s y techo: una por subida")
        for e in esperas:
            self.assertIn("esperando desde las 02:36", e["cuerpo"])
        self.assertIn("1 vueltas, espero 300s", esperas[0]["cuerpo"])
        self.assertIn("4 vueltas, espero 1800s", esperas[3]["cuerpo"])
        self.assertEqual(len([e for e in eventos if e["tipo"] == "pasada"]),
                         0, "la vuelta vacía no deja línea de pasada")

    def test_el_corte_resume_vueltas_vacias_y_tiempo_esperado(self):
        """#114: el final del log dice cuántas vueltas vacías hubo y cuánto
        se esperó, en la misma línea del motivo."""
        corte, eventos, _, n = correr(["vacia"], vueltas_vacias_max=60)
        self.assertEqual(n, 60)
        fin = [e for e in eventos if e["tipo"] == "bucle"][-1]
        self.assertIn("nada nuevo en 60 vueltas", fin["cuerpo"])
        # 300 + 600 + 1200 + 57*1800 = 104700s = 29h 5m.
        self.assertIn("60 vueltas, 29h 5m esperados", fin["cuerpo"])
        corte, eventos, _, _ = correr(
            ["vacia"], creditos=[99.0, 2.5], piso=3.0, vueltas_vacias_max=9)
        fin = [e for e in eventos if e["tipo"] == "bucle"][-1]
        self.assertIn("piso", fin["cuerpo"])
        self.assertIn("1 vueltas, 5m 0s esperados", fin["cuerpo"])

    def test_vigilar_falso_no_barrera(self):
        """#114: '¿cambió algo?' en False no dispara el barrido completo,
        pero la vuelta cuenta igual como vacía: el tope corta igual."""
        vueltas_vigilar = []
        def vigilar():
            vueltas_vigilar.append(1)
            return False
        corte, eventos, dormidos, n = correr(
            ["vacia"], vueltas_vacias_max=3, vigilar=vigilar)
        self.assertEqual(n, 0, "sin cambio no hay barrido completo")
        self.assertEqual(len(vueltas_vigilar), 3)
        self.assertEqual(dormidos, [300.0, 600.0, 1200.0])
        self.assertIn("nada nuevo", corte)
        fin = [e for e in eventos if e["tipo"] == "bucle"][-1]
        self.assertIn("3 vueltas, 35m 0s esperados", fin["cuerpo"])

    def test_vigilar_fallo_barrera_y_deja_lado_en_el_log(self):
        """#114: sin dato no hay forma de afirmar que no cambió: se barra
        completo y la falla queda anotada (el silencio no es confirmación)."""
        def vigilar():
            raise RuntimeError("sin red")
        corte, eventos, dormidos, n = correr(
            ["vacia"], vueltas_vacias_max=1, vigilar=vigilar)
        self.assertEqual(n, 1, "el fallo no impide el barrido")
        vig = [e for e in eventos if e["tipo"] == "vigilar"]
        self.assertEqual(len(vig), 1)
        self.assertIn("sin red", vig[0]["cuerpo"])
        self.assertIn("barrio completo", vig[0]["cuerpo"])

    def test_el_corte_queda_en_el_log_con_su_motivo(self):
        for resultados, creditos, spec, motivo_esperada in (
                (["trabajo"], None, {"max_pasadas": 1}, "tope"),
                (["vacia"], None, {"vueltas_vacias_max": 1}, "nada nuevo"),
                (["vacia"], [2.5], {"piso": 3.0}, "piso")):
            with self.subTest(motivo=motivo_esperada):
                corte, eventos, _, _ = correr(resultados, creditos=creditos,
                                              **spec)
                fin = [e for e in eventos if e["tipo"] == "bucle"][-1]
                self.assertEqual(fin["ref"], "bucle")
                self.assertIn("corte: ", fin["cuerpo"])
                self.assertIn(motivo_esperada, fin["cuerpo"])


class ElCliFreno(unittest.TestCase):
    """El freno viejo del CLI: no puede salir con código 0 (#91) — se leería
    como "salió bien" y se comería la noche."""

    CONTEXTO = json.dumps({
        "default_context": "h",
        "contexts": {
            "h": {"tracker": {"kind": "github"},
                  "repos": {"root": "/no/existe", "paths": []},
                  "autonomy": "frontier",
                  "budget": {"polarity": "remaining", "provider": "none"},
                  "run": {"kind": "local"}},
        },
    })

    def _run(self, args, config=None):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["HOME"] = tmp
            env["HARNESS_CONFIG_DIR"] = tmp
            env["HARNESS_OFFLINE"] = "1"
            env.pop("HERDR_ENV", None)
            (Path(tmp) / "config.json").write_text(config or self.CONTEXTO)
            return subprocess.run([str(HARNESS), *args], env=env,
                                  capture_output=True, text=True, timeout=120)

    def test_freno_viejo_al_arrancar_sale_distinto_de_cero(self):
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "harness-stop"
            freno.write_text("")
            p = self._run(["run", "--loop", "--freno", str(freno)])
        self.assertNotEqual(p.returncode, 0,
                            "salir 0 con el freno puesto se lee como éxito")
        self.assertIn(str(freno), p.stderr)
        self.assertIn("rm", p.stderr, "tiene que decir cómo sacarlo")

    def test_sin_freno_no_se_queja(self):
        """Sin freno arranca; que después falle por herdr u otra cosa no es
        un aviso de freno."""
        with tempfile.TemporaryDirectory() as d:
            p = self._run(["run", "--loop", "--freno", str(Path(d) / "no-existe")])
        self.assertNotIn("freno", p.stderr)

    def test_sin_loop_el_freno_no_interviene(self):
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "harness-stop"
            freno.write_text("")
            p = self._run(["run", "--freno", str(freno)])
        self.assertNotIn("freno", p.stderr)


class LaFronteraSeRecalcula(unittest.TestCase):
    """Entre pasada y pasada la frontera se recalcula desde el log y GitHub
    (#88): un ticket que se parkeó deja de entrar, y uno que se destrabó
    entra. Se corre la `run_main` real con el mundo de mentira."""

    CONFIG = json.dumps({
        "default_context": "h",
        "contexts": {
            "h": {"tracker": {"kind": "github"},
                  "repos": {"root": "~/repos", "paths": ["agent-harness"]},
                  "autonomy": "frontier",
                  "budget": {"polarity": "remaining", "provider": "openrouter"},
                  "run": {"kind": "local"}},
        },
    })

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.d.joinpath("config.json").write_text(self.CONFIG)
        self.log_path = self.d / "events.jsonl"
        self.freno = self.d / "freno"
        self._cargar_cli()

    def tearDown(self):
        self.tmp.cleanup()

    def _cargar_cli(self):
        # `bin/harness` no tiene extensión: spec_from_file_location no
        # infiere el loader, hay que dárselo.
        loader = SourceFileLoader("harness_cli", str(HARNESS))
        spec = importlib.util.spec_from_loader("harness_cli", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        self.cli = mod

    def _sembrar_escalera_agotada(self):
        """Cuatro abandonos que consumieron peldaño: la escalera de #7 está
        agotada y la primera pasada lo parkea, no lo despacha."""
        lineas = []
        for intento in range(1, 5):
            lineas.append(json.dumps({
                "timestamp": "2026-08-20T02:{:02d}:00Z".format(intento),
                "contexto": "h", "origen": "harness", "run_id": "r{}".format(intento),
                "ticket": "agent-harness#7", "attempt": intento,
                "tipo": "abandono", "ref": "ticket/7",
                "cuerpo": "gate rojo en el worktree: sin PR", "clase": "modelo",
            }))
        self.log_path.write_text("\n".join(lineas) + "\n")

    def _issue(self, n, etiqueta):
        return {"number": n, "title": "ticket {}".format(n),
                "labels": [{"name": etiqueta}], "body": "Sin bloqueos."}

    def _raw(self, issues):
        return {
            "offline": False,
            "agents": [],
            "contexts": [{
                "name": "h", "tracker": "github", "autonomy": "frontier",
                "vault": None, "run": "local",
                "budget": {"polarity": "remaining", "provider": "openrouter",
                           "credits": None, "total": 0.0, "used": 0.0},
                "repos": [{
                    "name": "agent-harness", "tracker": "github",
                    "slug": "Drokoz/agent-harness", "branch": "main",
                    "default_branch": "main",
                    "readiness_source": "default-branch",
                    "status_porcelain": "",
                    "path": str(self.d / "repos" / "agent-harness"),
                    "exists": {"gate": True, "skills": True, "context": True},
                    "issues": issues,
                    "prs": [], "prs_merged": [], "closed_count": 0,
                }],
            }],
        }

    def test_parké_deja_de_entrar_y_destrabado_entra(self):
        self._sembrar_escalera_agotada()
        mundo = Mundo(prs=[{"number": 31, "headRefName": "ticket/9"}])
        estado = {"n": 0}

        def collect_mock(contexts):
            estado["n"] += 1
            # La vuelta 2 es el proposer de frontera vacía (#115), que hace
            # su propia llamada de collect: el mundo no cambia por ella.
            if estado["n"] >= 3:
                # Un issue que se destraba en la madrugada, y el freno que
                # corta el bucle entre la pasada 2 y la (inexistente) 3.
                self.freno.write_text("")
                return self._raw([self._issue(7, "ready-for-human"),
                                  self._issue(9, "ready-for-agent")])
            return self._raw([self._issue(7, "ready-for-agent")])

        args = self.cli.build_parser().parse_args([
            "run", "--context", "h", "--loop", "--log", str(self.log_path),
            "--freno", str(self.freno), "--piso", "3", "--max-pasadas", "9",
        ])
        with mock.patch.object(self.cli, "OFFLINE", False), \
             mock.patch.object(self.cli, "collect", side_effect=collect_mock), \
             mock.patch.object(self.cli.adapters, "run", mundo.cmd), \
             mock.patch.object(self.cli, "_creditos_usado", return_value=None), \
             mock.patch.object(self.cli, "_creditos_restantes", return_value=99.0), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None, lambda *a: None)), \
             mock.patch.dict(os.environ, {"HERDR_ENV": "1", "HOME": str(self.d),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch("time.sleep"):
            self.d.joinpath("repos", "agent-harness").mkdir(parents=True)
            contexto = self.cli.load_config().select("h")
            salida = io.StringIO()
            with mock.patch("sys.stdout", new=salida):
                code = self.cli.run_main(args, contexto)
        texto = salida.getvalue()
        eventos = leer_log(self.log_path)

        self.assertEqual(code, 0)
        self.assertIn("corte: freno", texto)
        # La pasada con trabajo queda con su número; la vacía (parkeó #7)
        # no deja línea de `pasada`, deja la racha de `espera` (#114).
        por_pasada = [e for e in eventos if e["tipo"] == "pasada"]
        self.assertEqual([e["ref"] for e in por_pasada], ["bucle/2"])
        esperas = [e for e in eventos if e["tipo"] == "espera"]
        self.assertEqual(len(esperas), 1)
        corte = [e for e in eventos if e["tipo"] == "bucle"][-1]
        self.assertIn("corte: freno", corte["cuerpo"])
        # La primera pasada parkeó #7: una sola vez, sin despacharlo.
        self.assertEqual(len([e for e in eventos if e["tipo"] == "park"]), 1)
        self.assertEqual(
            [e for e in eventos if e["ref"] == "ticket/7"
             and e["tipo"] in ("worktree", "pane", "agente", "prompt", "gate")],
            [], "un ticket parkeado no se despacha de nuevo")
        # La segunda pasada despachó #9, que entró a la frontera recién.
        self.assertEqual(len([e for e in eventos
                              if e["tipo"] == "corrida" and "inicio" in e["cuerpo"]]),
                         1, "sólo la pasada 2 tuvo jobs que despachar")
        self.assertTrue(any(e["tipo"] == "pr" and "PR #31 abierto" in e["cuerpo"]
                            for e in eventos))
        self.assertIn("#9/agent-harness", texto)
        self.assertIn("parkeado", texto)  # "nada en la frontera (1 parkeado(s))"
        # Con la frontera vacía, el bucle propone trabajo en vez de sólo
        # esperar (#115): una vez, en la pasada que la encontró vacía.
        prop = [e for e in eventos if e["tipo"] == "proponer" and e["ref"] == "bucle"]
        self.assertEqual(len(prop), 1, "el proposer corre una vez por bucle")
        self.assertIn("frontera vacia", prop[0]["cuerpo"])
        self.assertEqual(prop[0]["run_id"], esperas[0]["run_id"],
                         "el proposer escribe en el log de la pasada vacía")
        # El log de la corrida de #9 apunta a la segunda pasada, no a la primera.
        refs_9 = {e["run_id"] for e in eventos if e.get("ticket") == "agent-harness#9"}
        self.assertEqual(refs_9, {por_pasada[0]["run_id"]})


class LaVigilacionBarata(unittest.TestCase):
    """El "¿cambió algo?" barato (#114), cableado en el CLI: sin cambio de
    firma no hay barrido completo; con cambio, sí; y la firma del repo es
    una sola llamada de search. Se corre la `run_main` real con el mundo
    de mentira."""

    CONFIG = json.dumps({
        "default_context": "h",
        "contexts": {
            "h": {"tracker": {"kind": "github"},
                  "repos": {"root": "~/repos", "paths": ["agent-harness"]},
                  "autonomy": "frontier",
                  "budget": {"polarity": "remaining", "provider": "openrouter"},
                  "run": {"kind": "local"}},
        },
    })

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.d.joinpath("config.json").write_text(self.CONFIG)
        self.log_path = self.d / "events.jsonl"
        self.freno = self.d / "freno"
        loader = SourceFileLoader("harness_cli_vigilar", str(HARNESS))
        spec = importlib.util.spec_from_loader("harness_cli_vigilar", loader)
        self.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(self.cli)

    def tearDown(self):
        self.tmp.cleanup()

    def _raw(self, issues):
        return {
            "offline": False,
            "agents": [],
            "contexts": [{
                "name": "h", "tracker": "github", "autonomy": "frontier",
                "vault": None, "run": "local",
                "budget": {"polarity": "remaining", "provider": "openrouter",
                           "credits": None, "total": 0.0, "used": 0.0},
                "repos": [{
                    "name": "agent-harness", "tracker": "github",
                    "slug": "Drokoz/agent-harness", "branch": "main",
                    "default_branch": "main",
                    "readiness_source": "default-branch",
                    "status_porcelain": "",
                    "path": str(self.d / "repos" / "agent-harness"),
                    "exists": {"gate": True, "skills": True, "context": True},
                    "issues": issues, "prs": [], "prs_merged": [],
                    "closed_count": 0,
                }],
            }],
        }

    def test_sin_cambio_no_barrera_con_cambio_si(self):
        """Firma igual dos vueltas seguidas: la segunda no barre. Firma
        nueva: sí barre, y el corte queda con el resumen de la espera."""
        firmas = [(3, "2026-09-06T01:00:00Z"),
                  (3, "2026-09-06T01:00:00Z"),
                  (4, "2026-09-06T02:00:00Z")]
        estado = {"vigilar": 0, "collect": 0}

        def firma(slug):
            f = firmas[min(estado["vigilar"], len(firmas) - 1)]
            estado["vigilar"] += 1
            return f

        def collect_mock(contexts):
            estado["collect"] += 1
            if estado["collect"] >= 2:
                self.freno.write_text("")
            return self._raw([])

        args = self.cli.build_parser().parse_args([
            "run", "--context", "h", "--loop", "--log", str(self.log_path),
            "--freno", str(self.freno), "--piso", "3", "--max-pasadas", "9",
            "--sin-proponer",
        ])
        mundo = Mundo(prs=[])
        with mock.patch.object(self.cli, "OFFLINE", False), \
             mock.patch.object(self.cli, "slug_of", return_value="fake/repo"), \
             mock.patch.object(self.cli, "_vigilar_repo", side_effect=firma), \
             mock.patch.object(self.cli, "collect", side_effect=collect_mock), \
             mock.patch.object(self.cli.adapters, "run", mundo.cmd), \
             mock.patch.object(self.cli, "_creditos_usado", return_value=None), \
             mock.patch.object(self.cli, "_creditos_restantes",
                               return_value=99.0), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None, lambda *a: None)), \
             mock.patch.dict(os.environ, {"HERDR_ENV": "1", "HOME": str(self.d),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch("time.sleep"):
            self.d.joinpath("repos", "agent-harness").mkdir(parents=True)
            contexto = self.cli.load_config().select("h")
            salida = io.StringIO()
            with mock.patch("sys.stdout", new=salida):
                code = self.cli.run_main(args, contexto)
        eventos = leer_log(self.log_path)

        self.assertEqual(code, 0)
        self.assertEqual(estado["collect"], 2,
                         "la vuelta sin cambio no barrió completo")
        self.assertEqual(estado["vigilar"], 3, "una consulta barata por vuelta")
        self.assertEqual(len([e for e in eventos if e["tipo"] == "proponer"]),
                         0, "--sin-proponer: ni el proposer corre")
        corte = [e for e in eventos if e["tipo"] == "bucle"][-1]
        self.assertIn("corte: freno", corte["cuerpo"])
        self.assertIn("3 vueltas", corte["cuerpo"])
        self.assertIn("esperados", corte["cuerpo"])

    def test_vigilar_fallo_en_el_cli_barrera_y_deja_lado(self):
        """`gh` que no contesta no es "no cambió": se barra completo y la
        falla queda en el log (el silencio no es confirmación)."""
        def firma(slug):
            raise RuntimeError("gh search/issues no respondió (fake/repo)")
        estado = {"collect": 0}

        def collect_mock(contexts):
            estado["collect"] += 1
            self.freno.write_text("")
            return self._raw([])

        args = self.cli.build_parser().parse_args([
            "run", "--context", "h", "--loop", "--log", str(self.log_path),
            "--freno", str(self.freno), "--piso", "3", "--max-pasadas", "9",
            "--sin-proponer",
        ])
        mundo = Mundo(prs=[])
        with mock.patch.object(self.cli, "OFFLINE", False), \
             mock.patch.object(self.cli, "slug_of", return_value="fake/repo"), \
             mock.patch.object(self.cli, "_vigilar_repo", side_effect=firma), \
             mock.patch.object(self.cli, "collect", side_effect=collect_mock), \
             mock.patch.object(self.cli.adapters, "run", mundo.cmd), \
             mock.patch.object(self.cli, "_creditos_usado", return_value=None), \
             mock.patch.object(self.cli, "_creditos_restantes",
                               return_value=99.0), \
             mock.patch.object(self.cli, "_sesiones_de_pi",
                               return_value=(lambda *a: None, lambda *a: None)), \
             mock.patch.dict(os.environ, {"HERDR_ENV": "1", "HOME": str(self.d),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch("time.sleep"):
            self.d.joinpath("repos", "agent-harness").mkdir(parents=True)
            contexto = self.cli.load_config().select("h")
            salida = io.StringIO()
            with mock.patch("sys.stdout", new=salida):
                code = self.cli.run_main(args, contexto)
        eventos = leer_log(self.log_path)
        self.assertEqual(code, 0)
        self.assertEqual(estado["collect"], 1, "el fallo barra completo")
        vig = [e for e in eventos if e["tipo"] == "vigilar"]
        self.assertEqual(len(vig), 1, "la falla queda anotada en el log")
        self.assertIn("barrio completo", vig[0]["cuerpo"])

    def test_firma_es_una_sola_llamada_con_updated_at(self):
        """La firma del repo: `(total, updated_at del más reciente)` de un
        solo search; sin respuesta, lanza (no adivina)."""
        data = {"total_count": 7, "items": [
            {"updated_at": "2026-09-06T02:33:00Z"},
            {"updated_at": "2026-09-06T01:00:00Z"}]}
        with mock.patch.object(self.cli.adapters, "gh_json",
                               return_value=data) as gh:
            self.assertEqual(self.cli._vigilar_repo("fake/repo"),
                             (7, "2026-09-06T02:33:00Z"))
        args = gh.call_args[0][1]
        self.assertEqual(gh.call_args[0][0], "fake/repo")
        self.assertEqual(len(args), 2)
        self.assertTrue(args[1].startswith("search/issues?q=repo:fake/repo"))
        self.assertIn("sort=updated", args[1])
        with mock.patch.object(self.cli.adapters, "gh_json",
                               return_value=None):
            with self.assertRaises(RuntimeError):
                self.cli._vigilar_repo("fake/repo")

    def test_sin_slug_resolvable_no_existe(self):
        """Sin firma no hay forma de afirmar que no cambió: no hay vigilar,
        y el bucle barra completo cada vuelta, como antes de #114."""
        self.d.joinpath("repos", "agent-harness").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(self.d),
                                          "HARNESS_CONFIG_DIR": str(self.d)}), \
             mock.patch.object(self.cli, "slug_of", return_value=None):
            contextos = self.cli.load_config().select("h")
            self.assertIsNone(self.cli._armar_vigilar(contextos))

    def test_armar_vigilar_compara_firmas_por_repo(self):
        """Primera vez: hay que barrer (no hay firma que comparar). Firma
        igual: no barra. Firma nueva: sí."""
        self.d.joinpath("repos", "agent-harness").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(self.d),
                                          "HARNESS_CONFIG_DIR": str(self.d)}):
            contextos = self.cli.load_config().select("h")
        firmas = [(1, "a"), (1, "a"), (2, "b")]
        estado = {"n": 0}

        def fake(slug):
            f = firmas[min(estado["n"], len(firmas) - 1)]
            estado["n"] += 1
            return f

        with mock.patch.dict(os.environ, {"HOME": str(self.d)}), \
             mock.patch.object(self.cli, "slug_of", return_value="fake/repo"), \
             mock.patch.object(self.cli, "_vigilar_repo", side_effect=fake):
            v = self.cli._armar_vigilar(contextos)
            self.assertTrue(v(), "sin firma previa, hay que barrer una vez")
            self.assertFalse(v(), "firma sin cambio: no hay que barrer")
            self.assertTrue(v(), "firma nueva: hay que barrer")


class LaLineaViva(unittest.TestCase):
    """#114: en TTY las vueltas vacías seguidas se colapsan en una línea
    que se actualiza en el sitio; el trabajo la baja."""

    def setUp(self):
        loader = SourceFileLoader("harness_cli_viva", str(HARNESS))
        spec = importlib.util.spec_from_loader("harness_cli_viva", loader)
        self.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(self.cli)
        self.cli.LINEA_VIVA["espera"] = False
        self.cli.LINEA_VIVA["ancho"] = 0

    class _TTY:
        def __init__(self):
            self.txt = []

        def write(self, s):
            self.txt.append(s)

        def flush(self):
            pass

        def isatty(self):
            return True

    def test_esperas_se_sobrescriben_y_el_trabajo_baja(self):
        t = self._TTY()
        e1 = {"timestamp": "2026-09-06T02:36:55Z", "ref": "bucle", "tipo": "espera",
              "cuerpo": "esperando desde las 02:36, 1 vueltas, espero 300s"}
        e2 = {"timestamp": "2026-09-06T02:41:55Z", "ref": "bucle", "tipo": "espera",
              "cuerpo": "esperando desde las 02:36, 2 vueltas, espero 600s"}
        e3 = {"timestamp": "2026-09-06T02:51:55Z", "ref": "bucle/3",
              "tipo": "pasada", "cuerpo": "pasada 3: 1 job(s)"}
        with mock.patch("sys.stdout", new=t), \
             mock.patch.object(self.cli.shutil, "get_terminal_size",
                               return_value=os.terminal_size((120, 24))):
            self.cli._imprimir_evento(e1)
            self.cli._imprimir_evento(e2)
            self.cli._imprimir_evento(e3)
        self.assertEqual(len(t.txt), 4)
        self.assertTrue(t.txt[0].endswith("\n"), "la primera va normal")
        self.assertTrue(t.txt[1].startswith("\r"),
                        "la segunda sobrescribe, sin salto de línea")
        self.assertNotIn("\n", t.txt[1])
        self.assertGreaterEqual(
            len(t.txt[1]), len(t.txt[0]),
            "el reescrito cubre la línea anterior (sin colas)")
        self.assertEqual(t.txt[2], "\n",
                         "el trabajo baja la línea viva antes de escribir")
        self.assertTrue(t.txt[3].endswith("\n"))

    def test_en_no_tty_cada_evento_va_a_su_linea(self):
        class _Pipe:
            def __init__(self):
                self.txt = []

            def write(self, s):
                self.txt.append(s)

            def flush(self):
                pass

            def isatty(self):
                return False
        t = _Pipe()
        e = {"timestamp": "2026-09-06T02:36:55Z", "ref": "bucle", "tipo": "espera",
             "cuerpo": "esperando desde las 02:36, 1 vueltas, espero 300s"}
        with mock.patch("sys.stdout", new=t):
            self.cli._imprimir_evento(e)
            self.cli._imprimir_evento(e)
        self.assertEqual(len(t.txt), 2)
        for parte in t.txt:
            self.assertTrue(parte.endswith("\n"),
                            "log redirigido: sin caracteres de control")
            self.assertNotIn("\r", parte)


if __name__ == "__main__":
    unittest.main()
