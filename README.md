# agent-harness

`./scripts/gate.sh` corre la suite de unittest y una corrida real de `harness status`
(en modo sin adaptadores, sin red); sale 0 si mergea y distinto de 0 si algo está
roto (nada se salta en silencio). Busca un intérprete de Python 3.9 explícito
(`python3.9`, `/usr/bin/python3`); si no hay ninguno, es rojo.

`bin/harness` imprime el estado del trabajo autónomo, un bloque por contexto. Los
contextos —repos, tracker, autonomía, presupuesto, vault y cómo ejecutar— se declaran
en `~/.config/harness/config.json`: ver `docs/harness/config.md`.

`harness run` es el dispatcher (PLAN.md, §11): lanza la frontera desbloqueada del
contexto, un worktree y un pane de herdr por ticket, con tope de paralelismo
(`--max`, por defecto 2). El gate es la red: ningún PR sin `scripts/gate.sh` verde,
y nunca hay merge automático. Todo queda anotado en un log JSONL append-only de
eventos. Un contexto con `autonomy: manual` no se toca solo.
