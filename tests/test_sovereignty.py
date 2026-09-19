"""Les invariants de souveraineté — ce qui rend l'argument vérifiable.

Principe produit : « code libre, mesure payante,
jamais d'interrupteur ». Un modèle économique qui repose sur la souveraineté
ne vaut que si elle se prouve. Ces tests échouent si quelqu'un ajoute un jour
une vérification de licence, un appel sortant obligatoire ou de la télémétrie.

Ils ne testent pas une fonctionnalité : ils testent une PROMESSE.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coeos import accounts, proxy, router
from coeos.app import app
from coeos.config import load_config
from tests.conftest import BASE_SETTINGS

SRC = Path(__file__).resolve().parent.parent / "src" / "coeos"
REGISTRY = {"glm": {"name": "GLM", "or": "z-ai/glm-5.2"},
            "autre": {"name": "Autre", "or": "acme/autre"}}


@pytest.fixture()
def client(cfg_file):
    with TestClient(app) as c:
        yield c


def _loaded(write_cfg):
    write_cfg({"coeos": {**BASE_SETTINGS, "models": REGISTRY},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})


# ── Invariant 1 : aucun interrupteur ────────────────────────────────────────

def test_no_licence_check_anywhere_in_the_code():
    """Aucune notion de licence, d'expiration ou d'activation ne doit exister.

    Si ce test tombe un jour, c'est que le modèle Adobe est revenu par la
    fenêtre — celui dont on prétend affranchir le client."""
    interdits = ("licence_key", "license_key", "licence_valid", "license_valid",
                 "activation_key", "expires_at", "subscription_valid",
                 "check_licence", "check_license", "seat_count", "seats_used")
    trouves = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for mot in interdits:
            if mot in text:
                trouves.append(f"{path.name}: {mot}")
    assert not trouves, f"vérification de licence détectée : {trouves}"


def test_the_box_routes_without_ever_asking_us(client, write_cfg):
    """Une box coupée de nous route quand même. C'est l'invariant, pas un
    comportement de repli."""
    _loaded(write_cfg)
    h = client.get("/health").json()
    assert h["ok"] is True and h["axes_servable"] > 0
    # Le routeur est publié et résolvable, sans qu'aucun de nos serveurs
    # n'ait été contacté (aucun réseau n'est monté dans ces tests).
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert "CoeOS" in ids
    assert router.resolve_logical(load_config(), "glm") == ("openrouter", "z-ai/glm-5.2")


# ── Invariant 2 : aucun appel sortant obligatoire ───────────────────────────

def test_only_the_settings_poll_and_the_providers_are_reachable_out():
    """Les seules URL en dur du code doivent être : les providers du client,
    et le flux de settings (désactivable). Toute autre destination serait une
    remontée non déclarée."""
    autorises = ("openrouter.ai", "api.coeos.io", "odyssai.eu")
    # Un schéma nu (`"http://"` dans un startswith de validation) n'est pas une
    # destination : on ne retient qu'une URL suivie d'un hôte.
    url = re.compile(r'https?://([A-Za-z0-9][A-Za-z0-9._-]*)')
    suspects = []
    for path in SRC.rglob("*.py"):
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for host in url.findall(line):
                if any(a in host for a in autorises):
                    continue
                suspects.append(f"{path.name}: {host}")
    assert not suspects, f"destination sortante non déclarée : {suspects}"


def test_the_settings_poll_can_be_switched_off():
    """Le poll doit être désactivable — sinon « aucun appel sortant
    obligatoire » est faux."""
    lifespan = (SRC / "app.py").read_text(encoding="utf-8")
    assert "COEOS_NO_POLL" in lifespan
    # Et il ne doit rien bloquer : c'est une tâche de fond, pas une étape de
    # démarrage.
    assert "asyncio.create_task(updates.periodic_loop())" in lifespan


def test_an_unreachable_master_never_breaks_a_request(client, write_cfg, monkeypatch):
    """Le master injoignable est un non-événement pour le service rendu."""
    _loaded(write_cfg)
    from coeos import updates

    class Boom:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise RuntimeError("réseau coupé")

    monkeypatch.setattr(updates.httpx, "AsyncClient", Boom)
    import asyncio

    state = asyncio.run(updates.check())
    assert state["error"]                       # l'échec est constaté…
    assert client.get("/health").json()["ok"]   # …et ne casse rien


# ── Invariant 3 : aucune télémétrie ─────────────────────────────────────────

def test_metering_stays_local(client, write_cfg):
    """La consommation est comptée POUR l'opérateur, jamais remontée."""
    _loaded(write_cfg)
    # Au niveau des IMPORTS : « requests » apparaît aussi comme mot anglais
    # dans les compteurs et les messages, un substring naïf crie au loup.
    text = (SRC / "metering.py").read_text(encoding="utf-8")
    imports = [l.strip() for l in text.splitlines()
               if l.startswith(("import ", "from "))]
    for ligne in imports:
        for client_http in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert client_http not in ligne, \
                f"metering.py importe de quoi sortir : {ligne}"


def test_published_settings_carry_no_client_data(client, write_cfg):
    """Ce qui sort de la box vers le monde ne contient rien du client."""
    _loaded(write_cfg)
    accounts.create_user("alice")
    body = client.get("/settings/current").text
    for interdit in ("alice", "sk-or-test", "ck_"):
        assert interdit not in body


# ── Invariant 4 : router sans nos tables ────────────────────────────────────

def test_the_operator_can_route_without_our_tables(client, write_cfg):
    """La preuve que la dépendance n'existe pas : l'opérateur réaffecte chaque
    axe à la main, vers le provider de son choix. Ce n'est pas un confort, c'est
    l'invariant 4 rendu exécutable."""
    _loaded(write_cfg)
    r = client.put("/admin/mapping/axis/code",
                   json={"model": "un-modele-a-moi", "provider": "odyssai"})
    assert r.status_code == 200
    axis = r.json()["axis"]
    assert axis["model"] == "un-modele-a-moi" and axis["provider"] == "odyssai"

    c = router.coeos_cfg(load_config())
    assert next(a for a in router.bound_axes(c) if a["key"] == "code")["model"] == "un-modele-a-moi"


def test_a_hand_written_setting_needs_nothing_from_us(client, write_cfg):
    """Un opérateur peut composer son propre setting, sans TMB ni master."""
    write_cfg({"coeos": {"name": "table maison", "enabled": True,
                         "default_axis": "tout",
                         "axes": [{"key": "tout", "label": "Tout",
                                   "model": "mistralai/mistral-large"}],
                         "models": {}},
               "providers": {"openrouter": {"api_key": "sk-or-test"}}})
    assert client.get("/health").json()["ok"] is True
    # Sans entrée de registre, la liaison EST l'id upstream : rien à importer.
    assert router.resolve_logical(load_config(), "mistralai/mistral-large") == \
        ("openrouter", "mistralai/mistral-large")
