# harness-guard

Una extensión de [pi](https://github.com/earendil-works/pi) que convierte las reglas del
prompt en reglas mecánicas, **sólo adentro de un worktree del harness**.

## Por qué

El prompt le dice al agente "never merge and never force-push". Un prompt es una
sugerencia estadística, no una ley. A las 3 de la mañana, con el gate en rojo, un modelo
barato tiene todos los incentivos para destrabar la situación de la peor manera posible:
mergear, forzar un push, borrar una rama, resetear el árbol.

`pi` deja interceptar la tool antes de que se ejecute (`tool_call` → `{ block: true, reason }`).
Esto es eso.

La idea viene de [`bash-guard`](https://github.com/amosblomqvist/pi-config/tree/main/extensions/bash-guard)
de amosblomqvist, pero la lista es distinta: bash-guard bloquea `git commit` y `git push`
porque para él son operaciones del humano. Para nosotros **son el trabajo**.

## Alcance

`~/.pi/agent/extensions/` es global: también afecta al `pi` interactivo del humano. Por eso
la extensión no hace nada salvo que el cwd del proceso sea un worktree del harness, o sea
`…/.worktrees/<repo>-ticket-<n>` — que es exactamente lo que arma `dispatch.worktree_path`.
Fuera de ahí, no existe.

## Qué bloquea

| | |
|---|---|
| `gh pr merge`, `git merge` | el merge es decisión humana |
| `git push --force` / `-f` / `--force-with-lease` | reescribe historia que otro ya vio |
| `git push … main` / `HEAD:main` | saltarse el PR |
| `git checkout/switch main` | `main` está checkouteado en otro lado; moverlo desincroniza la copia principal |
| `git worktree …` | los worktrees son del dispatcher |
| `git branch -d/-D` | la rama es lo único que sobrevive a un intento fallido |
| `git reset --hard`, `git clean -f` | tira a la basura trabajo sin commitear |
| `git reflog expire`, `git gc --prune` | borra el último camino de vuelta |
| `rm` / `find -delete` fuera del worktree | incluye `~`, `..` y `$VAR` sin expandir |
| `write`/`edit` fuera del worktree | la forma silenciosa de romper el checkout principal |
| redirección `>` fuera del worktree | `/tmp` y `/dev` sí |
| `sudo`, `curl \| sh`, `shutdown`, `mkfs`, `dd of=/dev/…` | catastrófico y nunca parte de un ticket |
| `gh repo delete`, `gh api -X DELETE` | destruye estado fuera del worktree |

**Pasa entero el trabajo del agente**: `git add`, `git commit`, `git push` de su rama,
`git rebase`, `gh pr create`, `gh issue view`, el gate, los tests, `npm install`, y
cualquier `rm` adentro del worktree.

Dos detalles que importan:

- **Comillas.** Un `git commit -m "no sudo"` no es un `sudo`. El tokenizador respeta
  comillas, si no el agente quedaría trabado por su propio mensaje de commit.
- **El cwd se arrastra.** `cd ~ && rm -rf junk` borra en `~`, no en el worktree. Las rutas
  relativas se resuelven contra el `cd` pero se comparan siempre contra la raíz del worktree.

## Rastro

Cada bloqueo escribe una línea `tipo: "guard"` en `~/.local/state/harness/events.jsonl`,
con el mismo esquema que el `EventLog` del dispatcher, y avisa por stderr (que queda en el
pane). Un bloqueo que no deja rastro es un bloqueo que a la mañana no existió.

## Instalar

```bash
cp -r extensions/harness-guard ~/.pi/agent/extensions/
```

Sin dependencias npm a propósito: nada que instalar, nada que se rompa a las 3 de la mañana.
`pi` lo autodescubre al arrancar; en una sesión abierta, `/reload`.

## Tests

```bash
node --experimental-strip-types --test extensions/harness-guard/*.test.ts
```

Corren dentro de `scripts/gate.sh`. Si no hay `node`, el gate es rojo: un guard sin testear
es peor que no tener guard.
