# `harness quota`: la cuota de Claude por las sesiones locales

El `/usage` de Claude Code dice "based on local sessions on this machine": calcula
el consumo leyendo los `.jsonl` de las sesiones locales. `harness quota` lee la
misma fuente (`~/.claude/projects/**/*.jsonl`) y mide la cuota sola, sin anotar
nada a mano. Cada línea de mensaje asistente trae el modelo y el `usage`
completo (`input`, `cache_creation`, `cache_read`, `output`, `thinking` — el
thinking va dentro del output y no suma dos veces).

## Uso

    harness quota                  la tabla corta
    harness quota --json           el agregado crudo
    harness quota --projects PATH  otra carpeta de sesiones (tests, otra máquina)

Qué dice la tabla:

- **total ponderado**: no la suma cruda de los cuatro componentes —
  `input`, `cache_creation`, `cache_read`, `output`— sino cada uno pesado por
  su costo relativo (`PESOS` en `harness/quota.py`, comentario ahí de dónde
  salen los ratios: input=1x, cache write≈1.25x, cache read≈0.1x, output≈5x,
  del precio por millón de tokens de la API de Claude). Es la línea que
  arregla el ticket #54: la primera corrida real dio 13.617.021.213 tokens
  porque sumaba `cache_read` crudo —que se re-cuenta entero en cada
  mensaje— como si pesara lo mismo que un `input`. Abajo del ponderado, la
  tabla muestra el desglose crudo de los cuatro componentes; `--json` los
  trae siempre completos, sin perder el dato: la ponderación es una vista.
- **por modelo**: el total histórico de cada modelo (crudo, sin ponderar).
  Cada modelo se mide por separado, así que un ticket corrido con `--model
  fable` queda medido con su propio total y su propia ventana de 5h: se ve
  si Fable consume su bucket (la semana de Fable que muestra `/usage`) y
  cuánto queda del general.
- **pico en 5h**: el máximo de tokens dentro de *cualquier* ventana de 5
  horas, por modelo. Es la ventana rodante de la cuota de 5h: es lo comparable
  contra el "% used · resets ..." de `/usage`.
- **por semana**: el total por semana, con el reset de **viernes 17:00
  America/Santiago** (la "Current week" de `/usage`).
- **por proyecto / ticket**: la carpeta de la sesión codifica el path del
  worktree (`...--worktrees-<repo>-ticket-<n>`), así que el consumo se
  atribuye por proyecto y, cuando el nombre lo permite, por ticket.
- **harness vs resto**: los worktrees bajo `<repos.root>/.worktrees/` son del
  harness; todo lo demás es el resto. Es la línea que responde "cuánto se
  comió el harness y cuánto el 9-5".

Cada corrida escribe una línea `tipo: quota` en el log de eventos
(`~/.local/state/harness/events.jsonl`), para que el histórico quede junto a
todo lo demás. En modo offline (`HARNESS_OFFLINE=1`) no escribe: el gate no
tiene efectos de lado.

## Limitaciones (las mismas que `/usage`)

1. **Es aproximado**: la fuente son las sesiones locales, no el servidor.
2. **Sólo ve este usuario de esta máquina**: no ve otros dispositivos ni otros
   usuarios de la máquina (ni claude.ai).

Están en el `--help` de la CLI, en el epígrafe de esta doc y al pie de la
tabla.

## Topes vigentes

Para que las semanas de la tabla sean comparables: hay un **promo de +50%
hasta el 2026-08-31**. Es decir, desde el 2026-08-23 (cuando se anotó esto) el
tope efectivo es 1,5× el base; las semanas que empiecen el viernes 2026-08-28
y el viernes 2026-09-04 cruzan el fin del promo y hay que leerlas a medias.
`/usage` no divulga el tope en tokens, así que la medida directa son los %;
esta herramienta da los tokens absolutos para comparar semanas entre sí.

## Calibración contra `/usage`

Procedimiento: correr `harness quota --json`, y con `claude -p "/usage"`
capturar los `%` de la ventana de 5h, de la semana (all models) y de la semana
de Fable, y anotarlos acá con la fecha. Dos veces, separadas en el tiempo.

### Calibración 1 — 2026-08-23

`/usage` (capturado con `claude -p "/usage"` dos veces, ~25 min de distancia;
sin actividad de Claude Code entre tanto, por lo que el % no se movió):

| Medida | `/usage` | `harness quota` |
|---|---|---|
| Ventana 5h | 21% (reset vie 2026-08-24 01:09 SCL) | 170.670.050 tokens de opus-5 en la ventana de 5h equivalente (2026-08-23 22:40 → 2026-08-24 03:40 UTC) |
| Semana (all models) | 18% (reset vie 2026-08-28 17:00 SCL) | 1.238.167.237 tokens desde el 2026-08-21 17:00 SCL (100% opus-5) |
| Semana (Fable) | 0% | 0 tokens de fable en la semana |

Lectura: la escala coincide (de los 18% de semana se desprende un tope
implícito de ~6,9 mil millones de tokens, y el pico de 5h histórico de esta
máquina, 952M, se ubica en el rango coherente con el 21% de 5h). El `0%` de
Fable es exacto: no hay mensajes de fable en la semana.

### Calibración 2 — pendiente

`/usage` es server-side: hay que repetirla separada en el tiempo (mínimo
después de un reset de semana, idealmente cruzando el fin del promo del
2026-08-31 para calibrar el salto de tope), con actividad entre tanto. El
agente que implementó #39 no puede esperar una semana, así que queda anotado
para Tomás o para la próxima corrida de agentes.

## Forma del `--json`

`harness quota --json` devuelve el agregado crudo: `total` (con sus cuatro
componentes intactos), `ponderado` (el mismo total pesado por `PESOS`),
`por_modelo` (con `mensajes`), `pico_5h` (total + inicio y fin de la
ventana), `por_semana` (clave = inicio de semana en ISO con offset SCL, por
modelo, crudo), `por_semana_cuota` (la misma semana pero ponderada y
partida `harness`/`resto`), `por_dia` (ponderado, partido `harness`/`resto`,
clave = fecha calendario en America/Santiago — la "noche" del harness),
`por_proyecto` (claves `[harness]` para los worktrees, con `tickets` crudo
y `tickets_ponderado`), `harness`, `resto`, `archivos`, `mensajes`.

## La cuota en `status` y `report` (#47)

`harness status` y `harness report` leen la misma fuente (sin `--projects`:
siempre `~/.claude/projects`) y cruzan el resultado contra el log de
eventos para mostrar, del resumen de la mañana: la semana de cuota vigente
(total y cuánto es del harness), el costo de cada ticket en las dos
monedas —dólares de OpenRouter y tokens de Claude ponderados— y, si el
ticket escaló (#38), el desglose por peldaño. `report` además dibuja la
evolución por noche (`por_dia`) en su propia sección. En modo offline
(`HARNESS_OFFLINE=1`) no se calcula nada de esto: la pantalla lo nota y
sigue, no se rompe. La función pura que hace el cruce es
`harness.summary.con_cuota`; el cableado (leer sesiones, `state.pasos`,
`quota.puntos_de_ticket`/`cuota_por_ventana`) vive en `bin/harness`.
