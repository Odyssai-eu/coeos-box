"""Canal de settings — la box interroge **coeos-master** (le control-plane, chez
nous), compare la date `updated` publiée à celle qui tourne, et PROPOSE la mise
à jour. Jamais appliquée en silence : c'est l'opérateur qui décide.

C'est la valeur récurrente de l'abonnement : à chaque nouveau modèle, TMB
rejuge, le master republie un setting versionné, les box le récupèrent.

  COEOS_MASTER_URL       racine du master (défaut: https://api.coeos.io)
  COEOS_SETTINGS_URL     override direct de l'URL du setting (prioritaire)
  COEOS_MASTER_TOKEN     clé d'abonnement présentée au master si ses settings sont payants
  COEOS_SETTINGS_BASE    base raw d'un repo de settings (GitHub public) : index.json + <nom>.json
  COEOS_UPDATE_INTERVAL  secondes entre deux vérifications (défaut 86400 = 24 h)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx

from .config import config_txn, load_config

DEFAULT_MASTER = "https://api.coeos.io"
SETTINGS_PATH = "/settings/current"


def is_disabled() -> bool:
    """D17 / G-9 / #120 — la box est autonome. `updates.py` ne poll pas
    le master. Les settings TMB sont chargés par l'opérateur depuis
    GitHub (M3-12 / #97) via la console de la box. L'env
    `COEOS_UPDATES_DISABLED=1` est posée par le compose CoeOS
    (cf. le docker-compose du déploiement)."""
    return (os.environ.get("COEOS_UPDATES_DISABLED") or "0").strip() in ("1", "true", "yes")
# Le master publie aussi son CATALOGUE (full/open/eco). Une box qui le récupère
# peut assigner un setting par user sans rien importer à la main.
CATALOG_PATH = "/settings/catalog"

STATE: dict = {"checked_at": None, "available": False, "local_updated": None,
               "remote_updated": None, "remote_name": None, "error": None,
               "source": None}

# ETag du dernier setting récupéré : le master sert /settings/current avec un
# ETag, donc une box qui n'a rien à apprendre coûte un 304 et pas un transfert.
_ETAG: dict = {"tag": None}


def master_url() -> str:
    return (os.environ.get("COEOS_MASTER_URL") or DEFAULT_MASTER).strip().rstrip("/")


def settings_url() -> str:
    direct = (os.environ.get("COEOS_SETTINGS_URL") or "").strip()
    if direct:
        return direct
    base = settings_base()
    if base:
        return base + "/index.json"
    return master_url() + SETTINGS_PATH


def settings_base() -> str:
    """Base raw d'un repo de settings (GitHub public : source des settings).
    Layout: index.json {settings:[...], default} + <nom>.json par setting.
    Vide -> ancien mode master (fallback)."""
    return (os.environ.get("COEOS_SETTINGS_BASE") or "").strip().rstrip("/")


async def _get_json(url: str, headers: dict | None = None):
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        r = await client.get(url, headers=headers or {})
        return r.json() if r.status_code == 200 else None


def interval_s() -> int:
    try:
        return max(300, int(os.environ.get("COEOS_UPDATE_INTERVAL", "86400")))
    except ValueError:
        return 86400


async def _fetch_remote(use_etag: bool = False) -> dict | None:
    """Le setting publié par le master. `None` = injoignable ou inchangé (304)."""
    headers = {}
    if use_etag and _ETAG["tag"]:
        headers["If-None-Match"] = _ETAG["tag"]
    # La clé d'abonnement : si le master gate ses settings (paywall), la box la
    # présente ici. Absente = pull anonyme (master public), comportement actuel.
    tok = (os.environ.get("COEOS_MASTER_TOKEN") or "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    base = settings_base()
    if base:
        idx = await _get_json(base + "/index.json")
        default = (idx or {}).get("default") or "eco"
        return await _get_json(f"{base}/{default}.json")
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        r = await client.get(settings_url(), headers=headers)
        if r.status_code == 304:
            return None
        if r.status_code == 200:
            tag = r.headers.get("ETag")
            if tag:
                _ETAG["tag"] = tag
            return r.json()
        return None


async def check() -> dict:
    """Compare le `updated` (date ISO) distant au local. Une mise à jour est
    'available' quand la date distante est strictement plus récente. Lecture
    seule."""
    local = (load_config().get("coeos") or {}).get("updated") or ""
    try:
        remote = await _fetch_remote()
        if remote is None:
            STATE.update(error="settings unreachable", checked_at=_now(),
                         source=settings_url())
            return dict(STATE)
        r_upd = str(remote.get("updated") or "")
        STATE.update(checked_at=_now(), local_updated=local, remote_updated=r_upd,
                     remote_name=remote.get("name"), source=settings_url(),
                     available=bool(r_upd and r_upd > local), error=None)
    except Exception as e:
        STATE.update(error=str(e)[:200], checked_at=_now(), source=settings_url())
    return dict(STATE)


async def apply() -> dict:
    """Récupère le setting publié et remplace la config coeos. Les clés
    provider (cfg['providers']) ne sont pas touchées."""
    remote = await _fetch_remote()
    if remote is None:
        raise RuntimeError("settings du master injoignables")
    with config_txn() as cfg:
        cfg["coeos"] = remote
    STATE.update(available=False, local_updated=str(remote.get("updated") or ""))
    sys.stderr.write(f"[coeos] settings mis à jour depuis {settings_url()} "
                     f"→ {remote.get('updated')}\n")
    return {"ok": True, "updated": remote.get("updated"), "name": remote.get("name")}


async def catalog_url() -> str:
    return master_url() + CATALOG_PATH


async def pull_catalog() -> dict:
    """Récupère le catalogue publié par le master et le pose dans la config.

    Les settings publiés remplacent ceux du même nom ; ceux que le master ne
    publie plus ne sont PAS supprimés — une box dont des users sont assignés à
    un setting retiré continue de tourner. On rapporte ce qui a bougé plutôt
    que de nettoyer dans le dos de l'opérateur."""
    base = settings_base()
    if base:
        idx = await _get_json(base + "/index.json")
        names = (idx or {}).get("settings") if isinstance(idx, dict) else None
        if not names:
            raise RuntimeError(f"index.json introuvable ou vide sur {base}")
        kept = {}
        for n in names:
            one = await _get_json(f"{base}/{n}.json")
            if isinstance(one, dict):
                kept[n] = one
        if not kept:
            raise RuntimeError("aucun setting importable depuis le repo")
    else:
        hdrs = {}
        tok = (os.environ.get("COEOS_MASTER_TOKEN") or "").strip()
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(await catalog_url(), headers=hdrs)
        if r.status_code != 200:
            raise RuntimeError(f"catalogue injoignable (HTTP {r.status_code})")
        payload = r.json()
        published = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(published, dict) or not published:
            raise RuntimeError("le master ne publie aucun setting")
        kept = {k: v for k, v in published.items() if isinstance(v, dict)}
    with config_txn() as cfg:
        current = cfg.get("settings") if isinstance(cfg.get("settings"), dict) else {}
        merged = {**current, **kept}
        cfg["settings"] = merged
    sys.stderr.write(f"[coeos] catalogue récupéré : {', '.join(sorted(kept))}\n")
    return {"ok": True, "pulled": sorted(kept),
            "kept_locally": sorted(set(current) - set(kept))}


async def periodic_loop() -> None:
    """Tâche de fond : vérifie maintenant, puis à chaque intervalle. Les échecs
    tombent dans STATE['error'] — le poll ne fait jamais tomber l'app.

    D17 / G-9 / #120 — la box est AUTONOME : `updates.py` ne poll pas
    `coeos-master`. Tant que `COEOS_UPDATES_DISABLED=1`, la boucle
    est un no-op. Les settings TMB sont chargés par l'opérateur
    depuis GitHub (M3-12 / #97) via la console de la box.
    """
    if is_disabled():
        STATE.update(disabled=True, checked_at=_now(), note="updates.inert (COEOS_UPDATES_DISABLED)")
        return
    while True:
        try:
            await check()
        except Exception as e:
            STATE.update(error=str(e)[:200], checked_at=_now())
        await asyncio.sleep(interval_s())


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")
