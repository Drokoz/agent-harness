"""Dispatcher local: `harness run` (Fase 2 del PLAN.md).

Toma la frontera desbloqueada y la deja trabajando: un worktree por ticket,
un pane de herdr por agente, con un tope de paralelismo. Cada acción escribe
una línea JSONL —timestamp, contexto, origen, tipo, referencia, cuerpo— en un
log append-only, para que un observador futuro reconstruya qué pasó sin
invalidar nada de lo escrito.

El gate es la red: un repo sin `scripts/gate.sh` no se despacha, y un job cuyo
gate queda rojo en el worktree se abandona. Ningún PR sin gate verde. Y no
existe merge automático: el dispatcher no tiene comando de merge, eso es del
humano.

El módulo es testeable sin herdr ni git: `Dispatcher` recibe el mundo como
callables (`run_cmd`, `credits`, `dormir`) y los tests inyectan falsos. En
producción el adaptador es `harness.adapters.run`.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from harness.costo_pi import clasificar_salida

ORIGEN = "harness"

# Línea de estado de la TUI de pi: "↑78k ↓5.9k R34k CH0.0% $0.056 11.4%/262k"
CTX_RE = re.compile(r"(\d+(?:\.\d+)?)%/\d+k")
# El costo acumulado de esa misma línea: "$0.056".
COSTO_RE = re.compile(r"\$(\d+(?:\.\d+)?)")


# --------------------------------------------------------------------- puros
def una_linea(texto):
    """Los prompts se mandan en una sola línea: con saltos de línea herdr
    reporta éxito pero no entrega nada."""
    return re.sub(r"\s+", " ", texto).strip()


CLAUDE_JSON = Path.home() / ".claude.json"


def confiar_en(worktree, config=None):
    """Marca el worktree como carpeta de confianza para Claude Code.

    Claude pregunta "¿es un proyecto en el que confiás?" la primera vez que
    corre en un directorio, y **un worktree siempre es un directorio nuevo**.
    Sin esto el agente queda `blocked` en el diálogo sin haber escrito una
    línea: el ticket muere antes de empezar.

    No es aflojar nada. El worktree es un checkout del repo del propio dueño,
    creado por su propio harness, con el `.claude/settings.json` que él mismo
    versionó — que es justamente lo que el diálogo enumera. Es responder lo
    que la persona respondería, sin que tenga que estar despierta.
    """
    config = Path(config) if config else CLAUDE_JSON
    try:
        datos = json.loads(config.read_text())
    except (OSError, ValueError):
        datos = {}
    proyectos = datos.setdefault("projects", {})
    entrada = proyectos.setdefault(str(worktree), {})
    entrada["hasTrustDialogAccepted"] = True
    try:
        config.write_text(json.dumps(datos, indent=2))
        return True
    except OSError:
        return False


def nombre_agente(repo, issue):
    """Nombre de agente herdr: [a-z][a-z0-9_-]{0,31}, único entre agentes vivos.

    Dos repos pueden tener el mismo número de issue, así que el nombre lleva
    el repo; sin eso `agent prompt` apuntaría al equivocado.
    """
    base = re.sub(r"[^a-z0-9_-]+", "-", str(repo).lower()).strip("-")
    return ("{}-{}".format(base[:24], issue))[:32]


# Lockfile -> con qué se instala. El orden importa: gana el primero que esté,
# y el lockfile manda sobre package.json porque es el que fija versiones.
INSTALADORES = (
    ("pnpm-lock.yaml", ["pnpm", "install", "--frozen-lockfile"]),
    ("yarn.lock", ["yarn", "install", "--immutable"]),
    ("package-lock.json", ["npm", "ci"]),
)


def comando_de_instalacion(archivos):
    """Con qué instalar las dependencias de este repo, o None si no hay nada.

    Un repo de Go no tiene lockfile de node y no hay que inventarle un comando:
    devolver None es la respuesta correcta, no un fallback a npm.
    """
    presentes = set(archivos)
    for lock, cmd in INSTALADORES:
        if lock in presentes:
            return list(cmd)
    return None


# Un diff que toca estas rutas no es trabajo del ticket, es trabajo de la red:
# un agente trabado puede hacer verde el gate editándolo. Prefijos de nombre
# de config del runner de tests (jest.config.js/ts/mjs, .mocharc.yml, etc.).
CONFIG_RUNNER_TESTS = (
    "jest.config", "vitest.config", ".mocharc", "mocha.opts",
    "karma.conf", "pytest.ini", ".pytest.ini", "phpunit.xml",
)


def ruta_protegida(ruta):
    """Qué protege esta ruta del diff de un PR, o None si no protege nada.

    El gate, los workflows que pueden rodearlo, y la config del runner de
    tests: con ella se cambia qué corren los tests sin que el cambio se vea
    como código en el diff.
    """
    ruta = (ruta or "").strip()
    if not ruta:
        return None
    if ruta == "scripts/gate.sh":
        return "el gate"
    if ruta.startswith(".github/workflows/"):
        return "los workflows"
    base = ruta.rsplit("/", 1)[-1]
    if any(base.startswith(pref) for pref in CONFIG_RUNNER_TESTS):
        return "la config del runner de tests"
    return None


def toca_protegido(cambios):
    """El primer `(ruta, qué_protege)` de la lista de archivos de un diff,
    o None si el diff no toca nada protegido."""
    for ruta in cambios:
        que_protege = ruta_protegida(ruta)
        if que_protege:
            return (ruta, que_protege)
    return None


def copiar_entorno(repo_path, worktree):
    """Lleva al worktree los `.env` que git no versiona.

    Están en .gitignore con razón —traen credenciales—, así que `worktree add`
    no los trae y el gate corre sin configuración. Se copian los que el worktree
    no tenga ya: si el repo versiona un `.env` de ejemplo, ese gana.
    """
    copiados = []
    origen, destino = Path(repo_path), Path(worktree)
    for f in sorted(origen.glob(".env*")):
        if not f.is_file() or f.name.endswith((".template", ".example", ".bak")):
            continue
        objetivo = destino / f.name
        if objetivo.exists():
            continue
        try:
            objetivo.write_bytes(f.read_bytes())
            copiados.append(f.name)
        except OSError:
            pass
    return copiados


def worktree_path(repo_path, issue):
    """El worktree aislado de un ticket. La unidad de aislamiento es el
    worktree (PLAN.md): dos tickets en paralelo no se pisan."""
    repo_path = Path(repo_path)
    return repo_path.parent / ".worktrees" / "{}-ticket-{}".format(repo_path.name, issue)


# Cuántas transcripciones por ticket sobreviven al podado (#66): lo más
# viejo se borra al guardar la nueva, para que no crezcan sin límite.
TRANSCRIPCIONES_POR_TICKET = 5


def worktree_logs_dir(repo_path):
    """Dónde vive la transcripción de cada intento (#66).

    Junto a los worktrees (`worktree_path`), fuera del checkout principal:
    la transcripción es evidencia del dispatcher, no contenido del repo.
    """
    return Path(repo_path).parent / ".worktrees" / "logs"


def transcripcion_path(repo_path, issue, attempt, tipo="ticket"):
    """Un archivo por trabajo e intento (#66): un intento nunca pisa el
    archivo del anterior, ni el próximo intento destruye la evidencia de
    este. `tipo` es "ticket" (lo normal) o "pr" (el mantenedor de
    conflictos, #56). El nombre lleva el repo porque dos repos pueden
    tener el mismo número."""
    nombre = "{}-{}-{}-intento-{}.log".format(
        Path(repo_path).name, tipo, issue, attempt)
    return worktree_logs_dir(repo_path) / nombre


def podar_transcripciones(dir, repo, issue, keep=TRANSCRIPCIONES_POR_TICKET,
                          tipo="ticket"):
    """Sólo las `keep` transcripciones más recientes del trabajo
    sobreviven (#66); los trabajos vecinos y los archivos que no son
    transcripciones no se tocan. Devuelve los nombres borrados."""
    dir = Path(dir)
    if not dir.is_dir():
        return []
    patron = re.compile(
        r"^{}-{}-{}-intento-(\d+)\.log$".format(re.escape(str(repo)), tipo, issue))
    archivos = []
    for f in dir.iterdir():
        m = patron.match(f.name)
        if m and f.is_file():
            archivos.append((int(m.group(1)), f))
    archivos.sort()
    borrados = []
    for _, f in archivos[:-keep]:
        try:
            f.unlink()
        except OSError:
            continue
        borrados.append(f.name)
    return borrados


def prompt_de(issue, gate_tail=None):
    """El trabajo de un agente, en una línea. En inglés: es machine-facing.

    `gate_tail` es la cola del gate rojo del intento anterior (peldaño 2 de
    la escalera, #38): se pega al final y `una_linea` la aplasta junto con
    el resto, así que sigue siendo una sola línea aunque traiga saltos.
    """
    texto = (
        "Read AGENTS.md and CONTEXT.md, then implement GitHub issue {} in this "
        "worktree (branch ticket/{}). Follow this sequence to the end: write the "
        "code; commit every change, so that `git status --porcelain` is empty; "
        "run ./scripts/gate.sh; and only if it exits 0, push the branch and open "
        "the PR with 'Closes {}' in the body. Finishing with uncommitted changes, "
        "or without an open PR, counts as failure and the work is discarded: the "
        "worktree is deleted and only the branch survives. Never merge and never "
        "force-push. Stop once the PR is open.".format(
            "#" + str(issue), issue, "#" + str(issue))
    )
    if gate_tail:
        texto += " The previous attempt's gate failed with: {}".format(gate_tail)
    return una_linea(texto)


# ----------------------------------------------------------------- escalera
# La escalera de reintentos (#38, PLAN.md §"Barato primero, escalada
# asimétrica"): dos peldaños baratos de pi/Qwen -- el segundo con más
# thinking y la cola del gate del intento anterior en el prompt -- y dos
# caros de Claude. El thinking/effort sube ANTES de cambiar de modelo:
# los peldaños 1 y 2 comparten runner y modelo, sólo cambia el thinking.
ESCALERA = (
    {"kind": "pi", "model": "qwen/qwen3.8-27b", "extra_args": ("--thinking", "medium")},
    {"kind": "pi", "model": "qwen/qwen3.8-27b", "extra_args": ("--thinking", "high"),
     "cola_gate": True},
    {"kind": "claude", "model": "sonnet", "extra_args": ("--effort", "medium")},
    {"kind": "claude", "model": "opus", "extra_args": ("--effort", "medium")},
)


def peldano_de(escalados):
    """El peldaño que le toca a un ticket con `escalados` abandonos que ya
    consumieron peldaño (`state.intentos_que_escalan`, que excluye los
    `infra` por #37): un dict de `ESCALERA`, o None si la escalera está
    agotada -- parkear, no despachar de nuevo.

    Puro y sin memoria: `escalados` sale de releer el log cada vez
    (`state.intentos_que_escalan`), así que el peldaño sobrevive reiniciar
    el dispatcher (#38).
    """
    if escalados < 0:
        escalados = 0
    if escalados >= len(ESCALERA):
        return None
    return ESCALERA[escalados]


def peldano_numero(kind, model, extra_args):
    """El número (1-based) del peldaño de `ESCALERA` que corresponde a
    kind/modelo/args, o 0 si no coincide con ninguno: el fallback de la
    corrida (--kind/--model) no es un peldaño, no se le finge número."""
    args = tuple(extra_args or ())
    for i, p in enumerate(ESCALERA):
        if (p["kind"] == kind and p["model"] == model
                and tuple(p.get("extra_args", ())) == args):
            return i + 1
    return 0


def peldano_etiqueta(kind, model, extra_args):
    """El peldaño con que arrancó un ticket (#81): la decisión menos obvia
    del despacho, y tiene que verse sin ir a buscar el JSONL. El número, si
    coincide con `ESCALERA`, seguido de runner, modelo y los args del
    peldaño: `peldaño 1 · pi qwen/qwen3.8-27b --thinking medium`."""
    partes = [kind] + (list(extra_args) if extra_args else [])
    if model:
        partes.insert(1, model)
    etiqueta = " ".join(partes)
    n = peldano_numero(kind, model, extra_args)
    return "peldaño {} · {}".format(n, etiqueta) if n else etiqueta


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------- tanda
# Los runners que corren contra la cuota de Claude, no contra la key de
# OpenRouter: para ellos el tope por presupuesto no existe (#68).
RUNNERS_SIN_CREDITOS = ("claude",)


def decidir_tanda(candidatos, creditos, costo_promedio, margen,
                  max_tickets=None, presupuesto=True):
    """Qué tickets de la tanda entran y cuáles se quedan fuera (#68).

    `candidatos` (Jobs, en el orden de la frontera), `creditos` (lo que
    queda en la key, float | None — None es "no se pudo leer"),
    `costo_promedio` (`state.costo_promedio`, float | None) y `margen`
    (el colchón en dólares: no se arranca un ticket si lo que queda no
    cubre el costo medio más el margen). `max_tickets` acota la tanda a
    mano sin tocar etiquetas. `presupuesto` (True por defecto) corta el
    tope por plata del todo: un contexto que no mide créditos de
    OpenRouter no se dimensiona con plata, y el llamador se lo dice — el
    tope manual `max_tickets` sigue aplicando.

    Devuelve una decisión por candidato, en orden: `{"job", "despachar",
    "motivo"}`. `motivo` es "" en los que entran, y en los que se quedan
    fuera es la razón, para anotar en el log en vez de omitirlos en
    silencio: una tanda que dejó cosas afuera lo dice.

    Reglas, en el orden que se evalúan:

    - Un runner que no gasta créditos (`claude`) nunca cae por
      presupuesto: corre contra cuota, no contra la key, y el motivo lo
      dice aunque todo lo demás quede afuera.
    - Sin costo medio no hay con qué dimensionar: no se dispara nada
      (conservador) y se anota el porqué. Sin crédito leído tampoco: no
      se puede prometer plata que no se puede medir. Ambas cosas quedan
      anotadas, no en silencio.
    - Cada arranque gasta el costo medio (la estimación que se tiene) y
      exige dejar el margen por delante. El crédito no se relee entre
      candidatos: la decisión se toma una vez, con el crédito de ahora.
    - `max_tickets` acota la tanda; si un candidato cae por ambos topes,
      los dos motivos quedan en la razón.

    Puro: sin créditos ni historial inyectados no hay nada que decida.
    """
    decisiones = []
    restante = creditos
    despachados = 0
    for j in candidatos:
        es_claude = (j.kind or "") in RUNNERS_SIN_CREDITOS
        razones = []
        if not es_claude and presupuesto:
            if costo_promedio is None:
                razones.append("costo medio sin historial: sin dimensionar "
                               "no se arranca nada")
            elif creditos is None:
                razones.append("creditos desconocidos: sin medirlos no se "
                               "puede prometer nada")
            elif restante < costo_promedio + margen:
                razones.append("presupuesto: quedan ${:.4f} y se exigen "
                               "${:.4f} (costo medio ${:.4f} + margen "
                               "${:.4f})".format(restante, costo_promedio +
                               margen, costo_promedio, margen))
        if (max_tickets is not None and despachados >= max_tickets):
            # Se evalúa igual que el presupuesto haya fallado: un candidato
            # que cae por los dos topes se anota con los dos motivos.
            razones.append("max-tickets {}".format(max_tickets))
        despachar = not razones
        if despachar:
            despachados += 1
            if not es_claude and presupuesto and costo_promedio is not None:
                restante -= costo_promedio
        if es_claude and despachar:
            # Los que corren contra cuota no gastan del presupuesto, y se
            # dice: no caen por él, punto.
            motivo = "no gasta creditos"
        else:
            motivo = "; ".join(razones)
        decisiones.append({"job": j, "despachar": despachar, "motivo": motivo})
    return decisiones


def _json_field(texto, claves):
    """`texto["a"]["b"]...` sin excepción: None si el JSON no coopera."""
    try:
        data = json.loads(texto)
    except (ValueError, TypeError):
        return None
    for clave in claves:
        if not isinstance(data, dict):
            return None
        data = data.get(clave)
    return data


# --------------------------------------------------------------------- log
class EventLog:
    """Log de eventos append-only, una línea JSONL por acción.

    Los campos fijos: timestamp, contexto, origen, tipo, referencia (ref) y
    cuerpo, más las tres claves del historial (ticket #35): `run_id` (una
    instancia del log es una corrida: dos tandas sobre el mismo archivo se
    distinguen), `ticket` ("repo#issue"; None en las líneas que no son de
    un ticket) y `attempt` (el intento global del ticket, que el
    dispatcher numera leyendo el historial antes de despachar). Las
    líneas `abandono` llevan además `clase` (#37): `infra`, `modelo` o
    `humano`; None en cualquier otro tipo de línea.

    `origen` existe y hoy siempre vale `harness`: es la marca de quién
    escribió la línea, para que un observador futuro no invalide lo escrito.

    `listener` (ticket #81): un callback que recibe cada dict de línea
    apenas se escribe, para la salida en vivo de `run` en stdout. El
    archivo manda: si el listener falla, el evento queda en el JSONL igual.
    """

    def __init__(self, path, contexto, reloj=None, run_id=None, listener=None):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.contexto = contexto
        self.reloj = reloj or _ahora
        self.run_id = run_id or uuid.uuid4().hex[:12]
        # Callback que recibe cada dict de línea apenas se escribe (#81):
        # la salida en vivo de `run` en stdout. El archivo manda: si el
        # listener falla, el evento queda en el JSONL igual.
        self.listener = listener

    def write(self, tipo, ref, cuerpo, ticket=None, attempt=None, clase=None):
        linea = {
            "timestamp": self.reloj(),
            "contexto": self.contexto,
            "origen": ORIGEN,
            "run_id": self.run_id,
            "ticket": ticket,
            "attempt": attempt,
            "tipo": tipo,
            "ref": ref,
            "cuerpo": cuerpo,
            "clase": clase,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(linea, ensure_ascii=False) + "\n")
        if self.listener is not None:
            try:
                self.listener(linea)
            except Exception:
                pass
        return linea


# --------------------------------------------------------------------- bucle
@dataclass
class BucleSpec:
    """Cómo corre el modo `--loop` (#88). Ningún bucle sin límite: corta
    por el piso de presupuesto, el tope de pasadas con trabajo, el tope de
    vueltas con la frontera vacía, o el archivo de freno."""

    piso: float = 3.0            # no arranca otra pasada con menos crédito que esto
    max_pasadas: int = 12        # tope duro de pasadas CON trabajo
    espera: float = 60.0         # segundos entre pasadas con trabajo
    espera_vacia: float = 300.0  # frontera vacía: espera más larga, no es el fin (#101)
    vueltas_vacias_max: int = 60  # tope contra estar mirando para siempre


@dataclass
class Pasada:
    """El resultado de una pasada, visto desde el bucle.

    La frontera se recalcula adentro de la pasada (relee el log de eventos
    y GitHub, no memoria): un ticket que se parkeó deja de entrar y uno que
    se destrabó entra. El bucle sólo decide si repite.

    `log` es el EventLog de ESTA pasada (nuevo run_id): si hay, el bucle
    anota la pasada ahí, para que la corrida y su línea `pasada` se lean de
    la misma corrida; sin `log` se anota en el log que recibió el bucle."""

    trabajo: bool
    detalle: str = ""
    log: "EventLog | None" = None


def bucle(ejecutar_pasada, spec, log, creditos=None, freno=None,
          dormir=None):
    """El modo `--loop` (#88): repite la pasada hasta que la frontera se
    vacíe — y la frontera vacía no es el fin de la noche, es una espera
    (#101): un issue etiquetado a las 4am tiene que encontrar el bucle
    todavía vivo.

    Ningún bucle sin límite. El corte es siempre por una de estas, y queda
    anotado en el log de eventos con su motivo:
    - el archivo de freno aparece — se atiende entre pasadas, nunca en
      medio de una (#88);
    - el crédito restante baja del piso (`creditos()` -> float | None; sin
      medir, el piso no corta, los demás cortes sí);
    - se agota el tope de pasadas CON trabajo (una vacía no gasta turno, #101);
    - nada nuevo en `vueltas_vacias_max` vueltas con la frontera vacía.

    Cada pasada queda en el log con su número (`pasada`, ref `bucle/N`).
    `ejecutar_pasada` -> `Pasada` es una pasada completa; `dormir` es el
    reloj — los tests inyectan uno que no espera de verdad. `log` es el
    EventLog de respaldo (las pasadas que no traen su propio log, y el corte
    si no hubo pasadas). Devuelve el motivo del corte.
    """
    if dormir is None:
        # Sin inyectar: el real. Se resuelve en la llamada, no en el `def`,
        # para que parchear `time.sleep` alcance a esta corrida.
        dormir = time.sleep
    def freno_puesto():
        return bool(freno) and Path(freno).exists()

    def creditos_restantes():
        if creditos is None:
            return None
        try:
            v = creditos()
        except (TypeError, ValueError):
            return None
        return None if v is None else float(v)

    pasadas = 0    # las que encontraron trabajo
    vacias = 0     # las seguidas sin nada en la frontera
    n = 0
    alvo = log
    while True:
        if freno_puesto():
            motivo = "freno puesto ({})".format(freno)
            break
        c = creditos_restantes()
        if c is not None and c < spec.piso:
            motivo = "presupuesto US${:.2f} bajo el piso de US${:.2f}".format(
                c, spec.piso)
            break
        if pasadas >= spec.max_pasadas:
            motivo = "tope de {} pasadas con trabajo".format(spec.max_pasadas)
            break
        if vacias >= spec.vueltas_vacias_max:
            motivo = "nada nuevo en {} vueltas".format(spec.vueltas_vacias_max)
            break
        n += 1
        p = ejecutar_pasada()
        if p.log is not None:
            alvo = p.log
        if p.trabajo:
            pasadas += 1
            vacias = 0
            alvo.write("pasada", "bucle/{}".format(n),
                       "pasada {}: {}".format(n, p.detalle))
            espera = spec.espera
        else:
            vacias += 1
            alvo.write("pasada", "bucle/{}".format(n),
                       "pasada {}: frontera vacia (vuelta {} de {}), "
                       "espero {}s".format(n, vacias, spec.vueltas_vacias_max,
                                           int(spec.espera_vacia)))
            espera = spec.espera_vacia
        dormir(espera)
    alvo.write("bucle", "bucle", "corte: " + motivo)
    return motivo


# --------------------------------------------------------------------- jobs
@dataclass
class Job:
    """Un ticket en la frontera, y su recorrido por el dispatcher."""

    repo: str           # nombre del repo
    repo_path: str      # dónde vive el checkout principal
    slug: str           # owner/repo para gh
    issue: int
    branch: str = ""
    worktree: str = ""
    pane: str = ""
    agent: str = ""
    estado: str = "pendiente"   # pendiente | hecho | abandonado
    motivo: str = ""
    clase_abandono: str = ""   # infra | modelo | humano (#37); "" si no abandono
    # None = no medido todavía. 0.0 = medido y es cero (nunca prendió agente).
    # Un cero por default se confunde con "no costó nada"; ver `_repartir_costo`.
    costo: Optional[float] = None
    attempt: int = 1  # el intento global: lo numera el dispatcher con el historial
    # El peldaño de la escalera (#38), ya resuelto por quien arma el Job
    # (`state.intentos_que_escalan` + `peldano_de`): "" = usa spec.kind/model,
    # para no romper a quien todavía no pasa por la escalera.
    kind: str = ""
    model: str = ""
    extra_args: Tuple[str, ...] = ()          # --thinking/--effort del peldaño
    gate_tail: Optional[str] = None           # cola del gate rojo del intento anterior
    # El primer prompt no llego (#113): no es un veredicto todavía, lo
    # decide `dispatch` al ver si quedan vueltas para recolocarlo.
    prompt_perdido: bool = False


@dataclass
class DispatchSpec:
    """Cómo corre esta tanda. 24 GB: dos agentes, no cinco."""

    contexto: str
    max_parallel: int = 2
    kind: str = "pi"
    model: str = ""
    wait_ms: int = 3_600_000      # espera máxima por agente (una noche)
    watchdog_check_min: int = 5   # cada N minutos se mide si el agente avanza (#41)
    watchdog_kill_min: int = 12   # sin progreso X minutos: matar y abandonar (#41)
    gate_timeout: int = 1800      # segundos para gate.sh dentro del worktree
    install_timeout: int = 900    # segundos para instalar dependencias
    start_retries: int = 4        # reintentos de agent start (pane sin shell)
    start_wait_s: float = 2.0     # espera entre reintentos de arranque
    # La carrera del primer prompt (#113), medida en el log de la noche del
    # 2026-08-26: el `agent prompt --wait` devuelve `agent_prompt_stalled`
    # en ~3s en vez de esperar `verify_wait_s`, así que los 3 reintentos
    # antiguos entraban en una ventana de 16s y, con la máquina cargada,
    # 5 de 9 tickets agotaban la corrida entera. El agente sano se cura en
    # ~3s, así que la ventana se ensancha repartiendo la espera en vez de
    # apretar los intentos:
    verify_retries: int = 5       # reintentos si el primer prompt se pierde (#113)
    verify_wait_s: float = 30.0   # cuánto esperar a que el contexto salga de 0%
    verify_poll_s: float = 10.0   # espera entre intentos del prompt (#113)
    # Si la ventana entera termina sin que el prompt llegue, el job no
    # abandona la corrida: vuelve a la cola de la MISMA corrida (no de la
    # pasada siguiente), hasta estas veces (#113); agotadas, se abandona
    # `infra`.
    prompt_requeues: int = 2
    infra_retries: int = 2        # reintentos en el acto de un abandono infra (#37)
    infra_retry_wait_s: float = 2.0
    # El router (#38, seam para #45): si no es None, se consulta antes de
    # correr un peldaño de Claude -- piso de cuota y calendario. () -> (bool,
    # motivo). La política no vive acá: dispatch.py sólo llama lo que se
    # inyecte; sin nada inyectado, siempre permitido.
    permitir_claude: Optional[Callable[[], Tuple[bool, str]]] = None


class Dispatcher:
    """Lanza y cosecha la frontera. Todo el mundo entra por callables,
    para que los tests no necesiten herdr ni git de verdad.

    El segundo tipo de job —el mantenedor de conflictos (#56)— hereda este
    ciclo de vida y redefine sus costuras: el ref de las líneas (`_ref_de`),
    el prompt (`_prompt`), el worktree (la rama ya existe), la baseline
    previa (`_baseline`) y la condición de terminación (`_done`)."""

    # El tipo de las líneas de principio y fin de la corrida en el log:
    # "corrida" para la frontera de tickets; el mantenedor lo cambia a su
    # propio tipo para que sea distinguible en el log (#56).
    _tipo_corrida = "corrida"

    def __init__(self, spec, log, run_cmd, credits=None, dormir=None,
                 costo_real=None, salida_real=None):
        self.spec = spec
        self.log = log
        self.run_cmd = run_cmd                 # (args, cwd=None, timeout=30) -> (ok, out)
        # (repo, issue, desde) -> float | None: el costo medido de verdad,
        # de las sesiones de pi (#90). Sin inyectar, se cae al reparto del
        # delta de créditos de siempre.
        self.costo_real = costo_real
        # (repo, issue, desde) -> {"stop","error"} | None: cómo cortó pi
        # (#98). Sin inyectar, la clasificación es la del dispatcher sola.
        self.salida_real = salida_real
        # Desde cuándo mirar sesiones: lo fija `dispatch` al arrancar la
        # corrida, para no leer los intentos de anoche.
        self._desde = None
        self.credits = credits or (lambda: None)  # () -> usado (float) | None
        # Sin inyectar, el reloj real; resuelto en la llamada para que
        # parchear `time.sleep` alcance a esta instancia.
        self.dormir = dormir or time.sleep

    def _log(self, job, tipo, ref, cuerpo, clase=None):
        """Evento de un job: además de lo fijo, el ticket ("repo#issue") y
        el intento global, para que el historial sepa qué se intentó y
        cuántas veces. El mantenedor redefine la clave a la del PR
        ("repo#pr<N>", #56)."""
        self.log.write(tipo, ref, cuerpo,
                       ticket="{}#{}".format(job.repo, job.issue),
                       attempt=job.attempt, clase=clase)

    # ----------------------------------------------- costuras por tipo de job
    def _ref_de(self, job):
        """El ref de las líneas del job: "ticket/N" para un ticket, "pr/N"
        para el mantenedor de conflictos (#56)."""
        return "ticket/{}".format(job.issue)

    def _claves_costo(self, job):
        """(repo, issue) con qué leer el costo medido de pi (#90)."""
        return (job.repo, job.issue)

    def _es_hecho(self, job):
        """¿El job terminó bien? "hecho" para un ticket; el mantenedor
        cuenta su "resuelto" como tal (#56)."""
        return job.estado == "hecho"

    def _nombre_agente(self, job):
        return nombre_agente(job.repo, job.issue)

    def _prompt(self, job):
        return prompt_de(job.issue, job.gate_tail)

    def _baseline(self, job, ref):
        """Lo que el job debía preservar antes de despachar. El ticket no
        trae baseline: la trae el mantenedor de conflictos (#56)."""
        return True

    def _done(self, job, ref):
        """La condición de terminación del job, después del gate verde:
        para un ticket, que el agente dejó un PR abierto sobre su rama; el
        mantenedor la redefine a "PR mergeable y trabajo preservado" (#56)."""
        self._pr_abierto(job, ref)

    def _transcripcion_destino(self, job):
        return transcripcion_path(job.repo_path, job.issue, job.attempt)

    def _podar_transcripciones(self, dir, job):
        return podar_transcripciones(dir, Path(job.repo_path).name, job.issue)

    # ----------------------------------------------------------- nivel corrida
    def dispatch(self, jobs):
        """Toda la corrida: los jobs en paralelo (hasta el tope), el costo
        medido con los creditos de antes y después, y las líneas de principio
        y fin. Devuelve los jobs con su estado final.

        Un job que pierde el primer prompt no abandona la corrida: vuelve a
        la cola de ESTA misma corrida (su worktree y pane ya quedaron
        limpios en `_limpiar`) hasta `spec.prompt_requeues` veces — perder
        una pasada entera por la carrera con la TUI de pi cuesta una hora;
        reintentar cuesta un arranque de agente (#113)."""
        ref = "corrida"
        self.log.write(self._tipo_corrida, ref,
                       "inicio: {} ticket(s), max {} en paralelo".format(
                           len(jobs), self.spec.max_parallel))
        antes = self.credits()
        # Desde acá miran las sesiones de pi: un ticket reintentado ya tiene
        # sesiones de anoche en el disco y ese costo no es de esta corrida.
        desde = self._desde = self.log.reloj()
        # La base de toda la corrida se actualiza una sola vez acá, no por
        # ticket (#85): sin fetch, origin/<rama por defecto> puede estar
        # viejo y rebasar contra él da la sensación de estar al día.
        self._fetch_base(jobs)
        resultados = []
        vueltas_prompt = {}
        with cf.ThreadPoolExecutor(max_workers=max(1, self.spec.max_parallel)) as ex:
            cola = list(jobs)
            en_vuelo = set()
            while cola or en_vuelo:
                for j in cola:
                    en_vuelo.add(ex.submit(self.run_job, j))
                cola = []
                # Sin barrera: al primer job que termina se lo procesa de
                # inmediato, y si perdió el prompt se recoloca para ocupar
                # el slot liberado mientras los otros siguen corriendo.
                hecho, _ = cf.wait(en_vuelo, return_when=cf.FIRST_COMPLETED)
                en_vuelo.difference_update(hecho)
                for f in hecho:
                    j = f.result()
                    resultados.append(j)
                    if self._volver_a_cola(j, vueltas_prompt):
                        cola.append(j)
        despues = self.credits()
        self._repartir_costo(resultados, antes, despues, desde)
        if antes is not None and despues is not None:
            self.log.write("costo", ref,
                           "creditos antes {} / despues {} / delta ${:.4f}".format(
                               antes, despues, despues - antes))
        hechas = [j for j in resultados if self._es_hecho(j)]
        abandonadas = [j for j in resultados if j.estado == "abandonado"]
        # Un job que el router aplazó (#38) queda "pendiente": no gastó
        # peldaño, no es un abandono, y se cuenta aparte para no mentir en
        # el resumen de la corrida.
        aplazadas = len(resultados) - len(hechas) - len(abandonadas)
        conocidos = [j.costo for j in resultados if j.costo is not None]
        costo_txt = "${:.4f}".format(sum(conocidos)) if conocidos else "desconocido"
        sin_medir = len(resultados) - len(conocidos)
        if sin_medir:
            costo_txt += " ({} sin medir)".format(sin_medir)
        extra = ", {} aplazado(s) (router)".format(aplazadas) if aplazadas else ""
        self.log.write(self._tipo_corrida, ref,
                       "fin: {} hecho(s), {} abandonado(s){}, costo de jobs {}".format(
                           len(hechas), len(abandonadas), extra, costo_txt))
        return resultados

    # ------------------------------------------------------------- un job
    def run_job(self, job):
        ref = self._ref_de(job)
        permitido, motivo = self._permitir_claude(job)
        if not permitido:
            # No se gasta el peldaño: el job queda "pendiente" (no
            # "abandonado") y la próxima corrida lo vuelve a intentar. Nada
            # se creó todavía -- worktree y pane serían plata tirada si el
            # router ya sabe que este peldaño no corre ahora.
            self._log(job, "peldano", ref,
                      "claude no permitido ahora (router): " + motivo)
            return job
        # El peldaño con que arrancó (#81): antes de cualquier otra acción
        # del ticket, para que la salida en vivo muestre la decisión menos
        # obvia del despacho.
        self._log(job, "peldano", ref,
                  peldano_etiqueta(job.kind or self.spec.kind,
                                    job.model or self.spec.model,
                                    job.extra_args))
        try:
            if not self._baseline(job, ref):
                pass  # ya se abandono con su motivo
            elif not self._worktree_ok(job, ref):
                pass  # ya se abandono con su motivo
            elif not self._pane(job, ref):
                pass  # ya se abandono con su motivo
            elif not self._agente(job, ref):
                self._abandonar(job, ref, "no arranco el agente", clase="infra")
            elif not self._prompt_verificado(job, ref):
                # No se abandona acá (#113): `dispatch` decide si el job
                # vuelve a la cola de esta misma corrida o si se abandona
                # al agotarse las vueltas. El resto de la cadena se salta.
                job.prompt_perdido = True
            elif self._bloqueado(job, ref):
                pass  # ya se abandono con su motivo
            elif not self._arbol_limpio(job, ref):
                pass  # ya se abandono con su motivo
            elif not self._rama_con_commits(job, ref):
                pass  # ya se abandono con su motivo
            elif not self._gate_verde(job, ref):
                pass  # ya se abandono con su motivo
            else:
                self._done(job, ref)
        finally:
            self._limpiar(job, ref)
        return job

    def _volver_a_cola(self, job, vueltas):
        """(#113) La ventana entera de reintentos terminó y el prompt no
        llegó: en vez de abandonar la corrida, el job vuelve a la cola de
        ESTA misma corrida. `_limpiar` ya cerró el pane y quitó el
        worktree (la rama quedó: `_worktree` la reutiliza y rebasa, igual
        que entre pasadas), así que aquí solo se reinicia el estado del
        job. Agotadas `spec.prompt_requeues`, sí abandona `infra`."""
        if not getattr(job, "prompt_perdido", False):
            return False
        clave = id(job)
        n = vueltas.get(clave, 0)
        ref = self._ref_de(job)
        if n >= self.spec.prompt_requeues:
            self._abandonar(job, ref, "primer prompt perdido: el contexto "
                                      "no salio de 0% en {} intentos".format(
                                          self.spec.verify_retries),
                            clase="infra")
            return False
        vueltas[clave] = n + 1
        job.estado = "pendiente"
        job.motivo = ""
        job.clase_abandono = ""
        job.prompt_perdido = False
        job.pane = ""
        job.agent = ""
        self._log(job, "prompt", ref,
                  "prompt perdido: vuelve a la cola de la misma corrida "
                  "(vuelta {} de {})".format(n + 1, self.spec.prompt_requeues))
        return True

    def _permitir_claude(self, job):
        """(permitido, motivo). Sólo se consulta si el peldaño de este job
        es de Claude y hay una política inyectada (`spec.permitir_claude`,
        #38 -- seam para el router de #45): el piso de cuota y el
        calendario todavía no existen acá, dispatch.py no los hardcodea."""
        kind = job.kind or self.spec.kind
        if kind != "claude" or self.spec.permitir_claude is None:
            return True, ""
        return self.spec.permitir_claude()

    # -------------------------------------------------- pasos del ciclo de vida
    def _fetch_base(self, jobs):
        """El dispatcher nunca hacía fetch (#85): `origin/<rama por
        defecto>` podía estar viejo y rebasar contra un origin/main
        desactualizado daba la sensación de estar al día sin estarlo.
        Una vez por corrida y por repo —no por ticket— se trae la rama
        por defecto antes de calcular la base. Un fetch que falla (sin
        red) no voltea la corrida: se sigue con lo que hay y queda
        anotado que la base puede estar vieja."""
        vistos = set()
        for job in jobs:
            if job.repo_path in vistos:
                continue
            vistos.add(job.repo_path)
            rama = self._rama_base(job)
            ok, out = self.run_cmd(["git", "-C", job.repo_path, "fetch",
                                    "origin", rama], timeout=60)
            if ok:
                self.log.write("fetch", "corrida",
                               "origin/{} de {} actualizado".format(rama,
                                                                   job.repo))
            else:
                self.log.write("fetch", "corrida",
                               "fetch origin/{} de {} fallo ({}): la base "
                               "puede estar vieja".format(rama, job.repo,
                                                          out.strip()[-120:]))

    def _worktree(self, job, ref):
        """Worktree aislado por ticket. Si la rama ya existe (un intento
        anterior la dejo), se reutiliza en vez de chocar — pero antes se
        actualiza sobre la base, para que el reintento no arranque viejo
        (#64). La rama nueva nace sobre la base de la corrida, no sobre
        el HEAD del checkout principal (#85): una sola definición de
        base para crear, rebasar y contar commits."""
        job.branch = "ticket/{}".format(job.issue)
        job.worktree = str(worktree_path(job.repo_path, job.issue))
        ref_base, sha_base = self._base(job)
        self._log(job, "base", ref,
                  "base efectiva {} (sha {})".format(ref_base, sha_base[:8]))
        self.run_cmd(["git", "-C", job.repo_path, "worktree", "prune"])
        if Path(job.worktree).exists():
            self.run_cmd(["git", "-C", job.repo_path, "worktree", "remove",
                          "--force", job.worktree])
        args = ["git", "-C", job.repo_path, "worktree", "add"]
        existe, base_vieja = self.run_cmd(["git", "-C", job.repo_path,
                                           "rev-parse", "--verify", "--quiet",
                                           "refs/heads/" + job.branch])
        if existe:
            # La rama ya está (un intento anterior la dejó): se reutiliza.
            args += [job.worktree, job.branch]
        else:
            # `worktree add -b <rama> <path> <base>`. El nombre de la rama va
            # pegado a -b: si va el path, git lo toma como nombre de rama y
            # falla con "is not a valid branch name". El punto de partida es
            # la base de la corrida, para que la rama nueva no herede el
            # atraso de la main local (#85).
            args += ["-b", job.branch, job.worktree, ref_base]
        ok, out = self.run_cmd(args, timeout=120)
        if not ok:
            self._log(job, "worktree", ref, "fallo: " + out.strip()[-200:])
            return False
        if existe:
            # La rama venia del intento anterior: su base puede estar vieja.
            return self._actualizar_sobre_base(job, ref, base_vieja.strip())
        return True

    def _rama_base(self, job):
        """El nombre de la rama por defecto del remote (a qué apunta
        `origin/HEAD`), una consulta local que no toca la red. Sin
        remote resuelto, `main`. Es el nombre, no la base: la base es el
        ref `<rama>` actualizado por el fetch de la corrida (#85)."""
        ok, out = self.run_cmd(["git", "-C", job.repo_path, "symbolic-ref",
                                "refs/remotes/origin/HEAD"])
        prefijo = "refs/remotes/origin/"
        if ok and out.strip().startswith(prefijo):
            return out.strip()[len(prefijo):]
        return "main"

    def _base(self, job):
        """(ref, sha) de la base de todo lo que el job toca (#85):
        `origin/<rama por defecto>`, actualizada por el fetch de la
        corrida. Es la única definición de base: la usan crear la rama
        nueva, rebasar la reutilizada y contar commits — antes cada una
        medía la suya (HEAD del checkout principal, origin/main) y un
        reintento podía quedar adelante o atrás del intento anterior.

        Si el ref del remote no resuelve (sin remote, o fetch que falló
        sin el ref) cae al HEAD del checkout principal: la base vieja,
        posiblemente atrasada — la corrida no muere por eso, y el fetch
        que falló ya quedó anotado en el log."""
        ref = "origin/" + self._rama_base(job)
        ok, out = self.run_cmd(["git", "-C", job.repo_path, "rev-parse",
                                "--verify", "--quiet", ref])
        sha = out.strip().splitlines()[-1].strip() if ok and out.strip() else ""
        if not sha:
            ref = "HEAD"
            ok, out = self.run_cmd(["git", "-C", job.repo_path, "rev-parse",
                                    "--verify", "--quiet", ref])
            sha = (out.strip().splitlines()[-1].strip()
                   if ok and out.strip() else "")
        return ref, sha

    def _actualizar_sobre_base(self, job, ref, base_vieja):
        """El reintento no arranca sobre la base vieja (#64): la rama
        reutilizada se rebasa sobre la base de la corrida (#85) antes de
        despachar, y queda en el log qué base tenía y sobre qué se
        actualizó.

        Si conflictúa, el ticket no se despacha a ciegas: se anota y se
        manda a la cola de mantenimiento (#56), y el rebase se aborta para
        que no quede medio rebase en el worktree."""
        ref_base, _ = self._base(job)
        ok, out = self.run_cmd(["git", "-C", job.worktree, "rebase", ref_base],
                               timeout=120)
        if not ok:
            self.run_cmd(["git", "-C", job.worktree, "rebase", "--abort"],
                         timeout=60)
            motivo = "rebase sobre {} conflictua: {}".format(
                ref_base, out.strip()[-200:])
            self._log(job, "worktree", ref,
                      "rama reutilizada: base vieja {}, {}".format(
                          base_vieja[:8], motivo))
            self._sacar_a_mantenimiento(job, ref, motivo)
            return False
        _, ahora = self.run_cmd(["git", "-C", job.worktree, "rev-parse",
                                 "HEAD"])
        self._log(job, "worktree", ref,
                  "rama reutilizada: base vieja {} actualizada sobre {} "
                  "(ahora {})".format(base_vieja[:8], ref_base,
                                      ahora.strip()[:8]))
        return True

    def _worktree_ok(self, job, ref):
        ok = self._reintentar_infra(job, ref, "no se pudo crear el worktree",
                                    lambda: self._worktree(job, ref))
        if ok:
            self._log(job, "worktree", ref, job.worktree)
        return ok

    def _pane(self, job, ref):
        """Un pane de herdr con el cwd en el worktree; sin robar el foco."""
        return self._reintentar_infra(job, ref, "no se pudo crear el pane de herdr",
                                      lambda: self._pane_intentar(job, ref))

    def _pane_intentar(self, job, ref):
        ok, out = self.run_cmd(["herdr", "pane", "split", "--current",
                                "--direction", "right", "--cwd", job.worktree,
                                "--no-focus"], timeout=60)
        pane = _json_field(out, ("result", "pane", "pane_id"))
        if ok and pane:
            job.pane = pane
            self._log(job, "pane", ref, pane)
            return True
        self._log(job, "pane", ref, "fallo: " + out.strip()[:200])
        return False

    def _agente(self, job, ref):
        job.agent = self._nombre_agente(job)
        # El peldaño de la escalera (#38) resuelve kind/modelo por ticket;
        # sin peldaño asignado (job.kind == ""), se usa el de la corrida
        # entera -- el comportamiento de antes de la escalera.
        kind = job.kind or self.spec.kind
        modelo = job.model or self.spec.model
        args = ["herdr", "agent", "start", job.agent, "--kind", kind,
                "--pane", job.pane, "--timeout", "120000"]
        # El modelo va primero y el resto del peldaño (--thinking/--effort)
        # después: son del mismo peldaño, van juntos.
        extra = (["--model", modelo] if modelo else []) + list(job.extra_args)
        if extra:
            args += ["--"] + extra
        # El diálogo de confianza de Claude tapia el arranque en cada worktree.
        if kind == "claude":
            confiar_en(job.worktree)
        # `pane split` vuelve antes de que el shell del pane esté listo, así que
        # el primer `agent start` puede rebotar con agent_pane_busy. Es una
        # carrera, no un veredicto: se reintenta.
        for intento in range(1, self.spec.start_retries + 1):
            ok, out = self.run_cmd(args, timeout=150)
            if ok:
                self._log(job, "agente", ref,
                          "{} (kind {}, pane {})".format(job.agent, kind,
                                                        job.pane))
                return True
            if _json_field(out, ("error", "code")) != "agent_pane_busy":
                break
            self._log(job, "agente", ref,
                      "pane sin shell todavia, intento {}".format(intento))
            self.dormir(self.spec.start_wait_s)
        self._log(job, "agente", ref, "fallo: " + out.strip()[:200])
        return False

    def _prompt_verificado(self, job, ref):
        """Manda el prompt y verifica de verdad que llego: el primero después
        de `agent start` se pierde (carrera con la TUI de pi) y herdr reporta
        éxito igual. La evidencia de que llego es que el contexto del agente
        sube de 0%; si no, se reintenta. Un trabajo que nunca arranco no puede
        contarse como lanzado."""
        prompt = self._prompt(job)
        for intento in range(1, self.spec.verify_retries + 1):
            # `--wait --until working` es lo que hace que herdr entregue el
            # prompt y confirme que llegó. Sin `--wait`, `--timeout` es un
            # error de uso y no se manda nada.
            ok, out = self.run_cmd(
                ["herdr", "agent", "prompt", job.agent, prompt,
                 "--wait", "--until", "working",
                 "--timeout", str(int(self.spec.verify_wait_s * 1000))],
                timeout=int(self.spec.verify_wait_s) + 60)
            estado = _json_field(out, ("result", "agent", "agent_status"))
            self._log(job, "prompt", ref, "intento {}: {}".format(
                intento, estado or _json_field(out, ("error", "code")) or "sin respuesta"))
            if ok and estado in ("working", "blocked"):
                return True
            if self._llego(job):
                return True
            self.dormir(self.spec.verify_poll_s)
        return False

    def _llego(self, job):
        """¿El contexto del agente salio de 0%? Se lee de la línea de estado
        de la TUI, en la salida reciente del pane."""
        if not job.pane:
            return False
        ok, out = self.run_cmd(["herdr", "pane", "read", job.pane,
                                "--source", "recent", "--lines", "12"], timeout=30)
        if ok and any(float(m.group(1)) > 0 for m in CTX_RE.finditer(out)):
            return True
        # El contexto en porcentaje lo imprime la TUI de pi y nadie más. Para
        # cualquier otro agente la evidencia es lo que herdr ya sabe: que dejó
        # de estar quieto. `blocked` también cuenta como llegado — procesó algo
        # y se trabó, que es un problema distinto y lo detecta _bloqueado.
        if not job.agent:
            return False
        ok, out = self.run_cmd(["herdr", "agent", "get", job.agent], timeout=30)
        if not ok:
            return False
        return _json_field(out, ("result", "agent", "agent_status")) in (
            "working", "blocked")

    def _bloqueado(self, job, ref):
        """Espera a que el agente se asiente. `blocked` = abrió un prompt de
        aprobación o una pregunta: se abandona (humano), se anota, y el
        dispatcher sigue con el siguiente; no se queda esperando a un
        humano que no va a estar.

        Watchdog de progreso (#41): esperar una hora a un agente en bucle
        gasta la noche sin una línea escrita. La espera se corta cada
        `watchdog_check_min` minutos y en cada corte se mide si hay
        progreso — un commit nuevo en la rama, el contexto que sube o el
        costo que sube. Sin progreso durante `watchdog_kill_min` minutos,
        se corta al agente y se abandona `modelo`: reintentarlo no va a
        cambiar nada. Un agente corriendo el gate es la excepción
        explícita: el gate de un worktree limpio puede pasar de los 12
        minutos y su espera es legítima, así que el reloj se le reinicia.

        El timeout del `agent wait` ya no es un veredicto: es el tick del
        watchdog. El veredicto de antes (timeout de espera, `modelo`) se
        mantiene al agotarse el presupuesto total `wait_ms`: el agente
        avanzaba y nunca se asientó. Y si el comando de herdr en sí no
        contesta con nada reconocible (ni un estado, ni "timeout"), eso es
        infra -- un problema del propio herdr, no del agente -- y se
        reintenta en el acto (#37)."""
        check_ms = int(self.spec.watchdog_check_min * 60 * 1000)
        rondas = max(1, int(self.spec.wait_ms // check_ms))
        senal = self._senal(job)
        sin_progreso = 0
        for _ in range(rondas):
            estado, error, out = self._wait_corto(job, check_ms)
            if estado == "blocked":
                self._abandonar(job, ref,
                                "agente bloqueado (aprobacion o pregunta pendiente)",
                                clase="humano")
                return True
            if estado:
                self._log(job, "agente", ref, "se asieto: {}".format(estado))
                return False
            if error != "timeout":
                self._abandonar(job, ref,
                                "fallo al esperar al agente: " + out.strip()[:200],
                                clase="infra")
                return True
            senal, sin_progreso, matado = self._watchdog(job, ref, senal,
                                                         sin_progreso)
            if matado:
                return True
        self._abandonar(job, ref,
                        "timeout de espera ({} ms)".format(self.spec.wait_ms),
                        clase="modelo")
        return True

    def _wait_corto(self, job, check_ms):
        """Un `agent wait` acotado a un chequeo del watchdog (#41).

        Devuelve `(estado, error, out)`: estado es el agente (`idle`,
        `blocked`, `done`...) o None; error es el código de herdr o None.
        None y None = herdr no dijo nada reconocible, que es una falla de
        infraestructura, no un veredicto sobre el agente (se reintenta en
        el acto, #37)."""
        args = ["herdr", "agent", "wait", job.agent,
                # `done` va en la lista porque es donde se asienta claude
                # cuando termina. Sin él, un agente que ya dejó el PR
                # abierto no matchea ningún estado y el wait cuelga hasta
                # el timeout.
                "--until", "idle", "--until", "blocked", "--until", "done",
                "--timeout", str(check_ms)]
        out = ""
        for intento in range(self.spec.infra_retries + 1):
            ok, out = self.run_cmd(args, timeout=check_ms // 1000 + 120)
            estado = _json_field(out, ("result", "agent", "agent_status"))
            if estado:
                return estado, None, out
            error = _json_field(out, ("error", "code"))
            if error == "timeout":
                return None, error, out
            if intento < self.spec.infra_retries:
                self.dormir(self.spec.infra_retry_wait_s)
        return None, error, out

    def _senal(self, job):
        """Lo que el agente ha producido, para el watchdog de progreso
        (#41): `(HEAD de la rama, contexto %, costo, corriendo el gate)`.

        Que suba cualquiera de los tres primeros cuenta como progreso. El
        gate se lleva aparte porque su espera es legítima y puede pasar
        el umbral: un worktree limpio instala y corre el gate entero, y
        mientras tanto no hay commits, ni contexto, ni costo que suban."""
        head = ""
        if job.worktree:
            ok, out = self.run_cmd(["git", "-C", job.worktree, "rev-parse",
                                    "HEAD"], timeout=30)
            if ok and out.strip():
                head = out.strip().splitlines()[-1].strip()
        ctx = costo = 0.0
        en_gate = False
        if job.pane:
            ok, out = self.run_cmd(["herdr", "pane", "read", job.pane,
                                    "--source", "recent", "--lines", "40"],
                                   timeout=30)
            if ok:
                m = CTX_RE.findall(out)
                if m:
                    ctx = float(m[-1])
                c = COSTO_RE.findall(out)
                if c:
                    costo = float(c[-1])
                en_gate = "gate.sh" in out
        return (head, ctx, costo, en_gate)

    def _watchdog(self, job, ref, senal, sin_progreso):
        """Un tick del watchdog (#41): ¿el agente avanzó desde el corte
        anterior? Devuelve `(senal, sin_progreso, matado)`. Cada corte sin
        progreso anota sus minutos en el log, para calibrar el umbral con
        datos después."""
        nueva = self._senal(job)
        if nueva != senal:
            return nueva, 0, False
        sin_progreso += self.spec.watchdog_check_min
        if nueva[3]:
            self._log(job, "watchdog", ref,
                      "sin progreso {} min, pero el agente esta corriendo el "
                      "gate: espera legitima, se lo deja".format(sin_progreso))
            return nueva, 0, False
        self._log(job, "watchdog", ref,
                  "sin progreso {} min (commits, contexto y costo quietos)"
                  .format(sin_progreso))
        if sin_progreso >= self.spec.watchdog_kill_min:
            self._matar(job, ref, sin_progreso)
            return nueva, sin_progreso, True
        return nueva, sin_progreso, False

    def _matar(self, job, ref, sin_progreso):
        """El watchdog decide (#41): sin progreso pasado el umbral, y sin
        gate en marcha. Se corta al agente (ctrl+c) y se abandona `modelo`:
        un bucle no se arregla esperándolo, y al peldaño siguiente de la
        escalera se le da otra chance. El pane y el worktree los limpia
        `_limpiar`; la rama queda con lo que haya."""
        self.run_cmd(["herdr", "agent", "send-keys", job.agent, "ctrl+c"],
                     timeout=60)
        self._log(job, "watchdog", ref,
                  "sin progreso {} min: agente matado".format(sin_progreso))
        self._abandonar(job, ref,
                        "watchdog: sin progreso durante {} min (sin commits "
                        "nuevos, contexto quieto, costo quieto)"
                        .format(sin_progreso),
                        clase="modelo")

    def _preparar(self, job, ref):
        """Deja el worktree en condiciones de correr el gate.

        `git worktree add` trae lo versionado y nada más. Sin esto el gate se
        pone rojo por falta de dependencias y el ticket se abandona por una
        razón que no tiene nada que ver con su código.
        """
        wt = Path(job.worktree)
        # Instalar PRIMERO, con el worktree todavía sin .env. El de
        # ENTREVESTIDOS-BACK fija NODE_ENV=production, y con eso puesto yarn
        # omite las devDependencies —donde vive el runner de tests— y el gate se
        # queda sin poder correrlos. El worktree de un agente siempre es un
        # entorno de desarrollo, diga lo que diga el .env de producción.
        cmd = comando_de_instalacion([f.name for f in wt.iterdir()]
                                     if wt.exists() else [])
        if cmd and not (wt / "node_modules").exists():
            ok, out = self.run_cmd(cmd, cwd=job.worktree,
                                   timeout=self.spec.install_timeout)
            self._log(job, "worktree", ref, "instalar con {}: {}".format(
                cmd[0], "ok" if ok else "fallo: " + out.strip()[-160:]))

        copiados = copiar_entorno(job.repo_path, job.worktree)
        if copiados:
            self._log(job, "worktree", ref, "entorno: " + ", ".join(copiados))

    def _arbol_limpio(self, job, ref):
        """Antes de correr el gate: el worktree tiene que estar limpio.

        El PR contiene la rama, no el árbol de trabajo: cambios sin commitear
        no entran al PR, y un gate verde sobre ellos sería un gate mentiroso
        adentro del propio dispatcher. Sucio = abandono (modelo), sin correr
        el gate. Que el propio `git status` falle es otra cosa -- infra, no
        el trabajo del agente -- y se reintenta en el acto (#37).
        """
        salida = []

        def intentar():
            ok, out = self.run_cmd(["git", "-C", job.worktree, "status", "--porcelain"])
            salida[:] = [ok, out]
            return ok

        if not self._reintentar_infra(
                job, ref,
                "no se pudo verificar el arbol (git status fallo): sin gate, sin PR",
                intentar):
            return False
        _, out = salida
        if not out.strip():
            return True
        self._log(job, "gate", ref, "arbol sucio: " + out.strip()[:200])
        self._abandonar(job, ref, "arbol sucio en el worktree: hay cambios "
                                   "sin commitear que no entran al PR; sin "
                                   "medicion, sin PR", clase="modelo")
        return False

    def _rama_con_commits(self, job, ref):
        """Antes de correr el gate: la rama tiene que tener al menos un
        commit por delante de la base de la corrida (#85: la misma base
        que usaron crear la rama nueva y rebasar la reutilizada).

        Contar cero significa que la rama no trae trabajo, y el
        gate correría sobre el código de la base sin cambios -- el mismo
        verde que uno que trabajó bien, escrito en el log como un logro
        (#52 lo pasó así, y #45 otra vez). Verde significa "no rompe
        nada", no "hizo algo". Cero commits = abandono (modelo), con
        motivo propio: sin gate, sin `gate: verde` y sin PR. Que el propio
        `git rev-list` falle es otra cosa -- infra, no el trabajo del
        agente -- y se reintenta en el acto (#37).
        """
        salida = []

        def intentar():
            _, base = self._base(job)
            if not base:
                return False
            ok, out = self.run_cmd(["git", "-C", job.worktree, "rev-list",
                                    "--count", base + "..HEAD"])
            salida[:] = [out]
            return ok

        if not self._reintentar_infra(
                job, ref,
                "no se pudo verificar si la rama trae commits (git fallo): "
                "sin medicion, sin PR",
                intentar):
            return False
        (out,) = salida
        n = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if n.isdigit() and int(n) > 0:
            return True
        self._log(job, "gate", ref,
                  "la rama no tiene commits por delante de la base")
        self._abandonar(job, ref, "la rama no tiene commits por delante de "
                                   "su base: no hay trabajo que medir; sin "
                                   "medicion, sin PR", clase="modelo")
        return False

    def _gate_verde(self, job, ref):
        """El gate se corre en el worktree, por el dispatcher: no confía en
        la palabra del agente. Rojo = abandono, y no hay PR."""
        self._preparar(job, ref)
        ok, out = self.run_cmd(["./scripts/gate.sh"], cwd=job.worktree,
                               timeout=self.spec.gate_timeout)
        if ok:
            self._log(job, "gate", ref, "verde")
            return True
        self._log(job, "gate", ref, "rojo: " + out.strip()[-200:])
        self._abandonar(job, ref, "gate rojo en el worktree: sin PR",
                        clase="modelo", firme=True)
        return False

    def _pr_abierto(self, job, ref):
        """El agente debió dejar un PR abierto sobre su rama, y antes de
        declarar hecho se verifica que ese PR sea lo que el gate midió:
        apunta al mismo HEAD y no toca rutas protegidas."""
        num = None
        if job.slug:
            ok, out = self.run_cmd(["gh", "pr", "list", "--state", "open",
                                    "--limit", "20", "--json",
                                    "number,headRefName", "-R", job.slug])
            try:
                prs = json.loads(out) if ok and out else []
            except ValueError:
                prs = []
            for pr in prs:
                if pr.get("headRefName") == job.branch:
                    num = pr.get("number")
                    break
        if num is None:
            self._abandonar(job, ref, "el agente termino sin PR abierto", clase="modelo")
            return
        if not self._pr_mide_el_head(job, ref, num):
            return
        if not self._pr_no_toca_protegido(job, ref, num):
            return
        job.estado = "hecho"
        self._log(job, "pr", ref, "PR #{} abierto (gate verde)".format(num))

    def _pr_mide_el_head(self, job, ref, num):
        """El gate midió el HEAD de la rama. Si el PR apunta a otro commit,
        el verde no aplica a lo que va a mergear."""
        ok, out = self.run_cmd(["git", "-C", job.worktree, "rev-parse", "HEAD"])
        head = out.strip().splitlines()[-1].strip() if ok and out.strip() else ""
        ok, out = self.run_cmd(["gh", "pr", "view", str(num), "--json", "headRefOid",
                                "-R", job.slug])
        oid = _json_field(out, ("headRefOid",))
        if head and oid and head == oid:
            return True
        self._log(job, "gate", ref,
                       "head del PR {} != HEAD de la rama {}".format(oid, head))
        self._abandonar(job, ref, "el head del PR no coincide con el HEAD de la "
                                   "rama: el gate midio algo que no va en el PR",
                        clase="modelo")
        return False

    def _pr_no_toca_protegido(self, job, ref, num):
        """Un diff que toca el gate, los workflows o la config del runner de
        tests no se acepta: queda anotado y el ticket se marca para humano
        (la etiqueta, no la clase: es el agente el que tocó lo protegido).
        No poder leer los archivos del PR es otra cosa -- un `gh` que no
        contesta -- y se reintenta en el acto (#37)."""
        salida = []

        def intentar():
            ok, out = self.run_cmd(["gh", "pr", "view", str(num), "--json", "files",
                                    "-R", job.slug])
            salida[:] = [ok, out]
            return ok and bool(out)

        if not self._reintentar_infra(
                job, ref, "no se pudieron leer los archivos del PR", intentar):
            return False
        _, out = salida
        try:
            files = json.loads(out)
        except ValueError:
            files = []
        rutas = [f.get("path") for f in files if isinstance(f, dict)]
        tocada = toca_protegido(rutas)
        if not tocada:
            return True
        ruta, que_protege = tocada
        self._log(job, "gate", ref, "el PR toca {} ({})".format(que_protege, ruta))
        self._abandonar(job, ref, "el PR toca {} ({}): no se acepta; el ticket "
                                   "queda para humano".format(que_protege, ruta),
                        clase="modelo")
        self._marcar_para_humano(job, ref)
        return False

    def _marcar_para_humano(self, job, ref):
        """La etiqueta canónica del triage: `ready-for-human`, sacando
        `ready-for-agent` en la misma llamada. La usan tanto un PR que toca
        rutas protegidas como `parkear` (escalera agotada, #38).

        Sin esto el ticket sigue en la frontera (`AGENT_LABEL` en
        `snapshot.py`): la próxima corrida lo vuelve a despachar, el agente
        vuelve a tocar lo mismo, y se rechaza de nuevo — para siempre,
        gastando un slot y plata cada vez.
        """
        if not job.slug:
            return
        self.run_cmd(["gh", "issue", "edit", str(job.issue),
                      "--add-label", "ready-for-human",
                      "--remove-label", "ready-for-agent",
                      "-R", job.slug])

    def _para_humano(self, job, ref, comentario, log_cuerpo):
        """Fuera de la frontera de la flota, con el motivo en el issue
        (#38, #64): `ready-for-human` lo saca de la cola de agentes, y el
        comentario evita que un humano (o el mantenedor de conflictos, #56)
        tenga que releer el log de eventos para entender por qué."""
        self._marcar_para_humano(job, ref)
        if job.slug:
            self.run_cmd(["gh", "issue", "comment", str(job.issue),
                          "--body", comentario, "-R", job.slug])
        self._log(job, "park", ref, log_cuerpo)

    def parkear(self, job, ref, motivos):
        """Agota la escalera de reintentos (#38): saca `ready-for-agent`,
        pone `ready-for-human` -- la etiqueta canónica de "para humano"
        (`docs/agents/triage-labels.md`; no se inventa `needs-human`) -- y
        comenta el motivo de cada intento, para que un humano no tenga que
        releer el log de eventos para entender por qué.

        Un ticket parkeado no vuelve a la frontera (`state.estado_frontier`,
        #43 -- `PARKEADOS` incluye `ready-for-human`): la etiqueta ya lo saca,
        no hace falta nada más acá.
        """
        comentario = "Escalera de reintentos agotada tras {} intento(s):\n".format(
            len(motivos)) + "\n".join(
                "{}. {}".format(i, m) for i, m in enumerate(motivos, 1))
        self._para_humano(job, ref, comentario,
                          "escalera agotada tras {} intento(s)".format(
                              len(motivos)))

    def _sacar_a_mantenimiento(self, job, ref, motivo):
        """Un ticket que no se despacha a ciegas (#64): a la cola de
        mantenimiento (#56), que hoy es la frontera de humanos — la etiqueta
        `ready-for-human` lo saca de la flota para siempre y el comentario
        dice qué conflicto hay que resolver."""
        comentario = ("Cola de mantenimiento (#56): la rama `ticket/{}` no "
                      "se puede actualizar sobre la base y no se despacha a "
                      "ciegas. {}. La rama queda como quedó, con sus commits; "
                      "hay que resolverlo a mano sobre la base."
                      ).format(job.issue, motivo)
        self._para_humano(job, ref, comentario,
                          "cola de mantenimiento (#56): " + motivo)
        self._abandonar(job, ref, "no despachable a ciegas: " + motivo,
                        clase="humano")

    # --------------------------------------------------------------- limpieza
    def _rescate(self, job, ref):
        """Lo que el abandono no debe borrar (#66), en el orden que hace
        posible cada cosa: la transcripción con el pane todavía abierto, el
        commit WIP con el worktree todavía en pie. Un job que terminó bien
        no pasa por acá: no deja WIP ni transcripción."""
        self._transcripcion(job, ref)
        self._wip_abandono(job, ref)

    def _transcripcion(self, job, ref):
        """La salida reciente del agente, en un archivo por ticket e intento
        (#66): sin ella no hay forma de saber por qué se detuvo —la misma
        lección que `adapters.run` cuando descartaba stderr. El log de
        eventos apunta al archivo, para ir del abandono a la transcripción.
        """
        if not (job.agent or job.pane):
            return
        destino = self._transcripcion_destino(job)
        ok, out = ((False, "") if not job.agent
                   else self.run_cmd(["herdr", "agent", "read", job.agent,
                                      "--source", "recent", "--lines", "200"],
                                     timeout=30))
        if not ok and job.pane:
            # El agente no contestó: el pane sí queda abierto hasta acá, y
            # lo que muestra es también evidencia.
            ok, out = self.run_cmd(["herdr", "pane", "read", job.pane,
                                    "--source", "recent", "--lines", "200"],
                                   timeout=30)
        if not ok:
            self._log(job, "transcripcion", ref,
                      "no se pudo leer la salida del agente")
            return
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(out, encoding="utf-8")
        except OSError:
            self._log(job, "transcripcion", ref,
                      "no se pudo escribir " + str(destino))
            return
        self._podar_transcripciones(destino.parent, job)
        self._log(job, "transcripcion", ref, str(destino))

    def _wip_abandono(self, job, ref):
        """Los cambios sin commitear no entraron al PR ni al gate (#36),
        pero borrarlos con el worktree es borrar el trabajo (#66): viajan a
        la rama como commit WIP, y el próximo intento los encuentra cuando
        reutiliza la rama. Arbol limpio = nada que rescatar."""
        if not job.worktree:
            return
        ok, out = self.run_cmd(["git", "-C", job.worktree, "status",
                                "--porcelain"])
        if not ok or not out.strip():
            return
        msg = ("WIP: trabajo sin commitear que dejo el abandono de {} "
               "(intento {})".format(job.branch or "ticket/{}".format(job.issue),
                                     job.attempt))
        ok, out = self.run_cmd(["git", "-C", job.worktree, "add", "-A"],
                               timeout=60)
        if not ok:
            self._log(job, "wip", ref,
                      "no se pudieron agregar los cambios: " + out.strip()[-160:])
            return
        ok, out = self.run_cmd(["git", "-C", job.worktree, "commit", "-m", msg],
                               timeout=60)
        if not ok:
            self._log(job, "wip", ref, "el commit WIP fallo: " + out.strip()[-160:])
            return
        self._log(job, "wip", ref, msg)

    def _repartir_costo(self, jobs, antes, despues, desde=None):
        """El costo por job, después de que la corrida entera terminó.

        La fuente primaria es la sesión de pi (#90): `message.usage.cost.total`
        sumado por sesión es el número exacto, no una estimación, y sigue ahí
        aunque la corrida no llegue a leer el crédito final. `desde` recorta a
        los intentos de esta corrida —un ticket reintentado ya tiene sesiones
        de anoche en el disco.

        El reparto del delta de créditos queda de plan B, para los jobs que pi
        no midió (un peldaño de Claude, una sesión que no quedó en el disco).

        Leer el costo de la línea de estado de la TUI (pane por pane) no es
        confiable: esa línea es de la TUI de pi y ni siquiera ahí se puede
        garantizar que siga visible cuando el job ya terminó (ver #53 — una
        corrida real midió $1.7085 de delta de créditos con los cuatro jobs
        en $0.0000). En vez de scrapear la pantalla, se reparte el delta de
        créditos de la corrida (medido una sola vez, al principio y al final)
        entre los jobs que sí prendieron un pane —los únicos que pudieron
        haber gastado algo.

        Un job que nunca llegó a tener pane cuesta $0.0000 de verdad: no hay
        nada que estimar. Un job que sí prendió pane pero no hay créditos
        para medir el delta (falta la key, la API no contestó) queda con
        costo desconocido —`None`, no 0.0— porque un cero ahí sería
        indistinguible de un gasto real de cero."""
        medibles = [j for j in jobs if j.pane]
        for j in jobs:
            if not j.pane:
                j.costo = 0.0
        if not medibles:
            return

        # 1) Lo que pi midió de verdad. No es una estimación y no depende de
        #    que la corrida llegue a leer el crédito final.
        medidos = []
        for j in medibles:
            c = self._costo_de_pi(j, desde)
            if c is None:
                continue
            j.costo = c
            medidos.append(j)
            self._log(j, "costo", self._ref_de(j),
                      "${:.4f} (sesion de pi)".format(c))

        # 2) El resto se reparte lo que sobra del delta. La resta puede dar
        #    negativa (el delta se contamina con cualquier otra cosa que use
        #    la misma key), y un costo negativo miente peor que un cero.
        resto = [j for j in medibles if j not in medidos]
        if not resto:
            return
        if antes is None or despues is None:
            for j in resto:
                self._log(j, "costo", self._ref_de(j), "desconocido")
            return
        sobra = max(0.0, (despues - antes) - sum(j.costo for j in medidos))
        share = sobra / len(resto)
        for j in resto:
            j.costo = share
            # "estimado" no es decorativo (#65): un promedio y una medición no
            # pueden leerse igual, porque el router decide con la diferencia.
            self._log(j, "costo", self._ref_de(j),
                      "${:.4f} (estimado: reparto del delta)".format(share))

    def _costo_de_pi(self, job, desde):
        """El costo real del ticket, si hay con qué medirlo. Nunca puede tumbar
        una corrida: leer el disco es mejor esfuerzo."""
        if not self.costo_real:
            return None
        try:
            repo, issue = self._claves_costo(job)
            return self.costo_real(repo, issue, desde)
        except Exception:
            return None

    def _limpiar(self, job, ref):
        """Cierra el pane y quita el worktree: un pane que queda abierto para
        siempre hace inutilizable la pantalla después de unas cuantas tandas.
        La rama queda: si el agente dejo commits, son recuperables, y si el
        ticket vuelve a la frontera el próximo intento la reutiliza.

        Un abandono rescata antes (#66): la transcripción necesita el pane
        abierto y el commit WIP necesita el worktree en pie, así que nada de
        eso se cierra ni se borra hasta después."""
        if job.estado == "abandonado":
            self._rescate(job, ref)
        cerrado = ""
        if job.pane:
            self.run_cmd(["herdr", "pane", "close", job.pane], timeout=60)
            cerrado = "pane {} cerrado; ".format(job.pane)
        if job.worktree:
            self.run_cmd(["git", "-C", job.repo_path, "worktree", "remove",
                          "--force", job.worktree], timeout=120)
        self._log(job, "limpieza", ref,
                  "{}worktree removido, rama {} quedo".format(cerrado, job.branch))

    def _abandonar(self, job, ref, motivo, clase="modelo", firme=False):
        """Marca el job como abandonado, con su clase (#37): `infra`
        (worktree, pane, arranque del agente, prompt perdido, timeout de
        red), `modelo` (gate rojo, sin PR, sin progreso) o `humano`
        (agente bloqueado en una aprobación o pregunta). Una causa que
        ningún llamador clasifica cae en `modelo`: el default conservador
        es el que gasta peldaño de escalada, no el que reintenta gratis.
        """
        # El dispatcher ve el síntoma; pi guarda la causa (#98). Su veredicto
        # sólo pisa el default `modelo`: un `infra` o un `humano` que el
        # llamador ya sabe no se discute, y encima esos casos ni siquiera
        # llegan a tener sesión que consultar.
        sacar = False
        # `firme`: hay evidencia positiva de trabajo malo (un gate en rojo) y
        # eso no lo perdona ningún error de pi. La noche del 2026-08-26 un
        # "gate rojo en el worktree" salió `infra` porque la sesión además
        # había tenido un `Connection error.` en el medio.
        if clase == "modelo" and not firme:
            real, detalle = self._salida_de_pi(job)
            if real:
                clase = real
                motivo = "{} (pi: {})".format(motivo, detalle)
                # Reintentar contra una pared es gastar de gusto. Sólo acá:
                # los `humano` que el llamador ya sabía (un rebase que
                # conflictúa) se rutean ellos mismos, y marcar dos veces
                # es una llamada de más a la API por cada abandono.
                sacar = clase == "humano"
        job.estado = "abandonado"
        job.motivo = motivo
        job.clase_abandono = clase
        self._log(job, "abandono", ref, motivo, clase=clase)
        if sacar:
            self._marcar_para_humano(job, ref)

    def _salida_de_pi(self, job):
        """Cómo cortó la sesión de pi, si hay una. Nunca puede tumbar una
        corrida: leer el disco es mejor esfuerzo."""
        if not self.salida_real:
            return (None, "")
        try:
            return clasificar_salida(self.salida_real(job.repo, job.issue,
                                                      self._desde))
        except Exception:
            return (None, "")

    def _reintentar_infra(self, job, ref, motivo, intentar):
        """Reintenta `intentar()` (sin argumentos, devuelve bool) en el
        acto hasta `spec.infra_retries` veces más antes de rendirse.

        Una falla de infraestructura (worktree, pane, o un comando de
        herdr/git/gh que no contesta bien) es transitoria y no tiene nada
        que ver con el trabajo del agente: se reintenta ahí mismo y, si
        se agota, se abandona clasificado `infra` -- eso es lo que
        `state.intentos_que_escalan` excluye al contar peldaños.
        """
        for intento in range(self.spec.infra_retries + 1):
            if intentar():
                return True
            if job.estado == "abandonado":
                # `intentar()` ya dejó un veredicto propio (el conflicto de
                # rebase que manda a la cola de mantenimiento, #64):
                # reintentar no lo arregla, volvería a chocar con lo mismo.
                return False
            if intento < self.spec.infra_retries:
                self.dormir(self.spec.infra_retry_wait_s)
        self._abandonar(job, ref, motivo, clase="infra")
        return False


# ---------------------------------------------------------------- cosecha
# Los keywords que la API de GitHub reconoce para cerrar issues. Una
# referencia cruzada (`owner/otro-repo#N`) no cierra nada de este repo (#30).
_CLOSES_KEYWORDS = re.compile(
    r"\b(?:Closes|Fixes|Resolves)\s+([#\w./-]+)", re.IGNORECASE)


def _closes_en_cuerpo(cuerpo):
    """Números de issue que el cuerpo del PR promete cerrar con
    `Closes|Fixes|Resolves #N`. Las referencias cruzadas a otro repo
    (`owner/otro-repo#N`) se descartan: no se cierran solas."""
    nums = []
    for token in _CLOSES_KEYWORDS.findall(cuerpo or ""):
        m = re.fullmatch(r"#(\d+)", token)
        if m:
            n = int(m.group(1))
            if n not in nums:
                nums.append(n)
    return nums


def cosechar(slug, run_cmd, log, limit=20):
    """Después de mergear, el issue tiene que quedar cerrado: los agentes
    escriben "Closes #N" de forma inconsistente, y un issue que sigue abierto
    vuelve a la frontera y se re-trabaja para siempre. Cierra, por cada PR
    merged reciente, los issues que cerró y que siguen abiertos.

    Lee además el **cuerpo** del PR (#30): la API no siempre registra el
    vínculo aunque la palabra clave esté escrita, y un PR puede mergearse
    con `Closes #N` y `closingIssuesReferences` vacío. Lo que se cierra por
    esa vía se anota distinto en el log: el vínculo de GitHub falló, y
    saberlo importa. Idempotente: lo que ya está cerrado no se toca."""
    cerrados = []
    ok, out = run_cmd(["gh", "pr", "list", "--state", "merged", "--limit", str(limit),
                       "--json", "number,headRefName,body,closingIssuesReferences",
                       "-R", slug])
    if not ok or not out:
        return cerrados
    try:
        prs = json.loads(out)
    except ValueError:
        return cerrados
    for pr in prs:
        por_api = [iss.get("number") for iss in pr.get("closingIssuesReferences") or []
                   if iss.get("number") is not None]
        # Solo lo que la API no vinculó: un número que sí figura en
        # closingIssuesReferences se anota como su propio caso, no como fallo.
        por_cuerpo = [n for n in _closes_en_cuerpo(pr.get("body"))
                      if n not in por_api]
        candidatos = [(n, False) for n in por_api] + [(n, True) for n in por_cuerpo]
        for num, por_cuerpo_ref in candidatos:
            ok2, state = run_cmd(["gh", "issue", "view", str(num), "--json", "state",
                                  "-R", slug])
            if ok2 and '"OPEN"' in state:
                ok3, _ = run_cmd(["gh", "issue", "close", str(num), "-R", slug])
                if ok3:
                    cerrados.append(num)
                    if por_cuerpo_ref:
                        cuerpo = ("PR #{} merged; `Closes #{}` en el cuerpo pero "
                                  "la API no registro el vinculo".format(
                                      pr.get("number"), num))
                    else:
                        cuerpo = ("PR #{} merged pero el issue seguia abierto".format(
                            pr.get("number")))
                    log.write("issue-cerrado", "#{}".format(num), cuerpo)
    return cerrados
