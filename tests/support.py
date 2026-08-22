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

from harness.adapters import normalize_issues  # noqa: E402


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


# Un repo inventado para que el golden ejercite lo que las fixtures no tienen:
# un issue bloqueado por otro abierto, uno "bloqueado" por uno ya cerrado, algo
# sin triage y un PR en draft.
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


def golden_raw():
    """El escenario fijo del golden: lo crudo, tal como lo devuelven los adaptadores."""
    return {
        "offline": False,
        "credits": openrouter_credits(),
        "agents": herdr_agents(),
        "repos": [
            {"name": "agent-harness", "slug": "Drokoz/agent-harness", "branch": "ticket/3",
             "status_porcelain": " M bin/harness\n?? harness/\n", "exists": dict(READY_TODO),
             "issues": gh_issues(), "prs": gh_prs()},
            {"name": "koku", "slug": "Drokoz/koku", "branch": "main", "status_porcelain": "",
             "exists": {"gate": True, "skills": False, "context": False},
             "issues": KOKU_ISSUES, "prs": KOKU_PRS},
            {"name": "sin-remote", "slug": None, "branch": "main", "status_porcelain": "",
             "exists": {"gate": False, "skills": False, "context": False},
             "issues": None, "prs": None},
        ],
    }
