"""Capa fina sobre el mundo: red, disco y subprocesos. No decide nada.

Cada adaptador trae datos crudos y devuelve `None` cuando no pudo traerlos.
Ninguno levanta excepción: un adaptador caído degrada su sección del snapshot
en vez de llevarse la pantalla. La lógica está en `harness.snapshot`.

La única excepción es `load_config`: una config rota no se degrada en silencio
—sin contextos no hay nada que mirar— así que levanta `ConfigError` y el CLI la
imprime como una línea de error.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from harness.config import (CONFIG_ENV, CONFIG_NAME, ConfigError, default_config,
                            parse_config)
from harness.snapshot import READINESS

PI_MODELS = Path.home() / ".pi" / "agent" / "models.json"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"


def run(args, cwd=None, timeout=30):
    """Corre un comando y devuelve (ok, stdout). Nunca levanta excepción."""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, p.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""


# --------------------------------------------------------------------- OpenRouter
def openrouter_credits(offline=False):
    """`data` crudo de /credits, o None si no hay key o la API no contesta."""
    if offline:
        return None
    try:
        key = json.loads(PI_MODELS.read_text())["providers"]["openrouter"]["apiKey"]
    except (OSError, KeyError, ValueError):
        return None
    req = urllib.request.Request(CREDITS_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)["data"]
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError):
        return None


# -------------------------------------------------------------------------- herdr
def herdr_agents(offline=False):
    """Los agentes crudos de `herdr agent list`. None = no hay sesión que inspeccionar."""
    if offline:
        return None
    if os.environ.get("HERDR_ENV") != "1":
        return None  # no estamos dentro de herdr: no hay sesión que inspeccionar
    ok, out = run(["herdr", "agent", "list"])
    if not ok:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    agents = data.get("result", data)
    if isinstance(agents, dict):
        agents = agents.get("agents", [])
    return agents if isinstance(agents, list) else []


# -------------------------------------------------------------------------- config
def config_path():
    """Dónde vive la config. `HARNESS_CONFIG_DIR` la mueve (los tests la usan)."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser() / CONFIG_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "harness" / CONFIG_NAME


def load_config(path=None):
    """La config del usuario, o la de arranque si todavía no escribió ninguna."""
    path = Path(path) if path else config_path()
    try:
        texto = path.read_text()
    except FileNotFoundError:
        return default_config()
    except OSError as e:
        raise ConfigError("{}: no se pudo leer ({})".format(path, e.strerror))
    try:
        data = json.loads(texto)
    except ValueError as e:
        raise ConfigError("{}: JSON inválido ({})".format(path, e))
    return parse_config(data, donde=str(path))


# --------------------------------------------------------------------------- repos
def repo_paths(repos):
    """Los repos de un contexto, ya en el disco.

    Un path que termina en `/*` son los repos git que cuelgan de esa carpeta (el
    caso entrevestidos: una carpeta contenedora con un repo por subproyecto). Lo
    que no existe se saltea, como hacía `repos.conf`: un repo que no está todavía
    no es un error de config.
    """
    root = Path(repos.root).expanduser()
    out = []
    for entrada in repos.paths:
        p = Path(entrada).expanduser()
        if not p.is_absolute():
            p = root / entrada
        if p.name == "*":
            out.extend(sorted(h for h in _subdirs(p.parent) if (h / ".git").exists()))
        elif p.is_dir():
            out.append(p)
    vistos, unicos = set(), []
    for p in out:
        if str(p) not in vistos:
            vistos.add(str(p))
            unicos.append(p)
    return unicos


def _subdirs(path):
    try:
        return [h for h in path.iterdir() if h.is_dir()]
    except OSError:
        return []


def slug_of(path):
    ok, url = run(["git", "-C", str(path), "remote", "get-url", "origin"])
    if not ok:
        return None
    m = re.search(r"github\.com[:/]+([^/]+/[^/\s]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def _markdown_files(dirpath):
    """Los .md de una carpeta, ordenados. Carpeta inexistente → []: no todo
    repo documenta decisiones y una vault vacía no es un error de config."""
    try:
        return sorted(f for f in dirpath.iterdir()
                      if f.is_file() and f.suffix == ".md")
    except OSError:
        return []


def adr_files(repo_path):
    """Los ADRs de un repo (`docs/adr/*.md`), ya en el disco y ordenados."""
    return _markdown_files(Path(repo_path) / "docs" / "adr")


def vault_decision_files(vault):
    """Las notas de `vault/decisiones/`. Vault sin declarar, sin carpeta o
    vacía → []: un contexto sin vault no rompe, sólo no aporta notas."""
    if not vault:
        return []
    return _markdown_files(Path(vault).expanduser() / "decisiones")


def gh_json(slug, args):
    ok, out = run(["gh", *args, "-R", slug])
    if not ok or not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


# Issues abiertos con sus dependencias nativas: `blockedBy` es la cantidad de
# bloqueantes ABIERTOS, la misma que ve la UI de GitHub. Dos páginas de 100:
# mismo techo que el `--limit 200` del REST de antes.
ISSUES_GQL = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(states: [OPEN], first: 100, after: $after) {
      nodes {
        number
        title
        body
        labels(first: 20) { nodes { name } }
        issueDependenciesSummary { blockedBy }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".strip()


def normalize_issue(node):
    """Un issue (nodo GraphQL o salida de `gh issue list`) a la forma que lee
    `snapshot`: `blocked_by` son los bloqueantes abiertos según las dependencias
    nativas; None cuando no hay datos nativos y hay que parsear el body."""
    summary = node.get("issueDependenciesSummary") or {}
    blocked = summary.get("blockedBy")
    if isinstance(blocked, bool) or not isinstance(blocked, int):
        blocked = None
    labels = node.get("labels") or {}
    label_nodes = labels.get("nodes") if isinstance(labels, dict) else labels
    label_nodes = label_nodes or []
    return {
        "number": node["number"],
        "title": node.get("title") or "",
        "body": node.get("body") or "",
        "labels": [{"name": l["name"]} for l in label_nodes],
        "blocked_by": blocked,
    }


def normalize_issues(nodes):
    return [normalize_issue(n) for n in nodes]


def gh_graphql_issues(slug):
    """Issues abiertos con dependencias nativas, vía GraphQL. None si gh no
    contesta o la consulta falla: el llamador usa el fallback REST."""
    owner, sep, name = slug.partition("/")
    if not sep or not owner or not name:
        return None
    nodes, after = [], None
    for _ in range(2):
        args = ["api", "graphql", "-f", "query=" + ISSUES_GQL,
                "-F", "owner=" + owner, "-F", "name=" + name]
        if after:
            args += ["-F", "after=" + after]
        ok, out = run(["gh", *args])
        if not ok:
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        if data.get("errors"):
            return None
        issues = (data.get("data") or {}).get("repository", {}).get("issues") or {}
        nodes.extend(issues.get("nodes") or [])
        info = issues.get("pageInfo") or {}
        after = info.get("endCursor") if info.get("hasNextPage") else None
        if not after:
            break
    return normalize_issues(nodes)


def collect_repo(path, tracker="github", offline=False):
    """Todo lo crudo de un repo. Una llamada por dato, en paralelo afuera.

    `tracker` es el del contexto: sólo a un repo de un contexto con tracker de
    GitHub tiene sentido pedirle issues y PRs con `gh`.
    """
    ok, branch = run(["git", "-C", str(path), "branch", "--show-current"])
    branch = branch if ok else None
    ok, porcelain = run(["git", "-C", str(path), "status", "--porcelain"])

    raw = {
        "name": path.name,
        "path": str(path),
        "tracker": tracker,
        "slug": slug_of(path),  # git es local: también corre en modo sin adaptadores
        "branch": branch,
        "status_porcelain": porcelain if ok else None,
        "exists": {key: (path / rel).exists() for key, rel, _ in READINESS},
        "issues": None,
        "prs": None,
    }
    if offline or tracker != "github" or not raw["slug"]:
        return raw

    # La frontera sale de las dependencias nativas de GitHub (GraphQL). Si no
    # están disponibles, cae al REST y `snapshot` parsea el body como fallback.
    raw["issues"] = gh_graphql_issues(raw["slug"])
    if raw["issues"] is None:
        rest = gh_json(raw["slug"], ["issue", "list", "--state", "open", "--limit", "200",
                                     "--json", "number,title,labels,body"])
        raw["issues"] = normalize_issues(rest) if rest is not None else None
    raw["prs"] = gh_json(raw["slug"], ["pr", "list", "--state", "open", "--limit", "50",
                                       "--json", "number,title,isDraft,headRefName"])
    return raw


def collect_budget(spec, credits):
    """Lo crudo del presupuesto de un contexto. Los números los saca `snapshot`."""
    return {
        "polarity": spec.polarity,
        "provider": spec.provider,
        "credits": credits if spec.provider == "openrouter" else None,
        "total": spec.total,
        "used": spec.used,
    }


def collect(contexts, offline=False, workers=8):
    """Lo crudo de todos los contextos, en paralelo. Entrada de `snapshot()`.

    Los agentes de herdr son de la máquina, no de un contexto: se piden una sola
    vez. Los créditos de OpenRouter también, aunque los mire más de un contexto.
    """
    quiere_credits = any(c.budget.provider == "openrouter" for c in contexts)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        fut_agents = ex.submit(herdr_agents, offline)
        fut_credits = ex.submit(openrouter_credits, offline) if quiere_credits else None
        pendientes = [
            (c, [ex.submit(collect_repo, p, c.tracker.kind, offline)
                 for p in repo_paths(c.repos)])
            for c in contexts
        ]
        credits = fut_credits.result() if fut_credits else None
        crudos = [
            {
                "name": c.name,
                "tracker": c.tracker.kind,
                "autonomy": c.autonomy,
                "vault": c.vault,
                "run": c.run.kind,
                "budget": collect_budget(c.budget, credits),
                "repos": [f.result() for f in futs],
            }
            for c, futs in pendientes
        ]
        agents = fut_agents.result()
    return {"offline": offline, "agents": agents, "contexts": crudos}
