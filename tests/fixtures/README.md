# Fixtures

Salidas crudas de los adaptadores, capturadas del mundo real. Son la entrada de
`snapshot()`: si mañana `gh` o `herdr` cambian su forma, se recaptura acá y los
tests de snapshot lo acusan.

| archivo | comando |
| --- | --- |
| `gh_issue_list.json` | `gh issue list --state open --limit 200 --json number,title,labels,body -R Drokoz/agent-harness` |
| `gh_issue_graphql.json` | `gh api graphql` con la consulta `ISSUES_GQL` de `harness/adapters.py` (issues abiertos + `issueDependenciesSummary.blockedBy`), owner=Drokoz name=agent-harness |
| `gh_pr_list.json` | `gh pr list --state all --limit 3 --json number,title,isDraft,headRefName -R Drokoz/agent-harness` |
| `herdr_agent_list.json` | `herdr agent list` |
| `openrouter_credits.json` | `GET https://openrouter.ai/api/v1/credits` |
| `claude_sessions/` | layout sintético de `~/.claude/projects/` (una carpeta por proyecto con el path codificado, `<sesion>.jsonl` por sesión), para `harness quota`; las líneas asistente tienen el schema real de `message.usage` |

Capturadas el 2026-08-22, reindentadas (`json.dumps(..., indent=2, sort_keys=True)`)
para que un diff se lea; el contenido no se tocó, con una excepción: los montos de
`openrouter_credits.json` están cambiados a valores redondos. La forma es la real —
`data.total_credits` y `data.total_usage`, que es todo lo que lee el harness — pero
capturarla en vivo pide la API key personal, y el saldo real no es un dato que este
repo necesite versionar.
