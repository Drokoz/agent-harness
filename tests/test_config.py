"""Tests de `harness.config`: qué es un contexto válido y qué error da uno que no.

Nada acá toca el disco: `parse_config` recibe el diccionario ya leído. Lo que sí
lee archivos es `harness.adapters.load_config`, y se testea en test_adapters.py.
"""

import copy
import unittest

import support

from harness.config import (DEFAULT_CONFIG, Config, ConfigError, RunTopes,
                            default_config, parse_config)


def ctx(**over):
    base = {
        "tracker": {"kind": "github"},
        "repos": {"root": "~/Documents/Github", "paths": ["koku"]},
        "autonomy": "frontier",
        "budget": {"polarity": "remaining", "provider": "openrouter"},
        "vault": None,
        "run": {"kind": "local"},
    }
    base.update(over)
    return base


def conf(**contexts):
    return {"contexts": contexts or {"personal": ctx()}}


class TestValido(unittest.TestCase):
    def test_un_contexto_declara_todo(self):
        c = parse_config(conf(personal=ctx(vault="~/vault")))
        p = c.contexts[0]
        self.assertEqual(p.name, "personal")
        self.assertEqual(p.tracker.kind, "github")
        self.assertEqual(p.repos.paths, ["koku"])
        self.assertEqual(p.autonomy, "frontier")
        self.assertEqual(p.budget.polarity, "remaining")
        self.assertEqual(p.vault, "~/vault")
        self.assertEqual(p.run.kind, "local")

    def test_contexto_de_trabajo(self):
        trabajo = ctx(
            tracker={"kind": "jira", "url": "https://x.atlassian.net", "project": "GRO",
                     "token": "secreto"},
            repos={"root": "~/work", "paths": []},
            autonomy="manual",
            budget={"polarity": "spent", "provider": "manual", "total": 200.0, "used": 120.0},
            run={"kind": "ssh", "host": "tomas@localhost"},
        )
        t = parse_config(conf(trabajo=trabajo)).contexts[0]
        self.assertEqual(t.tracker.project, "GRO")
        self.assertEqual(t.autonomy, "manual")
        self.assertEqual((t.budget.polarity, t.budget.total, t.budget.used),
                         ("spent", 200.0, 120.0))
        self.assertEqual(t.run.host, "tomas@localhost")

    def test_no_muta_lo_que_recibe(self):
        data = conf(personal=ctx())
        copia = copy.deepcopy(data)
        parse_config(data)
        self.assertEqual(data, copia)

    def test_vault_es_opcional(self):
        self.assertIsNone(parse_config(conf(personal=ctx())).contexts[0].vault)


class TestQuota(unittest.TestCase):
    """`quota` es opcional (#74): la constante de calibración de `harness
    quota`, medida contra `/usage`, vive en config y no en el código."""

    def test_sin_quota_no_hay_tope(self):
        c = parse_config(conf(personal=ctx())).contexts[0]
        self.assertIsNone(c.quota.tope_semanal)

    def test_quota_vacia_es_valida(self):
        c = parse_config(conf(personal=ctx(quota={}))).contexts[0]
        self.assertIsNone(c.quota.tope_semanal)

    def test_tope_semanal(self):
        c = parse_config(
            conf(personal=ctx(quota={"tope_semanal": 900000000}))).contexts[0]
        self.assertEqual(c.quota.tope_semanal, 900000000.0)

    def test_clave_desconocida_dentro_de_quota(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"tope": 10})))

    def test_tope_no_numerico(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"tope_semanal": "900M"})))

    def test_tope_no_positivo(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"tope_semanal": 0})))

    def test_projects_opcional(self):
        """`quota.projects` (#75): sin declarar, la quota mira un solo usuario."""
        c = parse_config(conf(personal=ctx())).contexts[0]
        self.assertIsNone(c.quota.projects)

    def test_projects_lista(self):
        c = parse_config(conf(personal=ctx(quota={"projects": [
            "~/.claude/projects", "/Users/tomasherceg/.claude/projects"]}))).contexts[0]
        self.assertEqual(c.quota.projects,
                         ["~/.claude/projects",
                          "/Users/tomasherceg/.claude/projects"])

    def test_projects_y_tope_juntos(self):
        c = parse_config(conf(personal=ctx(quota={
            "tope_semanal": 900000000, "projects": ["~/.claude/projects"]}))).contexts[0]
        self.assertEqual(c.quota.tope_semanal, 900000000.0)
        self.assertEqual(c.quota.projects, ["~/.claude/projects"])

    def test_projects_no_lista(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"projects": "~/.claude/projects"})))

    def test_projects_vacia(self):
        """Una lista vacía no es "una fuente": es un error, no un default."""
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"projects": []})))

    def test_projects_elemento_no_texto(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"projects": [1]})))

    def test_remotas_opcional(self):
        """`quota.remotas` (#144): sin declarar, la cuota no mira otro host."""
        c = parse_config(conf(personal=ctx())).contexts[0]
        self.assertIsNone(c.quota.remotas)

    def test_remotas_lista(self):
        c = parse_config(conf(personal=ctx(quota={"remotas": [
            "tomasherceg@localhost"]}))).contexts[0]
        self.assertEqual(c.quota.remotas, ["tomasherceg@localhost"])

    def test_remotas_objeto_ssh(self):
        """La forma del ticket #144: `{"ssh": "user@host"}`."""
        c = parse_config(conf(personal=ctx(quota={"remotas": [
            {"ssh": "tomasherceg@localhost"}]}))).contexts[0]
        self.assertEqual(c.quota.remotas, ["tomasherceg@localhost"])

    def test_remotas_objeto_sin_ssh(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"remotas": [
                {"host": "tomasherceg@localhost"}]})))

    def test_remotas_objeto_sin_arroba(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"remotas": [
                {"ssh": "localhost"}]})))

    def test_remotas_y_tope_juntos(self):
        c = parse_config(conf(personal=ctx(quota={
            "tope_semanal": 900000000,
            "remotas": ["tomasherceg@localhost"]}))).contexts[0]
        self.assertEqual(c.quota.tope_semanal, 900000000.0)
        self.assertEqual(c.quota.remotas, ["tomasherceg@localhost"])

    def test_remotas_no_lista(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"remotas": "tomasherceg@localhost"})))

    def test_remotas_vacia(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"remotas": []})))

    def test_remotas_sin_arroba(self):
        """Una remota es `user@host`: sin `@` no es un destino SSH."""
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"remotas": ["localhost"]})))

    def test_remotas_elemento_no_texto(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(quota={"remotas": [1]})))

    def test_default_config_es_valida(self):
        c = default_config()
        self.assertIsInstance(c, Config)
        self.assertEqual(c.names, ["personal"])
        self.assertEqual(c.default, "personal")

    def test_default_config_sigue_los_repos_de_repos_conf(self):
        """`repos.conf` se migró acá: los mismos repos, ahora como contexto."""
        paths = default_config().contexts[0].repos.paths
        for repo in ("agent-harness", "ERP-IphoneUp", "koku", "Health-Link",
                     "entrevestidos", "herceg-motors"):
            self.assertIn(repo, paths)
        self.assertIn("entrevestidos/*", paths)  # el caso anidado


class TestSeleccion(unittest.TestCase):
    def setUp(self):
        self.c = parse_config({"default_context": "trabajo",
                               "contexts": {"personal": ctx(),
                                            "trabajo": ctx(autonomy="manual")}})

    def test_por_defecto(self):
        self.assertEqual([c.name for c in self.c.select()], ["trabajo"])

    def test_uno(self):
        self.assertEqual([c.name for c in self.c.select("personal")], ["personal"])

    def test_todos(self):
        self.assertEqual([c.name for c in self.c.select(todos=True)],
                         ["personal", "trabajo"])

    def test_todos_gana_sobre_el_nombre(self):
        self.assertEqual(len(self.c.select("personal", todos=True)), 2)

    def test_contexto_que_no_existe(self):
        with self.assertRaises(ConfigError) as e:
            self.c.select("laboral")
        self.assertIn("laboral", str(e.exception))
        self.assertIn("personal, trabajo", str(e.exception))

    def test_default_sin_declarar_es_el_primero(self):
        self.assertEqual(parse_config(conf(personal=ctx())).default, "personal")


class TestErroresClaros(unittest.TestCase):
    """Una config mal escrita da una línea que se entiende, no un stack trace."""

    def esperar(self, data, *fragmentos):
        with self.assertRaises(ConfigError) as e:
            parse_config(data)
        for frag in fragmentos:
            self.assertIn(frag, str(e.exception))

    def test_no_es_un_objeto(self):
        self.esperar([], "se esperaba un objeto JSON", "una lista")

    def test_sin_contexts(self):
        self.esperar({}, 'falta "contexts"')

    def test_contexts_vacio(self):
        self.esperar({"contexts": {}}, "no hay ningún contexto")

    def test_campo_obligatorio_faltante(self):
        for campo in ("tracker", "repos", "autonomy", "budget", "run"):
            with self.subTest(campo=campo):
                roto = ctx()
                del roto[campo]
                self.esperar(conf(personal=roto), 'contexto "personal"', 'falta "%s"' % campo)

    def test_campo_desconocido(self):
        self.esperar(conf(personal=ctx(autonomia="frontier")), '"autonomia"', "campos:")

    def test_tracker_desconocido(self):
        self.esperar(conf(personal=ctx(tracker={"kind": "linear"})),
                     "linear", "github, jira")

    def test_jira_sin_url(self):
        self.esperar(conf(trabajo=ctx(tracker={"kind": "jira", "project": "GRO"})),
                     'contexto "trabajo"', '"url"')

    def test_autonomia_desconocida(self):
        self.esperar(conf(personal=ctx(autonomy="auto")), "auto", "frontier, manual")

    def test_polaridad_desconocida(self):
        self.esperar(conf(personal=ctx(budget={"polarity": "arriba",
                                               "provider": "openrouter"})),
                     "arriba", "remaining, spent")

    def test_budget_manual_sin_total(self):
        self.esperar(conf(personal=ctx(budget={"polarity": "spent", "provider": "manual"})),
                     'falta "total"')

    def test_budget_manual_con_total_no_numerico(self):
        self.esperar(conf(personal=ctx(budget={"polarity": "spent", "provider": "manual",
                                               "total": "doscientos"})),
                     '"total"', "número")

    def test_ssh_sin_host(self):
        self.esperar(conf(personal=ctx(run={"kind": "ssh"})), '"host"')

    def test_repos_paths_no_es_lista(self):
        self.esperar(conf(personal=ctx(repos={"root": "~", "paths": "koku"})),
                     '"paths"', "lista")

    def test_default_context_que_no_existe(self):
        self.esperar({"default_context": "laboral", "contexts": {"personal": ctx()}},
                     "laboral", "no existe")

    def test_el_error_dice_de_que_contexto_habla(self):
        self.esperar({"contexts": {"personal": ctx(), "trabajo": ctx(autonomy="auto")}},
                     'contexto "trabajo"')


class TestTopes(unittest.TestCase):
    """#166: los topes de la corrida se declaran en la config, no duermen
    en el código."""

    def test_sin_seccion_los_defaults_documentados(self):
        t = parse_config(conf(personal=ctx())).contexts[0].topes
        self.assertEqual((t.budget_minutos, t.piso, t.max_pasadas),
                         (180.0, 3.0, 12))
        self.assertEqual((RunTopes().budget_minutos, RunTopes().piso,
                          RunTopes().max_pasadas), (180.0, 3.0, 12))

    def test_topes_se_declaran_en_config(self):
        t = parse_config(conf(personal=ctx(
            topes={"budget_minutos": 60, "piso": 1.5, "max_pasadas": 4})),
        ).contexts[0].topes
        self.assertEqual((t.budget_minutos, t.piso, t.max_pasadas),
                         (60.0, 1.5, 4))

    def test_topes_parciales_mezclan_con_los_defaults(self):
        t = parse_config(conf(personal=ctx(topes={"piso": 2.0}))).contexts[0].topes
        self.assertEqual((t.budget_minutos, t.piso, t.max_pasadas),
                         (180.0, 2.0, 12))

    def test_tope_desconocido_es_error(self):
        with self.assertRaises(ConfigError) as e:
            parse_config(conf(personal=ctx(topes={"tope": 1})))
        self.assertIn('"tope"', str(e.exception))

    def test_topes_no_objeto_es_error(self):
        with self.assertRaises(ConfigError):
            parse_config(conf(personal=ctx(topes=[1, 2])))

    def test_max_usd_por_defecto_es_cero(self):
        """#165: sin declarar, la corrida no autoriza gastar plata."""
        t = parse_config(conf(personal=ctx())).contexts[0].topes
        self.assertEqual(t.max_usd, 0.0)
        self.assertEqual(RunTopes().max_usd, 0.0)

    def test_max_usd_se_declara_en_config(self):
        t = parse_config(conf(personal=ctx(topes={"max_usd": 5}))).contexts[0].topes
        self.assertEqual(t.max_usd, 5.0)

    def test_max_usd_no_finito_es_error(self):
        for v in (float("nan"), float("inf")):
            with self.subTest(v=v):
                with self.assertRaises(ConfigError) as e:
                    parse_config(conf(personal=ctx(topes={"max_usd": v})))
                self.assertIn('"max_usd"', str(e.exception))

    def test_max_usd_negativo_es_error(self):
        with self.assertRaises(ConfigError) as e:
            parse_config(conf(personal=ctx(topes={"max_usd": -1})))
        self.assertIn('"max_usd"', str(e.exception))

    def test_piso_negativo_es_error(self):
        with self.assertRaises(ConfigError) as e:
            parse_config(conf(personal=ctx(topes={"piso": -1})))
        self.assertIn('"piso"', str(e.exception))

    def test_max_pasadas_cero_o_no_entero_es_error(self):
        for v in (0, "12", 1.5, True):
            with self.subTest(v=v):
                with self.assertRaises(ConfigError) as e:
                    parse_config(conf(personal=ctx(topes={"max_pasadas": v})))
                self.assertIn('"max_pasadas"', str(e.exception))

    def test_el_error_dice_de_que_contexto_habla(self):
        with self.assertRaises(ConfigError) as e:
            parse_config(conf(personal=ctx(topes={"piso": -1})))
        self.assertIn('contexto "personal"', str(e.exception))


class TestSecretos(unittest.TestCase):
    def test_el_token_se_guarda_pero_no_se_muestra_en_el_default(self):
        """DEFAULT_CONFIG no trae credenciales: las credenciales son del usuario."""
        self.assertNotIn("token", repr(DEFAULT_CONFIG))


if __name__ == "__main__":
    unittest.main()
