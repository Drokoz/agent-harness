"""Las partes del CLI `harness`, separadas por lo que arriesgan.

- `harness.adapters`: todo lo que toca el mundo (red, disco, subprocesos) y no decide nada.
- `harness.snapshot`: toda la lógica, en funciones puras.
- `harness.render`: sólo dibuja.

`bin/harness` los pega: adapters -> snapshot -> render -> stdout.
"""
