#!/usr/bin/env bash
# Gate del harness: el único veredicto de "esto mergea" (PLAN.md, Fase 0).
#   exit 0 = verde (mergeable) · exit != 0 = rojo.
# Nada se salta en silencio: si un chequeo no se puede correr, es rojo, no verde.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY=python3

fail() { echo "ROJO: $*" >&2; exit 1; }

# 1) El gate corre sobre Python 3.9: la versión mínima a la que apunta el código.
echo "== python 3.9"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 9) else 1)' \
  || fail "se necesita Python 3.9 (el intérprete es: $("$PY" --version 2>&1))"

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
