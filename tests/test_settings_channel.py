"""S1.5 — canal de fraîcheur des settings servi par le SaaS."""

import pytest
from fastapi.testclient import TestClient

from coeos.app import app
from tests.conftest import BASE_SETTINGS


@pytest.fixture()
def client(cfg_file):
    with TestClient(app) as c:
        yield c


def test_current_public_and_stripped(client, write_cfg):
    write_cfg({"coeos": {**BASE_SETTINGS,
                         "models": {"glm-5.2": {"or": "z-ai/glm-5.2"}},
                         "decider_model": "local-gemma",
                         "regime": "cloud"},
               "providers": {"openrouter": {"api_key": "sk-or-SECRET"}}})
    r = client.get("/settings/current")   # public, aucune clé
    assert r.status_code == 200
    body = r.json()
    # l'essentiel est là
    assert body["name"] and body["updated"]
    assert body["axes"] and "model" in body["axes"][0]
    # Le setting porte sa DÉFINITION complète : les axes lient des noms
    # logiques, donc le registre doit voyager avec (renversé le 17/08/2026 —
    # sans lui, une box qui pull ne sait pas résoudre ce qu'elle reçoit).
    assert body["models"] == {"glm-5.2": {"or": "z-ai/glm-5.2"}}
    # le decider structure prime sur le legacy decider_model (decider_spec)
    assert body["decider"]["or"] == "anthropic/haiku"
    # l'operateur-specifique reste dehors
    assert "regime" not in body and "providers" not in body
    assert "mode" not in body and "axis_override" not in body
    # aucune trace de la clé provider
    assert "sk-or-SECRET" not in r.text


def test_etag_304(client, write_cfg):
    write_cfg({"coeos": BASE_SETTINGS})
    r = client.get("/settings/current")
    etag = r.headers["ETag"]
    assert etag and r.headers.get("Cache-Control")
    r2 = client.get("/settings/current", headers={"If-None-Match": etag})
    assert r2.status_code == 304
