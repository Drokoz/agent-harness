"""Los contextos del harness: qué se sigue, con qué tracker y con qué presupuesto.

Un contexto es una vida entera: personal y trabajo tienen tracker distinto, repos
distintos, autonomía distinta, presupuesto con polaridad opuesta y su propia vault.
El harness no tiene "una lista de repos": tiene contextos, y cada uno declara la suya.

La config vive en `~/.config/harness/config.json` —fuera del repo, porque contiene
credenciales— y este módulo no la lee: sólo la valida y la interpreta. El que toca el
disco es `harness.adapters.load_config`; el que expande los repos es
`harness.adapters.repo_paths`.

Forma de un contexto (todo lo que no tiene default es obligatorio):

    "personal": {
      "tracker":  {"kind": "github"},
      "repos":    {"root": "~/Documents/Github",
                   "paths": ["koku", "entrevestidos", "entrevestidos/*"]},
      "autonomy": "frontier",
      "budget":   {"polarity": "remaining", "provider": "openrouter"},
      "vault":    "~/vault",
      "run":      {"kind": "local"}
    }

Un `path` que termina en `/*` son los repos git que cuelgan de esa carpeta: el caso
entrevestidos, una carpeta contenedora con un repo por subproyecto.

Ningún error de acá es un stack trace: todo lo que está mal sale como `ConfigError`
con el nombre del contexto y el campo que hay que arreglar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

CONFIG_ENV = "HARNESS_CONFIG_DIR"
CONFIG_NAME = "config.json"

TRACKERS = ("github", "jira")
AUTONOMIES = ("frontier", "manual")
POLARITIES = ("remaining", "spent")
PROVIDERS = ("openrouter", "manual", "none")
RUNNERS = ("local", "ssh")

CONTEXT_KEYS = ("tracker", "repos", "autonomy", "budget", "vault", "run",
                "quota")


class ConfigError(Exception):
    """Config inválida. El mensaje se lee sin abrir el código: eso es todo el punto."""


@dataclass
class Tracker:
    """De dónde salen los tickets. `token` nunca entra al snapshot ni se imprime."""

    kind: str
    url: str = ""
    project: str = ""
    token: str = ""


@dataclass
class Repos:
    root: str
    paths: List[str] = field(default_factory=list)


@dataclass
class BudgetSpec:
    """Qué presupuesto sigue el contexto y para qué lado se mira.

    `remaining` = cuánto queda antes de quedarme sin (personal: el recurso escaso
    es la plata). `spent` = cuánto llevo usado de lo que hay que gastar (trabajo:
    el presupuesto es de la empresa y dejarlo sin usar también es perderlo).
    """

    polarity: str
    provider: str
    total: float = 0.0
    used: float = 0.0


@dataclass
class QuotaSpec:
    """Calibración y fuentes de `harness quota`, opcional.

    `tope_semanal` es el tope semanal estimado en tokens PONDERADOS. No lo
    inventa el código: sale de medir `/usage` (interactivo) y va en config
    para poder re-medirlo sin tocar nada más.

    `projects` (#75) son los directorios de sesiones a leer: sin declarar,
    la cuota mira un solo usuario del sistema (`~/.claude/projects`); con la
    lista, suma los declarados, y una fuente ilegible se salta y marca el
    total como piso.
    """

    tope_semanal: Optional[float] = None
    projects: Optional[List[str]] = None


@dataclass
class Run:
    """Cómo se ejecuta el trabajo de este contexto: acá o por SSH en otro usuario."""

    kind: str
    host: str = ""


@dataclass
class Context:
    name: str
    tracker: Tracker
    repos: Repos
    autonomy: str
    budget: BudgetSpec
    run: Run
    vault: Optional[str] = None
    quota: QuotaSpec = field(default_factory=QuotaSpec)


@dataclass
class Config:
    default: str
    contexts: List[Context] = field(default_factory=list)

    @property
    def names(self):
        return [c.name for c in self.contexts]

    def select(self, name=None, todos=False):
        """Los contextos a mirar: uno, todos, o el default. Nunca devuelve vacío."""
        if todos:
            return list(self.contexts)
        if name is None:
            name = self.default
        for c in self.contexts:
            if c.name == name:
                return [c]
        raise ConfigError(
            'no existe el contexto "{}" (hay: {})'.format(name, ", ".join(self.names))
        )


# La config de arranque: lo que seguía `repos.conf`, ya como contexto.
DEFAULT_CONFIG = {
    "default_context": "personal",
    "contexts": {
        "personal": {
            "tracker": {"kind": "github"},
            "repos": {
                "root": "~/Documents/Github",
                "paths": ["agent-harness", "ERP-IphoneUp", "koku", "Health-Link",
                          "entrevestidos", "entrevestidos/*", "herceg-motors"],
            },
            "autonomy": "frontier",
            "budget": {"polarity": "remaining", "provider": "openrouter"},
            "vault": None,
            "run": {"kind": "local"},
        },
    },
}


def _obj(value, donde):
    if not isinstance(value, dict):
        raise ConfigError("{}: se esperaba un objeto JSON, no {}".format(donde, _tipo(value)))
    return value


def _tipo(value):
    return {dict: "un objeto", list: "una lista", str: "un texto",
            bool: "un booleano", type(None): "null"}.get(type(value), "un número")


def _keys(value, permitidas, donde):
    sobran = sorted(set(value) - set(permitidas))
    if sobran:
        raise ConfigError("{}: no entiendo {} (campos: {})".format(
            donde, ", ".join('"{}"'.format(k) for k in sobran), ", ".join(permitidas)))


def _choice(value, opciones, campo, donde):
    if value not in opciones:
        raise ConfigError('{}: {} "{}" no existe (hay: {})'.format(
            donde, campo, value, ", ".join(opciones)))
    return value


def _str(value, campo, donde):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("{}: {} tiene que ser un texto no vacío".format(donde, campo))
    return value


def _num(value, campo, donde):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("{}: {} tiene que ser un número".format(donde, campo))
    return float(value)


def _required(value, campo, donde):
    if campo not in value:
        raise ConfigError('{}: falta "{}"'.format(donde, campo))
    return value[campo]


def _tracker(raw, donde):
    raw = _obj(raw, donde + ' → "tracker"')
    donde = donde + ' → "tracker"'
    _keys(raw, ("kind", "url", "project", "token"), donde)
    kind = _choice(_required(raw, "kind", donde), TRACKERS, "kind", donde)
    tracker = Tracker(kind=kind, url=raw.get("url", ""), project=raw.get("project", ""),
                      token=raw.get("token", ""))
    if kind == "jira":
        _str(tracker.url, '"url"', donde)
        _str(tracker.project, '"project"', donde)
    return tracker


def _repos(raw, donde):
    raw = _obj(raw, donde + ' → "repos"')
    donde = donde + ' → "repos"'
    _keys(raw, ("root", "paths"), donde)
    paths = raw.get("paths", [])
    if not isinstance(paths, list):
        raise ConfigError('{}: "paths" tiene que ser una lista'.format(donde))
    for p in paths:
        _str(p, "cada path", donde)
    return Repos(root=_str(raw.get("root", "~"), '"root"', donde), paths=list(paths))


def _budget(raw, donde):
    raw = _obj(raw, donde + ' → "budget"')
    donde = donde + ' → "budget"'
    _keys(raw, ("polarity", "provider", "total", "used"), donde)
    polarity = _choice(_required(raw, "polarity", donde), POLARITIES, "polarity", donde)
    provider = _choice(_required(raw, "provider", donde), PROVIDERS, "provider", donde)
    spec = BudgetSpec(polarity=polarity, provider=provider)
    if provider == "manual":
        spec.total = _num(_required(raw, "total", donde), '"total"', donde)
        spec.used = _num(raw.get("used", 0.0), '"used"', donde)
    return spec


def _run(raw, donde):
    raw = _obj(raw, donde + ' → "run"')
    donde = donde + ' → "run"'
    _keys(raw, ("kind", "host"), donde)
    kind = _choice(_required(raw, "kind", donde), RUNNERS, "kind", donde)
    host = raw.get("host", "")
    if kind == "ssh":
        _str(host, '"host"', donde)
    return Run(kind=kind, host=host)


def _quota(raw, donde):
    """Opcional: la calibración de `harness quota` (#74) y sus fuentes (#75).
    Sin la sección, sin `tope_semanal`, o `quota` vacío: sin tope — la tabla
    no inventa porcentajes y dice que falta calibrar."""
    if raw is None:
        return QuotaSpec()
    raw = _obj(raw, donde + ' → "quota"')
    donde = donde + ' → "quota"'
    _keys(raw, ("tope_semanal", "projects"), donde)
    tope = None
    if "tope_semanal" in raw:
        tope = _num(raw["tope_semanal"], '"tope_semanal"', donde)
        if tope <= 0:
            raise ConfigError(
                '{}: "tope_semanal" tiene que ser mayor que cero'.format(donde))
    projects = None
    if "projects" in raw:
        raw_p = raw["projects"]
        if not isinstance(raw_p, list) or not raw_p:
            raise ConfigError(
                '{}: "projects" tiene que ser una lista de rutas no vacía'.format(
                    donde))
        for p in raw_p:
            _str(p, "cada ruta de projects", donde)
        projects = list(raw_p)
    return QuotaSpec(tope_semanal=tope, projects=projects)


def _context(name, raw):
    donde = 'contexto "{}"'.format(name)
    raw = _obj(raw, donde)
    _keys(raw, CONTEXT_KEYS, donde)
    vault = raw.get("vault")
    if vault is not None:
        _str(vault, '"vault"', donde)
    return Context(
        name=name,
        tracker=_tracker(_required(raw, "tracker", donde), donde),
        repos=_repos(_required(raw, "repos", donde), donde),
        autonomy=_choice(_required(raw, "autonomy", donde), AUTONOMIES, "autonomy", donde),
        budget=_budget(_required(raw, "budget", donde), donde),
        run=_run(_required(raw, "run", donde), donde),
        vault=vault,
        quota=_quota(raw.get("quota"), donde),
    )


def parse_config(data, donde="config"):
    """Valida la config cruda y devuelve un `Config`. Pura: no toca el disco."""
    data = _obj(data, donde)
    _keys(data, ("default_context", "contexts"), donde)
    contexts = _obj(_required(data, "contexts", donde), donde + ' → "contexts"')
    if not contexts:
        raise ConfigError('{}: no hay ningún contexto declarado en "contexts"'.format(donde))
    parsed = [_context(name, raw) for name, raw in contexts.items()]
    default = data.get("default_context", parsed[0].name)
    _str(default, '"default_context"', donde)
    if default not in [c.name for c in parsed]:
        raise ConfigError('{}: el contexto por defecto "{}" no existe (hay: {})'.format(
            donde, default, ", ".join(c.name for c in parsed)))
    return Config(default=default, contexts=parsed)


def default_config():
    """La config de arranque, ya parseada: lo que seguía `repos.conf`."""
    return parse_config(DEFAULT_CONFIG, donde="config por defecto")
