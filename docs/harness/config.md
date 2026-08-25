# La config del harness

Vive en `~/.config/harness/config.json`, **fuera del repo**: ahí van los tokens de
los trackers. El harness la lee y no la escribe nunca. Si el archivo no existe corre
con la config de arranque (`DEFAULT_CONFIG` en `harness/config.py`), que es lo que
seguía el viejo `repos.conf`.

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
      "quota":    {"tope_semanal": 900000000}
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

`vault` y `quota` son opcionales; todo lo demás es obligatorio. Un campo de más o un valor que no
existe son error: el harness sale con código 2 y una línea que dice qué contexto y
qué campo hay que arreglar.

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
