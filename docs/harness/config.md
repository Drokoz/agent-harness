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
      "run":      {"kind": "local"}
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

`vault` es opcional; todo lo demás es obligatorio. Un campo de más o un valor que no
existe son error: el harness sale con código 2 y una línea que dice qué contexto y
qué campo hay que arreglar.

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
