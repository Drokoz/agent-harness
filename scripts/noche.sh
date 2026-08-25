#!/usr/bin/env bash
# noche.sh — corre `harness run` en bucle hasta que la frontera se vacíe,
# el presupuesto baje del piso, o aparezca el archivo de freno.
#
# `harness run` es una sola pasada: calcula los jobs al arrancar y no vuelve
# a mirar. La escalera de reintentos (#38) sube de peldaño leyendo el log,
# así que sólo escala cuando se lo vuelve a invocar. Este bucle es lo que
# convierte la escalera en algo que trabaja de noche.
#
#   caffeinate -imsu ./scripts/noche.sh
#
# Para frenarlo desde otra terminal:  touch /tmp/harness-stop
set -uo pipefail

CONTEXTO="${CONTEXTO:-harness}"
MAX="${MAX:-2}"
PISO_USD="${PISO_USD:-3}"          # no arranca otra pasada por debajo de esto
PASADAS_MAX="${PASADAS_MAX:-12}"   # tope duro: nunca un bucle sin límite
ESPERA="${ESPERA:-60}"             # segundos entre pasadas
FRENO="${FRENO:-/tmp/harness-stop}"
LOG="${LOG:-$HOME/.local/state/harness/noche-$(date +%Y%m%d-%H%M).log}"

mkdir -p "$(dirname "$LOG")"
cd "$(dirname "$0")/.." || exit 1

decir() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }

credito() {
  python3 - <<'PY' 2>/dev/null || echo 0
import json, pathlib, urllib.request
k = json.loads((pathlib.Path.home()/'.pi/agent/models.json').read_text())
k = k['providers']['openrouter']['apiKey']
r = urllib.request.Request('https://openrouter.ai/api/v1/credits',
                           headers={'Authorization': 'Bearer ' + k})
d = json.load(urllib.request.urlopen(r, timeout=20))['data']
print('%.2f' % (d.get('total_credits', 0) - d['total_usage']))
PY
}

decir "inicio · contexto=$CONTEXTO max=$MAX piso=US\$$PISO_USD tope=$PASADAS_MAX pasadas"
decir "log: $LOG"

for ((i = 1; i <= PASADAS_MAX; i++)); do
  if [[ -e "$FRENO" ]]; then
    decir "freno encontrado ($FRENO): corto"; break
  fi

  c=$(credito)
  if awk -v c="$c" -v p="$PISO_USD" 'BEGIN{exit !(c < p)}'; then
    decir "credito US\$$c bajo el piso de US\$$PISO_USD: corto"; break
  fi

  decir "pasada $i/$PASADAS_MAX · credito US\$$c"
  salida=$(./bin/harness run --context "$CONTEXTO" --max "$MAX" 2>&1)
  printf '%s\n' "$salida" >> "$LOG"
  printf '%s\n' "$salida" | grep -E "^  (OK|ABANDONADO)" | while read -r l; do decir "  $l"; done

  if printf '%s' "$salida" | grep -q "nada en la frontera"; then
    decir "frontera vacia: no queda nada por despachar"; break
  fi

  decir "espero ${ESPERA}s antes de la proxima pasada"
  sleep "$ESPERA"
done

decir "fin · credito final US\$$(credito)"
decir "a la manana: harness status --context $CONTEXTO"
