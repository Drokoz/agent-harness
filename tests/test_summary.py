"""Tests del resumen de la mañana (ticket #7) sobre logs sintéticos.

El log es el JSONL append-only del dispatcher (formato de
`harness.dispatch.EventLog`); acá se fabrica en un tmpdir. Los tres casos
que pide el ticket: log vacío, log normal y log de un solo evento, más el
comportamiento de la marca y de `--since` (filtrar por fecha).
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import support  # noqa: F401  (pone la raíz en sys.path)

from harness.summary import (PrAbierto, Resumen, Ticket, con_cuota, construir,
                             filtrar, guardar_marca, leer_eventos, leer_marca,
                             parse_evento, parse_fecha, reconciliar, resumir)

HARNESS = support.ROOT / "bin" / "harness"


def evento(timestamp, tipo, ref="?", contexto="personal", cuerpo="", ticket=None):
    return json.dumps({"timestamp": timestamp, "contexto": contexto,
                       "origen": "harness", "tipo": tipo, "ref": ref,
                       "ticket": ticket, "cuerpo": cuerpo}, ensure_ascii=False)


LOG_NORMAL = [
    evento("2026-08-21T23:00:00Z", "corrida", "corrida",
           cuerpo="inicio: 3 ticket(s), max 2 en paralelo"),
    evento("2026-08-22T01:10:00Z", "worktree", "ticket/3",
           cuerpo="/home/u/repo/.worktrees/agent-harness-ticket-3"),
    evento("2026-08-22T01:12:00Z", "pane", "ticket/3", cuerpo="w1:p9"),
    evento("2026-08-22T01:13:00Z", "agente", "ticket/3",
           cuerpo="agent-harness-3 (kind pi, pane w1:p9)"),
    evento("2026-08-22T01:14:00Z", "prompt", "ticket/3", cuerpo="intento 1"),
    evento("2026-08-22T03:00:00Z", "gate", "ticket/3", cuerpo="verde"),
    evento("2026-08-22T03:00:10Z", "pr", "ticket/3",
           cuerpo="PR #19 abierto (gate verde)", ticket="agent-harness#3"),
    evento("2026-08-22T03:00:20Z", "costo", "ticket/3", cuerpo="$0.0560"),
    evento("2026-08-22T03:01:00Z", "limpieza", "ticket/3",
           cuerpo="pane w1:p9 cerrado; worktree removido, rama ticket/3 quedo"),
    evento("2026-08-22T02:00:00Z", "abandono", "ticket/11",
           cuerpo="gate rojo en el worktree: sin PR"),
    evento("2026-08-22T02:05:00Z", "costo", "ticket/11", cuerpo="$0.2840"),
    evento("2026-08-22T05:00:00Z", "pr", "ticket/9",
           cuerpo="PR #20 abierto (gate verde)", ticket="agent-harness#9"),
    evento("2026-08-22T05:10:00Z", "costo", "ticket/9", cuerpo="$0.4300"),
    evento("2026-08-22T05:11:00Z", "costo", "corrida",
           cuerpo="creditos antes 25.0 / despues 24.18 / delta $0.8200"),
    evento("2026-08-22T05:11:01Z", "issue-cerrado", "#9",
           cuerpo="PR #20 merged pero el issue seguia abierto"),
    evento("2026-08-22T05:12:00Z", "corrida", "corrida",
           cuerpo="fin: 2 hecho(s), 1 abandonado(s), costo de jobs $0.77"),
]


def escribir_log(path, lineas):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lineas) + "\n", encoding="utf-8")


class TestParseo(unittest.TestCase):
    def test_linea_valida(self):
        e = parse_evento(evento("2026-08-22T01:00:00Z", "pr", "ticket/3"))
        self.assertEqual(e["tipo"], "pr")

    def test_lineas_que_no_cooperan(self):
        self.assertIsNone(parse_evento("esto no es json"))
        self.assertIsNone(parse_evento("[1, 2, 3]"))
        self.assertIsNone(parse_evento(json.dumps({"tipo": "pr"})))  # sin timestamp
        self.assertIsNone(parse_evento(json.dumps({"timestamp": "ayer a la tarde",
                                                   "tipo": "pr"})))


class TestLeerEventos(unittest.TestCase):
    def test_log_inexistente_es_vacio(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(leer_eventos(Path(tmp) / "no-existe" / "e.jsonl"), [])

    def test_lineas_rotas_se_saltan(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "e.jsonl"
            escribir_log(p, [evento("2026-08-22T01:00:00Z", "pr", "ticket/3"),
                             '{"timestamp": "cortado a mitad',
                             "   ",
                             evento("2026-08-22T02:00:00Z", "abandono", "ticket/9")])
            eventos = leer_eventos(p)
        self.assertEqual([e["tipo"] for e in eventos], ["pr", "abandono"])


class TestParseFecha(unittest.TestCase):
    def test_formatos(self):
        self.assertEqual(parse_fecha("2026-08-21"),
                         datetime(2026, 8, 21, tzinfo=timezone.utc))
        self.assertEqual(parse_fecha("2026-08-21T08:30:00Z"),
                         datetime(2026, 8, 21, 8, 30, tzinfo=timezone.utc))
        self.assertEqual(parse_fecha("2026-08-21T08:30:00+00:00"),
                         datetime(2026, 8, 21, 8, 30, tzinfo=timezone.utc))
        # Naive = UTC: así lo escribe el log.
        self.assertEqual(parse_fecha("2026-08-21T08:30:00"),
                         datetime(2026, 8, 21, 8, 30, tzinfo=timezone.utc))

    def test_inválido_es_none(self):
        for s in ("", "ayer", "31-08-2026", "2026-13-01"):
            self.assertIsNone(parse_fecha(s), s)


class TestResumir(unittest.TestCase):
    def test_log_vacio(self):
        tickets, trabados, costo = resumir([])
        self.assertEqual((tickets, trabados, costo), ([], [], 0.0))

    def test_un_solo_evento(self):
        """El caso mínimo: un solo evento de abandono cuenta como trabado y
        nada más."""
        eventos = [json.loads(evento("2026-08-22T02:00:00Z", "abandono",
                                     "ticket/11",
                                     cuerpo="agente bloqueado (aprobacion o "
                                            "pregunta pendiente)"))]
        tickets, trabados, costo = resumir(eventos)
        self.assertEqual(tickets, [])
        self.assertEqual(costo, 0.0)
        self.assertEqual(trabados[0].ref, "ticket/11")
        self.assertIn("bloqueado", trabados[0].motivo)

    def test_log_normal(self):
        eventos = [json.loads(l) for l in LOG_NORMAL]
        tickets, trabados, costo = resumir(eventos)
        self.assertEqual([t.ref for t in tickets], ["ticket/3", "ticket/9"])
        self.assertIn("PR #19", tickets[0].detalle)
        self.assertEqual([t.ref for t in trabados], ["ticket/11"])
        # Suma de los costos por job; el delta de créditos de la corrida no
        # entra (mediría el mismo gasto dos veces).
        self.assertAlmostEqual(costo, 0.0560 + 0.2840 + 0.4300)

    def test_extrae_repo_y_numero_del_pr(self):
        """El repo sale de `ticket` ("repo#issue"), el número del cuerpo:
        son la clave que necesita `reconciliar` para consultar en vivo."""
        eventos = [json.loads(l) for l in LOG_NORMAL]
        tickets, _, _ = resumir(eventos)
        self.assertEqual([(t.repo, t.numero) for t in tickets],
                         [("agent-harness", 19), ("agent-harness", 20)])
        self.assertEqual([t.estado for t in tickets], ["abierto", "abierto"])
        self.assertFalse(any(t.en_vivo for t in tickets))

    def test_pr_sin_ticket_ni_numero_queda_sin_repo(self):
        eventos = [json.loads(evento("2026-08-22T01:00:00Z", "pr", "ticket/3",
                                     cuerpo="PR abierto"))]
        tickets, _, _ = resumir(eventos)
        self.assertIsNone(tickets[0].repo)
        self.assertIsNone(tickets[0].numero)

    def test_eventos_sin_tipo_no_cuentan(self):
        eventos = [json.loads(evento("2026-08-22T01:00:00Z", "gate",
                                     "ticket/3", cuerpo="verde"))]
        self.assertEqual(resumir(eventos), ([], [], 0.0))


class TestReconciliar(unittest.TestCase):
    """`reconciliar` corrige el estado de cada ticket contra GitHub: el
    merge es humano y el log nunca se entera solo."""

    def _ticket(self, repo="agent-harness", numero=19):
        return Ticket(contexto="personal", ref="ticket/3",
                     detalle="PR #19 abierto (gate verde)", repo=repo, numero=numero)

    def test_mergeado(self):
        out = reconciliar([self._ticket()], lambda repo, num: "MERGED")
        self.assertEqual(out[0].estado, "mergeado")
        self.assertTrue(out[0].en_vivo)

    def test_cerrado_sin_mergear_es_distinto_de_mergeado(self):
        out = reconciliar([self._ticket()], lambda repo, num: "CLOSED")
        self.assertEqual(out[0].estado, "cerrado")
        self.assertTrue(out[0].en_vivo)

    def test_sigue_abierto_pero_confirmado_en_vivo(self):
        out = reconciliar([self._ticket()], lambda repo, num: "OPEN")
        self.assertEqual(out[0].estado, "abierto")
        self.assertTrue(out[0].en_vivo)

    def test_resolver_sin_respuesta_cae_al_log(self):
        out = reconciliar([self._ticket()], lambda repo, num: None)
        self.assertEqual(out[0].estado, "abierto")
        self.assertFalse(out[0].en_vivo)

    def test_sin_repo_o_numero_no_llama_al_resolver(self):
        llamado = []
        resolver = lambda repo, num: llamado.append((repo, num)) or "MERGED"
        out = reconciliar([self._ticket(repo=None), self._ticket(numero=None)], resolver)
        self.assertEqual(llamado, [])
        self.assertFalse(out[0].en_vivo)
        self.assertFalse(out[1].en_vivo)


class TestFiltrar(unittest.TestCase):
    def test_desde_none_es_todo(self):
        eventos = [json.loads(l) for l in LOG_NORMAL]
        self.assertEqual(filtrar(eventos, None), eventos)

    def test_desde_corta_lo_anterior(self):
        eventos = [json.loads(l) for l in LOG_NORMAL]
        desde = parse_fecha("2026-08-22T04:00:00Z")
        self.assertEqual([e["tipo"] for e in filtrar(eventos, desde)],
                         ["pr", "costo", "costo", "issue-cerrado", "corrida"])

    def test_desde_incluye_el_momento_exacto(self):
        eventos = [json.loads(evento("2026-08-22T01:00:00Z", "pr", "ticket/3"))]
        self.assertEqual(len(filtrar(eventos, parse_fecha("2026-08-22T01:00:00Z"))), 1)


class TestConstruir(unittest.TestCase):
    def test_vacio_no_paso_nada(self):
        r = construir([], [])
        self.assertFalse(r.paso_algo)

    def test_normal(self):
        prs = [PrAbierto(repo="agent-harness", number=19, title="Fix"),
               PrAbierto(repo="koku", number=22, title="Migrar el schema")]
        r = construir([json.loads(l) for l in LOG_NORMAL], prs,
                      desde="2026-08-21T23:00:00Z", hasta="2026-08-22T06:00:00Z")
        self.assertTrue(r.paso_algo)
        self.assertEqual(len(r.prs), 2)
        self.assertAlmostEqual(r.costo, 0.77)

    def test_sin_resolver_pr_no_reconcilia(self):
        """Sin `resolver_pr` (el default), los tickets quedan como los deja
        el log: abierto, sin confirmar en vivo."""
        r = construir([json.loads(l) for l in LOG_NORMAL], [])
        self.assertTrue(all(t.estado == "abierto" and not t.en_vivo
                            for t in r.tickets))

    def test_resolver_pr_reconcilia_cada_ticket(self):
        """`ticket/3` (PR #19) aparece mergeado en vivo; `ticket/9` (PR #20)
        sigue abierto: son estados que hay que poder distinguir."""
        estados = {19: "MERGED", 20: "OPEN"}
        resolver = lambda repo, numero: estados.get(numero)
        r = construir([json.loads(l) for l in LOG_NORMAL], [], resolver_pr=resolver)
        por_numero = {t.numero: t for t in r.tickets}
        self.assertEqual(por_numero[19].estado, "mergeado")
        self.assertTrue(por_numero[19].en_vivo)
        self.assertEqual(por_numero[20].estado, "abierto")
        self.assertTrue(por_numero[20].en_vivo)


class TestConCuota(unittest.TestCase):
    """`con_cuota` (#47): cruza el resumen del período con la cuota
    (harness.quota) y los peldaños de cada ticket (harness.state) — datos
    que `construir` no puede calcular solo, así que entran por parámetro."""

    def _resumen(self):
        return Resumen(estado="ok", desde="2026-08-21T23:00:00Z", tickets=[
            Ticket(contexto="personal", ref="ticket/3",
                  detalle="PR #19 abierto (gate verde)",
                  repo="agent-harness", numero=19),
            Ticket(contexto="personal", ref="ticket/9",
                  detalle="PR #20 abierto (gate verde)",
                  repo="agent-harness", numero=20),
        ])

    def test_semana_de_cuota(self):
        r = con_cuota(self._resumen(), cuota_semana=1000.0,
                      cuota_semana_harness=600.0)
        self.assertEqual(r.cuota_semana, 1000.0)
        self.assertEqual(r.cuota_semana_harness, 600.0)

    def test_sin_cuota_queda_en_none(self):
        r = con_cuota(self._resumen())
        self.assertIsNone(r.cuota_semana)
        self.assertIsNone(r.cuota_semana_harness)

    def test_costo_y_cuota_por_ticket(self):
        """La clave es (repo, ref), no (repo, numero): `numero` es el PR
        (19, 20 acá), y lo que hace falta para cruzar contra el historial y
        la cuota es el issue, que sólo `ref` ("ticket/3") trae."""
        r = con_cuota(self._resumen(),
                      costos_por_ticket={("agent-harness", "ticket/3"): 0.42},
                      cuotas_por_ticket={("agent-harness", "ticket/3"): 12345.0})
        por_ref = {t.ref: t for t in r.tickets}
        self.assertEqual(por_ref["ticket/3"].costo, 0.42)
        self.assertEqual(por_ref["ticket/3"].cuota, 12345.0)
        # El otro ticket, sin entrada en los dicts, se queda con lo que traía.
        self.assertEqual(por_ref["ticket/9"].costo, 0.0)
        self.assertEqual(por_ref["ticket/9"].cuota, 0.0)

    def test_peldanos_por_ticket(self):
        peldanos = [{"attempt": 1, "runner": "pi", "motivo": "gate rojo",
                    "costo": 0.05, "cuota": None},
                   {"attempt": 2, "runner": "claude", "motivo": None,
                    "costo": 0.0, "cuota": 8200.0}]
        r = con_cuota(self._resumen(),
                      peldanos_por_ticket={("agent-harness", "ticket/3"): peldanos})
        por_ref = {t.ref: t for t in r.tickets}
        self.assertEqual(por_ref["ticket/3"].peldanos, peldanos)
        self.assertEqual(por_ref["ticket/9"].peldanos, [])

    def test_no_toca_el_original(self):
        """`con_cuota` devuelve un `Resumen` nuevo: el que se le pasó no
        se muta (mismo criterio que el resto del módulo, ver `reconciliar`)."""
        original = self._resumen()
        con_cuota(original, cuota_semana=1000.0,
                 costos_por_ticket={("agent-harness", "ticket/3"): 0.42})
        self.assertIsNone(original.cuota_semana)
        self.assertEqual(original.tickets[0].costo, 0.0)

    def test_ticket_sin_entrada_no_matchea(self):
        r = Resumen(estado="ok", tickets=[
            Ticket(contexto="personal", ref="ticket/3", detalle="PR abierto")])
        out = con_cuota(r, costos_por_ticket={("otro-repo", "ticket/9"): 99.0})
        self.assertEqual(out.tickets[0].costo, 0.0)


class TestMarca(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "harness" / "last_viewed"
            guardar_marca(p, lambda: "2026-08-22T08:00:00Z")
            self.assertEqual(leer_marca(p),
                             datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc))

    def test_marca_inexistente_es_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(leer_marca(Path(tmp) / "last_viewed"))


CONFIG_SIN_REPOS = json.dumps({
    "default_context": "personal",
    "contexts": {
        "personal": {"tracker": {"kind": "github"},
                     "repos": {"root": "/no/existe", "paths": []},
                     "autonomy": "frontier",
                     "budget": {"polarity": "remaining", "provider": "openrouter"},
                     "run": {"kind": "local"}},
    },
})


def correr_cli(state, *args):
    """El CLI de verdad, no offline, sin red: HOME y estado en un tmpdir y
    cero repos en la config. `state` es la XDG_STATE_HOME (dónde viven
    events.jsonl y last_viewed)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("HARNESS_OFFLINE", "HERDR_ENV", "XDG_STATE_HOME")}
    with tempfile.TemporaryDirectory() as cfg:
        env.update(HOME=state, XDG_STATE_HOME=state, HARNESS_CONFIG_DIR=cfg)
        (Path(cfg) / "config.json").write_text(CONFIG_SIN_REPOS)
        return subprocess.run([str(HARNESS), *args], env=env, capture_output=True,
                              text=True, timeout=60)


def evento_run(timestamp, tipo, ref, cuerpo, run_id, ticket=None, attempt=1):
    """Una línea del esquema nuevo (con `run_id`/`attempt`): hace falta para
    fabricar una escalada de verdad —dos peldaños, dos `run_id`— en los
    tests de integración de cuota (#47)."""
    return json.dumps({"timestamp": timestamp, "contexto": "personal",
                       "origen": "harness", "run_id": run_id, "ticket": ticket,
                       "attempt": attempt, "tipo": tipo, "ref": ref,
                       "cuerpo": cuerpo, "clase": None}, ensure_ascii=False)


CONFIG_CUOTA = json.dumps({
    "default_context": "personal",
    "contexts": {
        "personal": {"tracker": {"kind": "github"},
                     "repos": {"root": "/u/docs", "paths": []},
                     "autonomy": "frontier",
                     "budget": {"polarity": "remaining", "provider": "none"},
                     "run": {"kind": "local"}},
    },
})

# El ticket 256 de f7league escaló (#38): un primer intento con pi que
# murió (gate rojo, $0.05) y uno con claude que cerró sin costo en
# dólares -gastó cuota, no OpenRouter. Las sesiones de Claude de ese
# ticket son las de tests/fixtures/claude_sessions (mismo fixture que
# tests/test_quota.py): 3 mensajes de opus-5, 16.780 tokens ponderados.
LOG_ESCALADA = [
    evento_run("2026-08-25T09:00:00Z", "worktree", "ticket/256",
              "/u/docs/.worktrees/f7league-ticket-256", run_id="r1",
              ticket="f7league#256", attempt=1),
    evento_run("2026-08-25T09:01:00Z", "agente", "ticket/256",
              "f7league-256 (kind pi, pane w1:p1)", run_id="r1",
              ticket="f7league#256", attempt=1),
    evento_run("2026-08-25T09:05:00Z", "abandono", "ticket/256",
              "gate rojo en el worktree: sin PR", run_id="r1",
              ticket="f7league#256", attempt=1),
    evento_run("2026-08-25T09:06:00Z", "costo", "ticket/256", "$0.05",
              run_id="r1", ticket="f7league#256", attempt=1),
    evento_run("2026-08-25T09:30:00Z", "worktree", "ticket/256",
              "/u/docs/.worktrees/f7league-ticket-256", run_id="r2",
              ticket="f7league#256", attempt=2),
    evento_run("2026-08-25T09:31:00Z", "agente", "ticket/256",
              "f7league-256 (kind claude, pane w1:p2)", run_id="r2",
              ticket="f7league#256", attempt=2),
    evento_run("2026-08-28T22:30:00Z", "pr", "ticket/256",
              "PR #99 abierto (gate verde)", run_id="r2",
              ticket="f7league#256", attempt=2),
]


def _instalar_sesiones(home):
    """Copia el fixture de sesiones de Claude (tests/fixtures/claude_sessions)
    a `<home>/.claude/projects`, para que `harness status`/`report` (que
    leen `~/.claude/projects` de verdad) las encuentren."""
    shutil.copytree(support.FIXTURES / "claude_sessions",
                    Path(home) / ".claude" / "projects")


class TestCuotaEnStatusYReport(unittest.TestCase):
    """El cableado de la cuota (#47) en `harness status` y `harness
    report`: la semana, el costo por ticket en las dos monedas, y los
    peldaños de un ticket escalado. Todo pasa por `bin/harness`, así que
    esto es lo único que ejercita esa parte del cableado — el resto está
    en test_quota.py, test_state.py y acá arriba (`TestConCuota`)."""

    def _estado(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(d), True)
        return d

    def _correr(self, *args):
        state = self._estado()
        _instalar_sesiones(state)
        escribir_log(state / "harness" / "events.jsonl", LOG_ESCALADA)
        env = {k: v for k, v in os.environ.items()
               if k not in ("HARNESS_OFFLINE", "HERDR_ENV", "XDG_STATE_HOME")}
        with tempfile.TemporaryDirectory() as cfg:
            env.update(HOME=str(state), XDG_STATE_HOME=str(state), HARNESS_CONFIG_DIR=cfg)
            (Path(cfg) / "config.json").write_text(CONFIG_CUOTA)
            return subprocess.run([str(HARNESS), *args], env=env,
                                  capture_output=True, text=True, timeout=120)

    def test_status_muestra_costo_por_ticket_en_las_dos_monedas(self):
        p = self._correr("status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("cuota de la semana", p.stdout)
        self.assertIn("$0.05", p.stdout)
        self.assertIn("16,780", p.stdout)

    def test_status_muestra_los_dos_peldanos(self):
        p = self._correr("status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("peldaño 1 (pi)", p.stdout)
        self.assertIn("peldaño 2 (claude)", p.stdout)

    def test_json_trae_la_cuota_del_ticket(self):
        p = self._correr("status", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        t = data["resumen"]["tickets"][0]
        self.assertAlmostEqual(t["costo"], 0.05)
        self.assertAlmostEqual(t["cuota"], 16780.0)
        self.assertEqual(len(t["peldanos"]), 2)
        self.assertIsNotNone(data["resumen"]["cuota_semana"])

    def test_report_muestra_la_cuota_y_la_evolucion_por_noche(self):
        p = self._correr("report")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Cuota de la semana", p.stdout)
        self.assertIn("16,780 tok", p.stdout)
        self.assertIn("2026-08-25", p.stdout)  # evolución por noche
        self.assertIn("peldaño 1", p.stdout)
        self.assertIn("peldaño 2", p.stdout)

    def test_offline_no_calcula_cuota(self):
        """Sin adaptadores no hay de dónde leer sesiones: la pantalla no
        se rompe, simplemente no muestra la cuota (#47)."""
        state = self._estado()
        _instalar_sesiones(state)
        escribir_log(state / "harness" / "events.jsonl", LOG_ESCALADA)
        env = {k: v for k, v in os.environ.items()
               if k not in ("HERDR_ENV", "XDG_STATE_HOME")}
        with tempfile.TemporaryDirectory() as cfg:
            env.update(HOME=str(state), XDG_STATE_HOME=str(state),
                       HARNESS_CONFIG_DIR=cfg, HARNESS_OFFLINE="1")
            (Path(cfg) / "config.json").write_text(CONFIG_CUOTA)
            p = subprocess.run([str(HARNESS), "status"], env=env,
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("cuota de la semana", p.stdout)


class TestCLI(unittest.TestCase):
    """El cableado de `harness status` contra el log y la marca, de punta a punta."""

    def _estado(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(d), True)
        return d

    def test_primer_status_muestra_el_log_y_guarda_la_marca(self):
        state = self._estado()
        escribir_log(state / "harness" / "events.jsonl", LOG_NORMAL)
        p = correr_cli(state, "status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("desde el principio", p.stdout)
        self.assertIn("2 tickets con PR abierto (gate verde)", p.stdout)
        self.assertIn("1 trabado(s)", p.stdout)
        self.assertIn("gate rojo en el worktree: sin PR", p.stdout)
        self.assertIn("costo del período: $0.77", p.stdout)
        self.assertTrue((state / "harness" / "last_viewed").exists())

    def test_segundo_status_solo_cubre_lo_nuevo(self):
        """La marca se actualiza sola: lo que ya se vio no vuelve a contarse."""
        state = self._estado()
        (state / "harness").mkdir(parents=True)
        (state / "harness" / "last_viewed").write_text("2026-08-22T06:00:00Z")
        escribir_log(state / "harness" / "events.jsonl", LOG_NORMAL)
        p = correr_cli(state, "status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("no pasó nada", p.stdout)  # todo el log es anterior a la marca

    def test_since_mira_otro_periodo_sin_mover_la_marca(self):
        state = self._estado()
        (state / "harness").mkdir(parents=True)
        (state / "harness" / "last_viewed").write_text("2026-08-22T06:00:00Z")
        escribir_log(state / "harness" / "events.jsonl", LOG_NORMAL)
        p = correr_cli(state, "status", "--since", "2026-08-21")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("tickets con PR abierto", p.stdout)
        self.assertEqual((state / "harness" / "last_viewed").read_text(),
                         "2026-08-22T06:00:00Z")

    def test_since_inválido_es_error_sin_stack(self):
        state = self._estado()
        p = correr_cli(state, "status", "--since", "el martes")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("--since inválido", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_json_trae_el_resumen(self):
        state = self._estado()
        escribir_log(state / "harness" / "events.jsonl", LOG_NORMAL)
        p = correr_cli(state, "status", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        r = json.loads(p.stdout)["resumen"]
        self.assertEqual([t["ref"] for t in r["tickets"]], ["ticket/3", "ticket/9"])
        self.assertEqual([t["ref"] for t in r["trabados"]], ["ticket/11"])
        self.assertAlmostEqual(r["costo"], 0.77)

    def test_offline_no_toca_ni_log_ni_marca(self):
        """El gate corre status offline: no debe tener efectos de lado."""
        state = self._estado()
        escribir_log(state / "harness" / "events.jsonl", LOG_NORMAL)
        env = {k: v for k, v in os.environ.items()
               if k not in ("HERDR_ENV", "XDG_STATE_HOME")}
        with tempfile.TemporaryDirectory() as cfg:
            env.update(HOME=state, XDG_STATE_HOME=state, HARNESS_CONFIG_DIR=cfg,
                       HARNESS_OFFLINE="1")
            (Path(cfg) / "config.json").write_text(CONFIG_SIN_REPOS)
            p = subprocess.run([str(HARNESS), "status"], env=env, capture_output=True,
                               text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Resumen", p.stdout)
        self.assertNotIn("tickets con PR", p.stdout)
        self.assertFalse((state / "harness" / "last_viewed").exists())


if __name__ == "__main__":
    unittest.main()
