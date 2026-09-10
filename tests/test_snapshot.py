"""Tests de `snapshot()`, que es donde vive toda la lógica riesgosa.

Entrada: lo crudo que trajeron los adaptadores (fixtures reales en tests/fixtures/).
Nada acá toca la red, el disco ni un subproceso, y hay un test que lo verifica.
"""

import copy
import json
import unittest
from unittest import mock

import support

from harness.snapshot import (COSTO_TICKET, READINESS, SCHEMA_VERSION, Snapshot,
                              as_dict, blockers_of, snapshot)


def raw(offline=False, credits=None, agents=None, repos=None, budget=None, **over):
    """Lo crudo con un solo contexto, que es lo que mira casi todo este archivo."""
    contexto = {"name": "personal", "tracker": "github", "autonomy": "frontier",
                "vault": None, "run": "local", "repos": repos or [],
                "budget": budget if budget is not None else budget_raw(credits=credits)}
    contexto.update(over)
    return {"offline": offline, "agents": agents, "contexts": [contexto]}


def budget_raw(**over):
    base = {"polarity": "remaining", "provider": "openrouter", "credits": None,
            "total": 0.0, "used": 0.0}
    base.update(over)
    return base


def repo_raw(**over):
    base = {"name": "koku", "tracker": "github", "slug": "Drokoz/koku", "branch": "main",
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
        r = snap.contexts[0].repos[0]
        self.assertEqual(len(r.frontier), 10)
        self.assertIn(3, [i.number for i in r.frontier])
        self.assertEqual(r.frontier_source, "body")
        self.assertEqual(r.degraded, [])

    def test_issues_reales_con_dependencias_nativas(self):
        """La fixture GraphQL es la respuesta real de Drokoz/agent-harness: los
        cinco tickets con bloqueantes abiertos NO entran a la frontera."""
        snap = snapshot(raw(repos=[repo_raw(issues=support.gh_issues_native())]))
        r = snap.contexts[0].repos[0]
        self.assertEqual([i.number for i in r.frontier], [1, 4, 12, 15])
        self.assertEqual([i.number for i in r.blocked], [5, 6, 7, 8, 9])
        self.assertEqual(r.frontier_source, "native")
        self.assertEqual(r.degraded, [])

    def test_prs_reales(self):
        snap = snapshot(raw(repos=[repo_raw(prs=support.gh_prs())]))
        self.assertEqual([p.number for p in snap.contexts[0].repos[0].prs], [13, 10])
        self.assertFalse(snap.contexts[0].repos[0].prs[0].draft)

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
        self.assertEqual(snap.contexts[0].budget.state, "ok")
        self.assertAlmostEqual(snap.contexts[0].budget.total, 25.0)


class TestFrontera(unittest.TestCase):
    def test_excluye_bloqueados(self):
        issues = [issue(1), issue(2, body="Blocked by #1")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).contexts[0].repos[0]
        self.assertEqual([i.number for i in r.frontier], [1])
        self.assertEqual([i.number for i in r.blocked], [2])

    def test_bloqueante_cerrado_no_bloquea(self):
        """#99 no está en la lista de abiertos: el issue vuelve a la frontera."""
        r = snapshot(raw(repos=[repo_raw(issues=[issue(2, body="Blocked by #99")])])).contexts[0].repos[0]
        self.assertEqual([i.number for i in r.frontier], [2])
        self.assertEqual(r.blocked, [])

    def test_varios_bloqueantes_basta_uno_abierto(self):
        issues = [issue(1), issue(5, body="Blocked by #1, #99")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).contexts[0].repos[0]
        self.assertEqual([i.number for i in r.blocked], [5])

    def test_sin_label_no_entra(self):
        issues = [issue(1, labels=()), issue(2, labels=("needs-triage",))]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).contexts[0].repos[0]
        self.assertEqual(r.frontier, [])
        self.assertEqual([i.number for i in r.triage], [2])

    def test_blockers_of(self):
        self.assertEqual(blockers_of("Blocked by #3, #4"), {3, 4})
        self.assertEqual(blockers_of("Blocked by: #12"), {12})
        self.assertEqual(blockers_of("Blocked by none (can start)"), set())
        self.assertEqual(blockers_of(None), set())

    def test_la_frontera_lleva_el_body_del_issue(self):
        """#42: el dispatcher decide las skills del agente leyendo el body
        del ticket (`Skills: ...`), así que la frontera tiene que
        conservarlo, no sólo el número y el título."""
        issues = [issue(1, body="## Archivos probables\nSkills: research")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).contexts[0].repos[0]
        self.assertEqual([i.number for i in r.frontier], [1])
        self.assertEqual(
            r.frontier[0].body, "## Archivos probables\nSkills: research")
        # Sin body, vacío: no None.
        r = snapshot(raw(repos=[repo_raw(issues=[issue(2)])])).contexts[0].repos[0]
        self.assertEqual(r.frontier[0].body, "")


class TestFronteraNativa(unittest.TestCase):
    """La frontera sale de issueDependenciesSummary.blockedBy (bloqueantes
    abiertos, lo que ve la UI de GitHub), no del regex del body."""

    def issue(self, number, blocked_by, body=""):
        i = issue(number, body=body)
        i["blocked_by"] = blocked_by
        return i

    def test_bloqueantes_abiertos_no_entran_a_la_frontera(self):
        issues = [issue(1), self.issue(2, blocked_by=1)]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).contexts[0].repos[0]
        self.assertEqual([i.number for i in r.frontier], [1])
        self.assertEqual([i.number for i in r.blocked], [2])

    def test_cero_bloqueantes_abiertos_esta_en_la_frontera(self):
        # blockedBy ya no cuenta cerrados: 0 quiere decir disponible.
        r = snapshot(raw(repos=[repo_raw(issues=[self.issue(2, blocked_by=0)])])).contexts[0].repos[0]
        self.assertEqual([i.number for i in r.frontier], [2])
        self.assertEqual(r.blocked, [])

    def test_nativas_premen_al_body(self):
        """Con datos nativos el cuerpo no se mira: 'Blocked by #1' no cuenta."""
        issues = [issue(1), self.issue(2, blocked_by=0, body="Blocked by #1")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).contexts[0].repos[0]
        self.assertEqual([i.number for i in r.frontier], [1, 2])
        self.assertEqual(r.blocked, [])

    def test_sin_datos_nativos_cae_al_body(self):
        issues = [issue(1), issue(2, body="Blocked by #1")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).contexts[0].repos[0]
        self.assertEqual([i.number for i in r.frontier], [1])
        self.assertEqual([i.number for i in r.blocked], [2])

    def test_fuente_nativa(self):
        issues = [self.issue(1, blocked_by=0), self.issue(2, blocked_by=1)]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).contexts[0].repos[0]
        self.assertEqual(r.frontier_source, "native")

    def test_fuente_body(self):
        r = snapshot(raw(repos=[repo_raw(issues=[issue(1)])])).contexts[0].repos[0]
        self.assertEqual(r.frontier_source, "body")

    def test_mezclado_se_nota_body_y_cada_issue_usa_lo_suyo(self):
        """Si algún issue no trae nativas, la fuente se reporta como body y ese
        issue se decide por su cuerpo."""
        issues = [self.issue(1, blocked_by=0), issue(2, body="Blocked by #1")]
        r = snapshot(raw(repos=[repo_raw(issues=issues)])).contexts[0].repos[0]
        self.assertEqual(r.frontier_source, "body")
        self.assertEqual([i.number for i in r.frontier], [1])
        self.assertEqual([i.number for i in r.blocked], [2])

    def test_fuente_none_sin_issues(self):
        r = snapshot(raw(repos=[repo_raw(issues=[])])).contexts[0].repos[0]
        self.assertIsNone(r.frontier_source)


class TestEstadoFrontera(unittest.TestCase):
    """El ticket #43: cada issue de la frontera tiene estado (libre,
    despachado, pr-abierto, mergeado, parkeado), y sólo los libres se despachan."""

    def repo(self, issues, prs=None, merged=None, agentes=None, eventos=None):
        crudo = raw(agents=agentes, repos=[repo_raw(issues=issues, prs=prs or [])])
        r = crudo["contexts"][0]["repos"][0]
        if merged is not None:
            r["prs_merged"] = merged
        if eventos is not None:
            crudo["eventos"] = eventos
        return snapshot(crudo).contexts[0].repos[0]

    def ev(self, tipo, run_id="r1", cuerpo="koku-7 (kind pi, pane w9:p1)"):
        return {"timestamp": "2026-08-24T02:00:00Z", "contexto": "personal",
                "origen": "harness", "run_id": run_id, "ticket": "koku#7",
                "attempt": 1, "tipo": tipo, "ref": "ticket/7", "cuerpo": cuerpo}

    def prs(self, rama, num=21):
        return [{"number": num, "title": "t", "isDraft": False, "headRefName": rama}]

    def estados(self, r):
        return [(i.number, i.estado) for i in r.frontier]

    def test_libre_sin_pr_ni_log(self):
        self.assertEqual(self.estados(self.repo([issue(7)])), [(7, "libre")])

    def test_pr_abierto_sobre_su_rama(self):
        r = self.repo([issue(7)], prs=self.prs("ticket/7"))
        self.assertEqual(self.estados(r), [(7, "pr-abierto")])

    def test_pr_de_otra_rama_no_cuenta(self):
        r = self.repo([issue(7)], prs=self.prs("ticket/9"))
        self.assertEqual(self.estados(r), [(7, "libre")])

    def test_pr_merged_sobre_su_rama(self):
        r = self.repo([issue(7)], merged=[{"number": 5, "headRefName": "ticket/7"}])
        self.assertEqual(self.estados(r), [(7, "mergeado")])

    def test_despachado_con_agente_vivo(self):
        agente = {"cwd": "/x/.worktrees/koku-ticket-7", "agent_status": "working"}
        r = self.repo([issue(7)], agentes=[agente], eventos=[self.ev("agente")])
        self.assertEqual(self.estados(r), [(7, "despachado")])

    def test_corrida_muerta_vuelve_a_libre(self):
        """Despacho de una corrida anterior, sin PR y sin agente vivo: no colga."""
        r = self.repo([issue(7)], eventos=[self.ev("agente", run_id="r-anoche")])
        self.assertEqual(self.estados(r), [(7, "libre")])

    def test_parkeado_por_ready_for_human(self):
        r = self.repo([issue(7, labels=("ready-for-agent", "ready-for-human"))])
        self.assertEqual(self.estados(r), [(7, "parkeado")])

    def test_parkeado_gana_al_pr_abierto(self):
        r = self.repo([issue(7, labels=("ready-for-agent", "ready-for-human"))],
                      prs=self.prs("ticket/7"))
        self.assertEqual(self.estados(r), [(7, "parkeado")])

    def test_bloqueados_y_triage_no_llevan_estado(self):
        r = self.repo([issue(1), issue(2, body="Blocked by #1"),
                       issue(3, labels=("needs-triage",))])
        self.assertEqual(r.blocked[0].estado, None)
        self.assertEqual(r.triage[0].estado, None)

    def test_json_trae_el_estado(self):
        crudo = raw(repos=[repo_raw(issues=[issue(7)], prs=self.prs("ticket/7"))])
        data = as_dict(snapshot(crudo))
        self.assertEqual(data["contexts"][0]["repos"][0]["frontier"][0]["estado"],
                         "pr-abierto")
        self.assertEqual(data["version"], SCHEMA_VERSION)


class TestReadinessRamaPorDefecto(unittest.TestCase):
    """El caso del issue #22: el working tree está parado en una rama de
    feature y la tabla no debe mentir. La readiness se mira contra la rama
    por defecto (lo que trae `exists`), y el snapshot respeta de dónde salió."""

    def repo(self, **over):
        return snapshot(raw(repos=[repo_raw(**over)])).contexts[0].repos[0]

    def test_en_rama_de_feature_y_listo_en_la_por_defecto(self):
        """Repo en rama de feature sin los archivos en el working tree, pero
        con todo en la rama por defecto: cuenta como listo."""
        r = self.repo(branch="fix/pdp-dynamic-server-usage", default_branch="main",
                      readiness_source="default-branch")
        self.assertTrue(all(r.ready.values()))
        self.assertEqual(r.missing, [])
        self.assertEqual((r.default_branch, r.readiness_source), ("main", "default-branch"))

    def test_lo_que_falla_en_la_por_defecto_sigue_faltando(self):
        r = self.repo(default_branch="main", readiness_source="default-branch",
                      exists={"gate": False, "skills": True, "context": True})
        self.assertFalse(r.ready["gate"])
        self.assertEqual(r.missing, [READINESS[0][2]])

    def test_fallback_al_working_tree_se_mantiene(self):
        r = self.repo(default_branch="main", readiness_source="working-tree",
                      exists={"gate": True, "skills": False, "context": False})
        self.assertEqual((r.default_branch, r.readiness_source), ("main", "working-tree"))
        self.assertEqual(r.ready, {"gate": True, "skills": False, "context": False})

    def test_crudo_sin_las_claves_nuevas_funciona(self):
        """Atrás: un crudo viejo sin `default_branch`/`readiness_source".
        no rompe y se lee como working tree, que es cómo se hacía antes."""
        r = self.repo()
        self.assertIsNone(r.default_branch)
        self.assertEqual(r.readiness_source, "working-tree")

    def test_json_trae_las_claves_nuevas(self):
        data = as_dict(snapshot(raw(repos=[repo_raw(default_branch="main",
                                                  readiness_source="default-branch")])))
        repo = data["contexts"][0]["repos"][0]
        self.assertEqual(repo["default_branch"], "main")
        self.assertEqual(repo["readiness_source"], "default-branch")
        self.assertEqual(data["version"], SCHEMA_VERSION)


class TestRepos(unittest.TestCase):
    def test_repo_sin_remote_no_rompe(self):
        """Sin slug no hay issues que traer, pero el repo sigue en la tabla."""
        r = snapshot(raw(repos=[repo_raw(slug=None, issues=None, prs=None,
                                         exists={"gate": True})])).contexts[0].repos[0]
        self.assertIsNone(r.slug)
        self.assertFalse(r.has_work)
        self.assertEqual(r.ready, {"gate": True, "skills": False, "context": False})
        self.assertEqual(len(r.missing), 2)
        self.assertEqual(r.degraded, [])

    def test_branch_y_dirty(self):
        r = snapshot(raw(repos=[repo_raw(branch=None, status_porcelain=" M a\n?? b\n\n")])).contexts[0].repos[0]
        self.assertEqual(r.branch, "?")
        self.assertEqual(r.dirty, 2)

    def test_dirty_cero_si_git_fallo(self):
        r = snapshot(raw(repos=[repo_raw(status_porcelain=None)])).contexts[0].repos[0]
        self.assertEqual(r.dirty, 0)

    def test_offline_no_inventa_trabajo(self):
        """Sin adaptadores no se pidieron issues: no es degradación, es que no se preguntó."""
        r = snapshot(raw(offline=True, repos=[repo_raw(issues=None, prs=None)])).contexts[0].repos[0]
        self.assertEqual(r.degraded, [])
        self.assertFalse(r.has_work)


class TestPresupuesto(unittest.TestCase):
    def test_calcula_bien(self):
        b = snapshot(raw(credits={"total_credits": 25.0, "total_usage": 11.634528})).contexts[0].budget
        self.assertEqual(b.state, "ok")
        self.assertAlmostEqual(b.left, 13.365472)
        self.assertEqual(b.tickets, int(13.365472 / COSTO_TICKET))
        self.assertEqual(b.tickets, 55)

    def test_gastado_todo(self):
        b = snapshot(raw(credits={"total_credits": 5.0, "total_usage": 5.0})).contexts[0].budget
        self.assertEqual(b.left, 0.0)
        self.assertEqual(b.tickets, 0)

    def test_sin_datos(self):
        self.assertEqual(snapshot(raw()).contexts[0].budget.state, "missing")

    def test_respuesta_rara_no_rompe(self):
        for malo in ({}, {"total_credits": 1.0}, {"total_credits": "x", "total_usage": 0}):
            with self.subTest(credits=malo):
                self.assertEqual(snapshot(raw(credits=malo)).contexts[0].budget.state, "missing")

    def test_offline(self):
        self.assertEqual(snapshot(raw(offline=True, credits={"total_credits": 1.0,
                                                             "total_usage": 0.0})).contexts[0].budget.state,
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
        caido, sano = snap.contexts[0].repos
        self.assertEqual(caido.degraded, ["issues", "prs"])
        self.assertEqual(caido.frontier, [])
        self.assertFalse(caido.has_work)
        self.assertEqual(sano.degraded, [])
        self.assertTrue(sano.has_work)
        self.assertEqual(snap.contexts[0].budget.state, "ok")
        self.assertEqual(snap.agents.state, "ok")

    def test_solo_los_issues_caidos(self):
        r = snapshot(raw(repos=[repo_raw(issues=None, prs=support.gh_prs())])).contexts[0].repos[0]
        self.assertEqual(r.degraded, ["issues"])
        self.assertTrue(r.has_work)  # los PRs siguen ahí

    def test_todo_caido_sigue_dando_snapshot(self):
        snap = snapshot(raw(repos=[repo_raw(issues=None, prs=None, status_porcelain=None,
                                            branch=None)]))
        self.assertEqual(snap.contexts[0].budget.state, "missing")
        self.assertEqual(snap.agents.state, "outside")
        self.assertEqual(len(snap.contexts[0].repos), 1)


class TestContextos(unittest.TestCase):
    def test_un_snapshot_por_contexto(self):
        snap = snapshot(support.golden_raw())
        self.assertEqual([c.name for c in snap.contexts], ["personal", "trabajo"])
        personal, trabajo = snap.contexts
        self.assertEqual((personal.tracker, personal.autonomy, personal.run),
                         ("github", "frontier", "local"))
        self.assertEqual((trabajo.tracker, trabajo.autonomy, trabajo.run),
                         ("jira", "manual", "ssh"))
        self.assertEqual(trabajo.vault, "~/wl-devlead-vault")

    def test_los_agentes_son_de_la_maquina_no_del_contexto(self):
        snap = snapshot(support.golden_raw())
        self.assertEqual(snap.agents.state, "ok")
        self.assertFalse(hasattr(snap.contexts[0], "agents"))

    def test_un_solo_contexto(self):
        snap = snapshot(support.golden_raw("trabajo"))
        self.assertEqual([c.name for c in snap.contexts], ["trabajo"])

    def test_sin_contextos_no_rompe(self):
        snap = snapshot({"offline": False, "agents": None, "contexts": []})
        self.assertEqual(snap.contexts, [])

    def test_tracker_que_no_es_github_no_degrada(self):
        """No es que gh se cayó: es que a Jira no se le pregunta con gh (todavía)."""
        r = snapshot(raw(repos=[repo_raw(tracker="jira", issues=None, prs=None)],
                         tracker="jira")).contexts[0].repos[0]
        self.assertEqual(r.degraded, [])
        self.assertFalse(r.has_work)
        self.assertEqual(r.ready, {k: True for k, _, _ in READINESS})


class TestPolaridad(unittest.TestCase):
    """El mismo número, dos objetivos opuestos: que no se acabe / que no sobre."""

    def test_personal_mira_lo_que_queda(self):
        b = snapshot(raw(credits={"total_credits": 25.0, "total_usage": 10.0})) \
            .contexts[0].budget
        self.assertEqual((b.polarity, b.provider), ("remaining", "openrouter"))
        self.assertAlmostEqual(b.left, 15.0)
        self.assertAlmostEqual(b.ratio, 0.4)

    def test_trabajo_mira_lo_que_lleva_usado(self):
        b = snapshot(raw(budget=budget_raw(polarity="spent", provider="manual",
                                           total=200.0, used=128.4))).contexts[0].budget
        self.assertEqual((b.state, b.polarity), ("ok", "spent"))
        self.assertAlmostEqual(b.used, 128.4)
        self.assertAlmostEqual(b.ratio, 0.642)

    def test_la_polaridad_sobrevive_al_presupuesto_caido(self):
        for over in ({"provider": "openrouter", "credits": None},
                     {"provider": "manual", "total": None}):
            with self.subTest(**over):
                b = snapshot(raw(budget=budget_raw(polarity="spent", **over))) \
                    .contexts[0].budget
                self.assertEqual(b.state, "missing")
                self.assertEqual(b.polarity, "spent")

    def test_offline_conserva_la_polaridad(self):
        b = snapshot(raw(offline=True, budget=budget_raw(polarity="spent"))) \
            .contexts[0].budget
        self.assertEqual((b.state, b.polarity), ("offline", "spent"))

    def test_contexto_sin_presupuesto(self):
        b = snapshot(raw(budget=budget_raw(provider="none"))).contexts[0].budget
        self.assertEqual(b.state, "unset")

    def test_total_cero_no_divide_por_cero(self):
        b = snapshot(raw(budget=budget_raw(provider="manual", total=0.0,
                                           used=0.0))).contexts[0].budget
        self.assertEqual((b.state, b.ratio), ("ok", 0.0))


class TestJson(unittest.TestCase):
    """`--json` emite el snapshot entero: es el contrato con la web futura."""

    def test_es_serializable_y_completo(self):
        data = as_dict(snapshot(support.golden_raw()))
        texto = json.dumps(data)  # sin default=: nada raro adentro
        self.assertEqual(data["version"], SCHEMA_VERSION)
        self.assertEqual([c["name"] for c in data["contexts"]], ["personal", "trabajo"])
        self.assertIn("frontier", texto)
        self.assertIn("readiness_source", texto)
        self.assertEqual(data["contexts"][1]["budget"]["polarity"], "spent")
        self.assertEqual(data["agents"]["state"], "ok")

    def test_no_recorta_como_recorta_la_pantalla(self):
        """La pantalla corta la lista de bloqueados a 3; el JSON los lleva todos."""
        data = as_dict(snapshot(support.golden_raw("personal")))
        repo = data["contexts"][0]["repos"][0]
        self.assertEqual([i["number"] for i in repo["blocked"]], [5, 6, 7, 8, 9])
        self.assertEqual(repo["frontier_source"], "native")

    def test_ida_y_vuelta_por_json(self):
        data = as_dict(snapshot(support.golden_raw()))
        self.assertEqual(json.loads(json.dumps(data)), data)


if __name__ == "__main__":
    unittest.main()
