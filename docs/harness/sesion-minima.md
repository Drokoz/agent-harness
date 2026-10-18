# La sesión mínima del implementador (#42)

El 72% del gasto de cuota medido vino de sesiones con más de 150k de contexto,
y las sesiones del propio harness corrían a ~142k de `cache_read` por mensaje —
todo eso se factura en cada turno aunque esté cacheado. Un implementador no
necesita playwright, ni los plugins, ni catorce skills: necesita el ticket,
`AGENTS.md`, `CONTEXT.md` y las dos skills que va a usar.

El harness ahora arma la sesión mínima por kind en `args_sesion_minima`
(`harness/dispatch.py`), que produce los args después del `--` de
`herdr agent start`.

## Qué arranca cada kind

**`pi`** (peldaños 1-2 de la escalera):

```
--no-skills --skill .pi/skills/implement --skill .pi/skills/tdd --model <m> --thinking <n>
```

- `--no-skills` apaga la discovery entera: las skills del repo (`.pi/skills/`)
  y las globales (`~/.pi/agent/skills/`, `~/.agents/skills/`) no entran al
  sistema prompt.
- `--skill <dir>` carga las que el ticket necesita, relativas al worktree (el
  cwd del agente). Es aditivo incluso con `--no-skills` (documentado por pi).
- Pi no tiene servidores MCP ni plugins: su equivalente de "sin MCP, sin
  plugins" es que no hay nada que quitar. Lo único que sí se controla es la
  discovery de skills, y la extensión `harness-guard` se mantiene a
  propósito: es la red de la máquina (bloquea merge, force-push,
  `reset --hard`), no contexto del ticket.

**`claude`** (peldaños 3-4):

```
--strict-mcp-config --setting-sources project --permission-mode auto \
--settings {"autoMode":{"skipAutoPermissionPrompt":true}} --model <m> --effort <n>
```

- `--strict-mcp-config` sin `--mcp-config` = cero servidores MCP: no los del
  proyecto en `~/.claude.json`, no los de user, no los de plugins.
- `--setting-sources project` = sin los settings de usuario
  (`~/.claude/settings.json`): los plugins habilitados a user-scope
  (playwright, frontend-design, vercel), los hooks y el contexto
  `autoMode.environment` de otro repo que se les colaba a todas las
  sesiones.
- `--permission-mode auto` + el `--settings` con el opt-in: los settings de
  usuario traían `defaultMode: auto` y el opt-in aceptado; al saltarlos hay
  que reponerlos explícitos, o la sesión queda en manual y el agente se
  bloquea en la primera aprobación.
- **Límite documentado:** Claude no tiene flag por skill, así que las skills
  del proyecto (`.claude/skills/`) se cargan igual. Lo que se saca son los
  plugins (y con ellos sus skills/commands/agents) y los skills globales de
  user-scope.

## Las skills del ticket

El ticket declara skills en una línea de su cuerpo (convención del ticket, no
del harness):

```
Skills: research, domain-modeling
```

- `skills_de(body)` (`harness/dispatch.py`): la primera línea `Skills:`
  (mayúsculas a voluntad, separadas por coma) sobre la base del
  implementador, `("implement", "tdd")`. Nombres inválidos se ignoran, los
  repetidos se dedupan.
- `bin/harness` lo pasa al `Job` (y al `MantenJob`/`ProponeJob`, que usan la
  base). Para pi, `_agente` filtra por las que existen en el worktree
  (`.pi/skills/<nombre>/SKILL.md`): una declarada que no hay se anota en el
  log de eventos (tipo `skills`) y se deja afuera, porque un `--skill`
  inexistente tumbaría el arranque del agente.

## Una sesión fresca por ticket

Un ticket es una sesión nueva: el harness nunca pasa `--resume` ni
`--continue` (ni `herdr agent start` los soporta: arranca un agente en un
pane nuevo). El test `test_ninguna_sesion_lleva_resume_ni_continue` lo
garantiza para todos los kinds.

## Medición (contexto base antes/después)

El contexto base es lo que el primer mensaje de una sesión factura:
`input + cache_read + cache_creation` (claude) o `input + cacheRead +
cacheWrite` (pi). Se mide con sesiones reales de una línea, en el worktree,
con el mismo prompt en ambos lados —no por estimación. Para leer el primer
mensaje en claude se usa `--output-format stream-json` (el JSON de `-p`
agrega toda la sesión):

```bash
# antes: como arrancaba el harness (settings de usuario, plugins, MCP, skills)
claude -p "OK" --model sonnet --output-format stream-json
# después: sesión mínima
claude -p "OK" --model sonnet --strict-mcp-config --setting-sources project \
  --permission-mode auto --settings '{"autoMode":{"skipAutoPermissionPrompt":true}}' \
  --output-format stream-json

# antes: discovery completa de pi
pi -p "OK" --model qwen/qwen3.8-27b --mode json --no-session
# después: solo las skills de la base
pi -p "OK" --model qwen/qwen3.8-27b --no-skills \
  --skill .pi/skills/implement --skill .pi/skills/tdd --mode json --no-session
```

El `cache_read` del primer mensaje depende de si el prefijo ya estaba
cacheado en la cuenta (lo está, tras la primera corrida de la misma
config), así que el número a comparar es la suma de los tres componentes,
no el `cache_read` solo.

### Números (medidos el 2026-09-07, worktree de este ticket, prompt de una línea)

**claude (sonnet), primer mensaje de la sesión:**

- antes (settings de usuario, plugins, MCP, skills globales y del proyecto):
  `input 2 + cache_read 23.899 + cache_creation 14.667` = **38.568 tokens**
- después (sesión mínima): `input 2 + cache_read 23.899 + cache_creation 4.862`
  = **28.763 tokens**
- diferencia: **9.805 tokens por mensaje (−25%)**: los plugins
  (playwright, frontend-design, vercel), las skills de user-scope, el
  contexto `autoMode.environment` y los hooks de los settings de usuario.
- La `cache_read` de 23.899 es el prefijo base ya cacheado en la cuenta
  (idéntico en las dos configs); lo que el cambio quita es lo que cada
  config crea por sesión (14.667 → 4.862). Las skills del proyecto
  (`.claude/skills/`) siguen cargadas en las dos, por el límite documentado.

**pi (qwen3.8-27b vía OpenRouter), primer mensaje:**

- antes (discovery completa: 16 skills del repo + 2 globales):
  `input 425 + cache_read 6.272` = **6.697 tokens**
- después (`--no-skills` + implement + tdd): `input 6.312` = **6.312 tokens**
  (primera corrida de la config nueva: el cache todavía no está tibio y se
  factura input; desde la segunda corrida en adelante es `cache_read`).
- diferencia: **385 tokens por mensaje (−6%)**: la discovery de pi es
  progresiva (al prompt entra nombre y descripción de cada skill, no el
  SKILL.md), así que 18 skills fuera pesan poco. El peldaño de pi nunca fue
  el driver de los 150k: el driver era el lado claude.

Nota: el ~142k por mensaje del relevo de agosto se midió con una config de
usuario más pesada (más plugins/MCP conectados); el delta de arriba es lo que
este cambio quita sobre la config actual, medido igual a ambos lados.

## Necesita humano

- Verificar en el próximo peldaño de Claude real que la sesión no se detiene
  en ningún diálogo del modo auto (el probe de `-p` no ejercita la TUI; el
  arranque se verificaría igual por el log: un `blocked` en vez de `working`
  lo delata).
