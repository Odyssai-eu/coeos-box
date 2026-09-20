"""Les providers que la box sait servir — DEFINIS PAR L'OPERATEUR.

Deux fournis d'origine (OpenRouter, un local generique), et autant d'autres que
le client veut : il les ajoute au dashboard avec un nom, une URL et une cle
eventuelle. Ils vivent en CONFIG, pas dans ce fichier — un dict fige en dur
aurait voulu dire "les providers que NOUS avons prevus", ce qui contredit le
produit.

La distinction qui porte le modele :

- **cloud** : une cle du client, chez le client. Sans cle, l'upstream refuse.
- **local** : un endpoint OpenAI-compatible sur le reseau du client — OdyssAI-X,
  Ollama, une machine du LAN. **Aucune cle**, et rien ne sort de chez lui.

Un provider local n'a donc pas de cle a resoudre : l'exiger rendrait le 100 %
on-prem impossible.
"""

from __future__ import annotations

import os

import httpx

PROVIDER_ID = "openrouter"   # defaut historique quand rien n'est precise
LOCAL_PROVIDER_ID = "odyssai"

# Les deux d'origine. Ce ne sont pas une liste fermee : `providers_of(cfg)` y
# ajoute ceux que l'operateur a declares. registry_field = la cle qu'une entree
# de registre utilise pour son id natif, ex. {"glm-5.2": {"or": "z-ai/glm-5.2"}}.
BUILTIN_PROVIDERS: dict[str, dict] = {
    "openrouter": {
        "label": "OpenRouter",
        "api_base": "https://openrouter.ai/api/v1",
        "registry_field": "or",
        "api_key_env": "OPENROUTER_API_KEY",
        # Le nom du drapeau de raisonnement CHEZ CE PROVIDER.
        "thinking_field": "thinking",
        # Explicite : sans ça, la règle des providers ajoutés s'appliquerait et
        # OpenRouter passerait pour servable sans clé.
        "keyless": False,
        "keys_url": "https://openrouter.ai/settings/keys",
        "models_url": "https://openrouter.ai/models",
    },
    "odyssai": {
        "label": "Local (OpenAI-compatible)",
        # Defaut vide : l'adresse appartient au client, elle se saisit au
        # dashboard. Coder une IP ici serait exactement le hard-code que le
        # registre logique existe pour eviter.
        "api_base": "",
        "registry_field": "endpoint",
        "api_key_env": "ODYSSAI_API_KEY",   # optionnel : la plupart n'en ont pas
        # OdyssAI-X n'ecoute QUE `enable_thinking` — verifie au fil sur le
        # 397B le 2026-08-18 : `thinking:false` est ignore et le modele passe
        # tout son budget en raisonnement (reponse vide).
        "thinking_field": "enable_thinking",
        "keyless": True,
        "keys_url": "",
        "models_url": "",
    },
}

# Prefixes accepted in explicit model ids ("or:z-ai/glm-5.2").
PREFIX_TO_PROVIDER = {"or": "openrouter", "openrouter": "openrouter",
                      "local": "odyssai", "odyssai": "odyssai"}

# Ce qu'un provider ajoute par l'operateur vaut par defaut. `endpoint` comme
# champ de registre (l'orthographe generique), `thinking` comme drapeau de
# raisonnement (le plus repandu) — les deux restent modifiables par entree.
_CUSTOM_DEFAULTS = {
    "registry_field": "endpoint",
    "thinking_field": "thinking",
    "api_key_env": "",
    "keys_url": "",
    "models_url": "",
    "custom": True,
}


def providers_of(cfg: dict) -> dict[str, dict]:
    """Les providers connus de CETTE box : les deux d'origine, plus ceux que
    l'operateur a declares. Un provider declare peut surcharger les metadonnees
    d'un builtin (son label, son champ de raisonnement) sans le remplacer."""
    out = {pid: dict(meta) for pid, meta in BUILTIN_PROVIDERS.items()}
    declared = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    for pid, entry in declared.items():
        if not isinstance(entry, dict):
            continue
        base = out.get(pid)
        if base is None:
            base = {**_CUSTOM_DEFAULTS, "label": entry.get("label") or pid, "api_base": ""}
        # Seules les metadonnees voyagent ici ; api_key / api_base / enabled
        # restent lus par leurs accesseurs dedies (provider_key, api_base...).
        for field in ("label", "registry_field", "thinking_field", "keys_url", "models_url"):
            value = entry.get(field)
            if isinstance(value, str) and value.strip():
                base[field] = value.strip()
        out[pid] = base
    return out


def provider_keyless(pid: str, cfg: dict | None = None) -> bool:
    """Ce provider sert-il sans cle ? Les builtins le declarent ; un provider
    ajoute par l'operateur est keyless tant qu'aucune cle n'est saisie — on ne
    lui demande pas de dire s'il en veut une, on regarde s'il en a une."""
    meta = (providers_of(cfg) if cfg is not None else BUILTIN_PROVIDERS).get(pid, {})
    if "keyless" in meta:
        return bool(meta["keyless"])
    if cfg is None:
        return False
    return not (provider_cfg(cfg, pid).get("api_key") or "").strip()


# Compat : du code lit encore PROVIDERS comme un dict global. Il ne contient que
# les builtins — tout ce qui doit voir les providers de l'operateur passe par
# providers_of(cfg).
PROVIDERS = BUILTIN_PROVIDERS


def api_base(cfg: dict, pid: str = PROVIDER_ID) -> str:
    """L'adresse du provider. Celle d'un provider local vient de la config du
    client — c'est SON reseau, on ne la connait pas."""
    configured = (provider_cfg(cfg, pid).get("api_base") or "").strip()
    return configured or providers_of(cfg).get(pid, {}).get("api_base", "")


def provider_cfg(cfg: dict, pid: str = PROVIDER_ID) -> dict:
    return (cfg.get("providers") or {}).get(pid) or {}


def provider_key(cfg: dict, pid: str = PROVIDER_ID) -> str | None:
    """Resolve the upstream key for the CURRENT caller.

    Un compte authentifié (S1.2) est servi par le coffre — sa clé byok ou la
    clé plateforme selon son mode, et RIEN d'autre : la clé globale de
    l'opérateur (config/env) n'est jamais un repli pour un compte. Sans
    compte (instance ouverte, dev, /health), comportement historique :
    config du dashboard → env."""
    from . import accounts
    account = accounts.current_account.get()
    if account and int(account.get("user_id") or 0) != 0:
        k = accounts.account_provider_key(account, pid)
        if k:
            return k
        # Un compte ADMIN est l'operateur : faute de cle a lui, il retombe sur
        # la cle "maison" de l'instance (config/env ci-dessous) — c'est SA cle.
        # Un compte byok NON-admin ne retombe JAMAIS : il apporte sa cle ou 503
        # (isolation S1.2, decision #5). Un compte platform sans cle plateforme
        # non plus.
        if not account.get("admin"):
            return None
    pc = provider_cfg(cfg, pid)
    direct = (pc.get("api_key") or "").strip()
    if direct:
        return direct
    env_var = providers_of(cfg).get(pid, {}).get("api_key_env") or ""
    return (os.environ.get(env_var) or "").strip() or None if env_var else None


def provider_enabled(cfg: dict, pid: str = PROVIDER_ID) -> bool:
    """Enabled unless explicitly set to False (default True)."""
    return provider_cfg(cfg, pid).get("enabled", True) is not False


def provider_ready(cfg: dict, pid: str = PROVIDER_ID) -> bool:
    """Le provider peut servir : connu, active, et joignable.

    Cloud : il faut une cle. Local : il faut une adresse — exiger une cle
    rendrait le 100 % on-prem impossible."""
    if pid not in providers_of(cfg) or not provider_enabled(cfg, pid):
        return False
    if provider_keyless(pid, cfg):
        return bool(api_base(cfg, pid))
    return provider_key(cfg, pid) is not None


def ready_providers(cfg: dict) -> list[str]:
    return [pid for pid in providers_of(cfg) if provider_ready(cfg, pid)]


def redact_provider(cfg: dict, pid: str = PROVIDER_ID) -> dict:
    """Safe-to-return view. NEVER includes the api_key."""
    meta = providers_of(cfg).get(pid, {})
    pc = provider_cfg(cfg, pid)
    stored = bool((pc.get("api_key") or "").strip())
    env_set = bool((os.environ.get(meta.get("api_key_env") or "") or "").strip()) if meta.get("api_key_env") else False
    return {
        "id": pid,
        "label": meta.get("label", pid),
        "api_base": api_base(cfg, pid),
        "api_key_env": meta.get("api_key_env", ""),
        "api_key_set": provider_key(cfg, pid) is not None,
        "api_key_source": "config" if stored else ("env" if env_set else "none"),
        "enabled": provider_enabled(cfg, pid),
        "ready": provider_ready(cfg, pid),
        "keys_url": meta.get("keys_url", ""),
        "label_custom": bool(meta.get("custom")),
        "registry_field": meta.get("registry_field", "endpoint"),
        "thinking_field": meta.get("thinking_field", "thinking"),
    }


async def list_upstream_models(cfg: dict, pid: str = PROVIDER_ID) -> list[dict]:
    """Interroge le /models du provider — c'est ce que fait le bouton Test, et
    c'est aussi ce qui peuple le picker de modeles."""
    headers: dict[str, str] = {}
    key = provider_key(cfg, pid)
    if key:
        headers["authorization"] = f"Bearer {key}"
    url = f"{api_base(cfg, pid).rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                d = r.json()
                return d.get("data", []) if isinstance(d, dict) else []
    except Exception:
        pass
    return []


def thinking_field(pid: str | None, cfg: dict | None = None) -> str:
    """Le nom du drapeau de raisonnement chez ce provider.

    `thinking` côté cloud, `enable_thinking` chez OdyssAI-X. Se tromper de nom
    est silencieux : aucune erreur, le modèle raisonne quand même."""
    known = providers_of(cfg) if cfg is not None else BUILTIN_PROVIDERS
    return known.get(pid or PROVIDER_ID, {}).get("thinking_field", "thinking")
