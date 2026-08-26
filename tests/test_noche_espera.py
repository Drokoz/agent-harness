"""Con la frontera vacía, el bucle de la noche espera en vez de cortar (#101).

Si a las 3am se cierra el último ticket, cortar significa dejar cinco horas de
máquina sin usar: un issue etiquetado a las 4am no lo agarra nadie. Y una
pasada vacía no puede gastar un turno de `PASADAS_MAX`, que es un tope contra
un bucle de trabajo descontrolado, no contra esperar.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import support

NOCHE = support.ROOT / "scripts" / "noche.sh"


class Falso:
    """Un `harness` de mentira: contesta lo que se le diga, pasada por pasada,
    y deja constancia de cuántas veces lo llamaron.

    Las salidas van a archivos y el guión hace `cat`: interpolarlas adentro de
    un `echo "..."` rompe el guión apenas una salida trae comillas, y un guión
    que no parsea nunca corre --- se ve como cero pasadas, no como un error."""

    def __init__(self, d, salidas):
        self.dir = Path(d) / "falso"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cuenta = self.dir / "cuenta"
        for i, texto in enumerate(salidas):
            (self.dir / "salida-{}".format(i + 1)).write_text(texto + "\n")
        (self.dir / "salida-ultima").write_text((salidas[-1] if salidas else "") + "\n")
        self.ruta = self.dir / "harness"
        self.ruta.write_text(
            "#!/usr/bin/env bash\n"
            "D={d}\n"
            'n=$(( $(cat "$D/cuenta" 2>/dev/null || echo 0) + 1 ))\n'
            'echo "$n" > "$D/cuenta"\n'
            '[ -n "${{HOOK:-}}" ] && eval "$HOOK"\n'
            'cat "$D/salida-$n" 2>/dev/null || cat "$D/salida-ultima"\n'
            "exit 0\n".format(d=str(self.dir)))
        self.ruta.chmod(0o755)

    def pasadas(self):
        return int(self.cuenta.read_text().strip()) if self.cuenta.exists() else 0


class Corrida:
    """Lo que quedó de correr el script. Lleva `pasadas` adentro a propósito:
    leerlo después, afuera del `with tempfile`, da 0 con el directorio ya
    borrado --- un assert que pasa sin mirar nada."""

    def __init__(self, proc, pasadas):
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.returncode = proc.returncode
        self.pasadas = pasadas


def correr(d, falso, **env):
    e = dict(os.environ)
    e.update({"FRENO": str(Path(d) / "no-existe"),
              "LOG": str(Path(d) / "noche.log"),
              "CREDITO_FAKE": "99", "ESPERA": "0", "ESPERA_VACIA": "0",
              "HARNESS_BIN": str(falso.ruta)})
    e.update(env)
    proc = subprocess.run(["bash", str(NOCHE)], capture_output=True,
                          text=True, env=e, timeout=120)
    return Corrida(proc, falso.pasadas())


class LaCostura(unittest.TestCase):
    """Sin costura, correr `noche.sh` ES despachar agentes de verdad. Ya pasó
    (2026-08-25): un test lo corrió, despachó #30 y #56, y el timeout del test
    mató al dispatcher dejando dos agentes vivos sin watchdog ni cosecha."""

    def test_el_script_usa_harness_bin_y_no_el_del_repo(self):
        with tempfile.TemporaryDirectory() as d:
            f = Falso(d, ['harness: "h": nada en la frontera'])
            r = correr(d, f, PASADAS_MAX="1", VUELTAS_VACIAS_MAX="1")
        self.assertEqual(r.pasadas, 1, "no llamo al falso: llamo al de verdad")


class FronteraVacia(unittest.TestCase):
    def test_una_frontera_vacia_no_termina_la_noche(self):
        with tempfile.TemporaryDirectory() as d:
            f = Falso(d, ['harness: "h": nada en la frontera'])
            r = correr(d, f, PASADAS_MAX="3", VUELTAS_VACIAS_MAX="4")
        self.assertGreater(r.pasadas, 1,
                           "con una sola pasada esta esperando cero")
        self.assertIn("espero", r.stdout.lower())

    def test_una_pasada_vacia_no_gasta_un_turno_de_trabajo(self):
        """PASADAS_MAX es el tope contra un bucle de trabajo descontrolado."""
        with tempfile.TemporaryDirectory() as d:
            f = Falso(d, ['harness: "h": nada en la frontera'])
            r = correr(d, f, PASADAS_MAX="2", VUELTAS_VACIAS_MAX="5")
        self.assertEqual(r.pasadas, 5,
                         "las vacias tienen que contar contra su propio tope")

    def test_esperar_no_es_para_siempre(self):
        with tempfile.TemporaryDirectory() as d:
            f = Falso(d, ['harness: "h": nada en la frontera'])
            r = correr(d, f, PASADAS_MAX="9", VUELTAS_VACIAS_MAX="2")
        self.assertEqual(r.pasadas, 2)
        self.assertIn("nada nuevo", r.stdout.lower())

    def test_un_ticket_que_aparece_mientras_espera_se_despacha(self):
        """La razón de ser del ticket: un issue etiquetado a las 4am."""
        with tempfile.TemporaryDirectory() as d:
            f = Falso(d, ['harness: "h": nada en la frontera',
                          'harness: "h": nada en la frontera',
                          "  OK ticket/42",
                          'harness: "h": nada en la frontera'])
            r = correr(d, f, PASADAS_MAX="2", VUELTAS_VACIAS_MAX="3")
        self.assertIn("OK ticket/42", r.stdout)

    def test_el_freno_corta_tambien_mientras_espera(self):
        """El freno aparece durante la primera espera: tiene que cortar ahí,
        no cuando se acabe el tope de vueltas vacías."""
        with tempfile.TemporaryDirectory() as d:
            freno = Path(d) / "freno"
            f = Falso(d, ['harness: "h": nada en la frontera'])
            r = correr(d, f, PASADAS_MAX="9", VUELTAS_VACIAS_MAX="9",
                       FRENO=str(freno), HOOK='touch "{}"'.format(freno))
        self.assertEqual(r.pasadas, 1)
        self.assertIn("freno", r.stdout.lower())

    def test_el_piso_de_presupuesto_corta_tambien_mientras_espera(self):
        with tempfile.TemporaryDirectory() as d:
            f = Falso(d, ['harness: "h": nada en la frontera'])
            r = correr(d, f, PASADAS_MAX="9", VUELTAS_VACIAS_MAX="9",
                       PISO_USD="10", CREDITO_FAKE="2.50")
        self.assertEqual(r.pasadas, 0)
        self.assertIn("bajo el piso", r.stdout)


if __name__ == "__main__":
    unittest.main()
