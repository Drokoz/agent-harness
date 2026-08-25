"""El bucle de la noche (scripts/noche.sh) tiene que fallar ruidoso.

La noche del 2026-08-25 no corrió nada: había quedado un `/tmp/harness-stop`
de una prueba anterior, el script lo encontró, escribió una línea en un log
que nadie estaba mirando y salió con código 0 al segundo. Un freno viejo se
come una noche entera y parece que salió bien.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import support

NOCHE = support.ROOT / "scripts" / "noche.sh"


def correr(**env):
    e = dict(os.environ)
    e.update(env)
    return subprocess.run(["bash", str(NOCHE)], capture_output=True,
                          text=True, env=e, timeout=60)


class FrenoViejo(unittest.TestCase):
    def test_el_freno_al_arrancar_sale_distinto_de_cero(self):
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "harness-stop"
            freno.write_text("")
            r = correr(FRENO=str(freno), LOG=str(Path(d) / "noche.log"),
                       CREDITO_FAKE="99")
        self.assertNotEqual(r.returncode, 0,
                            "salir 0 con el freno puesto se lee como exito")

    def test_el_freno_al_arrancar_avisa_por_stderr(self):
        """El stdout se va al log; lo que el humano ve en la terminal es stderr."""
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "harness-stop"
            freno.write_text("")
            r = correr(FRENO=str(freno), LOG=str(Path(d) / "noche.log"),
                       CREDITO_FAKE="99")
        self.assertIn(str(freno), r.stderr)
        self.assertIn("rm", r.stderr, "tiene que decir como sacarlo")

    def test_sin_freno_no_se_queja(self):
        """Sin freno arranca; que despues corte por credito o por lo que sea
        no es asunto de este test, pero el aviso de freno no puede aparecer."""
        with tempfile.TemporaryDirectory() as d:
            r = correr(FRENO=str(Path(d) / "no-existe"),
                       LOG=str(Path(d) / "noche.log"),
                       PASADAS_MAX="0", CREDITO_FAKE="99")
        self.assertNotIn("freno", r.stderr)
        self.assertEqual(r.returncode, 0)


class PisoDePresupuesto(unittest.TestCase):
    def test_por_debajo_del_piso_no_despacha(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "noche.log"
            r = correr(FRENO=str(Path(d) / "no-existe"), LOG=str(log),
                       PASADAS_MAX="1", PISO_USD="10", CREDITO_FAKE="2.50")
        self.assertEqual(r.returncode, 0)
        self.assertIn("bajo el piso", r.stdout)


if __name__ == "__main__":
    unittest.main()
