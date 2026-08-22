# Harness de trabajo autónomo — plan

Fecha: 2026-08-21. Objetivo: pasar de "le pido cosas a Claude Code" a un pipeline
donde el pensamiento caro lo hago yo + un modelo bueno, y la ejecución la reparte
un orquestador entre agentes baratos, con un gate automático que decide qué merge.

---

## 0. Punto de partida (verificado en esta máquina)

| Pieza | Estado | Nota |
|---|---|---|
| `herdr` 0.7.5 | instalado, `HERDR_ENV=1` | multiplexor con API de control: panes, worktrees, `agent start/prompt/wait` |
| `pi` 0.83.0 | instalado (`@mariozechner/pi-coding-agent`) | provider actual: **xai / grok-4.5**. Soporta `--skill`, `--print`, `--mode json/rpc` |
| `claude` | instalado | Opus 5, skills, plugins |
| `supacode` | instalado | (sin rol asignado por ahora) |
| Modelo local | **no instalado** | Qwen3.8-27B UD-Q4_K_M = 16.5 GB, vía `llama serve -hf` |
| Hardware | M4 Pro, **24 GB** | ← la restricción que define todo el diseño |
| Repos activos | koku, Health-Link, entrevestidos, ERP-IphoneUp, herceg-motors | entrevestidos ya tiene **spec-kit** |

Agentes que herdr sabe arrancar y controlar: `pi, claude, codex, gemini, cursor, opencode, copilot, grok, amp, droid, ...`
Sandcastle soporta: `claudeCode, codex, pi, cursor, opencode, copilot` en Docker / Podman / Vercel microVM.

**Conclusión temprana:** herdr y sandcastle orquestan *el mismo set de agentes*, y las skills de
Matt Pocock son markdown portable que corre igual bajo `claude` que bajo `pi --skill`.
No hay que elegir stack: hay que elegir **el contrato entre capas**.

---

## 1. La idea central: tres capas y un contrato

```
CAPA 1 — INTENCIÓN          (caro, humano + Opus, en Claude Code)
  grill-with-docs → CONTEXT.md + ADRs
  to-spec         → spec
  to-tickets      → N tickets, cada uno con blocking edges + acceptance criteria
  wayfinder       → sólo para trabajo grande (mapa de decisiones multi-sesión)
        │
        ▼   ← EL CONTRATO: un ticket = 1 context window, vertical slice,
        │      demoable solo, con "Blocked by" explícito y label ready-for-agent
        │
CAPA 2 — ORQUESTACIÓN       (barato, determinista, sin LLM en el loop)
  dispatcher: calcula la FRONTERA (tickets cuyos blockers están cerrados)
  y asigna cada uno a un runner aislado
    · local  → herdr worktree + pane + agent start
    · escala → sandcastle (Docker sandbox, branch strategy)
        │
        ▼
CAPA 3 — EJECUCIÓN + GATE   (agente cualquiera + verificación mecánica)
  /implement (→ /tdd) → tests/hurl/E2E verdes → PR → /code-review → merge humano
```

Todo el valor está en que la **Capa 1 produce algo que una máquina puede consumir sin
interpretar**. `to-tickets` ya lo hace por diseño: tracer-bullet vertical, sized to a
single fresh context window, blocking edges declaradas, acceptance criteria como checkboxes.
Eso es literalmente una cola con un DAG y una done-condition. Es el input que necesita
cualquier orquestador.

**Corolario incómodo:** el cuello de botella del trabajo autónomo NO es el paralelismo.
Es (a) la calidad del spec y (b) la confiabilidad del gate. Si el gate no es confiable,
paralelizar sólo produce basura más rápido. Por eso el orden de las fases es el que sigue,
y el orquestador llega tercero, no primero.

---

## 2. Restricciones reales que hay que respetar

1. **24 GB de RAM.** Qwen3.8-27B a UD-Q4_K_M ocupa 16.5 GB residentes. Con eso cargado no te
   quedan panes de herdr + Postgres en Docker + un dev server de Next. Es un hecho, no un
   detalle de tuning. Opciones honestas:
   - usar un quant más chico (UD-Q3_K_XL, ~13 GB) y aceptar degradación, o
   - usar un modelo más chico para el rol de grunt (Qwen3 8B/14B), o
   - correr el 27B **sólo cuando los panes están idle** (rol batch nocturno, no rol interactivo).
   El 27B local **no** va a ser tu implementador. Va a ser tu obrero barato de tareas mecánicas.
2. **Colisión de recursos al paralelizar.** koku y ERP-IphoneUp usan Postgres en Docker con
   puertos fijos (:5433, :5440). Dos worktrees corriendo tests a la vez pelean por el mismo
   puerto y la misma base. Esta —y no "quiero más agentes"— es la razón real por la que
   eventualmente vas a necesitar sandcastle.
3. **entrevestidos ya tiene spec-kit.** Dos sistemas de spec en el mismo repo = confusión
   garantizada. Un repo, un sistema. No mezclar.
4. **Cinco repos activos, una persona.** El piloto es UNO. Se generaliza cuando el ciclo
   completo dio la vuelta al menos tres veces sin intervención manual.

---

## 3. Fases

### Fase 0 — El gate (lo primero, sin excepción) — ✅ HECHA en ERP-IphoneUp (2026-08-21)

Antes de automatizar nada: en el repo piloto, un solo comando tiene que responder
verde/rojo sin ambigüedad, sin que yo mire nada.

```bash
# scripts/gate.sh  → exit 0 = mergeable
pnpm typecheck && pnpm lint && <suite de integración> && <e2e si aplica>
```

Ya tenés las piezas: hurl en koku (`backend/tests/*.hurl`), E2E en ERP-IphoneUp.
Falta empaquetarlas en un `gate.sh` único y que sea el mismo que corre el agente,
el pre-commit y CI. **Si el gate no existe, nada de lo que sigue es seguro.**

Criterio de salida de la fase: `./scripts/gate.sh` corre limpio en main y falla
ruidosamente si rompo algo a propósito.

**Resultado en ERP-IphoneUp:** `scripts/gate.sh` verde en 89s (59s en caliente),
51 tests de integración corriendo de verdad. Chequea: Postgres arriba (lo levanta solo) →
istock_test existe → goose up → drift de sqlc → gofmt/build/vet → unit en paralelo →
integración serializada (-p 1) → antiskip con allowlist → vitest + build de Vite.
Verificado que con un DSN inválido da ROJO, no falso verde.

Tres defectos reales que encontró en main, invisibles hasta ahora:
1. 4 archivos sin gofmt (CI no chequea formato).
2. `TestIntegrationRepuestosConsumo` no era idempotente (INSERT ciego contra un índice
   único con COALESCE) → fallaba en la segunda corrida.
3. `TestIntegrationCompraConRepuestos` asertaba comportamiento VIEJO (repuestos como
   reparación con snapshot de costo) cuando el producto ya los trata como PLAN
   (`costo` NO suma a `equipo.costo_reparacion`). Llevaba roto sin que nada avisara
   porque CI lo salta y localmente se salta sin TEST_DATABASE_URL.

### Fase 1 — Pipeline de intención (skills de Matt Pocock), repo piloto

```bash
claude plugins install mattpocock-skills     # o: npx skills@latest add mattpocock/skills
cd <repo-piloto> && claude
/setup-matt-pocock-skills                    # tracker + labels + docs layout
```

Esto escribe `docs/agents/issue-tracker.md`, `docs/agents/domain.md`,
`docs/agents/triage-labels.md` y un bloque `## Agent skills` en el CLAUDE.md existente.

Primer ciclo real, a mano, sin orquestador:

```
/grill-with-docs   → CONTEXT.md (vocabulario del dominio) + ADRs
/to-spec           → spec de UNA feature del backlog
/to-tickets        → 3-6 tickets con blocking edges
/implement <#1>    → uno solo, a mano, mirando
/code-review
```

Objetivo de la fase: **medir**. ¿Cuántos de esos tickets un agente los cierra solo,
con el gate en verde, sin que yo intervenga? Si la respuesta es "0 de 5", el problema
está en el spec y ningún orquestador lo arregla. Si es "4 de 5", ya se puede automatizar.

Nota sobre CONTEXT.md: es la pieza más subestimada. Es lo que hace que un agente barato
(pi + grok, o el modelo local) escriba código que usa tu vocabulario en vez de inventar el
suyo. Es el multiplicador que hace viable bajar de modelo.

### Fase 2 — Dispatcher local con herdr

Sin frameworks. Un script que hace tres cosas: calcular la frontera, lanzar, cosechar.

```bash
#!/usr/bin/env bash
# dispatch.sh — lanza los tickets desbloqueados en panes aislados de herdr
set -euo pipefail
MAX_PARALLEL=${MAX_PARALLEL:-2}          # 24 GB: dos, no cinco
KIND=${KIND:-pi}                          # pi | claude | codex

frontier() {                              # issues ready-for-agent sin blockers abiertos
  gh issue list --label ready-for-agent --state open --json number,body \
  | jq -r '.[] | select((.body | scan("[Bb]locked by:? #([0-9]+)") | length) == 0) | .number'
  # (versión completa: resolver cada #N y filtrar los que sigan abiertos)
}

for n in $(frontier | head -"$MAX_PARALLEL"); do
  wt=$(herdr worktree create --branch "ticket/$n" --base main --no-focus --json | jq -r '.result.path')
  pane=$(herdr pane split --current --direction right --cwd "$wt" --no-focus --json | jq -r '.result.pane.pane_id')
  herdr agent start "t$n" --kind "$KIND" --pane "$pane" -- --model xai/grok-4.5
  herdr agent prompt "t$n" "/implement #$n. Cuando el gate (./scripts/gate.sh) esté verde, abrí PR y parás." \
    --wait --until idle --until blocked --timeout 3600000
  herdr agent read "t$n" --source recent --lines 200 > ".harness/logs/$n.log"
done
```

Lo importante de este script no es el script: es que **la unidad de aislamiento es el
worktree**, la **unidad de trabajo es el issue**, y el **criterio de terminación es el gate**.
Cambiar `--kind pi` por `--kind claude` o `--kind codex` es una variable de entorno.
Esa es la "conexión a orquestadores" que buscás: las skills son markdown portable, el
ticket es el contrato, el runner es intercambiable.

`--until blocked` es clave: herdr detecta cuando el agente abrió un prompt de aprobación
o una pregunta. Ese es tu punto de intervención humana, y es explícito.

### Fase 3 — Sandcastle, cuando la Fase 2 duela

El síntoma que justifica el salto: dos agentes peleando por el puerto de Postgres,
o querer correr 5 tickets en paralelo y no tener RAM. Ahí sandcastle aporta lo que
herdr no: **sandbox real** (Docker/Podman con bind-mount, o Vercel Firecracker) con su
propio entorno, y branch strategy explícita.

```ts
import { run, claudeCode, pi } from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";

await run({
  agent: pi("xai/grok-4.5"),           // o claudeCode("claude-opus-5")
  sandbox: docker(),                    // Postgres propio por sandbox → sin colisión de puertos
  promptFile: ".sandcastle/prompt.md",  // el prompt renderiza el issue #N
  branch: { strategy: "branch", name: `ticket/${n}` },
});
```

Mismo contrato, distinto runner. El `dispatch.sh` de la Fase 2 pasa de llamar a herdr
a llamar a sandcastle, y nada más cambia. Con `docker()` local seguís limitado por los
24 GB; el desbloqueo real de escala es el sandbox de Vercel (microVMs en la nube), que
es donde esto deja de depender de tu máquina.

### Fase 4 — Qwen3.8 como obrero barato (ver Addendum §8: casi seguro NO local)

Instalación:

```bash
brew install llama.cpp
llama serve -hf unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M      # OpenAI-compatible en :8080
```

`~/.pi/agent/models.json`:

```json
{ "providers": { "llama-cpp": {
    "baseUrl": "http://localhost:8080/v1", "api": "openai-completions", "apiKey": "none",
    "models": [{ "id": "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M" }] } } }
```

**Qué SÍ le doy al modelo local** (tareas mecánicas, verificables, sin costo de error alto):
- Triage: leer un issue nuevo y proponer label (`needs-info` / `ready-for-agent`).
- Pre-flight de tickets: "¿este ticket tiene acceptance criteria testeables? sí/no + por qué".
- Resumir los logs de los panes (`herdr agent read`) a una línea por agente.
- Mensajes de commit, changelogs, release notes.
- Primera pasada de `/code-review` sobre el diff, para filtrar lo obvio antes de gastar Opus.
- Correr de noche sobre el backlog cuando la máquina está libre.

**Qué NO le doy:** el `/implement`, el `/grill-with-docs`, el spec. Con 24 GB y Q4 va a
andar a ~10-20 tok/s compitiendo con todo lo demás; usarlo de implementador es cambiar
costo de tokens por costo de tu tiempo, que es el recurso caro.

Esta fase es **la última porque es la de menor valor marginal**. Optimiza costo, no
capacidad. Si la hacés primera, vas a pasar tres días peleando con quants en vez de
arreglar tu gate.

---

## 4. Router de modelos

| Etapa | Runner | Modelo | Por qué |
|---|---|---|---|
| grill-with-docs, wayfinder | claude | Opus 5 (thinking alto) | ambigüedad + juicio; es donde se gana o se pierde todo |
| to-spec, to-tickets | claude | Opus 5 | el contrato tiene que estar bien |
| implement (ticket normal) | pi o claude | grok-4.5 / Sonnet | el spec ya hizo el trabajo duro |
| implement (ticket delicado: pagos, migraciones) | claude | Opus 5 | el memory ya dice: validar a mano mutaciones/schemas/pagos |
| code-review (1ª pasada) | pi | Qwen local | filtro barato |
| code-review (final) | claude | Opus 5 | el que firma |
| triage, changelogs, resúmenes | pi | Qwen local | mecánico |

---

## 5. Qué NO hacer

- **No empezar por el modelo local.** Es la fase de menor valor y la de más fricción.
- **No paralelizar antes de tener gate.** Multiplica errores, no throughput.
- **No adoptar las skills en los 5 repos a la vez.** Un piloto, tres ciclos completos, después se copia.
- **No mezclar spec-kit y to-spec en entrevestidos.**
- **No construir un "orquestador" propio.** `dispatch.sh` son 30 líneas de bash sobre `gh` + `herdr`.
  El día que necesite más, ese día existe sandcastle.
- **No sacar al humano del merge.** El merge sigue siendo mío. El memory ya dice: hurl verde = merge OK.
  Eso es una regla de decisión, no una delegación.

---

## 6. Criterios de éxito

- **Fase 0 lista:** `./scripts/gate.sh` es la única fuente de verdad de "esto mergea".
- **Fase 1 lista:** un ciclo grill→spec→tickets→implement completo, y sé el % de tickets
  que cierran solos.
- **Fase 2 lista:** `./dispatch.sh` deja 2 PRs abiertos con gate verde mientras hago otra cosa.
- **Fase 3 lista:** 5 tickets en paralelo sin que peleen por puertos ni RAM.
- **Fase 4 lista:** el triage del backlog lo hace la máquina y yo sólo confirmo.

## 7. Primeros tres comandos

```bash
# 1. elegir piloto y armar el gate
cd <repo-piloto> && $EDITOR scripts/gate.sh

# 2. instalar skills y configurar el repo
claude plugins install mattpocock-skills
claude -> /setup-matt-pocock-skills

# 3. un ciclo entero a mano, midiendo
claude -> /grill-with-docs   → /to-spec   → /to-tickets   → /implement #1
```

---

## 8. Addendum — dónde corre Qwen3.8 y dónde corre el harness

Precios verificados el 2026-08-21. Qwen3.8-27B salió el 14-ago-2026 (Apache 2.0, 28B denso,
262k nativo / 1M extendido, tool calling nativo).

### Hosted: más barato que cualquier GPU propia, por mucho

| Provider (vía OpenRouter) | In / Out $/M | tok/s |
|---|---|---|
| CoreWeave | $0.40 / $3.00 | 32 |
| Chutes | $0.40 / $3.00 | 22 |
| Venice | $0.45 / $3.20 | **77** |
| io.net | $0.48 / $3.40 | 72 |
| Alibaba Cloud Intl. | $0.575 / $3.45 | 48 |

Costo de un ticket típico de `/implement` (~300k input acumulado, ~40k output):

| Opción | Costo/ticket | 20 tickets/semana |
|---|---|---|
| Qwen3.8-27B hosted | **$0.24** | ~$5 / semana |
| grok-4.5 (lo que usás hoy en pi) | $0.84 | ~$17 / semana |
| Qwen3.8 local en la M4 Pro | $0 en tokens | 16.5 GB + ~10-20 tok/s + la máquina inutilizable |

Y una GPU alquilada 24/7 para servirlo:

| | $/hora | $/mes 24/7 |
|---|---|---|
| RTX 4090 (RunPod Community) | $0.34 | **$245** |
| A100 80GB (RunPod) | $1.39 | ~$1.000 |

**El break-even de un 4090 24/7 contra el endpoint hosted está en ~380M tokens/mes
(≈12M por día).** Trabajando solo no vas a acercarte ni de lejos. Alquilar GPU para
esto es pagar 50x por el mismo modelo.

Y hay una diferencia de capacidad, no sólo de plata: **el endpoint te da el contexto de 1M;
tu Q4 local en 24 GB no**, porque el KV cache de 262k no entra. Local no es "lo mismo pero
gratis", es un modelo recortado.

**Conclusión:** el modelo local se justifica por privacidad, offline o no tener rate limits.
No por costo. Si el driver es costo, la respuesta es un endpoint hosted.

### Lo bueno: es el mismo cableado

`pi` habla OpenAI-compatible, así que local y hosted son literalmente el mismo archivo
con otra URL. Podés prototipar local y flipear a hosted sin tocar nada más:

```jsonc
// ~/.pi/agent/models.json — local
{ "providers": { "llama-cpp": {
    "baseUrl": "http://localhost:8080/v1", "api": "openai-completions", "apiKey": "none",
    "models": [{ "id": "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M" }] } } }

// ~/.pi/agent/models.json — hosted (mismas 6 líneas)
{ "providers": { "openrouter": {
    "baseUrl": "https://openrouter.ai/api/v1", "api": "openai-completions",
    "apiKey": "sk-or-...", "models": [{ "id": "qwen/qwen3.8-27b" }] } } }
```

Como ya deployás en Vercel, **Vercel AI Gateway** es la otra variante: una sola key,
routing entre providers con failover y tracking de costo por etapa del pipeline —
útil justo cuando tenés un router de modelos como el de §4.

### "Dejarlo en una máquina": son dos cosas distintas

Esto es lo importante y conviene no confundirlo:

| | Qué necesita | Dónde |
|---|---|---|
| **El runner** (herdr + git + docker + gh + `dispatch.sh` + el gate) | CPU, disco, red, Docker. **Cero GPU.** | una máquina always-on tuya |
| **El modelo** | GPU / VRAM | un endpoint HTTP |

La máquina que dejás corriendo **no tiene que hospedar el modelo**. Tiene que correr tu
gate: Postgres en Docker, migraciones goose, el build de Go, los E2E. Eso es un laburo de
CPU. Opciones:

- **Mac mini M4 16-24 GB** (~US$600-1.000, una vez, ~30 W, silencioso): corre herdr con
  sesiones persistentes (`herdr --session`), tu Docker, tus E2E, y te conectás por SSH o
  `herdr --remote <ssh-target>`. Es la opción que más se parece a tu entorno actual, así
  que el gate va a comportarse igual que en tu máquina. **La recomendada.**
- **VPS (Hetzner dedicado, ~€15-30/mes)**: más barato, pero es x86/Linux — si tu gate
  depende de algo de macOS o de arm64 vas a debuggear diferencias en vez de features.
- **Tu propia M4 Pro**: gratis y ya la tenés. Contra: cuando el harness trabaja, vos no.

En las tres, el modelo se pide por HTTP. La GPU no entra en la ecuación.

### Orden sugerido para esta parte

1. Apuntar `pi` a `qwen/qwen3.8-27b` vía OpenRouter y usarlo en los roles baratos de §4
   (triage, resúmenes, 1ª pasada de review). Cinco minutos, cero infra.
2. Comparar contra grok-4.5 en tickets reales. Si Qwen aguanta, el costo de ejecución
   se divide por ~3,5.
3. Recién ahí, si querés independencia total, montar el llama.cpp local —
   sabiendo que es una decisión de soberanía, no de plata.

---

## 9. El ritmo real: 9-5 de lunes a viernes

Restricción nueva y la más importante de todas: **la ventana de atención son noches y
fines de semana.** Eso cambia dos decisiones del plan.

### El recurso escaso son las horas-noche, no los dólares

Si el harness sólo puede trabajar mientras dormís, cada noche es un presupuesto fijo de
~8 horas de máquina. Comparar entonces:

| | tok/s | Trabajo por noche de 8h |
|---|---|---|
| Qwen3.8 hosted (Venice/io.net) | 72-77 | ~2M tokens de salida |
| Qwen3.8 local, Q4, M4 Pro 24 GB | ~10-20 | ~400k |

Correr local para ahorrar ~$5 por semana te cuesta **5x menos trabajo terminado por noche**.
Es el peor cambio posible dado tu horario. **Hosted, sin dudarlo.** El local queda como
curiosidad de fin de semana, no como parte del harness.

### El agente no se puede trabar

Un agente que a las 02:00 abre un prompt de aprobación y espera a que alguien conteste
desperdició la noche entera. Con supervisión en vivo `--until blocked` es tu punto de
intervención; **sin supervisión es tu modo de falla principal.** Diseño para desatendido:

- Cada ticket en su **worktree propio**, y permisos amplios *dentro* de ese worktree.
  El aislamiento es lo que hace seguro no preguntar.
- **El gate es la red**, no la aprobación humana. Si `gate.sh` no pasa, no hay PR. Nunca merge automático.
- Si un agente igual se traba: `dispatch.sh` lo abandona, lo deja anotado, y **pasa al
  siguiente ticket de la frontera**. No se queda esperando.
- Nada de tocar prod de noche. Migraciones, pagos y schemas quedan etiquetados para hacerse
  despierto y a mano — ya es tu regla de siempre.

### Cronograma

```
23:00  launchd dispara dispatch.sh → trabaja la frontera
07:00  se detiene, deja PRs con gate verde + resumen de la noche
```

### Ritmo semanal

| Cuándo | Qué |
|---|---|
| Lun-Vie 9-5 | nada (o el Mac mini, si lo hay) |
| Noche, 1-2 h | revisar y mergear los PRs de anoche; después `/grill-with-docs` → `/to-spec` → `/to-tickets` para recargar la cola |
| Mientras dormís | `dispatch.sh` implementa la frontera |
| Fin de semana | lo grande: `/wayfinder`, ADRs, decisiones de arquitectura |

La clave: **vos producís tickets, la máquina los consume.** Si una noche no cargás la cola,
la noche siguiente el harness no tiene qué hacer. El trabajo de la persona pasa a ser
mantener la frontera llena, no escribir el código.

### ¿Mac mini, entonces?

Depende de una sola cosa: **¿la MacBook se va con vos al 9-5?**

- **Sí, se la lleva** → el Mac mini se justifica ya: convierte 8 horas de ventana en 24,
  triplicando el throughput semanal. Ahí se paga solo rápido.
- **No, queda en casa** → no lo necesitás todavía. Dejá la M4 Pro corriendo de noche
  (`herdr --session harness` sobrevive el logout), medí un par de semanas, y comprá el mini
  cuando el cuello sea real.

---

## 10. `harness status` (v0, 2026-08-22)

`agent-harness/bin/harness`, symlinkeado en `~/.local/bin/harness`. Una sola pantalla,
~2,5s, no escribe nada. Deliberadamente un script y no una app: la idea es vivir con él
dos semanas y recién ahí especificar el dashboard con datos de uso en vez de suposiciones.

Muestra:
- **Presupuesto**: créditos de OpenRouter (lee la key de `~/.pi/agent/models.json`,
  la misma que usa pi) + estimación de tickets restantes.
- **Agentes vivos**: `herdr agent list`, ordenados `blocked` → `working` → `idle`,
  con el repo de cada uno. Requiere estar dentro de una sesión de herdr.
- **Trabajo**: por repo, la frontera real (issues `ready-for-agent` cuyos blockers
  ya están cerrados), los bloqueados, los PRs esperando review y los `needs-triage`.
- **Listo para el harness**: qué le falta a cada repo (gate → skills → CONTEXT.md).
  Esta es la tabla que responde "qué necesito en cada proyecto".

Repos seguidos: `repos.conf`. Uso: `harness status`, o `-q` para saltear la tabla
de readiness.
