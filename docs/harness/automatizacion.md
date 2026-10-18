# El harness contra las seis reglas de automatización confiable (#152)

Auditoría del harness contra las seis reglas de
[Building effective agent automations](https://claude.dev/blog/building-effective-agent-automations/),
con "publicar en Slack" reemplazado por "mergear". Por cada regla: dónde se cumple
(archivo:línea) o por qué no. Lo que no se cumple no se describe, se despacha: un issue
`ready-for-agent` por incumplimiento, enlazado abajo y con este ticket (#152) como parent.

Fecha: 2026-10-18. Evidencia leída de la rama actual (`harness/*.py`, `bin/harness`,
`extensions/harness-guard/`); las líneas apuntan a esa rama y pueden moverse en el rebase.

## Regla 1 — Leer cada fuente desde un marcador, no desde una ventana fija de tiempo

**Cumple.** Cada fuente de lectura lleva su propio cursor, y el cursor se avanza al leer:

| Fuente | Dónde |
|---|---|
| El log de eventos del dispatcher | marcador `last_viewed` junto al log: se lee `bin/harness:647`, se avanza `bin/harness:653` (salvo `--since`, que mira otro período a propósito y no lo mueve). Sin marca: desde el principio, no desde una ventana inventada |
| Los eventos de presupuesto de reloj | la ventana se declara explícita: `INICIO_NOCHE` (`bin/harness:619`) se pasa a `state.minutos_de_ticket(..., desde=)` (`harness/state.py:120`). Ventana declarada, no fija ni oculta |
| El tracker (issues/PRs) | no tiene marcador porque no hay stream que consumir: el snapshot lo lee en vivo completo y su estado se deriva (`harness/snapshot.py:337`). Leer "todo lo abierto ahora" es el marcador correcto de una frontera |
| Los agentes vivos de herdr | fuente instantánea (¿quién vive ahora?), no un stream: sin marcador aplica |
| La cuota de Claude (`/usage`) | estado instantáneo de la cuenta, no un stream: sin marcador aplica |

El anti-patrón de la regla — una ventana fija de "las últimas N horas" que se pierde lo
que entró antes y cuenta dos veces lo que entró dos veces — no aparece en ninguna fuente.

## Regla 2 — Una fuente que no se pudo leer se reporta como ilegible, nunca como "no había nada"

**Cumple.** La distinción `None` (no se pudo leer) vs `[]` (leído, y no hay nada) se hace
en los adaptadores y sobrevive hasta la pantalla y el exit code. Casos:

- **El tracker**: `issues: None` degrada el repo (`harness/snapshot.py:381-382`);
  `Repo.tracker_ilegible` (`harness/snapshot.py:189`); `harness status` lo muestra y
  **sale 1** (`bin/harness:445`); `run` lo anota en el log y no despacha una tanda
  vacía por él (`bin/harness:903-914`); `run --loop` corta la primera pasada con el
  motivo (`bin/harness:1089-1099`, `harness/dispatch.py:774-779`) y `main` devuelve 1
  (`bin/harness:1296`) — "no se pudo leer" no sale 0 (familia de #91, #130).
- **Los agentes de herdr**: `herdr agent list` que falla devuelve `[]` y la pantalla
  dice "ninguno trabajando" (`harness/adapters.py:70-71`, `harness/snapshot.py:264-266`,
  `harness/render.py:243-244`). Sin `HERDR_ENV` es `None` → "fuera de herdr", otro
  estado. **Matiz honesto:** un binario de herdr caído en el medio de la sesión se lee
  como "ninguno trabajando", no como "ilegible". Consecuencia contenida: `estado_frontier`
  (`harness/state.py:525`) marca el ticket `libre` y se re-despacha — gasto de un
  intento, no un dato falso duradero —, pero la pantalla sí miente.
- **El PR/estado en vivo**: `pr_state` devuelve `None` si no se pudo consultar
  (`harness/adapters.py:218-229`); el ticket se queda con lo que dice el log, sin
  corregirse a ciegas (`harness/summary.py:309-322`).
- **El presupuesto**: créditos no leídos = `None` = "no se puede prometer plata que no
  se puede medir" (`harness/dispatch.py:513-516`), no "infinita".
- **Las fuentes de cuota**: una fuente que falta marca el total como piso (`≥`), no lo
  deja pasar como medición (`harness/quota.py:497-512`).
- **La revisión de diff**: sin veredicto se anota `revisión ... fallida` y el PR igual
  aparece — un fallo del revisor no limpia el PR (`harness/review.py:284-313`).

## Regla 3 — Re-chequear en vivo justo antes de actuar

**Cumple** para las acciones del harness (despachar, declarar hecho, mantener);
**no cumple** para el merge, que es de un humano y donde la falla ya costó: **issue #128**.

Los re-checks en vivo, en el orden del ciclo de vida (`harness/dispatch.py`):

- La frontera se lee de GitHub al principio de cada pasada, no de memoria
  (`bin/harness:1079-1087`): "ticket todavía abierto y libre" se verifica
  (`harness/snapshot.py:337`).
- La base se fetchea una vez por corrida antes de crear o rebasear la rama
  (`harness/dispatch.py:1164-1188`); un fetch que falla se anota y no se calla.
- Antes de declarar hecho: el PR existe y apunta al HEAD que el gate midió
  (`harness/dispatch.py:1749-1764`), el gate se corrió recién entonces sobre el
  worktree (`harness/dispatch.py:1681-1693`), y el diff no toca rutas protegidas
  (`harness/dispatch.py:1766-1798`).
- El mantenedor de conflictos re-checkea el veredicto de mergeabilidad **después** de
  resolver, con paciencia para el `UNKNOWN` de GitHub (`harness/maintain.py:302-312`),
  y contra la baseline previa verifica que la resolución no descartó trabajo
  (`harness/maintain.py:314-352`) — el PR de otro que se movió en el medio se ve.
- "Nadie más mergeó": los PRs merged recientes re-entran a la frontera como estado
  `mergeado` (`harness/snapshot.py:337` vía `prs_merged`) y el ticket no se re-trabaja.

**La acción "mergear" es la excepción, y su re-check (el gate de CI) no es
obligatorio**: `gh pr merge` mergea con el gate en rojo sin preguntar (#128: nueve PRs
mergearon en la semana que el gate estuvo rojo). No es una instancia de la regla 3 que
el harness cumpla peor: es el re-check de la regla 3 en el único paso que el harness no
controla, y hoy no manda. Queda en #128.

## Regla 4 — Algo cuenta como hecho solo cuando se confirma

**Cumple.** El harness no se cree la palabra del agente ni la de su propio exit code:

- "Hecho" de un ticket = PR abierto **verificado contra la API** (número del PR sobre la
  rama, `harness/dispatch.py:1695-1721`), HEAD del PR == HEAD medido por el gate
  (`harness/dispatch.py:1749-1764`), diff no protegido (`harness/dispatch.py:1766-1798`).
  Sin PR: abandono, no logro (`harness/dispatch.py:1713`).
- "Hecho" del mantenedor = `mergeable` en vivo según GitHub + gate verde local + trabajo
  preservado contra la baseline (`harness/maintain.py:354-377`).
- Lo que el harness no hizo (el merge) se reconcilia contra GitHub cada vez que alguien
  mira: `harness status` corrige el estado del log contra `pr_state` en vivo
  (`harness/summary.py:309-322`, `harness/adapters.py:218-229`), y `cosechar` cierra
  issues que el PR mergeado prometió (`harness/dispatch.py:2116-2160`). El log nunca
  miente: lo que no se confirma queda como lo que era.
- El mismo principio en el prompt del agente: "verificar contra la API, no contra el
  índice de búsqueda ni contra el exit code" (regla 5 del relevo, `docs/SESSION_HANDOFF.md`).

## Regla 5 — Las preferencias se releen en cada corrida desde un lugar que el agente no puede editar; si no se pueden leer, se detiene en vez de usar defaults

**Parcial.** El lugar sí no puede editarlo el agente; las relecciones y la parada no.
Tres issues:

| Parte | Estado |
|---|---|
| Lugar que el agente no edita | **Cumple**: la config vive en `~/.config/harness/config.json`, fuera del repo y fuera del worktree, y el guard bloquea escrituras fuera del worktree (`extensions/harness-guard/reglas.ts:288` + la regla `write/edit` al final de `reglas.ts`); el dispatcher la lee, nunca la escribe (`harness/adapters.py:93-105`) |
| Releídas en cada corrida | **No**: se leen una vez al arrancar el proceso (`bin/harness:378`) y la corrida entera —incluido `--loop`, que puede durar la noche— corre con esa foto. Issue #163 abajo |
| Si no se pueden leer, se detiene | **No**: sin config en el disco se usan los defaults del código en silencio. Issue #164 abajo |

**Issue #163 — `run --loop` no relee la config en cada pasada.**
`load_config()` corre una sola vez en `main` (`bin/harness:378`); `_run_loop`
(`bin/harness:1228-1297`) y `_una_pasada` (`bin/harness:1065`) reusan los contextos que
recibieron, aunque la pasada se prometa "recalcula la frontera desde el log y GitHub"
— todo salvo las preferencias. Editar la config en el medio de la noche no tiene efecto
hasta relanzar; peor, si la config se corrompe o se borra en el medio de la corrida, la
noche sigue con la foto vieja en vez de detenerse. Test: corriendo un bucle de una pasada
por vez, cambiar la config entre pasadas y esperar que la segunda vea el cambio; y
romperla y esperar que la corrida se detenga con el motivo en el log, no siga.

**Issue #164 — `load_config` usa defaults en silencio cuando no hay config.**
`harness/adapters.py:97-99`: `FileNotFoundError` → `default_config()`. "Aún no escribí
ninguna config" se trata igual que "la config no se puede leer", y el harness arranca
con repos, tracker y presupuesto inventados por el código, sin que nadie se entere. Lo
que sí se detiene es el JSON inválido o el `OSError` (`harness/adapters.py:101-105`,
`ConfigError` impreso en `bin/harness:379-381`): la regla se cumple a medias, y a medias
no cumple. Test: con el path de config apuntando a (a) nada y (b) un archivo sin
permisos de lectura, `main` debe salir ≠ 0 con la razón en el mensaje, no arrancar
contextos default.

## Regla 6 — Acceso de solo lectura donde solo se lee, y un tope de gasto por corrida

**Parcial.** El tope por corrida existe para el reloj (minutos de agente) y para los
créditos *entre* pasadas; **no existe tope de gasto dentro de la propia corrida**
(piso, no techo), y la restricción de lectura en el runner Claude no está forzada.
Tres issues: #165, #166, #167.

| Parte | Estado |
|---|---|
| Solo lectura donde solo se lee | **Parcial**: el revisor de diff corre en pi con `--tools read,grep,find,ls` (`harness/review.py:262,273-276`); el guard de máquina bloquea merge/force-push/DELETE fuera del worktree (`extensions/harness-guard/reglas.ts:185,191-193`); pero el peldaño Claude no tiene el equivalente forzado (sus skills de proyecto se cargan igual, `docs/harness/sesion-minima.md`): issue #167 abajo |
| Tope de gasto por corrida | **Parcial**: minutos de agente por ticket —el tope real, porque es la moneda que se acaba— con su ventana declarada (#116, `harness/dispatch.py:881`, `harness/state.py:120`, `bin/harness:619`); créditos solo como **piso** entre pasadas (`harness/dispatch.py:632`, `744-746`): impide arrancar, no detiene. Issues #165 y #166 abajo |

**Issue #165 — el tope de gasto de la corrida no existe como tope: es piso, no techo.**
`BucleSpec.piso` (`harness/dispatch.py:632`) corta cuando *quedan menos de* US$3:
prohíbe arrancar otra pasada, pero no pone techo a la corrida en curso. Una corrida que
ya tiene tickets en vuelo gasta hasta que el reloj (`wait_ms`) los maten uno por uno;
el costo total de la noche sale de lo que el presupuesto dimensionó *antes*, no de un
límite que la corte. Y el piso no mide todos los runners: los de Claude no gastan de la
key de OpenRouter (`harness/dispatch.py:451-453`), así que para esa mitad de la flota no
hay límite de plata ni local ni global en la corrida — su sola contención es la cuota
semanal de la cuenta. Test: con créditos inyectados cayendo por debajo del piso en el
medio de una corrida en curso, el bucle no la corta; y un bucle donde todos los jobs
son de Claude no corta jamás por crédito.

**Issue #166 — los topes de la corrida no son legibles en cada corrida desde un lugar que el agente no puede editar.**
`budget_minutos` y los topes del bucle viven en `DispatchSpec`/`BucleSpec`
(`harness/dispatch.py:881,632-637`): defaults del código más flags de CLI
(`--piso`, `--max-pasadas`). No están en `~/.config/harness/config.json`
(`harness/config.py:39-44` no los declara), así que (1) cada corrida usa los defaults
del código — el default sí, pero el default durmido en el código — y (2) el runner que
lanza la noche (o un ticket que lo relance) puede pasar `--piso 0` en vez de que la
preferencia la declare la persona y el agente no la pueda tocar. Es la mitad "preferencia"
de la regla 6, y la misma familia que la regla 5. Test: mover los topes a la config
validada (`harness/config.py`) y fallar la corrida con motivo si no se pueden leer.

**Issue #167 — la restricción de solo lectura y de escritura solo existe para pi.**
Donde hoy hay un job que solo lee (el revisor de diff), la restricción sí es forzada en
la herramienta: el revisor corre en pi con `--tools read,grep,find,ls`
(`harness/review.py:262,273-276`) y el guard de máquina bloquea además
merge/force-push/escrituras fuera del worktree en cualquier sesión pi del harness
(`extensions/harness-guard/`). El vacío es la otra mitad de la flota: el peldaño Claude
(`harness/dispatch.py:375-381`, arrancado con `args_sesion_minima`, `harness/dispatch.py:327-342`)
no tiene equivalente forzado — Claude no trae un flag `--tools` de solo lectura, el guard
es una extensión de pi y no opera en Claude, y sus skills del proyecto se cargan igual
(documentado como límite en `docs/harness/sesion-minima.md`). Si mañana un job de solo
lectura corre en Claude, la restricción se sostiene con el prompt, no con la herramienta.
Test: que una sesión Claude del harness no pueda correr `gh pr merge`/force-push
(restricción forzada en la herramienta, no en el prompt), o que no reciba skills del
proyecto que no declaró; hasta entonces la regla queda forzada para pi y documentada
para Claude.

## Cruce con los bugs abiertos

- **#128 (merge con gate de CI en rojo)** es la **regla 3** en su acción "mergear":
  el re-check en vivo (el gate) existe — corre en CI por cada PR
  (`.github/workflows/ci.yml`) — pero no es obligatorio, así que el merge actúa sin
  esperar su veredicto. No es un caso nuevo: se lo deja a #128 (check requerido en
  `main`), que lo arregla donde la regla se rompe.
- **#137 (el presupuesto de reloj mide lapso, no minutos de agente, y deja tickets
  fuera para siempre)** no es regla 6: no es un tope de gasto, es una **ventana**
  mal declarada — familia de la **regla 1** (y de la regla 5 del relevo: "un tope que
  se mide sobre el log entero no es un presupuesto, es una condena"). El tope sí
  existe y sí corta por corrida (`harness/dispatch.py:881`); lo que estaba roto era
  contra qué se medía. Queda en #137.

## Índice de issues nuevos

Abiertos el 2026-10-18, todos `ready-for-agent` y con #152 como parent (sub-issue):

| Issue | Regla | Qué |
|---|---|---|
| #163 | 5 | `run --loop` no relee la config en cada pasada y no se detiene si deja de poder leerla |
| #164 | 5 | `load_config` arranca con defaults en silencio cuando no hay config |
| #165 | 6 | El límite de crédito es piso (no arrancar), no techo: la corrida en curso no tiene tope de gasto |
| #166 | 6 | Los topes de la corrida no son preferencias re-leídas de config: defaults del código + flags de CLI |
| #167 | 6 | La restricción de solo lectura / de escritura solo está forzada en pi; el peldaño Claude se sostiene con el prompt |

Cada issue trae su propio test que reproduce el caso; este ticket no cambió código.
