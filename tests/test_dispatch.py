"""El dispatcher (`harness run`) con el mundo de mentira: un fake `run_cmd`
con respuestas scripteadas por herdr, git y gh.

Lo que se verifica acá son las reglas que hacen seguro dejarlo corriendo:
un worktree aislado por ticket, el prompt verificado de verdad (el primero
se pierde), el gate como red, el abandono sin atascarse, la limpieza del
pane y del worktree, el log JSONL con `origen`, el costo, y que jamás sale
un comando de merge.
"""

import json
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import support
from harness import adapters, dispatch, state
from harness.dispatch import (ESCALERA, DispatchSpec, Dispatcher, EventLog, Job,
                              cosechar, decidir_tanda, nombre_agente, peldano_de,
                              prompt_de, una_linea, worktree_path)

ROOT = support.ROOT
HARNESS = ROOT / "bin" / "harness"

NOMBRE_VALIDO = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# La línea de estado de la TUI de pi, con contexto a 0% (prompt todavía no llega).
PANE_CERO = "⠏ Working...\n~/repo (ticket/7)\n↑1k ↓0.2k R2k CH0.0% $0.012 0.0%/262k ...\n"
# El prompt llegó: el contexto subió y hay costo acumulado.
PANE_ARRANCO = "⠏ Working...\n~/repo (ticket/7)\n↑12k ↓2k R4k CH0.1% $0.056 1.2%/262k ...\n"


class Mundo:
    """El mundo del dispatcher: run_cmd scripteado + créditos + sueño corto.

    `respuestas` es una lista de (pruebo(args), (ok, out)); gana la primera
    que aplique. Lo que no coincide cae al default por comando.
    """

    def __init__(self, pane_out=PANE_ARRANCO, wait_out=None, gate=(True, "VERDE"),
                 prs=None, credits=(None, None), head_oid="a" * 40,
                 pr_head_oid=None, pr_files=None, origin_oid=None):
        self.pane_out = pane_out
        self.wait_out = wait_out or '{"result":{"agent":{"agent_status":"idle"}}}'
        self.gate = gate
        self.prs = prs if prs is not None else [
            {"number": 31, "headRefName": "ticket/7"}]
        self.creditos = list(credits)
        self.head_oid = head_oid
        # Dónde está origin/<rama por defecto> tras el fetch (#85); sin
        # definir, en el mismo lugar que el HEAD local.
        self.origin_oid = origin_oid if origin_oid is not None else head_oid
        self.pr_head_oid = pr_head_oid if pr_head_oid is not None else head_oid
        self.pr_files = pr_files if pr_files is not None else [
            "harness/dispatch.py", "tests/test_dispatch.py"]
        self.n_creditos = 0
        self.respuestas = []
        self.llamadas = []
        self.lock = threading.Lock()
        self.activo = 0
        self.max_activo = 0

    def responder(self, pruebo, ok_out):
        self.respuestas.append((pruebo, ok_out))
        return self

    def cmd(self, args, cwd=None, timeout=30):
        args = list(args)
        with self.lock:
            self.llamadas.append((tuple(args), cwd, timeout))
            self.activo += 1
            self.max_activo = max(self.max_activo, self.activo)
        try:
            for pruebo, ok_out in self.respuestas:
                if pruebo(args):
                    return ok_out(args) if callable(ok_out) else ok_out
            return self._default(args, cwd)
        finally:
            with self.lock:
                self.activo -= 1

    def _default(self, args, cwd):
        a = args[0]
        if a == "git":
            if "rev-list" in args:
                return (True, "1\n")  # la rama lleva un commit por delante
            if "rev-parse" in args:
                if args[-1] == "HEAD":
                    return (True, self.head_oid + "\n")
                if args[-1].startswith("origin/"):
                    return (True, self.origin_oid + "\n")
                return (False, "")  # la rama no existe todavía: -b
            return (True, "")
        if a == "herdr":
            sub = args[1:3]
            if sub[:2] == ["pane", "split"]:
                return (True, '{"result":{"pane":{"pane_id":"w9:p1"}}}')
            if sub[:2] == ["pane", "read"]:
                return (True, self.pane_out)
            if sub[:2] == ["agent", "wait"]:
                return (True, self.wait_out)
            return (True, "")
        if a == "gh":
            if "pr" in args and "list" in args:
                return (True, json.dumps(self.prs))
            if "pr" in args and "view" in args:
                if "files" in args:
                    return (True, json.dumps([{"path": p}
                                              for p in self.pr_files]))
                return (True, json.dumps({"headRefOid": self.pr_head_oid}))
            if "issue" in args and "close" in args:
                return (True, "")
            return (True, '{"state":"CLOSED"}')
        if a == "./scripts/gate.sh":
            return self.gate
        return (True, "")

    def credits(self):
        i = min(self.n_creditos, len(self.creditos) - 1)
        self.n_creditos += 1
        return self.creditos[i]

    def dormir(self, s):
        pass

    def llamo(self, *parte):
        return [c for c in self.llamadas
                if all(x in c[0] for x in parte)]


def log_en(tmp, nombre="ev.jsonl"):
    return EventLog(Path(tmp) / nombre, "personal",
                    reloj=lambda: "2026-08-22T12:00:00Z")


def spec(**kw):
    return DispatchSpec(contexto="personal", **kw)


def job(issue=7, repo="koku", path="/repos/koku", slug="Drokoz/koku",
        attempt=1, kind="", model="", extra_args=(), gate_tail=None,
        wip=None):
    return Job(repo=repo, repo_path=path, slug=slug, issue=issue,
               attempt=attempt, kind=kind, model=model,
               extra_args=extra_args, gate_tail=gate_tail, wip=wip)


def job_de_peldano(escalados, **kw):
    """Un job en el peldaño que le toca a `escalados` abandonos previos
    (#38): lo mismo que arma `bin/harness` antes de llamar a `dispatch`."""
    p = peldano_de(escalados)
    return job(kind=p["kind"], model=p["model"], extra_args=p["extra_args"], **kw)


class TestTandaPorPresupuesto(unittest.TestCase):
    """#68: el tamaño de la tanda lo decide el presupuesto. Puro: los
    créditos y el costo medio (del historial, #35) se inyectan; la
    decisión es por candidato, en el orden de la frontera, y cada uno que
    se queda fuera dice por qué."""

    def candidatos(self, n, kind="pi", base=40):
        return [job(issue=base + i, kind=kind) for i in range(n)]

    def despachados(self, decisiones):
        return [d["job"] for d in decisiones if d["despachar"]]

    def decisiones_despachadas(self, decisiones):
        return [d for d in decisiones if d["despachar"]]

    def fuera(self, decisiones):
        return [d for d in decisiones if not d["despachar"]]

    def test_cuantos_entran_con_las_plata_que_quedan(self):
        # El caso del issue: 15 tickets libres y US$1.86. Con costo medio
        # $0.43 y margen $0.10 caben 4, no 15.
        d = decidir_tanda(self.candidatos(15), creditos=1.86,
                          costo_promedio=0.43, margen=0.10)
        self.assertEqual(len(d), 15)
        self.assertEqual([j.issue for j in self.despachados(d)],
                         [40, 41, 42, 43])
        self.assertTrue(all("presupuesto" in x["motivo"]
                            for x in self.fuera(d)))

    def test_un_ticket_arranca_si_caben_costo_mas_margen(self):
        d = decidir_tanda([job(kind="pi")], creditos=0.53,
                          costo_promedio=0.43, margen=0.10)
        self.assertTrue(d[0]["despachar"])
        d = decidir_tanda([job(kind="pi")], creditos=0.52,
                          costo_promedio=0.43, margen=0.10)
        self.assertFalse(d[0]["despachar"])
        self.assertIn("presupuesto", d[0]["motivo"])

    def test_el_margen_cero_arranca_con_lo_justo(self):
        d = decidir_tanda([job(kind="pi")], creditos=0.43,
                          costo_promedio=0.43, margen=0.0)
        self.assertTrue(d[0]["despachar"])

    def test_sin_historial_no_se_puede_dimensionar(self):
        d = decidir_tanda(self.candidatos(2), creditos=100.0,
                          costo_promedio=None, margen=0.10)
        self.assertEqual(len(self.fuera(d)), 2)
        self.assertTrue(all("costo medio" in x["motivo"]
                            for x in self.fuera(d)))

    def test_creditos_desconocidos_se_anotan_y_no_se_dispacha(self):
        # Sin leer el crédito no hay forma de prometer plata: el tope
        # tampoco se aplica en silencio, se dice por qué se excluyó.
        d = decidir_tanda(self.candidatos(2), creditos=None,
                          costo_promedio=0.43, margen=0.10)
        self.assertEqual(len(self.fuera(d)), 2)
        self.assertTrue(all("creditos desconocidos" in x["motivo"]
                            for x in self.fuera(d)))

    def test_el_runner_sin_creditos_no_se_ve_limitado(self):
        # Claude corre contra cuota, no contra la key de OpenRouter: el
        # tope por presupuesto no le aplica, y se dice por qué.
        d = decidir_tanda(self.candidatos(2, kind="claude"), creditos=0.0,
                          costo_promedio=0.43, margen=0.10)
        self.assertEqual(len(self.despachados(d)), 2)
        self.assertTrue(all(x["motivo"] == "no gasta creditos"
                            for x in self.decisiones_despachadas(d)))
        d = decidir_tanda([job(kind="claude")], creditos=None,
                          costo_promedio=None, margen=0.10)
        self.assertTrue(d[0]["despachar"])

    def test_el_claude_corre_aunque_la_plata_se_agote(self):
        d = decidir_tanda(self.candidatos(1) + self.candidatos(1, kind="claude", base=60),
                          creditos=0.05, costo_promedio=0.43, margen=0.10)
        self.assertEqual([j.issue for j in self.despachados(d)], [60])

    def test_max_tickets_acota_la_tanda(self):
        d = decidir_tanda(self.candidatos(5), creditos=100.0,
                          costo_promedio=0.43, margen=0.10, max_tickets=2)
        self.assertEqual([j.issue for j in self.despachados(d)], [40, 41])
        self.assertTrue(all("max-tickets" in x["motivo"]
                            for x in self.fuera(d)[2:]))

    def test_max_tickets_no_sube_el_tope_de_presupuesto(self):
        # Piden 5, la plata alcanza para 4: gana la plata, y el motivo
        # lo dice en vez de omitirlo.
        d = decidir_tanda(self.candidatos(5), creditos=1.86,
                          costo_promedio=0.43, margen=0.10, max_tickets=5)
        self.assertEqual(len(self.despachados(d)), 4)
        self.assertTrue(all("presupuesto" in x["motivo"]
                            for x in self.fuera(d)))

    def test_ambos_topes_se_apuntan_juntos(self):
        d = decidir_tanda(self.candidatos(4), creditos=1.30,
                          costo_promedio=0.43, margen=0.10, max_tickets=2)
        # el tercero cae por ambos: los dos motivos quedan en la razón
        terc = self.fuera(d)[1]
        self.assertIn("max-tickets", terc["motivo"])
        self.assertIn("presupuesto", terc["motivo"])

    def test_sin_provider_de_creditos_no_hay_tope_por_plata(self):
        # El contexto no mide OpenRouter: no se dimensiona con plata, no
        # es "creditos desconocidos"; el tope manual sí sigue valiendo.
        d = decidir_tanda(self.candidatos(3), creditos=None,
                          costo_promedio=None, margen=0.10,
                          presupuesto=False)
        self.assertEqual(len(self.despachados(d)), 3)
        d = decidir_tanda(self.candidatos(3), creditos=None,
                          costo_promedio=None, margen=0.10,
                          presupuesto=False, max_tickets=2)
        self.assertEqual(len(self.despachados(d)), 2)

    def test_el_orden_de_la_frontera_se_mantiene(self):
        d = decidir_tanda(self.candidatos(5, base=38), creditos=1.86,
                          costo_promedio=0.43, margen=0.10)
        self.assertEqual([j.issue for j in [d["job"] for d in d]],
                         [38, 39, 40, 41, 42])


def despachar(mundo, jobs, costo_real=None, salida_real=None, **kw):
    with tempfile.TemporaryDirectory() as tmp:
        log = log_en(tmp)
        d = Dispatcher(spec(**kw), log, mundo.cmd, credits=mundo.credits,
                       dormir=mundo.dormir, costo_real=costo_real,
                       salida_real=salida_real)
        res = d.dispatch(jobs)
        lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
    return res, lineas


class TestPuros(unittest.TestCase):
    def test_una_linea(self):
        self.assertEqual(una_linea("a\nb"), "a b")
        self.assertEqual(una_linea("a\r\nb\r c"), "a b c")
        self.assertEqual(una_linea("  a\n  "), "a")

    def test_prompt_es_una_linea_y_lleva_las_reglas(self):
        p = prompt_de(7)
        self.assertNotIn("\n", p)
        self.assertIn("#7", p)
        self.assertIn("Closes #7", p)
        self.assertIn("./scripts/gate.sh", p)
        # Sin distinguir mayusculas: la regla puede caer al inicio de oracion.
        self.assertIn("never merge", p.lower())

    def test_prompt_exige_commitear_y_empujar(self):
        """Cinco de seis tickets de la tanda del 2026-08-24 se abandonaron con
        cero commits en la rama: el prompt presentaba el PR como condicional
        ("open the PR only if...") y nunca decia que commitear era obligatorio.
        El trabajo sin commitear no entra al PR y la limpieza lo borra."""
        p = prompt_de(7)
        self.assertIn("commit", p)
        self.assertIn("push", p)
        # Que quede dicho que no terminar asi es un fracaso, no una opcion.
        self.assertIn("discarded", p)

    def test_nombre_agente_es_valido_y_distingue(self):
        for n in (nombre_agente("koku", 7), nombre_agente("ERP-IphoneUp", 7),
                  nombre_agente("entrevestidos-ventas", 12345)):
            self.assertRegex(n, NOMBRE_VALIDO, n)
        self.assertNotEqual(nombre_agente("koku", 7), nombre_agente("koku", 8))
        self.assertNotEqual(nombre_agente("koku", 7), nombre_agente("koku-api", 7))

    def test_worktree_diferente_por_ticket(self):
        a = worktree_path("/repos/koku", 7)
        b = worktree_path("/repos/koku", 8)
        self.assertNotEqual(a, b)
        self.assertIn(".worktrees", a.parts)

    def test_prompt_sin_cola_de_gate_no_la_menciona(self):
        self.assertEqual(prompt_de(7), prompt_de(7, gate_tail=None))
        self.assertEqual(prompt_de(7), prompt_de(7, gate_tail=""))

    def test_prompt_con_cola_de_gate_va_en_una_sola_linea(self):
        """Peldaño 2 de la escalera (#38): la cola del gate del intento
        anterior se pega al prompt, y sigue siendo una sola línea aunque
        la cola traiga saltos -- herdr no entrega nada si no lo es."""
        p = prompt_de(7, gate_tail="TypeError: x is undefined\n  at foo.js:12")
        self.assertNotIn("\n", p)
        self.assertIn("TypeError: x is undefined", p)
        self.assertIn("at foo.js:12", p)

    def test_prompt_sin_wip_es_el_de_siempre(self):
        """#80: un ticket sin WIP previo no recibe ruido extra: el prompt
        es idéntico al de siempre."""
        self.assertEqual(prompt_de(7), prompt_de(7, wip=None))
        self.assertEqual(prompt_de(7), prompt_de(7, wip=()))
        self.assertNotIn("WIP", prompt_de(7))

    def test_prompt_con_wip_lo_dice_todo(self):
        """#80: que existe el WIP, de qué intento viene y con qué motivo se
        abandonó; las dos opciones (retomar o descartar y empezar de cero);
        que descartar es legítimo; dónde está la transcripción (#66); y que
        la decisión queda registrada."""
        wip = (2, "gate rojo en el worktree: sin PR",
               "/logs/koku-ticket-7-intento-2.log")
        p = prompt_de(7, wip=wip)
        self.assertNotIn("\n", p)
        # Existe, y de dónde viene.
        self.assertIn("WIP commit left by the abandoned attempt 2", p)
        self.assertIn("gate rojo en el worktree: sin PR", p)
        # Las dos opciones, y que descartar no es un fracaso.
        self.assertIn("resume from that WIP commit", p)
        self.assertIn("discard it and start from scratch", p)
        self.assertIn("legitimate choice", p)
        self.assertIn("not a failure", p)
        # La transcripción del intento anterior (#66).
        self.assertIn("transcript at /logs/koku-ticket-7-intento-2.log", p)
        # La decisión queda registrada, medible.
        self.assertIn("WIP decision", p)

    def test_prompt_con_wip_y_cola_de_gate_juntos(self):
        """#80 + #38: las dos adiciones conviven en una sola línea."""
        p = prompt_de(7, gate_tail="boom", wip=(1, "arbol sucio", "/t.log"))
        self.assertNotIn("\n", p)
        self.assertIn("boom", p)
        self.assertIn("arbol sucio", p)


class TestPeldanoDe(unittest.TestCase):
    """`peldano_de` (#38): puro, sin memoria -- `escalados` es lo único que
    entra, y siempre sale de releer el log (`state.intentos_que_escalan`)."""

    def test_hay_cuatro_peldanos_y_el_quinto_es_parquear(self):
        self.assertEqual(len(ESCALERA), 4)
        for i in range(4):
            self.assertIsNotNone(peldano_de(i))
        self.assertIsNone(peldano_de(4))
        self.assertIsNone(peldano_de(5))

    def test_peldano_1_y_2_son_el_mismo_runner_y_modelo(self):
        """El thinking sube ANTES de cambiar de modelo (#38)."""
        p1, p2 = peldano_de(0), peldano_de(1)
        self.assertEqual(p1["kind"], "pi")
        self.assertEqual(p1["kind"], p2["kind"])
        self.assertEqual(p1["model"], p2["model"])
        self.assertNotEqual(p1["extra_args"], p2["extra_args"])
        self.assertIn("medium", p1["extra_args"])
        self.assertIn("high", p2["extra_args"])
        self.assertNotIn("cola_gate", p1)
        self.assertTrue(p2.get("cola_gate"))

    def test_peldano_3_cambia_a_claude_sonnet(self):
        p = peldano_de(2)
        self.assertEqual(p["kind"], "claude")
        self.assertEqual(p["model"], "sonnet")
        self.assertEqual(p["extra_args"], ("--effort", "medium"))

    def test_peldano_4_es_claude_opus(self):
        p = peldano_de(3)
        self.assertEqual(p["kind"], "claude")
        self.assertEqual(p["model"], "opus")

    def test_negativo_es_el_primer_peldano(self):
        self.assertEqual(peldano_de(-1), peldano_de(0))

    def test_se_deriva_del_historial_no_de_memoria(self):
        """El peldaño no vive en ningún objeto vivo del dispatcher: se
        recalcula leyendo el log cada vez (#38), así que sobrevive
        reiniciar el proceso -- acá, dos abandonos que escalan en dos
        `run_id` distintos (dos "reinicios") alcanzan el peldaño 3."""
        evs = [
            {"tipo": "abandono", "ref": "ticket/7", "ticket": "koku#7",
             "run_id": "r1", "cuerpo": "gate rojo", "clase": "modelo"},
            {"tipo": "abandono", "ref": "ticket/7", "ticket": "koku#7",
             "run_id": "r2", "cuerpo": "timeout", "clase": "modelo"},
        ]
        escalados = state.intentos_que_escalan(evs, "koku", 7)
        p = peldano_de(escalados)
        self.assertEqual((p["kind"], p["model"]), ("claude", "sonnet"))


class TestEtiquetaPeldano(unittest.TestCase):
    """El peldaño con que arrancó un ticket (#81): la decisión menos obvia
    del despacho, y tiene que verse en la salida en vivo."""

    def test_cada_peldano_de_escalera_lleva_su_numero(self):
        from harness.dispatch import peldano_etiqueta, peldano_numero
        for i, p in enumerate(ESCALERA):
            self.assertEqual(peldano_numero(p["kind"], p["model"],
                                            p["extra_args"]), i + 1)
            etiqueta = peldano_etiqueta(p["kind"], p["model"], p["extra_args"])
            self.assertTrue(etiqueta.startswith("peldaño {} · ".format(i + 1)),
                            etiqueta)
            self.assertIn(p["kind"], etiqueta)
            self.assertIn(p["model"], etiqueta)
            self.assertIn(" ".join(p["extra_args"]), etiqueta)

    def test_los_peldanos_1_y_2_se_distinigen_por_el_thinking(self):
        from harness.dispatch import peldano_etiqueta
        p1, p2 = ESCALERA[0], ESCALERA[1]
        self.assertNotEqual(
            peldano_etiqueta(p1["kind"], p1["model"], p1["extra_args"]),
            peldano_etiqueta(p2["kind"], p2["model"], p2["extra_args"]))

    def test_spec_fuera_de_la_escalera_no_finge_numero(self):
        """El fallback --kind/--model no es un peldaño: se muestra sin número."""
        from harness.dispatch import peldano_etiqueta, peldano_numero
        self.assertEqual(peldano_numero("pi", "xai/grok", ()), 0)
        self.assertEqual(peldano_etiqueta("pi", "xai/grok", ()), "pi xai/grok")
        self.assertEqual(peldano_etiqueta("pi", "", ()), "pi")


class TestEventLog(unittest.TestCase):
    def test_lineas_completas_y_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "e.jsonl"
            reloj = lambda: "2026-08-22T12:00:00Z"
            l1 = EventLog(p, "personal", reloj=reloj)
            l1.write("worktree", "ticket/7", "/x")
            l2 = EventLog(p, "personal", reloj=lambda: "otra-timestamp")
            l2.write("gate", "ticket/7", "verde")
            lineas = [json.loads(l) for l in p.read_text().splitlines()]
            self.assertEqual(len(lineas), 2)
            for linea in lineas:
                self.assertEqual(
                    sorted(linea), ["attempt", "clase", "contexto", "cuerpo",
                                    "origen", "ref", "run_id", "ticket",
                                    "timestamp", "tipo"])
                self.assertEqual(linea["origen"], "harness")
                self.assertIsNone(linea["clase"])
            self.assertEqual(lineas[0]["contexto"], "personal")
            self.assertEqual(lineas[1]["timestamp"], "otra-timestamp")
            # una instancia es una corrida: el run_id distingue tandas
            self.assertNotEqual(lineas[0]["run_id"], lineas[1]["run_id"])

    def test_lleva_run_id_ticket_y_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "e.jsonl"
            log = EventLog(p, "personal",
                           reloj=lambda: "2026-08-22T12:00:00Z",
                           run_id="r1")
            log.write("corrida", "corrida", "inicio: 1 ticket(s)")
            log.write("gate", "ticket/7", "verde", ticket="koku#7",
                      attempt=2)
            lineas = [json.loads(l) for l in p.read_text().splitlines()]
            self.assertEqual(lineas[0]["run_id"], "r1")
            self.assertIsNone(lineas[0]["ticket"])
            self.assertIsNone(lineas[0]["attempt"])
            self.assertEqual(lineas[1]["ticket"], "koku#7")
            self.assertEqual(lineas[1]["attempt"], 2)

    def test_lleva_clase(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "e.jsonl"
            log = EventLog(p, "personal", reloj=lambda: "2026-08-22T12:00:00Z")
            log.write("abandono", "ticket/7", "algo", ticket="koku#7",
                      attempt=1, clase="infra")
            linea = json.loads(p.read_text().splitlines()[0])
            self.assertEqual(linea["clase"], "infra")

    def test_listener_recibe_cada_linea_escrita(self):
        """El listener (#81) ve cada evento en el momento en que se escribe,
        con el mismo dict que va al JSONL."""
        vistos = []
        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog(Path(tmp) / "e.jsonl", "personal",
                           reloj=lambda: "2026-08-22T12:00:00Z",
                           listener=vistos.append)
            log.write("corrida", "corrida", "inicio: 1 ticket(s)")
            log.write("gate", "ticket/7", "verde", ticket="koku#7")
            archivo = [json.loads(l) for l in log.path.read_text().splitlines()]
        self.assertEqual(vistos, archivo)

    def test_listener_que_falla_no_tumba_la_escritura(self):
        """El archivo es la fuente: un listener que explota (stdout roto)
        no impide que el evento quede en el JSONL ni lanza afuera."""
        def rompe(_linea):
            raise OSError("broken pipe")
        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog(Path(tmp) / "e.jsonl", "personal",
                           reloj=lambda: "2026-08-22T12:00:00Z",
                           listener=rompe)
            log.write("gate", "ticket/7", "verde", ticket="koku#7")
            self.assertEqual(len(log.path.read_text().splitlines()), 1)


class TestCicloDeVida(unittest.TestCase):
    def test_happy_path_llega_a_hecho(self):
        m = Mundo()
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        tipos = [l["tipo"] for l in lineas]
        for esperado in ("corrida", "worktree", "pane", "agente", "prompt",
                         "gate", "pr", "costo", "limpieza"):
            self.assertIn(esperado, tipos, tipos)
        self.assertNotIn("abandono", tipos)

    def test_worktrees_aislados(self):
        m = Mundo()
        res, lineas = despachar(m, [job(7), job(8)], max_parallel=2)
        wts = [l["cuerpo"] for l in lineas if l["tipo"] == "worktree"]
        self.assertEqual(len(wts), 2)
        self.assertNotEqual(wts[0], wts[1])
        splits = m.llamo("herdr", "pane", "split")
        self.assertEqual(len(splits), 2)
        cwds = []
        for s in splits:
            a = s[0]
            self.assertIn("--cwd", a)
            cwds.append(a[a.index("--cwd") + 1])
        self.assertEqual(len(set(cwds)), 2)

    def test_el_job_deja_sus_marcas_en_el_log(self):
        m = Mundo()
        res, lineas = despachar(m, [job(attempt=3)])
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertEqual(len({l["run_id"] for l in lineas}), 1)
        for l in lineas:
            if l["ref"] == "ticket/7":
                self.assertEqual(l["ticket"], "koku#7")
                self.assertEqual(l["attempt"], 3)
            else:
                self.assertIsNone(l["ticket"])
                self.assertIsNone(l["attempt"])

    def test_el_peldano_con_que_arranco_queda_en_el_log(self):
        """El peldaño es la decisión menos obvia del despacho (#81): se
        anota antes de cualquier otra acción del ticket, y dice runner,
        modelo y args del peldaño."""
        m = Mundo()
        res, lineas = despachar(m, [job_de_peldano(0)])
        peldanos = [l for l in lineas if l["tipo"] == "peldano"]
        self.assertEqual(len(peldanos), 1)
        self.assertEqual(peldanos[0]["cuerpo"],
                         "peldaño 1 · pi qwen/qwen3.8-27b --thinking medium")
        self.assertEqual(peldanos[0]["ticket"], "koku#7")
        self.assertLess(lineas.index(peldanos[0]),
                        lineas.index([l for l in lineas
                                      if l["tipo"] == "worktree"][0]))

    def test_fallback_sin_peldano_no_finge_numero(self):
        """kind/modelo vacíos = el fallback de la corrida, no un peldaño de
        la escalera: la etiqueta se escribe sin número."""
        m = Mundo()
        res, lineas = despachar(m, [job()])
        self.assertEqual([l["cuerpo"] for l in lineas if l["tipo"] == "peldano"],
                         ["pi"])

    def test_aplazo_del_router_no_anota_peldano_despachado(self):
        """Un job que el router aplazó no se despachó: en el log queda el
        aplazo, no un peldaño que no se corrió."""
        m = Mundo()
        res, lineas = despachar(m, [job_de_peldano(2)],
                                permitir_claude=lambda: (False, "cuota"))
        self.assertEqual(res[0].estado, "pendiente")
        self.assertEqual([l["cuerpo"] for l in lineas if l["tipo"] == "peldano"],
                         ["claude no permitido ahora (router): cuota"])

    def test_nunca_merge(self):
        m = Mundo()
        despachar(m, [job()])
        merges = [c for c in m.llamadas
                  if "merge" in c[0] and "pr" in c[0]]
        self.assertEqual(merges, [])

    def test_el_prompt_se_envia_en_una_linea(self):
        m = Mundo()
        despachar(m, [job()])
        (prompt_call,) = m.llamo("herdr", "agent", "prompt")
        texto = prompt_call[0][4]  # herdr agent prompt <target> <texto> ...
        self.assertNotIn("\n", texto)
        self.assertNotIn("\r", texto)

    def test_bloqueado_se_abandona_y_sigue_el_siguiente(self):
        m = Mundo(prs=[{"number": 31, "headRefName": "ticket/7"},
                       {"number": 32, "headRefName": "ticket/8"}])
        wait_bloqueado = '{"result":{"agent":{"agent_status":"blocked"}}}'
        # el agente del ticket 7 queda bloqueado; el del 8 termina bien
        m.responder(lambda a: a[:3] == ["herdr", "agent", "wait"] and a[3] == "koku-7",
                    (True, wait_bloqueado))
        res, lineas = despachar(m, [job(7), job(8)])
        por_issue = {r.issue: r for r in res}
        self.assertEqual(por_issue[7].estado, "abandonado")
        self.assertIn("bloqueado", por_issue[7].motivo)
        self.assertEqual(por_issue[8].estado, "hecho")
        abandono = [l for l in lineas if l["tipo"] == "abandono"]
        self.assertEqual(abandono[0]["ref"], "ticket/7")

    def test_gate_rojo_no_tiene_pr(self):
        m = Mundo(gate=(False, "ROJO: unittest"))
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertIn("gate", res[0].motivo)
        self.assertNotIn("pr", [l["tipo"] for l in lineas])

    def test_terminar_sin_pr_es_abandono(self):
        m = Mundo(prs=[{"number": 31, "headRefName": "otra-rama"}])
        res, _ = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertIn("sin PR", res[0].motivo)

    def test_timeout_de_espera_es_un_tick_del_watchdog(self):
        """Antes, el timeout de una hora era el veredicto. Ahora cada
        timeout es un chequeo de progreso (#41): sin progreso, el agente
        se mata mucho antes de la hora."""
        m = Mundo()
        m.wait_out = '{"error":{"code":"timeout"},"id":"cli:agent:wait"}'
        res, _ = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertIn("sin progreso", res[0].motivo)

    def test_falla_el_worktree_y_el_job_no_arranca(self):
        m = Mundo().responder(
            lambda a: a[0] == "git" and "worktree" in a and "add" in a,
            (False, "fatal: no pathspec"))
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(m.llamo("herdr", "agent", "start"), [])
        self.assertEqual(m.llamo("herdr", "agent", "prompt"), [])

    def test_rama_ya_existente_no_choca(self):
        m = Mundo()
        m.responder(lambda a: "rev-parse" in a, (True, "sha\n"))
        despachar(m, [job()])
        (add,) = m.llamo("git", "worktree", "add")
        self.assertNotIn("-b", add[0])

    def test_rama_nueva_pone_el_nombre_despues_de_menos_b(self):
        """`worktree add -b <rama> <path>`, en ese orden.

        Si `-b` queda pegado antes del path, git lee el path como nombre de
        rama y la rama como path, y falla con "is not a valid branch name".
        Pasó en producción el 2026-08-23: la corrida entera —quince tickets—
        murió acá, y como `run` se comía el stderr el log sólo decía "fallo:".
        """
        m = Mundo()
        m.responder(lambda a: "rev-parse" in a, (False, ""))  # la rama no existe
        despachar(m, [job()])
        (add,) = m.llamo("git", "worktree", "add")
        args = add[0]
        i = args.index("-b")
        self.assertEqual(args[i + 1], "ticket/7",
                         "después de -b va el nombre de la rama, no el path")
        self.assertTrue(args[i + 2].endswith("ticket-7"),
                        "el path del worktree va al final")


class TestPromptPerdido(unittest.TestCase):
    def test_reintenta_hasta_que_el_contexto_sube(self):
        m = Mundo(pane_out=PANE_CERO)  # el contexto nunca sube solo

        def pane_por_intento(args):
            # cuenta los prompts enviados; el segundo hace "llegar" el trabajo
            if len(m.llamo("herdr", "agent", "prompt")) >= 2:
                return (True, PANE_ARRANCO)
            return (True, PANE_CERO)

        m.responder(lambda a: a[:3] == ["herdr", "pane", "read"], pane_por_intento)
        res, lineas = despachar(m, [job()], verify_wait_s=0.05, verify_poll_s=0.01)
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertEqual(len(m.llamo("herdr", "agent", "prompt")), 2)
        intentos = [l for l in lineas if l["tipo"] == "prompt"]
        self.assertEqual(len(intentos), 2)

    def test_si_nunca_sube_se_abandona(self):
        # `prompt_requeues=0` desactiva la recolocación de la misma corrida
        # (#113) y deja el comportamiento antiguo: la ventana entera de
        # intentos es todo lo que el job recibe.
        m = Mundo(pane_out=PANE_CERO)
        res, lineas = despachar(m, [job()], verify_wait_s=0.01,
                                verify_poll_s=0.01, verify_retries=3,
                                prompt_requeues=0)
        self.assertEqual(res[0].estado, "abandonado")
        self.assertIn("0%", res[0].motivo)
        self.assertEqual(len(m.llamo("herdr", "agent", "prompt")), 3)
        self.assertIn("abandono", [l["tipo"] for l in lineas])


class TestPromptPerdidoMismaCorrida(unittest.TestCase):
    """#113: el prompt perdido no termina la corrida del ticket en esa
    corrida: si no llega, el job vuelve a la cola de la MISMA corrida, no
    de la pasada siguiente. La evidencia es la noche del 2026-08-26: 4 de
    9 tickets perdieron el primer prompt en dos pasadas seguidas (los
    reintentos entraban en 16s, porque `agent prompt --wait` devuelve
    `agent_prompt_stalled` en ~3s en vez de esperar) y recién corrieron
    en la tercera, dos horas después.
    """

    def test_prompt_que_llega_tarde_se_recupera_en_la_misma_corrida(self):
        # El agente recién recibe el prompt en su segundo arranque: la
        # primera vuelta (worktree, pane, agente, prompt) lo pierde y el
        # job vuelve a la cola; en la segunda llega.
        m = Mundo(pane_out=PANE_CERO)
        lanzamientos = {"n": 0}

        def agente_start(args):
            lanzamientos["n"] += 1
            if lanzamientos["n"] >= 2:
                return (True, '{"result":{"agent":{"agent_status":"working"}}}')
            return (True, '{"result":{"agent":{"agent_status":"stalled"}}}')

        def prompt(args):
            # La TUI recién acepta el prompt desde el segundo arranque:
            # antes, el envío no levanta el estado.
            if lanzamientos["n"] >= 2:
                return (True, '{"result":{"agent":{"agent_status":"working"}}}')
            return (True, '{"result":{"agent":{"agent_status":"stalled"}}}')

        m.responder(lambda a: a[:3] == ["herdr", "agent", "start"], agente_start)
        m.responder(lambda a: a[:3] == ["herdr", "agent", "prompt"], prompt)
        res, lineas = despachar(m, [job()], verify_wait_s=0.01,
                                verify_poll_s=0.01, verify_retries=2)
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertEqual(lanzamientos["n"], 2, "el job se re-arrancó una vez")
        self.assertEqual(len(m.llamo("herdr", "agent", "prompt")), 3,
                         "2 intentos perdidos + 1 que llegó")
        recolocaciones = [l for l in lineas if "vuelve a la cola" in l["cuerpo"]]
        self.assertEqual(len(recolocaciones), 1)
        # Nada de abandono: un prompt perdido no gasta peldaño de escalada.
        self.assertNotIn("abandono", [l["tipo"] for l in lineas])

    def test_prompt_que_nunca_llega_agota_las_vueltas_y_abandona_infra(self):
        m = Mundo(pane_out=PANE_CERO)
        res, lineas = despachar(m, [job()], verify_wait_s=0.01,
                                verify_poll_s=0.01, verify_retries=2,
                                prompt_requeues=2)
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(res[0].clase_abandono, "infra")
        self.assertIn("0%", res[0].motivo)
        # 3 vueltas x 2 intentos: la corrida entera se agota antes de ceder.
        self.assertEqual(len(m.llamo("herdr", "agent", "prompt")), 6)
        self.assertEqual(len(m.llamo("herdr", "agent", "start")), 3)
        recolocaciones = [l for l in lineas if "vuelve a la cola" in l["cuerpo"]]
        self.assertEqual(len(recolocaciones), 2)
        self.assertEqual(len([l for l in lineas
                              if l["tipo"] == "abandono"]), 1)
        self.assertEqual(self._clase_de(lineas), ["infra"])

    @staticmethod
    def _clase_de(lineas):
        return [l.get("clase") for l in lineas if l["tipo"] == "abandono"]


class TestLimpieza(unittest.TestCase):
    def test_cierra_el_pane_y_quita_el_worktree(self):
        m = Mundo()
        despachar(m, [job()])
        self.assertEqual(len(m.llamo("herdr", "pane", "close")), 1)
        removes = m.llamo("git", "worktree", "remove")
        self.assertEqual(len(removes), 1)
        self.assertNotIn("branch", removes[0][0])  # la rama queda

    def test_limpieza_tambien_si_falla_a_medio(self):
        m = Mundo(gate=(False, "ROJO"))
        despachar(m, [job()])
        self.assertEqual(len(m.llamo("herdr", "pane", "close")), 1)
        self.assertEqual(len(m.llamo("git", "worktree", "remove")), 1)


class TestClaseDeAbandono(unittest.TestCase):
    """La clase de un abandono decide si se quema un peldaño de la escalera.
    El dispatcher sólo ve el síntoma ("el agente terminó sin PR abierto") y
    lo clasifica `modelo`; la causa la guarda pi (#98). El 2026-08-24, entre
    las 11:48 y las 11:55, cinco agentes murieron con "Connection error.":
    no fueron cinco fracasos del modelo, fue un corte de red, y la escalera
    les cobró un peldaño a los cinco."""

    def _abandonar(self, salida):
        """Un mundo donde el agente termina sin PR: el `modelo` por default."""
        m = Mundo(credits=[100.0, 100.10]).responder(
            lambda a: a[0] == "gh" and a[1] == "pr" and a[2] == "list",
            (True, "[]"))
        res, lineas = despachar(m, [job()], salida_real=lambda *a, **k: salida)
        self.assertTrue([l for l in lineas if l["tipo"] == "abandono"],
                        "el escenario tiene que terminar en abandono")
        return res, lineas

    def test_un_corte_de_red_es_infra_y_no_quema_peldano(self):
        res, lineas = self._abandonar({"stop": "error",
                                       "error": "Connection error."})
        self.assertEqual(res[0].estado, "abandonado")
        ab = [l for l in lineas if l["tipo"] == "abandono"][0]
        self.assertEqual(ab["clase"], "infra")
        self.assertIn("Connection error", ab["cuerpo"],
                      "el motivo tiene que llevar la causa, no solo el sintoma")
        self.assertEqual(state.intentos_que_escalan(lineas, "koku", 7), 0)

    def test_sin_creditos_es_humano_y_sale_de_la_frontera(self):
        """Reintentar contra una pared es gastar de gusto: sale de la
        frontera en el acto, sin esperar a que se agote la escalera."""
        m = Mundo(credits=[100.0, 100.10]).responder(
            lambda a: a[0] == "gh" and a[1] == "pr" and a[2] == "list",
            (True, "[]"))
        _, lineas = despachar(m, [job()], salida_real=lambda *a, **k: {
            "stop": "error", "error": '402 "You have run out of credits"'})
        ab = [l for l in lineas if l["tipo"] == "abandono"][0]
        self.assertEqual(ab["clase"], "humano")
        (edit,) = m.llamo("gh", "issue", "edit")
        self.assertIn("ready-for-human", edit[0])
        self.assertIn("ready-for-agent", edit[0])

    def test_sin_sesion_en_el_disco_no_cambia_nada(self):
        _, lineas = self._abandonar(None)
        ab = [l for l in lineas if l["tipo"] == "abandono"][0]
        self.assertEqual(ab["clase"], "modelo")
        self.assertEqual(state.intentos_que_escalan(lineas, "koku", 7), 1)

    def test_una_sesion_que_termino_bien_deja_la_clasificacion_del_dispatcher(self):
        """pi no se cayó: el que sabe por qué no hubo PR es el dispatcher."""
        _, lineas = self._abandonar({"stop": "stop", "error": ""})
        ab = [l for l in lineas if l["tipo"] == "abandono"][0]
        self.assertEqual(ab["clase"], "modelo")

    def test_un_gate_rojo_no_lo_perdona_ningun_error_de_pi(self):
        """El gate en rojo es evidencia positiva de trabajo malo. Que la
        sesión además haya cortado con un error de red no lo excusa: la noche
        del 26, "gate rojo en el worktree" salió `infra` porque pi había
        tenido un `Connection error.` en el medio."""
        m = Mundo(credits=[100.0, 100.10]).responder(
            lambda a: a[0] == "bash" or (a and str(a[-1]).endswith("gate.sh")),
            (False, "ROJO: unittest"))
        _, lineas = despachar(m, [job()], salida_real=lambda *a, **k: {
            "stop": "error", "error": "Connection error."})
        ab = [l for l in lineas if l["tipo"] == "abandono"]
        rojo = [l for l in ab if "gate rojo" in l["cuerpo"]]
        self.assertTrue(rojo, "el escenario tiene que dar gate rojo")
        self.assertEqual(rojo[0]["clase"], "modelo")

    def test_la_evidencia_de_pi_no_pisa_un_infra_que_ya_estaba_clasificado(self):
        """Un abandono que el dispatcher ya sabe que es infra (nunca arrancó
        el agente) no tiene sesión que consultar, y no se toca."""
        m = Mundo(credits=[100.0, 100.10]).responder(
            lambda a: a[:3] == ["herdr", "agent", "start"],
            (False, '{"error":{"code":"boom"}}'))
        _, lineas = despachar(m, [job()],
                              salida_real=lambda *a, **k: {"stop": "stop"})
        ab = [l for l in lineas if l["tipo"] == "abandono"][0]
        self.assertEqual(ab["clase"], "infra")


class TestCosto(unittest.TestCase):
    """El costo por job (#53): la línea de estado de la TUI de pi no es una
    fuente confiable —una corrida real midió $1.7085 de delta de créditos con
    los cuatro jobs en $0.0000— así que el costo sale de repartir el delta de
    créditos de la corrida entre los jobs que prendieron un pane.
    """

    def test_costo_por_job_se_reparte_el_delta_de_creditos(self):
        m = Mundo(credits=[100.0, 100.25])
        res, lineas = despachar(m, [job()])
        self.assertAlmostEqual(res[0].costo, 0.25)
        costo = [l for l in lineas if l["tipo"] == "costo" and l["ref"] == "ticket/7"]
        self.assertIn("0.25", costo[0]["cuerpo"])

    def test_costo_se_reparte_entre_varios_jobs(self):
        m = Mundo(credits=[100.0, 100.30])
        res, _ = despachar(m, [job(7), job(8)], max_parallel=2)
        for r in res:
            self.assertAlmostEqual(r.costo, 0.15)
        self.assertAlmostEqual(sum(r.costo for r in res), 0.30)

    def test_una_linea_de_estado_real_de_pi_no_se_usa_para_el_costo(self):
        """Aunque el pane muestre una línea de estado real de pi con costo
        (la evidencia de que el prompt llegó, ver `_llego`), el costo del job
        sale del delta de créditos, no de leerla."""
        real = ("⠏ Working...\n~/repo (ticket/7)\n"
                "↑78k ↓5.9k R34k CH0.0% $0.056 11.4%/262k ...\n")
        m = Mundo(pane_out=real, credits=[100.0, 101.7085])
        res, _ = despachar(m, [job()])
        self.assertAlmostEqual(res[0].costo, 1.7085)

    def test_costo_desconocido_no_es_cero_sin_creditos(self):
        m = Mundo(credits=[None, None])
        res, lineas = despachar(m, [job()])
        self.assertIsNone(res[0].costo)
        costo = [l for l in lineas if l["tipo"] == "costo" and l["ref"] == "ticket/7"]
        self.assertEqual(costo[0]["cuerpo"], "desconocido")

    def test_el_costo_medido_por_pi_le_gana_al_reparto(self):
        """El reparto es una estimación; la sesión de pi es el número."""
        m = Mundo(credits=[100.0, 100.30])
        res, lineas = despachar(m, [job()], costo_real=lambda *a, **k: 0.42)
        self.assertAlmostEqual(res[0].costo, 0.42)
        costo = [l for l in lineas if l["tipo"] == "costo" and l["ref"] == "ticket/7"]
        self.assertIn("0.42", costo[0]["cuerpo"])
        self.assertIn("pi", costo[0]["cuerpo"])

    def test_lo_que_pi_no_midio_se_reparte_lo_que_sobra_del_delta(self):
        """Si de dos jobs uno tiene medición real, el otro no puede llevarse
        medio delta: se lleva lo que queda después de descontar la real."""
        m = Mundo(credits=[100.0, 100.50])
        reales = {7: 0.20, 8: None}
        res, _ = despachar(m, [job(7), job(8)], max_parallel=2,
                           costo_real=lambda repo, issue, desde=None: reales[issue])
        por_issue = {r.issue: r.costo for r in res}
        self.assertAlmostEqual(por_issue[7], 0.20)
        self.assertAlmostEqual(por_issue[8], 0.30)

    def test_una_medicion_mas_grande_que_el_delta_no_deja_costo_negativo(self):
        """El delta de créditos se contamina con cualquier otra cosa que use
        la misma key; la resta puede dar negativa y un costo negativo miente."""
        m = Mundo(credits=[100.0, 100.10])
        reales = {7: 0.90, 8: None}
        res, _ = despachar(m, [job(7), job(8)], max_parallel=2,
                           costo_real=lambda repo, issue, desde=None: reales[issue])
        por_issue = {r.issue: r.costo for r in res}
        self.assertAlmostEqual(por_issue[8], 0.0)

    def test_sin_medicion_de_pi_sigue_el_reparto_de_siempre(self):
        m = Mundo(credits=[100.0, 100.25])
        res, _ = despachar(m, [job()], costo_real=lambda *a, **k: None)
        self.assertAlmostEqual(res[0].costo, 0.25)

    def test_el_reparto_se_marca_estimado_y_la_medicion_no(self):
        """Un promedio y una medición no pueden leerse igual en el log: el
        router (#45) decide con esa diferencia (#65)."""
        m = Mundo(credits=[100.0, 100.25])
        _, lineas = despachar(m, [job()], costo_real=lambda *a, **k: None)
        cuerpo = [l["cuerpo"] for l in lineas
                  if l["tipo"] == "costo" and l["ref"] == "ticket/7"][0]
        self.assertIn("estimado", cuerpo)
        _, lineas = despachar(m, [job()], costo_real=lambda *a, **k: 0.42)
        cuerpo = [l["cuerpo"] for l in lineas
                  if l["tipo"] == "costo" and l["ref"] == "ticket/7"][0]
        self.assertNotIn("estimado", cuerpo)

    def test_pi_mide_aunque_no_haya_creditos_para_repartir(self):
        """La medición real no depende de que la corrida termine bien."""
        m = Mundo(credits=[None, None])
        res, _ = despachar(m, [job()], costo_real=lambda *a, **k: 0.33)
        self.assertAlmostEqual(res[0].costo, 0.33)

    def test_a_pi_se_le_pregunta_solo_por_esta_corrida(self):
        """Sin recorte, un ticket reintentado arrastra el costo de anoche."""
        vistos = []
        m = Mundo(credits=[100.0, 100.25])

        def espia(repo, issue, desde=None):
            vistos.append((repo, issue, desde))
            return 0.1

        despachar(m, [job()], costo_real=espia)
        self.assertEqual(len(vistos), 1)
        self.assertIsNotNone(vistos[0][2], "hay que pasarle desde cuando mirar")

    def test_costo_es_cero_si_el_job_nunca_prendio_pane(self):
        m = Mundo(credits=[100.0, 100.25]).responder(
            lambda a: a[0] == "git" and "worktree" in a and "add" in a,
            (False, "fatal: no pathspec"))
        res, _ = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(res[0].costo, 0.0)

    def test_costo_de_la_corrida_por_diferencia_de_creditos(self):
        m = Mundo(credits=[100.0, 100.25])
        _, lineas = despachar(m, [job()])
        costo = [l for l in lineas if l["tipo"] == "costo" and l["ref"] == "corrida"]
        self.assertEqual(len(costo), 1)
        self.assertIn("0.25", costo[0]["cuerpo"])

    def test_sin_creditos_no_hay_evento_de_corrida(self):
        m = Mundo(credits=[None, None])
        _, lineas = despachar(m, [job()])
        self.assertNotIn("corrida", [l["ref"] for l in lineas
                                     if l["tipo"] == "costo"])


class TestParalelismo(unittest.TestCase):
    def test_el_tope_linda_el_concurrencia(self):
        import time as _t

        def espera_lenta(args):
            _t.sleep(0.05)
            return (True, '{"result":{"agent":{"agent_status":"idle"}}}')

        m = Mundo().responder(
            lambda a: a[:3] == ["herdr", "agent", "wait"], espera_lenta)
        res, _ = despachar(m, [job(i) for i in (1, 2, 3, 4)], max_parallel=2)
        self.assertEqual(len(res), 4)  # la cola entera se drena
        self.assertEqual(m.max_activo, 2)  # pero nunca más de 2 a la vez

    def test_max_configurable(self):
        m = Mundo()
        res, _ = despachar(m, [job(i) for i in (1, 2, 3)], max_parallel=1)
        self.assertEqual(m.max_activo, 1)
        self.assertEqual(len(res), 3)


class TestCosecha(unittest.TestCase):
    def test_cierra_el_issue_dejado_abierto_por_el_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = Mundo()
            m.prs = [{"number": 30, "headRefName": "ticket/7",
                      "closingIssuesReferences": [{"number": 7}]}]
            m.responder(lambda a: a[:3] == ["gh", "issue", "view"],
                        (True, '{"state":"OPEN"}'))
            log = log_en(tmp)
            cerrados = cosechar("Drokoz/koku", m.cmd, log)
            self.assertEqual(cerrados, [7])
            self.assertEqual(len(m.llamo("gh", "issue", "close", "7")), 1)
            lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
            self.assertEqual(lineas[0]["tipo"], "issue-cerrado")

    def test_no_toque_un_issue_que_ya_esta_cerrado(self):
        m = Mundo()
        m.prs = [{"number": 30, "headRefName": "ticket/7",
                  "closingIssuesReferences": [{"number": 7}]}]
        m.responder(lambda a: a[:3] == ["gh", "issue", "view"],
                    (True, '{"state":"CLOSED"}'))
        with tempfile.TemporaryDirectory() as tmp:
            cerrados = cosechar("Drokoz/koku", m.cmd, log_en(tmp))
        self.assertEqual(cerrados, [])
        self.assertEqual(m.llamo("gh", "issue", "close"), [])

    def test_cierra_lo_que_el_cuerpo_dice_y_la_api_no_vinculo(self):
        """#30: `closingIssuesReferences` vacío pero `Closes #7` en el cuerpo."""
        with tempfile.TemporaryDirectory() as tmp:
            m = Mundo()
            m.prs = [{"number": 30, "headRefName": "ticket/7",
                      "body": "Refactor y cierra el todo: Closes #7",
                      "closingIssuesReferences": []}]
            m.responder(lambda a: a[:3] == ["gh", "issue", "view"],
                        (True, '{"state":"OPEN"}'))
            log = log_en(tmp)
            cerrados = cosechar("Drokoz/koku", m.cmd, log)
            self.assertEqual(cerrados, [7])
            self.assertEqual(len(m.llamo("gh", "issue", "close", "7")), 1)
            lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
            self.assertEqual(lineas[0]["tipo"], "issue-cerrado")
            self.assertIn("no registro el vinculo", lineas[0]["cuerpo"])

    def test_no_cierra_referencias_cruez_a_otros_repos(self):
        """#30: `Closes owner/otro-repo#7` no se cierra sola."""
        m = Mundo()
        m.prs = [{"number": 30, "headRefName": "ticket/7",
                  "body": "Closes owner/otro-repo#7",
                  "closingIssuesReferences": []}]
        m.responder(lambda a: a[:3] == ["gh", "issue", "view"],
                    (True, '{"state":"OPEN"}'))
        with tempfile.TemporaryDirectory() as tmp:
            cerrados = cosechar("Drokoz/koku", m.cmd, log_en(tmp))
        self.assertEqual(cerrados, [])
        self.assertEqual(m.llamo("gh", "issue", "view"), [])
        self.assertEqual(m.llamo("gh", "issue", "close"), [])

    def test_el_issue_cerrado_citado_en_el_cuerpo_no_se_toca(self):
        m = Mundo()
        m.prs = [{"number": 30, "headRefName": "ticket/7",
                  "body": "Closes #7",
                  "closingIssuesReferences": []}]
        m.responder(lambda a: a[:3] == ["gh", "issue", "view"],
                    (True, '{"state":"CLOSED"}'))
        with tempfile.TemporaryDirectory() as tmp:
            cerrados = cosechar("Drokoz/koku", m.cmd, log_en(tmp))
        self.assertEqual(cerrados, [])
        self.assertEqual(m.llamo("gh", "issue", "close"), [])

    def test_el_vinculo_de_la_api_se_anota_como_su_propio_caso(self):
        """Si la API sí vinculó, el cierre no se anota como fallo de vínculo."""
        with tempfile.TemporaryDirectory() as tmp:
            m = Mundo()
            m.prs = [{"number": 30, "headRefName": "ticket/7",
                      "body": "Closes #7",
                      "closingIssuesReferences": [{"number": 7}]}]
            m.responder(lambda a: a[:3] == ["gh", "issue", "view"],
                        (True, '{"state":"OPEN"}'))
            log = log_en(tmp)
            cerrados = cosechar("Drokoz/koku", m.cmd, log)
            self.assertEqual(cerrados, [7])
            self.assertEqual(len(m.llamo("gh", "issue", "close", "7")), 1)
            lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
            self.assertNotIn("no registro el vinculo", lineas[0]["cuerpo"])


class TestSnapshotPath(unittest.TestCase):
    def test_el_repo_lleva_su_path(self):
        from harness import snapshot as snap
        raw = support.raw_personal()
        raw["repos"][0]["path"] = "/repos/agent-harness"
        repos = snap.snapshot({"offline": False, "agents": None,
                               "contexts": [raw]}).contexts[0].repos
        self.assertEqual(repos[0].path, "/repos/agent-harness")


class TestCliRun(unittest.TestCase):
    """`harness run` en el CLI: el contexto manual no toma nada solo, y sin
    herdr ni adaptadores no corre."""

    DOS_CONTEXTOS = json.dumps({
        "default_context": "manual-ctx",
        "contexts": {
            "manual-ctx": {"tracker": {"kind": "github"},
                           "repos": {"root": "/no/existe", "paths": []},
                           "autonomy": "manual",
                           "budget": {"polarity": "remaining", "provider": "none"},
                           "run": {"kind": "local"}},
            "frontier-ctx": {"tracker": {"kind": "github"},
                             "repos": {"root": "/no/existe", "paths": []},
                             "autonomy": "frontier",
                             "budget": {"polarity": "remaining", "provider": "none"},
                             "run": {"kind": "local"}},
        },
    })

    def _run(self, *args, env_extra=None):
        env_extra = env_extra or {}
        with tempfile.TemporaryDirectory() as tmp:
            import os as _os
            env = dict(_os.environ)
            env["HOME"] = tmp
            env["HARNESS_CONFIG_DIR"] = tmp
            env["HARNESS_OFFLINE"] = "1"
            env["HERDR_ENV"] = ""
            env.pop("HERDR_ENV", None)
            env.update(env_extra)
            (Path(tmp) / "config.json").write_text(self.DOS_CONTEXTOS)
            return subprocess.run([str(HARNESS), *args], env=env,
                                  capture_output=True, text=True, timeout=120)

    def test_contexto_manual_no_toma_nada(self):
        p = self._run("run", "--context", "manual-ctx")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("autonomía manual", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_frontier_sin_herdr_no_corre(self):
        p = self._run("run", "--context", "frontier-ctx")
        self.assertEqual(p.returncode, 2)
        self.assertIn("herdr", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()


class TestElAgenteArranca(unittest.TestCase):
    """Los tres motivos por los que la corrida del 2026-08-23 no despachó nada.

    Cada uno mataba tickets distintos y ninguno tenía test.
    """

    def test_confia_el_worktree_antes_de_arrancar_claude(self):
        """Claude Code pregunta "¿confiás en esta carpeta?" en cada directorio
        nuevo, y un worktree siempre lo es. El agente queda `blocked` en el
        diálogo sin haber escrito una línea."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "claude.json"
            cfg.write_text(json.dumps({"projects": {"/otro": {"x": 1}}}))
            dispatch.confiar_en("/repos/.worktrees/koku-ticket-7", cfg)
            d = json.loads(cfg.read_text())
            self.assertTrue(
                d["projects"]["/repos/.worktrees/koku-ticket-7"]["hasTrustDialogAccepted"])
            self.assertEqual(d["projects"]["/otro"], {"x": 1},
                             "no se tocan los otros proyectos")

    def test_confiar_es_idempotente_y_no_pisa_lo_que_ya_hay(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "claude.json"
            cfg.write_text(json.dumps(
                {"projects": {"/w": {"hasTrustDialogAccepted": True, "otra": 2}}}))
            dispatch.confiar_en("/w", cfg)
            d = json.loads(cfg.read_text())
            self.assertEqual(d["projects"]["/w"]["otra"], 2)

    def test_reintenta_cuando_el_pane_todavia_no_tiene_shell(self):
        """`pane split` vuelve antes de que el shell esté listo, y
        `agent start` muere con agent_pane_busy. Es una carrera, no un
        error definitivo: se reintenta."""
        m = Mundo()
        intentos = []

        def start(args):
            intentos.append(1)
            if len(intentos) < 2:
                return (False, '{"error":{"code":"agent_pane_busy",'
                               '"message":"pane w9:p1 is not an available shell"}}')
            return (True, "")

        m.responder(lambda a: a[:3] == ["herdr", "agent", "start"], start)
        res, _ = despachar(m, [job()])
        self.assertGreaterEqual(len(intentos), 2, "no reintentó el arranque")
        self.assertNotEqual(res[0].motivo, "no arranco el agente")

    def test_el_prompt_llego_tambien_sin_la_barra_de_estado_de_pi(self):
        """La evidencia de que el prompt llegó era el contexto de la TUI de pi
        ("11.4%/262k"). Claude no imprime nada parecido, así que todo agente
        claude se declaraba perdido. herdr ya sabe el estado del agente."""
        m = Mundo(pane_out="una pantalla de claude, sin porcentajes de pi\n")
        m.responder(lambda a: a[:3] == ["herdr", "agent", "get"],
                    (True, '{"result":{"agent":{"agent_status":"working"}}}'))
        res, _ = despachar(m, [job()])
        self.assertNotIn("primer prompt perdido", res[0].motivo)


class TestElPromptSeEntrega(unittest.TestCase):
    """El prompt nunca se enviaba, y nadie se enteraba.

    `herdr agent prompt --timeout N` sin `--wait` es un error de uso —herdr
    contesta "--timeout requires --wait"— y el dispatcher descartaba el
    resultado. O sea: cero prompts entregados en toda la vida del dispatcher.
    Lo que fallaba después (la verificación) era el síntoma.
    """

    def test_el_prompt_se_manda_esperando_a_que_el_agente_arranque(self):
        m = Mundo()
        despachar(m, [job()])
        (p,) = m.llamo("herdr", "agent", "prompt")
        args = p[0]
        self.assertIn("--wait", args,
                      "--timeout sin --wait es un error de uso: herdr no manda nada")
        self.assertIn("--until", args)
        self.assertIn("working", args)

    def test_si_herdr_dice_que_se_atasco_se_reintenta(self):
        # Sin barra de pi: el veredicto tiene que salir de herdr, no del pane.
        m = Mundo(pane_out="ni un porcentaje a la vista\n")
        intentos = []

        def prompt(args):
            intentos.append(1)
            if len(intentos) < 2:
                return (False, '{"error":{"code":"agent_prompt_stalled"}}')
            return (True, '{"result":{"agent":{"agent_status":"working"}}}')

        m.responder(lambda a: a[:3] == ["herdr", "agent", "prompt"], prompt)
        res, _ = despachar(m, [job()])
        self.assertEqual(len(intentos), 2)
        self.assertNotIn("primer prompt perdido", res[0].motivo)

    def test_el_veredicto_es_de_herdr_y_no_de_leer_la_pantalla(self):
        """La TUI de pi era la única que publicaba el contexto en porcentaje.
        Preguntarle a herdr sirve para cualquier agente."""
        m = Mundo(pane_out="ni un porcentaje a la vista\n")
        m.responder(lambda a: a[:3] == ["herdr", "agent", "prompt"],
                    (True, '{"result":{"agent":{"agent_status":"working"}}}'))
        res, _ = despachar(m, [job()])
        self.assertNotIn("primer prompt perdido", res[0].motivo)


class TestElAgenteQueTermina(unittest.TestCase):
    """Claude se asienta en `done`, no en `idle`.

    El dispatcher esperaba `--until idle --until blocked`, así que un agente
    que ya había terminado —PR abierto y todo— no matcheaba ningún estado y el
    wait se quedaba colgado hasta el timeout: una hora de reloj por ticket
    terminado, con la corrida entera detrás haciendo cola.
    """

    def test_espera_tambien_por_done(self):
        m = Mundo()
        despachar(m, [job()])
        (w,) = m.llamo("herdr", "agent", "wait")
        self.assertIn("done", w[0], "un agente que termina en done no lo espera nadie")

    def test_done_no_es_estar_bloqueado(self):
        m = Mundo(wait_out='{"result":{"agent":{"agent_status":"done"}}}')
        res, _ = despachar(m, [job()])
        self.assertNotIn("bloqueado", res[0].motivo)


class TestElWorktreeQuedaUsable(unittest.TestCase):
    """Un worktree recién creado no puede correr el gate todavía.

    `git worktree add` trae lo versionado y nada más: no hay `node_modules` y no
    hay `.env.local` (está en .gitignore, como corresponde). El gate arranca por
    los tests, jest no existe, y muere en un segundo con un rojo que no tiene
    nada que ver con el código del ticket.

    Pasó con #70, #71 y #73 en la tanda del 2026-08-23: tres tickets abandonados
    por deuda de infraestructura, no por su trabajo.
    """

    def test_instala_segun_el_lockfile(self):
        self.assertEqual(dispatch.comando_de_instalacion(["yarn.lock"])[0], "yarn")
        self.assertEqual(dispatch.comando_de_instalacion(["pnpm-lock.yaml"])[0], "pnpm")
        self.assertEqual(dispatch.comando_de_instalacion(["package-lock.json"])[0], "npm")

    def test_sin_lockfile_no_instala_nada(self):
        """Un repo de Go no tiene nada que instalar: no inventar un comando."""
        self.assertIsNone(dispatch.comando_de_instalacion(["go.mod"]))

    def test_el_lockfile_manda_sobre_package_json(self):
        """Con los dos presentes gana el lockfile, que es lo que fija versiones."""
        cmd = dispatch.comando_de_instalacion(["package.json", "yarn.lock"])
        self.assertEqual(cmd[0], "yarn")

    def test_copia_los_env_que_git_no_trae(self):
        with tempfile.TemporaryDirectory() as tmp:
            origen, destino = Path(tmp) / "repo", Path(tmp) / "wt"
            origen.mkdir(), destino.mkdir()
            (origen / ".env.local").write_text("SECRETO=1")
            (origen / ".env.template").write_text("SECRETO=")
            (origen / "package.json").write_text("{}")
            copiados = dispatch.copiar_entorno(origen, destino)
            self.assertEqual((destino / ".env.local").read_text(), "SECRETO=1")
            self.assertIn(".env.local", copiados)
            self.assertFalse((destino / "package.json").exists(),
                             "sólo los .env, no el repo entero")

    def test_no_pisa_un_env_que_el_worktree_ya_tenga(self):
        with tempfile.TemporaryDirectory() as tmp:
            origen, destino = Path(tmp) / "repo", Path(tmp) / "wt"
            origen.mkdir(), destino.mkdir()
            (origen / ".env.local").write_text("del repo")
            (destino / ".env.local").write_text("versionado")
            dispatch.copiar_entorno(origen, destino)
            self.assertEqual((destino / ".env.local").read_text(), "versionado")


class TestElOrdenDelPreparado(unittest.TestCase):
    """Instalar va ANTES de copiar los .env, y no al revés.

    El `.env` de ENTREVESTIDOS-BACK fija `NODE_ENV=production`. Copiado antes
    del install, yarn omite las devDependencies —donde vive jest— y los tests
    del repo no pueden correr: `jest: command not found`.

    El worktree de un agente siempre es un entorno de desarrollo, sin importar
    lo que diga el .env de producción del repo.
    """

    def test_instala_antes_de_copiar_el_entorno(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = Path(tmp) / "repo", Path(tmp) / "wt"
            repo.mkdir(), wt.mkdir()
            (repo / ".env").write_text("NODE_ENV=production")
            (wt / "yarn.lock").write_text("")
            (wt / "package.json").write_text("{}")

            orden = []
            m = Mundo()
            m.responder(lambda a: a[0] in ("yarn", "pnpm", "npm"),
                        lambda a: (orden.append("instalar"), (True, ""))[1])

            d = Dispatcher(spec(), log_en(tmp), m.cmd, dormir=m.dormir)
            j = Job(repo="r", repo_path=str(repo), slug="o/r", issue=7)
            j.worktree = str(wt)

            original = dispatch.copiar_entorno

            def espiar(a, b):
                orden.append("copiar_entorno")
                return original(a, b)

            dispatch.copiar_entorno = espiar
            try:
                d._preparar(j, "ticket/7")
            finally:
                dispatch.copiar_entorno = original

            self.assertEqual(orden, ["instalar", "copiar_entorno"],
                             "el .env de producción no puede estar puesto al instalar")


class TestElGateMideLoQueVaEnElPR(unittest.TestCase):
    """El gate corre en el worktree, pero el PR contiene la rama (#36).

    Un cambio sin commitear hace verde el gate sobre código que no va en el
    PR; y un agente trabado puede hacer verde el gate editándolo. Las dos
    vías de escape cierran aquí: árbol limpio antes del gate, HEAD de la
    rama == head del PR, y ningún diff que toque el gate, los workflows o la
    config del runner de tests.
    """

    def test_arbol_sucio_no_corre_el_gate_y_no_abre_pr(self):
        m = Mundo()
        m.responder(lambda a: a[0] == "git" and "status" in a,
                    (True, " M harness/dispatch.py\n?? basura.txt\n"))
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        self.assertIn("sucio", res[0].motivo)
        self.assertEqual(m.llamo("./scripts/gate.sh"), [],
                         "no se corre el gate sobre un árbol sucio")
        self.assertNotIn("pr", [l["tipo"] for l in lineas])

    def test_arbol_sucio_y_gate_rojo_no_es_lo_mismo(self):
        """Son problemas distintos: uno es el estado del worktree antes del
        gate, el otro es el veredicto del gate."""
        sucio_m = Mundo()
        sucio_m.responder(lambda a: a[0] == "git" and "status" in a,
                          (True, " M x.py\n"))
        (j_sucio,), _ = despachar(sucio_m, [job()])
        (j_rojo,), _ = despachar(Mundo(gate=(False, "ROJO: unittest")), [job()])
        self.assertIn("sucio", j_sucio.motivo)
        self.assertIn("gate", j_rojo.motivo)
        self.assertNotIn("sucio", j_rojo.motivo,
                         "el gate rojo no se confunde con árbol sucio")
        self.assertNotIn("gate", j_sucio.motivo,
                         "árbol sucio no llega a correr el gate")

    def test_pr_que_no_apunta_al_head_no_cuenta(self):
        """El gate midió el HEAD de la rama; si el PR apunta a otro commit,
        el verde no aplica a lo que va a mergear."""
        m = Mundo(pr_head_oid="b" * 40)
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        self.assertIn("head", res[0].motivo)
        self.assertNotIn("pr", [l["tipo"] for l in lineas])

    def test_diff_que_toca_el_gate_se_rechaza(self):
        m = Mundo(pr_files=["harness/dispatch.py", "scripts/gate.sh"])
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        self.assertIn("gate.sh", res[0].motivo)
        (marco,) = m.llamo("gh", "issue", "edit", "--add-label", "ready-for-human",
                           "--remove-label", "ready-for-agent")
        self.assertEqual(marco[0][3], "7")
        self.assertNotIn("pr", [l["tipo"] for l in lineas])

    def test_workflow_y_config_del_runner_tambien_estan_protegidos(self):
        for ruta in (".github/workflows/ci.yml", "jest.config.js"):
            m = Mundo(pr_files=[ruta])
            res, _ = despachar(m, [job()])
            self.assertEqual(res[0].estado, "abandonado", (ruta, res[0].motivo))

    def test_marcar_para_humano_saca_ready_for_agent(self):
        """`_marcar_para_humano` no sólo agrega `ready-for-human`: tiene que
        sacar `ready-for-agent` en la misma llamada, o el ticket sigue en la
        frontera y la próxima corrida lo vuelve a despachar (#52)."""
        m = Mundo()
        d = Dispatcher(spec(), log_en(tempfile.mkdtemp()), m.cmd, dormir=m.dormir)
        j = job()

        d._marcar_para_humano(j, "ticket/7")

        (llamada,) = m.llamo("gh", "issue", "edit")
        args = llamada[0]
        self.assertIn("7", args)
        self.assertIn("Drokoz/koku", args)
        i_add = args.index("--add-label")
        self.assertEqual(args[i_add + 1], "ready-for-human")
        i_remove = args.index("--remove-label")
        self.assertEqual(args[i_remove + 1], "ready-for-agent")

    def test_ruta_protegida(self):
        self.assertEqual(dispatch.ruta_protegida("scripts/gate.sh"), "el gate")
        self.assertEqual(dispatch.ruta_protegida(".github/workflows/ci.yml"),
                         "los workflows")
        self.assertEqual(dispatch.ruta_protegida("jest.config.js"),
                         "la config del runner de tests")
        self.assertIsNone(dispatch.ruta_protegida("harness/dispatch.py"))
        self.assertIsNone(dispatch.ruta_protegida("README.md"))
        self.assertIsNone(dispatch.ruta_protegida("docs/scripts/gate.sh"),
                          "sólo el gate del repo, no uno homónimo en otro path")


class TestRamaSinCommits(unittest.TestCase):
    """#67: una rama con cero commits sobre su base produce exactamente el
    mismo gate verde que una que trabajó bien (#52 pasó el gate así, y #45
    otra vez). Verde significa "no rompe nada", no "hizo algo": sin commits
    no se corre el gate, se abandona con motivo propio, no se escribe
    `gate: verde` en el log y no se abre PR.
    """

    @staticmethod
    def _mundo_sin_commits():
        m = Mundo()
        m.responder(lambda a: a[0] == "git" and "rev-list" in a, (True, "0\n"))
        return m

    def test_rama_sin_commits_no_corre_el_gate_y_no_abre_pr(self):
        m = self._mundo_sin_commits()
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        self.assertIn("commits", res[0].motivo)
        self.assertEqual(res[0].clase_abandono, "modelo")
        self.assertEqual(m.llamo("./scripts/gate.sh"), [],
                         "no se corre el gate sobre una rama sin commits")
        self.assertNotIn("pr", [l["tipo"] for l in lineas])

    def test_rama_sin_commits_no_escribe_gate_verde(self):
        """El evento `gate: verde` miente en el historial: #35 lo contaba
        como gate pasado y #38 lo escalaba como trabajo hecho."""
        _, lineas = despachar(self._mundo_sin_commits(), [job()])
        self.assertFalse(any(l["tipo"] == "gate"
                             and str(l["cuerpo"]).startswith("verde")
                             for l in lineas))

    def test_rama_sin_commits_no_se_confunde_con_sucio_ni_rojo(self):
        """Tres problemas distintos, tres motivos distintos: el árbol
        sucio (estado del worktree), la rama sin commits (no hay trabajo
        que medir) y el gate rojo (el veredicto del gate)."""
        (j,), _ = despachar(self._mundo_sin_commits(), [job()])
        self.assertNotIn("sucio", j.motivo)
        self.assertNotIn("gate", j.motivo,
                         "el motivo sin commits no se mezcla con el del gate")
        sucio = Mundo()
        sucio.responder(lambda a: a[0] == "git" and "status" in a, (True, " M x\n"))
        (j_sucio,), _ = despachar(sucio, [job()])
        (j_rojo,), _ = despachar(Mundo(gate=(False, "ROJO")), [job()])
        self.assertNotIn("commits", j_sucio.motivo)
        self.assertNotIn("commits", j_rojo.motivo)


class TestWatchdog(unittest.TestCase):
    """El watchdog de progreso (#41): un agente en bucle no se espera una
    hora. Cada `watchdog_check_min` minutos (default 5) se mide si hay
    progreso — commit nuevo en la rama, contexto que sube o costo que
    sube — y sin progreso durante `watchdog_kill_min` minutos (default 12)
    se mata al agente y se abandona `modelo`. Un agente corriendo el gate
    no se toca: esa espera es legítima y puede pasar el umbral."""

    TIMEOUT = '{"error":{"code":"timeout"},"id":"cli:agent:wait"}'

    @staticmethod
    def _pantalla(ctx):
        return ("⠏ Working...\n~/repo (ticket/7)\n"
                "↑12k ↓2k R4k CH0.1% $0.056 {}%/262k ...\n").format(ctx)

    def _wait_con_timeouts_luego_idle(self, m, timeouts):
        """`agent wait`: `timeout` las primeras N veces, `idle` después."""
        def wait(args):
            if len(m.llamo("herdr", "agent", "wait")) <= timeouts:
                return (True, self.TIMEOUT)
            return (True, '{"result":{"agent":{"agent_status":"idle"}}}')
        m.responder(lambda a: a[:3] == ["herdr", "agent", "wait"], wait)

    def _pane_por_espera(self, m, pantallas):
        """`pane read`: la pantalla i según cuántas esperas ya hubo.

        Las lecturas antes de la primera espera (verificar el prompt, la
        señal base del watchdog) ven la primera pantalla.
        """
        def pane(args):
            i = min(len(m.llamo("herdr", "agent", "wait")),
                    len(pantallas) - 1)
            return (True, pantallas[i])
        m.responder(lambda a: a[:3] == ["herdr", "pane", "read"], pane)

    def test_defaults_cinco_minutos_de_chequeo_y_doce_de_umbral(self):
        s = spec()
        self.assertEqual(s.watchdog_check_min, 5)
        self.assertEqual(s.watchdog_kill_min, 12)

    def test_avanza_y_no_lo_matan(self):
        """El agente que avanza (el contexto sube en cada corte) se deja
        estar hasta que se asienta: sin watchdog, sin abandono."""
        m = Mundo()
        self._wait_con_timeouts_luego_idle(m, timeouts=2)
        self._pane_por_espera(m, [self._pantalla("1.2"), self._pantalla("3.4"),
                                  self._pantalla("5.6")])
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertNotIn("abandono", [l["tipo"] for l in lineas])
        self.assertEqual(m.llamo("herdr", "agent", "send-keys"), [])

    def test_un_commit_nuevo_cuenta_como_progreso(self):
        m = Mundo(pr_head_oid="b" * 40)  # el PR apunta al commit nuevo
        self._wait_con_timeouts_luego_idle(m, timeouts=1)
        # pane quieto, pero el HEAD de la rama cambia entre corte y corte
        heads = []

        def head(args):
            heads.append(1)
            oid = "a" * 40 if len(heads) <= 1 else "b" * 40
            return (True, oid + "\n")

        m.responder(lambda a: "rev-parse" in a and a[-1] == "HEAD", head)
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertEqual(m.llamo("herdr", "agent", "send-keys"), [])

    def test_no_avanza_y_lo_matan(self):
        """Commits quietos, contexto quieto, costo quieto, y el wait no se
        asienta: pasado el umbral se corta al agente (ctrl+c) y se abandona
        `modelo`."""
        m = Mundo()
        m.wait_out = self.TIMEOUT
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        self.assertEqual(res[0].clase_abandono, "modelo")
        self.assertIn("sin progreso", res[0].motivo)
        (matada,) = m.llamo("herdr", "agent", "send-keys")
        self.assertEqual(matada[0][-1], "ctrl+c")
        self.assertIn("abandono", [l["tipo"] for l in lineas])

    def test_un_agente_corriendo_el_gate_no_lo_matan(self):
        """El gate en un worktree limpio puede pasar de los 12 minutos: es
        una espera legítima. Con el gate en pantalla, cinco cortes sin
        progreso (25 min) no alcanzan el umbral."""
        gate = ("⠏ Working...\n~/repo (ticket/7)\n"
                "↑12k ↓2k R4k CH0.1% $0.056 1.2%/262k ...\n"
                "❯ ./scripts/gate.sh\nPASS tests/integration.test.js\n"
                "Tests: 51 passed\n")
        m = Mundo(pane_out=gate)
        self._wait_con_timeouts_luego_idle(m, timeouts=5)
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertNotIn("abandono", [l["tipo"] for l in lineas])
        self.assertEqual(m.llamo("herdr", "agent", "send-keys"), [])

    def test_el_log_deja_cuanto_estuvo_sin_progreso(self):
        """Para calibrar el umbral con datos (#41): cada corte sin progreso
        anota sus minutos en el log de eventos."""
        m = Mundo()
        m.wait_out = self.TIMEOUT
        _, lineas = despachar(m, [job()])
        cuerpos = [l["cuerpo"] for l in lineas if l["tipo"] == "watchdog"]
        self.assertTrue(any("sin progreso 5 min" in c for c in cuerpos), cuerpos)
        self.assertTrue(any("sin progreso 15 min" in c for c in cuerpos), cuerpos)

    def test_avanzando_siempre_mata_al_agotar_el_presupuesto(self):
        """El watchdog no sustituye al presupuesto total (`wait_ms`): un
        agente que avanza y nunca se asienta se abandona igual al agotarlo."""
        m = Mundo()
        self._wait_con_timeouts_luego_idle(m, timeouts=999)
        self._pane_por_espera(m, [self._pantalla("1.2"), self._pantalla("2.2")])
        res, _ = despachar(m, [job()], wait_ms=300_000)  # una ronda de 5 min
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        self.assertIn("timeout de espera", res[0].motivo)
        self.assertEqual(res[0].clase_abandono, "modelo")


class TestClasificacionDeAbandonos(unittest.TestCase):
    """Cada abandono lleva una clase -- infra, modelo o humano -- en el
    evento (#37). `infra` es la única que reintenta en el acto (hasta
    `infra_retries` veces) antes de rendirse; las otras dos son
    abandonos definitivos de ese intento, sin reintento local.
    """

    def _clase(self, lineas, ref="ticket/7"):
        (abandono,) = [l for l in lineas
                       if l["tipo"] == "abandono" and l["ref"] == ref]
        return abandono["clase"]

    def test_worktree_es_infra_y_reintenta(self):
        intentos = []
        m = Mundo()

        def add(args):
            intentos.append(1)
            return (False, "fatal: no pathspec")

        m.responder(lambda a: a[0] == "git" and "worktree" in a and "add" in a, add)
        res, lineas = despachar(m, [job()], infra_retries=2, infra_retry_wait_s=0)
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(res[0].clase_abandono, "infra")
        self.assertEqual(self._clase(lineas), "infra")
        self.assertEqual(len(intentos), 3)  # 1 + 2 reintentos

    def test_worktree_se_recupera_con_el_reintento(self):
        intentos = []
        m = Mundo()

        def add(args):
            intentos.append(1)
            if len(intentos) < 2:
                return (False, "fatal: no pathspec")
            return (True, "")

        m.responder(lambda a: a[0] == "git" and "worktree" in a and "add" in a, add)
        res, lineas = despachar(m, [job()], infra_retries=2, infra_retry_wait_s=0)
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertNotIn("abandono", [l["tipo"] for l in lineas])
        self.assertEqual(len(intentos), 2)

    def test_pane_es_infra_y_reintenta(self):
        m = Mundo()
        m.responder(lambda a: a[:2] == ["herdr", "pane"] and "split" in a,
                    (False, "fallo de pane"))
        res, lineas = despachar(m, [job()], infra_retries=1, infra_retry_wait_s=0)
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(res[0].clase_abandono, "infra")
        self.assertEqual(self._clase(lineas), "infra")
        self.assertEqual(len(m.llamo("herdr", "pane", "split")), 2)  # 1 + 1 reintento

    def test_arranque_del_agente_es_infra(self):
        m = Mundo()
        m.responder(lambda a: a[:3] == ["herdr", "agent", "start"],
                    (False, '{"error":{"code":"algo raro"}}'))
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(self._clase(lineas), "infra")

    def test_prompt_perdido_es_infra(self):
        m = Mundo(pane_out=PANE_CERO)
        res, lineas = despachar(m, [job()], verify_wait_s=0.01,
                                verify_poll_s=0.01, verify_retries=1,
                                prompt_requeues=0)
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(self._clase(lineas), "infra")

    def test_agente_bloqueado_es_humano(self):
        m = Mundo(wait_out='{"result":{"agent":{"agent_status":"blocked"}}}')
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(res[0].clase_abandono, "humano")
        self.assertEqual(self._clase(lineas), "humano")

    def test_timeout_de_espera_es_modelo(self):
        m = Mundo()
        m.wait_out = '{"error":{"code":"timeout"},"id":"cli:agent:wait"}'
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].clase_abandono, "modelo")
        self.assertEqual(self._clase(lineas), "modelo")

    def test_fallo_al_esperar_es_infra_y_reintenta(self):
        llamadas = []
        m = Mundo()

        def wait(args):
            llamadas.append(1)
            return (False, '{"error":{"code":"conexion perdida"}}')

        m.responder(lambda a: a[:3] == ["herdr", "agent", "wait"], wait)
        res, lineas = despachar(m, [job()], infra_retries=2, infra_retry_wait_s=0)
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(self._clase(lineas), "infra")
        self.assertEqual(len(llamadas), 3)

    def test_gate_rojo_es_modelo(self):
        m = Mundo(gate=(False, "ROJO: unittest"))
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].clase_abandono, "modelo")
        self.assertEqual(self._clase(lineas), "modelo")

    def test_arbol_sucio_es_modelo(self):
        m = Mundo()
        m.responder(lambda a: a[0] == "git" and "status" in a,
                    (True, " M harness/dispatch.py\n"))
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].clase_abandono, "modelo")
        self.assertEqual(self._clase(lineas), "modelo")

    def test_git_status_fallido_es_infra_y_reintenta(self):
        llamadas = []
        m = Mundo()

        def status(args):
            llamadas.append(1)
            return (False, "git: error")

        m.responder(lambda a: a[0] == "git" and "status" in a, status)
        res, lineas = despachar(m, [job()], infra_retries=2, infra_retry_wait_s=0)
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(self._clase(lineas), "infra")
        # 1 + 2 reintentos, más uno del rescate del abandono (#66):
        # antes de borrar el worktree hay que saber si queda algo sin commitear.
        self.assertEqual(len(llamadas), 4)

    def test_sin_pr_es_modelo(self):
        m = Mundo(prs=[{"number": 31, "headRefName": "otra-rama"}])
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].clase_abandono, "modelo")
        self.assertEqual(self._clase(lineas), "modelo")

    def test_head_no_coincide_es_modelo(self):
        m = Mundo(pr_head_oid="b" * 40)
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].clase_abandono, "modelo")
        self.assertEqual(self._clase(lineas), "modelo")

    def test_pr_toca_protegido_es_modelo(self):
        m = Mundo(pr_files=["scripts/gate.sh"])
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].clase_abandono, "modelo")
        self.assertEqual(self._clase(lineas), "modelo")

    def test_no_leer_archivos_del_pr_es_infra_y_reintenta(self):
        llamadas = []
        m = Mundo()

        def ver_archivos(args):
            llamadas.append(1)
            return (False, "")

        m.responder(lambda a: a[:2] == ["gh", "pr"] and "view" in a and "files" in a,
                    ver_archivos)
        res, lineas = despachar(m, [job()], infra_retries=2, infra_retry_wait_s=0)
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(self._clase(lineas), "infra")
        self.assertEqual(len(llamadas), 3)

    def test_causa_desconocida_por_default_es_modelo(self):
        """El default conservador: sin clase explicita, gasta peldaño."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Dispatcher(spec(), log_en(tmp), Mundo().cmd)
            j = job()
            d._abandonar(j, "ticket/7", "algo que no se anticipo")
        self.assertEqual(j.clase_abandono, "modelo")


class TestEscaleraDispatcher(unittest.TestCase):
    """Un test por peldaño (#38), contra el mundo scripteado: kind, modelo
    y thinking/effort que le llegan a `herdr agent start`, sobre un Job ya
    resuelto por `peldano_de` -- lo mismo que arma `bin/harness` antes de
    llamar a `Dispatcher.dispatch`."""

    def _args_de_start(self, m):
        (start,) = m.llamo("herdr", "agent", "start")
        return list(start[0])

    def test_peldano_1_pi_qwen_thinking_medio(self):
        m = Mundo()
        res, _ = despachar(m, [job_de_peldano(0)])
        args = self._args_de_start(m)
        self.assertEqual(args[args.index("--kind") + 1], "pi")
        cola = args[args.index("--") + 1:]
        self.assertEqual(cola, ["--model", "qwen/qwen3.8-27b", "--thinking", "medium"])
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)

    def test_peldano_2_thinking_alto_y_cola_el_gate_en_el_prompt(self):
        m = Mundo()
        j = job_de_peldano(1, gate_tail="ROJO: fallo del intento anterior")
        despachar(m, [j])
        args = self._args_de_start(m)
        cola = args[args.index("--") + 1:]
        self.assertEqual(cola, ["--model", "qwen/qwen3.8-27b", "--thinking", "high"])
        (prompt,) = m.llamo("herdr", "agent", "prompt")
        texto = prompt[0][4]
        self.assertIn("ROJO: fallo del intento anterior", texto)
        self.assertNotIn("\n", texto)

    def test_peldano_3_claude_sonnet_effort_medio(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(dispatch, "CLAUDE_JSON", Path(tmp) / "claude.json"):
                m = Mundo()
                res, _ = despachar(m, [job_de_peldano(2)])
        args = self._args_de_start(m)
        self.assertEqual(args[args.index("--kind") + 1], "claude")
        cola = args[args.index("--") + 1:]
        self.assertEqual(cola, ["--model", "sonnet", "--effort", "medium"])
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)

    def test_peldano_4_claude_opus_effort_medio(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(dispatch, "CLAUDE_JSON", Path(tmp) / "claude.json"):
                m = Mundo()
                res, _ = despachar(m, [job_de_peldano(3)])
        args = self._args_de_start(m)
        cola = args[args.index("--") + 1:]
        self.assertEqual(cola, ["--model", "opus", "--effort", "medium"])


class TestWipEnElPrompt(unittest.TestCase):
    """#80: el dispatcher le manda al agente el prompt con el WIP cuando el
    job lo trae (lo arma `bin/harness` de `state.ultimo_wip`), y el de
    siempre cuando no. Mismo patrón que la cola del gate (#38): el Job ya
    resuelto entra, y se verifica lo que sale por `herdr agent prompt`."""

    def _prompt_enviado(self, m):
        (prompt,) = m.llamo("herdr", "agent", "prompt")
        return prompt[0][4]

    def test_un_job_con_wip_envia_el_prompt_con_wip(self):
        m = Mundo()
        j = job(wip=(2, "gate rojo en el worktree: sin PR",
                     "/logs/koku-ticket-7-intento-2.log"))
        res, _ = despachar(m, [j])
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        texto = self._prompt_enviado(m)
        self.assertIn("WIP commit left by the abandoned attempt 2", texto)
        self.assertIn("gate rojo en el worktree: sin PR", texto)
        self.assertIn("resume from that WIP commit", texto)
        self.assertIn("discard it and start from scratch", texto)
        self.assertIn("legitimate choice", texto)
        self.assertIn("transcript at /logs/koku-ticket-7-intento-2.log", texto)
        self.assertIn("WIP decision", texto)
        self.assertNotIn("\n", texto)

    def test_un_job_sin_wip_envia_el_prompt_de_siempre(self):
        """Sin WIP previo, nada de ruido extra: el prompt no lo menciona."""
        m = Mundo()
        despachar(m, [job()])
        texto = self._prompt_enviado(m)
        self.assertNotIn("WIP", texto)
        self.assertNotIn("transcript", texto)

    def test_wip_y_cola_de_gate_viajan_juntos(self):
        m = Mundo()
        j = job(gate_tail="boom", wip=(1, "arbol sucio", "/t.log"))
        despachar(m, [j])
        texto = self._prompt_enviado(m)
        self.assertIn("boom", texto)
        self.assertIn("WIP", texto)
        self.assertNotIn("\n", texto)


class TestRouterSeam(unittest.TestCase):
    """El router (#38, seam para #45): si el peldaño es de Claude y hay una
    política inyectada que lo bloquea (piso de cuota, calendario -- #45),
    no se gasta nada -- ni worktree ni pane -- y el job queda pendiente,
    no abandonado: no consume peldaño."""

    def test_claude_bloqueado_por_el_router_no_gasta_peldano(self):
        m = Mundo()
        with tempfile.TemporaryDirectory() as tmp:
            log = log_en(tmp)
            d = Dispatcher(
                spec(permitir_claude=lambda: (False, "piso de cuota semanal")),
                log, m.cmd, credits=m.credits, dormir=m.dormir)
            res = d.dispatch([job_de_peldano(2)])
            lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
        self.assertEqual(res[0].estado, "pendiente")
        self.assertEqual(m.llamo("git", "worktree", "add"), [])
        self.assertEqual(m.llamo("herdr", "pane", "split"), [])
        peldano_lineas = [l for l in lineas if l["tipo"] == "peldano"]
        self.assertEqual(len(peldano_lineas), 1)
        self.assertIn("piso de cuota semanal", peldano_lineas[0]["cuerpo"])

    def test_pi_no_consulta_al_router(self):
        """El router sólo aplica a los peldaños de Claude: pi/Qwen sigue
        corriendo aunque haya una política inyectada."""
        m = Mundo()
        llamado = []

        def bloquear():
            llamado.append(1)
            return (False, "no deberia llamarse")

        res, _ = despachar(m, [job_de_peldano(0)], permitir_claude=bloquear)
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertEqual(llamado, [])


class TestParkear(unittest.TestCase):
    """Al agotar la escalera (#38): saca `ready-for-agent`, pone
    `ready-for-human` y comenta el motivo de cada intento."""

    def test_parkear_cambia_labels_y_comenta_los_motivos(self):
        m = Mundo()
        motivos = ["gate rojo en el worktree: sin PR",
                  "timeout de espera (3600000 ms)",
                  "agente bloqueado (aprobacion o pregunta pendiente)",
                  "el agente termino sin PR abierto"]
        with tempfile.TemporaryDirectory() as tmp:
            log = log_en(tmp)
            d = Dispatcher(spec(), log, m.cmd, credits=m.credits, dormir=m.dormir)
            d.parkear(job(attempt=5), "ticket/7", motivos)
            lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
        (edit,) = m.llamo("gh", "issue", "edit")
        self.assertEqual(edit[0][edit[0].index("--add-label") + 1], "ready-for-human")
        self.assertEqual(edit[0][edit[0].index("--remove-label") + 1], "ready-for-agent")
        (comentario,) = m.llamo("gh", "issue", "comment")
        cuerpo = comentario[0][comentario[0].index("--body") + 1]
        for motivo in motivos:
            self.assertIn(motivo, cuerpo)
        park_lineas = [l for l in lineas if l["tipo"] == "park"]
        self.assertEqual(len(park_lineas), 1)

    def test_sin_slug_no_comenta_pero_no_explota(self):
        m = Mundo()
        with tempfile.TemporaryDirectory() as tmp:
            log = log_en(tmp)
            d = Dispatcher(spec(), log, m.cmd, credits=m.credits, dormir=m.dormir)
            d.parkear(job(slug=""), "ticket/7", ["algo"])
        self.assertEqual(m.llamo("gh", "issue", "edit"), [])
        self.assertEqual(m.llamo("gh", "issue", "comment"), [])


class TestReintentoSobreBase(unittest.TestCase):
    """Rama reutilizada del reintento (#64): antes de despachar se rebasa
    sobre la base, para que un ticket que vuelve a la frontera no arranque
    sobre la base vieja. Si conflictúa, no se despacha a ciegas: se anota
    y se manda a la cola de mantenimiento (#56), que hoy es la frontera de
    humanos (etiqueta + comentario en el issue).
    """

    BASE_VIEJA = "b" * 40

    def mundo_con_rama_existente(self):
        m = Mundo()
        m.responder(
            lambda a: a[0] == "git" and "rev-parse" in a
            and "refs/heads/ticket/7" in a,
            (True, self.BASE_VIEJA + "\n"))
        return m

    def rebase_conflicto(self, m):
        return m.responder(
            lambda a: a[0] == "git" and "rebase" in a and "--abort" not in a,
            (False, "CONFLICT (content): Merge conflict in x.py\n"
                    "Rebasing (1/2)"))

    def rebases(self, m):
        return [c for c in m.llamo("git", "rebase") if "--abort" not in c[0]]

    def test_rama_vieja_se_rebasa_antes_de_despachar(self):
        m = self.mundo_con_rama_existente()
        m.head_oid = "c" * 40  # el tip que deja el rebase
        m.pr_head_oid = "c" * 40
        res, _ = despachar(m, [job()])
        self.assertEqual(res[0].estado, "hecho")
        self.assertEqual(len(self.rebases(m)), 1)
        self.assertIn("origin/main", self.rebases(m)[0][0])
        i_rebase = next(i for i, c in enumerate(m.llamadas) if "rebase" in c[0])
        i_agente = next(i for i, c in enumerate(m.llamadas)
                        if c[0][:3] == ("herdr", "agent", "start"))
        self.assertLess(i_rebase, i_agente,
                        "el rebase va antes de arrancar al agente")

    def test_el_log_deja_la_base_vieja_y_sobre_que_se_actualizo(self):
        m = self.mundo_con_rama_existente()
        m.head_oid = "c" * 40
        _, lineas = despachar(m, [job()])
        reutilizadas = [l for l in lineas if l["tipo"] == "worktree"
                        and "rama reutilizada" in l["cuerpo"]]
        self.assertEqual(len(reutilizadas), 1)
        self.assertIn(self.BASE_VIEJA[:8], reutilizadas[0]["cuerpo"])
        self.assertIn("origin/main", reutilizadas[0]["cuerpo"])

    def test_rama_nueva_no_cambia_de_comportamiento(self):
        m = Mundo()  # default: la rama no existe
        res, _ = despachar(m, [job()])
        self.assertEqual(res[0].estado, "hecho")
        self.assertEqual(self.rebases(m), [])
        (add,) = m.llamo("git", "worktree", "add")
        self.assertIn("-b", add[0])

    def test_rebase_que_conflictua_no_despacha(self):
        m = self.mundo_con_rama_existente()
        self.rebase_conflicto(m)
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "abandonado")
        self.assertEqual(res[0].clase_abandono, "humano")
        # El agente nunca arrancó.
        self.assertEqual(m.llamo("herdr", "agent", "start"), [])
        # No queda medio rebase en el worktree.
        self.assertEqual(len(m.llamo("git", "rebase", "--abort")), 1)

    def test_el_conflicto_manda_a_la_cola_de_mantenimiento(self):
        m = self.mundo_con_rama_existente()
        self.rebase_conflicto(m)
        _, lineas = despachar(m, [job()])
        # Fuera de la frontera de agentes, con el motivo en el issue (#56).
        (edit,) = m.llamo("gh", "issue", "edit")
        self.assertIn("ready-for-human", edit[0])
        self.assertIn("ready-for-agent", edit[0])
        self.assertEqual(len(m.llamo("gh", "issue", "comment")), 1)
        # Y se anota en el log.
        self.assertTrue(any(l["tipo"] == "park" for l in lineas))
        trabajo = [l for l in lineas if l["tipo"] == "worktree"
                   and "rama reutilizada" in l["cuerpo"]]
        self.assertEqual(len(trabajo), 1)
        self.assertIn(self.BASE_VIEJA[:8], trabajo[0]["cuerpo"])
        self.assertIn("origin/main", trabajo[0]["cuerpo"])

    def test_el_conflicto_no_gasta_reintento_de_infra(self):
        """Un rebase que conflictúa no es infra: no hay que reintentarlo dos
        veces más para volver a chocar con lo mismo, va directo a la cola."""
        m = self.mundo_con_rama_existente()
        self.rebase_conflicto(m)
        despachar(m, [job()])
        self.assertEqual(len(self.rebases(m)), 1)


class TestBaseUnica(unittest.TestCase):
    """#85: el dispatcher usaba dos bases distintas (rama nueva desde el
    HEAD del checkout principal, rama reutilizada rebasada sobre
    origin/main) y nunca hacía fetch, así que origin/main podía estar
    viejo. Una sola definición de base —la rama por defecto del remote,
    tras el fetch único de la corrida— la usan las tres cosas: crear la
    rama nueva, rebasar la reutilizada y contar commits."""

    ORIGEN = "d" * 40

    def mundo(self, rama_existente=False, **kw):
        m = Mundo(origin_oid=self.ORIGEN, **kw)
        if rama_existente:
            m.responder(
                lambda a: a[0] == "git" and "rev-parse" in a
                and "refs/heads/ticket/7" in a,
                (True, "b" * 40 + "\n"))
        return m

    def fetches(self, m):
        return m.llamo("git", "fetch", "origin", "main")

    def test_fetch_de_la_rama_por_defecto_antes_de_cualquier_worktree(self):
        m = self.mundo()
        despachar(m, [job()])
        self.assertEqual(len(self.fetches(m)), 1)
        i_fetch = next(i for i, c in enumerate(m.llamadas)
                       if "fetch" in c[0])
        i_wt = next(i for i, c in enumerate(m.llamadas)
                    if "worktree" in c[0] and "add" in c[0])
        self.assertLess(i_fetch, i_wt, "el fetch va antes de calcular la base")

    def test_fetch_uno_por_corrida_no_por_ticket(self):
        m = self.mundo()
        despachar(m, [job(7), job(8)])
        self.assertEqual(len(self.fetches(m)), 1)

    def test_fetch_uno_por_repo(self):
        m = self.mundo()
        despachar(m, [job(7, path="/repos/koku"),
                      job(7, repo="koku-api", path="/repos/koku-api")])
        self.assertEqual(len(self.fetches(m)), 2)

    def test_fetch_que_falla_no_voltea_la_corrida(self):
        m = self.mundo()
        m.responder(
            lambda a: a[0] == "git" and "fetch" in a,
            (False, "fatal: unable to access "
                    "'https://github.com/Drokoz/koku/': Failed to connect"))
        res, lineas = despachar(m, [job()])
        self.assertEqual(res[0].estado, "hecho")
        avisos = [l for l in lineas if l["tipo"] == "fetch"
                  and "vieja" in l["cuerpo"]]
        self.assertEqual(len(avisos), 1,
                         "queda anotado que la base puede estar vieja")

    def test_rama_nueva_nace_sobre_la_base_remota(self):
        m = self.mundo()
        despachar(m, [job()])
        (add,) = m.llamo("git", "worktree", "add")
        self.assertIn("origin/main", add[0])

    def test_rama_reutilizada_y_contador_usan_la_base_remota(self):
        m = self.mundo(rama_existente=True)
        m.head_oid = "c" * 40  # el tip que deja el rebase
        despachar(m, [job()])
        rebases = [c for c in m.llamo("git", "rebase") if "--abort" not in c[0]]
        self.assertEqual(len(rebases), 1)
        self.assertEqual(rebases[0][0][-1], "origin/main")
        (rev,) = m.llamo("git", "rev-list", "--count")
        self.assertEqual(rev[0][-1], self.ORIGEN + "..HEAD",
                         "contar commits mide contra la base, no contra el "
                         "HEAD del checkout principal")

    def test_la_base_efectiva_de_cada_job_queda_en_el_log_con_su_sha(self):
        m = self.mundo()
        _, lineas = despachar(m, [job(7), job(8)])
        bases = [l for l in lineas if l["tipo"] == "base"
                 and l["ref"].startswith("ticket/")]
        self.assertEqual(len(bases), 2)
        for l in bases:
            self.assertIn("origin/main", l["cuerpo"])
            self.assertIn(self.ORIGEN[:8], l["cuerpo"])


class TestBaseConGitReal(unittest.TestCase):
    """#85 con git de verdad: con la `main` local atrasada respecto de
    `origin/main` —lo normal después de mergear PRs desde GitHub sin
    hacer `git pull`—, rama nueva y rama reutilizada terminan sobre la
    misma base."""

    # La identidad va en CADA llamada, no configurada en un repo: el
    # `avance` es un `clone` y nadie se la ponía, así que `commit` salía 128
    # en cualquier máquina sin identidad global -- el runner de CI, por
    # ejemplo, donde el gate venía rojo desde el 2026-08-26 sin que nadie
    # mirara.
    # Ni la identidad ni la rama por defecto se dan por sentadas: las dos
    # salen de la config global, que en una máquina de trabajo está puesta y
    # en un runner no. Sin `-b main`, el bare nace con HEAD en `master`, el
    # clone commitea en `master`, y el `push origin main` no tiene qué
    # empujar --- que es exactamente cómo fallaba el gate en CI.
    IDENT = ("-c", "user.email=t@e.com", "-c", "user.name=T",
             "-c", "init.defaultBranch=main")

    def _g(self, cwd, *args):
        subprocess.run(["git", *self.IDENT, *args], cwd=cwd, check=True,
                       capture_output=True, text=True)

    def _sha(self, cwd, ref):
        return subprocess.run(["git", "-C", str(cwd), "rev-parse", ref],
                              check=True, capture_output=True,
                              text=True).stdout.strip()

    def _repo(self, tmp):
        """(checkout con main atrasada, sha real de origin/main)."""
        origen = Path(tmp) / "origin.git"
        origen.mkdir()
        self._g(origen, "init", "--bare", "-b", "main")
        local = Path(tmp) / "koku"
        local.mkdir()
        self._g(local, "init", "-b", "main")
        self._g(local, "remote", "add", "origin", str(origen))
        (local / "f.txt").write_text("1\n")
        self._g(local, "add", ".")
        self._g(local, "commit", "-m", "c1")
        self._g(local, "push", "-u", "origin", "main")
        self._g(local, "remote", "set-head", "origin", "main")
        # origin/main avanza y la main local queda atrás: el caso que
        # antes dejaba los dos caminos sobre bases distintas.
        avance = Path(tmp) / "avance"
        subprocess.run(["git", "clone", str(origen), str(avance)],
                       check=True, capture_output=True, text=True)
        (avance / "f.txt").write_text("2\n")
        self._g(avance, "add", ".")
        self._g(avance, "commit", "-m", "c2")
        self._g(avance, "push", "origin", "main")
        return str(local), self._sha(avance, "HEAD")

    def test_nueva_y_reutilizada_terminan_sobre_la_misma_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha_origen = self._repo(tmp)
            # La reutilizada es de un intento anterior: nació sobre la
            # base vieja (la main local que quedó atrás) y tiene un
            # commit de trabajo.
            self._g(Path(repo), "branch", "ticket/8", "main")
            wt8 = Path(tmp) / "w8"
            self._g(Path(repo), "worktree", "add", str(wt8), "ticket/8")
            (wt8 / "g.txt").write_text("trabajo\n")
            self._g(wt8, "add", ".")
            self._g(wt8, "commit", "-m", "intento anterior")
            self._g(Path(repo), "worktree", "remove", "--force", str(wt8))
            with tempfile.TemporaryDirectory() as tmp2:
                log = log_en(tmp2)
                d = Dispatcher(spec(), log, adapters.run,
                               dormir=lambda s: None)
                j7 = job(issue=7, path=repo)
                j8 = job(issue=8, path=repo)
                d._fetch_base([j7, j8])
                self.assertTrue(d._worktree(j7, "ticket/7"))
                self.assertTrue(d._worktree(j8, "ticket/8"))
                self.assertEqual(self._sha(j7.worktree, "HEAD"), sha_origen,
                                 "rama nueva sobre la base")
                self.assertEqual(self._sha(j8.worktree, "HEAD^"), sha_origen,
                                 "rama reutilizada rebasada sobre la misma base")
                self.assertTrue(d._rama_con_commits(j8, "ticket/8"))
                # Cero commits contra la base = cero, medido contra la
                # misma base que usaron las otras dos.
                self.assertFalse(d._rama_con_commits(j7, "ticket/7"))
                lineas = [json.loads(l) for l in
                          log.path.read_text().splitlines()]
                bases = [l for l in lineas if l["tipo"] == "base"]
                self.assertEqual(len(bases), 2)
                for l in bases:
                    self.assertIn(sha_origen[:8], l["cuerpo"])


class TestElAbandonoDejaPistas(unittest.TestCase):
    """El abandono no borra ni el trabajo ni el porqué (#66).

    2026-08-24: el guard de árbol sucio (#36) rechazó bien un worktree con
    cambios sin commitear —y la limpieza los borró. US$0.43 y 19 minutos, y
    sin transcripción para saber por qué el agente se detuvo. El rescate va
    en el orden que las hace posibles: la transcripción con el pane todavía
    abierto, el commit WIP con el worktree todavía en pie. Un job que
    termina bien no deja ni lo uno ni lo otro.
    """

    def _job_en(self, tmp, **kw):
        (Path(tmp) / "koku").mkdir(parents=True, exist_ok=True)
        return job(path=str(Path(tmp) / "koku"), **kw)

    def _despachar(self, m, j, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            log = log_en(tmp)
            d = Dispatcher(spec(**kw), log, m.cmd, credits=m.credits,
                           dormir=m.dormir)
            res = d.dispatch([j])
            lineas = [json.loads(l) for l in log.path.read_text().splitlines()]
        return res, lineas

    def test_el_abandono_guarda_la_transcripcion_por_ticket_e_intento(self):
        tmp = tempfile.mkdtemp()
        m = Mundo(gate=(False, "ROJO: unittest"))
        salida = "linea 1\nlinea 2\n"
        m.responder(lambda a: a[:3] == ["herdr", "agent", "read"], (True, salida))
        j = self._job_en(tmp)
        res, lineas = self._despachar(m, j)
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        destino = dispatch.transcripcion_path(j.repo_path, j.issue, j.attempt)
        self.assertEqual(destino.parent, Path(tmp) / ".worktrees" / "logs")
        self.assertEqual(destino.name, "koku-ticket-7-intento-1.log",
                         "por ticket e intento, no por corrida")
        self.assertEqual(destino.read_text(), salida)
        # El log de eventos apunta al archivo: se va del abandono a la transcripción.
        (tc,) = [l for l in lineas if l["tipo"] == "transcripcion"]
        self.assertIn(str(destino), tc["cuerpo"])
        self.assertEqual(tc["ref"], "ticket/7")
        # Antes de cerrar el pane, no después.
        (read,) = m.llamo("herdr", "agent", "read")
        (close,) = m.llamo("herdr", "pane", "close")
        self.assertLess(m.llamadas.index(read), m.llamadas.index(close))

    def test_los_intentos_y_tickets_no_se_pisan(self):
        base = "/repos/koku"
        p = dispatch.transcripcion_path
        self.assertNotEqual(p(base, 7, 1), p(base, 7, 2), "intento distinto")
        self.assertNotEqual(p(base, 7, 1), p(base, 8, 1), "ticket distinto")

    def test_si_no_se_puede_leer_al_agente_se_le_al_pane(self):
        tmp = tempfile.mkdtemp()
        m = Mundo(gate=(False, "ROJO: unittest"))
        m.responder(lambda a: a[:3] == ["herdr", "agent", "prompt"],
                    (True, '{"result":{"agent":{"agent_status":"working"}}}'))
        m.responder(lambda a: a[:3] == ["herdr", "agent", "read"], (False, "fallo"))
        m.responder(lambda a: a[:3] == ["herdr", "pane", "read"], (True, "del pane\n"))
        j = self._job_en(tmp)
        res, _ = self._despachar(m, j)
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        destino = dispatch.transcripcion_path(j.repo_path, j.issue, j.attempt)
        self.assertEqual(destino.read_text(), "del pane\n")

    def test_sin_pane_ni_agente_no_hay_transcripcion(self):
        tmp = tempfile.mkdtemp()
        m = Mundo().responder(
            lambda a: a[0] == "git" and "worktree" in a and "add" in a,
            (False, "fatal: no pathspec"))
        j = self._job_en(tmp)
        res, lineas = self._despachar(m, j)
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        self.assertEqual(m.llamo("herdr", "agent", "read"), [])
        self.assertFalse((Path(tmp) / ".worktrees").exists())
        self.assertNotIn("transcripcion", [l["tipo"] for l in lineas])

    def test_el_podado_deja_los_mas_recientes_por_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for n in range(1, 8):
                (d / "koku-ticket-7-intento-{}.log".format(n)).write_text(str(n))
            (d / "koku-ticket-8-intento-1.log").write_text("vecino")
            (d / "koku-ticket-7-intento-2.txt").write_text("no es transcripcion")
            borrados = dispatch.podar_transcripciones(d, "koku", 7)
            self.assertEqual(borrados, ["koku-ticket-7-intento-1.log",
                                        "koku-ticket-7-intento-2.log"],
                             "de los más viejos, por intento, sin tocar vecinos")
            restantes = sorted(f.name for f in d.iterdir())
            self.assertEqual(restantes, [
                "koku-ticket-7-intento-2.txt",
                "koku-ticket-7-intento-3.log", "koku-ticket-7-intento-4.log",
                "koku-ticket-7-intento-5.log", "koku-ticket-7-intento-6.log",
                "koku-ticket-7-intento-7.log", "koku-ticket-8-intento-1.log"])

    def test_el_rescate_poda_los_mas_viejos(self):
        tmp = tempfile.mkdtemp()
        logs = Path(tmp) / ".worktrees" / "logs"
        logs.mkdir(parents=True)
        for n in range(1, 7):
            (logs / "koku-ticket-7-intento-{}.log".format(n)).write_text("viejo")
        m = Mundo(gate=(False, "ROJO: unittest"))
        m.responder(lambda a: a[:3] == ["herdr", "agent", "read"], (True, "nuevo\n"))
        j = self._job_en(tmp, attempt=7)
        res, _ = self._despachar(m, j)
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        restantes = sorted(f.name for f in logs.iterdir())
        self.assertEqual(restantes,
                         ["koku-ticket-7-intento-{}.log".format(n)
                          for n in range(3, 8)],
                         "las dos más viejas no sobreviven al rescate")

    def test_el_abandono_con_arbol_sucio_deja_un_commit_wip_en_la_rama(self):
        tmp = tempfile.mkdtemp()
        m = Mundo()
        m.responder(lambda a: a[0] == "git" and "status" in a,
                    (True, " M harness/dispatch.py\n?? evidencia.txt\n"))
        j = self._job_en(tmp)
        res, lineas = self._despachar(m, j)
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        self.assertIn("sucio", res[0].motivo)
        (add,) = m.llamo("git", "add", "-A")
        self.assertIn(j.worktree, add[0])
        (commit,) = m.llamo("git", "commit")
        self.assertIn(j.worktree, commit[0], "sobre la rama del worktree")
        msg = commit[0][commit[0].index("-m") + 1]
        self.assertIn("WIP", msg)
        self.assertIn("abandono", msg)
        self.assertIn("intento 1", msg)
        # El WIP viaja ANTES de que el worktree se borre.
        (remove,) = m.llamo("git", "worktree", "remove")
        self.assertLess(m.llamadas.index(commit), m.llamadas.index(remove))
        (wip,) = [l for l in lineas if l["tipo"] == "wip"]
        self.assertIn("WIP", wip["cuerpo"])

    def test_el_abandono_con_arbol_limpio_no_deja_wip(self):
        tmp = tempfile.mkdtemp()
        m = Mundo(gate=(False, "ROJO: unittest"))
        j = self._job_en(tmp)
        res, _ = self._despachar(m, j)
        self.assertEqual(res[0].estado, "abandonado", res[0].motivo)
        self.assertEqual(m.llamo("git", "add", "-A"), [])
        self.assertEqual(m.llamo("git", "commit"), [])

    def test_el_job_que_termina_bien_no_deja_wip_ni_transcripcion(self):
        tmp = tempfile.mkdtemp()
        m = Mundo()
        j = self._job_en(tmp)
        res, lineas = self._despachar(m, j)
        self.assertEqual(res[0].estado, "hecho", res[0].motivo)
        self.assertEqual(m.llamo("git", "commit"), [])
        self.assertEqual(m.llamo("herdr", "agent", "read"), [])
        self.assertNotIn("transcripcion", [l["tipo"] for l in lineas])
        self.assertNotIn("wip", [l["tipo"] for l in lineas])
        self.assertFalse((Path(tmp) / ".worktrees").exists())
