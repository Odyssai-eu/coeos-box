"""Métrologie d'usage (S1.4) + quotas (S1.3).

Chaque appel amont ABOUTI écrit une ligne : (user, axe, modèle, tokens
in/out, coût si l'amont le donne — OpenRouter le donne). C'est le socle de
toute facturation future (S4) et la source de la vue Decisions de Némo.
Les refus (401/429/503) ne comptent pas : le budget mesure ce qui a servi.

Quotas, par compte (admin exempté) :
  - req/min      : fenêtre fixe en mémoire (instance unique en v1)
  - tokens/jour  : somme du jour dans la table usage, cache 15 s
Défauts par env (COEOS_RPM_DEFAULT=60, COEOS_DAY_TOKENS_DEFAULT=0=illimité),
surcharge par compte (colonnes users.rpm_limit / users.day_tokens ; 0 = défaut).
`/v1/me*` n'est jamais limité : un compte à sec doit pouvoir se réparer.
"""

from __future__ import annotations

import os
import time
from contextvars import ContextVar
from datetime import datetime, timezone

from . import accounts

# Décision de routage de la requête en cours (axe + modèle logique), posée par
# les handlers d'app.py juste après resolve_target. "_decider" pour les appels
# de classification, "_direct" pour un bypass du routeur.
current_decision: ContextVar[dict | None] = ContextVar("coeos_decision", default=None)


def _now_parts() -> tuple[str, str]:
    dt = datetime.now(timezone.utc)
    return dt.isoformat(timespec="seconds"), dt.strftime("%Y-%m-%d")


def _ensure_table(c) -> None:
    c.executescript("""
      CREATE TABLE IF NOT EXISTS usage(
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        day TEXT NOT NULL,
        user_id INTEGER NOT NULL DEFAULT 0,
        name TEXT NOT NULL DEFAULT '',
        axis TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0,
        protocol TEXT NOT NULL DEFAULT 'openai'
      );
      CREATE INDEX IF NOT EXISTS usage_user_day ON usage(user_id, day);
    """)


def record(account: dict | None, decision: dict | None, usage: dict | None,
           model: str, protocol: str = "openai") -> None:
    """Écrit une ligne d'usage. Tolérant : jamais d'exception vers l'appelant
    — la métrologie ne casse pas une réponse déjà servie."""
    try:
        u = usage or {}
        tin = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        tout = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        cost = float(u.get("cost") or 0)
        if not (tin or tout or cost):
            return
        d = decision or {}
        axis = d.get("x-coeos-axis") or d.get("axis") or "_direct"
        ts, day = _now_parts()
        acc = account or {}
        with accounts._connect() as c:
            _ensure_table(c)
            c.execute("""INSERT INTO usage(ts, day, user_id, name, axis, model,
                         tokens_in, tokens_out, cost, protocol)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (ts, day, int(acc.get("user_id") or 0), acc.get("name") or "",
                       axis, model, tin, tout, cost, protocol))
    except Exception:
        pass


def last_usage_from_sse(raw: bytes, prev: dict | None) -> dict | None:
    """Repère le dernier bloc `usage` d'un flux SSE relayé en bytes. Appelé
    chunk par chunk ; retourne le dernier usage vu (ou celui d'avant)."""
    if b'"usage"' not in raw:
        return prev
    import json
    found = prev
    for line in raw.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line.startswith("data:") or '"usage"' not in line:
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            u = json.loads(payload).get("usage")
            if isinstance(u, dict):
                found = u
        except Exception:
            continue
    return found


# ── lecture ──────────────────────────────────────────────────────────────────

def summary(user_id: int | None = None, days: int = 30) -> dict:
    where, args = "", []
    if user_id is not None:
        where = "WHERE user_id=?"
        args.append(user_id)
    with accounts._connect() as c:
        _ensure_table(c)
        q = f"""SELECT COUNT(*) n, SUM(tokens_in) tin, SUM(tokens_out) tout,
                       SUM(cost) cost FROM usage {where}
                {"AND" if where else "WHERE"} day >= date('now', ?)"""
        args2 = args + [f"-{int(days)} days"]
        tot = dict(c.execute(q, args2).fetchone())
        by_axis = [dict(r) for r in c.execute(
            f"""SELECT axis, COUNT(*) n, SUM(tokens_in) tin, SUM(tokens_out) tout,
                SUM(cost) cost FROM usage {where}
                {"AND" if where else "WHERE"} day >= date('now', ?)
                GROUP BY axis ORDER BY cost DESC""", args2).fetchall()]
        by_model = [dict(r) for r in c.execute(
            f"""SELECT model, COUNT(*) n, SUM(tokens_in) tin, SUM(tokens_out) tout,
                SUM(cost) cost FROM usage {where}
                {"AND" if where else "WHERE"} day >= date('now', ?)
                GROUP BY model ORDER BY cost DESC""", args2).fetchall()]
    return {"days": days,
            "totals": {"requests": tot["n"] or 0, "tokens_in": tot["tin"] or 0,
                       "tokens_out": tot["tout"] or 0, "cost": round(tot["cost"] or 0, 6)},
            "by_axis": by_axis, "by_model": by_model}


# ── quotas (S1.3) ────────────────────────────────────────────────────────────

_RPM: dict[int, tuple[int, int]] = {}          # user_id -> (minute_epoch, count)
_DAY_CACHE: dict[int, tuple[float, int]] = {}  # user_id -> (checked_at, tokens_today)
_DAY_TTL = 15.0


def _defaults() -> tuple[int, int]:
    return (int(os.environ.get("COEOS_RPM_DEFAULT", "60") or 0),
            int(os.environ.get("COEOS_DAY_TOKENS_DEFAULT", "0") or 0))


def _tokens_today(user_id: int) -> int:
    now = time.monotonic()
    hit = _DAY_CACHE.get(user_id)
    if hit and now - hit[0] < _DAY_TTL:
        return hit[1]
    with accounts._connect() as c:
        _ensure_table(c)
        row = c.execute("""SELECT SUM(tokens_in)+SUM(tokens_out) FROM usage
                           WHERE user_id=? AND day=date('now')""", (user_id,)).fetchone()
    total = int(row[0] or 0)
    _DAY_CACHE[user_id] = (now, total)
    return total


def check_limits(account: dict) -> tuple[dict, dict] | None:
    """None = passe. Sinon (detail_429, headers). Admin exempté."""
    if account.get("admin"):
        return None
    uid = int(account.get("user_id") or 0)
    rpm_def, day_def = _defaults()
    rpm = int(account.get("rpm_limit") or 0) or rpm_def
    day_cap = int(account.get("day_tokens") or 0) or day_def

    if rpm > 0:
        minute = int(time.time() // 60)
        cur_min, count = _RPM.get(uid, (minute, 0))
        if cur_min != minute:
            cur_min, count = minute, 0
        if count >= rpm:
            retry = 60 - int(time.time() % 60) or 1
            return ({"error": "rate_limited",
                     "message": f"Rate limit: {rpm} requests/min. Retry in {retry}s."},
                    {"Retry-After": str(retry), "X-RateLimit-Limit": str(rpm),
                     "X-RateLimit-Remaining": "0"})
        _RPM[uid] = (cur_min, count + 1)

    if day_cap > 0 and _tokens_today(uid) >= day_cap:
        return ({"error": "daily_budget_exhausted",
                 "message": f"Daily token budget ({day_cap}) exhausted. "
                            "Resets at midnight UTC."},
                {"Retry-After": "3600", "X-RateLimit-Limit": str(rpm or rpm_def),
                 "X-RateLimit-Remaining": "0"})
    return None


def rate_headers(account: dict) -> dict:
    if account.get("admin"):
        return {}
    rpm_def, _ = _defaults()
    rpm = int(account.get("rpm_limit") or 0) or rpm_def
    if rpm <= 0:
        return {}
    minute = int(time.time() // 60)
    cur_min, count = _RPM.get(int(account.get("user_id") or 0), (minute, 0))
    left = rpm - count if cur_min == minute else rpm
    return {"X-RateLimit-Limit": str(rpm), "X-RateLimit-Remaining": str(max(0, left))}
