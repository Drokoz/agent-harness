# El routing de OpenRouter: declarado, verificado y con evidencia (#126)

La flota pasa por OpenRouter, que rutea `qwen/qwen3.8-27b` entre varios
proveedores de atrás. Si uno de ellos limita, toda la noche se apaga en la
misma línea: la del 2026-09-01 OpenRouter eligió **Reka**, Reka devolvió
`Upstream error from Reka: Too many requests`, y seis de siete tickets se
abandonaron en esa misma línea —sin que un solo fallo fuera del modelo.

`pi` deja excluir proveedores con `compat.openRouterRouting` a nivel de
provider en `~/.pi/agent/models.json`, sin declarar el modelo. El problema no
era el arreglo, era dónde vivía: editado a mano, fuera del repo —no versionado,
no testeado, y una reinstalación lo pierde en silencio. La misma clase de
dependencia invisible que el `.env` de un worktree.

## Las tres piezas

- **La declaración** — `harness/routing.json`, en el repo: qué routing necesita
  el harness (`allow_fallbacks` y la lista de proveedores a `ignore`), con la
  evidencia de dónde salió la lista.
- **El estado** — `harness/routing.py`: puro, contra el dict de una
  models.json. `ok | ausente | distinto | sin-config`. Lo extra en lo real no
  invalida: el routing esperado está puesto aunque haya algo más.
- **El disco** — `harness doctor` lo mira y `harness doctor --fix` lo aplica,
  preservando el resto del archivo (apiKey, otros providers): idempotente,
  sin editar JSON a mano.

## `harness doctor`

    harness doctor                el estado + la evidencia del log
    harness doctor --fix          aplica el routing declarado a models.json

Salida:

    Routing OpenRouter  ✓ ok
      esperado  allow_fallbacks=True ignore=reka
      real      allow_fallbacks=True ignore=reka
      evidencia del log  reka (6)
      evidencia sin declarar: alibaba — revisar y sumarlo a harness/routing.json

- **La evidencia sale del log, no de una corazonada**: `costo_pi` ya guarda el
  `errorMessage` de cada sesión, y `proveedores_con_error` extrae el proveedor
  de atrás de los `Upstream error from X: ...`. Un proveedor con evidencia que
  no está en la lista declarada se marca: es la revisión que evita que la
  lista se quede atrás de la realidad.
- **Salidas**: 0 con el routing en orden; 1 cuando no (ausente, distinto,
  sin-config); 2 con la declaración faltante o un `--fix` que no pudo aplicar.
  Todo disco local: corre igual offline.

## `harness status`

La línea `Routing` se dibuja junto a la readiness (debajo de `Agentes`):

    Routing       ✓ ok  ignore: reka

Ausente o distinto, la línea dice el estado y la salida es la misma:
`harness doctor --fix`. Sin `~/.pi/agent/models.json` (o sin declaración en el
repo) se marca en su lugar, sin romperse: una máquina recién instalada no es
un error de status.
