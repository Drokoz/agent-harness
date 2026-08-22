"""Cosas compartidas por los tests: las fixtures y el escenario del golden.

Importar esto también pone la raíz del repo en sys.path, que es lo que hace
importable el paquete `harness` cuando unittest descubre desde tests/.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN = ROOT / "tests" / "golden"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Después de tocar sys.path: si no, `harness` todavía no es importable.
from harness.adapters import normalize_issues  # noqa: E402
from harness.config import parse_config  # noqa: E402


def fixture(name):
    """Una salida cruda capturada del mundo real. Ver tests/fixtures/README.md."""
    return json.loads((FIXTURES / name).read_text())


def gh_issues():
    return fixture("gh_issue_list.json")


def gh_issues_native():
    """Issues con dependencias nativas: la respuesta real de GraphQL, normalizada
    igual que la trae el adaptador."""
    nodes = fixture("gh_issue_graphql.json")["data"]["repository"]["issues"]["nodes"]
    return normalize_issues(nodes)


def gh_prs():
    return fixture("gh_pr_list.json")


def herdr_agents():
    return fixture("herdr_agent_list.json")["result"]["agents"]


def openrouter_credits():
    return fixture("openrouter_credits.json")["data"]


# Un repo inventado para que el golden ejercite el fallback al parseo del body
# (sin datos nativos): un issue bloqueado por otro abierto, uno "bloqueado" por
# uno ya cerrado, algo sin triage y un PR en draft.
KOKU_ISSUES = [
    {"number": 7, "title": "Cerrar caja del dia sin doble conteo",
     "labels": [{"name": "ready-for-agent"}], "body": "Sin bloqueos."},
    {"number": 8, "title": "Reporte mensual por sucursal",
     "labels": [{"name": "ready-for-agent"}], "body": "Blocked by #7"},
    {"number": 9, "title": "Exportar a CSV",
     "labels": [{"name": "ready-for-agent"}], "body": "Blocked by #99"},
    {"number": 10, "title": "Se rompe el login con Safari",
     "labels": [{"name": "needs-triage"}], "body": ""},
]
KOKU_PRS = [
    {"number": 21, "title": "WIP: cierre de caja", "isDraft": True, "headRefName": "ticket/7"},
    {"number": 22, "title": "Migrar el schema de ventas", "isDraft": False,
     "headRefName": "ticket/5"},
]

READY_TODO = {"gate": True, "skills": True, "context": True}

# Los dos contextos del PLAN, con las dos polaridades de presupuesto y los dos
# trackers. El token está para que los tests puedan verificar que no se filtra.
DOS_CONTEXTOS = {
    "default_context": "personal",
    "contexts": {
        "personal": {
            "tracker": {"kind": "github"},
            "repos": {"root": "~/Documents/Github", "paths": ["agent-harness", "koku"]},
            "autonomy": "frontier",
            "budget": {"polarity": "remaining", "provider": "openrouter"},
            "vault": "~/notas",
            "run": {"kind": "local"},
        },
        "trabajo": {
            "tracker": {"kind": "jira", "url": "https://wl.atlassian.net",
                        "project": "GRO", "token": "secreto-de-jira"},
            "repos": {"root": "~/work", "paths": ["groceries-wl"]},
            "autonomy": "manual",
            "budget": {"polarity": "spent", "provider": "manual",
                       "total": 200.0, "used": 128.4},
            "vault": "~/wl-devlead-vault",
            "run": {"kind": "ssh", "host": "wl@localhost"},
        },
    },
}


def config_dos_contextos():
    return parse_config(DOS_CONTEXTOS)


def raw_personal():
    """El contexto personal del golden, tal como lo devuelven los adaptadores."""
    return {
        "name": "personal",
        "tracker": "github",
        "autonomy": "frontier",
        "vault": "~/notas",
        "run": "local",
        "budget": {"polarity": "remaining", "provider": "openrouter",
                   "credits": openrouter_credits(), "total": 0.0, "used": 0.0},
        "repos": [
            # agent-harness con la fixture real de GraphQL: la frontera por
            # dependencias nativas, que es el camino de por defecto.
            {"name": "agent-harness", "tracker": "github", "slug": "Drokoz/agent-harness",
             "branch": "ticket/3", "status_porcelain": " M bin/harness\n?? harness/\n",
             "exists": dict(READY_TODO), "issues": gh_issues_native(), "prs": gh_prs()},
            {"name": "koku", "tracker": "github", "slug": "Drokoz/koku", "branch": "main",
             "status_porcelain": "",
             "exists": {"gate": True, "skills": False, "context": False},
             "issues": KOKU_ISSUES, "prs": KOKU_PRS},
            {"name": "sin-remote", "tracker": "github", "slug": None, "branch": "main",
             "status_porcelain": "",
             "exists": {"gate": False, "skills": False, "context": False},
             "issues": None, "prs": None},
        ],
    }


def raw_trabajo():
    """El contexto trabajo: tracker Jira (todavía sin adaptador) y presupuesto a gastar."""
    return {
        "name": "trabajo",
        "tracker": "jira",
        "autonomy": "manual",
        "vault": "~/wl-devlead-vault",
        "run": "ssh",
        "budget": {"polarity": "spent", "provider": "manual", "credits": None,
                   "total": 200.0, "used": 128.4},
        "repos": [
            {"name": "groceries-wl", "tracker": "jira", "slug": "wl/groceries",
             "branch": "develop", "status_porcelain": " M pom.xml\n",
             "exists": {"gate": True, "skills": False, "context": True},
             "issues": None, "prs": None},
        ],
    }


def golden_raw(*nombres):
    """El escenario fijo del golden: lo crudo, tal como sale de `adapters.collect`."""
    disponibles = {"personal": raw_personal, "trabajo": raw_trabajo}
    nombres = nombres or ("personal", "trabajo")
    return {
        "offline": False,
        "agents": herdr_agents(),
        "contexts": [disponibles[n]() for n in nombres],
    }
