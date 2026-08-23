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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

ORIGEN = "harness"

# Línea de estado de la TUI de pi: "↑78k ↓5.9k R34k CH0.0% $0.056 11.4%/262k"
CTX_RE = re.compile(r"(\d+(?:\.\d+)?)%/\d+k")
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


def prompt_de(issue):
    """El trabajo de un agente, en una línea. En inglés: es machine-facing."""
    return una_linea(
        "Read AGENTS.md and CONTEXT.md, then implement GitHub issue {} in this "
        "worktree (branch ticket/{}). Rules: open the PR only if "
        "./scripts/gate.sh exits 0 in this worktree; the PR body MUST contain "
        "'Closes {}'; never merge and never force-push; stop once the PR is "
        "open.".format("#" + str(issue), issue, "#" + str(issue))
    )


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    cuerpo. `origen` existe y hoy siempre vale `harness`: es la marca de quién
    escribió la línea, para que un observador futuro no invalide lo escrito.
    """

    def __init__(self, path, contexto, reloj=None):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.contexto = contexto
        self.reloj = reloj or _ahora

    def write(self, tipo, ref, cuerpo):
        linea = {
            "timestamp": self.reloj(),
            "contexto": self.contexto,
            "origen": ORIGEN,
            "tipo": tipo,
            "ref": ref,
            "cuerpo": cuerpo,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(linea, ensure_ascii=False) + "\n")
        return linea


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
    costo: float = 0.0


@dataclass
class DispatchSpec:
    """Cómo corre esta tanda. 24 GB: dos agentes, no cinco."""

    contexto: str
    max_parallel: int = 2
    kind: str = "pi"
    model: str = ""
    wait_ms: int = 3_600_000      # espera máxima por agente (una noche)
    gate_timeout: int = 1800      # segundos para gate.sh dentro del worktree
    install_timeout: int = 900    # segundos para instalar dependencias
    start_retries: int = 4        # reintentos de agent start (pane sin shell)
    start_wait_s: float = 2.0     # espera entre reintentos de arranque
    verify_retries: int = 3       # reintentos si el primer prompt se pierde
    verify_wait_s: float = 30.0   # cuánto esperar a que el contexto salga de 0%
    verify_poll_s: float = 2.0


class Dispatcher:
    """Lanza y cosecha la frontera. Todo el mundo entra por callables,
    para que los tests no necesiten herdr ni git de verdad."""

    def __init__(self, spec, log, run_cmd, credits=None, dormir=time.sleep):
        self.spec = spec
        self.log = log
        self.run_cmd = run_cmd                 # (args, cwd=None, timeout=30) -> (ok, out)
        self.credits = credits or (lambda: None)  # () -> usado (float) | None
        self.dormir = dormir

    # ----------------------------------------------------------- nivel corrida
    def dispatch(self, jobs):
        """Toda la corrida: los jobs en paralelo (hasta el tope), el costo
        medido con los creditos de antes y después, y las líneas de principio
        y fin. Devuelve los jobs con su estado final."""
        ref = "corrida"
        self.log.write("corrida", ref,
                       "inicio: {} ticket(s), max {} en paralelo".format(
                           len(jobs), self.spec.max_parallel))
        antes = self.credits()
        with cf.ThreadPoolExecutor(max_workers=max(1, self.spec.max_parallel)) as ex:
            resultados = list(ex.map(self.run_job, jobs))
        despues = self.credits()
        if antes is not None and despues is not None:
            self.log.write("costo", ref,
                           "creditos antes {} / despues {} / delta ${:.4f}".format(
                               antes, despues, despues - antes))
        hechas = [j for j in resultados if j.estado == "hecho"]
        self.log.write("corrida", ref,
                       "fin: {} hecho(s), {} abandonado(s), costo de jobs ${:.4f}".format(
                           len(hechas), len(resultados) - len(hechas),
                           sum(j.costo for j in resultados)))
        return resultados

    # ------------------------------------------------------------- un job
    def run_job(self, job):
        ref = "ticket/{}".format(job.issue)
        try:
            if not self._worktree_ok(job, ref):
                pass  # ya se abandono con su motivo
            elif not self._pane(job, ref):
                self._abandonar(job, ref, "no se pudo crear el pane de herdr")
            elif not self._agente(job, ref):
                self._abandonar(job, ref, "no arranco el agente")
            elif not self._prompt_verificado(job, ref):
                self._abandonar(job, ref, "primer prompt perdido: el contexto "
                                          "no salio de 0% en {} intentos".format(
                                              self.spec.verify_retries))
            elif self._bloqueado(job, ref):
                pass  # ya se abandono con su motivo
            elif not self._gate_verde(job, ref):
                pass  # ya se abandono con su motivo
            else:
                self._pr_abierto(job, ref)
        finally:
            job.costo = self._costo_pane(job)
            if job.costo:
                self.log.write("costo", ref, "${:.4f}".format(job.costo))
            self._limpiar(job, ref)
        return job

    # -------------------------------------------------- pasos del ciclo de vida
    def _worktree(self, job, ref):
        """Worktree aislado por ticket. Si la rama ya existe (un intento
        anterior la dejo), se reutiliza en vez de chocar."""
        job.branch = "ticket/{}".format(job.issue)
        job.worktree = str(worktree_path(job.repo_path, job.issue))
        self.run_cmd(["git", "-C", job.repo_path, "worktree", "prune"])
        if Path(job.worktree).exists():
            self.run_cmd(["git", "-C", job.repo_path, "worktree", "remove",
                          "--force", job.worktree])
        args = ["git", "-C", job.repo_path, "worktree", "add"]
        existe, _ = self.run_cmd(["git", "-C", job.repo_path, "rev-parse",
                                  "--verify", "--quiet", "refs/heads/" + job.branch])
        if existe:
            # La rama ya está (un intento anterior la dejó): se reutiliza.
            args += [job.worktree, job.branch]
        else:
            # `worktree add -b <rama> <path>`. El nombre de la rama va pegado a
            # -b: si va el path, git lo toma como nombre de rama y falla con
            # "is not a valid branch name".
            args += ["-b", job.branch, job.worktree]
        ok, out = self.run_cmd(args, timeout=120)
        if not ok:
            self.log.write("worktree", ref, "fallo: " + out.strip()[-200:])
        return ok

    def _worktree_ok(self, job, ref):
        if self._worktree(job, ref):
            self.log.write("worktree", ref, job.worktree)
            return True
        self._abandonar(job, ref, "no se pudo crear el worktree")
        return False

    def _pane(self, job, ref):
        """Un pane de herdr con el cwd en el worktree; sin robar el foco."""
        ok, out = self.run_cmd(["herdr", "pane", "split", "--current",
                                "--direction", "right", "--cwd", job.worktree,
                                "--no-focus"], timeout=60)
        pane = _json_field(out, ("result", "pane", "pane_id"))
        if ok and pane:
            job.pane = pane
            self.log.write("pane", ref, pane)
            return True
        self.log.write("pane", ref, "fallo: " + out.strip()[:200])
        return False

    def _agente(self, job, ref):
        job.agent = nombre_agente(job.repo, job.issue)
        args = ["herdr", "agent", "start", job.agent, "--kind", self.spec.kind,
                "--pane", job.pane, "--timeout", "120000"]
        if self.spec.model:
            args += ["--", "--model", self.spec.model]
        # El diálogo de confianza de Claude tapia el arranque en cada worktree.
        if self.spec.kind == "claude":
            confiar_en(job.worktree)
        # `pane split` vuelve antes de que el shell del pane esté listo, así que
        # el primer `agent start` puede rebotar con agent_pane_busy. Es una
        # carrera, no un veredicto: se reintenta.
        for intento in range(1, self.spec.start_retries + 1):
            ok, out = self.run_cmd(args, timeout=150)
            if ok:
                self.log.write("agente", ref,
                               "{} (kind {}, pane {})".format(job.agent,
                                                              self.spec.kind,
                                                              job.pane))
                return True
            if _json_field(out, ("error", "code")) != "agent_pane_busy":
                break
            self.log.write("agente", ref,
                           "pane sin shell todavia, intento {}".format(intento))
            self.dormir(self.spec.start_wait_s)
        self.log.write("agente", ref, "fallo: " + out.strip()[:200])
        return False

    def _prompt_verificado(self, job, ref):
        """Manda el prompt y verifica de verdad que llego: el primero después
        de `agent start` se pierde (carrera con la TUI de pi) y herdr reporta
        éxito igual. La evidencia de que llego es que el contexto del agente
        sube de 0%; si no, se reintenta. Un trabajo que nunca arranco no puede
        contarse como lanzado."""
        prompt = prompt_de(job.issue)
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
            self.log.write("prompt", ref, "intento {}: {}".format(
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
        aprobación o una pregunta: se abandona, se anota, y el dispatcher
        sigue con el siguiente; no se queda esperando a un humano que no va
        a estar."""
        wait_s = self.spec.wait_ms // 1000 + 120
        # `done` va en la lista porque es donde se asienta claude cuando
        # termina. Sin él, un agente que ya dejó el PR abierto no matchea
        # ningún estado y el wait cuelga hasta el timeout —una hora de reloj
        # por ticket terminado, con la corrida entera haciendo cola detrás.
        ok, out = self.run_cmd(["herdr", "agent", "wait", job.agent,
                                "--until", "idle", "--until", "blocked",
                                "--until", "done",
                                "--timeout", str(self.spec.wait_ms)],
                               timeout=wait_s)
        status = _json_field(out, ("result", "agent", "agent_status"))
        if status == "blocked":
            self._abandonar(job, ref, "agente bloqueado (aprobacion o pregunta pendiente)")
            return True
        if ok and status:
            self.log.write("agente", ref, "se asieto: {}".format(status))
            return False
        error = _json_field(out, ("error", "code"))
        if error == "timeout":
            self._abandonar(job, ref, "timeout de espera ({} ms)".format(self.spec.wait_ms))
        else:
            self._abandonar(job, ref, "fallo al esperar al agente: " + out.strip()[:200])
        return True

    def _preparar(self, job, ref):
        """Deja el worktree en condiciones de correr el gate.

        `git worktree add` trae lo versionado y nada más. Sin esto el gate se
        pone rojo por falta de dependencias y el ticket se abandona por una
        razón que no tiene nada que ver con su código.
        """
        copiados = copiar_entorno(job.repo_path, job.worktree)
        if copiados:
            self.log.write("worktree", ref, "entorno: " + ", ".join(copiados))
        wt = Path(job.worktree)
        cmd = comando_de_instalacion([f.name for f in wt.iterdir()]
                                     if wt.exists() else [])
        if not cmd or (wt / "node_modules").exists():
            return
        ok, out = self.run_cmd(cmd, cwd=job.worktree, timeout=self.spec.install_timeout)
        self.log.write("worktree", ref, "instalar con {}: {}".format(
            cmd[0], "ok" if ok else "fallo: " + out.strip()[-160:]))

    def _gate_verde(self, job, ref):
        """El gate se corre en el worktree, por el dispatcher: no confía en
        la palabra del agente. Rojo = abandono, y no hay PR."""
        self._preparar(job, ref)
        ok, out = self.run_cmd(["./scripts/gate.sh"], cwd=job.worktree,
                               timeout=self.spec.gate_timeout)
        if ok:
            self.log.write("gate", ref, "verde")
            return True
        self.log.write("gate", ref, "rojo: " + out.strip()[-200:])
        self._abandonar(job, ref, "gate rojo en el worktree: sin PR")
        return False

    def _pr_abierto(self, job, ref):
        """El agente debió dejar un PR abierto sobre su rama. Si no, el
        ticket no tiene entregable y el trabajo no cuenta."""
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
        if num is not None:
            job.estado = "hecho"
            self.log.write("pr", ref, "PR #{} abierto (gate verde)".format(num))
        else:
            self._abandonar(job, ref, "el agente termino sin PR abierto")

    # --------------------------------------------------------------- limpieza
    def _costo_pane(self, job):
        """El costo de la sesión, de la línea de estado de la TUI ($ antes
        del porcentaje de contexto)."""
        if not job.pane:
            return 0.0
        ok, out = self.run_cmd(["herdr", "pane", "read", job.pane,
                                "--source", "recent", "--lines", "12"], timeout=30)
        if not ok:
            return 0.0
        for linea in reversed(out.splitlines()):
            if CTX_RE.search(linea):
                costos = COSTO_RE.findall(linea)
                return float(costos[-1]) if costos else 0.0
        return 0.0

    def _limpiar(self, job, ref):
        """Cierra el pane y quita el worktree: un pane que queda abierto para
        siempre hace inutilizable la pantalla después de unas cuantas tandas.
        La rama queda: si el agente dejo commits, son recuperables, y si el
        ticket vuelve a la frontera el próximo intento la reutiliza."""
        cerrado = ""
        if job.pane:
            self.run_cmd(["herdr", "pane", "close", job.pane], timeout=60)
            cerrado = "pane {} cerrado; ".format(job.pane)
        if job.worktree:
            self.run_cmd(["git", "-C", job.repo_path, "worktree", "remove",
                          "--force", job.worktree], timeout=120)
        self.log.write("limpieza", ref,
                       "{}worktree removido, rama {} quedo".format(cerrado, job.branch))

    def _abandonar(self, job, ref, motivo):
        job.estado = "abandonado"
        job.motivo = motivo
        self.log.write("abandono", ref, motivo)


# ---------------------------------------------------------------- cosecha
def cosechar(slug, run_cmd, log, limit=20):
    """Después de mergear, el issue tiene que quedar cerrado: los agentes
    escriben "Closes #N" de forma inconsistente, y un issue que sigue abierto
    vuelve a la frontera y se re-trabaja para siempre. Cierra, por cada PR
    merged reciente, los issues que cerró y que siguen abiertos."""
    cerrados = []
    ok, out = run_cmd(["gh", "pr", "list", "--state", "merged", "--limit", str(limit),
                       "--json", "number,headRefName,closingIssuesReferences",
                       "-R", slug])
    if not ok or not out:
        return cerrados
    try:
        prs = json.loads(out)
    except ValueError:
        return cerrados
    for pr in prs:
        for iss in pr.get("closingIssuesReferences") or []:
            num = iss.get("number")
            if num is None:
                continue
            ok2, state = run_cmd(["gh", "issue", "view", str(num), "--json", "state",
                                  "-R", slug])
            if ok2 and '"OPEN"' in state:
                ok3, _ = run_cmd(["gh", "issue", "close", str(num), "-R", slug])
                if ok3:
                    cerrados.append(num)
                    log.write("issue-cerrado", "#{}".format(num),
                              "PR #{} merged pero el issue seguia abierto".format(
                                  pr.get("number")))
    return cerrados
