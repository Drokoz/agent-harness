"""El generador y el juez de trabajo nuevo: `harness proponer` (#115).

Cuando la frontera se vacía, las horas más baratas de la máquina se gastan
en preguntar si hay trabajo. En vez de eso se gastan en mirarlo: un agente
barato (generador, peldaño 1) lee el código y el historial de issues y
propone trabajo nuevo; un segundo agente un peldaño arriba por costo (juez,
caro y adversarial) decide qué pasa la barra.

La máquina no toca las etiquetas: un prompt no es una ley (regla 7 del
relevo). El generador NUNCA abre un issue: escribe un archivo JSON con las
propuestas, y el harness —que aplica las etiquetas él mismo— valida cada
una (evidencia real que se puede ir a leer, dedup contra abiertos Y
cerrados, tope por corrida) y abre la que pasa, a `needs-triage`. El juez
igual: escribe su veredicto en un archivo, y el harness es el único que
pone `ready-for-agent`. El generador no tiene el label a mano, y el juez
recién lo gana con un veredicto explícito.

El juez empieza rechazando: lo que no trae un veredicto explícito de
promover, no se promueve, y cero promovidos es un resultado válido que se
anota como tal, no como un error. Lo que no promueve queda en
`needs-triage` para que un humano lo mire a la mañana; nada se tira.

El juez es el primer consumidor rutinario de Claude dentro del bucle de la
noche: `decidir_peldano_juez` lo respeta contra el piso de cuota (la
costura `permitir_claude` de `DispatchSpec` sigue guardando el aplazamiento
del peldaño de Claude, #45), y sin permiso —o forzado a mano— corre entero
en Qwen.

Como todo en este repo, el módulo es testeable sin herdr ni git:
`Proponer` hereda `Dispatcher`, que recibe el mundo como callables.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from harness.dispatch import (ESCALERA, SKILLS_IMPLEMENTADOR, Dispatcher,
                              nombre_agente, podar_transcripciones,
                              transcripcion_path, una_linea)

# Peldaños fijos (#115): el generador va en el barato (peldaño 1) y el juez
# un peldaño arriba por costo — del pi/Qwen al Claude. Sin cuota para
# Claude (piso) o forzado a mano, el juez cae al peldaño 2: mismo Qwen con
# más thinking, y la noche no se queda sin juez.
GENERADOR = ESCALERA[0]
JUEZ_CLAUDE = ESCALERA[2]
JUEZ_QWEN = ESCALERA[1]

# El tope duro de issues nuevos por corrida (#115). Sin tope, una mañana
# son cuarenta issues y la cola de triage deja de leerse.
MAX_PROPOSITAS_DEFAULT = 5

# La cola de issues contra la que dedupar (abiertos Y cerrados: un issue
# que ya se rechazó no se vuelve a proponer).
DEDUP_LIMIT = 200

# `archivo:linea` en un texto: extensión en el archivo, línea numérica.
REF_RE = re.compile(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9_]+):(\d+)")

# El issue que deja `gh issue create`, en la URL que imprime.
ISSUE_URL_RE = re.compile(r"issues/(\d+)")


def clave_propuesta(repo, rol):
    """La clave de log de un job de proponer: "repo#propuesta-<rol>".

    Distinguible de la del ticket ("repo#<N>") y de la del mantenedor
    ("repo#pr<N>"): los reductores de `state` no cruzan las tres colas, así
    que un abandono del generador no escala la escalera de ningún ticket.
    """
    return "{}#propuesta-{}".format(repo, rol)


# --------------------------------------------------------------------- puros
def normalizar_titulo(titulo):
    """El título como se compara para dedup: minúsculas, sin espacios
    dobles ni de borde. Un título distinto en mayúsculas no es trabajo
    nuevo."""
    return re.sub(r"\s+", " ", str(titulo or "")).strip().lower()


def referencias(cuerpo):
    """Las referencias `archivo:linea` que cita un texto, como
    `("archivo", linea)`. Es la evidencia que un issue generado tiene que
    traer, y la mitad del dedup: lo que un issue ya citó no se vuelve a
    proponer."""
    return [(a, int(b)) for a, b in REF_RE.findall(cuerpo or "")]


def es_duplicado(titulo, evidencia, conocidos):
    """¿Esta propuesta ya existe? Devuelve el número del issue contra el
    que choca, o None si no es duplicado.

    Por título contra `conocidos` (issues abiertos Y cerrados:
    `[(number, title, body)]`) y por lo que cita: si la evidencia apunta a
    una referencia que un issue conocido ya citó, la propuesta es lo mismo
    vestido de otra forma."""
    t = normalizar_titulo(titulo)
    refs = list(evidencia or ())
    for numero, titulo_conocido, _ in conocidos:
        if t and t == normalizar_titulo(titulo_conocido):
            return numero
    for numero, _, cuerpo in conocidos:
        citadas = set(referencias(cuerpo))
        if any(r in citadas for r in refs):
            return numero
    return None


def validar_propuesta(p, leer_linea):
    """(ok, motivo) de una propuesta cruda del generador.

    `leer_linea(archivo, linea)` -> el contenido de la línea o None
    (archivo inexistente o línea fuera de rango): la evidencia tiene que
    poder irse a leer. Sin evidencia no hay issue que se abra — no es un
    criterio, es la definición de propuesta válida.
    """
    if not isinstance(p, dict):
        return False, "no es un objeto"
    if not str(p.get("title") or "").strip():
        return False, "sin título"
    evidencia = p.get("evidence")
    if not isinstance(evidencia, list) or not evidencia:
        return False, "sin evidencia (archivo:linea)"
    for ref in evidencia:
        if not isinstance(ref, dict):
            return False, "evidencia mal formada: {}".format(ref)
        archivo, linea = str(ref.get("file") or ""), ref.get("line")
        if not archivo or not isinstance(linea, int) \
                or isinstance(linea, bool) or linea < 1:
            return False, "evidencia sin archivo:linea legible: {}".format(ref)
        if leer_linea(archivo, linea) is None:
            return False, "la evidencia no se puede leer: {}:{}".format(
                archivo, linea)
    if not str(p.get("problem") or "").strip():
        return False, "sin problema (qué está mal hoy y qué se rompe por eso)"
    criterios = p.get("acceptance")
    if not isinstance(criterios, list) or not criterios \
            or not all(str(c).strip() for c in criterios):
        return False, "sin acceptance criteria que un agente pueda cumplir"
    return True, ""


def aplicar_tope(dentro, maximo):
    """(dentro, fuera): el tope duro de propuestas por corrida. Lo que se
    queda fuera no se descarta en silencio: el llamador lo anota."""
    return dentro[:maximo], dentro[maximo:]


def parsear_veredictos(texto, numeros):
    """El veredicto del juez, adversarial por default: devuelve
    `[(issue, promover, motivo)]` para CADA issue juzgado.

    El default es rechazar: un issue que el juez no contestó (o un archivo
    que no se puede leer) no se promueve, y el motivo queda dicho.
    `promover` tiene que ser un `true` explícito — nada más lo promueve.
    """
    fallo = None
    entradas = []
    if not texto:
        fallo = "sin veredicto: el juez no dejó el archivo"
    else:
        try:
            data = json.loads(texto)
        except ValueError:
            data = None
            fallo = "veredicto ilegible (JSON roto): el default es rechazar"
        lista = None
        if isinstance(data, dict):
            lista = data.get("veredictos")
        elif isinstance(data, list):
            lista = data
        if lista is None and fallo is None:
            fallo = "veredicto sin lista de issues: el default es rechazar"
        elif isinstance(lista, list):
            entradas = [e for e in lista if isinstance(e, dict)]
    out = []
    for n in numeros:
        entrada = next((e for e in entradas
                        if e.get("issue") in (n, str(n))), None)
        if fallo and entrada is None:
            out.append((n, False, fallo))
            continue
        if entrada is None:
            out.append((n, False, "sin veredicto: el juez no lo contestó"))
            continue
        motivo = str(entrada.get("motivo") or "").strip() or "sin motivo"
        if entrada.get("promover") is True:
            out.append((n, True, motivo))
        else:
            out.append((n, False, motivo))
    return out


def cuerpo_issue(p):
    """El cuerpo del issue que el harness abre para una propuesta válida:
    el problema (no la solución), la evidencia con sus `archivo:linea`
    para ir a leerla, los acceptance criteria como checkboxes, y la marca
    de origen — la cola de triage tiene que saber que lo abrió la máquina."""
    lineas = ["## El problema", "", str(p["problem"]).strip(), "",
              "## Evidencia", ""]
    for ref in p["evidence"]:
        lineas.append("- `{}`:{}".format(ref["file"], ref["line"]))
    lineas += ["", "## Criterios de aceptación", ""]
    for c in p["acceptance"]:
        lineas.append("- [ ] {}".format(str(c).strip()))
    lineas += ["", "---",
               "Propuesto por el generador del harness (#115). El juez lo "
               "revisa antes de que pueda salir a `ready-for-agent`."]
    return "\n".join(lineas)


def decidir_peldano_juez(forzado_qwen, tope_semanal, usado_semana,
                         piso_frac):
    """(peldaño, motivo) del juez (#115): el primer consumidor rutinario de
    Claude dentro del bucle de la noche respeta el piso de cuota y se puede
    forzar a correr entero en Qwen si la semana viene ajustada.

    `tope_semanal` y `usado_semana` en puntos ponderados (la unidad de la
    cuota, #74), `piso_frac` la fracción del tope que no se puede gastar en
    el juez. Sin tope calibrado no hay piso que respetar, y sin piso no se
    deja a Claude corriendo: Qwen. Puro: la lectura de la cuota es del
    llamador."""
    if forzado_qwen:
        return JUEZ_QWEN, "forzado a Qwen (--juez-qwen)"
    if not tope_semanal:
        return JUEZ_QWEN, "la cuota no tiene tope_semanal calibrado: sin " \
                          "tope no hay piso, y sin piso no corre Claude"
    restante = tope_semanal - usado_semana
    piso = tope_semanal * piso_frac
    if restante < piso:
        return JUEZ_QWEN, "piso de cuota: quedan {:.0f} de {:.0f} puntos de " \
                          "la semana (piso {:.0f}, {:.0f}%): corre en Qwen".format(
                              restante, tope_semanal, piso, piso_frac * 100)
    return JUEZ_CLAUDE, "piso de cuota respetado: quedan {:.0f} de {:.0f} " \
                        "puntos (piso {:.0f})".format(restante, tope_semanal,
                                                     piso)


# ------------------------------------------------------------------- prompts
def prompt_generador(maximo):
    """El trabajo del generador, en una línea. En inglés: es machine-facing.

    El generador no abre issues: escribe `proposals.json` y se detiene.
    Abrirlos es del harness, porque la regla dura (evidencia real, dedup,
    tope, la etiqueta) no se sostiene con un prompt."""
    texto = (
        "You are the work PROPOSER for this repo. Read AGENTS.md first, then "
        "the code, the open AND closed issues (`gh issue list --state all`), "
        "and the recent PRs. Propose at most {} NEW pieces of work that a "
        "cheap agent could take. Rules: never re-propose anything that exists "
        "open or closed; each proposal must carry real evidence you actually "
        "read (file:line that exists and says what you claim), the problem "
        "(what is wrong today and what breaks because of it), NOT a solution, "
        "and acceptance criteria a cheap agent can fulfill without inventing "
        "anything; propose what hurts (bugs, dead code paths, broken "
        "invariants), not what is comfortable (renames, tests of already "
        "tested things, refactors nobody asked for). Do not modify any file "
        "except the one below. Write ONLY the file `proposals.json` in the "
        "current directory with exactly this shape: "
        '{{"proposals": [{{"title": ..., "problem": ..., '
        '"evidence": [{{"file": ..., "line": ...}}], "acceptance": [ ... ]}}]}}. '
        "Write zero proposals if there is nothing honest to propose: an empty "
        "list is a valid answer. Never create, edit, label or close issues "
        "yourself. Stop once proposals.json is written.".format(maximo)
    )
    return una_linea(texto)


def prompt_juez(numeros):
    """El trabajo del juez, en una línea. Adversarial: empieza rechazando.

    Como el generador, no toca issues: escribe `veredictos.json` y se
    detiene. Las etiquetas son del harness."""
    texto = (
        "You are the JUDGE of machine-generated work proposals in this repo. "
        "You start by REJECTING: an issue is promoted only if you actively "
        "decide it deserves to be. For each of these open issues ({}): read "
        "the issue and READ the code it cites. Answer, per issue: is the "
        "evidence real (does file:line say what the issue claims)? is it a "
        "duplicate of anything open or closed? are the acceptance criteria "
        "implementable, or a wish? is it worth doing (a bug with evidence is "
        "not the same as a refactor nobody asked for)? Zero promotions is a "
        "VALID and expected outcome, not an error. Do not modify any file "
        "except the one below. Write ONLY the file `veredictos.json` in the "
        "current directory with exactly this shape: "
        '{{"veredictos": [{{"issue": <number>, "promover": true|false, '
        '"motivo": <one line, in Spanish>}}]}} — one entry per issue, and '
        '"promover" true ONLY if you would rather work this than not. Never '
        "create, edit, label or close issues yourself. Stop once "
        "veredictos.json is written.".format(
            ", ".join("#" + str(n) for n in numeros))
    )
    return una_linea(texto)


# -------------------------------------------------------------------- datos
@dataclass
class ProponeJob:
    """Un paso de la corrida de proponer (generador o juez) y su recorrido
    por el dispatcher. No hay ticket: la clave del log es
    `clave_propuesta`, no "repo#<N>".

    `issues_conocidos` (generador): la cola contra la que dedupar
    (abiertos Y cerrados). `issues` (juez): los issues que juzga."""

    repo: str
    repo_path: str
    slug: str
    rol: str                     # "generador" | "juez"
    salida: str                  # el archivo que el agente tiene que dejar
    maximo: int = MAX_PROPOSITAS_DEFAULT
    issues_conocidos: Tuple[Tuple[int, str, str], ...] = ()
    issues: Tuple[int, ...] = ()
    branch: str = ""
    worktree: str = ""
    pane: str = ""
    agent: str = ""
    estado: str = "pendiente"    # pendiente | hecho | abandonado
    motivo: str = ""
    clase_abandono: str = ""
    costo: Optional[float] = None
    attempt: int = 1
    kind: str = ""
    model: str = ""
    extra_args: Tuple[str, ...] = ()
    # Sesión mínima (#42): proponer no es implementar, pero el default de
    # la base del implementador es el comportamiento del dispatcher.
    skills: Tuple[str, ...] = SKILLS_IMPLEMENTADOR
    # Lo que el agente dejó, ya leído en `_done` (mientras el worktree está
    # en pie): generador, `[{"proposal", "ok", "motivo", "duplicado_de"}]`;
    # juez, el texto crudo de `veredictos.json` (el veredicto lo decide
    # `parsear_veredictos`, adversarial).
    datos: object = None


def _leer_linea_de(worktree):
    """`leer_linea` contra el worktree del agente: la evidencia se verifica
    donde el agente la leyó, no en el checkout del llamador."""
    def leer_linea(archivo, linea):
        p = Path(worktree) / str(archivo)
        if not p.is_file():
            return None
        try:
            lineas = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        if linea > len(lineas):
            return None
        return lineas[linea - 1]
    return leer_linea


def issues_conocidos(slug, run_cmd, limit=DEDUP_LIMIT):
    """La cola de dedup (#115): los issues abiertos Y cerrados del repo,
    `[(number, title, body)]`. Devuelve None si gh no contesta: proponer a
    ciegas contra el historial es fabricar duplicados, y no se propone."""
    ok, out = run_cmd(["gh", "issue", "list", "--state", "all",
                       "--limit", str(limit), "--json",
                       "number,title,body", "-R", slug])
    if not ok or not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return [(i.get("number"), i.get("title") or "", i.get("body") or "")
            for i in data if isinstance(i, dict)
            and i.get("number") is not None]


# --------------------------------------------------------------------- clase
class Proponer(Dispatcher):
    """La corrida de proponer: generador, validación mecánica, apertura a
    `needs-triage`, juez adversarial y promoción. Mismo esqueleto que el
    dispatcher (worktree aislado, pane, agente verificado, watchdog,
    limpieza), con las costuras de un job que no escribe código: sin
    commits ni gate, y un árbol sucio sólo se tolera en el archivo de
    salida que el agente debía dejar."""

    _tipo_corrida = "proponer"

    # ------------------------------------------------------------- costuras
    def _log(self, job, tipo, ref, cuerpo, clase=None):
        self.log.write(tipo, ref, cuerpo,
                       ticket=clave_propuesta(job.repo, job.rol),
                       attempt=job.attempt, clase=clase)

    def _ref_de(self, job):
        return "propuesta/{}".format(job.rol)

    def _claves_costo(self, job):
        # La sesión de pi del worktree `-proponer-<rol>` no codifica un
        # número de ticket: sin medir, el costo queda desconocido (None),
        # que es lo honesto.
        return (job.repo, "propuesta-{}".format(job.rol))

    def _necesita_verificacion(self, job):
        """Un job de proponer no produce código: no hay commits que contar
        ni gate que correr. Su red es la validación mecánica que hace el
        harness después (`proponer_repo`): evidencia real, dedup, tope."""
        return False

    def _es_hecho(self, job):
        return job.estado == "hecho"

    def _nombre_agente(self, job):
        # "repo-proponer-generador" / "repo-proponer-juez": no colisiona con
        # el agente de un ticket ("repo-<n>") ni con el de un PR ("repo-pr<n>").
        return nombre_agente(job.repo, "proponer-{}".format(job.rol))[:32]

    def _prompt(self, job):
        if job.rol == "generador":
            return prompt_generador(job.maximo)
        return prompt_juez(job.issues)

    def _transcripcion_destino(self, job):
        return transcripcion_path(job.repo_path, "propuesta-{}".format(job.rol),
                                  job.attempt)

    def _podar_transcripciones(self, dir, job):
        return podar_transcripciones(dir, Path(job.repo_path).name,
                                     "propuesta-{}".format(job.rol))

    def _wip_abandono(self, job, ref):
        # Nada que rescatar en una rama desprendida: el archivo de salida,
        # si existía, ya se leyó en `_done`; lo demás no era trabajo.
        pass

    # --------------------------------------------------------------- pasos
    def _worktree(self, job, ref):
        """El worktree descartable del job: `--detach` sobre la base de la
        corrida, sin rama — no hay rama que recuperar. El nombre codifica
        el rol: generador y juez corren en worktrees propios y sus
        transcripciones no se pisan."""
        job.worktree = str(Path(job.repo_path).parent / ".worktrees" /
                           "{}-proponer-{}".format(Path(job.repo_path).name,
                                                   job.rol))
        self.run_cmd(["git", "-C", job.repo_path, "worktree", "prune"])
        if Path(job.worktree).exists():
            self.run_cmd(["git", "-C", job.repo_path, "worktree", "remove",
                          "--force", job.worktree], timeout=120)
        ref_base, sha_base = self._base(job)
        self._log(job, "base", self._ref_de(job),
                  "base efectiva {} (sha {})".format(ref_base, sha_base[:8]))
        ok, out = self.run_cmd(["git", "-C", job.repo_path, "worktree",
                                "add", "--detach", job.worktree, ref_base],
                               timeout=120)
        if not ok:
            self._log(job, "worktree", self._ref_de(job),
                      "fallo: " + out.strip()[-200:])
            return False
        return True

    def _arbol_limpio(self, job, ref):
        """El generador y el juez no tocan código: el único cambio que se
        tolera en el árbol es el archivo de salida (`?? proposals.json` /
        `?? veredictos.json`). Un `.py` modificado es el generador
        refactoring en vez de proponer: se abandona."""
        ref = self._ref_de(job)
        salida = []

        def intentar():
            ok, out = self.run_cmd(["git", "-C", job.worktree, "status",
                                    "--porcelain"])
            salida[:] = [ok, out]
            return ok

        if not self._reintentar_infra(
                job, ref, "no se pudo verificar el arbol (git status fallo)",
                intentar):
            return False
        _, out = salida
        esperado = "?? " + job.salida
        for l in [l for l in out.splitlines() if l.strip()]:
            if l != esperado:
                self._log(job, "gate", ref,
                          "arbol sucio fuera de la salida: " + l[:120])
                self._abandonar(job, ref, "el agente toco archivos que no "
                                           "era su trabajo: " + l[:120],
                                clase="modelo")
                return False
        return True

    def _done(self, job, ref):
        """La terminación: el archivo de salida existe y se lee. El
        VEREDICTO —qué se abre, qué se promueve— no se toma acá: la
        evidencia se valida contra el worktree (que `_limpiar` remueve al
        volver de `run_job`), y la decisión final es mecánica, en
        `proponer_repo`."""
        ref = self._ref_de(job)
        archivo = Path(job.worktree) / job.salida
        try:
            texto = archivo.read_text(encoding="utf-8")
        except OSError:
            self._abandonar(job, ref,
                            "el agente termino sin {}: sin salida no hay "
                            "{}".format(job.salida,
                                        "propuestas" if job.rol == "generador"
                                        else "veredictos"),
                            clase="modelo")
            return
        if job.rol == "generador":
            try:
                data = json.loads(texto)
            except ValueError:
                self._abandonar(job, ref,
                                "proposals.json ilegible (JSON roto): el "
                                "default es no confiar", clase="modelo")
                return
            lista = data.get("proposals") if isinstance(data, dict) else data
            if not isinstance(lista, list):
                self._abandonar(job, ref,
                                "proposals.json sin lista de propuestas",
                                clase="modelo")
                return
            leer = _leer_linea_de(job.worktree)
            datos = []
            for p in lista:
                if not isinstance(p, dict):
                    datos.append({"proposal": {"title": "?"}, "ok": False,
                                  "motivo": "no es un objeto",
                                  "duplicado_de": None})
                    continue
                ok, motivo = validar_propuesta(p, leer)
                dup = None
                if ok:
                    ev = []
                    for r in p.get("evidence") or ():
                        if isinstance(r, dict) and r.get("file") \
                                and isinstance(r.get("line"), int) \
                                and not isinstance(r.get("line"), bool):
                            ev.append((str(r["file"]), int(r["line"])))
                    dup = es_duplicado(p.get("title"), ev,
                                       job.issues_conocidos)
                    if dup is not None:
                        ok = False
                        motivo = "duplicado de #{} (abiertos o cerrados)" \
                                 .format(dup)
                datos.append({"proposal": p, "ok": ok, "motivo": motivo,
                              "duplicado_de": dup})
            job.datos = datos
            job.estado = "hecho"
            job.motivo = "{} propuesta(s)".format(len(datos))
            self._log(job, "propuesta", ref, job.motivo)
        else:
            job.datos = texto
            job.estado = "hecho"
            job.motivo = "veredictos leidos"
            self._log(job, "juez", ref, job.motivo)

    # ---------------------------------------------------------------- flujo
    def proponer_repo(self, repo, slug, repo_path, maximo, peldano_juez):
        """Una noche de proponer sobre un repo: generador -> validación y
        apertura a `needs-triage` -> juez -> promoción. Devuelve un dict de
        resumen para el llamador (y para el log del bucle).

        `peldano_juez` es el peldaño YA resuelto (`decidir_peldano_juez` +
        el llamador, con su decisión anotada en el log): acá se respeta. Si
        el peldaño es de Claude y `spec.permitir_claude` (la costura del
        router, #45) dice que no, el juez queda aplazado y los issues se
        quedan en `needs-triage` para la próxima corrida."""
        resumen = {"repo": repo, "abiertas": 0, "promovidas": 0,
                   "rechazadas": 0, "descartadas": 0, "juez": "",
                   "detalle": []}
        ref_g = "propuesta/generador"
        conocidos = issues_conocidos(slug, self.run_cmd)
        if conocidos is None:
            self.log.write("proponer", ref_g,
                           "no se pudo leer la cola de issues (gh): sin "
                           "dedup no se propone a ciegas")
            resumen["detalle"].append("gh: sin cola de issues")
            return resumen

        # ------------------------------------------------------ generador
        gen = ProponeJob(repo=repo, repo_path=repo_path, slug=slug,
                         rol="generador", salida="proposals.json",
                         maximo=maximo, issues_conocidos=tuple(conocidos),
                         kind=GENERADOR["kind"], model=GENERADOR["model"],
                         extra_args=tuple(GENERADOR["extra_args"]))
        self.run_job(gen)
        if gen.estado != "hecho":
            resumen["juez"] = "sin juez (generador {})".format(gen.estado)
            resumen["detalle"].append("generador {}: {}".format(
                gen.estado, gen.motivo))
            return resumen

        # Validación ya hecha en `_done` (el worktree del generador muere
        # al volver de run_job). El tope y la apertura son mecánicos.
        for d in gen.datos:
            if not d["ok"]:
                resumen["descartadas"] += 1
                self.log.write("propuesta", ref_g,
                               "descartada: {} — {}".format(
                                   d["proposal"].get("title") or "?",
                                   d["motivo"]))
        validas = [d["proposal"] for d in gen.datos if d["ok"]]
        dentro, fuera = aplicar_tope(validas, maximo)
        for p in fuera:
            resumen["descartadas"] += 1
            self.log.write("propuesta", ref_g,
                           "descartada por tope de {} por corrida: {}".format(
                               maximo, p.get("title") or "?"))
        abiertas = []
        for p in dentro:
            ok, out = self.run_cmd(["gh", "issue", "create",
                                    "--title", str(p["title"]).strip(),
                                    "--body", cuerpo_issue(p),
                                    "--add-label", "needs-triage",
                                    "-R", slug])
            num = None
            if ok:
                m = ISSUE_URL_RE.search(out or "")
                num = int(m.group(1)) if m else None
            if num is None:
                resumen["descartadas"] += 1
                self.log.write("propuesta", ref_g,
                               "no se pudo abrir el issue (gh issue create "
                               "fallo o no devolvió número): "
                               + (out or "").strip()[-160:])
                continue
            abiertas.append(num)
            resumen["abiertas"] += 1
            self.log.write("propuesta", "#{}".format(num),
                           "issue abierto a needs-triage: {}".format(
                               str(p["title"]).strip()),
                           ticket="{}#{}".format(repo, num))
        if not abiertas:
            self.log.write("proponer", "propuesta/juez",
                           "sin propuestas válidas que juzgar: no hay juez "
                           "(no es un error)")
            resumen["juez"] = "sin propuestas"
            return resumen

        # ---------------------------------------------------------------- juez
        numeros = tuple(abiertas)
        juez = ProponeJob(repo=repo, repo_path=repo_path, slug=slug,
                          rol="juez", salida="veredictos.json",
                          issues=numeros,
                          kind=peldano_juez["kind"],
                          model=peldano_juez.get("model", ""),
                          extra_args=tuple(peldano_juez.get("extra_args", ())))
        self.run_job(juez)
        if juez.estado != "hecho":
            resumen["juez"] = "juez {}: {}".format(juez.estado, juez.motivo)
            if juez.estado == "pendiente":
                self.log.write("juez", "propuesta/juez",
                               "aplazado (router): los issues quedan en "
                               "needs-triage para la próxima corrida — "
                               + juez.motivo)
            else:
                self.log.write("juez", "propuesta/juez",
                               "{}: los issues quedan en needs-triage — "
                               "{}".format(juez.estado, juez.motivo))
            return resumen

        veredictos = dict((n, (prom, mot))
                          for n, prom, mot in
                          parsear_veredictos(juez.datos, numeros))
        for num in numeros:
            promover, motivo = veredictos[num]
            if promover:
                self.run_cmd(["gh", "issue", "edit", str(num),
                              "--add-label", "ready-for-agent",
                              "--remove-label", "needs-triage",
                              "-R", slug])
                resumen["promovidas"] += 1
                self.log.write("promocion", "#{}".format(num), motivo,
                               ticket="{}#{}".format(repo, num))
            else:
                self.run_cmd(["gh", "issue", "comment", str(num),
                              "--body",
                              "Juez del harness (#115): no promovido a "
                              "ready-for-agent. " + motivo +
                              " Queda en needs-triage para triage humano.",
                              "-R", slug])
                resumen["rechazadas"] += 1
                self.log.write("rechazo", "#{}".format(num), motivo,
                               ticket="{}#{}".format(repo, num))
        resumen["juez"] = "corrido"
        cero = "" if resumen["promovidas"] else \
            " — cero promovidos es un resultado válido y esperable"
        self.log.write("proponer", "propuesta/juez",
                       "{} issue(s) juzgado(s): {} promovido(s), {} "
                       "rechazado(s){}{}".format(
                           len(numeros), resumen["promovidas"],
                           resumen["rechazadas"],
                           cero if not resumen["promovidas"] else "",
                           " (quedan en needs-triage)"
                           if resumen["rechazadas"] else ""))
        return resumen
