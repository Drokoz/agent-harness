"""El mantenedor de conflictos: `harness maintain` (#56, segundo tipo de job).

Los conflictos se van a generar y está bien: para eso están los worktrees.
Lo que faltaba no es evitarlos, es un agente que revise los PRs abiertos y
los resuelva. Es un segundo tipo de job: no sale de la frontera de issues
sino de los PRs abiertos, tiene otro prompt (resolver, no implementar) y
otra condición de terminación (el PR queda mergeable y el gate verde sobre
la rama resuelta, no se abre un PR nuevo).

Es una segunda cola con prioridad sobre la de tickets nuevos: si el
harness produce PRs más rápido de lo que los mantiene mergeables, la pila
se pudre sola. Por eso `harness run` la procesa antes que la frontera.

La parte difícil: qué cuenta como resuelto. Una resolución `--ours` o
`--theirs` pasa el gate y tira trabajo en silencio — el patrón del gate
mentiroso en un lugar nuevo: verde no alcanza. Antes de despachar el
resolver se toma una baseline (los archivos que el PR toca contra la base,
y los tests entre ellos); después, la resolución sólo se acepta si el diff
sigue tocando esos archivos y los tests siguen existiendo. Si no, se
abandona y el PR queda anotado para humano.

El log lo registra como su propio tipo de evento (`mantenimiento`, ref
"pr/N", clave "repo#pr<N>"), distinguible de un job de ticket ("corrida",
"ticket/N", "repo#<N>"): los reductores de `state` no cruzan las dos
colas, así que un abandono de mantenimiento no escala la escalera del
ticket con el mismo número.

Como todo en este repo, el módulo es testeable sin herdr ni git:
`Mantener` hereda `Dispatcher`, que recibe el mundo como callables.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from harness.dispatch import (Dispatcher, nombre_agente, podar_transcripciones,
                              transcripcion_path, una_linea, worktree_path)
from harness.state import clave_mantenimiento

# Los valores de `mergeable` que contesta `gh pr view --json mergeable`.
# UNKNOWN es "GitHub todavía lo calcula": no es un veredicto, la próxima
# pasada lo encuentra otra vez.
CONFLICTING = "CONFLICTING"
MERGEABLE = "MERGEABLE"


# --------------------------------------------------------------------- puros
def mergeable_de(slug, numero, run_cmd):
    """El veredicto de `gh pr view --json mergeable` para un PR, o None si
    gh no contestó: una falla del adaptador no es un conflicto."""
    ok, out = run_cmd(["gh", "pr", "view", str(numero), "--json", "mergeable",
                       "-R", slug])
    if not ok or not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data.get("mergeable") if isinstance(data, dict) else None


def prs_no_mergeables(slug, run_cmd, limit=20):
    """Los PRs abiertos del repo que GitHub dice que no son mergeables
    (#56). La fuente es `gh pr view --json mergeable` por PR; MERGEABLE no
    entra a la cola y UNKNOWN se salta (ver arriba). Sin gh, sin cola."""
    ok, out = run_cmd(["gh", "pr", "list", "--state", "open",
                       "--limit", str(limit),
                       "--json", "number,headRefName,title", "-R", slug])
    if not ok or not out:
        return []
    try:
        prs = json.loads(out)
    except ValueError:
        return []
    malos = []
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("number") is None:
            continue
        if mergeable_de(slug, pr["number"], run_cmd) == CONFLICTING:
            malos.append(pr)
    return malos


def es_test(ruta):
    """¿Esta ruta de un diff es un test? Lo que una resolución no puede
    descartar (#56): los tests que el PR agrega tienen que seguir
    existiendo. Reconoce lo habitual sin pretender ser exhaustivo."""
    ruta = (ruta or "").strip().strip("/")
    if not ruta:
        return False
    partes = ruta.split("/")
    if "test" in partes[:-1] or "tests" in partes[:-1]:
        return True
    base = partes[-1].lower()
    if base.startswith("test_") or base.endswith(("_test.py", "_test.go",
                                                   "_test.rs")):
        return True
    return bool(re.search(r"\.(test|spec)\.[a-z]+$", base))


@dataclass(frozen=True)
class Baseline:
    """Lo que el PR contenía antes de despachar el resolver (#56): los
    archivos del diff contra la base (tienen que seguir siendo tocados
    después de resolver) y los tests entre ellos (tienen que seguir
    existiendo). Sin ella no hay contra qué verificar, y sin verificación
    una resolución `--ours` pasa en silencio."""

    archivos: frozenset
    tests: frozenset

    @classmethod
    def de_pr(cls, files):
        rutas = frozenset(f.get("path") for f in files
                          if isinstance(f, dict) and f.get("path"))
        return cls(archivos=rutas, tests=frozenset(r for r in rutas if es_test(r)))


def preserva(baseline, archivos_despues):
    """¿Esta resolución no descarta trabajo? (#56) Devuelve (ok, motivo).

    El diff contra la base tiene que seguir tocando los archivos que el PR
    tocaba antes de resolver, y los tests que el PR agregó tienen que
    seguir existiendo (siguen en el diff). Que toque MÁS no cuenta en
    contra: resolver un conflicto puede abrir más archivos, no menos.
    Sin baseline no se puede verificar, y lo inverificable no se acepta.
    """
    if baseline is None:
        return False, "no hay baseline previa: no se puede verificar la resolucion"
    ahora = set(archivos_despues or ())
    tests_perdidos = sorted(baseline.tests - ahora)
    if tests_perdidos:
        return False, "la resolucion descarto tests del PR: " + ", ".join(tests_perdidos)
    perdidos = sorted(baseline.archivos - ahora)
    if perdidos:
        return False, "el diff ya no toca los archivos que tocaba el PR: " + ", ".join(perdidos)
    return True, ""


def prompt_de_resolucion(pr, branch, base):
    """El trabajo del resolver, en una línea. En inglés: es machine-facing.

    Resolver, no implementar: no hay ticket que cerrar, el PR ya existe.
    La regla dura va en el prompt aunque el veredicto final sea mecánico:
    una resolución que descarta trabajo se rechaza aunque pase el gate.
    """
    texto = (
        "Read AGENTS.md, then resolve the merge conflicts between branch {} "
        "(PR #{}) and {} in this worktree. This is a resolve job, not an "
        "implement job: bring the branch up to date by merging {} into it, "
        "resolve every conflict by combining the work of both sides, and "
        "never discard changes from either side -- a resolution that drops "
        "work is rejected even if it passes the gate. Do not add features "
        "and do not refactor. Once all conflicts are resolved, commit; run "
        "./scripts/gate.sh, and only if it exits 0, push the branch (no "
        "force-push). Never merge the PR and never close it. Stop once the "
        "branch is pushed.".format(branch, pr, base, base)
    )
    return una_linea(texto)


# --------------------------------------------------------------------- jobs
@dataclass
class MantenJob:
    """Un PR abierto que no es mergeable, y su recorrido por el mantenedor
    (#56). La referencia es el PR, no un ticket: no hay issue que cerrar
    ni rama que crear."""

    repo: str
    repo_path: str
    slug: str
    pr: int               # número del PR
    branch: str = ""      # headRefName del PR (la rama ya existe)
    base: str = "main"    # contra qué se resuelve (rama por defecto del repo)
    worktree: str = ""
    pane: str = ""
    agent: str = ""
    estado: str = "pendiente"   # pendiente | resuelto | abandonado
    motivo: str = ""
    clase_abandono: str = ""
    costo: Optional[float] = None
    attempt: int = 1
    kind: str = ""
    model: str = ""
    extra_args: Tuple[str, ...] = ()
    baseline: Optional[Baseline] = field(default=None)


class Mantener(Dispatcher):
    """El ciclo de vida de un job de mantenimiento (#56): mismo esqueleto
    que el dispatcher de tickets (worktree aislado, pane, agente
    verificado, watchdog, gate corrido por el dispatcher, limpieza), con
    las costuras del segundo tipo de job: la rama ya existe, el prompt es
    de resolver, la baseline se toma antes, y la terminación es "PR
    mergeable y trabajo preservado", no "PR abierto"."""

    _tipo_corrida = "mantenimiento"

    # ------------------------------------------------------------- costuras
    def _log(self, job, tipo, ref, cuerpo, clase=None):
        """El tipo propio del log del PR: clave "repo#pr<N>" (#56),
        distinguible del ticket "repo#<N>". Los reductores de `state` no
        cruzan las dos colas."""
        self.log.write(tipo, ref, cuerpo,
                       ticket=clave_mantenimiento(job.repo, job.pr),
                       attempt=job.attempt, clase=clase)

    def _ref_de(self, job):
        return "pr/{}".format(job.pr)

    def _claves_costo(self, job):
        # La sesión de pi vive en el worktree `...-ticket-<pr>` (ver
        # `_worktree`): el costo se lee con el número de PR, no de ticket.
        return (job.repo, job.pr)

    def _es_hecho(self, job):
        return job.estado in ("hecho", "resuelto")

    def _nombre_agente(self, job):
        # "pr<N>" en el lugar del issue: no colisiona con el agente de un
        # ticket ("repo-N") ni con el de otro PR.
        return nombre_agente(job.repo, "pr{}".format(job.pr))

    def _prompt(self, job):
        return prompt_de_resolucion(job.pr, job.branch, job.base)

    def _transcripcion_destino(self, job):
        return transcripcion_path(job.repo_path, job.pr, job.attempt, tipo="pr")

    def _podar_transcripciones(self, dir, job):
        return podar_transcripciones(dir, Path(job.repo_path).name, job.pr,
                                     tipo="pr")

    # --------------------------------------------------------------- pasos
    def _baseline(self, job, ref):
        """Antes de despachar el resolver: qué toca el PR (#56). Es la
        referencia contra la que se acepta o se descarta la resolución;
        no poder leerla es una falla del mundo (infra, no consume
        peldaño) y no se despacha a ciegas."""
        salida = []

        def intentar():
            ok, out = self.run_cmd(["gh", "pr", "view", str(job.pr),
                                    "--json", "files", "-R", job.slug])
            salida[:] = [ok, out]
            return ok and bool(out)

        if not self._reintentar_infra(job, ref,
                                      "no se pudo leer el diff del PR antes "
                                      "de resolver: no se despacha a ciegas",
                                      intentar):
            return False
        _, out = salida
        try:
            files = json.loads(out)
        except ValueError:
            files = []
        job.baseline = Baseline.de_pr(files)
        self._log(job, "mantenimiento", ref,
                  "baseline: {} archivos, {} tests".format(
                      len(job.baseline.archivos), len(job.baseline.tests)))
        return True

    def _worktree(self, job, ref):
        """El worktree de la rama del PR (#56): la rama ya existe, no se
        crea ni se actualiza sobre la base — el conflicto ES el trabajo:
        el resolver lo resuelve adentro del worktree. El nombre del
        worktree codifica el PR (`<repo>-ticket-<pr>`), que es lo que
        `costo_pi` extrae de la ruta de la sesión."""
        job.worktree = str(worktree_path(job.repo_path, job.pr))
        self.run_cmd(["git", "-C", job.repo_path, "worktree", "prune"])
        if Path(job.worktree).exists():
            self.run_cmd(["git", "-C", job.repo_path, "worktree", "remove",
                          "--force", job.worktree])
        # La rama remota manda: si en lo local quedó una copia de un
        # intento anterior, el fetch deja ver la del PR antes de tomarla.
        self.run_cmd(["git", "-C", job.repo_path, "fetch", "origin",
                      job.branch], timeout=120)
        existe, _ = self.run_cmd(["git", "-C", job.repo_path, "rev-parse",
                                  "--verify", "--quiet",
                                  "refs/heads/" + job.branch])
        if existe:
            args = ["git", "-C", job.repo_path, "worktree", "add",
                    job.worktree, job.branch]
        else:
            args = ["git", "-C", job.repo_path, "worktree", "add",
                    "--track", "-b", job.branch, "origin/" + job.branch]
        ok, out = self.run_cmd(args, timeout=120)
        if not ok:
            self._log(job, "worktree", ref, "fallo: " + out.strip()[-200:])
            return False
        return True

    def _mergeable_ahora(self, job, ref):
        """El veredicto de GitHub sobre el PR resuelto, con un poco de
        paciencia: `UNKNOWN` es "todavía lo calculo", no un veredicto, y
        un abandono por un cálculo lento gastaría peldaño de gusto."""
        veredicto = None
        for _ in range(3):
            veredicto = mergeable_de(job.slug, job.pr, self.run_cmd)
            if veredicto in (MERGEABLE, CONFLICTING):
                return veredicto
            self.dormir(self.spec.verify_poll_s)
        return veredicto

    def _preserva_trabajo(self, job, ref):
        """La resolución no se acepta si descarta trabajo (#56): el diff
        contra la base tiene que seguir tocando los archivos del PR de
        antes, y los tests que el PR agregó tienen que seguir existiendo.
        Una resolución `--ours`/`--theirs` pasa el gate y tira trabajo en
        silencio: el verde del gate no alcanza, se verifica contra la
        baseline."""
        salida = []

        def intentar():
            ok, out = self.run_cmd(["gh", "pr", "view", str(job.pr),
                                    "--json", "files", "-R", job.slug])
            salida[:] = [ok, out]
            return ok and bool(out)

        if not self._reintentar_infra(job, ref,
                                      "no se pudo leer el diff del PR "
                                      "resuelto: no se puede verificar",
                                      intentar):
            return False
        _, out = salida
        try:
            files = json.loads(out)
        except ValueError:
            files = []
        rutas = [f.get("path") for f in files if isinstance(f, dict)]
        ok, motivo = preserva(job.baseline, rutas)
        if ok:
            return True
        self._log(job, "gate", ref, motivo)
        self._abandonar(job, ref, "la resolucion no se acepta: " + motivo,
                        clase="modelo")
        self._para_humano(job, ref,
                          "Mantenimiento (#56): la resolucion automatica no "
                          "se acepta: " + motivo + " El PR queda anotado para "
                          "humano: resolver los conflictos a mano sin "
                          "descartar este trabajo.",
                          "resolucion no aceptada, PR para humano: " + motivo)
        return False

    def _done(self, job, ref):
        """La condición de terminación del mantenimiento (#56): el PR queda
        mergeable según GitHub, el gate quedó verde sobre la rama resuelta
        (ya corrió en `_gate_verde`), el PR apunta a lo que el gate midió,
        y la resolución no descartó trabajo. Lo decide el mantenedor, no
        la palabra del agente."""
        veredicto = self._mergeable_ahora(job, ref)
        if veredicto != MERGEABLE:
            self._log(job, "mantenimiento", ref,
                      "el PR sigue {} despues de la resolucion".format(
                          veredicto or "sin veredicto"))
            self._abandonar(job, ref,
                            "el PR no quedo mergeable despues de la "
                            "resolucion ({}): sin aceptar".format(
                                veredicto or "sin veredicto"),
                            clase="modelo")
            return
        if not self._pr_mide_el_head(job, ref, job.pr):
            return
        if not self._preserva_trabajo(job, ref):
            return
        job.estado = "resuelto"
        self._log(job, "mantenimiento", ref,
                  "PR #{} mergeable, gate verde, trabajo preservado".format(
                      job.pr))

    # ---------------------------------------------------- fuera de la flota
    def _marcar_para_humano(self, job, ref):
        """El mantenimiento no tiene ticket que etiquetar: el PR queda
        anotado por comentario, y el ticket detrás del PR se queda en
        estado `pr-abierto` en la frontera, que ya lo saca de la cola."""

    def _para_humano(self, job, ref, comentario, log_cuerpo):
        """El PR queda anotado para humano (#56): comentario con el motivo,
        para que quien lo tome no tenga que releer el log de eventos."""
        if job.slug:
            self.run_cmd(["gh", "pr", "comment", str(job.pr),
                          "--body", comentario, "-R", job.slug])
        self._log(job, "mantenimiento", ref, log_cuerpo)

    def parkear(self, job, ref, motivos):
        """Agota la escalera de reintentos del mantenimiento (#56): el PR
        queda anotado para humano con el motivo de cada intento. Sin
        worktree ni agente: no hay peldaño que correr."""
        comentario = ("Mantenimiento (#56): escalera de reintentos agotada "
                      "tras {} intento(s):\n".format(len(motivos))
                      + "\n".join("{}. {}".format(i, m)
                                  for i, m in enumerate(motivos, 1))
                      + "\nEl PR queda anotado para humano: resolver los "
                        "conflictos a mano.")
        self._para_humano(job, ref, comentario,
                          "escalera agotada tras {} intento(s): para "
                          "humano".format(len(motivos)))
