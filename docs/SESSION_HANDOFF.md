# Relevo de sesión — agent-harness

Estado al **2026-08-24, 21:00 (America/Santiago)**. Leer esto primero; `PLAN.md` sigue
siendo el documento canónico del diseño, y los issues de GitHub son la verdad del trabajo
pendiente.

---

## Dónde está cada cosa

| Qué | Dónde |
|---|---|
| El diseño y su historia | `PLAN.md` |
| Cómo trabajar en este repo | `AGENTS.md` |
| El spec de orquestación v1 | issue **#34** |
| Lo que falta | issues abiertos, etiqueta `ready-for-agent` |
| El gate | `./scripts/gate.sh` — única fuente de verdad de "esto mergea" |
| El estado en vivo | `harness status --context harness` |
| El bucle de la noche | `./scripts/noche.sh` (con `caffeinate -imsu`) |
| El guard del agente | `extensions/harness-guard/` — copiado a `~/.pi/agent/extensions/` |

---

## Qué se construyó el 23 y 24 de agosto

El repo pasó de 272 a 486 tests. Mergeado y verificado:

- **Gate honesto** (#36): exige árbol limpio, verifica que el HEAD del PR sea lo que midió,
  y rechaza diffs que toquen el gate o los workflows.
- **Historial con intentos** (#35): cada evento lleva `run_id`, `ticket` y `attempt`; un
  reductor puro (`harness/state.py`) contesta intentos, motivos, duraciones y tasas.
- **Frontera con estado** (#43): `libre | despachado | pr-abierto | mergeado | parkeado`.
  Una corrida muerta vuelve sola a `libre`.
- **Clases de abandono** (#37): `infra | modelo | humano`. Una falla de plomería no gasta
  peldaño de escalada.
- **Escalera de reintentos** (#38): `ESCALERA` en `harness/dispatch.py`. Dos peldaños de
  Qwen (el segundo con `--thinking high` y la cola del gate del intento anterior en el
  prompt), después Sonnet, después Opus, después park. El peldaño se recalcula leyendo el
  log, así que sobrevive reiniciar el dispatcher.
- **Cuota medible** (#39, #54, #47): `harness quota` lee las sesiones locales de Claude
  Code, pondera por costo relativo y atribuye por proyecto y por ticket.
- **Costo por job** (#53) y **resumen reconciliado contra GitHub** (#55).

---

## Reglas aprendidas operando (no están en el diseño, salieron de las corridas)

1. **El prompt de cierre pesa más que el modelo.** Decir "abrí el PR si el gate está verde"
   sin exigir commitear dio 1 de 6. Con la secuencia completa y la consecuencia explícita
   ("terminar con cambios sin commitear es un fracaso y el trabajo se descarta"), 5 de 5.
2. **Verde no significa "hizo algo".** Un worktree sin commits pasa el gate. Ver #67.
3. **Una conclusión sobre el modelo puede ser de la configuración.** Qwen dio 4/4 y después
   1/6 con la misma config. Antes ya había pasado con el sufijo `:nitro`.
4. **La red que rechaza tiene que preservar.** Ver #66.
5. **El silencio no es confirmación.** Verificar contra la API, no contra el índice de
   búsqueda de GitHub ni contra el exit code de un comando que reporta éxito igual.
6. **Los prompts van en una sola línea** y el primero después de `agent start` se pierde
   siempre: hay que verificar que el contexto suba de 0 % y reintentar.
7. **Un prompt no es una ley.** Lo que tiene que ser imposible se hace imposible, no se
   pide. `extensions/harness-guard/` bloquea merge, force-push, `reset --hard` y escrituras
   fuera del worktree desde la tool, antes de que se ejecuten. El prompt sigue diciéndolo,
   pero ya no es lo único que lo sostiene.
8. **El síntoma que ve el dispatcher no es la causa.** "El agente terminó sin PR abierto"
   es lo que se observa; el porqué lo guarda pi en `stopReason`/`errorMessage`. Cinco
   abandonos del 24 de agosto, clasificados `modelo`, eran un solo corte de red de siete
   minutos. Antes de sacar conclusiones sobre un modelo, mirar `costo_pi.salida_de`.
9. **Fallar en silencio con código 0 es peor que fallar.** Un `/tmp/harness-stop` viejo se
   comió la noche del 25 y el log parecía una corrida normal. Ver #91.

---

## Presupuesto y cuota (medido el 2026-08-24)

- **OpenRouter**: quedan **US$1.86** de US$10. Un ticket en el peldaño 1 sale ~US$0.43.
- **Cuota de Claude**: `drokoz` y `tomasherceg` **comparten una única cuenta y una única
  bolsa semanal** (confirmado con `/usage` desde los dos usuarios).
- **Constante de calibración**: **10,47M tokens ponderados por punto porcentual**; tope
  semanal **≈1.047M** con el promo de +50 % vigente hasta el 31-ago, **≈698M** sin promo.
  Re-medir después del **4 de septiembre**, que es la primera semana limpia.
- El reset semanal es **viernes 17:00 America/Santiago**.
- `harness quota` sólo ve un usuario del sistema, así que su total es un **piso** (#75).
  Hay un script suelto en `/Users/Shared/quota-semana.py` para medir el otro usuario.

**Dato que reordena prioridades:** en la semana del 21-ago, las sesiones de diseño
consumieron 14,3 puntos, los agentes 4,6 y el 9-5 sólo 3,4. Los agentes son la parte
barata. Es una sola semana y atípica, pero apunta a que acortar las sesiones largas vale
más que cualquier regla de qué día correr agentes.

---

## La cola, por umbral

**Umbral 1 — dejarlo corriendo de noche sin que nadie toque nada**

| Ticket | Qué |
|---|---|
| #41 | Watchdog de progreso: matar al agente que no avanza |
| #64 | Rebasar la rama del reintento sobre main antes de despachar |
| #66 | Al abandonar, preservar el trabajo (commit WIP) y la transcripción |
| #68 | El tamaño de la tanda lo decide el presupuesto, más `--only` y `--max-tickets` |
| #72 | El contador de intentos se contamina entre repos con el mismo número de issue |

**Umbral 2 — que la mañana cueste veinte minutos**: #56 (mantenedor de conflictos),
#46 (revisor barato del diff), #74 (desgloses de quota ponderados y corte por día).

**Umbral 3 — que se administre solo**: #45 (router), #40 (headless, `ready-for-human`),
#42 (sesión mínima), #44 (stacking), #65 (costo real por ticket), #75 (quota multiusuario).

Fuera de esos: #30, #33 (backlog viejo), #8 y #5 (contexto trabajo, otra vía).

---

## Cómo lanzar una tanda

```bash
harness status --context harness      # ver la frontera y su estado
harness run --context harness --max 2 # despachar; la escalera elige runner y modelo
```

`--kind` y `--model` son **sólo un fallback**: desde #38 la escalera fija el peldaño por
ticket, y un ticket sin historia arranca siempre en pi + Qwen.

**Gotcha vigente:** `harness run` toma **toda** la frontera. Para acotar una tanda hoy hay
que quitarle `ready-for-agent` a los que no entran y devolvérselo después — y verificar
por API, no por `gh issue list`, que va atrasado. Eso es exactamente lo que elimina #68.

**El merge sigue siendo humano.** Verificar de forma independiente (correr el gate, leer
el diff), y recién ahí mergear. Nunca merge automático.
