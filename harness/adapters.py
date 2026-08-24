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
        if p.returncode == 0:
            return True, p.stdout.strip()
        # Cuando falla, el motivo casi siempre está en stderr (git, gh y herdr
        # escriben ahí). Devolver sólo stdout deja el log diciendo "fallo: ".
        return False, (p.stderr.strip() or p.stdout.strip())
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


def default_branch_of(path):
    """La rama por defecto del remote: a qué rama apunta `origin/HEAD`.

    `origin/HEAD` es un ref simbólico local que git mantiene con `git fetch`,
    así que consultarlo no toca la red (como `slug_of`). None cuando no hay
    remote origin o git nunca supo cuál es la rama por defecto.
    """
    ok, out = run(["git", "-C", str(path), "symbolic-ref", "refs/remotes/origin/HEAD"])
    prefijo = "refs/remotes/origin/"
    if not ok or not out.startswith(prefijo):
        return None
    rama = out[len(prefijo):]
    return rama or None


def exists_on_default_branch(path):
    """Los archivos de READINESS que están en la rama por defecto del remote.

    Una sola llamada local de git (no toca la red): el árbol completo de
    `origin/HEAD`. None cuando la rama por defecto no se puede inspeccionar
    (sin remote, sin `origin/HEAD`, git falló): el llamador cae al working
    tree en vez de inventar un estado.
    """
    ok, out = run(["git", "-C", str(path), "ls-tree", "-r", "--name-only",
                   "origin/HEAD"])
    if not ok:
        return None
    arbol = set(out.splitlines())
    return {key: rel in arbol for key, rel, _ in READINESS}


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


def pr_state(slug, number):
    """El estado en vivo de un PR: OPEN, MERGED o CLOSED. None si no se pudo
    consultar (sin red, sin gh, PR inexistente): el merge es humano y no deja
    evento en el log del dispatcher, así que esto es lo único que reconcilia
    qué pasó después de lo último que hizo el harness.
    """
    ok, out = run(["gh", "pr", "view", str(number), "--json", "state", "-R", slug])
    if not ok or not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    estado = data.get("state") if isinstance(data, dict) else None
    return estado if estado in ("OPEN", "MERGED", "CLOSED") else None


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
    cerrados: issues(states: [CLOSED]) { totalCount }
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
    """Issues abiertos con dependencias nativas y el conteo de cerrados, vía GraphQL.

    Devuelve (issues, cerrados). `(None, None)` si gh no contesta o la consulta
    falla: el llamador usa el fallback REST. El conteo de cerrados va en la misma
    consulta a propósito — sacarlo por REST agregaría una llamada por repo y
    rompería el invariante de que los issues abiertos no salen del REST."""
    owner, sep, name = slug.partition("/")
    if not sep or not owner or not name:
        return None, None
    nodes, after, cerrados = [], None, None
    for _ in range(2):
        args = ["api", "graphql", "-f", "query=" + ISSUES_GQL,
                "-F", "owner=" + owner, "-F", "name=" + name]
        if after:
            args += ["-F", "after=" + after]
        ok, out = run(["gh", *args])
        if not ok:
            return None, None
        try:
            data = json.loads(out)
        except ValueError:
            return None, None
        if data.get("errors"):
            return None, None
        repo_node = (data.get("data") or {}).get("repository") or {}
        if cerrados is None:
            cerrados = (repo_node.get("cerrados") or {}).get("totalCount")
        issues = repo_node.get("issues") or {}
        nodes.extend(issues.get("nodes") or [])
        info = issues.get("pageInfo") or {}
        after = info.get("endCursor") if info.get("hasNextPage") else None
        if not after:
            break
    return normalize_issues(nodes), cerrados


def collect_repo(path, tracker="github", offline=False):
    """Todo lo crudo de un repo. Una llamada por dato, en paralelo afuera.

    `tracker` es el del contexto: sólo a un repo de un contexto con tracker de
    GitHub tiene sentido pedirle issues y PRs con `gh`.
    """
    ok, branch = run(["git", "-C", str(path), "branch", "--show-current"])
    branch = branch if ok else None
    ok, porcelain = run(["git", "-C", str(path), "status", "--porcelain"])

    # La readiness se mira en la rama por defecto, no en el working tree: un
    # repo parado en una rama de feature anterior al gate no debe decir que
    # "falta el gate" si la rama por defecto ya lo tiene. Git es local (lo
    # mismo que `slug_of`): también corre en modo sin adaptadores. Si la rama
    # por defecto no se puede inspeccionar, cae al working tree y lo marca.
    default = default_branch_of(path)
    en_defecto = exists_on_default_branch(path)

    raw = {
        "name": path.name,
        "path": str(path),
        "tracker": tracker,
        "slug": slug_of(path),  # git es local: también corre en modo sin adaptadores
        "branch": branch,
        "default_branch": default,
        "status_porcelain": porcelain if ok else None,
        "readiness_source": ("default-branch" if en_defecto is not None
                             else "working-tree"),
        "exists": en_defecto if en_defecto is not None
        else {key: (path / rel).exists() for key, rel, _ in READINESS},
        "issues": None,
        "prs": None,
    }
    if offline or tracker != "github" or not raw["slug"]:
        return raw

    # La frontera sale de las dependencias nativas de GitHub (GraphQL). Si no
    # están disponibles, cae al REST y `snapshot` parsea el body como fallback.
    raw["issues"], raw["closed_count"] = gh_graphql_issues(raw["slug"])
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
