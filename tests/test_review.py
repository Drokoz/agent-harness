"""Tests del revisor barato sobre el diff (#46).

Todo contra el mundo de mentira: un `run_cmd` falsable para `Revisor`, y las
funciones puras (parseo del JSON del revisor, orden por gravedad, ida y
vuelta del cuerpo de la línea `review`) se prueban directo, sin nada que
correr. Los casos que fija el ticket: como mucho 10 hallazgos ordenados por
gravedad, read-only por construcción, fallas que no bloquean, y "sin
hallazgos" como veredicto propio.
"""

import json
import unittest

import support  # noqa: F401  (pone la raíz en sys.path)

from harness.review import (Hallazgo, MAX_HALLAZGOS, Revisor, Revision,
                            cuerpo_revision, normalizar_gravedad, ordenar,
                            parsear_hallazgos, parsear_revision, prompt_review)

DIFF = "--- a/harness/dispatch.py\n+++ b/harness/dispatch.py\n@@ -1,2 +1,2 @@\n-x\n+y\n"


class TestGravedad(unittest.TestCase):
    def test_normaliza_ingles_y_castellano(self):
        self.assertEqual(normalizar_gravedad("high"), "alta")
        self.assertEqual(normalizar_gravedad("blocker"), "alta")
        self.assertEqual(normalizar_gravedad("Alta"), "alta")
        self.assertEqual(normalizar_gravedad("medium"), "media")
        self.assertEqual(normalizar_gravedad("moderate"), "media")
        self.assertEqual(normalizar_gravedad("low"), "baja")
        self.assertEqual(normalizar_gravedad("nit"), "baja")

    def test_lo_que_no_se_reconoce_cae_a_baja(self):
        for s in ("apocalíptico", "", None, "  "):
            self.assertEqual(normalizar_gravedad(s), "baja")


class TestOrdenar(unittest.TestCase):
    def hallazgos(self, n):
        return [Hallazgo("media", "a.py", "m{}".format(i)) for i in range(n)]

    def test_ordena_por_gravedad(self):
        hs = [Hallazgo("baja", "", "b"), Hallazgo("alta", "", "a"),
              Hallazgo("media", "", "m")]
        self.assertEqual([h.gravedad for h in ordenar(hs)],
                         ["alta", "media", "baja"])

    def test_estable_dentro_del_grado(self):
        hs = [Hallazgo("media", "", "m1"), Hallazgo("alta", "", "a1"),
              Hallazgo("media", "", "m2")]
        self.assertEqual([h.detalle for h in ordenar(hs)],
                         ["a1", "m1", "m2"])

    def test_acota_a_maximo(self):
        self.assertEqual(len(ordenar(self.hallazgos(MAX_HALLAZGOS + 3))),
                         MAX_HALLAZGOS)
        self.assertEqual(len(ordenar(self.hallazgos(4))), 4)


class TestParsearHallazgos(unittest.TestCase):
    def test_arreglo_puro(self):
        texto = json.dumps([
            {"severity": "low", "file": "a.py", "issue": "nit"},
            {"severity": "high", "file": "b.py", "issue": "bug"},
        ])
        hs = parsear_hallazgos(texto)
        self.assertEqual([h.gravedad for h in hs], ["alta", "baja"])
        self.assertEqual(hs[0].archivo, "b.py")
        self.assertEqual(hs[0].detalle, "bug")

    def test_arreglo_envuelto_en_prosa(self):
        texto = "Aquí están mis findings:\n" + json.dumps(
            [{"severity": "medium", "file": "c.py", "issue": "x"}])
        hs = parsear_hallazgos(texto)
        self.assertEqual(len(hs), 1)
        self.assertEqual(hs[0].gravedad, "media")

    def test_objeto_con_findings(self):
        texto = json.dumps({"findings": [
            {"severity": "high", "issue": "bug"}]})
        hs = parsear_hallazgos(texto)
        self.assertEqual(len(hs), 1)
        self.assertEqual(hs[0].archivo, "")

    def test_vacio_es_vacio_no_none(self):
        """`[]` es el veredicto "no hay nada que decir": información, no
        falla. Distinguirlo de `None` es todo el punto."""
        self.assertEqual(parsear_hallazgos("[]"), [])
        self.assertEqual(parsear_hallazgos("Okay, nothing to report: []"), [])

    def test_garbage_es_none(self):
        for texto in ("", "no hay JSON acá", "[{roto", "{",
                      json.dumps({"sin": "lista"})):
            self.assertIsNone(parsear_hallazgos(texto), texto)

    def test_items_que_no_cooperan_se_saltan(self):
        texto = json.dumps([
            "un string",
            {"severity": "high"},                    # sin detalle
            {"severity": "high", "file": "a.py",
             "issue": "va"},
            {"file": "b.py", "issue": "sin severidad"},
        ])
        hs = parsear_hallazgos(texto)
        self.assertEqual(len(hs), 2)
        self.assertEqual([h.gravedad for h in hs], ["alta", "baja"])

    def test_acota_a_diez(self):
        texto = json.dumps([
            {"severity": "high", "issue": "h{}".format(i)} for i in range(15)])
        self.assertEqual(len(parsear_hallazgos(texto)), MAX_HALLAZGOS)


class TestCuerpoRevision(unittest.TestCase):
    def test_ida_y_vuelta_con_hallazgos(self):
        rev = Revision("ok", hallazgos=[
            Hallazgo("alta", "harness/x.py", "bug"),
            Hallazgo("baja", "", "nit")])
        cuerpo = cuerpo_revision(19, rev)
        self.assertEqual(cuerpo,
                         "revisión PR #19: 2 hallazgo(s) "
                         "[[\"alta\", \"harness/x.py\", \"bug\"], "
                         "[\"baja\", \"\", \"nit\"]]")
        de = parsear_revision(cuerpo)
        self.assertEqual(de.estado, "ok")
        self.assertEqual([h.linea for h in de.hallazgos],
                         ["[alta] harness/x.py: bug", "[baja] nit"])

    def test_ida_y_vuelta_sin_hallazgos(self):
        cuerpo = cuerpo_revision(20, Revision("sin_hallazgos"))
        self.assertEqual(cuerpo, "revisión PR #20: sin hallazgos")
        de = parsear_revision(cuerpo)
        self.assertEqual(de.estado, "sin_hallazgos")
        self.assertEqual(de.hallazgos, [])

    def test_ida_y_vuelta_fallida(self):
        rev = Revision("fallida", motivo="el revisor no respondio: timeout")
        cuerpo = cuerpo_revision(21, rev)
        de = parsear_revision(cuerpo)
        self.assertEqual(de.estado, "fallida")
        self.assertEqual(de.motivo, "el revisor no respondio: timeout")

    def test_cuerpos_ajenos_es_none(self):
        self.assertIsNone(parsear_revision("PR #19 abierto (gate verde)"))
        self.assertIsNone(parsear_revision(""))
        self.assertIsNone(parsear_revision(None))
        self.assertIsNone(parsear_revision("revisión PR #19: algo raro"))
        self.assertIsNone(parsear_revision(
            "revisión PR #19: 1 hallazgo(s) [{roto"))


class TestPrompt(unittest.TestCase):
    def test_lleva_el_diff_y_las_reglas(self):
        p = prompt_review("Drokoz/koku", 31, DIFF)
        self.assertIn(DIFF, p)
        self.assertIn("PR #31", p)
        self.assertIn("read-only", p)
        self.assertIn("no commits", p)
        self.assertIn("no PR comments", p)
        self.assertIn("no labels", p)
        self.assertIn(str(MAX_HALLAZGOS), p)
        self.assertIn("severity", p)
        self.assertIn("[]", p)

    def test_el_diff_no_se_aplasta(self):
        # A diferencia del prompt de herdr, este va por CLI: el diff
        # conserva sus saltos de línea (el contexto del diff es semántica).
        self.assertIn("\n", prompt_review("a/b", 1, DIFF))


# --------------------------------------------------------------------- revisor
class FalsoMundo:
    """`run_cmd` de mentira: `gh pr diff` devuelve `diff`, el revisor
    devuelve `salida`; lo que no coincide devuelve `(True, "")`."""

    def __init__(self, diff=(True, DIFF), salida=(True, "[]")):
        self.diff = diff
        self.salida = salida
        self.llamadas = []

    def __call__(self, args, cwd=None, timeout=30):
        args = list(args)
        self.llamadas.append((args, cwd, timeout))
        if args[0] == "gh":
            return self.diff
        return self.salida


class TestRevisor(unittest.TestCase):
    def test_hallazgos(self):
        mundo = FalsoMundo(salida=(True, json.dumps([
            {"severity": "low", "file": "a.py", "issue": "nit"},
            {"severity": "high", "file": "b.py", "issue": "bug"},
        ])))
        rev = Revisor(mundo).revisar_pr("Drokoz/koku", 31)
        self.assertEqual(rev.estado, "ok")
        self.assertEqual([h.gravedad for h in rev.hallazgos], ["alta", "baja"])
        # el diff se leyó del PR, no de la rama
        self.assertTrue(any(a[0] == "gh" and "diff" in a for a, _, _
                            in mundo.llamadas))

    def test_sin_hallazgos_es_veredicto(self):
        rev = Revisor(FalsoMundo(salida=(True, "[]"))).revisar_pr("a/b", 1)
        self.assertEqual(rev.estado, "sin_hallazgos")
        self.assertEqual(rev.hallazgos, [])

    def test_diff_inlegible_es_falla(self):
        for diff in ((False, "boom"), (True, "")):
            mundo = FalsoMundo(diff=diff)
            rev = Revisor(mundo).revisar_pr("a/b", 1)
            self.assertEqual(rev.estado, "fallida", diff)
            self.assertIn("diff", rev.motivo)
            # ni siquiera se arrancó el revisor: una llamada, la de gh
            self.assertEqual(len(mundo.llamadas), 1)

    def test_revisor_que_no_responde_es_falla(self):
        rev = Revisor(FalsoMundo(salida=(False, "timeout"))).revisar_pr("a/b", 1)
        self.assertEqual(rev.estado, "fallida")
        self.assertIn("no respondio", rev.motivo)

    def test_salida_sin_json_es_falla_no_limpio(self):
        """Prosa sin arreglo no es "sin hallazgos": es una revisión fallida.
        Marcar un PR como limpio porque el revisor balbuceó sería mentir."""
        rev = Revisor(FalsoMundo(
            salida=(True, "the diff looks fine to me"))).revisar_pr("a/b", 1)
        self.assertEqual(rev.estado, "fallida")

    def test_lee_solo(self):
        """Read-only por construcción: en pi la CLI corre con sólo tools de
        lectura. La promesa no va sólo en el prompt, va en el comando."""
        mundo = FalsoMundo()
        Revisor(mundo).revisar_pr("a/b", 1)
        pi = [a for a, _, _ in mundo.llamadas if a[0] == "pi"]
        self.assertEqual(len(pi), 1)
        args = pi[0]
        self.assertIn("--tools", args)
        self.assertEqual(args[args.index("--tools") + 1], "read,grep,find,ls")
        self.assertIn("-p", args)

    def test_por_default_el_runner_barato(self):
        mundo = FalsoMundo()
        Revisor(mundo).revisar_pr("a/b", 1)
        args, _, _ = mundo.llamadas[1]
        self.assertEqual(args[0], "pi")
        self.assertIn("qwen/qwen3.8-27b", args)

    def test_timeout_del_revisor_se_fija(self):
        mundo = FalsoMundo()
        Revisor(mundo, timeout=900).revisar_pr("a/b", 1)
        self.assertEqual(mundo.llamadas[1][2], 900)

    def test_diff_gigante_se_trunca(self):
        gigante = "x" * 200_000  # más grande que diff_max por default
        mundo = FalsoMundo(diff=(True, gigante))
        Revisor(mundo).revisar_pr("a/b", 1)
        prompt = [a for a, _, _ in mundo.llamadas if a[0] == "pi"][0]
        self.assertIn("diff truncado", prompt[prompt.index("-p") + 1])
        self.assertLess(len(prompt), len(gigante))


if __name__ == "__main__":
    unittest.main()
