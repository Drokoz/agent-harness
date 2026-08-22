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
