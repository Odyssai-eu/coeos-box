"""S1.2/S1.6 — coffre chiffré, isolation byok/platform, backoffice, dashboard."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from coeos import accounts, providers
from coeos.app import app
from tests.conftest import BASE_SETTINGS


@pytest.fixture()
def auth_db(tmp_path, monkeypatch, cfg_file):
    db = tmp_path / "auth.db"
    monkeypatch.setenv("COEOS_AUTH_DB", str(db))
    monkeypatch.delenv("COEOS_MASTER_KEY", raising=False)
    return db


@pytest.fixture()
def client(auth_db):
    with TestClient(app) as c:
        yield c


def _mk(name, **kw):
    accounts.create_user(name, **kw)
    return accounts.issue_key(name)


def test_vault_encrypts_at_rest(auth_db, tmp_path):
    accounts.create_user("eve")
    accounts.set_provider_key("user", 1, "openrouter", "sk-or-SECRET-123")
    raw = sqlite3.connect(auth_db).execute("SELECT key_enc FROM provider_keys").fetchone()[0]
    assert b"sk-or-SECRET-123" not in raw
    assert accounts.get_provider_key("user", 1, "openrouter") == "sk-or-SECRET-123"
    # la cle maitre vit hors base, en 0600
    mk = auth_db.parent / "coeos-master.key"
    assert mk.exists() and (mk.stat().st_mode & 0o777) == 0o600


def test_byok_isolation_no_operator_fallback(auth_db, write_cfg, monkeypatch):
    """Un compte byok SANS cle ne roule JAMAIS sur la cle globale/env."""
    write_cfg({"settings": BASE_SETTINGS,
               "providers": {"openrouter": {"api_key": "sk-or-OPERATOR"}}})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ENV")
    from coeos import config as config_mod
    cfg = config_mod.load_config()
    # sans compte : comportement historique (cle operateur)
    assert providers.provider_key(cfg) == "sk-or-OPERATOR"
    # compte byok sans cle -> None, pas la cle operateur
    key = _mk("frank")
    acc = accounts.resolve(key)
    tok = accounts.current_account.set(acc)
    try:
        assert providers.provider_key(cfg) is None
        # il pose SA cle -> c'est elle qui sert
        accounts.set_provider_key("user", acc["user_id"], "openrouter", "sk-or-FRANK")
        assert providers.provider_key(cfg) == "sk-or-FRANK"
    finally:
        accounts.current_account.reset(tok)


def test_admin_falls_back_to_operator_key(auth_db, write_cfg):
    """L'admin (= operateur) sans cle a lui utilise la cle maison de
    l'instance ; un byok non-admin ne le fait jamais (test au-dessus)."""
    write_cfg({"settings": BASE_SETTINGS,
               "providers": {"openrouter": {"api_key": "sk-or-HOUSE"}}})
    from coeos import config as config_mod
    cfg = config_mod.load_config()
    key = _mk("admin1", admin=True)
    acc = accounts.resolve(key)
    tok = accounts.current_account.set(acc)
    try:
        assert providers.provider_key(cfg) == "sk-or-HOUSE"
        # mais sa propre cle prime si posee
        accounts.set_provider_key("user", acc["user_id"], "openrouter", "sk-or-MINE")
        assert providers.provider_key(cfg) == "sk-or-MINE"
    finally:
        accounts.current_account.reset(tok)


def test_platform_mode_uses_platform_key_only(auth_db, write_cfg):
    write_cfg({"settings": BASE_SETTINGS,
               "providers": {"openrouter": {"api_key": "sk-or-OPERATOR"}}})
    from coeos import config as config_mod
    cfg = config_mod.load_config()
    key = _mk("grace", mode="platform")
    acc = accounts.resolve(key)
    tok = accounts.current_account.set(acc)
    try:
        assert providers.provider_key(cfg) is None          # pas de cle plateforme
        accounts.set_provider_key("platform", 0, "openrouter", "sk-or-PLATFORM")
        assert providers.provider_key(cfg) == "sk-or-PLATFORM"
    finally:
        accounts.current_account.reset(tok)


def test_me_endpoints(client, auth_db):
    key = _mk("henri")
    h = {"Authorization": f"Bearer {key}"}
    r = client.get("/v1/me", headers=h)
    assert r.status_code == 200
    assert r.json()["providers"]["openrouter"]["api_key_set"] is False
    r = client.put("/v1/me/providers/openrouter", headers=h,
                   json={"api_key": "sk-or-HENRI"})
    assert r.status_code == 200
    assert client.get("/v1/me", headers=h).json()["providers"]["openrouter"]["api_key_set"]
    assert client.delete("/v1/me/providers/openrouter", headers=h).json()["removed"] == 1


def test_platform_account_cannot_self_configure(client, auth_db):
    key = _mk("iris", mode="platform")
    r = client.put("/v1/me/providers/openrouter",
                   headers={"Authorization": f"Bearer {key}"},
                   json={"api_key": "x"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "platform_account"


def test_backoffice_platform_keys(client, auth_db):
    user_key = _mk("jack")
    admin_key = _mk("root", admin=True)
    ha = {"Authorization": f"Bearer {admin_key}"}
    assert client.get("/admin/platform/providers",
                      headers={"Authorization": f"Bearer {user_key}"}).status_code == 403
    r = client.put("/admin/platform/providers/openrouter", headers=ha,
                   json={"api_key": "sk-or-PLAT"})
    assert r.status_code == 200
    assert client.get("/admin/platform/providers", headers=ha) \
        .json()["openrouter"]["api_key_set"] is True


def test_dashboard_gated_with_login_flow(client, auth_db):
    admin_key = _mk("root2", admin=True)
    user_key = _mk("kim")
    # anonyme -> 401 avec formulaire, pas la page
    r = client.get("/dashboard")
    assert r.status_code == 401 and "admin key" in r.text
    # login mauvaise cle -> reste sur le formulaire
    assert client.post("/dashboard/login", data={"key": "ck_nope"}).status_code == 401
    # login cle non-admin -> refuse
    assert client.post("/dashboard/login", data={"key": user_key}).status_code == 401
    # login admin -> cookie -> dashboard 200
    r = client.post("/dashboard/login", data={"key": admin_key}, follow_redirects=True)
    assert r.status_code == 200 and "CoeOS" in r.text
    # le logo reste public (page de login l'affiche sans cookie)
    assert TestClient(app).get("/dashboard/images/coeos.png").status_code == 200
