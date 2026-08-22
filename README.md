# agent-harness

`./scripts/gate.sh` corre la suite de unittest y una corrida real de `harness status`
(en modo sin adaptadores, sin red); sale 0 si mergea y distinto de 0 si algo está
roto (nada se salta en silencio). Busca un intérprete de Python 3.9 explícito
(`python3.9`, `/usr/bin/python3`); si no hay ninguno, es rojo.

`bin/harness` imprime el estado del trabajo autónomo, un bloque por contexto. Los
contextos —repos, tracker, autonomía, presupuesto, vault y cómo ejecutar— se declaran
en `~/.config/harness/config.json`: ver `docs/harness/config.md`.
