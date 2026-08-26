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
# Con la frontera vacía no corta: espera, porque un issue etiquetado a las 4am
# tiene que encontrarlo vivo.
#
# Para frenarlo desde otra terminal:  touch /tmp/harness-stop
# Un freno que quedó de una corrida vieja no arranca: sale != 0 y avisa por
# stderr. Salir 0 al segundo se lee como "salió bien" y se come la noche.
set -uo pipefail

CONTEXTO="${CONTEXTO:-harness}"
MAX="${MAX:-2}"
PISO_USD="${PISO_USD:-3}"          # no arranca otra pasada por debajo de esto
PASADAS_MAX="${PASADAS_MAX:-12}"   # tope duro de pasadas CON trabajo
ESPERA="${ESPERA:-60}"             # segundos entre pasadas con trabajo
# Una frontera vacía no es el fin de la noche, es una espera (#101): un issue
# etiquetado a las 4am tiene que encontrar el bucle todavía vivo. Esperar no
# gasta pasada -- `PASADAS_MAX` es el tope contra un bucle de TRABAJO
# descontrolado, no contra estar mirando.
ESPERA_VACIA="${ESPERA_VACIA:-300}"        # no tiene sentido preguntar cada minuto
VUELTAS_VACIAS_MAX="${VUELTAS_VACIAS_MAX:-60}"   # 60 x 300s = 5 horas de espera
FRENO="${FRENO:-/tmp/harness-stop}"
# La costura para los tests: sin esto, correr este script ES despachar agentes
# de verdad. Ya pasó una vez (2026-08-25): un test lo corrió, despachó #30 y
# #56, y el timeout del test mató al dispatcher dejando dos agentes vivos sin
# watchdog ni cosecha.
HARNESS_BIN="${HARNESS_BIN:-./bin/harness}"
LOG="${LOG:-$HOME/.local/state/harness/noche-$(date +%Y%m%d-%H%M).log}"

mkdir -p "$(dirname "$LOG")"
cd "$(dirname "$0")/.." || exit 1

decir() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }

# CREDITO_FAKE es la costura para los tests: sin ella esto es una llamada de red.
credito() {
  if [[ -n "${CREDITO_FAKE:-}" ]]; then printf '%s\n' "$CREDITO_FAKE"; return; fi
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

if [[ -e "$FRENO" ]]; then
  {
    echo "noche.sh: no arranco, hay un freno puesto."
    echo "  archivo: $FRENO"
    echo "  puesto:  $(date -r "$FRENO" '+%Y-%m-%d %H:%M' 2>/dev/null || echo '?')"
    echo "  si es de una corrida vieja:  rm $FRENO"
  } >&2
  decir "freno encontrado al arrancar ($FRENO): no arranco"
  exit 2
fi

decir "inicio · contexto=$CONTEXTO max=$MAX piso=US\$$PISO_USD tope=$PASADAS_MAX pasadas"
decir "log: $LOG"

pasadas=0      # las que encontraron trabajo
vacias=0       # las seguidas sin nada en la frontera

while :; do
  if [[ -e "$FRENO" ]]; then
    decir "freno encontrado ($FRENO): corto"; break
  fi

  c=$(credito)
  if awk -v c="$c" -v p="$PISO_USD" 'BEGIN{exit !(c < p)}'; then
    decir "credito US\$$c bajo el piso de US\$$PISO_USD: corto"; break
  fi

  if (( pasadas >= PASADAS_MAX )); then
    decir "tope de $PASADAS_MAX pasadas con trabajo: corto"; break
  fi
  if (( vacias >= VUELTAS_VACIAS_MAX )); then
    decir "nada nuevo en $vacias vueltas: corto"; break
  fi

  decir "pasada $((pasadas + 1))/$PASADAS_MAX · credito US\$$c"
  salida=$("$HARNESS_BIN" run --context "$CONTEXTO" --max "$MAX" 2>&1)
  printf '%s\n' "$salida" >> "$LOG"
  printf '%s\n' "$salida" | grep -E "^  (OK|ABANDONADO)" | while read -r l; do decir "  $l"; done

  if printf '%s' "$salida" | grep -q "nada en la frontera"; then
    vacias=$((vacias + 1))
    decir "frontera vacia (vuelta $vacias/$VUELTAS_VACIAS_MAX): espero ${ESPERA_VACIA}s por si aparece algo"
    sleep "$ESPERA_VACIA"
    continue
  fi

  pasadas=$((pasadas + 1))
  vacias=0
  decir "espero ${ESPERA}s antes de la proxima pasada"
  sleep "$ESPERA"
done

decir "fin · credito final US\$$(credito)"
decir "a la manana: harness status --context $CONTEXTO"
