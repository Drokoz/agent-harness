"""Vista transversal de decisiones: "mostrame las decisiones de todos mis contextos".

Cruza dos fuentes, y sólo dos:

- los ADRs de cada repo (`docs/adr/*.md`), y
- las notas de la carpeta `decisiones/` de la vault que declara el contexto.

El harness lee la vault, no la administra: no crea ni borra nada dentro. La
memoria de Claude (`CLAUDE.md`, `~/.claude/...`) queda fuera a propósito: es
cómo trabajar con Tomás, no lo que aprende él — y por eso no se mezcla acá.

Los archivos en el disco los traen `harness.adapters` (`adr_files` y
`vault_decision_files`); todo lo de este módulo es puro y testable sin red.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Tuple

from harness.render import COLOR, PLAIN

ADR_DIR = "docs/adr"
VAULT_FOLDER = "decisiones"

_NUM_RE = re.compile(r"^(\d+)")


@dataclass
class Decision:
    """Una decisión vista de frente. `source` es la etiqueta que se dibuja:
    qué repo (`"repo:koku"`) o qué carpeta de la vault (`"vault:decisiones"`)."""

    context: str
    source: str
    id: str
    title: str
    path: str


def _heading(path):
    """El primer H1 del markdown, o None. Un archivo ilegible no rompe la vista."""
    try:
        for line in path.read_text().splitlines():
            if line.startswith("# "):
                return line[2:].strip() or None
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _nice(stem):
    """El nombre de archivo sin número ni separadores: el título de respaldo."""
    m = _NUM_RE.match(stem)
    resto = stem[m.end():] if m else stem
    return resto.replace("-", " ").replace("_", " ").strip() or stem


def adr_identity(path) -> Tuple[str, str]:
    """`(id, título)` de un ADR. Id = el número del archivo (`0001`); título
    = el primer H1, o el nombre del archivo cuando no hay."""
    stem = path.stem
    m = _NUM_RE.match(stem)
    return (m.group(1) if m else stem, _heading(path) or _nice(stem))


def note_identity(path) -> Tuple[str, str]:
    """`(id, título)` de una nota de la vault. Id = el nombre del archivo."""
    stem = path.stem
    return (stem, _heading(path) or stem)


def decisions_for(context, repos, vault_notes):
    """Las decisiones de un contexto.

    `repos`: `[(nombre, [archivos .md])]`. `vault_notes`: `[archivos .md]` o
    None cuando el contexto no declara vault. Un contexto sin vault no rompe:
    salen sus ADRs y punto.
    """
    out = []
    for nombre, archivos in repos:
        for a in archivos:
            ident = adr_identity(a)
            out.append(Decision(context, "repo:" + nombre, ident[0], ident[1], str(a)))
    if vault_notes:
        for n in vault_notes:
            ident = note_identity(n)
            out.append(
                Decision(context, "vault:" + VAULT_FOLDER, ident[0], ident[1], str(n)))
    return out


def as_list(decisions):
    """Para `--json`: la misma información, sin paths de datos en medio."""
    return [asdict(d) for d in decisions]


def _label(source):
    """Qué se dibuja como fuente: `koku/docs/adr` o `vault/decisiones`."""
    especie, _, nombre = source.partition(":")
    return nombre + "/" + ADR_DIR if especie == "repo" else "vault/" + nombre


def render_decisions(groups, color=True):
    """La vista completa. `groups`: `[(nombre_contexto, [Decision])]`."""
    c = COLOR if color else PLAIN
    total = sum(len(ds) for _, ds in groups)
    lines = ["", f" {c.bold}Decisiones{c.off}  {c.dim}{total}{c.off}"]
    for nombre, ds in groups:
        lines.append(f" {c.bold}▸ {nombre}{c.off}")
        if not ds:
            lines.append(f"   {c.dim}sin decisiones todavía{c.off}")
            continue
        for d in ds:
            lines.append(f"   {_label(d.source):<24} {d.id:<10} {d.title}")
    lines.append("")
    return "\n".join(lines) + "\n"
