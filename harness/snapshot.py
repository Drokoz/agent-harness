"""El estado del trabajo autónomo, calculado de una vez y sin tocar nada.

`snapshot(raw)` es pura: recibe lo crudo que trajeron los adaptadores y devuelve
el `Snapshot` que dibuja `harness.render`. No hace red, no lee disco, no llama
subprocesos. Toda la lógica riesgosa vive acá —qué está en la frontera, qué está
bloqueado, cuánto presupuesto queda, qué le falta a cada repo— que es lo único
que los tests necesitan mirar.

Un adaptador caído llega como `None` y degrada su sección: esa parte queda vacía
y marcada en `degraded`, el resto del snapshot se calcula igual.

El snapshot es por contexto: cada uno trae su presupuesto, sus repos y su frontera.
Los agentes de herdr son de la máquina, así que viven arriba de los contextos.

La forma de `raw` (ver `harness.adapters.collect`):

    {
      "offline": bool,
      "agents": [ {...} ] | None,          # None = no hay sesión de herdr
      "contexts": [
        {
          "name": str,
          "tracker": "github" | "jira",
          "autonomy": "frontier" | "manual",
          "vault": str | None,
          "run": "local" | "ssh",
          "budget": {
            "polarity": "remaining" | "spent",
            "provider": "openrouter" | "manual" | "none",
            "credits": {"total_credits": float, "total_usage": float} | None,
            "total": float, "used": float,   # sólo con provider "manual"
          },
          "repos": [
            {
              "name": str,
              "tracker": str,
              "slug": str | None,              # None = repo sin remote de GitHub
              "branch": str | None,
              "default_branch": str | None,    # a qué rama apunta origin/HEAD; None =
                                               # sin remote o sin origin/HEAD
              "status_porcelain": str | None,  # salida cruda de git status --porcelain
              "readiness_source": "default-branch" | "working-tree",
              "exists": {clave_de_READINESS: bool},  # según readiness_source
              # Issue: {"number": int, "title": str, "body": str,
              #         "labels": [{"name": str}],
              #         "blocked_by": int | None}  # bloqueantes abiertos según las
              #                                     # dependencias nativas de GitHub;
              #                                     # None = sin datos nativos,
              #                                     # se parsea el body (fallback)
              "issues": [ {...} ] | None,      # None = gh no contestó
              "prs": [ {...} ] | None,
            },
          ],
        },
      ],
    }
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional

SCHEMA_VERSION = 2

AGENT_LABEL = "ready-for-agent"
TRIAGE_LABEL = "needs-triage"
COSTO_TICKET = 0.24  # estimación §8 del PLAN; ajustar cuando haya datos reales

# Lo que un repo necesita para entrar al harness, en orden de dependencia.
READINESS = [
    ("gate", "scripts/gate.sh", "el gate: única fuente de verdad de 'esto mergea'"),
    ("skills", "docs/agents/issue-tracker.md", "/setup-matt-pocock-skills corrido"),
    ("context", "CONTEXT.md", "vocabulario del dominio para agentes baratos"),
]


@dataclass
class Budget:
    """El presupuesto de un contexto. `state`: offline | ok | missing | unset.

    `polarity` es para qué lado se mira, y son objetivos opuestos: en personal
    `remaining` (que no se acabe), en trabajo `spent` (que no sobre). El número
    es el mismo; lo que cambia es qué significa que esté alto.
    """

    state: str
    polarity: str = "remaining"
    provider: str = "none"
    total: float = 0.0
    used: float = 0.0
    left: float = 0.0
    tickets: int = 0
    ratio: float = 0.0


@dataclass
class Agent:
    who: str
    agent: str
    status: str
    repo: str
    pane: str


@dataclass
class Agents:
    """`state`: offline | outside (no estamos en herdr) | empty | ok."""

    state: str
    items: List[Agent] = field(default_factory=list)


@dataclass
class Issue:
    number: int
    title: str


@dataclass
class Pr:
    number: int
    title: str
    draft: bool


@dataclass
class Repo:
    name: str
    tracker: str
    slug: Optional[str]
    branch: str
    dirty: int
    ready: Dict[str, bool]
    default_branch: Optional[str] = None  # la rama que mira `ready`; None = no se pudo
    readiness_source: str = "working-tree"  # "default-branch" | "working-tree"
    missing: List[str] = field(default_factory=list)
    frontier: List[Issue] = field(default_factory=list)
    blocked: List[Issue] = field(default_factory=list)
    triage: List[Issue] = field(default_factory=list)
    prs: List[Pr] = field(default_factory=list)
    degraded: List[str] = field(default_factory=list)
    frontier_source: Optional[str] = None  # "native" | "body" | None
    closed_count: Optional[int] = None  # issues cerrados: sin esto sólo se sabe cuánto queda
    path: str = ""  # dónde vive en el disco (el dispatcher crea worktrees a partir de acá)

    @property
    def has_work(self):
        """Si no hay nada de esto, el repo no aparece en la sección Trabajo."""
        return bool(self.frontier or self.blocked or self.prs or self.triage)


@dataclass
class Context:
    """Un contexto ya resuelto: lo que declaró la config más lo que trajo el mundo."""

    name: str
    tracker: str
    autonomy: str
    run: str
    vault: Optional[str]
    budget: Budget
    repos: List[Repo] = field(default_factory=list)


@dataclass
class Snapshot:
    offline: bool
    agents: Agents
    contexts: List[Context] = field(default_factory=list)


BLOCKED_RE = re.compile(r"blocked by[:\s]*((?:#\d+[,\s]*)+)", re.I)


def blockers_of(body):
    """Los #N que un issue declara como bloqueantes en su cuerpo."""
    m = BLOCKED_RE.search(body or "")
    return {int(n) for n in re.findall(r"#(\d+)", m.group(1))} if m else set()


def _budget(raw, offline):
    raw = raw or {}
    polarity = raw.get("polarity", "remaining")
    provider = raw.get("provider", "none")
    base = dict(polarity=polarity, provider=provider)
    if offline:
        return Budget(state="offline", **base)
    if provider == "none":
        return Budget(state="unset", **base)
    if provider == "openrouter":
        credits = raw.get("credits")
        try:
            total = float(credits["total_credits"])
            used = float(credits["total_usage"])
        except (TypeError, KeyError, ValueError):
            return Budget(state="missing", **base)
    else:
        try:
            total = float(raw.get("total"))
            used = float(raw.get("used") or 0.0)
        except (TypeError, ValueError):
            return Budget(state="missing", **base)
    left = total - used
    return Budget(
        state="ok",
        total=total,
        used=used,
        left=left,
        tickets=int(left / COSTO_TICKET) if COSTO_TICKET else 0,
        ratio=(used / total) if total else 0.0,
        **base
    )


def _agents(raw, offline):
    if offline:
        return Agents(state="offline")
    if raw is None:
        return Agents(state="outside")
    if not raw:
        return Agents(state="empty")
    # Primero lo que necesita a un humano (blocked), después lo que está corriendo.
    ordenados = sorted(raw, key=lambda a: (a.get("agent_status") != "blocked",
                                           a.get("agent_status") != "working"))
    items = [
        Agent(
            # `name` sólo existe si el agente fue arrancado o renombrado con nombre
            # propio; si no, el título del terminal es lo más informativo que hay.
            who=a.get("name") or a.get("terminal_title_stripped") or "—",
            agent=a.get("agent", "?"),
            status=a.get("agent_status", "?"),
            repo=PurePosixPath(a.get("cwd", "")).name,
            pane=a.get("pane_id", ""),
        )
        for a in ordenados
    ]
    return Agents(state="ok", items=items)


def _issues(raw):
    """Parte los issues en frontera, bloqueados y sin triage.

    Un issue está bloqueado sólo si le quedan bloqueantes abiertos. La fuente de
    verdad son las dependencias nativas de GitHub (`blocked_by`: bloqueantes
    abiertos, lo que ve la UI); el parseo del body (`blockers_of`) queda como
    fallback para los issues sin datos nativos.
    """
    open_nums = {i["number"] for i in raw}
    frontier, blocked, triage = [], [], []
    for i in raw:
        labels = {l["name"] for l in i.get("labels", [])}
        issue = Issue(number=i["number"], title=i.get("title", ""))
        if TRIAGE_LABEL in labels:
            triage.append(issue)
        if AGENT_LABEL not in labels:
            continue
        blocked_by = i.get("blocked_by")
        if blocked_by is not None:
            es_bloqueado = int(blocked_by) > 0
        else:
            es_bloqueado = bool(blockers_of(i.get("body")) & open_nums)
        (blocked if es_bloqueado else frontier).append(issue)
    return frontier, blocked, triage


def _issues_source(issues):
    """De dónde salió la frontera de este repo: dependencias nativas o parseo
    del body. None cuando no hay issues que clasificar."""
    if not issues:
        return None
    if all(i.get("blocked_by") is not None for i in issues):
        return "native"
    return "body"


def _repo(raw, offline=False):
    porcelain = raw.get("status_porcelain")
    dirty = len([l for l in porcelain.splitlines() if l.strip()]) if porcelain else 0
    ready = {key: bool(raw.get("exists", {}).get(key)) for key, _, _ in READINESS}

    repo = Repo(
        name=raw.get("name", "?"),
        tracker=raw.get("tracker", "github"),
        slug=raw.get("slug"),
        branch=raw.get("branch") or "?",
        path=raw.get("path", ""),
        dirty=dirty,
        ready=ready,
        default_branch=raw.get("default_branch"),
        readiness_source=raw.get("readiness_source", "working-tree"),
        missing=[desc for key, _, desc in READINESS if not ready[key]],
    )
    if offline or repo.tracker != "github" or not repo.slug:
        # Sin remote de GitHub, con otro tracker, o sin adaptadores, no hay issues
        # ni PRs que traer: el repo sigue existiendo y sigue contando en readiness.
        return repo

    repo.closed_count = raw.get("closed_count")
    issues, prs = raw.get("issues"), raw.get("prs")
    if issues is None:
        repo.degraded.append("issues")
    else:
        repo.frontier, repo.blocked, repo.triage = _issues(issues)
        repo.frontier_source = _issues_source(issues)
    if prs is None:
        repo.degraded.append("prs")
    else:
        repo.prs = [
            Pr(number=p["number"], title=p.get("title", ""), draft=bool(p.get("isDraft")))
            for p in prs
        ]
    return repo


def _context(raw, offline):
    return Context(
        name=raw.get("name", "?"),
        tracker=raw.get("tracker", "github"),
        autonomy=raw.get("autonomy", "?"),
        run=raw.get("run", "?"),
        vault=raw.get("vault"),
        budget=_budget(raw.get("budget"), offline),
        repos=[_repo(r, offline) for r in raw.get("repos", [])],
    )


def snapshot(raw):
    """Todo el estado, en una estructura que sólo hay que dibujar."""
    offline = bool(raw.get("offline"))
    return Snapshot(
        offline=offline,
        agents=_agents(raw.get("agents"), offline),
        contexts=[_context(c, offline) for c in raw.get("contexts", [])],
    )


def as_dict(snap):
    """El snapshot como JSON: el contrato con cualquier cosa que no sea la terminal.

    `version` está para que un consumidor sepa contra qué forma se escribió; sube
    cuando un campo cambia de significado o desaparece.
    """
    return dict(version=SCHEMA_VERSION, **asdict(snap))
