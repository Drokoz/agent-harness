"""Tests de `snapshot()`, que es donde vive toda la lógica riesgosa.

Entrada: lo crudo que trajeron los adaptadores (fixtures reales en tests/fixtures/).
Nada acá toca la red, el disco ni un subproceso, y hay un test que lo verifica.
"""

import copy
import unittest
from unittest import mock

import support

from harness.snapshot import (COSTO_TICKET, READINESS, Snapshot, blockers_of,
                              snapshot)


def raw(**over):
    base = {"offline": False, "credits": None, "agents": None, "repos": []}
    base.update(over)
    return base


def repo_raw(**over):
    base = {"name": "koku", "slug": "Drokoz/koku", "branch": "main",
            "status_porcelain": "", "exists": {k: True for k, _, _ in READINESS},
            "issues": [], "prs": []}
    base.update(over)
    return base


def issue(number, labels=("ready-for-agent",), body="", title="t"):
    return {"number": number, "title": title, "body": body,
            "labels": [{"name": l} for l in labels]}


class TestPureza(unittest.TestCase):
    def test_no_toca_el_mundo(self):
        """Ni red, ni disco, ni subprocesos: sólo el diccionario que recibe."""
        entrada = support.golden_raw()  # leer fixtures ANTES de tapar el mundo
        boom = AssertionError("snapshot() tocó el mundo")
        with mock.patch("subprocess.run", side_effect=boom), \
                mock.patch("subprocess.Popen", side_effect=boom), \
                mock.patch("urllib.request.urlopen", side_effect=boom), \
                mock.patch("socket.socket", side_effect=boom), \
                mock.patch("builtins.open", side_effect=boom), \
                mock.patch("os.system", side_effect=boom):
            snap = snapshot(entrada)
        self.assertIsInstance(snap, Snapshot)

    def test_no_muta_la_entrada_y_es_determinista(self):
        entrada = support.golden_raw()
        copia = copy.deepcopy(entrada)
        primera = snapshot(entrada)
        self.assertEqual(entrada, copia, "snapshot() mutó lo que recibió")
        self.assertEqual(primera, snapshot(entrada))


class TestFixtures(unittest.TestCase):
    """Las fixtures son salidas reales: si gh o herdr cambian de forma, esto cae."""

    def test_issues_reales(self):
        """La fixture REST no trae dependencias nativas: cae al parseo del body."""
        snap = snapshot(raw(repos=[repo_raw(issues=support.gh_issues())]))
        r = snap.repos[0]
        self.assertEqual(len(r.frontier), 10)
        self.assertIn(3, [i.number for i in r.frontier])
        self.assertEqual(r.frontier_source, "body")
        self.assertEqual(r.degraded, [])

    def test_issues_reales_con_dependencias_nativas(self):
        """La fixture GraphQL es la respuesta real de Drokoz/agent-harness: los
        cinco tickets con bloqueantes abiertos NO entran a la frontera."""
        snap = snapshot(raw(repos=[repo_raw(issues=support.gh_issues_native())]))
        r = snap.repos[0]
        self.assertEqual([i.number for i in r.frontier], [1, 4, 12, 15])
        self.assertEqual([i.number for i in r.blocked], [5, 6, 7, 8, 9])
        self.assertEqual(r.frontier_source, "native")
        self.assertEqual(r.degraded, [])

    def test_prs_reales(self):
        snap = snapshot(raw(repos=[repo_raw(prs=support.gh_prs())]))
        self.assertEqual([p.number for p in snap.repos[0].prs], [13, 10])
        self.assertFalse(snap.repos[0].prs[0].draft)

    def test_agentes_reales(self):
        snap = snapshot(raw(agents=support.herdr_agents()))
        self.assertEqual(snap.agents.state, "ok")
        self.assertEqual(len(snap.agents.items), 8)
        # working primero, después el resto, en el orden en que vinieron.
        self.assertEqual([a.status for a in snap.agents.items][:2], ["working", "working"])
        self.assertEqual(snap.agents.items[1].who, "t3")  # tiene nombre propio
        self.assertEqual(snap.agents.items[2].repo, "F7League")  # cae al cwd

    def test_credits_reales(self):
        snap = snapshot(raw(credits=support.openrouter_credits()))
        self.assertEqual(snap.budget.state, "ok")
        self.assertAlmostEqual(snap.budget.total, 25.0)


class TestFrontera(unittest.TestCase):
    def test_excluye_bloqueados(self):
        issues = [issue(1), issue(2, body="Blocked by #1")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).repos[0]
        self.assertEqual([i.number for i in r.frontier], [1])
        self.assertEqual([i.number for i in r.blocked], [2])

    def test_bloqueante_cerrado_no_bloquea(self):
        """#99 no está en la lista de abiertos: el issue vuelve a la frontera."""
        r = snapshot(raw(repos=[repo_raw(issues=[issue(2, body="Blocked by #99")])])).repos[0]
        self.assertEqual([i.number for i in r.frontier], [2])
        self.assertEqual(r.blocked, [])

    def test_varios_bloqueantes_basta_uno_abierto(self):
        issues = [issue(1), issue(5, body="Blocked by #1, #99")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).repos[0]
        self.assertEqual([i.number for i in r.blocked], [5])

    def test_sin_label_no_entra(self):
        issues = [issue(1, labels=()), issue(2, labels=("needs-triage",))]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).repos[0]
        self.assertEqual(r.frontier, [])
        self.assertEqual([i.number for i in r.triage], [2])

    def test_blockers_of(self):
        self.assertEqual(blockers_of("Blocked by #3, #4"), {3, 4})
        self.assertEqual(blockers_of("Blocked by: #12"), {12})
        self.assertEqual(blockers_of("Blocked by none (can start)"), set())
        self.assertEqual(blockers_of(None), set())


class TestFronteraNativa(unittest.TestCase):
    """La frontera sale de issueDependenciesSummary.blockedBy (bloqueantes
    abiertos, lo que ve la UI de GitHub), no del regex del body."""

    def issue(self, number, blocked_by, body=""):
        i = issue(number, body=body)
        i["blocked_by"] = blocked_by
        return i

    def test_bloqueantes_abiertos_no_entran_a_la_frontera(self):
        issues = [issue(1), self.issue(2, blocked_by=1)]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).repos[0]
        self.assertEqual([i.number for i in r.frontier], [1])
        self.assertEqual([i.number for i in r.blocked], [2])

    def test_cero_bloqueantes_abiertos_esta_en_la_frontera(self):
        # blockedBy ya no cuenta cerrados: 0 quiere decir disponible.
        r = snapshot(raw(repos=[repo_raw(issues=[self.issue(2, blocked_by=0)])])).repos[0]
        self.assertEqual([i.number for i in r.frontier], [2])
        self.assertEqual(r.blocked, [])

    def test_nativas_premen_al_body(self):
        """Con datos nativos el cuerpo no se mira: 'Blocked by #1' no cuenta."""
        issues = [issue(1), self.issue(2, blocked_by=0, body="Blocked by #1")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).repos[0]
        self.assertEqual([i.number for i in r.frontier], [1, 2])
        self.assertEqual(r.blocked, [])

    def test_sin_datos_nativos_cae_al_body(self):
        issues = [issue(1), issue(2, body="Blocked by #1")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).repos[0]
        self.assertEqual([i.number for i in r.frontier], [1])
        self.assertEqual([i.number for i in r.blocked], [2])

    def test_fuente_nativa(self):
        issues = [self.issue(1, blocked_by=0), self.issue(2, blocked_by=1)]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).repos[0]
        self.assertEqual(r.frontier_source, "native")

    def test_fuente_body(self):
        r = snapshot(raw(repos=[repo_raw(issues=[issue(1)])])).repos[0]
        self.assertEqual(r.frontier_source, "body")

    def test_mezclado_se_nota_body_y_cada_issue_usa_lo_suyo(self):
        """Si algún issue no trae nativas, la fuente se reporta como body y ese
        issue se decide por su cuerpo."""
        issues = [self.issue(1, blocked_by=0), issue(2, body="Blocked by #1")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).repos[0]
        self.assertEqual(r.frontier_source, "body")
        self.assertEqual([i.number for i in r.frontier], [1])
        self.assertEqual([i.number for i in r.blocked], [2])

    def test_fuente_none_sin_issues(self):
        r = snapshot(raw(repos=[repo_raw(issues=[])])).repos[0]
        self.assertIsNone(r.frontier_source)


class TestRepos(unittest.TestCase):
    def test_repo_sin_remote_no_rompe(self):
        """Sin slug no hay issues que traer, pero el repo sigue en la tabla."""
        r = snapshot(raw(repos=[repo_raw(slug=None, issues=None, prs=None,
                                         exists={"gate": True})])).repos[0]
        self.assertIsNone(r.slug)
        self.assertFalse(r.has_work)
        self.assertEqual(r.ready, {"gate": True, "skills": False, "context": False})
        self.assertEqual(len(r.missing), 2)
        self.assertEqual(r.degraded, [])

    def test_branch_y_dirty(self):
        r = snapshot(raw(repos=[repo_raw(branch=None, status_porcelain=" M a\n?? b\n\n")])).repos[0]
        self.assertEqual(r.branch, "?")
        self.assertEqual(r.dirty, 2)

    def test_dirty_cero_si_git_fallo(self):
        r = snapshot(raw(repos=[repo_raw(status_porcelain=None)])).repos[0]
        self.assertEqual(r.dirty, 0)

    def test_offline_no_inventa_trabajo(self):
        """Sin adaptadores no se pidieron issues: no es degradación, es que no se preguntó."""
        r = snapshot(raw(offline=True, repos=[repo_raw(issues=None, prs=None)])).repos[0]
        self.assertEqual(r.degraded, [])
        self.assertFalse(r.has_work)


class TestPresupuesto(unittest.TestCase):
    def test_calcula_bien(self):
        b = snapshot(raw(credits={"total_credits": 25.0, "total_usage": 11.634528})).budget
        self.assertEqual(b.state, "ok")
        self.assertAlmostEqual(b.left, 13.365472)
        self.assertEqual(b.tickets, int(13.365472 / COSTO_TICKET))
        self.assertEqual(b.tickets, 55)

    def test_gastado_todo(self):
        b = snapshot(raw(credits={"total_credits": 5.0, "total_usage": 5.0})).budget
        self.assertEqual(b.left, 0.0)
        self.assertEqual(b.tickets, 0)

    def test_sin_datos(self):
        self.assertEqual(snapshot(raw()).budget.state, "missing")

    def test_respuesta_rara_no_rompe(self):
        for malo in ({}, {"total_credits": 1.0}, {"total_credits": "x", "total_usage": 0}):
            with self.subTest(credits=malo):
                self.assertEqual(snapshot(raw(credits=malo)).budget.state, "missing")

    def test_offline(self):
        self.assertEqual(snapshot(raw(offline=True, credits={"total_credits": 1.0,
                                                             "total_usage": 0.0})).budget.state,
                         "offline")


class TestAgentes(unittest.TestCase):
    def test_estados(self):
        self.assertEqual(snapshot(raw(agents=None)).agents.state, "outside")
        self.assertEqual(snapshot(raw(agents=[])).agents.state, "empty")
        self.assertEqual(snapshot(raw(offline=True, agents=[{}])).agents.state, "offline")

    def test_bloqueados_primero(self):
        crudos = [{"agent_status": "idle"}, {"agent_status": "working"},
                  {"agent_status": "blocked"}]
        items = snapshot(raw(agents=crudos)).agents.items
        self.assertEqual([a.status for a in items], ["blocked", "working", "idle"])

    def test_campos_faltantes(self):
        a = snapshot(raw(agents=[{}])).agents.items[0]
        self.assertEqual((a.who, a.agent, a.status, a.repo, a.pane), ("—", "?", "?", "", ""))


class TestDegradacion(unittest.TestCase):
    """Un adaptador caído se lleva su sección, no la pantalla."""

    def test_gh_caido_en_un_repo(self):
        snap = snapshot(raw(credits=support.openrouter_credits(),
                            agents=support.herdr_agents(),
                            repos=[repo_raw(name="caido", issues=None, prs=None),
                                   repo_raw(name="sano", issues=[issue(1)],
                                            prs=support.gh_prs())]))
        caido, sano = snap.repos
        self.assertEqual(caido.degraded, ["issues", "prs"])
        self.assertEqual(caido.frontier, [])
        self.assertFalse(caido.has_work)
        self.assertEqual(sano.degraded, [])
        self.assertTrue(sano.has_work)
        self.assertEqual(snap.budget.state, "ok")
        self.assertEqual(snap.agents.state, "ok")

    def test_solo_los_issues_caidos(self):
        r = snapshot(raw(repos=[repo_raw(issues=None, prs=support.gh_prs())])).repos[0]
        self.assertEqual(r.degraded, ["issues"])
        self.assertTrue(r.has_work)  # los PRs siguen ahí

    def test_todo_caido_sigue_dando_snapshot(self):
        snap = snapshot(raw(repos=[repo_raw(issues=None, prs=None, status_porcelain=None,
                                            branch=None)]))
        self.assertEqual(snap.budget.state, "missing")
        self.assertEqual(snap.agents.state, "outside")
        self.assertEqual(len(snap.repos), 1)


if __name__ == "__main__":
    unittest.main()
