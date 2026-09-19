"""S1.4 métrologie + S1.3 quotas."""

import pytest
from fastapi.testclient import TestClient

from coeos import accounts, metering
from coeos.app import app


@pytest.fixture()
def auth_db(tmp_path, monkeypatch, cfg_file):
    db = tmp_path / "auth.db"
    monkeypatch.setenv("COEOS_AUTH_DB", str(db))
    monkeypatch.delenv("COEOS_MASTER_KEY", raising=False)
    monkeypatch.delenv("COEOS_RPM_DEFAULT", raising=False)
    monkeypatch.delenv("COEOS_DAY_TOKENS_DEFAULT", raising=False)
    metering._RPM.clear()
    metering._DAY_CACHE.clear()
    return db


@pytest.fixture()
def client(auth_db):
    with TestClient(app) as c:
        yield c


def _mk(name, **kw):
    accounts.create_user(name, **kw)
    return accounts.issue_key(name)


def test_record_and_summary(auth_db):
    accounts.create_user("lea")
    acc = accounts.resolve(accounts.issue_key("lea"))
    metering.record(acc, {"x-coeos-axis": "debug"},
                    {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.002},
                    "deepseek/deepseek-v4-pro")
    metering.record(acc, {"axis": "_decider"},
                    {"prompt_tokens": 20, "completion_tokens": 5}, "gemma")
    s = metering.summary(acc["user_id"])
    assert s["totals"]["requests"] == 2
    assert s["totals"]["tokens_in"] == 120 and s["totals"]["tokens_out"] == 55
    assert s["totals"]["cost"] == pytest.approx(0.002)
    axes = {r["axis"] for r in s["by_axis"]}
    assert axes == {"debug", "_decider"}
    # usage vide -> pas de ligne
    metering.record(acc, {}, {}, "x")
    assert metering.summary(acc["user_id"])["totals"]["requests"] == 2


def test_sse_usage_extraction():
    prev = None
    prev = metering.last_usage_from_sse(b'data: {"choices":[{"delta":{}}]}\n\n', prev)
    assert prev is None
    chunk = b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\ndata: [DONE]\n\n'
    prev = metering.last_usage_from_sse(chunk, prev)
    assert prev == {"prompt_tokens": 7, "completion_tokens": 3}


def test_rpm_limit_429(client, auth_db):
    key = _mk("max")
    accounts.main(["set-limits", "max", "--rpm", "2"])
    h = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/models", headers=h).status_code == 200
    r = client.get("/v1/models", headers=h)
    assert r.status_code == 200
    assert r.headers.get("X-RateLimit-Limit") == "2"
    r = client.get("/v1/models", headers=h)
    assert r.status_code == 429
    assert r.headers.get("Retry-After")
    assert r.json()["error"]["error"] == "rate_limited"
    # /v1/me reste joignable meme limite
    assert client.get("/v1/me", headers=h).status_code == 200


def test_daily_budget_429(client, auth_db):
    key = _mk("nina")
    accounts.main(["set-limits", "nina", "--day-tokens", "100"])
    acc = accounts.resolve(key)
    metering.record(acc, {"axis": "debug"},
                    {"prompt_tokens": 80, "completion_tokens": 40}, "m")
    metering._DAY_CACHE.clear()
    r = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 429
    assert r.json()["error"]["error"] == "daily_budget_exhausted"


def test_admin_exempt(client, auth_db):
    key = _mk("boss", admin=True)
    accounts.main(["set-limits", "boss", "--rpm", "1"])
    h = {"Authorization": f"Bearer {key}"}
    for _ in range(3):
        assert client.get("/v1/models", headers=h).status_code == 200


def test_usage_endpoints(client, auth_db):
    key = _mk("olga")
    admin = _mk("root", admin=True)
    acc = accounts.resolve(key)
    metering.record(acc, {"x-coeos-axis": "python"},
                    {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.001}, "m1")
    r = client.get("/v1/usage", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.json()["totals"]["requests"] == 1
    r = client.get("/admin/usage", headers={"Authorization": f"Bearer {admin}"})
    assert r.status_code == 200 and r.json()["totals"]["requests"] == 1
    assert client.get("/admin/usage",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 403
