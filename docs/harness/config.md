# La config del harness

Vive en `~/.config/harness/config.json`, **fuera del repo**: ahí van los tokens de
los trackers. El harness la lee y no la escribe nunca. Si el archivo no existe o no
se puede leer, el harness se detiene con la razón (regla 5, #164): no arranca con
una config de arranque inventada por el código (`DEFAULT_CONFIG` en
`harness/config.py` queda como plantilla de lo que seguía el viejo `repos.conf`).

`HARNESS_CONFIG_DIR` la mueve a otra carpeta; `XDG_CONFIG_HOME` también se respeta.

## Forma

```json
{
  "default_context": "personal",
  "contexts": {
    "personal": {
      "tracker":  {"kind": "github"},
      "repos":    {"root": "~/Documents/Github",
                   "paths": ["koku", "entrevestidos", "entrevestidos/*"]},
      "autonomy": "frontier",
      "budget":   {"polarity": "remaining", "provider": "openrouter"},
      "vault":    "~/notas",
      "run":      {"kind": "local"},
      "quota":    {"tope_semanal": 900000000,
                   "projects": ["~/.claude/projects",
                                "/Users/tomasherceg/.claude/projects"]}
    },
    "trabajo": {
      "tracker":  {"kind": "jira", "url": "https://wl.atlassian.net",
                   "project": "GRO", "token": "..."},
      "repos":    {"root": "~/work", "paths": ["groceries-wl"]},
      "autonomy": "manual",
      "budget":   {"polarity": "spent", "provider": "manual",
                   "total": 200.0, "used": 128.4},
      "vault":    "~/wl-devlead-vault",
      "run":      {"kind": "ssh", "host": "wl@localhost"}
    }
  }
}
```

| Campo | Qué es |
|---|---|
| `default_context` | qué contexto muestra `harness status` sin argumentos. Si falta, el primero |
| `tracker.kind` | `github` (issues vía `gh`) o `jira` (todavía sin adaptador) |
| `repos.root` | de dónde cuelgan los paths relativos |
| `repos.paths` | un repo por entrada. `carpeta/*` = los repos git que cuelgan de esa carpeta |
| `autonomy` | `frontier` (el dispatcher toma solo el próximo ticket) o `manual` (lo elijo yo) |
| `budget.polarity` | `remaining` (que no se acabe) o `spent` (que no sobre) |
| `budget.provider` | `openrouter` (lee la key de `~/.pi/agent/models.json`), `manual` (`total`/`used` acá) o `none` |
| `vault` | dónde vive la vault del contexto. El harness la lee, no la administra |
| `run.kind` | `local` o `ssh` (con `host`): cómo se ejecuta el trabajo de este contexto |
| `quota.tope_semanal` | opcional: el tope semanal estimado de `harness quota`, en tokens ponderados. Es la constante de calibración (#74): sale de medir `/usage` (interactivo, lo mide un humano) y vive acá, no en el código. Con ella la tabla muestra el % del tope semanal en cada corte; sin ella, los tokens y la nota de que falta calibrar |
| `quota.projects` | opcional: la lista de directorios de sesiones que lee `harness quota` (#75). Sin declarar, mira un solo usuario del sistema (`~/.claude/projects`); con la lista suma los declarados (p.ej. otro usuario de la máquina que comparte la cuenta). `--projects` por coma gana sobre la config. Una fuente ilegible se salta, se dice cuál y por qué, y el total se marca como piso (`≥` / `"completa": false`) |
| `quota.remotas` | opcional: lista de fuentes remotas por SSH (`"user@host"`) que suma `harness quota` (#144): el usuario del 9-5 comparte la misma bolsa semanal de la cuenta. El consumo se lee corriendo `scripts/quota_remota.py --json` en el host (timeout corto, 30 s). `--remotas` por coma gana sobre la config. Si el SSH falla, la fuente cuenta como ilegible y el total se marca como piso, igual que con `projects` |
| `quota.umbral_sesion`, `quota.reserva_diaria`, `quota.costo_ticket`, `quota.hora_limite`, `quota.statusline_max_edad_min` | opcionales: la política de `harness run` sobre la cuota real (`harness usage`). Defaults 0.90, 0.08, 0.02, `"07:00"` y 10 min; qué hace cada uno en `docs/harness/quota.md` |
| `topes.budget_minutos` | opcional: el presupuesto de reloj por ticket de `harness run` — minutos de agente acumulados en el historial de la noche. Default 180 (documentado en `RunTopes` en `harness/config.py`). Un ticket que llegó hasta acá se parkea con su motivo (#116) |
| `topes.max_usd` | opcional: el tope de gasto de la corrida en US$ (#165) — un techo, no un piso. Default **0**: sin un monto explícito, la corrida no despacha ningún job que cueste plata (pi/OpenRouter); los peldaños de Claude (suscripción) no lo tocan. Lo gastado se mide desde que arrancó el proceso (delta de créditos usados de OpenRouter y costo de los jobs cerrados) y se consulta antes de despachar cada job pagado: no sale si gastado + costo medio (US$1.00 sin historial) supera el tope. El revisor de PRs (pi) también pasa por el tope. `--max-usd` puede bajarlo, nunca subirlo. El piso sigue cortando el bucle, pero nunca deja gastar más que esto |
| `escalera` | opcional: con qué runner arranca `harness run`/`maintain` en este contexto si la CLI no dice `--kind` (#174). `"claude"` (el default sin declarar): la escalera de la corrida es solo Claude, sonnet → opus, y ningún job va a pi. `"pi"`: la escalera completa, pi → pi high → sonnet → opus (gasta OpenRouter). `--kind` gana sobre la config |
| `repos.runner_min` | opcional: el runner mínimo por repo (#177). La clave es el **nombre de la carpeta del repo** (el último tramo de la ruta, el mismo nombre que muestra `harness status`), no la ruta: un repo que sale de `"F7League/*"` como `F7League/app-movil` se declara `"app-movil"`. Ejemplo: `{"F7League": "claude", "app-movil": "claude"}`. Un repo con `"claude"` nunca recibe un peldaño pi, ni con la etiqueta `runner:cheap` ni en una corrida `--kind pi`. `"pi"` es lo mismo que no declararlo |
| `topes.piso`, `topes.max_pasadas` | opcionales: los topes del bucle de `harness run --loop` — piso de crédito restante en US$ (default 3.00) y tope duro de pasadas con trabajo (default 12). La CLI (`--piso`, `--max-pasadas`) puede acotarla más, pero no amplificarla por encima de la config (#166) |

`vault`, `quota`, `topes`, `escalera` y `repos.runner_min` son opcionales; todo lo demás es obligatorio. Los topes se
releen de la config en cada corrida/pasada: una corrida que dura la noche no corre con
la foto que tomó al arranque, y si la config deja de poder leerse la corrida se detiene
con el motivo (#163/#164/#166). Un campo de más o un valor que no
existe son error: el harness sale con código 2 y una línea que dice qué contexto y
qué campo hay que arreglar.

## Plan aprobado: ninguna corrida real sin él (#176)

La noche del 2026-10-18 la corrida nocturna se programó sin que el dueño la hubiera
autorizado y gastó US$17. Desde #176 una corrida real necesita un plan aprobado del día:

1. `harness run --plan [flags]` — los mismos flags que la corrida que se va a aprobar
   (`--loop`, `--kind`, `--permitir-pi`, `--max-usd`, `--hasta`, `--freno`, `--only`).
   **Sin efectos**: lee el log y el tracker (el mismo `collect` de sólo lectura de la
   corrida), no escribe el log de eventos, no cosecha (cosechar cierra issues), no
   parkea, no consulta `/usage` (abre un pane), no crea ramas, worktrees ni panes. Lista
   cada job que saldría: repo, número, título, runner, modelo, peldaño, el porqué de
   `elegir_peldano`, y el costo estimado (pi: el promedio de `harness costo` de ese
   peldaño, si no el costo medio por ticket del historial, si no US$1.00 conservador;
   Claude: US$0 en plata, gasta cuota de la suscripción). Al final, el total estimado
   y el tope (`topes.max_usd`/`--max-usd`) que lo corta; con `--loop`, la hora límite y
   el freno. Lo guarda en `<estado>/harness/plan-FECHA.json` con un hash. Es la cota de
   arriba: la cuota, el presupuesto de la tanda y el reloj sólo sacan.
2. `harness aprobar [--si]` — muestra el plan **del archivo** (se aprueba lo que se
   vio, no un plan recalculado que podría ser otro), pide confirmación (`--si` no
   pregunta; sin terminal y sin `--si`, no aprueba) y suma el hash a
   `<estado>/harness/aprobado-FECHA` (una línea por plan aprobado: aprobar un
   `maintain` no borra la aprobación de la noche). Un plan editado a mano no se
   aprueba: el archivo lleva además `hash_documento`, un hash del documento entero
   (títulos, peldaño, estimados, `usa_pi`), para que no se lo pueda retocar para
   verse más barato con el `hash` intacto.
3. `harness run` (también `--loop`) recalcula el plan al arrancar, antes de cualquier
   efecto: sin `aprobado-FECHA`, o con un hash distinto (ticket nuevo, otro runner, otro
   tope, otra hora límite, otro modo), sale con código 2, dice qué cambió y cómo
   arreglarlo. Una aprobación de `--plan --loop` no vale para un `run` suelto, ni al
   revés.
4. Con `--loop`, cada pasada re-planifica: sale lo que el plan aprobado cubre (mismo
   repo, número, runner y modelo: la escalada pi → pi high sigue cubierta) **más**
   tickets nuevos que vayan a Claude (no gastan plata). Un ticket pagado nuevo queda
   `fuera` en el log con el motivo. El tope aprobado es techo: si la config sube
   `topes.max_usd` a mitad de la noche, la corrida no lo sigue.

El hash cubre lo que decide plata: modo, por contexto la corrida (runner,
`--permitir-pi`, `runner_min`), el tope, la hora límite y las entradas (repo, tipo,
número, runner, modelo, args del peldaño). No cubre timestamps, títulos, motivos ni
estimados: el costo medio se mueve con cada job y no puede invalidar la aprobación a
mitad de la noche.

**`FECHA` es la de la noche, no la del reloj.** De 12:00 de D a 11:59 de D+1
(America/Santiago) `FECHA` = D+1. El dueño aprueba de tarde (22:00 de D) y el timer
lanza a la 01:00 de D+1, donde el lanzador del servidor, `harness-noche`, mira
`~/.local/state/harness/aprobado-$(date +%F)` = D+1: los dos coinciden. Por eso:

- **Se aprueba entre las 12:00 y el lanzamiento.** Lo aprobado de mañana (antes de las
  12:00 de D) queda con fecha D y no vale para la noche que arranca a la 01:00 de D+1;
  lo de la noche anterior tampoco.
- `<estado>` es `XDG_STATE_HOME` o `~/.local/state`: en el servidor, la ruta del
  lanzador. Si no existe, el lanzador sale sin hacer nada.
- **El plan se saca con los flags exactos del lanzador** (`harness run --loop --kind
  claude --freno ~/.local/state/harness/freno`); si no, el hash es otro y la noche sale
  con código 2:

```sh
harness run --plan --loop --kind claude --freno ~/.local/state/harness/freno
harness aprobar
```

- **Cualquier ticket que aparezca o se cierre entre la aprobación y la 01:00 cambia el
  hash**, y la noche sale con código 2 sin hacer nada. El rechazo lista qué cambió
  (tickets de más o de menos, tope, hasta, corrida, modo) para volver a planear y
  aprobar. Es el costo de que nada corra sin que el dueño lo haya visto.

Un `touch` vacío del archivo (la aprobación vieja del lanzador) deja pasar al lanzador
pero no al harness: sin hash no hay plan aprobado.

**Uso manual.** `--only N` también pide aprobación: puede gastar plata igual que la
noche. La salida es `--sin-aprobacion`, sólo sin `--loop`: una corrida manual corre
sin plan aprobado si **todo** lo que despacharía es Claude (y la corrida no usa pi para
el revisor ni proponer); el tope de gasto queda en 0. Si algo paga, se niega igual.

Un `run` sin `--loop` cuyos contextos son todos `autonomy: manual` no pide aprobación
(no despacha nada); con `--loop` sí, porque la config se relee cada pasada y un
contexto que pasa a `frontier` a mitad de la noche no puede despachar sin plan ni techo.
Un plan aprobado sin nada pagado ni pi deja techo 0: una config que pase a
`escalera: pi` a mitad de la noche no prende el revisor ni el proponer en pi.

**`maintain` y `proponer` sueltos** pasan por la misma puerta: si la corrida usa pi
(`--kind pi`, `escalera: pi`, `--permitir-pi`) o el tope efectivo es > 0, necesitan
`harness maintain --plan` / `harness proponer --plan` (una entrada "corrida de harness
<cmd>" con el tope como peor caso) y `harness aprobar`. Si no, corren con tope 0, y
`--sin-aprobacion` también los deja correr sin plan si no usan pi (tope 0). `ideas` es
solo Claude: sin puerta. El `proponer` del `--loop` queda cubierto por el plan de la
noche.

## Con qué runner corre la noche

La corrida dice con qué runner arranca, y eso manda sobre la escalera de reintentos
(#174). La noche del 2026-10-18 `harness run --loop --kind claude` despachó los 49 jobs
a pi/Qwen (US$17 de OpenRouter) porque el peldaño 1 de la escalera ganaba siempre.
Ahora cada job pasa por `elegir_peldano` (`harness/dispatch.py`), en este orden:

1. `repos.runner_min` del repo es `claude` → escalera solo Claude, pase lo que pase.
2. La corrida es `pi` (`--kind pi` o `"escalera": "pi"`) → la escalera completa.
3. La corrida es `claude`, el ticket trae `runner:cheap` **y** la corrida tiene
   `--permitir-pi` → la escalera completa.
4. Si no → escalera solo Claude (sonnet → opus). Una etiqueta `runner:cheap` sin
   `--permitir-pi` se ignora, y se dice.

En la escalera solo Claude los abandonos de pi de corridas viejas no gastan peldaño: un
ticket que Qwen tumbó dos veces arranca en sonnet, no parkeado. La línea `corrida` del log
anota el runner de la corrida y de dónde salió (`--kind`, `config escalera`, `por
defecto`), y la línea `peldano` de cada job dice el peldaño y por qué.

Lo que no es un peldaño de ticket sigue la misma regla: en una corrida sin pi (ni
`--kind pi` ni `--permitir-pi`), el revisor de PRs corre en Claude sonnet con sólo tools
de lectura (`--tools Read,Grep,Glob --permission-mode plan`), y el generador y el juez de
`proponer` (también el del `--loop` con la frontera vacía) corren en Claude; `--juez-qwen`
se ignora y se anota.

El repo que se sabe que Qwen no cierra (F7League: 12 tickets, 0 PRs, US$9,40 la noche
del 2026-10-18) se declara en la config del servidor. Las claves de `runner_min` son
nombres de carpeta, no rutas: si F7League es un repo y además contiene subrepos
(`"F7League/*"`), cada uno se declara por su nombre (`F7League/app-movil` → `app-movil`):

```json
"repos": {"root": "~/Documents/Github",
          "paths": ["agent-harness", "F7League", "F7League/*"],
          "runner_min": {"F7League": "claude", "app-movil": "claude"}}
```

## La vault y las decisiones

La vault de un contexto es un lugar para lo que se aprende y no pertenece a ningún
proyecto: investigaciones, decisiones, reglas de negocio. Taxonomía personal (la del
trabajo, menos lo de equipo):

```
~/vault/
├── decisiones/      notas de decisión (las lee `harness decisions`)
├── investigations/
├── diario/
├── fundamentos/
├── guias/
└── handoff/
```

El harness **lee** la vault y **no la administra**: no crea, no borra, no escribe
dentro. La única carpeta que mira es `decisiones/`.

`harness decisions [--context N | --all] [--json]` cruza todas las decisiones:
los `docs/adr/*.md` de cada repo más las notas de `decisiones/` de la vault, y
cada resultado muestra su fuente (qué repo o qué carpeta de la vault). Un contexto
sin vault no rompe: sólo muestra sus ADRs. Todo es disco local, así que corre con
`HARNESS_OFFLINE=1`.

La memoria de Claude (`CLAUDE.md`, `~/.claude/...`) **no** entra en esta vista ni se
fusiona con la vault: es cómo trabajar con Tomás, no lo que aprende Tomás.

## Repos anidados

El caso entrevestidos: una carpeta que es un repo y además contiene un repo por
subproyecto. Se declaran las dos cosas —`"entrevestidos"` y `"entrevestidos/*"`— y el
harness sigue los tres. Lo que no está en el disco se saltea sin quejarse: un repo
que todavía no clonaste no es un error de config.

## Polaridad del presupuesto

El mismo número, dos objetivos opuestos.

- **personal** (`remaining`): el recurso escaso es la plata. Lidera lo que queda
  —`$13.37 de $25.00 disponibles`— y la barra se pone roja cuando se acaba.
- **trabajo** (`spent`): el presupuesto es de la empresa y dejarlo sin usar también es
  perderlo. Lidera lo gastado —`$128.40 de $200.00 usados`— y la barra se pone roja
  cuando va *atrasada*.
