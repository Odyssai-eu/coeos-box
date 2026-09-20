"""CoeOS box — FastAPI app: OpenAI-compatible surface + admin + dashboard.

Endpoint map (OpenRouter-only):
  POST /v1/chat/completions   the OpenAI surface (model:"coeos" | logical | or:<id>)
  POST /v1/messages           the Anthropic surface (Claude Code)
  GET  /v1/models             CoeOS + the registry's logical models
  GET/PUT /admin/coeos        read / import the TMB Settings OR a TMB Score
                              Table (routing table, underground) — a score
                              table is auto-detected (format marker) and
                              resolved once, per-axis, against the current
                              registry
  GET  /admin/army            the model roster (display names only)
  GET  /admin/settings-update  update status (?check=true to poll GitHub now)
  POST /admin/settings-update/apply  download + apply the latest settings
  GET  /admin/coeos/decisions routing decision counters
  GET/POST/DELETE /admin/coeos/configs  named config snapshots (save/load/delete)
  GET  /admin/providers       redacted OpenRouter view (never returns the key)
  PUT  /admin/providers/openrouter  set/clear the key
  GET  /dashboard             single-file web UI
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from pydantic import BaseModel

from . import __version__, accounts, anthropic_api, metering, proxy, router, updates
from .config import (config_txn, delete_named_config, list_saved_configs,
                     load_config, load_named_config, save_named_config)
from .providers import (BUILTIN_PROVIDERS, PREFIX_TO_PROVIDER, PROVIDER_ID, PROVIDERS,
                        list_upstream_models, provider_key, provider_ready,
                        providers_of, ready_providers,
                        redact_provider)
from .router import (COEOS_DISPLAY_ID, COEOS_MODEL_ID, bound_axes, coeos_cfg,
                     coeos_resolve, decider_spec, decisions, registry_of,
                     resolve_logical, resolve_score_table, unservable_axes)

_BUNDLED_SETTINGS = "TMB-Settings-SE.json"


def _bundled_settings_text() -> str | None:
    try:
        return (importlib.resources.files("coeos") / "settings" /
                _BUNDLED_SETTINGS).read_text()
    except Exception:
        return None


def _auto_import_settings() -> None:
    """First boot on an empty config: load the bundled TMB Settings so
    `docker compose up` + an env key = a working router immediately."""
    cfg = load_config()
    if coeos_cfg(cfg).get("axes"):
        return
    text = _bundled_settings_text()
    if not text:
        return
    try:
        settings = json.loads(text)
    except Exception as e:
        sys.stderr.write(f"[coeos-se] bundled settings unreadable: {e}\n")
        return
    with config_txn() as c:
        c["coeos"] = settings
    sys.stderr.write(f"[coeos-se] imported bundled settings: {settings.get('name')}\n")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _auto_import_settings()
    # Poll GitHub for a newer TMB Settings (offer, never auto-apply).
    # COEOS_NO_POLL=1 disables the background network call (tests).
    task = None
    if os.environ.get("COEOS_NO_POLL") != "1":
        task = asyncio.create_task(updates.periodic_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="CoeOS", version=__version__, lifespan=_lifespan)


# ── Auth par compte (S1.1) ───────────────────────────────────────────────────
# Clés par utilisateur (accounts.py, SQLite). Amorçage: instance OUVERTE tant
# qu'aucune clé n'existe (localhost/dev) ; dès la première clé émise, /v1/* et
# /admin/* exigent `Authorization: Bearer ck_…` (ou `x-api-key`). /admin/*
# exige un compte admin. La clé legacy COEOS_API_KEY reste honorée comme
# compte admin implicite (compat provisionnements existants).
# Le compte résolu est posé sur request.state.account — le routeur y lit
# `mode` (byok|platform, décision #4/#5) et S1.4 y lira l'identité.

def _extract_key(request: Request) -> str:
    got = (request.headers.get("x-api-key") or "").strip()
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        got = got or auth[7:].strip()
    # Le dashboard (navigateur) porte la cle en cookie HttpOnly, pose par
    # /dashboard/login — un navigateur ne sait pas envoyer un bearer.
    return got or (request.cookies.get("coeos_key") or "").strip()


_GATED_PAGES = ("/dashboard",)   # S1.6 : le plan admin n'est plus public


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    gated_api = path.startswith("/v1/") or path.startswith("/admin/")
    # /dashboard/login doit rester joignable pour se connecter, et les images
    # (logo/favicon) sont sans secret.
    gated_page = (path == "/dashboard" or path.startswith("/dashboard/")) \
        and path != "/dashboard/login" and not path.startswith("/dashboard/images/")
    # Paywall settings (ce que Sophie vend) : /settings/* n'exige une clé
    # kind='settings' QUE si le toggle settings_auth est ON — sinon public,
    # comportement historique (aucun pull existant ne casse).
    settings_path = path in ("/settings/current", "/settings/catalog")
    gated_settings = settings_path and await run_in_threadpool(accounts.settings_auth_enabled)
    token = None
    if gated_api or gated_page or gated_settings:
        legacy = (os.environ.get("COEOS_API_KEY") or "").strip()
        need = gated_settings or legacy or await run_in_threadpool(accounts.enforced)
        if need:
            got = _extract_key(request)
            account = None
            if legacy and got == legacy:
                account = {"user_id": 0, "name": "legacy-env", "mode": "byok",
                           "admin": 1, "balance": 0}
            elif got:
                account = await run_in_threadpool(accounts.resolve, got)
            if account is None:
                if gated_page and request.method == "GET":
                    return _dashboard_login_page()   # formulaire, pas un JSON nu
                return JSONResponse({"error": {"message": "invalid or missing API key",
                                               "code": 401}}, status_code=401)
            if (path.startswith("/admin/") or gated_page) and not account.get("admin"):
                return JSONResponse({"error": {"message": "admin key required",
                                               "code": 403}}, status_code=403)
            # Le TYPE de clé : une clé d'inférence n'ouvre pas les settings et
            # vice versa. Une clé admin (ou l'env legacy) passe partout.
            kind = account.get("key_kind")
            if not account.get("admin"):
                if path.startswith("/v1/") and kind not in (None, "inference"):
                    return JSONResponse({"error": {"message": "this key is not an inference key",
                                                   "code": 403}}, status_code=403)
                if settings_path and kind != "settings":
                    return JSONResponse({"error": {"message": "this key is not a settings key",
                                                   "code": 403}}, status_code=403)
            # Quotas (S1.3) sur /v1/* — sauf /v1/me* : un compte a sec doit
            # pouvoir se reparer. Admin exempte (metering.check_limits).
            if path.startswith("/v1/") and not path.startswith("/v1/me") \
                    and int(account.get("user_id") or 0):
                hit = metering.check_limits(account)
                if hit:
                    detail, hdrs = hit
                    return JSONResponse({"error": {**detail, "code": 429}},
                                        status_code=429, headers=hdrs)
            request.state.account = account
            token = accounts.current_account.set(account)
    try:
        resp = await call_next(request)
        if token is not None and path.startswith("/v1/"):
            for k, v in metering.rate_headers(accounts.current_account.get() or {}).items():
                resp.headers.setdefault(k, v)
        return resp
    finally:
        if token is not None:
            accounts.current_account.reset(token)


# ── Login dashboard (S1.6) ───────────────────────────────────────────────────
# Le dashboard est un plan ADMIN : une page de saisie de clé pose un cookie
# HttpOnly, et les fetches du dashboard vers /admin/* le portent d'office.

def _dashboard_login_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(status_code=401, content="""<!doctype html>
<html><head><meta charset="utf-8"><title>CoeOS — admin</title><style>
 body{background:#0d1117;color:#e6edf3;font:15px system-ui;display:grid;place-items:center;height:100vh;margin:0}
 form{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:28px;width:320px}
 h1{font-size:17px;margin:0 0 14px} input{width:100%;box-sizing:border-box;padding:9px;border-radius:6px;
 border:1px solid #30363d;background:#0d1117;color:#e6edf3;margin-bottom:12px}
 button{width:100%;padding:9px;border:0;border-radius:6px;background:#238636;color:#fff;cursor:pointer}
</style></head><body><form method="post" action="/dashboard/login">
<h1>CoeOS — admin key</h1>
<input type="password" name="key" placeholder="ck_…" autofocus autocomplete="off">
<button>Enter</button></form></body></html>""")


@app.post("/dashboard/login")
async def dashboard_login(request: Request):
    # Parse urlencoded en stdlib — request.form() exigerait python-multipart.
    from urllib.parse import parse_qs
    body = (await request.body()).decode()
    key = (parse_qs(body).get("key") or [""])[0].strip()
    account = await run_in_threadpool(accounts.resolve, key)
    legacy = (os.environ.get("COEOS_API_KEY") or "").strip()
    ok = (legacy and key == legacy) or (account and account.get("admin"))
    if not ok:
        return _dashboard_login_page()
    resp = RedirectResponse("/dashboard", status_code=303)
    secure = request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "") == "https"
    resp.set_cookie("coeos_key", key, httponly=True, samesite="lax",
                    secure=secure, max_age=30 * 86400, path="/")
    return resp


# ── Compte courant : reglages self-service (S1.2) ────────────────────────────
# Un compte byok gere SES cles provider ici ; un compte platform n'a rien a
# gerer (c'est le backoffice qui detient les cles) — on le lui dit.

def _require_account(request: Request) -> dict:
    account = getattr(request.state, "account", None)
    if not account or not int(account.get("user_id") or 0):
        raise HTTPException(status_code=400, detail={
            "error": "no_account_context",
            "message": "This instance runs open (no accounts) or you are using "
                       "the legacy operator key. Per-user settings need a real "
                       "account key (ck_…)."})
    return account


@app.get("/v1/me")
async def me(request: Request):
    account = _require_account(request)
    pids = await run_in_threadpool(
        accounts.configured_providers, "user", int(account["user_id"]))
    cfg = await run_in_threadpool(load_config)
    applied = router.coeos_cfg_for(cfg, account)
    return {"name": account["name"], "mode": account["mode"],
            "admin": bool(account.get("admin")),
            # Le setting qui s'applique VRAIMENT à ce token — un nom assigné mais
            # absent du catalogue retombe sur l'actif, et on veut que ça se voie.
            "setting": router.setting_name_for(cfg, account),
            "setting_assigned": str(account.get("setting") or "") or None,
            "axes_servable": len(router.bound_axes(applied)) - len(router.unservable_axes(applied)),
            "providers": {pid: {"api_key_set": pid in pids} for pid in PROVIDERS}}


class ProviderKeyBody(BaseModel):
    api_key: str


@app.put("/v1/me/providers/{pid}")
async def me_set_provider_key(pid: str, body: ProviderKeyBody, request: Request):
    account = _require_account(request)
    if pid not in PROVIDERS:
        raise HTTPException(404, f"unknown provider: {pid}")
    if account["mode"] == "platform":
        raise HTTPException(status_code=400, detail={
            "error": "platform_account",
            "message": "Your account is served by platform keys — nothing to "
                       "configure here."})
    if not body.api_key.strip():
        raise HTTPException(400, "api_key is empty")
    await run_in_threadpool(accounts.set_provider_key,
                            "user", int(account["user_id"]), pid, body.api_key)
    return {"ok": True, "provider": pid, "api_key_set": True}


@app.delete("/v1/me/providers/{pid}")
async def me_delete_provider_key(pid: str, request: Request):
    account = _require_account(request)
    n = await run_in_threadpool(accounts.delete_provider_key,
                                "user", int(account["user_id"]), pid)
    return {"ok": True, "removed": n}


# ── Usage (S1.4) ─────────────────────────────────────────────────────────────

@app.get("/v1/usage")
async def my_usage(request: Request, days: int = 30):
    account = _require_account(request)
    return await run_in_threadpool(metering.summary, int(account["user_id"]), days)


@app.get("/admin/usage")
async def admin_usage(days: int = 30):
    return await run_in_threadpool(metering.summary, None, days)


# ── Clés plateforme : backoffice TMB (S1.6, admin) ───────────────────────────

@app.get("/admin/platform/providers")
async def platform_providers():
    pids = await run_in_threadpool(accounts.configured_providers, "platform", 0)
    return {pid: {"api_key_set": pid in pids} for pid in PROVIDERS}


@app.put("/admin/platform/providers/{pid}")
async def platform_set_key(pid: str, body: ProviderKeyBody):
    if pid not in PROVIDERS:
        raise HTTPException(404, f"unknown provider: {pid}")
    if not body.api_key.strip():
        raise HTTPException(400, "api_key is empty")
    await run_in_threadpool(accounts.set_provider_key, "platform", 0, pid, body.api_key)
    return {"ok": True, "provider": pid, "api_key_set": True}


@app.delete("/admin/platform/providers/{pid}")
async def platform_delete_key(pid: str):
    n = await run_in_threadpool(accounts.delete_provider_key, "platform", 0, pid)
    return {"ok": True, "removed": n}


# ── Découverte LAN (CodeOS / client CoeOS) ──────────────────────────────────────
# Public (jamais gaté : middleware ne couvre que /v1/ et /admin/). vendor='odyssai.eu'
# pour que le scanner CodeOS matche. Pairing "pré-digéré" : la clé statique
# COEOS_API_KEY est pré-partagée (provisionnée), aucun handshake /pair.
@app.get("/.well-known/inference-engine.json")
async def well_known_inference_engine():
    gated = await run_in_threadpool(accounts.client_enforced)
    return {
        "vendor": "odyssai.eu",
        "product": "coeos-se",
        # /v1/models n'est plus public une fois l'auth active : le registre
        # revele la flotte, un anonyme n'a pas a le lire (S1.1).
        "auth": {"required": gated, "scheme": "bearer", "scope": "/v1/*",
                 "public_routes": ["/health", "/.well-known/*", "/settings/current"]},
    }


@app.get("/.well-known/coeos.json")
async def well_known_coeos():
    """Descripteur lu par le client CoeOS (`box-client.ts`, M3-10 / #95) pour
    afficher l'etat de la box dans Settings et connaitre le profil d'install.

    Public (route `/.well-known/*`, jamais gatee). Retourne `version` et
    `profile` (`vps`/`local`, pose au deploy, jamais modifiable par un user —
    D27). Les `endpoints` par classe (`local`/`cloud_dpa`/`cloud_no_dpa`) sont
    a `null` en V1 : la box route en interne par axe, pas par un endpoint fixe
    par classe ; le detail par classe est le suivi #95. Le client tolere `null`
    et affiche alors la box comme joignable avec son profil."""
    return {
        "version": __version__,
        "profile": router.coeos_profile(),
        "endpoints": {"local": None, "cloud_dpa": None, "cloud_no_dpa": None},
    }


# ── Canal de fraicheur des settings (S1.5) ───────────────────────────────────
# Le SaaS DEVIENT la source : la ou SE pollait GitHub (desormais gele), les
# clients (Nemo, une autre instance CoeOS) interrogent CE endpoint. C'est le
# moat — les tables TMB regenerees. On sert la table STRIPPEE de tout ce qui
# est propre a l'operateur : le registre (models, alias de flotte), le
# decider et le regime (infra locale), l'etat enabled. Il reste l'essentiel :
# quel modele logique gagne quel axe, avec sa provenance bench.
# Public et cachable (ETag = date `updated`) : lire "quel modele ou" n'a pas
# besoin d'authentification, comme le canal GitHub public d'avant.

def _public_settings() -> dict:
    """Le setting tel qu'il est PUBLIÉ vers les box.

    Il porte sa propre définition complète : la taxonomie, les liaisons, le
    REGISTRE et le décideur. Le registre voyageait pas jusqu'au 17/08/2026 —
    posture SaaS où le registre était l'« armée » propre à chaque opérateur.
    En BYOK c'est faux : un setting publié dont on ignore quels modèles il
    utilise n'est pas servable, les axes lient des noms logiques que la box
    n'aurait aucun moyen de résoudre. Les clés, elles, restent chez le client
    (`cfg['providers']`) et ne sont pas ici — ce sont elles qui décident ce
    que la box peut réellement servir, pas le nommage.

    Reste dehors : tout ce qui est propre à l'opérateur — clés, mode
    standard/expert, overrides d'axes, affectations d'agents, régime."""
    return _publishable(load_config().get("coeos") or {})


def _publishable(c: dict) -> dict:
    """La forme publiable d'UN setting (cf. _public_settings)."""
    out = {
        # Un setting publié est fait pour SERVIR. Sans ce champ, un setting
        # tiré du master arrivait désactivé : la box exposait ses modèles mais
        # plus le routeur `CoeOS`. Trouvé en testant la boucle sur rpi-dev,
        # pas en lisant le code.
        "enabled": bool(c.get("enabled", True)),
        "name": c.get("name"),
        "updated": c.get("updated"),
        "default_axis": c.get("default_axis"),
        "axes": [{"key": a.get("key"), "label": a.get("label"),
                  "model": a.get("model"), "description": a.get("description"),
                  "bench": a.get("bench"), "verified": a.get("verified")}
                 for a in (c.get("axes") or [])],
        "models": registry_of(c),
    }
    # Le décideur fait partie du setting : sans lui la box ne sait pas classer.
    spec = decider_spec(c)
    if spec:
        out["decider"] = spec
    # La score table fait la valeur du mode expert (candidats classés). Grosse,
    # mais l'ETag fait qu'une box à jour ne la retélécharge pas.
    if isinstance(c.get("score_table"), dict):
        out["score_table"] = c["score_table"]
    return out


@app.get("/settings/current")
async def settings_current(request: Request):
    pub = await run_in_threadpool(_public_settings)
    etag = f'W/"{pub.get("updated") or "none"}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(pub, headers={"ETag": etag,
                                      "Cache-Control": "public, max-age=300"})


# ── OpenAI surface ───────────────────────────────────────────────────────────

async def resolve_target(cfg: dict, model: str, headers, body: dict) -> tuple[str, str, dict]:
    """Shared model-id resolution for both wire surfaces (OpenAI + Anthropic):
    'coeos' → router decision; 'or:<id>' → explicit OpenRouter; a registry
    logical → registry lookup. Returns (pid, upstream, decision_headers)."""
    # 1. The virtual router id.
    if model.lower() == COEOS_MODEL_ID:
        d = await coeos_resolve(cfg, headers, body)
        # Politique thinking par AXE : un axe marque thinking:false (ex. l'axe
        # chat cowork de Nemo) coupe le raisonnement cote serveur — proxy traduit
        # enable_thinking:false -> reasoning:{enabled:false} pour OR. Le CLIENT
        # garde la priorite : on n'ecrase jamais un thinking/enable_thinking
        # explicite deja pose par l'appelant.
        if d.get("thinking") is False and "enable_thinking" not in body and "thinking" not in body:
            body["enable_thinking"] = False
        return d["provider"], d["upstream"], {
            "x-coeos-axis": d["axis"], "x-coeos-model": d["logical"],
            "x-coeos-provider": d["provider"]}

    # 2. Explicit OpenRouter prefix: "or:z-ai/glm-5.2".
    if ":" in model:
        prefix, upstream = model.split(":", 1)
        pid = PREFIX_TO_PROVIDER.get(prefix.lower())
        if pid and upstream.strip():
            if provider_key(cfg, pid) is None:
                raise HTTPException(status_code=503, detail={
                    "error": "provider_key_missing",
                    "message": f"{PROVIDERS[pid]['label']} key not set. Add it in "
                               f"the dashboard or via {PROVIDERS[pid]['api_key_env']}."})
            return pid, upstream.strip(), {"x-coeos-provider": pid}

    # 3. A logical model name from the registry.
    resolved = resolve_logical(cfg, model)
    if resolved is not None:
        pid, upstream = resolved
        return pid, upstream, {"x-coeos-model": model, "x-coeos-provider": pid}

    raise HTTPException(status_code=404, detail={
        "error": "unknown_model",
        "message": f"unknown model {model!r}. Use 'coeos', a logical model from "
                   "the registry (GET /v1/models), or an explicit 'or:<id>'."})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Raw dict on purpose: a strict schema would strip fields like
    # `reasoning_effort` or `extra_body`-style extras. Passthrough is the
    # contract — the upstream sees exactly what the client sent.
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        raise HTTPException(400, "body must be a JSON object")
    cfg = load_config()
    model = str(body.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "missing 'model'")
    pid, upstream, decision = await resolve_target(cfg, model, request.headers, body)
    metering.current_decision.set(decision)
    return await proxy.proxy_chat(cfg, pid, upstream, body, decision_headers=decision)


# ── Anthropic surface (/v1/messages) ─────────────────────────────────────────
# Claude Code & Anthropic SDK clients point ANTHROPIC_BASE_URL here. Claude
# tier names map to the router (haiku pinned to the fast axis); the request is
# translated to the OpenAI shape, routed exactly like /v1/chat/completions,
# and the upstream reply is translated (or stream-transcoded) back.

@app.post("/v1/messages")
async def anthropic_messages(req: anthropic_api.AnthropicMessagesRequest,
                             request: Request):
    cfg = load_config()
    model, forced_axis = anthropic_api.resolve_tier(req.model)
    headers = {k.lower(): v for k, v in request.headers.items()}
    if forced_axis:
        headers["x-coeos-axis"] = forced_axis
    body = anthropic_api.to_openai_body(req, stream=bool(req.stream))
    pid, upstream, decision = await resolve_target(cfg, model, headers, body)
    metering.current_decision.set(decision)
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    label = decision.get("x-coeos-model") or upstream

    if not req.stream:
        status, payload = await proxy.unary_upstream_json(cfg, pid, upstream, body)
        if status >= 400 or not (payload.get("choices")):
            err = (payload.get("error") or {}) if isinstance(payload, dict) else {}
            return JSONResponse(
                {"type": "error",
                 "error": {"type": "api_error",
                           "message": str(err.get("message") or payload)[:300]}},
                status_code=status if status >= 400 else 502, headers=decision)
        return JSONResponse(
            anthropic_api.openai_to_anthropic_response(payload, msg_id, label),
            headers=decision)

    chunks = proxy.stream_upstream_chunks(cfg, pid, upstream, body)
    return StreamingResponse(
        anthropic_api.transcode_stream(chunks, msg_id, label),
        media_type="text/event-stream", headers=decision)


@app.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(req: anthropic_api.AnthropicMessagesRequest):
    # Claude Code probes this to budget its context window — without it, it
    # refuses to talk to a custom ANTHROPIC_BASE_URL. Char-based estimate.
    return {"input_tokens": anthropic_api.estimate_input_tokens(req)}


@app.get("/v1/models")
async def v1_models():
    cfg = load_config()
    # Le catalogue d'un user, c'est CE que son setting expose (W1).
    c = router.coeos_cfg_for(cfg, accounts.current_account.get())
    now = int(time.time())
    data: list[dict] = []
    axes = bound_axes(c)
    # Absent n'est pas faux (cf. coeos_resolve) : un master ancien publie sans.
    if c.get("enabled") is not False and axes:
        ax_map = {ax["key"]: ax["model"] for ax in axes}
        data.append({
            "id": COEOS_DISPLAY_ID, "object": "model", "created": now,
            "owned_by": "coeos-se", "root": COEOS_DISPLAY_ID,
            "x_coeos": {
                "router": True,
                "settings": c.get("name"),
                "updated": c.get("updated"),
                "decider": (decider_spec(c) or {}).get("name"),
                "axes": ax_map,
            },
            # Contrat COMMUN avec OdyssAI-X (2026-08-01). Les consommateurs
            # (console CoeOS, CodeOS) identifient le routeur par METADONNEE —
            # `x_odyssai.kind == "router"` — jamais par le nom du modele. Sans
            # ce bloc, SE publiait bien l'id `CoeOS` mais restait invisible a
            # tout ce qui pilote un moteur : "no router published". SE et
            # OdyssAI-X ne tournent jamais ensemble, ils doivent donc etre
            # indiscernables pour un client.
            "x_odyssai": {
                "kind": "router", "ready": True,
                "axes": ax_map,
            },
        })
    for logical, entry in sorted(registry_of(c).items()):
        entry = entry if isinstance(entry, dict) else {}
        resolved = resolve_logical(cfg, logical)
        data.append({
            "id": logical, "object": "model", "created": now,
            # Un modele du registry est servi par OpenRouter : meme forme que
            # les alias cloud d'OdyssAI-X (`odyssai-cloud-<provider>`), pour
            # que le tri local/cloud et la preference de passerelle marchent
            # a l'identique cote console.
            "owned_by": "odyssai-cloud-openrouter",
            "x_coeos": {
                "router": False,
                "name": entry.get("name") or logical,
                "or": entry.get("or") or None,
                "resolvable": resolved is not None,
            },
            "x_odyssai": {
                "kind": "cloud", "ready": resolved is not None,
                "loaded": resolved is not None, "warm": True,
                # `upstream` = l'id canonique du provider : c'est la cle de
                # jointure forte cote console (etage 0 de join()).
                "upstream": entry.get("or") or logical,
            },
        })
    return {"object": "list", "data": data}


@app.get("/admin/army")
async def admin_army():
    """The roster of models CoeOS can field — display names only. The routing
    table itself (which axis → which model) is intentionally not surfaced."""
    c = coeos_cfg(load_config())
    army = [(e.get("name") or logical) for logical, e in registry_of(c).items()
            if isinstance(e, dict)]
    return {"enabled": bool(c.get("enabled")), "settings": c.get("name"),
            "version": c.get("version"), "updated": c.get("updated"),
            "army": sorted(set(army))}


# ── Settings par compte (W1) ────────────────────────────────────────────────
# Le catalogue tient les settings publiés (full/open/eco). L'org en choisit un
# par défaut ; un user peut en porter un autre via son token. Sans catalogue,
# tout le monde route sur le setting actif — le solo ne paie rien.

@app.get("/admin/settings")
async def admin_settings():
    cfg = await run_in_threadpool(load_config)
    names = router.setting_names(cfg)
    users = await run_in_threadpool(accounts.list_all)
    assigned: dict[str, str] = {}
    for row in users:
        want = str(row.get("setting") or "")
        if want:
            assigned[str(row["name"])] = want
    return {"catalog": [{"name": n,
                         "label": (router.catalog_of(cfg)[n].get("name") or n),
                         "updated": router.catalog_of(cfg)[n].get("updated"),
                         "axes": len(router.bound_axes(router.catalog_of(cfg)[n]))}
                        for n in names],
            "org_default": str(cfg.get("setting") or "") or None,
            "active": coeos_cfg(cfg).get("name"),
            "assigned": assigned}


@app.get("/settings/catalog")
async def settings_catalog(request: Request):
    """Le catalogue publié — ce qu'une box récupère du master. Public comme
    `/settings/current` : c'est de la table de routage, pas un secret."""
    cfg = await run_in_threadpool(load_config)
    catalog = router.catalog_of(cfg)
    published = {name: _publishable(setting) for name, setting in catalog.items()}
    stamp = "|".join(f"{n}:{(s.get('updated') or '')}" for n, s in sorted(catalog.items()))
    etag = f'W/"{stamp or "none"}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse({"settings": published},
                        headers={"ETag": etag, "Cache-Control": "public, max-age=300"})


@app.put("/admin/settings/catalog/{name}")
async def admin_settings_put(name: str, request: Request):
    """Pose un setting au catalogue sous un nom (`full`, `open`, `eco`…).

    C'est ainsi qu'un MASTER compose ce qu'il publie : sans ça, le catalogue ne
    pouvait être peuplé qu'en éditant le fichier de config à la main. Accepte
    la même chose que `PUT /admin/coeos` — une TMB Settings, ou une score table
    auto-détectée et résolue une fois contre son propre registre."""
    key = (name or "").strip().lower()
    if not key or not key.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, detail={"error": "bad_name",
            "message": "nom de setting invalide (alphanumérique, - et _)."})
    raw = await request.json()
    if not isinstance(raw, dict):
        raise HTTPException(400, detail={"error": "bad_body",
                                         "message": "expected a JSON object."})
    table = raw if raw.get("format") == "tmb-score-table/1" else None
    if table is not None:
        registry = registry_of(raw) or registry_of(coeos_cfg(load_config()))
        raw = {"name": table.get("source") or key, "updated": table.get("updated"),
               "axes": resolve_score_table(table, registry),
               "models": registry, "score_table": table, "enabled": True}
    if not isinstance(raw.get("axes"), list):
        raise HTTPException(422, detail={"error": "bad_setting",
            "message": "un setting doit porter une liste `axes`."})
    raw.setdefault("enabled", True)

    def _write():
        with config_txn() as cfg:
            cat = dict(cfg.get("settings") or {})
            cat[key] = raw
            cfg["settings"] = cat
    await run_in_threadpool(_write)
    return {"ok": True, "name": key, "axes": len(raw.get("axes") or []),
            "models": len(raw.get("models") or {})}


@app.delete("/admin/settings/catalog/{name}")
async def admin_settings_delete(name: str):
    key = (name or "").strip().lower()
    cfg = await run_in_threadpool(load_config)
    if key not in router.catalog_of(cfg):
        raise HTTPException(404, detail={"error": "unknown_setting",
                                         "message": f"'{key}' n'est pas au catalogue."})
    # Des users peuvent y être assignés : on le dit plutôt que de les casser
    # en silence — leur routage retombera sur le setting actif.
    users = [str(u["name"]) for u in await run_in_threadpool(accounts.list_all)
             if str(u.get("setting") or "") == key]

    def _write():
        with config_txn() as c:
            cat = dict(c.get("settings") or {})
            cat.pop(key, None)
            c["settings"] = cat
            if str(c.get("setting") or "") == key:
                c["setting"] = ""
    await run_in_threadpool(_write)
    return {"ok": True, "removed": key, "orphaned_users": sorted(set(users))}


@app.post("/admin/settings/load/{name}")
async def admin_settings_load(name: str):
    """Charge un setting du catalogue comme table de routage active.

    Remplace les axes, le registre et le decideur — jamais les providers ni
    leurs cles : ce sont les comptes du client, ils ne dependent pas du setting
    qu'il choisit."""
    key = (name or "").strip().lower()
    cfg = await run_in_threadpool(load_config)
    picked = router.catalog_of(cfg).get(key)
    if not isinstance(picked, dict):
        raise HTTPException(404, detail={"error": "unknown_setting",
                                         "message": f"'{key}' n'est pas au catalogue."})

    def _write():
        with config_txn() as c:
            c["coeos"] = {**picked, "enabled": picked.get("enabled", True)}
    await run_in_threadpool(_write)
    c = coeos_cfg(await run_in_threadpool(load_config))
    return {"ok": True, "loaded": key, "name": c.get("name"),
            "axes": len(router.bound_axes(c))}


@app.post("/admin/settings/pull")
async def admin_settings_pull():
    """Récupère le catalogue du master dans cette box."""
    try:
        return await updates.pull_catalog()
    except Exception as e:
        raise HTTPException(502, detail={"error": "catalog_unreachable",
                                         "message": str(e)[:200]})


class OrgSetting(BaseModel):
    setting: str = ""


@app.put("/admin/settings/default")
async def admin_settings_default(req: OrgSetting):
    cfg = await run_in_threadpool(load_config)
    want = (req.setting or "").strip()
    if want and want not in router.catalog_of(cfg):
        raise HTTPException(400, detail={"error": "unknown_setting",
            "message": f"'{want}' n'est pas au catalogue de cette box."})

    def _write():
        with config_txn() as c:
            c["setting"] = want
    await run_in_threadpool(_write)
    return {"ok": True, "org_default": want or None}


class UserSetting(BaseModel):
    setting: str = ""


@app.put("/admin/users/{name}/setting")
async def admin_user_setting(name: str, req: UserSetting):
    cfg = await run_in_threadpool(load_config)
    want = (req.setting or "").strip()
    if want and want not in router.catalog_of(cfg):
        raise HTTPException(400, detail={"error": "unknown_setting",
            "message": f"'{want}' n'est pas au catalogue de cette box."})
    try:
        out = await run_in_threadpool(accounts.set_setting, name, want)
    except SystemExit as e:
        raise HTTPException(404, detail={"error": "unknown_user", "message": str(e)})
    return {"ok": True, **out}


# ── Table de mapping : axe → modèle, + modes standard/expert ────────────────
# La box appartient au client : contrairement au SaaS, on ne cache PAS la table
# de routage. En standard elle est en lecture (la box route toute seule) ; en
# expert l'opérateur réaffecte un modèle par axe.

@app.get("/admin/mapping")
async def admin_mapping():
    c = coeos_cfg(await run_in_threadpool(load_config))
    reg = router.registry_of(c)
    return {"settings": c.get("name"), "updated": c.get("updated"),
            "has_score_table": isinstance(c.get("score_table"), dict),
            # L'armée complète : en expert sans score_table embarqué, c'est
            # elle qui peuple le choix (sinon on ne pourrait rien réaffecter).
            "army": sorted(({"logical": k,
                             "name": (v.get("name") if isinstance(v, dict) else None) or k}
                            for k, v in reg.items()), key=lambda r: r["name"].lower()),
            "axes": router.mapping_table(c)}


class AxisBinding(BaseModel):
    """Une liaison d'axe : le modèle, et le provider qui le sert."""
    model: str = ""
    provider: str = ""


@app.put("/admin/mapping/axis/{axis_key}")
async def admin_mapping_axis(axis_key: str, req: AxisBinding):
    """Réaffecte un axe. C'est l'édition NORMALE du setting : un client doit
    pouvoir définir ses 7 axes, sinon le setting n'existe pas pour lui.

    `model` vide remet le modèle publié. Le couple (provider, modèle) est écrit
    au registre sous le nom du modèle — pas de nom logique à inventer pour
    l'opérateur, il choisit un modèle dans une liste et c'est tout."""
    cfg = await run_in_threadpool(load_config)
    c = coeos_cfg(cfg)
    if axis_key not in {str(a.get("key")) for a in router.axes_of(c)}:
        raise HTTPException(404, detail={"error": "unknown_axis",
            "message": f"axe inconnu : {axis_key}"})
    want = (req.model or "").strip()
    pid = (req.provider or "").strip().lower()
    if want and pid and pid not in providers_of(cfg):
        raise HTTPException(400, detail={"error": "unknown_provider",
            "message": f"provider inconnu : {pid}"})

    def _write():
        with config_txn() as cfg2:
            c2 = cfg2.get("coeos") or {}
            axes = [dict(a) for a in (c2.get("axes") or []) if isinstance(a, dict)]
            reg = dict(c2.get("models") or {})
            if want:
                # Le modèle upstream EST son nom logique : l'opérateur ne
                # devrait pas avoir à inventer un alias pour changer un axe.
                reg.setdefault(want, {})
                entry = dict(reg[want])
                entry.setdefault("name", want)
                entry["endpoint"] = want
                if pid:
                    entry["provider"] = pid
                reg[want] = entry
            for a in axes:
                if str(a.get("key")) != axis_key:
                    continue
                # Le modèle publié se capture À L'INSTANT où on le remplace :
                # sans ça il est perdu, et l'opérateur qui a changé d'avis doit
                # aller le rechercher. Une fois posé, il ne bouge plus — un
                # nouvel import de setting réécrit l'axe entier, donc il se
                # remet naturellement à jour.
                if "suggested" not in a and (a.get("model") or "").strip():
                    a["suggested"] = a["model"]
                a["model"] = want
            c2["axes"] = axes
            c2["models"] = reg
            cfg2["coeos"] = c2
    await run_in_threadpool(_write)
    c = coeos_cfg(await run_in_threadpool(load_config))
    row = next((r for r in router.mapping_table(c) if r["key"] == axis_key), None)
    return {"ok": True, "axis": row}


# ── Settings auto-update (poll coeos-master, offer, apply on demand) ─────────

@app.get("/admin/settings-update")
async def admin_settings_update(check: bool = False):
    """Update status. `?check=true` forces a fresh GitHub poll."""
    if check or updates.STATE.get("checked_at") is None:
        return await updates.check()
    return dict(updates.STATE)


@app.post("/admin/settings-update/apply")
async def admin_settings_update_apply():
    """Download the latest TMB Settings from GitHub and apply (keys kept)."""
    try:
        return await updates.apply()
    except Exception as e:
        raise HTTPException(502, f"update failed: {e}")


# ── Admin: CoeOS settings ────────────────────────────────────────────────────

class CoeosSettings(BaseModel):
    # The TMB Settings the operator imports. Everything is data: the taxonomy
    # (axes) AND the per-axis bindings AND the per-provider registry.
    enabled: Optional[bool] = None
    name: Optional[str] = None
    regime: Optional[str] = None
    updated: Optional[str] = None
    note: Optional[str] = None
    # The decider's own setting: {name, or, comet}. `decider_model` (a logical
    # name looked up in the registry) is the legacy form, still accepted.
    decider: Optional[dict] = None
    decider_model: Optional[str] = None
    default_axis: Optional[str] = None
    axes: Optional[list] = None
    models: Optional[dict] = None
    # Affectation editable agent -> {model?, axis?} (N1.1c). Fusionnee sur le
    # manifeste par _agent_assignment ; cree les roles absents (ex. nemo-*).
    assignment: Optional[dict] = None
    score_table: Optional[dict] = None    # provenance only (2026-07-14): the
        # raw TMB Score Table that produced `axes` via a one-time resolve at
        # import — NOT consulted live by the router. Kept so a saved config
        # can be re-resolved later (e.g. after the registry changes) without
        # re-uploading the file.


def _validate_axes(axes: list) -> None:
    if not isinstance(axes, list):
        raise HTTPException(400, detail={"error": "bad_axes",
            "message": "axes must be a list of {key, label, model} objects."})
    seen = set()
    for ax in axes:
        if not isinstance(ax, dict) or not ax.get("key"):
            raise HTTPException(400, detail={"error": "bad_axis",
                "message": "each axis needs a non-empty 'key'."})
        k = str(ax["key"]).strip().lower()
        if k in seen:
            raise HTTPException(400, detail={"error": "dup_axis",
                "message": f"duplicate axis key: {k!r}."})
        seen.add(k)
        m = ax.get("model")
        if m and str(m).strip().lower() == COEOS_MODEL_ID:
            raise HTTPException(400, detail={"error": "reserved_id",
                "message": "'coeos' is the router's own id and can't be bound to an axis."})
        p = ax.get("provider")
        if p and p not in PROVIDERS:
            raise HTTPException(400, detail={"error": "bad_provider_pin",
                "message": f"axis {k!r}: provider must be one of {sorted(PROVIDERS)}."})


@app.get("/admin/keys")
async def admin_keys_list():
    """Onglet API Keys : les 2 toggles + les clés (inference/settings) avec
    secret, état actif/suspendu (admin)."""
    enabled = await run_in_threadpool(accounts.auth_enabled)
    auto = await run_in_threadpool(accounts.enforced)
    settings_on = await run_in_threadpool(accounts.settings_auth_enabled)
    keys = await run_in_threadpool(accounts.list_keys)
    effective = auto if enabled is None else enabled
    return {"auth_enabled": effective, "explicit": enabled is not None,
            "settings_auth": settings_on, "keys": keys}


@app.post("/admin/keys")
async def admin_keys_create(body: dict = Body(default={})):
    label = (body.get("name") or "").strip()
    kind = (body.get("kind") or "inference").strip()
    if kind not in ("inference", "settings"):
        raise HTTPException(status_code=400, detail={"error": "bad_kind"})
    key = await run_in_threadpool(accounts.issue_key_for, "default", label, kind)
    return {"ok": True, "key": key, "name": label, "kind": kind}


@app.delete("/admin/keys/{prefix}")
async def admin_keys_delete(prefix: str):
    n = await run_in_threadpool(accounts.revoke_key, prefix)
    if not n:
        raise HTTPException(status_code=404, detail={"error": "unknown_key"})
    return {"ok": True, "revoked": prefix}


@app.put("/admin/keys/{prefix}/active")
async def admin_keys_active(prefix: str, body: dict = Body(default={})):
    """Suspend/réactive une clé sans la supprimer (cycle abonnement)."""
    active = bool(body.get("active"))
    n = await run_in_threadpool(accounts.set_key_active, prefix, active)
    if not n:
        raise HTTPException(status_code=404, detail={"error": "unknown_key"})
    return {"ok": True, "prefix": prefix, "active": active}


@app.put("/admin/keys/auth")
async def admin_keys_auth(body: dict = Body(default={})):
    on = bool(body.get("enabled"))
    await run_in_threadpool(accounts.set_auth_enabled, on)
    return {"ok": True, "auth_enabled": on}


@app.put("/admin/keys/settings-auth")
async def admin_keys_settings_auth(body: dict = Body(default={})):
    """Le paywall des settings : ON exige une clé kind='settings' sur /settings/*."""
    on = bool(body.get("enabled"))
    await run_in_threadpool(accounts.set_settings_auth, on)
    return {"ok": True, "settings_auth": on}


@app.get("/admin/coeos")
async def admin_coeos_get():
    return coeos_cfg(load_config())


@app.put("/admin/coeos")
async def admin_coeos_update(request: Request):
    """Importing a TMB Settings file = a PUT with the file's JSON. A TMB
    SCORE TABLE (format: tmb-score-table/1) is auto-detected and resolved
    ONCE into axes=[{key,label,model,description}] against the CURRENT
    registry (best score per axis, reference-role rows excluded, ties
    broken by cost) — the raw table then travels along as `score_table` for
    provenance/re-resolve, but the router only ever reads the resolved
    `axes`. Partial updates supported (only non-None fields are applied)."""
    raw = await request.json()
    if not isinstance(raw, dict):
        raise HTTPException(400, detail={"error": "bad_body",
            "message": "expected a JSON object."})
    # Score table, unwrapped (the natural "drop this file in" gesture) OR
    # wrapped as {"score_table": {...}} — both resolve identically. Checking
    # BOTH matters: only checking top-level `format` would silently skip the
    # resolve step for the wrapped form (it'd just store an inert table with
    # axes left unresolved).
    table = raw if raw.get("format") == "tmb-score-table/1" else (
        raw.get("score_table") if isinstance(raw.get("score_table"), dict)
        and raw["score_table"].get("format") == "tmb-score-table/1" else None)
    if table is not None:
        registry = registry_of(coeos_cfg(load_config()))
        resolved_axes = resolve_score_table(table, registry)
        raw = {"name": table.get("source") or "TMB Score Table",
               "updated": table.get("updated"), "axes": resolved_axes,
               "score_table": table}
    try:
        req = CoeosSettings(**raw)
    except Exception as e:
        raise HTTPException(422, detail={"error": "bad_coeos_config",
            "message": f"could not parse CoeOS config: {e}"})
    if req.axes is not None:
        _validate_axes(req.axes)
    if req.models is not None and not isinstance(req.models, dict):
        raise HTTPException(400, detail={"error": "bad_registry",
            "message": "models must be an object: logical name -> {name, or}."})
    if req.decider is not None:
        bad = [k for k, v in req.decider.items() if not isinstance(v, (str, type(None)))]
        if bad:
            raise HTTPException(400, detail={"error": "bad_decider",
                "message": "decider must be {name, or} with string values."})
    with config_txn() as cfg:
        c = cfg.get("coeos") or {}
        for field in ("enabled", "name", "regime", "updated", "note",
                      "decider", "decider_model", "default_axis", "axes", "models",
                      "score_table", "assignment"):
            val = getattr(req, field)
            if val is not None:
                c[field] = bool(val) if field == "enabled" else val
        cfg["coeos"] = c
    return coeos_cfg(load_config())


# ── Agents : affectation LECTURE SEULE pour CodeOS ──────────────────────────
# SE n'a pas de surface de personnalisation, par choix : les modeles sont ceux
# des settings TMB officiels qu'il embarque. On expose donc /api/state avec la
# MEME forme que la console CoeOS (le plan de controle de l'edition complete),
# limitee aux champs qu'un client agent lit. Aucune ecriture.
def _roles_manifest() -> dict:
    try:
        path = importlib.resources.files("coeos") / "settings" / "coeos-roles.json"
        return (json.loads(path.read_text()) or {}).get("roles") or {}
    except Exception as e:
        sys.stderr.write(f"[coeos-se] roles manifest unreadable: {e}\n")
        return {}


def _agent_assignment(c: dict) -> dict:
    """role -> {model, axis, axes}. Par defaut un role appelle le ROUTEUR avec
    son hint d'axe : le routage par critere reste la regle, et c'est ce qui
    rend SE et l'edition complete indiscernables pour un agent.

    Exception, les groupes `panel` : router par axe ramenerait direct et
    alternative sur le modele du meme critere. Un membre de panel recoit donc
    un modele CONCRET, distinct de ses pairs — pris dans les bindings des
    settings officiels, en parcourant ses propres axes. Rien a regler : la
    diversite sort de la donnee deja presente."""
    binding = {a["key"]: a["model"] for a in bound_axes(c)}
    roles = _roles_manifest()
    claimed: dict[str, set] = {}
    out = {}
    for role, spec in roles.items():
        axes = spec.get("axes") or []
        first = axes[0] if axes else None
        group = spec.get("panel")
        model = COEOS_DISPLAY_ID
        if group:
            taken = claimed.setdefault(group, set())
            pick = next((binding[a] for a in axes
                         if binding.get(a) and binding[a] not in taken), None)
            # groupe plus large que le vivier de ses axes : on elargit a tous
            # les bindings plutot que de rendre deux membres identiques
            pick = pick or next((m for m in binding.values() if m not in taken), None)
            if pick:
                taken.add(pick)
                model = pick
        out[role] = {"model": model, "axis": first, "axes": axes,
                     "panel": group, "overridden": False}
    # Override editable (console -> settings.assignment). Cree les roles absents
    # du manifeste (les nemo-*) et surcharge model/axis. Socle "editable" : un
    # role peut pointer un modele CONCRET (ex. le chat non-raisonneur) au lieu
    # du routeur. N1.1c.
    override = c.get("assignment") if isinstance(c.get("assignment"), dict) else {}
    for role, ov in override.items():
        if not isinstance(ov, dict):
            continue
        cur = out.get(role) or {"model": COEOS_DISPLAY_ID, "axis": None,
                                "axes": [], "panel": None, "overridden": False}
        if ov.get("model"):
            cur["model"] = ov["model"]
        if "axis" in ov:
            cur["axis"] = ov["axis"]
        cur["overridden"] = True
        out[role] = cur
    return out


@app.get("/api/state")
async def api_state():
    """Etat lisible par un client agent (CodeOS). Lecture seule : SE ne
    personnalise pas, il sert les settings officiels."""
    c = coeos_cfg(load_config())
    return {"product": "coeos-se", "readonly": True,
            "router": COEOS_DISPLAY_ID if (c.get("enabled") and bound_axes(c)) else None,
            "provider": "coeos", "settings": c.get("name"),
            "updated": c.get("updated"),
            "assignment": _agent_assignment(c)}


@app.get("/admin/coeos/decisions")
async def admin_coeos_decisions():
    """Routing decision counts (model x axis x provider) for visibility."""
    return {"decisions": [
        {"model": k[0], "axis": k[1], "provider": k[2], "count": v}
        for k, v in sorted(decisions.items(), key=lambda kv: -kv[1])]}


@app.delete("/admin/coeos/decisions")
async def admin_coeos_decisions_clear():
    decisions.clear()
    return {"ok": True, "decisions": []}


# ── Admin: named config snapshots (save/load/delete) ────────────────────────

class ConfigName(BaseModel):
    name: str


@app.get("/admin/coeos/configs")
async def admin_coeos_configs_list():
    return {"configs": list_saved_configs()}


@app.post("/admin/coeos/configs/save")
async def admin_coeos_configs_save(req: ConfigName):
    if not req.name.strip():
        raise HTTPException(400, detail={"error": "bad_name",
            "message": "name required."})
    safe = save_named_config(req.name, coeos_cfg(load_config()))
    return {"ok": True, "name": safe}


@app.post("/admin/coeos/configs/load")
async def admin_coeos_configs_load(req: ConfigName):
    """Load = REPLACE the active coeos config wholesale (not a merge like a
    settings/score-table import) — restoring a saved snapshot means getting
    back exactly what was saved, not layering it on top of whatever is
    currently active."""
    try:
        blob = load_named_config(req.name)
    except FileNotFoundError:
        raise HTTPException(404, detail={"error": "not_found",
            "message": f"no saved config named {req.name!r}."})
    with config_txn() as cfg:
        cfg["coeos"] = blob
    return coeos_cfg(load_config())


@app.delete("/admin/coeos/configs/{name}")
async def admin_coeos_configs_delete(name: str):
    if not delete_named_config(name):
        raise HTTPException(404, detail={"error": "not_found",
            "message": f"no saved config named {name!r}."})
    return {"ok": True}


# ── Admin: providers (keys only — that's the whole setup) ───────────────────

class ProviderUpdate(BaseModel):
    api_key: Optional[str] = None        # non-empty value stores it in the config
    clear_api_key: Optional[bool] = None  # true wipes the stored key
    enabled: Optional[bool] = None
    # L'adresse d'un provider LOCAL appartient au client : elle se saisit ici,
    # elle n'est jamais devinee ni codee en dur.
    api_base: Optional[str] = None



@app.get("/admin/providers")
async def admin_providers_list():
    cfg = load_config()
    return {"data": [redact_provider(cfg, pid) for pid in PROVIDERS]}


@app.put("/admin/providers/{pid}")
async def admin_providers_update(pid: str, req: ProviderUpdate):
    if pid not in PROVIDERS:
        raise HTTPException(404, f"unknown provider {pid!r} (SE has: {sorted(PROVIDERS)})")
    with config_txn() as cfg:
        providers = cfg.setdefault("providers", {})
        cur = providers.setdefault(pid, {})
        if req.clear_api_key:
            cur.pop("api_key", None)
        elif req.api_key is not None and req.api_key.strip():
            cur["api_key"] = req.api_key.strip()
        if req.enabled is not None:
            cur["enabled"] = bool(req.enabled)
        if req.api_base is not None:
            base = req.api_base.strip()
            if base:
                cur["api_base"] = base
            else:
                cur.pop("api_base", None)
    return redact_provider(load_config(), pid)


class ProviderCreate(BaseModel):
    id: str
    label: str = ""
    api_base: str = ""
    api_key: str = ""


@app.post("/admin/providers")
async def admin_providers_create(req: ProviderCreate):
    """Declare un provider. Le client en ajoute autant qu'il veut : c'est SON
    reseau et SES comptes, on ne peut pas les prevoir."""
    pid = (req.id or "").strip().lower()
    if not pid or not pid.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, detail={"error": "bad_id",
            "message": "id invalide (alphanumerique, - et _)."})
    cfg = await run_in_threadpool(load_config)
    if pid in providers_of(cfg):
        raise HTTPException(409, detail={"error": "exists",
            "message": f"le provider {pid!r} existe deja."})
    base = (req.api_base or "").strip()
    if base and not base.startswith(("http://", "https://")):
        raise HTTPException(400, detail={"error": "bad_url",
            "message": "l'URL doit commencer par http:// ou https://."})

    def _write():
        with config_txn() as c:
            provs = c.setdefault("providers", {})
            entry = {"label": (req.label or pid).strip(), "api_base": base}
            if (req.api_key or "").strip():
                entry["api_key"] = req.api_key.strip()
            provs[pid] = entry
    await run_in_threadpool(_write)
    return redact_provider(await run_in_threadpool(load_config), pid)


@app.delete("/admin/providers/{pid}")
async def admin_providers_delete(pid: str):
    """Retire un provider ajoute. Les builtins ne se suppriment pas — on les
    desactive. Les axes qui pointaient dessus deviennent non servables, et
    /health le dira : on ne nettoie pas le routage dans le dos de l'operateur."""
    cfg = await run_in_threadpool(load_config)
    if pid in BUILTIN_PROVIDERS:
        raise HTTPException(400, detail={"error": "builtin",
            "message": f"{pid!r} est fourni d'origine : desactive-le au lieu de le supprimer."})
    if pid not in providers_of(cfg):
        raise HTTPException(404, detail={"error": "unknown_provider",
                                         "message": f"provider inconnu : {pid}"})
    orphans = [a["key"] for a in router.mapping_table(coeos_cfg(cfg))
               if router.provider_of(registry_of(coeos_cfg(cfg)).get(a["model"])) == pid]

    def _write():
        with config_txn() as c:
            provs = c.get("providers")
            if isinstance(provs, dict):
                provs.pop(pid, None)
    await run_in_threadpool(_write)
    return {"ok": True, "removed": pid, "orphaned_axes": sorted(orphans)}


@app.post("/admin/providers/{pid}/test")
async def admin_providers_test(pid: str):
    """Verify the provider is reachable by hitting its /v1/models."""
    cfg = load_config()
    if pid not in providers_of(cfg):
        raise HTTPException(404, f"unknown provider {pid!r}")
    has_key = provider_key(cfg, pid) is not None
    models = await list_upstream_models(cfg, pid)
    if not models:
        return {"ok": False, "models_count": 0,
                "error": "API key not set" if not has_key else
                         "upstream unreachable or empty /models"}
    return {"ok": True, "models_count": len(models), "auth_used": has_key,
            "sample": [m.get("id") for m in models[:10]]}


@app.get("/admin/providers/{pid}/upstream-models")
async def admin_providers_upstream(pid: str):
    cfg = load_config()
    if pid not in providers_of(cfg):
        raise HTTPException(404, f"unknown provider {pid!r}")
    return {"data": await list_upstream_models(cfg, pid)}


@app.get("/admin/models")
async def admin_models():
    """Tout ce que les providers configures publient — la source du picker de
    modeles. Un provider injoignable ne fait pas echouer les autres : il
    remonte son erreur et la liste continue."""
    cfg = await run_in_threadpool(load_config)
    out = []
    for pid in providers_of(cfg):
        if not provider_ready(cfg, pid):
            out.append({"provider": pid, "ready": False, "models": [],
                        "error": "not configured"})
            continue
        try:
            models = await list_upstream_models(cfg, pid)
        except Exception as e:
            out.append({"provider": pid, "ready": True, "models": [],
                        "error": str(e)[:120]})
            continue
        out.append({"provider": pid, "ready": True,
                    "models": sorted({str(m.get("id")) for m in models if m.get("id")}),
                    "error": None if models else "empty /models"})
    return {"providers": out}


# ── Misc ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Santé honnête : `axes_bound` dit ce que le setting DÉCLARE, `axes_servable`
    ce que la box sait réellement servir. Les deux ont divergé du 14 au 17/08/2026
    — 18 axes liés, 0 servable, et un health vert pendant trois jours. Quand plus
    rien n'est servable, on le dit : `ok: false`, statut `degraded`."""
    cfg = load_config()
    c = coeos_cfg(cfg)
    bound = bound_axes(c)
    broken = unservable_axes(c)
    servable = len(bound) - len(broken)
    providers = ready_providers(cfg)
    # Une box neuve sans clé n'est pas EN PANNE, elle attend son installation —
    # c'est le parcours normal du dashboard. Ne pas confondre les deux : `ok`
    # ne tombe que sur un vrai défaut, sinon l'alerte devient du bruit.
    ok = bool(bound) and servable > 0
    body = {"ok": ok, "version": __version__,
            "settings": c.get("name"), "updated": c.get("updated"),
            "axes_bound": len(bound),
            "axes_servable": servable,
            "providers_ready": providers}
    if not ok:
        body["status"] = "degraded"
        body["reason"] = ("aucun axe servable : le registre ne résout pas ces liaisons"
                          if bound else "aucun axe lié dans le setting")
    elif not providers:
        body["status"] = "setup"
        body["reason"] = "aucune clé provider : ajoute-la au dashboard"
    if broken:
        # La liste, pas juste un compte : on doit pouvoir agir sans deviner.
        body["axes_unservable"] = broken
    return body


@app.get("/")
async def index():
    return RedirectResponse("/dashboard")


@app.get("/dashboard")
async def dashboard():
    path = importlib.resources.files("coeos") / "dashboard" / "index.html"
    return FileResponse(str(path), media_type="text/html")


@app.get("/favicon.ico")
async def favicon():
    # Les navigateurs sondent /favicon.ico a la racine meme quand la page
    # declare son <link rel="icon"> : on sert le meme PNG plutot qu'un 404.
    path = importlib.resources.files("coeos") / "dashboard" / "images" / "theseus.png"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(str(path), media_type="image/png")


@app.get("/dashboard/images/{name}")
async def dashboard_image(name: str):
    # Static assets for the dashboard (logo). Filename-only, no path traversal.
    if "/" in name or ".." in name:
        raise HTTPException(404)
    path = importlib.resources.files("coeos") / "dashboard" / "images" / name
    if not path.is_file():
        raise HTTPException(404, "asset not found")
    return FileResponse(str(path))


@app.get("/endpoints")
async def endpoints_page():
    """Copy/paste connection settings for the common client apps."""
    path = importlib.resources.files("coeos") / "dashboard" / "endpoints.html"
    return FileResponse(str(path), media_type="text/html")
