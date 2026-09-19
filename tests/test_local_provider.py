"""Le provider local — ce qui rend le tier `open` démontrable, pas promis.

Un endpoint OpenAI-compatible sur le réseau du client (OdyssAI-X, Ollama, une
machine du LAN) : aucune clé, rien ne sort de chez lui.
"""

import pytest
from fastapi.testclient import TestClient

from coeos import providers, router
from coeos.app import app
from coeos.config import load_config
from tests.conftest import BASE_SETTINGS

LOCAL_BASE = "http://engine.lan:8000/v1"

REGISTRY = {
    "qwen-122b": {"name": "Qwen 3.5 122B", "provider": "odyssai",
                  "endpoint": "qwen3-5-122b-a10b"},
    "gemma-4":   {"name": "Gemma 4 31B", "provider": "odyssai",
                  "endpoint": "gemma-4-31b-it"},
    "glm":       {"name": "GLM 5.2", "or": "z-ai/glm-5.2"},   # cloud, sans `provider`
}


@pytest.fixture()
def client(cfg_file):
    with TestClient(app) as c:
        yield c


def _local(write_cfg, **extra):
    write_cfg({"coeos": {**BASE_SETTINGS, "models": REGISTRY,
                         "axes": [{"key": "code", "label": "Code", "model": "qwen-122b"},
                                  {"key": "plan", "label": "Plan", "model": "glm"}],
                         "decider": {"name": "Gemma 4", "provider": "odyssai",
                                     "endpoint": "gemma-4-31b-it"}},
               "providers": {"odyssai": {"api_base": LOCAL_BASE}, **extra}})


# ── Un provider local n'a pas de clé ────────────────────────────────────────

def test_local_is_ready_without_any_key(write_cfg):
    """Exiger une clé rendrait le 100 % on-prem impossible."""
    _local(write_cfg)
    cfg = load_config()
    assert providers.provider_ready(cfg, "odyssai") is True
    assert providers.provider_key(cfg, "odyssai") is None
    # Le cloud, lui, reste indisponible sans clé — les deux familles cohabitent.
    assert providers.provider_ready(cfg, "openrouter") is False


def test_local_without_an_address_is_not_ready(write_cfg):
    write_cfg({"coeos": {**BASE_SETTINGS, "models": REGISTRY}})
    assert providers.provider_ready(load_config(), "odyssai") is False


def test_the_address_belongs_to_the_client(write_cfg):
    """Aucune IP en dur : l'adresse vient de la config du client."""
    assert providers.PROVIDERS["odyssai"]["api_base"] == ""
    _local(write_cfg)
    assert providers.api_base(load_config(), "odyssai") == LOCAL_BASE


# ── Le routage choisit le provider par entrée de registre ───────────────────

def test_routing_picks_the_provider_from_the_entry(write_cfg):
    _local(write_cfg)
    cfg = load_config()
    assert router.resolve_logical(cfg, "qwen-122b") == ("odyssai", "qwen3-5-122b-a10b")
    # Sans clé OpenRouter, le modèle cloud du même registre n'est pas servable —
    # et ça n'empêche PAS le local de l'être.
    assert router.resolve_logical(cfg, "glm") is None


def test_a_local_decider_is_resolvable(write_cfg):
    """Le décideur tourne à chaque requête : le garder local évite d'envoyer le
    prompt dehors juste pour le classer."""
    _local(write_cfg)
    c = router.coeos_cfg(load_config())
    assert router.resolve_decider(load_config(), c) == ("odyssai", "gemma-4-31b-it")


def test_an_all_local_setting_is_fully_servable(client, write_cfg):
    """Aucune clé cloud, et pourtant la box sert."""
    _local(write_cfg)
    h = client.get("/health").json()
    assert "odyssai" in h["providers_ready"]
    assert h["axes_servable"] >= 1


# ── Le drapeau de raisonnement porte le nom du provider ─────────────────────

def test_thinking_flag_is_named_per_provider():
    """OdyssAI-X n'écoute QUE `enable_thinking` — mesuré au fil sur le 397B le
    2026-08-18 : avec `thinking:false` le modèle brûle tout son budget en
    raisonnement et répond vide. Se tromper de nom est SILENCIEUX."""
    from coeos.proxy import _prepare_body

    body = {"model": "coeos", "messages": [], "enable_thinking": False}

    local = _prepare_body(body, "qwen3-5-397b-a17b-q6h16", "odyssai")
    assert local["enable_thinking"] is False
    assert "thinking" not in local          # le nom cloud ne doit pas partir

    cloud = _prepare_body(body, "z-ai/glm-5.2", "openrouter")
    assert cloud["thinking"] is False
    assert cloud["reasoning"] == {"enabled": False}
    assert "enable_thinking" not in cloud


def test_thinking_on_travels_too():
    from coeos.proxy import _prepare_body

    on = _prepare_body({"model": "coeos", "messages": [], "enable_thinking": True},
                       "qwen3-5-397b-a17b-q6h16", "odyssai")
    assert on["enable_thinking"] is True


def test_no_thinking_flag_means_nothing_is_injected():
    """On traduit l'intention du client, on n'invente jamais de défaut."""
    from coeos.proxy import _prepare_body

    out = _prepare_body({"model": "coeos", "messages": []}, "gemma-4-31b-it", "odyssai")
    assert "enable_thinking" not in out and "thinking" not in out
