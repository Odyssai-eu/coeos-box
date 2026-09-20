"""La table de routage : les axes du setting, éditables.

Un setting qui ne s'édite pas n'existe pas pour un client — il ne peut pas
définir ses axes. Il n'y a donc plus de mode standard/expert ni de couche
d'override : le setting EST la vérité, et l'opérateur l'écrit.
"""

import pytest
from fastapi.testclient import TestClient

from coeos import router
from coeos.app import app
from coeos.config import load_config
from tests.conftest import BASE_SETTINGS

REGISTRY = {
    "glm": {"name": "GLM 5.2", "or": "z-ai/glm-5.2"},
    "mm": {"name": "MiniMax M3", "or": "minimax/m3"},
    "kimi": {"name": "Kimi K3", "or": "moonshotai/kimi-k3"},
}

SCORE_TABLE = {
    "format": "tmb-score-table/1",
    "updated": "2026-08-01",
    "source": "TMB",
    "axes": {"code": {"label": "Code"}, "plan": {"label": "Plan"}},
    "models": {
        "GLM 5.2": {"or_id": "z-ai/glm-5.2", "cost_per_test": 0.10,
                    "tps_median": 60.0, "kind": "cloud",
                    "axes": {"code": {"score": 90.0, "n": 3, "verified": True}}},
        "Kimi K3": {"or_id": "moonshotai/kimi-k3", "cost_per_test": 0.30,
                    "tps_median": 31.0, "kind": "cloud",
                    "axes": {"code": {"score": 95.0, "n": 2, "verified": True},
                             "plan": {"score": 80.0, "n": 1, "verified": False}}},
        # Un étalon de bench : jamais candidat, même avec le meilleur score.
        "Fusion-REF": {"role": "reference", "or_id": "minimax/m3",
                       "axes": {"code": {"score": 99.0, "n": 1}}},
    },
}


@pytest.fixture()
def client(cfg_file):
    with TestClient(app) as c:
        yield c


def _write(write_cfg, **extra):
    write_cfg({"coeos": {**BASE_SETTINGS, "models": REGISTRY, **extra},
               "providers": {"odyssai": {"api_base": "http://box.lan:8000/v1"}}})


def test_mapping_lists_every_axis_with_its_binding(client, write_cfg):
    _write(write_cfg)
    body = client.get("/admin/mapping").json()
    keys = [a["key"] for a in body["axes"]]
    assert keys == ["code", "plan", "swift"]
    code = body["axes"][0]
    assert code["model"] == "glm" and code["model_name"] == "GLM 5.2"
    assert code["bound"] is True and code["provider"] == "openrouter"
    assert body["axes"][2]["bound"] is False       # swift, trou assumé du setting


def test_candidates_ranked_and_reference_rows_excluded(client, write_cfg):
    _write(write_cfg, score_table=SCORE_TABLE)
    body = client.get("/admin/mapping").json()
    assert body["has_score_table"] is True
    code = next(a for a in body["axes"] if a["key"] == "code")
    assert [c["logical"] for c in code["candidates"]] == ["kimi", "glm"]
    assert code["candidates"][0]["score"] == 95.0
    # "mm" n'a de ligne que via l'étalon Fusion-REF → jamais proposé.
    assert "mm" not in [c["logical"] for c in code["candidates"]]


def test_editing_an_axis_is_the_normal_path(client, write_cfg):
    """Plus de gate, plus de mode : le modèle choisi devient sa propre entrée
    de registre — l'opérateur n'a pas à inventer un nom logique."""
    _write(write_cfg)
    r = client.put("/admin/mapping/axis/code",
                   json={"model": "qwen3-5-122b-a10b", "provider": "odyssai"})
    assert r.status_code == 200
    axis = r.json()["axis"]
    assert axis["model"] == "qwen3-5-122b-a10b" and axis["provider"] == "odyssai"

    c = router.coeos_cfg(load_config())
    assert next(a for a in router.bound_axes(c) if a["key"] == "code")["model"] == "qwen3-5-122b-a10b"
    entry = router.registry_of(c)["qwen3-5-122b-a10b"]
    assert router.upstream_of(entry) == "qwen3-5-122b-a10b"
    assert router.provider_of(entry) == "odyssai"


def test_the_router_serves_what_was_just_edited(client, write_cfg):
    """Le test qui compte : l'édition change ce qui part vraiment à l'upstream."""
    _write(write_cfg)
    client.put("/admin/mapping/axis/code",
               json={"model": "qwen3-5-122b-a10b", "provider": "odyssai"})
    assert router.resolve_logical(load_config(), "qwen3-5-122b-a10b") == \
        ("odyssai", "qwen3-5-122b-a10b")


def test_clearing_an_axis_leaves_it_unbound(client, write_cfg):
    _write(write_cfg)
    axis = client.put("/admin/mapping/axis/code", json={"model": ""}).json()["axis"]
    assert axis["model"] == "" and axis["bound"] is False


def test_editing_an_unknown_axis_is_refused(client, write_cfg):
    _write(write_cfg)
    r = client.put("/admin/mapping/axis/inexistant", json={"model": "kimi"})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "unknown_axis"


def test_editing_refuses_an_unknown_provider(client, write_cfg):
    """Un provider inconnu ne doit pas retomber sur OpenRouter en silence."""
    _write(write_cfg)
    r = client.put("/admin/mapping/axis/code", json={"model": "x", "provider": "fantome"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "unknown_provider"


def test_master_update_replaces_the_setting(client, write_cfg, monkeypatch):
    import asyncio

    from coeos import updates

    _write(write_cfg)
    published = {"name": "TMB full", "updated": "2027-01-01", "enabled": True,
                 "axes": [{"key": "code", "label": "Code", "model": "glm"}],
                 "models": REGISTRY}

    async def fake_fetch(use_etag: bool = False):
        return published

    monkeypatch.setattr(updates, "_fetch_remote", fake_fetch)
    assert asyncio.run(updates.apply())["updated"] == "2027-01-01"
    assert router.coeos_cfg(load_config())["updated"] == "2027-01-01"


def test_settings_source_is_master_not_github(monkeypatch):
    from coeos import updates

    monkeypatch.delenv("COEOS_SETTINGS_URL", raising=False)
    monkeypatch.delenv("COEOS_MASTER_URL", raising=False)
    assert updates.settings_url() == "https://api.coeos.io/settings/current"
    assert "github" not in updates.settings_url().lower()

    monkeypatch.setenv("COEOS_MASTER_URL", "http://coeos.lan:4600/")
    assert updates.settings_url() == "http://coeos.lan:4600/settings/current"


def test_the_suggested_model_survives_an_edit(client, write_cfg):
    """Changer d'avis ne doit pas obliger à retrouver le modèle conseillé."""
    _write(write_cfg)
    before = client.get("/admin/mapping").json()["axes"]
    assert next(a for a in before if a["key"] == "code")["suggested"] == ""

    client.put("/admin/mapping/axis/code", json={"model": "autre-modele", "provider": "odyssai"})
    after = next(a for a in client.get("/admin/mapping").json()["axes"] if a["key"] == "code")
    assert after["model"] == "autre-modele"
    assert after["suggested"] == "glm"          # ce que le setting recommandait

    # Une seconde édition ne doit PAS écraser la suggestion d'origine.
    client.put("/admin/mapping/axis/code", json={"model": "encore-un-autre"})
    again = next(a for a in client.get("/admin/mapping").json()["axes"] if a["key"] == "code")
    assert again["suggested"] == "glm"
