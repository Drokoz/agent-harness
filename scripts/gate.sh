#!/usr/bin/env bash
# Gate del harness: el único veredicto de "esto mergea" (PLAN.md, Fase 0).
#   exit 0 = verde (mergeable) · exit != 0 = rojo.
# Nada se salta en silencio: si un chequeo no se puede correr, es rojo, no verde.
#
# Intérprete: el gate no exige que el `python3` del PATH sea 3.9; busca uno
# explícito entre GATE_PY_CANDIDATES (por defecto: python3.9 /usr/bin/python3
# python3). El chequeo de sintaxis parsea con el intérprete que corre, así
# que ese intérprete tiene que ser 3.9.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() { echo "ROJO: $*" >&2; exit 1; }

PY_CANDIDATES="${GATE_PY_CANDIDATES:-python3.9 /usr/bin/python3 python3}"
PY=""
TRIED=""
for cand in $PY_CANDIDATES; do
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 9) else 1)' >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
  if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
    ver=$("$cand" --version 2>&1 | head -n1) || true
    [ -n "$ver" ] || ver="no responde"
  else
    ver="no existe"
  fi
  TRIED="${TRIED}  ${cand} → ${ver}"$'\n'
done

# 1) Un intérprete 3.9 explícito: con él valen los chequeos de sintaxis y de stdlib.
echo "== python 3.9"
if [ -z "$PY" ]; then
  echo "ROJO: no encontré un intérprete de Python 3.9. Sin él el chequeo de sintaxis" >&2
  echo "      no detecta código 3.10+ (match, etc.). Probé:" >&2
  printf '%s' "$TRIED" >&2
  echo "Instalá uno (p.ej. brew install python@3.9) o ajustá GATE_PY_CANDIDATES." >&2
  exit 1
fi
echo "   intérprete: $PY ($("$PY" --version 2>&1))"

# 2) Suite de unittest: humo del CLI + compatibilidad (3.9, stdlib) + formato.
echo "== unittest"
out=$("$PY" -m unittest discover -s tests -v 2>&1) \
  || { echo "$out"; fail "unittest"; }
echo "$out"
echo "$out" | grep -Eq 'Ran [1-9][0-9]* tests?' \
  || fail "la suite no corrió ningún test (¿descubrimiento roto?)"

# 3) Corrida real del CLI: `harness status` sin excepción, exit 0.
echo "== harness status"
./bin/harness status >/dev/null || fail "harness status no sale 0"

echo "VERDE"
