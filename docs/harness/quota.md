# La cuota de Claude: `harness usage` (real) y `harness quota` (estimada)

Dos comandos, dos cosas distintas:

- **`harness usage`**: la cuota **real**, la de la cuenta. Las barras de
  `/usage` ("Current session", "Current week (all models)", "Current week
  (Fable)") son server-side: incluyen el consumo de otros equipos (el Mac del
  9-5, claude.ai). Es lo que usa `harness run` para decidir. Ver abajo.
- **`harness quota`**: la **estimación** local, leyendo los `.jsonl` de
  `~/.claude/projects`. Sirve para atribuir (por proyecto, por ticket,
  harness vs resto), no para saber cuánto queda: no ve otras máquinas.

Ojo con la advertencia de `/usage` "Approximate, based on local sessions on
this machine — does not include other devices": **aplica sólo a la sección
de abajo** ("What's contributing to your limits usage?"), no a las barras de
arriba. Las barras son de la cuenta; esa sección (y `harness quota`) son
locales.

## `harness usage`: la cuota real

    harness usage                  la tabla: % y reset de cada bucket, con la
                                   fuente y la antigüedad de cada dato
    harness usage --json           lo mismo, parseable
    harness usage --fuente F       auto (por defecto) | statusline | usage
    harness usage --pane ID        el pane de herdr desde el cual abrir el
                                   temporal (por defecto HERDR_PANE_ID, o el
                                   enfocado de `herdr pane list` fuera de herdr)

Fuentes, en orden (`--fuente auto`):

1. **La statusline de las sesiones del harness.** Claude Code le pasa al
   comando de statusline, por stdin, `rate_limits` (`five_hour` y
   `seven_day`, con `used_percentage` y `resets_at` en epoch).
   `scripts/statusline-harness` lo anota, atómico, en
   `~/.local/state/harness/rate_limits.json` (o `$XDG_STATE_HOME/harness/`)
   junto a la hora de captura y el `session_id`. Las sesiones de Claude que
   lanza el harness (peldaños 3 y 4) lo llevan en `--settings`
   (`dispatch.SETTINGS_CLAUDE`); la statusline personal del usuario no se
   toca (con `--setting-sources project` sus settings no entran a esas
   sesiones). Así cada ticket de Claude refresca la cuota gratis. Si el
   archivo es más nuevo que `quota.statusline_max_edad_min` (10 min), se usa
   sin abrir nada. Límites: aparece recién después de la primera respuesta
   del modelo, y no trae el bucket por modelo (Fable).
2. **`/usage`, capturado por herdr.** Abre un pane temporal (`herdr pane
   split --no-focus`, cwd = `repos.root` del contexto, una carpeta en la
   que Claude ya confía), corre `claude`, espera la TUI con `herdr pane
   wait-output`, manda `/usage`, espera "Current week", lee
   (`--source recent-unwrapped`), relee mientras diga "Refreshing…", y
   **siempre** cierra (Escape + `pane close`, también si algo falla). No
   llama al modelo. Es la única fuente en frío y la única con los buckets
   por modelo. Tarda ~3 s. `herdr pane close` es asíncrono: el pane puede
   seguir en `herdr pane list` un segundo después.

Si ninguna se puede leer, `harness usage` sale 1 con el motivo de las dos.
**Nunca inventa un número**: el parser de `/usage` (`usage.parsear_usage`)
falla explícito si falta el bloque de la sesión o de la semana, si un bucket
no trae "% used" o si un reset no se entiende. Formatos de reset aceptados:
`1:10am`, `6pm`, `18:30`, `Oct 23, 6pm`, `Oct 23 at 6pm`, `in 2h 15m`, con o
sin zona entre paréntesis (sin zona, America/Santiago).

### El `--json` (el contrato para #145, #146, #147)

```json
{
  "alcance": "cuenta",
  "fuente": "usage",
  "consultado": "2026-10-17T23:44:15-03:00",
  "sesion": {"pct": 24, "reset": "2026-10-18T01:10:00-03:00",
             "fuente": "usage", "leido": "2026-10-17T23:44:15-03:00", "edad_s": 0},
  "semana": {"pct": 7, "reset": "2026-10-23T18:00:00-03:00", "...": "..."},
  "por_modelo": {"Fable": {"pct": 0, "reset": "2026-10-23T18:00:00-03:00", "...": "..."}}
}
```

Los `%` son 0-100 como en `/usage`; los resets, ISO 8601 con offset de
Santiago. `fuente` (arriba) es `usage`, `statusline` o la combinación; cada
bucket dice la suya, cuándo se leyó y su antigüedad en segundos. Con la
statusline, `por_modelo` viene vacío. El cupo por noche (#145), el router
(#146) y la ventana de quema (#147) pueden consumir esto (o
`harness.usage.leer_cuota` directo) en vez de la estimación local.

### Lo que hace `harness run` con la cuota

`run` (y `run --loop`) consulta la cuota **antes de cada tanda que tiene
tickets de Claude** y **antes de cada ticket de Claude** (la costura
`DispatchSpec.permitir_claude`). Los tickets de Qwen no la gastan, así que
sin Claude en la tanda no se consulta nada. Cada consulta y cada decisión
quedan en el log de eventos (`tipo: cuota`), y los tickets que no entran,
como `fuera` con el motivo.

1. **Sesión de 5h** sobre `umbral_sesion`: no se lanza Claude. Si en la
   tanda hay trabajo de Qwen, Claude queda fuera de esta tanda y la noche
   sigue; si sólo queda Claude, se **espera el reset** (en tramos de 5 min,
   atendiendo el freno de `--loop`) y se vuelve a consultar. Si el reset
   cae después de `hora_limite`, no se espera: Claude no corre más en esa
   corrida.
2. **Semana**: queda `100% − usado`; se reserva `reserva_diaria` por cada
   día que falta al reset (para el 9-5 y el resto); lo que sobra es la
   holgura, y `holgura / costo_ticket` es el tope de tickets de Claude de la
   tanda. Mientras más cerca del reset y más cuota sin usar, más tickets.
   Sin holgura, ningún ticket de Claude.
3. **Fallback**: si `/usage` (y la statusline) no se pueden leer, se usa la
   estimación de `harness quota` (sólo la semana, y sólo con
   `quota.tope_semanal`), y se anota en el log que se cayó a ella y por qué.
   Sin estimación tampoco, no hay tope por cuota (lo de antes).

El juez de `proponer` (#115) usa el `%` real de la semana contra
`--piso-cuota`, y con la sesión de 5h sobre el umbral corre en Qwen; si la
cuota real no se lee, decide con la estimación como antes.

### Umbrales (config del contexto, `quota.*`)

| Campo | Default | Qué es |
|---|---|---|
| `umbral_sesion` | `0.90` | fracción de la sesión de 5h desde la cual no se lanza Claude |
| `reserva_diaria` | `0.08` | fracción de la semana que se deja libre por día al reset (con 7 días, 56%; la última noche, casi nada) |
| `costo_ticket` | `0.02` | fracción de la semana que se estima que se come un ticket de Claude: dimensiona la tanda |
| `hora_limite` | `"07:00"` | HH:MM America/Santiago: la espera del reset de la sesión no pasa de acá |
| `statusline_max_edad_min` | `10` | minutos que vale el archivo de la statusline antes de capturar `/usage` |

Ejemplo de la cuenta: semana al 20% con 2 días al reset → queda 80%,
reserva 16%, holgura 64% → hasta 32 tickets de Claude. Semana al 70% con 6
días → reserva 48% > 30%: ninguno.

### Fragilidad

El texto de `/usage` no tiene contrato. Si cambia, `harness usage` falla con
un mensaje que dice qué bloque o qué línea no entendió, y `run` cae a la
estimación (anotado). Las fixtures reales están en `tests/fixtures/usage_*.txt`:
si el formato cambia, se recaptura ahí. La statusline (`rate_limits`) es
JSON estructurado y bastante menos frágil.

## `harness quota`: la estimación local

`harness quota` lee las sesiones locales (`~/.claude/projects/**/*.jsonl`) y
mide la cuota sola, sin anotar nada a mano. Cada línea de mensaje asistente trae el modelo y el `usage`
completo (`input`, `cache_creation`, `cache_read`, `output`, `thinking` — el
thinking va dentro del output y no suma dos veces).

## Uso

    harness quota                  la tabla corta
    harness quota --json           el agregado (crudo + ponderado)
    harness quota --projects RUTAS otra(s) carpeta(s) de sesiones, separadas
                                   por coma (tests, otro usuario de la máquina)

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
- **por modelo**: el total histórico de cada modelo, **ponderado** (la misma
  unidad que el total —ticket #74: antes el desglose sumaba crudos contra un
  total ponderado). Cada modelo se mide por separado, así que un ticket
  corrido con `--model fable` queda medido con su propio total y su propia
  ventana de 5h: se ve si Fable consume su bucket (la semana de Fable que
  muestra `/usage`) y cuánto queda del general.
- **pico en 5h**: el máximo de tokens dentro de *cualquier* ventana de 5
  horas, por modelo, en crudo (está marcado como tal en la tabla). Es la
  ventana rodante de la cuota de 5h: es lo comparable contra el "% used ·
  resets ..." de `/usage`.
- **por semana**: el total ponderado por semana, con el reset de **viernes
  21:00 UTC** (#148: el reset está fijo en UTC; en Santiago es 17:00 con
  UTC-4 y 18:00 en horario de verano, como dice `/usage`). Cada semana se
  muestra con su inicio en hora local real. El corte por
  semana es el que contesta "cuánto vamos esta semana", y por eso importa
  que esté ponderado: crudo, quedaba dominado por `cache_read`.
- **por día**: el total ponderado por fecha calendario en America/Santiago
  (la hora local del contexto), con los últimos 10 días en la tabla. Es la
  pregunta que se hace en la práctica: "¿cuánto gastamos hoy?" El `--json`
  trae todos los días, no sólo los 10.
- **por proyecto / ticket**: el total ponderado por proyecto; la carpeta de
  la sesión codifica el path del worktree
  (`...--worktrees-<repo>-ticket-<n>`), así que el consumo se atribuye por
  proyecto y, cuando el nombre lo permite, por ticket.
- **harness vs resto**: los worktrees bajo `<repos.root>/.worktrees/` son del
  harness; todo lo demás es el resto. Es la línea que responde "cuánto se
  comió el harness y cuánto el 9-5".

## Varias fuentes: los usuarios de la máquina (#75)

Por defecto la cuota mira un solo usuario del sistema (`~/.claude/projects`),
o sea: si hay dos usuarios que usan Claude Code contra la misma cuenta, la
medición está incompleta **por diseño**. Se declara la lista de fuentes de
dos formas, y `--projects` gana sobre la config:

- **config**: `quota.projects`, una lista de rutas en el contexto
  (`docs/harness/config.md`).
- **CLI**: `--projects RUTA1,RUTA2` separadas por coma.

Una fuente que no se puede leer **no rompe el comando**: se salta, y la salida
dice cuál y por qué (`no existe`, `no es un directorio`, `no se puede acceder
(permiso denegado)`), junto al recuento de fuentes leídas:

```
  fuentes: 1/2 leídas — el total es un piso
    falta /Users/tomasherceg/.claude/projects: no se puede acceder (permiso denegado)
  total ponderado ≥ 20,350 tokens (por costo relativo, ver PESOS) · piso
```

Con fuentes faltantes el total se marca como **piso (`≥`)**, no como medición:
un número que subestima sistemáticamente no puede leerse como exacto. En
`--json` la marca es `"completa": false` junto a `"fuentes": [{ruta, ok,
error}]`.

Contrato para consumidores: un total con `completa: false` es un piso, y no se
usa para autorizar gasto (p.ej. el piso de cuota del router, #45) salvo que la
config lo permita explícitamente.

## El porcentaje del tope: la constante de calibración

Con la constante configurada, cada corte muestra además el **porcentaje
estimado del tope semanal** —la unidad en la que se piensa. Sin ella, la
tabla no inventa un porcentaje: muestra los tokens y dice que falta
calibrar.

La constante vive en config, no en el código (`docs/harness/config.md`):

```json
"personal": {
  "tracker": {"kind": "github"},
  ...
  "quota": {"tope_semanal": 900000000}
}
```

`tope_semanal` es el tope semanal estimado en tokens ponderados, y sale de
leer `/usage` (interactivo: lo mide un humano, no el agente). Medición del
2026-08-24: la semana desde el viernes 21 a las 17:00 marcaba 25% con
224.880.155 tokens ponderados —o sea ~9.0M ponderados por punto y un tope
semanal de ~900M con el promo de +50% vigente hasta el 31-ago; sin promo,
~600M. Re-medir después del 4-sep y actualizar la config.

Cada corrida escribe una línea `tipo: quota` en el log de eventos
(`~/.local/state/harness/events.jsonl`), para que el histórico quede junto a
todo lo demás. En modo offline (`HARNESS_OFFLINE=1`) no escribe: el gate no
tiene efectos de lado.

## Limitaciones (las de la sección local de `/usage`, no las de sus barras)

1. **Es aproximado**: la fuente son las sesiones locales, no el servidor.
   Para la cuota real, `harness usage`.
2. **Sólo ve las sesiones locales de esta máquina**: no ve otros dispositivos
   ni (claude.ai). Otros usuarios de la máquina se declaran con
   `quota.projects`/`--projects` (#75); uno ilegible se salta y el total se
   marca como piso.

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

`harness quota --json` devuelve el agregado: `fuentes` (una entrada por fuente
declarada: `ruta`, `ok`, `error` — #75), `completa` (false = falta alguna
fuente y el total es un piso, no una medición), `total` (con sus cuatro
componentes intactos), `ponderado` (el mismo total pesado por `PESOS`),
`por_modelo` (con `mensajes` y `ponderado` por modelo), `pico_5h` (total +
inicio y fin de la ventana), `por_semana` (crudo, clave = inicio de semana en
ISO con offset SCL, por modelo), `por_semana_cuota` (la misma semana pero
ponderada y partida `harness`/`resto`), `por_semana_ponderado` (la misma
semana ponderada por modelo, la que dibuja la tabla —ticket #74), `por_dia`
(ponderado, partido `harness`/`resto`, clave = fecha calendario en
America/Santiago — la "noche" del harness), `por_proyecto` (claves
`[harness]` para los worktrees, con `ponderado`, `tickets` crudo y
`tickets_ponderado`), `harness`, `resto`, `archivos`, `mensajes`. El crudo no
se pierde: la ponderación es una vista, no una pérdida de dato.

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
