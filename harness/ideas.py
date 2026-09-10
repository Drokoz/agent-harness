"""El agente semanal de ideas: `harness ideas` (#117).

Cruza los repos del contexto (productos reales con clientes chilenos) y
busca lo que otro rubro pagaría: **qué ya está construido**. La materia
prima no es el mercado en abstracto: son productos andando.

La regla que separa una idea de un deseo: si no puede apuntar a código
que ya existe, no es una idea. Igual que un issue del harness necesita
`archivo:línea`, una idea necesita repo, archivo y línea que se puedan
ir a leer. "Una app de delivery" es un deseo; "el print-agent de koku
resuelve imprimir en una impresora de la LAN sin nube" es una idea,
porque hay algo construido detrás.

El prompt no es una ley (regla 7 del relevo): el agente (un modelo
bueno — criterio, no código) sólo escribe `ideas.json` en su worktree
descartable. El harness es el que valida (evidencia que se puede ir a
leer en los repos de verdad, dedup contra lo que ya hay en la vault,
tope por corrida, los campos que una nota tiene que traer — sin precio
no se escribe: repite el error de Entrevestidos) y el que escribe la
nota en `vault/ideas/`. No va a GitHub: no es trabajo, es material
para decidir.

El ritmo es semanal, no de cada noche: un sello en el directorio de
estado corta las corridas que están a menos de 7 días de la anterior
(`debe_correr`, `--forzar` lo salta a mano).

Como todo en este repo, el módulo es testeable sin herdr ni git:
`Ideas` hereda `Dispatcher`, que recibe el mundo como callables.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from harness.dispatch import (ESCALERA, SKILLS_IMPLEMENTADOR, Dispatcher,
                              nombre_agente, podar_transcripciones,
                              transcripcion_path, una_linea)
from harness.proposal import aplicar_tope, normalizar_titulo

# El modelo bueno (#117): esto es criterio, no código, y un modelo barato
# acá escribe folletos. Por defecto el peldaño 3 de la escalera (Claude
# Sonnet); `--opus` sube al 4.
IDEAS_CLAUDE = ESCALERA[2]
IDEAS_OPUS = ESCALERA[3]

# El tope duro de notas por corrida (#117): diez ideas es cero ideas.
MAX_IDEAS_DEFAULT = 3

# El ritmo semanal, en días (#117): no de cada noche.
DIAS_MINIMO = 7

# La cita de evidencia en una nota: `- `repo`:`archivo`:linea`. El mismo
# formato se usa para dedupar contra lo que la vault ya tiene.
CITACION_RE = re.compile(r"^- `([^`]+)`:`([^`]+)`:(\d+)$")
TITULO_RE = re.compile(r"^# (.+)$")


def clave_idea(repo):
    """La clave de log del job: "repo#ideas". Distinguible de la del
    ticket ("repo#<N>"), de la del mantenedor ("repo#pr<N>") y de la del
    proposer ("repo#propuesta-<rol>"): los reductores de `state` no
    cruzan las colas."""
    return "{}#ideas".format(repo)


def _parse_iso(s):
    """ISO 8601 -> datetime, o None. Acepta el `Z` UTC que escribe el
    harness (Python 3.9 no lo entiende en `fromisoformat`)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------- puros
def debe_correr(ultima_iso, ahora_iso, dias_minimo=DIAS_MINIMO):
    """(ok, motivo): ¿corre `ideas` ahora? El ritmo es semanal (#117):
    sin sello anterior, o con la última corrida a >= `dias_minimo` días,
    corre; si está más cerca, no — y lo dice. Puro: las horas las trae
    el llamador."""
    ahora = _parse_iso(ahora_iso)
    if ahora is None:
        return False, "la hora de ahora no se entiende: {}".format(ahora_iso)
    ultima = _parse_iso(ultima_iso)
    if ultima is None:
        return True, "primera corrida"
    dias = (ahora - ultima).total_seconds() / 86400.0
    if dias >= dias_minimo:
        return True, "la última corrida fue hace {:.1f} días".format(dias)
    return False, ("corrió hace {:.1f} días: ritmo semanal, {} días de "
                   "espera ({} para correr de todos modos)").format(
                       dias, dias_minimo, "--forzar")


def leer_ultima_corrida(path):
    """El sello de la última corrida de ideas, o "" si no hay."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def guardar_ultima_corrida(path, ahora_iso):
    """El sello de la última corrida: junto al log de eventos. Se escribe
    solo cuando el agente terminó (`hecho`): un abandono no congela el
    ritmo semanal siete días, se puede reintentar."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ahora_iso + "\n", encoding="utf-8")


def referencias_idea(evidence):
    """Las citas `("repo", "archivo", linea)` de una idea."""
    return [(str(r.get("repo") or ""), str(r.get("file") or ""), int(r.get("line")))
            for r in (evidence or ()) if isinstance(r, dict)]


def es_duplicado_idea(titulo, refs, conocidas):
    """¿Esta idea ya está escrita en la vault? Devuelve el título de la
    nota contra la que choca, o None.

    Por título (la misma idea redactada distinto) y por lo que cita: si
    la evidencia apunta a una referencia que una nota ya citó, es lo
    mismo vestido de otra forma."""
    t = normalizar_titulo(titulo)
    refs = set(refs)
    for titulo_c, refs_c in conocidas:
        if t and t == normalizar_titulo(titulo_c):
            return titulo_c
    for titulo_c, refs_c in conocidas:
        if refs & set(refs_c):
            return titulo_c
    return None


def validar_idea(idea, nombres, leer_linea):
    """(ok, motivo) de una idea cruda del agente.

    `nombres`: los repos del contexto (la evidencia no puede apuntar a
    otro lado). `leer_linea(repo, archivo, linea)` -> la línea o None:
    la evidencia tiene que poder irse a leer en el repo de verdad — sin
    eso es un deseo, no una idea. Los campos de la nota son los que el
    ticket pide: rubro concreto, quién paga, qué falta en días, versión
    mínima cobrable, precio (sin precio no se escribe) y por qué podría
    fallar (sin eso la nota es un folleto)."""
    if not isinstance(idea, dict):
        return False, "no es un objeto"
    if not str(idea.get("title") or "").strip():
        return False, "sin título"
    if not str(idea.get("patron") or "").strip():
        return False, "sin patron (qué está resuelto y en qué repo)"
    evidencia = idea.get("evidence")
    if not isinstance(evidencia, list) or not evidencia:
        return False, "sin evidencia (repo:archivo:linea): sin nada " \
                      "construido detrás no es una idea"
    for ref in evidencia:
        if not isinstance(ref, dict):
            return False, "evidencia mal formada: {}".format(ref)
        repo = str(ref.get("repo") or "")
        archivo = str(ref.get("file") or "")
        linea = ref.get("line")
        if repo not in nombres:
            return False, "evidencia en un repo del contexto inexistente: " \
                          "{}".format(repo or "?")
        if not archivo or not isinstance(linea, int) \
                or isinstance(linea, bool) or linea < 1:
            return False, "evidencia sin repo:archivo:linea legible: " \
                          "{}".format(ref)
        if leer_linea(repo, archivo, linea) is None:
            return False, "la evidencia no se puede leer: {}:{}:{}".format(
                repo, archivo, linea)
    if not str(idea.get("rubro") or "").strip():
        return False, "sin rubro (a qué rubro chileno concreto le sirve)"
    if not str(idea.get("paga") or "").strip():
        return False, "sin quién paga (quién firma el cheque y cuánto " \
                      "gasta hoy resolviéndolo de otra forma)"
    dias = idea.get("falta_dias")
    if not isinstance(dias, int) or isinstance(dias, bool) or dias < 0:
        return False, "sin qué falta en días de trabajo (falta_dias: " \
                      "un número, no \"un esfuerzo\")"
    if not str(idea.get("version_minima") or "").strip():
        return False, "sin version_minima (la versión más chica que se " \
                      "puede cobrar)"
    if not str(idea.get("precio") or "").strip():
        return False, "sin precio (cuánto y a quién): vender sin decirlo " \
                      "repite un error caro"
    if not str(idea.get("riesgo") or "").strip():
        return False, "sin riesgo (por qué podría no funcionar): sin eso " \
                      "la nota es un folleto"
    return True, ""


def nota_idea(idea, fecha):
    """La nota que el harness escribe en la vault para una idea válida:
    los campos que el ticket pide, en el orden en que se decide con ella."""
    lineas = ["# {}".format(str(idea["title"]).strip()), "",
              "Fecha: {} · Escrita por `harness ideas` (#117), agente "
              "semanal.".format(fecha), "",
              "## El patrón", "", str(idea["patron"]).strip(), "",
              "## Evidencia", ""]
    for r in idea["evidence"]:
        lineas.append("- `{}`:`{}`:{}".format(r["repo"], r["file"],
                                              r["line"]))
    lineas += ["", "## A qué rubro le sirve", "",
               str(idea["rubro"]).strip(), "",
               "## Quién firma el cheque", "", str(idea["paga"]).strip(),
               "", "## Qué falta", "",
               "{} días de trabajo.".format(idea["falta_dias"]), "",
               "## La versión más chica que se puede cobrar", "",
               str(idea["version_minima"]).strip(), "",
               "## Precio", "", str(idea["precio"]).strip(), "",
               "## Por qué podría no funcionar", "",
               str(idea["riesgo"]).strip(), ""]
    return "\n".join(lineas)


def leer_ideas_vault(ideas_dir):
    """Las ideas ya escritas en `vault/ideas/`, `[(titulo, refs)]`: la
    cola contra la que dedupar. Sin carpeta o vacía, []: no hay notas,
    no hay duplicados."""
    ideas_dir = Path(ideas_dir)
    if not ideas_dir.is_dir():
        return []
    out = []
    for f in sorted(ideas_dir.glob("*.md")):
        try:
            texto = f.read_text(encoding="utf-8")
        except OSError:
            continue
        titulo, refs = "", []
        for linea in texto.splitlines():
            linea = linea.strip()
            if not titulo:
                m = TITULO_RE.match(linea)
                if m:
                    titulo = m.group(1)
            m = CITACION_RE.match(linea)
            if m:
                refs.append((m.group(1), m.group(2), int(m.group(3))))
        if titulo:
            out.append((titulo, tuple(refs)))
    return out


def slug_de(titulo):
    """El nombre de archivo de una nota: el título, sin adornos."""
    s = re.sub(r"[^a-z0-9]+", "-", str(titulo).lower()).strip("-")
    return s[:40].rstrip("-") or "idea"


def nota_escribir(idea, fecha, ideas_dir):
    """La nota en `ideas_dir`, uno por idea. Un choque de nombre (otra
    nota del día con el mismo slug) no pisa: se numera. Devuelve la
    ruta escrita."""
    d = Path(ideas_dir)
    d.mkdir(parents=True, exist_ok=True)
    base = "{}-{}".format(fecha, slug_de(idea.get("title") or "?"))
    path = d / (base + ".md")
    n = 2
    while path.exists():
        path = d / ("{}-{}.md".format(base, n))
        n += 1
    path.write_text(nota_idea(idea, fecha), encoding="utf-8")
    return str(path)


# -------------------------------------------------------------------- prompts
def prompt_ideas(maximo, repos, conocidas):
    """El trabajo del agente, en una línea. En inglés: es machine-facing.

    El agente no escribe notas: escribe `ideas.json` y se detiene.
    Validar (evidencia real, dedup contra la vault, tope, los campos) y
    escribir las notas es del harness: la regla dura no se sostiene con
    un prompt."""
    lista = "; ".join("{} at {}".format(n, p) for n, p in repos)
    if conocidas:
        ya = "; ".join("'{}' ({})".format(
            t, ", ".join("{}:{}:{}".format(r, f, l) for r, f, l in refs))
            for t, refs in conocidas)
    else:
        ya = "none yet"
    texto = (
        "You are the IDEAS SCOUT for a set of real Chilean client "
        "products. Read the code in these repos: {}. Find at most {} "
        "things that are ALREADY BUILT that a different, concrete "
        "Chilean industry would pay for - what a partner would look "
        "for when hunting resale opportunities. Rules: an idea must "
        "point at real code you actually read (repo + file:line that "
        "exists and says what you claim); if nothing qualifies, write "
        "ZERO ideas - an empty list is a valid answer and inventing is "
        "a failure. Do not repeat ideas already written: {}. Each idea "
        "needs: title; patron (what is solved and where it lives); "
        "evidence (list of {{\"repo\", \"file\", \"line\"}}); rubro (a "
        "concrete Chilean industry, never 'pymes' in general); paga "
        "(who signs the check and what they pay today to solve it "
        "another way); falta_dias (integer: days of work to make it "
        "sellable to that industry); version_minima (the smallest "
        "sellable version); precio (how much, in CLP, and to whom - a "
        "note without a price repeats an expensive mistake); riesgo "
        "(why it might not work - a note without this is a brochure). "
        "Do not modify any file except the one below. Write ONLY the "
        "file `ideas.json` in the current directory with exactly this "
        "shape: "
        '{{"ideas": [{{"title": ..., "patron": ..., '
        '"evidence": [{{"repo": ..., "file": ..., "line": ...}}], '
        '"rubro": ..., "paga": ..., "falta_dias": <int>, '
        '"version_minima": ..., "precio": ..., "riesgo": ...}}]}}. '
        "Stop once ideas.json is written."
    ).format(lista, maximo, ya)
    return una_linea(texto)


# -------------------------------------------------------------------- datos
@dataclass
class IdeaJob:
    """La corrida de ideas: un solo job (cruza TODOS los repos del
    contexto), no uno por repo. `repos` son los checkouts de verdad
    que el agente lee y contra los que se valida la evidencia;
    `repo_path` es solo el checkout que aloja el worktree descartable
    donde el agente deja `ideas.json`."""

    repo: str
    repo_path: str
    repos: Tuple[Tuple[str, str], ...] = ()
    vault_dir: str = ""                  # vault/ideas/
    salida: str = "ideas.json"
    maximo: int = MAX_IDEAS_DEFAULT
    conocidas: Tuple[Tuple[str, Tuple[Tuple[str, str, int], ...]], ...] = ()
    branch: str = ""
    worktree: str = ""
    pane: str = ""
    agent: str = ""
    estado: str = "pendiente"            # pendiente | hecho | abandonado
    motivo: str = ""
    clase_abandono: str = ""
    costo: Optional[float] = None
    attempt: int = 1
    kind: str = ""
    model: str = ""
    extra_args: Tuple[str, ...] = ()
    # Sesión mínima (#42): la corrida de ideas no sale de un ticket que
    # declare skills; la base del implementador cubre su trabajo.
    skills: Tuple[str, ...] = SKILLS_IMPLEMENTADOR
    # Lo que el agente dejó, ya validado en `_done`:
    # `[{"idea", "ok", "motivo", "duplicada_de"}]`.
    datos: object = None


def _leer_linea_de(repos):
    """`leer_linea` contra los checkouts de verdad: la evidencia se
    verifica donde vive, no en el worktree descartable del agente."""
    tablas = dict(repos)

    def leer(repo, archivo, linea):
        base = tablas.get(str(repo))
        if base is None:
            return None
        p = Path(base) / str(archivo)
        if not p.is_file():
            return None
        try:
            lineas = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        if linea > len(lineas):
            return None
        return lineas[linea - 1]
    return leer


# --------------------------------------------------------------------- clase
class Ideas(Dispatcher):
    """La corrida de ideas: un job sin código (sin commits ni gate), la
    validación mecánica de lo que el agente dejó, y las notas que el
    harness (no el agente) escribe en la vault. Mismo esqueleto que el
    dispatcher; las costuras las redefine igual que `Proponer`."""

    _tipo_corrida = "ideas"

    def __init__(self, spec, log, run_cmd, escribir_nota=None, **resto):
        super().__init__(spec, log, run_cmd=run_cmd, **resto)
        # Costura para los tests: por defecto la nota se escribe en la
        # vault de verdad.
        self.escribir_nota = escribir_nota or nota_escribir

    # ------------------------------------------------------------- costuras
    def _log(self, job, tipo, ref, cuerpo, clase=None):
        self.log.write(tipo, ref, cuerpo, ticket=clave_idea(job.repo),
                       attempt=job.attempt, clase=clase)

    def _ref_de(self, job):
        return "idea"

    def _claves_costo(self, job):
        # La sesión no codifica un número de ticket: sin medir, el costo
        # queda desconocido (None), que es lo honesto.
        return (job.repo, "ideas")

    def _necesita_verificacion(self, job):
        """Un job de ideas no produce código: no hay commits que contar
        ni gate que correr. Su red es la validación mecánica que hace el
        harness después (`ideas_corrida`): evidencia real, dedup contra
        la vault, tope, campos."""
        return False

    def _es_hecho(self, job):
        return job.estado == "hecho"

    def _nombre_agente(self, job):
        return nombre_agente(job.repo, "ideas")[:32]

    def _prompt(self, job):
        return prompt_ideas(job.maximo, job.repos, job.conocidas)

    def _transcripcion_destino(self, job):
        return transcripcion_path(job.repo_path, "ideas", job.attempt)

    def _podar_transcripciones(self, dir, job):
        return podar_transcripciones(dir, Path(job.repo_path).name, "ideas")

    def _wip_abandono(self, job, ref):
        # Nada que rescatar en un worktree desprendido: ideas.json, si
        # existía, ya se leyó en `_done`; lo demás no era trabajo.
        pass

    # --------------------------------------------------------------- pasos
    def _worktree(self, job, ref):
        """El worktree descartable del job: `--detach` sobre la base de
        la corrida, sin rama — no hay rama que recuperar. El nombre no
        colisiona con el de un ticket ("repo-ticket-N") ni con el del
        proposer ("repo-proponer-<rol>")."""
        job.worktree = str(Path(job.repo_path).parent / ".worktrees" /
                           "{}-ideas".format(Path(job.repo_path).name))
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
        """El agente no toca código: el único cambio que se tolera en el
        worktree es el archivo de salida (`?? ideas.json`). Cualquier
        otra línea es el agente escribiendo afuera de su trabajo: se
        abandona."""
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
        """La terminación: `ideas.json` existe y se lee. Cada idea se
        valida acá — la evidencia contra los checkouts de verdad, el
        dedup contra la vault — y la decisión final (tope, notas) es
        mecánica, en `ideas_corrida`."""
        ref = self._ref_de(job)
        archivo = Path(job.worktree) / job.salida
        try:
            texto = archivo.read_text(encoding="utf-8")
        except OSError:
            self._abandonar(job, ref,
                            "el agente termino sin {}: sin salida no hay "
                            "ideas".format(job.salida),
                            clase="modelo")
            return
        try:
            data = json.loads(texto)
        except ValueError:
            self._abandonar(job, ref,
                            "ideas.json ilegible (JSON roto): el default "
                            "es no confiar", clase="modelo")
            return
        lista = data.get("ideas") if isinstance(data, dict) else data
        if not isinstance(lista, list):
            self._abandonar(job, ref,
                            "ideas.json sin lista de ideas", clase="modelo")
            return
        nombres = set(n for n, _ in job.repos)
        leer = _leer_linea_de(job.repos)
        datos = []
        for idea in lista:
            ok, motivo = (validar_idea(idea, nombres, leer)
                          if isinstance(idea, dict)
                          else (False, "no es un objeto"))
            dup = None
            if ok:
                dup = es_duplicado_idea(idea.get("title"),
                                        referencias_idea(idea["evidence"]),
                                        job.conocidas)
                if dup is not None:
                    ok = False
                    motivo = "duplicada de una nota ya escrita: '{}'".format(
                        dup)
            datos.append({"idea": idea if isinstance(idea, dict) else {},
                          "ok": ok, "motivo": motivo, "duplicada_de": dup})
        job.datos = datos
        job.estado = "hecho"
        job.motivo = "{} idea(s) del agente".format(len(datos))
        self._log(job, "idea", ref, job.motivo)

    # ---------------------------------------------------------------- flujo
    def ideas_corrida(self, repo, repo_path, repos, vault_dir, maximo,
                      conocidas, extra_args=()):
        """Una corrida de ideas sobre un contexto (#117). `repos` es
        `[(nombre, path)]` de los checkouts de verdad; la corrida aloja
        su worktree en el primero. `extra_args` son los del peldaño del
        modelo bueno (`--effort`, etc.). Devuelve un dict de resumen
        para el llamador (y el log)."""
        resumen = {"escribas": 0, "descartadas": 0, "estado": "",
                   "detalle": []}
        ideas_dir = str(Path(vault_dir).expanduser() / "ideas")
        job = IdeaJob(repo=repo, repo_path=repo_path, repos=tuple(repos),
                      vault_dir=ideas_dir, maximo=maximo,
                      conocidas=tuple(conocidas),
                      kind=self.spec.kind, model=self.spec.model,
                      extra_args=tuple(extra_args))
        self.run_job(job)
        resumen["estado"] = job.estado
        # El agente leyó los checkouts de verdad: si alguno quedó con
        # cambios sin commitear, la corrida lo dice — el prompt no es
        # una ley, y un repo ajeno ensuciado no se nota solo.
        for nombre, path in job.repos:
            ok, out = self.run_cmd(["git", "-C", path, "status",
                                    "--porcelain"])
            if ok and out.strip():
                self.log.write("ideas", "idea",
                               "aviso: {} quedó con cambios sin commitear "
                               "después de la corrida: {}".format(
                                   nombre,
                                   out.strip().splitlines()[0][:120]))
        if job.estado != "hecho":
            resumen["detalle"].append("agente {}: {}".format(
                job.estado, job.motivo))
            self.log.write("ideas", "idea",
                           "sin notas ({})".format(job.motivo))
            return resumen
        for d in job.datos:
            if not d["ok"]:
                resumen["descartadas"] += 1
                motivo = "descartada: {} — {}".format(
                    d["idea"].get("title") or "?", d["motivo"])
                resumen["detalle"].append(motivo)
                self.log.write("idea", "idea", motivo)
        validas = [d["idea"] for d in job.datos if d["ok"]]
        dentro, fuera = aplicar_tope(validas, maximo)
        for i in fuera:
            resumen["descartadas"] += 1
            motivo = "descartada por tope de {} notas por corrida: " \
                     "{}".format(maximo, i.get("title") or "?")
            resumen["detalle"].append(motivo)
            self.log.write("idea", "idea", motivo)
        fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for idea in dentro:
            path = self.escribir_nota(idea, fecha, ideas_dir)
            resumen["escribas"] += 1
            resumen["detalle"].append("nota: " + path)
            self.log.write("nota", "idea", path)
        self.log.write("ideas", "idea",
                       "{} idea(s) del agente: {} nota(s) escrita(s), "
                       "{} descartada(s)".format(
                           len(job.datos), resumen["escribas"],
                           resumen["descartadas"]))
        return resumen
