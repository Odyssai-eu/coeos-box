"""W1 — la résolution `token → clés + setting + quotas`.

Le vault, les comptes, les tokens `ck_` et les quotas existaient déjà ; c'est
le SETTING qui manquait. Un dev en `full`, un stagiaire en `eco`, sur la même
box et sans changer de clé.
"""

import pytest
from fastapi.testclient import TestClient

from coeos import accounts, router
from coeos.app import app
from coeos.config import load_config
from tests.conftest import BASE_SETTINGS

FULL = {
    "name": "TMB full", "enabled": True, "updated": "2026-08-01",
    "default_axis": "code",
    "axes": [{"key": "code", "label": "Code", "model": "frontier"}],
    "models": {"frontier": {"name": "Frontier", "or": "acme/frontier-max"}},
}
ECO = {
    "name": "TMB eco", "enabled": True, "updated": "2026-08-01",
    "default_axis": "code",
    "axes": [{"key": "code", "label": "Code", "model": "cheap"}],
    "models": {"cheap": {"name": "Cheap", "or": "acme/tiny"}},
}


@pytest.fixture()
def client(cfg_file):
    with TestClient(app) as c:
        yield c


def _cfg(write_cfg, **extra):
    write_cfg({"coeos": {**BASE_SETTINGS, "models": {"glm": {"or": "z-ai/glm-5.2"}}},
               "settings": {"full": FULL, "eco": ECO},
               "providers": {"openrouter": {"api_key": "sk-or-test"}},
               **extra})


def test_no_catalog_means_everyone_routes_on_the_active_setting(write_cfg):
    """Le cas solo ne paie rien pour la mécanique multi-user."""
    write_cfg({"coeos": {**BASE_SETTINGS, "models": {"glm": {"or": "z-ai/glm-5.2"}}}})
    cfg = load_config()
    assert router.coeos_cfg_for(cfg, None)["name"] == BASE_SETTINGS["name"]
    assert router.coeos_cfg_for(cfg, {"setting": "full"})["name"] == BASE_SETTINGS["name"]


def test_account_setting_wins_over_org_default(write_cfg):
    _cfg(write_cfg, setting="eco")
    cfg = load_config()
    assert router.coeos_cfg_for(cfg, None)["name"] == "TMB eco"                    # org
    assert router.coeos_cfg_for(cfg, {"setting": "full"})["name"] == "TMB full"    # user
    assert router.coeos_cfg_for(cfg, {"setting": ""})["name"] == "TMB eco"         # hérite


def test_unknown_setting_falls_back_without_lying(write_cfg):
    """Un nom inconnu ne route pas en douce sur autre chose de plausible : on
    retombe sur l'actif, et le nom rapporté n'est PAS celui qu'on a demandé."""
    _cfg(write_cfg)
    cfg = load_config()
    picked = router.coeos_cfg_for(cfg, {"setting": "fantome"})
    assert picked["name"] == BASE_SETTINGS["name"]
    assert router.setting_name_for(cfg, {"setting": "fantome"}) == BASE_SETTINGS["name"]


def test_routing_actually_differs_per_account(write_cfg):
    """Le test qui compte : deux comptes, deux modèles servis."""
    _cfg(write_cfg)
    cfg = load_config()

    token = accounts.current_account.set({"setting": "full"})
    try:
        assert router.resolve_logical(cfg, "frontier") == ("openrouter", "acme/frontier-max")
    finally:
        accounts.current_account.reset(token)

    token = accounts.current_account.set({"setting": "eco"})
    try:
        assert router.resolve_logical(cfg, "cheap") == ("openrouter", "acme/tiny")
        # Le modèle de `full` n'est pas au registre de `eco` : la liaison est
        # traitée comme un id upstream brut, pas résolue vers le frontier.
        assert router.resolve_logical(cfg, "frontier") == ("openrouter", "frontier")
    finally:
        accounts.current_account.reset(token)


def test_admin_lists_catalog_and_assignments(client, write_cfg):
    _cfg(write_cfg, setting="eco")
    d = client.get("/admin/settings").json()
    assert [c["name"] for c in d["catalog"]] == ["eco", "full"]
    assert d["org_default"] == "eco"
    assert next(c for c in d["catalog"] if c["name"] == "full")["axes"] == 1


def test_admin_rejects_unknown_setting(client, write_cfg):
    _cfg(write_cfg)
    r = client.put("/admin/settings/default", json={"setting": "fantome"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "unknown_setting"


def test_admin_sets_org_default(client, write_cfg):
    _cfg(write_cfg)
    assert client.put("/admin/settings/default", json={"setting": "full"}).json()["org_default"] == "full"
    assert router.coeos_cfg_for(load_config(), None)["name"] == "TMB full"
    # Vider le défaut est permis : on revient au setting actif.
    client.put("/admin/settings/default", json={"setting": ""})
    assert router.coeos_cfg_for(load_config(), None)["name"] == BASE_SETTINGS["name"]


def test_admin_assigns_setting_to_a_user(client, write_cfg):
    _cfg(write_cfg)
    accounts.create_user("alice")
    r = client.put("/admin/users/alice/setting", json={"setting": "full"})
    assert r.status_code == 200 and r.json()["setting"] == "full"

    key = accounts.issue_key("alice")
    account = accounts.resolve(key)
    assert account["setting"] == "full"
    assert router.coeos_cfg_for(load_config(), account)["name"] == "TMB full"


def test_admin_rejects_unknown_user(client, write_cfg):
    _cfg(write_cfg)
    r = client.put("/admin/users/personne/setting", json={"setting": "full"})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "unknown_user"


def test_me_reports_the_setting_really_applied(client, write_cfg):
    _cfg(write_cfg)
    accounts.create_user("bob")
    # AVANT d'émettre la clé : la première clé émise ferme l'amorçage, et les
    # appels /admin/* suivants exigeraient une clé admin.
    assert client.put("/admin/users/bob/setting", json={"setting": "eco"}).status_code == 200
    key = accounts.issue_key("bob")

    me = client.get("/v1/me", headers={"authorization": f"Bearer {key}"}).json()
    assert me["name"] == "bob"
    assert me["setting"] == "eco" and me["setting_assigned"] == "eco"
    assert me["axes_servable"] == 1


# ── W3 — publication et récupération du catalogue ───────────────────────────

def test_catalog_is_public_and_carries_definitions(client, write_cfg):
    _cfg(write_cfg)
    r = client.get("/settings/catalog")           # public, aucune clé
    assert r.status_code == 200
    published = r.json()["settings"]
    assert sorted(published) == ["eco", "full"]
    # Chaque setting publié porte sa définition : sans registre, une box qui
    # le récupère ne saurait pas résoudre les liaisons.
    assert published["full"]["models"] == FULL["models"]
    assert published["full"]["axes"][0]["model"] == "frontier"
    assert "sk-or-test" not in r.text             # jamais de clé


def test_catalog_etag_304(client, write_cfg):
    _cfg(write_cfg)
    r = client.get("/settings/catalog")
    etag = r.headers["ETag"]
    assert client.get("/settings/catalog",
                      headers={"If-None-Match": etag}).status_code == 304


def test_pull_merges_without_deleting_local(client, write_cfg, monkeypatch):
    """Un setting que le master ne publie plus n'est pas supprimé : des users
    peuvent y être assignés, on ne nettoie pas dans le dos de l'opérateur."""
    write_cfg({"coeos": {**BASE_SETTINGS, "models": {"glm": {"or": "z-ai/glm-5.2"}}},
               "settings": {"maison": {"name": "Setting maison", "axes": []}}})

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"settings": {"full": FULL, "eco": ECO}}

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None): return FakeResponse()

    from coeos import updates

    monkeypatch.setattr(updates.httpx, "AsyncClient", FakeClient)
    r = client.post("/admin/settings/pull")
    assert r.status_code == 200
    body = r.json()
    assert body["pulled"] == ["eco", "full"]
    assert body["kept_locally"] == ["maison"]

    cat = router.catalog_of(load_config())
    assert sorted(cat) == ["eco", "full", "maison"]


def test_pull_surfaces_an_unreachable_master(client, write_cfg, monkeypatch):
    _cfg(write_cfg)

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): raise RuntimeError("boom")

    from coeos import updates

    monkeypatch.setattr(updates.httpx, "AsyncClient", FakeClient)
    r = client.post("/admin/settings/pull")
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "catalog_unreachable"


def test_master_can_author_its_catalog(client, write_cfg):
    """Sans ça le catalogue ne se peuplait qu'en éditant le fichier à la main."""
    write_cfg({"coeos": {**BASE_SETTINGS, "models": {"glm": {"or": "z-ai/glm-5.2"}}}})
    r = client.put("/admin/settings/catalog/full", json=FULL)
    assert r.status_code == 200 and r.json() == {"ok": True, "name": "full",
                                                 "axes": 1, "models": 1}
    assert router.setting_names(load_config()) == ["full"]
    # Et il ressort par la surface publique, prêt à être récupéré par une box.
    published = client.get("/settings/catalog").json()["settings"]
    assert published["full"]["models"] == FULL["models"]


def test_catalog_name_is_validated(client, write_cfg):
    _cfg(write_cfg)
    assert client.put("/admin/settings/catalog/../etc", json=FULL).status_code in (400, 404)
    assert client.put("/admin/settings/catalog/eco", json={"pas": "d axes"}).status_code == 422


def test_removing_a_setting_names_the_orphaned_users(client, write_cfg):
    _cfg(write_cfg, setting="eco")
    accounts.create_user("carol")
    client.put("/admin/users/carol/setting", json={"setting": "eco"})

    r = client.delete("/admin/settings/catalog/eco")
    assert r.status_code == 200
    assert r.json()["orphaned_users"] == ["carol"]
    # Le défaut d'org pointait dessus : il est libéré, pas laissé pendant.
    assert client.get("/admin/settings").json()["org_default"] is None


def test_pulled_setting_still_serves_the_router(client, write_cfg, monkeypatch):
    """Un setting tiré du master doit rester ACTIF. Sans `enabled` dans la forme
    publiée, la box exposait les modèles mais plus le routeur `CoeOS` — trouvé
    en testant la boucle sur rpi-dev, pas en lisant le code."""
    write_cfg({"coeos": {**BASE_SETTINGS, "models": {"glm": {"or": "z-ai/glm-5.2"}}},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})
    client.put("/admin/settings/catalog/eco", json=ECO)
    published = client.get("/settings/catalog").json()["settings"]["eco"]
    assert published["enabled"] is True

    # Ce qu'une box stocke après un pull, c'est CETTE forme-là.
    accounts.create_user("dave")
    client.put("/admin/users/dave/setting", json={"setting": "eco"})

    def _write():
        from coeos.config import config_txn
        with config_txn() as cfg:
            cfg["settings"] = {"eco": published}
    _write()

    key = accounts.issue_key("dave")
    ids = [m["id"] for m in client.get("/v1/models",
                                       headers={"authorization": f"Bearer {key}"}).json()["data"]]
    assert "CoeOS" in ids and "cheap" in ids


# ── Source GitHub (COEOS_SETTINGS_BASE) — 2026-08-23, teardown OVH ────────────

def test_pull_catalog_from_github_base(client, monkeypatch):
    """La box importe depuis un repo raw (index.json + <nom>.json), pas le master."""
    from coeos import updates
    monkeypatch.setenv("COEOS_SETTINGS_BASE", "https://raw.example/tmb-settings/main")
    ECO = {"name": "eco", "updated": "2027-01-01", "axes": [], "models": {}}
    FULL = {"name": "full", "updated": "2027-01-01", "axes": [], "models": {}}
    async def fake_get(url, headers=None):
        if url.endswith("/index.json"): return {"settings": ["eco", "full"], "default": "eco"}
        if url.endswith("/eco.json"): return ECO
        if url.endswith("/full.json"): return FULL
        return None
    monkeypatch.setattr(updates, "_get_json", fake_get)
    r = client.post("/admin/settings/pull")
    assert r.status_code == 200
    assert r.json()["pulled"] == ["eco", "full"]


def test_settings_url_prefers_github_base(monkeypatch):
    from coeos import updates
    monkeypatch.delenv("COEOS_SETTINGS_URL", raising=False)
    monkeypatch.setenv("COEOS_SETTINGS_BASE", "https://raw.example/tmb-settings/main")
    assert updates.settings_url() == "https://raw.example/tmb-settings/main/index.json"
