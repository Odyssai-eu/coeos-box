"""CoeOS routing core — classify a request into a skill axis, resolve the
axis's bound model to a (provider, upstream id) pair.

Ported from OdyssAI-X scripts/api.py:6986-7269 (`coeos_resolve` and friends),
with the cluster servability machinery deleted (cloud-only: every published
model is always "hot"). OpenRouter is the only provider: resolution is a plain
registry lookup — no priority, no per-axis pin, no fallback table.

The taxonomy (axes) and bindings come ENTIRELY from the imported TMB Settings
— no hard-coded categories or rules.
"""

from __future__ import annotations

import os
import re
import sys

from fastapi import HTTPException

from . import proxy
from . import accounts
from .providers import PROVIDER_ID, PROVIDERS, provider_ready

_OR_FIELD = PROVIDERS[PROVIDER_ID]["registry_field"]  # "or"


# G-9 / #120 — profil d'installation + classe de la requête.
# D25 règle 5 : pas de classe posée par le client = "confidential"
# (fail-closed, jamais "open" implicite). Le profil est figé à
# l'installation (D27) et vit dans la variable d'env `COEOS_PROFILE`
# posée par le compose (cf. le docker-compose du déploiement).

def coeos_profile() -> str:
    """`vps` (OVH, par défaut) ou `local` (on-prem). Posé au deploy.
    Jamais un réglage utilisateur (D27)."""
    p = (os.environ.get("COEOS_PROFILE") or "vps").strip().lower()
    return p if p in ("vps", "local") else "vps"


def resolve_request_class(headers) -> str:
    """Lit `X-CoeOS-Class` (lowercase `x-coeos-class`). Absent ou
    invalide → "confidential" (D25 règle 5, fail-closed)."""
    try:
        v = (headers.get("x-coeos-class") or "").strip().lower()
    except Exception:
        return "confidential"
    return v if v in ("confidential", "open") else "confidential"

# Une entrée de registre veut dire « l'id upstream du modèle », et l'histoire
# l'orthographie de deux façons : `or` (ce moteur, hérité de SE) et `endpoint`
# (OdyssAI-X, ce qu'écrit la console). Un registre au format console était donc
# muet ici : 18 axes sur 18 en 503 le 14/08/2026, trois jours durant. On lit les
# deux, `or` prioritaire.
_UPSTREAM_FIELDS = (_OR_FIELD, "endpoint")


def provider_of(entry: object) -> str:
    """Le provider qui sert cette entrée de registre.

    `openrouter` par défaut — c'était le seul jusqu'au 2026-08-18. Une entrée
    peut désigner un provider local (`{"provider": "odyssai", "endpoint":
    "qwen3-5-122b-a10b"}`) : c'est ce qui permet à un setting de router vers
    les machines du client sans que rien ne sorte de chez lui."""
    if isinstance(entry, dict):
        pid = str(entry.get("provider") or "").strip().lower()
        if pid:
            # Pas de validation contre une liste ici : les providers sont
            # déclarés par l'opérateur, et valider contre les seuls builtins
            # ferait retomber les siens sur OpenRouter EN SILENCE. Un provider
            # inconnu échoue à la résolution et /health le dit.
            return pid
    return PROVIDER_ID


def upstream_of(entry: object) -> str:
    """L'id upstream d'une entrée de registre, quelle que soit son orthographe."""
    if not isinstance(entry, dict):
        return ""
    for field in _UPSTREAM_FIELDS:
        value = (entry.get(field) or "").strip() if isinstance(entry.get(field), str) else ""
        if value:
            return value
    return ""


COEOS_MODEL_ID = "coeos"      # canonical id, matched case-insensitively
COEOS_DISPLAY_ID = "CoeOS"    # public id emitted in /v1/models

# Per-(logical, axis, provider) decision counter — operator visibility.
decisions: dict[tuple, int] = {}


def coeos_cfg(cfg: dict) -> dict:
    return cfg.get("coeos") or {}


# ── Settings par compte (W1) ────────────────────────────────────────────────
# La box tient UN setting actif (`cfg["coeos"]`) et, optionnellement, un
# CATALOGUE de settings publiés (`cfg["settings"]` : full / open / eco). Le
# token d'un user porte son setting ; sans catalogue, ou sans setting sur le
# compte, tout le monde route sur le setting actif — le cas solo ne paie rien
# pour une mécanique multi-user qu'il n'utilise pas.

def catalog_of(cfg: dict) -> dict:
    cat = cfg.get("settings")
    return {k: v for k, v in cat.items() if isinstance(v, dict)} if isinstance(cat, dict) else {}


def setting_names(cfg: dict) -> list[str]:
    return sorted(catalog_of(cfg))


def coeos_cfg_for(cfg: dict, account: dict | None = None) -> dict:
    """Le setting qui s'applique à CE compte.

    Ordre : le setting du compte, sinon celui de l'org (`cfg['setting']`),
    sinon le setting actif. Un nom qui ne correspond à rien au catalogue ne
    fait pas tomber la requête sur un autre routage en douce — on retombe sur
    l'actif, et `/v1/me` dit ce qui s'applique vraiment."""
    catalog = catalog_of(cfg)
    if not catalog:
        return coeos_cfg(cfg)
    want = ""
    if isinstance(account, dict):
        want = str(account.get("setting") or "").strip()
    want = want or str(cfg.get("setting") or "").strip()
    picked = catalog.get(want) if want else None
    return picked if isinstance(picked, dict) else coeos_cfg(cfg)


def setting_name_for(cfg: dict, account: dict | None = None) -> str:
    """Le NOM du setting appliqué — pour que le user puisse le vérifier."""
    catalog = catalog_of(cfg)
    if not catalog:
        return str(coeos_cfg(cfg).get("name") or "")
    want = ""
    if isinstance(account, dict):
        want = str(account.get("setting") or "").strip()
    want = want or str(cfg.get("setting") or "").strip()
    return want if want in catalog else str(coeos_cfg(cfg).get("name") or "")


def axes_of(c: dict) -> list[dict]:
    """Les axes du setting. Chacun = {key, label, model, description?, bench?}.

    L'opérateur les édite directement (`PUT /admin/mapping/axis/{key}`) : un
    setting qui ne s'édite pas n'existe pas pour un client. Il n'y a plus de
    mode standard/expert ni de couche d'override — le setting EST la vérité."""
    axes = c.get("axes")
    return [a for a in axes if isinstance(a, dict) and a.get("key")] if isinstance(axes, list) else []


def bound_axes(c: dict) -> list[dict]:
    """Axes with a non-empty model binding. Unbound axes (a declared gap in
    the settings, e.g. swift with no strong model benched yet) stay visible in
    the config but are excluded from the routing menu — the decider can only
    pick an axis it can actually serve."""
    return [a for a in axes_of(c)
            if (a.get("model") or "").strip()
            and str(a["model"]).strip().lower() != COEOS_MODEL_ID]


def registry_of(c: dict) -> dict:
    """Logical model name → {name, or, note?}. The registry is the only place
    provider-native ids live; axes bind portable logical names."""
    reg = c.get("models")
    return reg if isinstance(reg, dict) else {}


def unservable_axes(c: dict) -> list[str]:
    """Axes liés à un modèle que le registre ne sait PAS résoudre en id upstream.

    Une liaison déclarée ne vaut rien si le logique ne se résout pas : c'est
    exactement ce qui est arrivé le 14/08/2026 — 18 axes « liés » et 100 % des
    requêtes en 503, pendant que /health affichait vert. Un axe sans liaison du
    tout n'est pas ici : c'est un trou assumé du setting, pas une panne."""
    registry = registry_of(c)
    bad = []
    for a in bound_axes(c):
        logical = str(a.get("model") or "").strip()
        entry = registry.get(logical)
        # Pas d'entrée = la liaison EST l'id upstream (compat settings écrits à
        # la main). Entrée présente mais sans id = panne silencieuse.
        if entry is not None and not upstream_of(entry):
            bad.append(str(a.get("key")))
    return bad


def _score_table_lookup(table: dict) -> dict[str, str]:
    """row-identity (name/alias/or_id/served_model, lowercased) -> row name."""
    out: dict[str, str] = {}
    for name, m in (table.get("models") or {}).items():
        if not isinstance(m, dict):
            continue
        for key in (name, m.get("alias"), m.get("or_id"), m.get("served_model")):
            if key:
                out[str(key).strip().lower()] = name
    return out


def axis_candidates(table: dict, registry: dict, axis_key: str) -> list[dict]:
    """Modèles candidats pour UN axe, restreints à ce que le registre de
    l'opérateur sait servir (son « armée »), classés meilleur d'abord :
    score décroissant, puis le moins cher (coût inconnu en dernier).

    Les lignes de rôle `reference` (nos étalons de bench) ne sont jamais
    candidates. C'est la brique commune du résolveur (qui prend le premier) et
    de la table de mapping du dashboard (qui les montre tous)."""
    lut = _score_table_lookup(table)
    models = table.get("models") or {}
    out: list[dict] = []
    for logical, entry in registry.items():
        # Le registre est joint à la table par sa CLÉ, sinon par l'id upstream
        # canonique de l'entrée — cf. resolve_score_table.
        row = lut.get(str(logical).strip().lower())
        if not row and upstream_of(entry):
            row = lut.get(upstream_of(entry).lower())
        if not row:
            continue
        m = models.get(row) or {}
        if m.get("role") == "reference":
            continue
        ax = (m.get("axes") or {}).get(axis_key) or {}
        score = ax.get("score")
        if score is None:
            continue
        out.append({
            "logical": logical,
            "name": (entry.get("name") if isinstance(entry, dict) else None) or logical,
            "score": score,
            "verified": bool(ax.get("verified")),
            "n": ax.get("n"),
            "cost": m.get("cost_per_test"),
            "cost_estimated": bool(m.get("cost_estimated")),
            "tps": m.get("tps_median"),
            "kind": m.get("kind"),
        })
    out.sort(key=lambda r: (-r["score"], r["cost"] is None,
                            r["cost"] if r["cost"] is not None else 0.0, r["logical"]))
    return out


def mapping_table(c: dict) -> list[dict]:
    """Table de mapping du dashboard : par axe, la liaison effective + les
    candidats classés. Sans score_table embarqué (settings livré sans
    provenance), les candidats sont vides — l'axe reste affiché avec sa
    liaison, on ne cache jamais le routage à l'opérateur de la box."""
    table = c.get("score_table") if isinstance(c.get("score_table"), dict) else {}
    registry = registry_of(c)
    rows = []
    for a in axes_of(c):
        key = str(a.get("key"))
        model = (a.get("model") or "").strip()
        entry = registry.get(model) if isinstance(registry.get(model), dict) else {}
        rows.append({
            "key": key,
            "label": a.get("label") or key,
            "description": a.get("description") or "",
            "model": model,
            "model_name": (entry or {}).get("name") or model,
            "bound": bool(model),
            "provider": provider_of(entry) if model else "",
            # Ce que le setting recommandait, s'il a été remplacé — pour
            # revenir en arrière sans avoir à le chercher.
            "suggested": (a.get("suggested") or "").strip(),
            "candidates": axis_candidates(table, registry, key) if table else [],
        })
    return rows


def resolve_score_table(table: dict, registry: dict) -> list[dict]:
    """One-time resolve: for each axis in the score-table's taxonomy, pick the
    best-scoring model restricted to what THIS operator's registry can serve
    (their 'army' — no separate fleet declaration). Reference-role rows (our
    benchmark etalons) are never picked. Tie on score -> cheaper wins (unknown
    cost sorts last). Unlike OdyssAI-X's live resolver, SE resolves ONCE at
    import time and writes a normal axes=[{key,label,model,description}] list
    — the router stays the simple pre-decided-binding lookup it already is;
    only the IMPORT source changed from a pre-baked settings file to the raw
    score table. Re-import (or a future 're-resolve') to pick up registry
    changes."""
    axes_meta = table.get("axes") or {}
    out = []
    for axis_key, meta in axes_meta.items():
        # Le classement (score, puis coût) vit dans axis_candidates — le
        # résolveur prend simplement le premier. Une seule règle, deux usages.
        cands = axis_candidates(table, registry, axis_key)
        out.append({"key": axis_key, "label": (meta or {}).get("label", axis_key),
                    "description": (meta or {}).get("description", ""),
                    "model": cands[0]["logical"] if cands else ""})
    return out


def decider_spec(c: dict) -> dict | None:
    """The decider's own setting: {name, or} — its display name and OpenRouter
    id, independent of the axis registry.

    Back-compat: a legacy `decider_model` string is looked up in the registry
    (or treated as a raw upstream id)."""
    d = c.get("decider")
    if isinstance(d, dict) and upstream_of(d):
        return d
    legacy = (c.get("decider_model") or "").strip()
    if not legacy:
        return None
    entry = registry_of(c).get(legacy)
    if isinstance(entry, dict):
        return {"name": entry.get("name") or legacy, _OR_FIELD: upstream_of(entry)}
    return {"name": legacy, _OR_FIELD: legacy}


def resolve_decider(cfg: dict, c: dict) -> tuple[str, str] | None:
    """Spec du décideur → (provider, id upstream). Le décideur peut être local :
    c'est même souhaitable — il tourne à chaque requête, et le garder chez le
    client évite d'envoyer le prompt dehors juste pour le classer."""
    spec = decider_spec(c)
    if not spec:
        return None
    pid = provider_of(spec)
    if not provider_ready(cfg, pid):
        return None
    upstream = upstream_of(spec)
    return (pid, upstream) if upstream else None


def resolve_logical(cfg: dict, logical: str) -> tuple[str, str] | None:
    """Logical name → (provider_id, upstream_model_id) on OpenRouter. Plain
    registry lookup — no priority, no fallback. A logical with no registry
    entry is treated as the upstream id itself (back-compat with hand-written
    settings)."""
    logical = (logical or "").strip()
    if not logical:
        return None
    entry = registry_of(coeos_cfg_for(cfg, accounts.current_account.get())).get(logical)
    if entry is None:
        # legacy : la liaison EST l'id upstream, servi par le provider par défaut
        return (PROVIDER_ID, logical) if provider_ready(cfg) else None
    pid = provider_of(entry)
    if not provider_ready(cfg, pid):
        return None
    upstream = upstream_of(entry)
    return (pid, upstream) if upstream else None


def header_axis(headers, keys: list[str]) -> str | None:
    """Explicit axis from the agent. `x-coeos-axis` wins; `x-coeos-category`
    is a back-compat alias. Returned only if it's a CONFIGURED bound axis."""
    for h in ("x-coeos-axis", "x-coeos-category"):
        v = (headers.get(h) or "").strip().lower()
        if v in keys:
            return v
    return None


def parse_axis(text: str, keys: list[str]) -> str | None:
    """Extract the chosen axis key from the decider's (possibly multi-token,
    reasoned) reply. Priority: an explicit final `AXIS: <key>` line → a bare
    reply whose first token is a key → the LAST word-bounded key mentioned
    anywhere. None if no configured key is found. (api.py:7104)"""
    if not text or not text.strip():
        return None
    low = text.lower()
    keyset = {k.lower() for k in keys}
    for m in reversed(list(re.finditer(r"axis\s*[:=]\s*[`\"']?([a-z0-9_]+)", low))):
        if m.group(1) in keyset:
            return m.group(1)
    first = low.strip().split()[0].strip('`"\',.') if low.strip() else ""
    if first in keyset:
        return first
    best, best_pos = None, -1
    for k in keys:
        for m in re.finditer(r"\b" + re.escape(k.lower()) + r"\b", low):
            if m.start() > best_pos:
                best, best_pos = k, m.start()
    return best


def _last_user_text(messages: list[dict]) -> str:
    if not messages:
        return ""
    c = messages[-1].get("content")
    if isinstance(c, str):
        return c[:8000]
    if isinstance(c, list):  # multimodal — keep the text parts
        return " ".join(p.get("text", "") for p in c
                        if isinstance(p, dict) and p.get("type") == "text")[:8000]
    return ""


async def llm_classify(cfg: dict, c: dict, axes: list[dict], messages: list[dict], class_requested: str = "open") -> str | None:
    """Ask the decider to UNDERSTAND the request and classify it into ONE
    configured axis. The taxonomy (keys + labels + per-axis frontier notes) is
    passed from config — nothing hard-coded. The decider is a reasoning
    router, not a tag matcher: full last message + axis descriptions + room to
    think, then a final `AXIS:` line. (api.py:7131)

    G-9 / #120 — le décideur est CONFINÉ À LA CLASSE de la requête.
    Sur une requête `confidential`, le décideur doit tourner en local
    (jamais hors machine, D25) ou sous DPA (jamais sans). Le setting
    CoeOS porte un champ `decider.class` (V1: "local" par défaut) ;
    tout autre config est refusée. Sur `open`, on garde le chemin
    historique (décideur cloud possible)."""
    resolved = resolve_decider(cfg, c)
    if resolved is None:
        return None  # decider unresolvable → caller falls back to default axis
    pid, upstream = resolved
    if class_requested == "confidential":
        # V1 : on refuse tout décideur cloud par défaut. L'opérateur pose
        # explicitement un décideur local dans la config (champ à venir
        # dans M3-10 / #95 + ticket dédié). En attendant, on retombe sur
        # default_axis plutôt que de classer hors classe.
        decider_class = (c.get("decider") or {}).get("class") or "cloud"
        if decider_class != "local":
            return None
    if resolved is None:
        return None  # decider unresolvable → caller falls back to default axis
    pid, upstream = resolved

    def _axis_line(ax: dict) -> str:
        line = f"- {ax['key']}: {ax.get('label', ax['key'])}"
        desc = ax.get("description") or ax.get("hint")
        if desc:
            line += f" — {desc}"
        return line

    keys = [ax["key"] for ax in axes]
    menu = "\n".join(_axis_line(ax) for ax in axes)
    prompt = (
        "You are CoeOS's routing classifier. UNDERSTAND the request — its true "
        "intent and the nature of the deliverable (target language, domain) — then "
        "pick the SINGLE best-matching skill axis from the menu. Prefer the MOST "
        "SPECIFIC axis that applies; choose a generic bucket (e.g. code_general) "
        "ONLY when no specific axis fits. Honour each axis's frontier notes "
        "(the '— …' clause, including its 'not here if …' guidance).\n\n"
        f"Axes:\n{menu}\n\n"
        f"Request:\n{_last_user_text(messages)}\n\n"
        "Reason in at most two short sentences, then end your reply with a final "
        f"line exactly: `AXIS: <key>` where <key> is one of: {', '.join(keys)}")
    try:
        buf = await proxy.unary_upstream_text(
            cfg, pid, upstream, [{"role": "user", "content": prompt}], max_tokens=600)
    except Exception as e:
        sys.stderr.write(f"[coeos-se] decider error: {e}\n")
        return None
    return parse_axis(buf, keys)


async def coeos_resolve(cfg: dict, headers, body: dict) -> dict:
    """Resolve `coeos` → routing decision. Classify the request into one
    CONFIGURED bound axis (explicit `x-coeos-axis` header → decider LLM →
    `default_axis`), then resolve that axis's binding via option 1.

    No silent fallback to a different model: if the recommended binding can't
    be resolved on any ready provider, surface a clear 503 telling the user
    which key to add or which registry id to fill. (api.py:7207)"""
    # Le setting qui s applique au demandeur (W1) : son token le porte.
    c = coeos_cfg_for(cfg, accounts.current_account.get())
    # ABSENT n'est pas FAUX : un setting publié par un master ancien ne porte
    # pas `enabled`, et le routeur tombait alors en « disabled » sans que rien
    # ne l'annonce. Seul un `false` explicite désactive.
    if c.get("enabled") is False:
        raise HTTPException(status_code=400, detail={
            "error": "coeos_disabled",
            "message": "CoeOS router is disabled. Enable it in the dashboard "
                       "or PUT /admin/coeos {\"enabled\": true}."})
    axes = bound_axes(c)
    if not axes:
        raise HTTPException(status_code=503, detail={
            "error": "coeos_no_axes",
            "message": "No skill axes are bound. Import a TMB Settings file "
                       "(dashboard → Import, or PUT /admin/coeos)."})
    keys = [ax["key"] for ax in axes]
    default_axis = c.get("default_axis")
    if default_axis not in keys:
        default_axis = keys[0]

    # Classify: explicit header → decider LLM → default. Le
    # decideur est confine a la classe (G-9 / #120) ; on la
    # passe pour que `llm_classify` puisse refuser un decideur
    # cloud sur une requete `confidential` (D25 regle 5).
    klass = resolve_request_class(headers)
    axis = header_axis(headers, keys)
    if not axis:
        axis = await llm_classify(cfg, c, axes, body.get("messages") or [], class_requested=klass)
    if axis not in keys:
        axis = default_axis

    ax = next(a for a in axes if a["key"] == axis)
    logical = str(ax["model"]).strip()
    resolved = resolve_logical(cfg, logical)
    if resolved is None:
        entry = registry_of(c).get(logical) or {}
        display = entry.get("name") or logical
        raise HTTPException(status_code=503, detail={
            "error": "coeos_unresolvable",
            "axis": axis,
            "recommended": display,
            "message": f"{display} — can't be served. Add your OpenRouter API key, "
                       "or fill this model's id in the registry. CoeOS does not "
                       "silently route to a different model."})
    pid, upstream = resolved

    k = (logical, axis, pid)
    decisions[k] = decisions.get(k, 0) + 1
    # G-9 / #120 — verdict de routage exposé au client. La classe
    # est demandée par le client via `X-CoeOS-Class` (cf.
    # `resolve_request_class()` plus bas) ; le profil d'install
    # est figé par `COEOS_PROFILE` (jamais modifiable par user,
    # D27). Le décideur est confiné à la classe de la requête
    # (cf. `llm_classify`) : si `class=confidential` et que le
    # décideur résolu sort de la classe, on refuse en amont.
    klass = resolve_request_class(headers)
    return {"provider": pid, "upstream": upstream, "axis": axis, "logical": logical,
            "thinking": ax.get("thinking"),
            "x_coeos_class": klass,
            "x_coeos_profile": coeos_profile(),
            "x_coeos_decider_used": "1"}  # G-9 : la résolution a passé par le décideur LLM
