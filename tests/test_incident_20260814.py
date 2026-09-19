"""Les trois défauts de l'incident du 14-17/08/2026, verrouillés.

Le 14/08, un push de settings depuis la console a écrit le registre au format
OdyssAI-X (`endpoint`) alors que ce moteur lisait `or`. Résultat : 18 axes sur
18 en 503, pendant trois jours, avec un `/health` au vert. le client, lui,
tombait sur un troisième mur : les modèles à raisonnement obligatoire.
"""

import json

import pytest
from fastapi.testclient import TestClient

from coeos import proxy, router
from coeos.app import app
from coeos.config import load_config
from tests.conftest import BASE_SETTINGS

# Le registre TEL QUE la console l'écrit : `endpoint`, pas `or`.
CONSOLE_REGISTRY = {
    "glm": {"name": "GLM 5.2", "endpoint": "z-ai/glm-5.2"},
    "mm": {"name": "MiniMax M3", "endpoint": "minimax/m3"},
}


@pytest.fixture()
def client(cfg_file):
    with TestClient(app) as c:
        yield c


# ── #11 — le registre au format console doit servir ─────────────────────────

def test_console_registry_resolves(write_cfg):
    write_cfg({"coeos": {**BASE_SETTINGS, "models": CONSOLE_REGISTRY},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})
    from coeos.config import load_config

    cfg = load_config()
    assert router.resolve_logical(cfg, "glm") == ("openrouter", "z-ai/glm-5.2")
    assert router.resolve_logical(cfg, "mm") == ("openrouter", "minimax/m3")


def test_or_wins_over_endpoint_when_both_present():
    entry = {"or": "z-ai/glm-5.2", "endpoint": "vieux/id-perime"}
    assert router.upstream_of(entry) == "z-ai/glm-5.2"


def test_upstream_of_tolerates_garbage():
    assert router.upstream_of(None) == ""
    assert router.upstream_of({"or": "  "}) == ""
    assert router.upstream_of({"or": 42}) == ""          # pas une chaîne
    assert router.upstream_of({"endpoint": " x/y "}) == "x/y"


def test_score_table_join_via_endpoint():
    """Le résolveur joint le registre à la table par l'id upstream : avec un
    registre au format console, ce join tombait aussi."""
    table = {
        "format": "tmb-score-table/1",
        "axes": {"code": {"label": "Code"}},
        "models": {"GLM 5.2": {"or_id": "z-ai/glm-5.2",
                               "axes": {"code": {"score": 90.0}}}},
    }
    axes = router.resolve_score_table(table, CONSOLE_REGISTRY)
    assert axes[0]["model"] == "glm"


# ── #12 — /health ne doit plus mentir ───────────────────────────────────────

def test_health_flags_unservable_axes(client, write_cfg):
    """La panne du 14/08 : des liaisons déclarées vers des entrées sans id."""
    write_cfg({"coeos": {**BASE_SETTINGS,
                         "models": {"glm": {"name": "GLM"}, "mm": {"name": "MM"}}},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})
    h = client.get("/health").json()
    assert h["ok"] is False
    assert h["status"] == "degraded"
    assert h["axes_bound"] == 2 and h["axes_servable"] == 0
    # La LISTE, pour agir sans deviner.
    assert sorted(h["axes_unservable"]) == ["code", "plan"]


def test_health_ok_with_console_registry(client, write_cfg):
    write_cfg({"coeos": {**BASE_SETTINGS, "models": CONSOLE_REGISTRY},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})
    h = client.get("/health").json()
    assert h["ok"] is True and h["axes_servable"] == 2
    assert "axes_unservable" not in h


def test_health_fresh_box_is_setup_not_broken(client, write_cfg):
    """Une box neuve sans clé attend son installation — ce n'est pas une panne."""
    write_cfg({"coeos": {**BASE_SETTINGS, "models": CONSOLE_REGISTRY}})
    h = client.get("/health").json()
    assert h["ok"] is True
    assert h["status"] == "setup" and h["providers_ready"] == []


# ── #13 — raisonnement obligatoire : servir, pas échouer ────────────────────

REFUSAL = json.dumps({"error": {"message": "Reasoning is mandatory for this endpoint "
                                           "and cannot be disabled.", "code": 400}})


def test_detects_the_refusal():
    assert proxy._reasoning_refused(400, REFUSAL) is True
    # Un autre 400 ne doit pas déclencher de rejeu.
    assert proxy._reasoning_refused(400, '{"error":{"message":"bad request"}}') is False
    # Ni un 500 qui contiendrait le mot par hasard.
    assert proxy._reasoning_refused(500, REFUSAL) is False


def test_drop_reasoning_keeps_everything_else():
    fwd = {"model": "x", "messages": [], "reasoning": {"enabled": False},
           "thinking": False, "max_tokens": 10}
    out = proxy._drop_reasoning(fwd)
    assert "reasoning" not in out and "thinking" not in out
    assert out["model"] == "x" and out["max_tokens"] == 10


def test_no_think_is_translated_then_retryable():
    """`enable_thinking: false` devient `reasoning:{enabled:false}` — c'est CE
    paramètre injecté par nous qui autorise le rejeu."""
    body = {"model": "coeos", "messages": [], "enable_thinking": False}
    fwd = proxy._prepare_body(body, "qwen/qwen3.8-max", "openrouter")
    assert fwd["reasoning"] == {"enabled": False}
    assert "reasoning" not in body        # le rejeu est permis
    fwd2 = proxy._prepare_body({**body, "reasoning": {"enabled": False}},
                               "qwen/qwen3.8-max", "openrouter")
    assert fwd2["reasoning"] == {"enabled": False}   # contrat client, pas de rejeu


def test_retries_without_reasoning_and_serves(monkeypatch, write_cfg):
    """Le vrai test : un modèle qui refuse doit quand même répondre."""
    write_cfg({"coeos": {**BASE_SETTINGS, "models": CONSOLE_REGISTRY},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})
    from coeos.config import load_config

    seen: list[dict] = []

    class FakeResponse:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = payload if isinstance(payload, str) else json.dumps(payload)

        def json(self):
            return json.loads(self.text)

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, headers=None, json=None):
            seen.append(json)
            if "reasoning" in json:
                return FakeResponse(400, REFUSAL)
            return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)

    import asyncio

    body = {"model": "coeos", "messages": [{"role": "user", "content": "ok?"}],
            "enable_thinking": False}
    resp = asyncio.run(proxy.proxy_chat(load_config(), "openrouter",
                                        "qwen/qwen3.8-max", body))
    assert resp.status_code == 200
    assert len(seen) == 2                       # refus, puis rejeu
    assert "reasoning" in seen[0] and "reasoning" not in seen[1]
    assert resp.headers.get(proxy.THINKING_FORCED_HEADER) == "forced-on"


def test_explicit_client_reasoning_is_not_retried(monkeypatch, write_cfg):
    """Un `reasoning` posé par le client est un contrat : on ne le contourne pas."""
    write_cfg({"coeos": {**BASE_SETTINGS, "models": CONSOLE_REGISTRY},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})
    from coeos.config import load_config

    seen: list[dict] = []

    class FakeResponse:
        def __init__(self, status, text):
            self.status_code = status
            self.text = text

        def json(self):
            return json.loads(self.text)

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, headers=None, json=None):
            seen.append(json)
            return FakeResponse(400, REFUSAL)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)

    import asyncio

    body = {"model": "coeos", "messages": [], "reasoning": {"enabled": False}}
    resp = asyncio.run(proxy.proxy_chat(load_config(), "openrouter",
                                        "qwen/qwen3.8-max", body))
    assert resp.status_code == 400
    assert len(seen) == 1                       # aucun rejeu


def test_streaming_retries_too(monkeypatch, write_cfg):
    """Le client streame : si le rejeu ne marchait qu'en non-streaming, le
    correctif ne servirait à rien pour le client qui a levé le défaut."""
    write_cfg({"coeos": {**BASE_SETTINGS, "models": CONSOLE_REGISTRY},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})
    from coeos.config import load_config

    seen: list[dict] = []

    class FakeStream:
        def __init__(self, payload):
            self.status_code = 400 if "reasoning" in payload else 200

        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def aread(self):
            return REFUSAL.encode()

        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        def stream(self, method, url, headers=None, json=None):
            seen.append(json)
            return FakeStream(json)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)

    import asyncio

    async def drain():
        body = {"model": "coeos", "messages": [], "enable_thinking": False,
                "stream": True}
        resp = await proxy.proxy_chat(load_config(), "openrouter",
                                      "qwen/qwen3.8-max", body)
        return b"".join([chunk async for chunk in resp.body_iterator])

    out = asyncio.run(drain())
    assert len(seen) == 2                          # refus, puis rejeu
    assert "reasoning" in seen[0] and "reasoning" not in seen[1]
    assert b'"content":"ok"' in out                # le client est servi
    assert b"Reasoning is mandatory" not in out    # l'erreur ne fuit pas


def test_a_setting_without_enabled_still_routes(client, write_cfg):
    """Un master ancien publie sans `enabled` : le routeur tombait en
    « coeos_disabled » et plus rien ne passait, sans que rien ne l'annonce.
    ABSENT n'est pas FAUX."""
    write_cfg({"coeos": {"name": "publié par un vieux master", "default_axis": "code",
                         "axes": [{"key": "code", "label": "Code", "model": "glm"}],
                         "models": CONSOLE_REGISTRY},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})
    assert "enabled" not in router.coeos_cfg(load_config())
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert "CoeOS" in ids


def test_enabled_false_still_disables(client, write_cfg):
    write_cfg({"coeos": {"name": "coupé exprès", "enabled": False, "default_axis": "code",
                         "axes": [{"key": "code", "label": "Code", "model": "glm"}],
                         "models": CONSOLE_REGISTRY},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert "CoeOS" not in ids
