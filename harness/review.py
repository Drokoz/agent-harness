"""El revisor barato sobre el diff, antes del merge humano (#46).

De los diez PRs de la primera corrida, dos no debían mergearse y sólo se veía
leyendo el diff. El recurso escaso a la mañana son los minutos de la persona,
así que un filtro barato antes de esa lectura se paga solo.

Es el único lugar del harness donde un segundo par de ojos LLM aporta algo que
el gate no puede dar: el gate dice si pasa, no si está bien. Para cada PR con
gate verde, un agente read-only (el runner barato, pi por default) revisa el
diff y devuelve como mucho 10 hallazgos, ordenados por gravedad.

El revisor no escribe nada —ni commits, ni comentarios en el PR, ni labels—,
y nunca bloquea: si falla o se cuelga, el fallo queda anotado como `revisión
fallida` en el log de eventos y el PR igual aparece en el resumen. Un PR sin
hallazgos se marca como tal: también es información.

El módulo es testeable sin agente ni red: `Revisor` recibe el mundo como
callable (`run_cmd`, la misma costura del dispatcher) y los tests inyectan
falsos. El formato del cuerpo de la línea `review` vive acá y en ningún otro
lugar: `dispatch` lo escribe con `cuerpo_revision` y `summary` lo lee con
`parsear_revision`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

# La promesa del ticket: como mucho 10 hallazgos, ordenados por gravedad.
MAX_HALLAZGOS = 10

# Las gravedades, de la más pesada a la más leve. Con lo que se ordena y con
# lo que se muestra en el resumen.
GRAVEDADES = ("alta", "media", "baja")
_RANK = {g: i for i, g in enumerate(GRAVEDADES)}

# El prompt es machine-facing (inglés), así que el revisor puede responder en
# cualquiera de los dos idiomas. Lo que no se reconoce cae a `baja`: un
# hallazgo que no sabe gritar no debe tapar los que sí.
_NORMA = {
    "alta": "alta", "high": "alta", "blocker": "alta", "critical": "alta",
    "severe": "alta", "mayor": "alta",
    "media": "media", "medium": "media", "moderate": "media",
    "warning": "media",
    "baja": "baja", "low": "baja", "minor": "baja", "info": "baja",
    "nit": "baja",
}


@dataclass
class Hallazgo:
    """Un hallazgo del revisor, ya normalizado y dentro del contrato."""

    gravedad: str   # "alta" | "media" | "baja"
    archivo: str = ""
    detalle: str = ""

    @property
    def linea(self):
        """La línea lista para el resumen: `[alta] archivo.py: detalle`."""
        donde = "{}: ".format(self.archivo) if self.archivo else ""
        return "[{}] {}{}".format(self.gravedad, donde, self.detalle)


@dataclass
class Revision:
    """El resultado de revisar un PR.

    `estado`: "ok" (con hallazgos), "sin_hallazgos" (el revisor miró y no
    encontró nada que decir: información, no falta) o "fallida" (no hay
    veredicto: el PR igual aparece en el resumen, con `motivo` contando el
    porqué).
    """

    estado: str
    hallazgos: List[Hallazgo] = field(default_factory=list)
    motivo: str = ""


# --------------------------------------------------------------------- puros
def normalizar_gravedad(s):
    """La gravedad a como se guarda: "alta" | "media" | "baja".

    Acepta lo que un LLM escribe en inglés o en castellano; lo que no se
    reconoce cae a "baja" (ver `_NORMA`). Nunca levanta."""
    return _NORMA.get(str(s or "").strip().lower(), "baja")


def ordenar(hallazgos, maximo=MAX_HALLAZGOS):
    """Los hallazgos ordenados por gravedad (la más pesada primero), estables
    dentro de cada grado, acotados a `maximo`."""
    if maximo is None:
        return sorted(hallazgos, key=lambda h: _RANK.get(h.gravedad, 99))
    return sorted(hallazgos, key=lambda h: _RANK.get(h.gravedad, 99))[:maximo]


def _primer_str(d, claves):
    """El primer valor string no vacío entre `claves`, o ""."""
    for clave in claves:
        v = d.get(clave)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _hallazgo_de(item):
    """Un objeto del JSON del revisor a `Hallazgo`, o None si no dice nada."""
    if not isinstance(item, dict):
        return None
    detalle = _primer_str(item, ("issue", "detail", "detalle", "message",
                                 "note", "description", "text"))
    if not detalle:
        return None
    archivo = _primer_str(item, ("file", "path", "archivo"))
    gravedad = normalizar_gravedad(
        item.get("severity", item.get("gravedad", item.get("nivel"))))
    return Hallazgo(gravedad=gravedad, archivo=archivo, detalle=detalle)


def _arreglo_json(texto):
    """El arreglo JSON dentro de `texto`, o None si no hay ninguno.

    El contrato del prompt es "SOLO el arreglo JSON", pero un LLM a veces
    envuelve la respuesta en prosa o en un objeto; se busca el arreglo en vez
    de tirarse. Un objeto que traiga una lista en `findings`/`hallazgos`/
    `results` también coopera."""
    texto = (texto or "").strip()
    try:
        data = json.loads(texto)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for clave in ("findings", "hallazgos", "results"):
                v = data.get(clave)
                if isinstance(v, list):
                    return v
    except ValueError:
        pass
    ini, fin = texto.find("["), texto.rfind("]")
    if 0 <= ini < fin:
        try:
            data = json.loads(texto[ini:fin + 1])
        except ValueError:
            return None
        if isinstance(data, list):
            return data
    return None


def parsear_hallazgos(texto):
    """El JSON del revisor a una lista de `Hallazgo` (posiblemente vacía),
    ya ordenada y acotada.

    `None` cuando el texto no trae un arreglo reconocible: la diferencia con
    `[]` es todo el punto. `[]` es el veredicto "no hay nada que decir" (un PR
    sin hallazgos, información); `None` es una respuesta que no se entiende
    (una revisión fallida).
    """
    data = _arreglo_json(texto)
    if data is None:
        return None
    return ordenar([h for h in (_hallazgo_de(x) for x in data) if h is not None])


# --------------------------------------------------------------------- prompt
def prompt_review(slug, numero, diff):
    """El trabajo del revisor. Machine-facing: en inglés, una sola línea de
    instrucción más el diff tal cual (a diferencia del prompt de herdr, esto
    va por CLI y los saltos de línea del diff se conservan)."""
    instruccion = (
        "You are a read-only code reviewer for PR #{} of {} before its human "
        "merge. Review the diff below. You must not write anything: no "
        "commits, no PR comments, no labels, no files. Answer with ONLY a "
        "JSON array, no prose, of at most {} findings ordered by severity "
        "(most severe first). Each finding is an object: "
        '{{"severity": "high"|"medium"|"low", "file": "<path>", '
        '"issue": "<one sentence>"}}. '
        "Only report real problems in this diff (bugs, regressions, "
        "security, correctness); do not report style. If there are no real "
        "problems, answer [].\nDIFF:\n".format(numero, slug, MAX_HALLAZGOS)
    )
    return instruccion + diff


# --------------------------------------------------------------------- cuerpo
# El cuerpo de la línea `review` del log de eventos. Tres formas, y una sola
# definición del formato: acá. `dispatch` lo escribe, `summary` lo lee.
#   revisión PR #19: 2 hallazgo(s) [["alta", "a.py", "bug"], ["baja", "", "nit"]]
#   revisión PR #19: sin hallazgos
#   revisión PR #19: fallida — no se pudo leer el diff del PR #19

def cuerpo_revision(numero, revision):
    """El cuerpo de la línea `review` para el log de eventos."""
    pref = "revisión PR #{}: ".format(numero)
    if revision.estado == "fallida":
        return pref + "fallida — " + (revision.motivo or "sin motivo")
    if not revision.hallazgos:
        return pref + "sin hallazgos"
    triples = [[h.gravedad, h.archivo, h.detalle] for h in revision.hallazgos]
    return (pref + "{} hallazgo(s) {}".format(
        len(triples), json.dumps(triples, ensure_ascii=False)))


_RE_REVISION = re.compile(r"^revisión PR #\d+: (.*)$")


def parsear_revision(cuerpo):
    """El cuerpo de una línea `review` de vuelta a `Revision`, o None si el
    cuerpo no es de una revisión.

    Ningún formato que no se entienda se vuelve un veredicto: se devuelve
    None y el llamador ignora la línea. Un log con una línea extraña no
    puede convertir un PR revisado en fallido (o en limpio) por error de
    redacción."""
    m = _RE_REVISION.match((cuerpo or "").strip())
    if not m:
        return None
    resto = m.group(1)
    if resto.startswith("fallida — "):
        return Revision("fallida", motivo=resto[len("fallida — "):])
    if resto == "sin hallazgos":
        return Revision("sin_hallazgos")
    ini = resto.find("[")
    if ini < 0:
        return None
    try:
        triples = json.loads(resto[ini:])
    except ValueError:
        return None
    if not isinstance(triples, list):
        return None
    hallazgos = []
    for t in triples:
        if (isinstance(t, (list, tuple)) and len(t) == 3
                and t[0] in _RANK and isinstance(t[2], str) and t[2]):
            hallazgos.append(Hallazgo(t[0], str(t[1] or ""), t[2]))
    if triples and not hallazgos:
        return None
    if not hallazgos:
        return Revision("sin_hallazgos")
    return Revision("ok", hallazgos=hallazgos)


# --------------------------------------------------------------------- revisor
class Revisor:
    """El agente read-only sobre el diff de un PR (#46).

    El mundo entra por `run_cmd` —la misma costura del dispatcher:
    `(args, cwd=None, timeout=30) -> (ok, out)`— para que los tests inyecten
    un falso sin agente ni red. Por default corre con el runner barato
    (pi/Qwen), no con la escalera del ticket: la review es un filtro de
    minutos de la persona, no trabajo que se re-escala.

    Nada de lo que pase acá toca el PR: el revisor recibe el diff por texto y
    devuelve texto; en pi, la CLI se corre con **sólo** tools de lectura
    (`--tools read,grep,find,ls`), así que la promesa de "no escribe nada" no
    va sólo en el prompt, va en la CLI.
    """

    TOOLS_LECTURA = "read,grep,find,ls"

    def __init__(self, run_cmd, kind="pi", model="qwen/qwen3.8-27b",
                 extra_args=(), timeout=600, diff_max=100_000):
        self.run_cmd = run_cmd
        self.kind = kind
        self.model = model
        self.extra_args = tuple(extra_args)
        self.timeout = timeout
        self.diff_max = diff_max

    def _comando(self, prompt):
        """Con qué CLI corre el revisor. Los flags van antes del prompt;
        en pi, la restricción de lectura va en la CLI, no en el prompt."""
        cmd = [self.kind]
        if self.kind == "pi":
            cmd += ["--tools", self.TOOLS_LECTURA]
        if self.model:
            cmd += ["--model", self.model]
        cmd += list(self.extra_args)
        return cmd + ["-p", prompt]

    def revisar_pr(self, slug, numero):
        """La review de un PR: leer el diff, correr al revisor, parsear.

        Devuelve `Revision` en los tres estados; nunca levanta por un fallo
        del mundo —lo clasifica como `fallida` con su motivo—, porque el
        llamador (el dispatcher) no puede permitirse que la review bloquee al
        job que ya terminó bien."""
        ok, out = self.run_cmd(["gh", "pr", "diff", str(numero), "-R", slug],
                               timeout=120)
        if not ok or not out.strip():
            return Revision(
                "fallida",
                motivo="no se pudo leer el diff del PR #{}".format(numero))
        diff = out
        if len(diff) > self.diff_max:
            # Un diff gigante no cabe en el contexto del runner barato y no
            # agrega: se trunca y se dice, en vez de fallar la review.
            diff = diff[:self.diff_max] + "\n… (diff truncado)"
        ok, out = self.run_cmd(self._comando(prompt_review(slug, numero, diff)),
                               timeout=self.timeout)
        if not ok:
            cola = out.strip()[-120:]
            return Revision(
                "fallida",
                motivo="el revisor no respondio"
                + (": " + cola if cola else ""))
        hallazgos = parsear_hallazgos(out)
        if hallazgos is None:
            return Revision(
                "fallida",
                motivo="el revisor no devolvio el JSON de hallazgos")
        if not hallazgos:
            return Revision("sin_hallazgos")
        return Revision("ok", hallazgos=hallazgos)
