# agent-harness

Herramientas para trabajar de forma autónoma: un pipeline donde una persona produce
tickets y una flota de agentes los consume, con un gate mecánico decidiendo qué mergea.

`PLAN.md` es el documento canónico. Leerlo antes de tocar nada.

Este repo se lee desde varios runners (Claude Code, pi, codex), por eso el contexto vive
en `AGENTS.md` y no en `CLAUDE.md`: las skills en `.claude/skills/` y `.pi/skills/` son
el mismo markdown, y cambiar de agente no debe cambiar el proceso.

## Agent skills

### Issue tracker

Los issues viven en GitHub Issues de este repo (`Drokoz/agent-harness`), vía la CLI `gh`.
See `docs/agents/issue-tracker.md`.

### Triage labels

El vocabulario canónico de cinco roles, sin renombrar: `needs-triage`, `needs-info`,
`ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: un `CONTEXT.md` en la raíz y ADRs en `docs/adr/`.
See `docs/agents/domain.md`.

## Output economy

Output tokens cost several times more than input (Qwen3.8: $0.40 in / $3.00 out). Verbosity
is the single largest controllable cost in this project. These rules are not style advice.

**Language.** Spanish to Tomás. **English for everything machine-facing**: prompts to other
agents, handoffs, structured returns, subagent reports. Durable artifacts in this repo —
commits, PRs, issues, ADRs — stay in Spanish, for consistency with what already exists.

**Say less.**

- No preamble, no postamble. Never announce what you are about to do.
- Report the result, not the process. The steps are visible in the transcript already.
- Never echo back a file you just read or wrote. Reference it by path.
- A final report is the deliverable, not a summary of the deliverable.
- Structured returns are the bare object. No prose wrapper, no explanation of the schema.
- Do not restate the ticket, the acceptance criteria, or the user's request.
- Agent-to-agent reports: 10 lines max unless the receiver asked for more.
- No decoration in machine-facing output: no tables, no emoji, no ASCII art.
- When something fails, one line of what and one line of why. Not the whole log.

**Say it anyway when it changes a decision.** Brevity is not omission. A caveat the reader
needs, a defect you found, a limit you hit: those are cheap and always worth the tokens.
Silence about a real problem costs far more than the words would have.

## Modelos en pi

No declarar modelos explícitamente en `~/.pi/agent/models.json`: un provider con lista de
`models` hace que pi les asigne specs por defecto conservadoras (128K de contexto, 16.4K de
salida, sin thinking). Sin esa lista, pi autodescubre el catálogo real del provider.

Y no usar el sufijo `:nitro`: rutea al proveedor más rápido, pero en el catálogo aparece como
una entrada distinta y degradada. `qwen/qwen3.8-27b` da 262K/131K con thinking;
`qwen/qwen3.8-27b:nitro` da 128K/16.4K sin thinking.
