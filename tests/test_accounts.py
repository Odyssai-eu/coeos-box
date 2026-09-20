"""S1.1 — comptes, clés, amorçage ouvert→exigé, gate admin."""

import pytest
from fastapi.testclient import TestClient

from coeos import accounts
from coeos.app import app


@pytest.fixture()
def auth_db(tmp_path, monkeypatch, cfg_file):
    db = tmp_path / "auth.db"
    monkeypatch.setenv("COEOS_AUTH_DB", str(db))
    return db


@pytest.fixture()
def client(auth_db):
    with TestClient(app) as c:
        yield c


def test_bootstrap_open_then_enforced(client, auth_db):
    # Aucune cle -> instance ouverte (mode dev)
    assert not accounts.enforced()
    assert client.get("/v1/models").status_code == 200
    # Premiere cle emise -> auth exigee partout sur /v1/*
    accounts.create_user("alice")
    key = accounts.issue_key("alice")
    assert accounts.enforced()
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 200
    # /health reste public
    assert client.get("/health").status_code == 200


def test_resolve_and_revoke(auth_db):
    accounts.create_user("bob", mode="platform")
    key = accounts.issue_key("bob", "laptop")
    acc = accounts.resolve(key)
    assert acc["name"] == "bob" and acc["mode"] == "platform" and not acc["admin"]
    # seule l'empreinte est stockee, jamais la cle
    import sqlite3
    rows = sqlite3.connect(auth_db).execute("SELECT hash, prefix FROM keys").fetchall()
    assert all(key not in h for h, _ in rows) and rows[0][1] == key[:accounts.PREFIX_LEN]
    assert accounts.revoke_key(key[:accounts.PREFIX_LEN]) == 1
    assert accounts.resolve(key) is None


def test_admin_gate(client, auth_db):
    accounts.create_user("carol")
    user_key = accounts.issue_key("carol")
    accounts.create_user("root", admin=True)
    admin_key = accounts.issue_key("root")
    # un compte simple ne passe pas /admin/*
    r = client.get("/admin/coeos", headers={"Authorization": f"Bearer {user_key}"})
    assert r.status_code == 403
    r = client.get("/admin/coeos", headers={"Authorization": f"Bearer {admin_key}"})
    assert r.status_code == 200


def test_legacy_env_key_still_admin(client, auth_db, monkeypatch):
    monkeypatch.setenv("COEOS_API_KEY", "legacy-secret")
    assert client.get("/v1/models").status_code == 401
    assert client.get("/admin/coeos",
                      headers={"Authorization": "Bearer legacy-secret"}).status_code == 200


def test_wrong_key_shapes(client, auth_db):
    accounts.create_user("dave")
    accounts.issue_key("dave")
    for bad in ("", "ck_deadbeef", "Bearer", "nonsense"):
        r = client.get("/v1/models", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401


# ── Onglet API Keys du dashboard (2026-08-20) ────────────────────────────────

def test_issued_key_is_recoverable_for_the_operator(auth_db):
    """Choix produit : l'admin de sa box peut recopier une clé qu'il a émise.
    Le hash authentifie, le chiffré Fernet permet la relecture."""
    from coeos import accounts as a
    k = a.issue_key_for("default", "laptop")
    keys = a.list_keys()
    assert len(keys) == 1
    assert keys[0]["name"] == "laptop"
    assert keys[0]["secret"] == k          # recopiable, à l'identique
    assert a.resolve(k) is not None        # et elle authentifie


def test_a_legacy_hash_only_key_is_not_recoverable(auth_db):
    """Une clé d'avant le stockage chiffré marche encore mais n'est pas recopiable."""
    from coeos import accounts as a
    a.ensure_user("default")
    # simule l'ancien schéma : insertion sans secret_enc
    with a._connect() as c:
        uid = c.execute("SELECT id FROM users WHERE name='default'").fetchone()["id"]
        raw = "ck_" + "0" * 40
        c.execute("INSERT INTO keys(user_id, prefix, hash, name, created) VALUES(?,?,?,?,?)",
                  (uid, raw[:a.PREFIX_LEN], a._hash(raw), "vieille", a._now()))
    assert a.list_keys()[0]["secret"] is None


def test_toggle_overrides_auto_enforcement(auth_db):
    """Le toggle prime : OFF ouvre /v1 même avec des clés, ON le ferme."""
    from coeos import accounts as a
    a.issue_key_for("default", "x")
    assert a.client_enforced() is True       # auto : une clé existe
    a.set_auth_enabled(False)
    assert a.client_enforced() is False      # toggle OFF ouvre
    a.set_auth_enabled(True)
    assert a.client_enforced() is True


def test_toggle_is_the_master_switch(auth_db):
    """Le toggle gouverne toute la box — un seul gate (enforced == client_enforced)."""
    from coeos import accounts as a
    a.issue_key_for("default", "x")
    a.set_auth_enabled(False)
    assert a.enforced() is False             # OFF ouvre TOUT (v1 + admin + dashboard)
    assert a.client_enforced() == a.enforced()
    a.set_auth_enabled(True)
    assert a.enforced() is True


# ── Deux types de clés + suspension + paywall settings (2026-08-20) ───────────

def test_two_key_kinds_are_isolated(auth_db):
    """Une clé d'inférence n'ouvre pas les settings, et réciproquement."""
    from coeos import accounts as a
    ki = a.issue_key_for("default", "inf", "inference")
    ks = a.issue_key_for("default", "sub", "settings")
    assert a.resolve(ki)["key_kind"] == "inference"
    assert a.resolve(ks)["key_kind"] == "settings"
    kinds = {k["name"]: k["kind"] for k in a.list_keys()}
    assert kinds == {"inf": "inference", "sub": "settings"}
    assert [k["name"] for k in a.list_keys(kind="settings")] == ["sub"]


def test_suspend_then_reactivate(auth_db):
    """Suspendre une clé la refuse sans la supprimer ; réactiver la rend."""
    from coeos import accounts as a
    k = a.issue_key_for("default", "acme", "settings")
    assert a.resolve(k) is not None                 # active
    assert a.set_key_active(k[:a.PREFIX_LEN], False) == 1
    assert a.resolve(k) is None                      # suspendue -> refusée
    assert a.list_keys()[0]["active"] is False
    a.set_key_active(k[:a.PREFIX_LEN], True)
    assert a.resolve(k) is not None                  # réactivée -> rend


def test_settings_paywall_toggle_defaults_off(auth_db):
    """Le paywall settings est OFF par défaut — aucun pull existant ne casse."""
    from coeos import accounts as a
    assert a.settings_auth_enabled() is False
    a.set_settings_auth(True)
    assert a.settings_auth_enabled() is True
    a.set_settings_auth(False)
    assert a.settings_auth_enabled() is False


def test_settings_gate_end_to_end(client, auth_db):
    """La route /settings/catalog : publique OFF, exige une clé settings ON."""
    from coeos import accounts as a
    # OFF (défaut) : public
    assert client.get("/settings/catalog").status_code == 200
    # ON : sans clé -> 401
    a.set_settings_auth(True)
    assert client.get("/settings/catalog").status_code == 401
    # une clé INFERENCE ne suffit pas -> 403
    ki = a.issue_key_for("default", "inf", "inference")
    assert client.get("/settings/catalog", headers={"x-api-key": ki}).status_code == 403
    # une clé SETTINGS passe
    ks = a.issue_key_for("default", "sub", "settings")
    assert client.get("/settings/catalog", headers={"x-api-key": ks}).status_code == 200
    # suspendue -> refusée
    a.set_key_active(ks[:a.PREFIX_LEN], False)
    assert client.get("/settings/catalog", headers={"x-api-key": ks}).status_code == 401
