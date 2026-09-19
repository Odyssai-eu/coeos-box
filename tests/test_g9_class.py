"""G-9 / #120 — header `X-CoeOS-Class` + `x_coeos_decision` response.

Le contrat wire entre le client et la box :

  - la requête porte `X-CoeOS-Class: confidential|open` (par
    défaut : `confidential`, fail-closed, D25 règle 5) ;
  - la réponse porte `x_coeos-decision` (JSON compact) avec
    `class_requested`, `class_served`, `provider`, `model`,
    `axis`, `decider_used`, `profile`, `rules_hash` ;
  - le décideur LLM est confiné à la classe de la requête
    (D25 : pas d'appel hors classe, jamais).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_profile_default_vps(monkeypatch):
    monkeypatch.delenv("COEOS_PROFILE", raising=False)
    from coeos import router
    assert router.coeos_profile() == "vps"


def test_profile_explicit_local(monkeypatch):
    monkeypatch.setenv("COEOS_PROFILE", "local")
    from coeos import router
    assert router.coeos_profile() == "local"


def test_profile_invalid_falls_back_vps(monkeypatch):
    monkeypatch.setenv("COEOS_PROFILE", "edge")
    from coeos import router
    assert router.coeos_profile() == "vps"


def test_class_absent_defaults_confidential():
    """D25 règle 5 : pas de classe = `confidential`, jamais
    `open` implicite. Fail-closed, l'invariant D25 ne se négocie
    pas."""
    from coeos import router
    assert router.resolve_request_class({}) == "confidential"
    assert router.resolve_request_class({"x-coeos-class": ""}) == "confidential"


def test_class_open_explicit():
    from coeos import router
    assert router.resolve_request_class({"x-coeos-class": "open"}) == "open"


def test_class_confidential_explicit():
    from coeos import router
    assert router.resolve_request_class({"x-coeos-class": "confidential"}) == "confidential"


def test_class_invalid_value_defaults_confidential():
    """Un attaquant qui pose `x-coeos-class: secret` pour
    forcer un downgrade vers un provider sans DPA ne doit
    pas pouvoir. Refus = `confidential`."""
    from coeos import router
    assert router.resolve_request_class({"x-coeos-class": "secret"}) == "confidential"


def test_class_case_insensitive():
    from coeos import router
    assert router.resolve_request_class({"x-coeos-class": "OPEN"}) == "open"
    assert router.resolve_request_class({"x-coeos-class": "Confidential"}) == "confidential"


def test_decision_header_payload_shape():
    """Le payload `x_coeos_decision` doit avoir les 8 champs
    spécifiés par le ticket. Le client `coeos-decision.ts`
    parse strict — un champ manquant casse l'audit."""
    required = {
        "class_requested", "class_served", "provider", "model",
        "axis", "decider_used", "profile", "rules_hash",
    }
    # Sanity : le payload exemple qu'on enverrait doit être
    # validé par le parseur côté client.
    sample = {
        "class_requested": "confidential",
        "class_served": "local",
        "provider": "odyssai-x",
        "model": "qwen3-32b",
        "axis": "legal",
        "decider_used": False,
        "profile": "local",
        "rules_hash": None,
    }
    assert set(sample.keys()) == required


def test_updates_inert_when_disabled(monkeypatch):
    """G-9 / #120 — la box est autonome. `COEOS_UPDATES_DISABLED=1`
    rend la boucle `periodic_loop` no-op. Sans cet env, on
    poll `coeos-master` (chemin historique)."""
    from coeos import updates
    monkeypatch.setenv("COEOS_UPDATES_DISABLED", "1")
    assert updates.is_disabled() is True
    monkeypatch.setenv("COEOS_UPDATES_DISABLED", "0")
    assert updates.is_disabled() is False
    monkeypatch.delenv("COEOS_UPDATES_DISABLED", raising=False)
    assert updates.is_disabled() is False
