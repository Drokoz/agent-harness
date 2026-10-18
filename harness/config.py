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

import re
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
                "quota", "topes")


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

    `remotas` (#144) son las fuentes por SSH (`"user@host"`): el usuario del
    9-5 comparte la bolsa semanal de la cuenta. El consumo se lee corriendo
    `scripts/quota_remota.py --json` en el host (sin red en los tests: el
    `run_cmd` se inyecta); si el SSH falla, la fuente cuenta como ilegible
    y el total se marca como piso, como con `projects`.

    `umbral_sesion`, `reserva_diaria`, `costo_ticket` y `hora_limite` son
    la política de `harness run` sobre la cuota real de `/usage`
    (`harness.usage.PoliticaCuota`, docs/harness/quota.md); sin declarar,
    los defaults de ahí.
    """

    tope_semanal: Optional[float] = None
    projects: Optional[List[str]] = None
    remotas: Optional[List[str]] = None
    umbral_sesion: float = 0.90
    reserva_diaria: float = 0.08
    costo_ticket: float = 0.02
    hora_limite: str = "07:00"
    # Cuánto vale el archivo de la statusline (`harness usage`): más viejo
    # que esto, se captura /usage por herdr.
    statusline_max_edad_min: float = 10.0

    def politica(self):
        from harness.usage import PoliticaCuota
        return PoliticaCuota(umbral_sesion=self.umbral_sesion,
                             reserva_diaria=self.reserva_diaria,
                             costo_ticket=self.costo_ticket,
                             hora_limite=self.hora_limite)


@dataclass
class Run:
    """Cómo se ejecuta el trabajo de este contexto: acá o por SSH en otro usuario."""

    kind: str
    host: str = ""


@dataclass
class RunTopes:
    """Los topes de la corrida, declarados en la config (#166).

    `budget_minutos` es el presupuesto de reloj por ticket (minutos de
    agente acumulados en el historial de la noche); `piso` y `max_pasadas`
    son los topes del bucle de `run --loop` (crédito restante y pasadas
    con trabajo).

    Sin declarar, los defaults documentados: 180 min por ticket, piso
    US$3.00, 12 pasadas con trabajo. `harness run`/`run --loop` los lee de
    acá en cada corrida; un flag de CLI puede acotarla más, pero no
    amplificarla por encima de la config.
    """

    budget_minutos: float = 180.0
    piso: float = 3.0
    max_pasadas: int = 12


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
    topes: RunTopes = field(default_factory=RunTopes)


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


def _topes(raw, donde):
    """Opcional: los topes de la corrida (#166). Sin la sección, los
    defaults de `RunTopes`, documentados ahí."""
    if raw is None:
        return RunTopes()
    raw = _obj(raw, donde + ' → "topes"')
    donde = donde + ' → "topes"'
    _keys(raw, ("budget_minutos", "piso", "max_pasadas"), donde)
    kw = {}
    for campo in ("budget_minutos", "piso"):
        if campo in raw:
            v = _num(raw[campo], '"{}"'.format(campo), donde)
            if v < 0:
                raise ConfigError(
                    '{}: "{}" tiene que ser mayor o igual que cero'.format(
                        donde, campo))
            kw[campo] = v
    if "max_pasadas" in raw:
        v = raw["max_pasadas"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ConfigError(
                '{}: "max_pasadas" tiene que ser un entero mayor que '
                'cero'.format(donde))
        kw["max_pasadas"] = v
    return RunTopes(**kw)


def _quota(raw, donde):
    """Opcional: la calibración de `harness quota` (#74) y sus fuentes (#75).
    Sin la sección, sin `tope_semanal`, o `quota` vacío: sin tope — la tabla
    no inventa porcentajes y dice que falta calibrar."""
    if raw is None:
        return QuotaSpec()
    raw = _obj(raw, donde + ' → "quota"')
    donde = donde + ' → "quota"'
    _keys(raw, ("tope_semanal", "projects", "remotas", "umbral_sesion",
                "reserva_diaria", "costo_ticket", "hora_limite",
                "statusline_max_edad_min"), donde)
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
    remotas = None
    if "remotas" in raw:
        raw_r = raw["remotas"]
        if not isinstance(raw_r, list) or not raw_r:
            raise ConfigError(
                '{}: "remotas" tiene que ser una lista no vacía'.format(donde))
        out = []
        for r in raw_r:
            # Dos formas: `{"ssh": "user@host"}` (la del ticket #144) o el
            # string `user@host` directamente.
            if isinstance(r, dict):
                _keys(r, ("ssh",), donde + ' → "remotas"')
                dest = _str(r.get("ssh"), '"ssh"', donde + ' → "remotas"')
            else:
                dest = _str(r, "cada remota", donde + ' → "remotas"')
            if "@" not in dest:
                raise ConfigError(
                    '{}: la remota "{}" tiene que ser user@host'.format(
                        donde, dest))
            if dest != dest.strip():
                raise ConfigError(
                    '{}: la remota "{}" trae espacios de más'.format(donde, dest))
            out.append(dest)
        remotas = out
    politica = {}
    for campo, minimo, abierto in (("umbral_sesion", 0.0, True),
                                   ("reserva_diaria", 0.0, False),
                                   ("costo_ticket", 0.0, True)):
        if campo in raw:
            v = _num(raw[campo], '"{}"'.format(campo), donde)
            if v > 1 or v < minimo or (abierto and v == minimo):
                raise ConfigError('{}: "{}" es una fracción: tiene que estar '
                                  'entre 0 y 1{}'.format(
                                      donde, campo,
                                      " (y mayor que 0)" if abierto else ""))
            politica[campo] = v
    if "hora_limite" in raw:
        h = _str(raw["hora_limite"], '"hora_limite"', donde)
        m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", h)
        if not m:
            raise ConfigError('{}: "hora_limite" tiene que ser HH:MM (24 h, '
                              'America/Santiago), p.ej. "07:00"'.format(donde))
        politica["hora_limite"] = h
    if "statusline_max_edad_min" in raw:
        v = _num(raw["statusline_max_edad_min"], '"statusline_max_edad_min"',
                 donde)
        if v <= 0:
            raise ConfigError('{}: "statusline_max_edad_min" tiene que ser '
                              'mayor que cero (minutos)'.format(donde))
        politica["statusline_max_edad_min"] = v
    return QuotaSpec(tope_semanal=tope, projects=projects, remotas=remotas,
                     **politica)


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
        topes=_topes(raw.get("topes"), donde),
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
