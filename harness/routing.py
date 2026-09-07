"""El routing de OpenRouter: qué necesita el harness, dónde vive y si está.

La flota pasa por OpenRouter, que rutea entre proveedores de atrás. Si uno de
ellos limita (`Upstream error from Reka: Too many requests`, #126), se apaga
una noche entera. `pi` deja excluir proveedores con `compat.openRouterRouting`
en el provider de `~/.pi/agent/models.json` —sin declarar el modelo—, pero
eso vivía editado a mano, fuera del repo: no versionado, no testeado, y una
reinstalación lo pierde en silencio (la misma clase de dependencia invisible
que el `.env` de un worktree).

Acá se separa en tres:

- la declaración (`routing.json`, junto a este archivo): lo que el harness
  necesita, en el repo;
- el estado (`estado`): puro, contra un dict de models.json sintético;
- el disco (`leer_models`/`aplicar`): lo que `harness doctor` usa para mirar
  y arreglar `~/.pi/agent/models.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Tuple

# Dónde pi guarda la config del provider, y dónde el repo declara lo esperado.
MODELS = "~/.pi/agent/models.json"
DECLARACION = Path(__file__).resolve().with_name("routing.json")


def cargar_declaracion(path=DECLARACION) -> Optional[dict]:
    """Lo que el repo declara (`routing.json`), o None si no hay / está roto."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def leer_models(path=MODELS) -> Optional[dict]:
    """`~/.pi/agent/models.json` crudo, o None si no existe o no se lee."""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def compat_de(models, provider) -> Optional[dict]:
    """El `compat` del provider, o None si no hay ninguno.

    Navegación defensiva: una models.json vieja puede no tener `compat`, o
    el provider puede no existir todavía."""
    if not isinstance(models, dict):
        return None
    providers = models.get("providers")
    if not isinstance(providers, dict):
        return None
    prov = providers.get(provider)
    if not isinstance(prov, dict):
        return None
    compat = prov.get("compat")
    return compat if isinstance(compat, dict) else None


def routing_de(models, provider) -> Optional[dict]:
    """El `compat.openRouterRouting` del provider, o None si no hay ninguno."""
    compat = compat_de(models, provider)
    if compat is None:
        return None
    r = compat.get("openRouterRouting")
    return r if isinstance(r, dict) else None


def estado(declaracion, models) -> Tuple[str, str]:
    """`(estado, detalle)` del routing contra lo que el repo declara.

    `estado`: `ok` | `ausente` | `distinto` | `sin-config`. Puro: `models`
    es el dict de una models.json (sintética en los tests) y None cuando el
    archivo no existe o no se pudo leer. Los keys que la declaración exige
    se miran todos; los extra en lo real no invalidan (el routing esperado
    está puesto aunque haya algo más).
    """
    provider = (declaracion or {}).get("provider") or "openrouter"
    esperado = (declaracion or {}).get("compat")
    if not isinstance(esperado, dict) or not esperado:
        return ("distinto", "la declaración no trae compat")
    if models is None:
        return ("sin-config", "no hay " + MODELS + " (o no se pudo leer)")
    real = compat_de(models, provider)
    if not isinstance(real, dict) or not isinstance(real.get("openRouterRouting"), dict):
        return ("ausente", "sin openRouterRouting en providers.{}".format(provider))
    difs = _dif(esperado, real)
    if difs:
        return ("distinto", "; ".join(difs))
    return ("ok", "")


def _dif(expected, real, prefijo=""):
    """Las diferencias de `expected` contra `real`, recursivo: un dict
    esperado se chequea key por key (lo extra en lo real no invalida: el
    routing esperado está puesto aunque haya algo más); un scalar se
    compara igualdad directa."""
    if isinstance(expected, dict):
        if not isinstance(real, dict):
            return ["{}≠ dict".format(prefijo or "routing")]
        difs = []
        for k, v in expected.items():
            if k not in real:
                difs.append("{}falta {}".format(prefijo, k))
            else:
                difs.extend(_dif(v, real[k],
                                 "{}{}.".format(prefijo, k)))
        return difs
    if real != expected:
        return ["{}{} ≠ {}".format(
            prefijo, json.dumps(real, ensure_ascii=False),
            json.dumps(expected, ensure_ascii=False))]
    return []


def aplicar(declaracion, path=MODELS) -> Tuple[bool, str]:
    """Pone el routing declarado en la models.json, preservando el resto.

    El resto del archivo es del usuario (apiKey, otros providers, overrides):
    se toca sólo `providers.<provider>.compat`, y dentro de
    `openRouterRouting` lo esperado gana por key. Idempotente. Devuelve
    `(ok, mensaje)`: nunca levanta.
    """
    path = Path(path).expanduser()
    try:
        texto = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (False, "no hay {}: primero la config de pi, "
                       "después doctor --fix".format(path))
    except OSError as e:
        return (False, "no se pudo leer {}: {}".format(path, e.strerror))
    try:
        models = json.loads(texto)
    except ValueError as e:
        return (False, "{} no es JSON: {}".format(path, e))
    if not isinstance(models, dict):
        return (False, "{} no es una models.json válida".format(path))
    provider = (declaracion or {}).get("provider") or "openrouter"
    esperado = (declaracion or {}).get("compat")
    if not isinstance(esperado, dict) or not esperado:
        return (False, "la declaración no trae compat")
    providers = models.setdefault("providers", {})
    prov = providers.setdefault(provider, {})
    if not isinstance(prov, dict):
        return (False, "providers.{} no es un objeto".format(provider))
    compat = prov.get("compat")
    if not isinstance(compat, dict):
        compat = {}
    for k, v in esperado.items():
        if k == "openRouterRouting" and isinstance(v, dict):
            base = compat.get("openRouterRouting")
            merged = dict(base) if isinstance(base, dict) else {}
            merged.update(v)
            compat[k] = merged
        else:
            compat[k] = v
    prov["compat"] = compat
    try:
        path.write_text(json.dumps(models, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    except OSError as e:
        return (False, "no se pudo escribir {}: {}".format(path, e.strerror))
    return (True, "aplicado en {}".format(path))
