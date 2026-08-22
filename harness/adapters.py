"""Capa fina sobre el mundo: red, disco y subprocesos. No decide nada.

Cada adaptador trae datos crudos y devuelve `None` cuando no pudo traerlos.
Ninguno levanta excepción: un adaptador caído degrada su sección del snapshot
en vez de llevarse la pantalla. La lógica está en `harness.snapshot`.
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

from harness.snapshot import READINESS

GITHUB_DIR = Path.home() / "Documents" / "Github"
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


# --------------------------------------------------------------------------- repos
def read_repos(root):
    conf = root / "repos.conf"
    if not conf.exists():
        return []
    out = []
    for line in conf.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        p = Path(line) if Path(line).is_absolute() else GITHUB_DIR / line
        if p.is_dir():
            out.append(p)
    return out


def slug_of(path):
    ok, url = run(["git", "-C", str(path), "remote", "get-url", "origin"])
    if not ok:
        return None
    m = re.search(r"github\.com[:/]+([^/]+/[^/\s]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def gh_json(slug, args):
    ok, out = run(["gh", *args, "-R", slug])
    if not ok or not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def collect_repo(path, offline=False):
    """Todo lo crudo de un repo. Una llamada por dato, en paralelo afuera."""
    ok, branch = run(["git", "-C", str(path), "branch", "--show-current"])
    branch = branch if ok else None
    ok, porcelain = run(["git", "-C", str(path), "status", "--porcelain"])

    raw = {
        "name": path.name,
        "path": str(path),
        "slug": slug_of(path),  # git es local: también corre en modo sin adaptadores
        "branch": branch,
        "status_porcelain": porcelain if ok else None,
        "exists": {key: (path / rel).exists() for key, rel, _ in READINESS},
        "issues": None,
        "prs": None,
    }
    if offline or not raw["slug"]:
        return raw

    raw["issues"] = gh_json(raw["slug"], ["issue", "list", "--state", "open", "--limit", "200",
                                          "--json", "number,title,labels,body"])
    raw["prs"] = gh_json(raw["slug"], ["pr", "list", "--state", "open", "--limit", "50",
                                       "--json", "number,title,isDraft,headRefName"])
    return raw


def collect(root, offline=False, workers=8):
    """Lo crudo de todos los adaptadores, en paralelo. Entrada de `snapshot()`."""
    repos = read_repos(root)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        fut_credits = ex.submit(openrouter_credits, offline)
        fut_agents = ex.submit(herdr_agents, offline)
        raw_repos = list(ex.map(lambda p: collect_repo(p, offline), repos))
    return {
        "offline": offline,
        "credits": fut_credits.result(),
        "agents": fut_agents.result(),
        "repos": raw_repos,
    }
