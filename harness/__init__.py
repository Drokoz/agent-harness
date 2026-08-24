"""Las partes del CLI `harness`, separadas por lo que arriesgan.

- `harness.config`: qué es un contexto y qué config es válida. Puro; no lee nada.
- `harness.adapters`: todo lo que toca el mundo (red, disco, subprocesos) y no decide nada.
- `harness.snapshot`: toda la lógica, en funciones puras.
- `harness.state`: el reductor del historial: qué se intentó y con qué
  resultado. Puro; no lee nada.
- `harness.render`: sólo dibuja.

`bin/harness` los pega: config -> adapters -> snapshot -> render -> stdout.
"""
