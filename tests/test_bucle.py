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


def correr(resultados, creditos=None, al_largo=None, freno=None, **spec):
    """Corre el bucle con un mundo de mentira. Devuelve
    `(corte, eventos, dormidos, nº de pasadas)`.

    `resultados` es la lista de qué encuentra cada pasada ("trabajo" o
    "vacia"); más allá del final se repite la última. `creditos` es la
    lista de lecturas de crédito (una por iteración del bucle; `None` =
    sin medir); `al_largo(i)` es un efecto de lado en la pasada i;
    `dormir` es un registro, no una espera.
    """
    with tempfile.TemporaryDirectory() as d:
        log_path = Path(d) / "events.jsonl"
        log = EventLog(log_path, "harness")
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
                      dormir=dormidos.append)
        return corte, leer_log(log_path), dormidos, estado["n"]


class ElBucle(unittest.TestCase):
    def test_corta_por_tope_de_pasadas_con_trabajo(self):
        corte, _, dormidos, n = correr(["trabajo"], max_pasadas=3)
        self.assertEqual(n, 3, "nunca un bucle sin límite")
        self.assertIn("tope", corte)
        self.assertEqual(dormidos, [60.0] * 3)

    def test_frontera_vacia_espera_en_vez_de_cortar(self):
        """#101: la frontera vacía no es el fin de la noche, es una espera."""
        corte, _, dormidos, n = correr(["vacia"], vueltas_vacias_max=3)
        self.assertEqual(n, 3)
        self.assertIn("nada nuevo", corte)
        self.assertEqual(dormidos, [300.0] * 3, "la espera vacía es la larga")

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

    def test_cada_pasada_queda_en_el_log_con_su_numero(self):
        corte, eventos, _, n = correr(
            ["trabajo", "vacia"], max_pasadas=2, vueltas_vacias_max=2)
        self.assertEqual(n, 3)
        por_pasada = [e for e in eventos if e["tipo"] == "pasada"]
        self.assertEqual([e["ref"] for e in por_pasada],
                         ["bucle/1", "bucle/2", "bucle/3"])
        for i, e in enumerate(por_pasada, 1):
            self.assertIn("pasada {}".format(i), e["cuerpo"])

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
             mock.patch.dict(os.environ, {"HERDR_ENV": "1",
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
        # Dos pasadas, cada una con su número; el corte, con su motivo.
        por_pasada = [e for e in eventos if e["tipo"] == "pasada"]
        self.assertEqual([e["ref"] for e in por_pasada], ["bucle/1", "bucle/2"])
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
        self.assertEqual(prop[0]["run_id"], por_pasada[0]["run_id"],
                         "el proposer escribe en el log de la pasada vacía")
        # El log de la corrida de #9 apunta a la segunda pasada, no a la primera.
        refs_9 = {e["run_id"] for e in eventos if e.get("ticket") == "agent-harness#9"}
        self.assertEqual(refs_9, {por_pasada[1]["run_id"]})


if __name__ == "__main__":
    unittest.main()
