"""El estado del trabajo autónomo, calculado de una vez y sin tocar nada.

`snapshot(raw)` es pura: recibe lo crudo que trajeron los adaptadores y devuelve
el `Snapshot` que dibuja `harness.render`. No hace red, no lee disco, no llama
subprocesos. Toda la lógica riesgosa vive acá —qué está en la frontera, qué está
bloqueado, cuánto presupuesto queda, qué le falta a cada repo— que es lo único
que los tests necesitan mirar.

Un adaptador caído llega como `None` y degrada su sección: esa parte queda vacía
y marcada en `degraded`, el resto del snapshot se calcula igual.

La forma de `raw` (ver `harness.adapters.collect`):

    {
      "offline": bool,
      "credits": {"total_credits": float, "total_usage": float} | None,
      "agents": [ {...} ] | None,          # None = no hay sesión de herdr
      "repos": [
        {
          "name": str,
          "slug": str | None,              # None = repo sin remote de GitHub
          "branch": str | None,
          "status_porcelain": str | None,  # salida cruda de git status --porcelain
          "exists": {clave_de_READINESS: bool},
          "issues": [ {...} ] | None,      # None = gh no contestó
          "prs": [ {...} ] | None,
        },
      ],
    }
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional

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
    """Presupuesto de OpenRouter. `state`: offline | ok | missing."""

    state: str
    total: float = 0.0
    used: float = 0.0
    left: float = 0.0
    tickets: int = 0


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
    slug: Optional[str]
    branch: str
    dirty: int
    ready: Dict[str, bool]
    missing: List[str] = field(default_factory=list)
    frontier: List[Issue] = field(default_factory=list)
    blocked: List[Issue] = field(default_factory=list)
    triage: List[Issue] = field(default_factory=list)
    prs: List[Pr] = field(default_factory=list)
    degraded: List[str] = field(default_factory=list)

    @property
    def has_work(self):
        """Si no hay nada de esto, el repo no aparece en la sección Trabajo."""
        return bool(self.frontier or self.blocked or self.prs or self.triage)


@dataclass
class Snapshot:
    offline: bool
    budget: Budget
    agents: Agents
    repos: List[Repo] = field(default_factory=list)


BLOCKED_RE = re.compile(r"blocked by[:\s]*((?:#\d+[,\s]*)+)", re.I)


def blockers_of(body):
    """Los #N que un issue declara como bloqueantes en su cuerpo."""
    m = BLOCKED_RE.search(body or "")
    return {int(n) for n in re.findall(r"#(\d+)", m.group(1))} if m else set()


def _budget(credits, offline):
    if offline:
        return Budget(state="offline")
    try:
        total = float(credits["total_credits"])
        used = float(credits["total_usage"])
    except (TypeError, KeyError, ValueError):
        return Budget(state="missing")
    left = total - used
    tickets = int(left / COSTO_TICKET) if COSTO_TICKET else 0
    return Budget(state="ok", total=total, used=used, left=left, tickets=tickets)


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

    Un issue está bloqueado sólo si alguno de sus bloqueantes sigue abierto: un
    "Blocked by #99" que ya se cerró no lo saca de la frontera.
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
        pendientes = blockers_of(i.get("body")) & open_nums
        (blocked if pendientes else frontier).append(issue)
    return frontier, blocked, triage


def _repo(raw, offline=False):
    porcelain = raw.get("status_porcelain")
    dirty = len([l for l in porcelain.splitlines() if l.strip()]) if porcelain else 0
    ready = {key: bool(raw.get("exists", {}).get(key)) for key, _, _ in READINESS}

    repo = Repo(
        name=raw.get("name", "?"),
        slug=raw.get("slug"),
        branch=raw.get("branch") or "?",
        dirty=dirty,
        ready=ready,
        missing=[desc for key, _, desc in READINESS if not ready[key]],
    )
    if offline or not repo.slug:
        # Sin remote de GitHub —o sin adaptadores— no hay issues ni PRs que traer:
        # el repo sigue existiendo y sigue contando para la tabla de readiness.
        return repo

    issues, prs = raw.get("issues"), raw.get("prs")
    if issues is None:
        repo.degraded.append("issues")
    else:
        repo.frontier, repo.blocked, repo.triage = _issues(issues)
    if prs is None:
        repo.degraded.append("prs")
    else:
        repo.prs = [
            Pr(number=p["number"], title=p.get("title", ""), draft=bool(p.get("isDraft")))
            for p in prs
        ]
    return repo


def snapshot(raw):
    """Todo el estado, en una estructura que sólo hay que dibujar."""
    offline = bool(raw.get("offline"))
    return Snapshot(
        offline=offline,
        budget=_budget(raw.get("credits"), offline),
        agents=_agents(raw.get("agents"), offline),
        repos=[_repo(r, offline) for r in raw.get("repos", [])],
    )
