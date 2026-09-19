"""Comptes et clés API du SaaS CoeOS — S1.1.

SQLite (stdlib, zéro dépendance), une base à côté de la config
(`COEOS_AUTH_DB`, défaut: même dossier que COEOS_CONFIG).

Modèle:
  users : name unique, mode 'byok'|'platform' (JAMAIS de repli silencieux de
          l'un vers l'autre — décision #5), balance inerte jusqu'à S4,
          admin, disabled.
  keys  : la clé complète n'est JAMAIS stockée — sha256 seulement, plus un
          préfixe d'identification affichable. Révocation = horodatage.

Amorçage: tant qu'AUCUNE clé active n'existe (et pas de COEOS_API_KEY legacy),
l'instance est ouverte — mode localhost/dev, un avertissement au boot. Dès la
première clé émise, l'auth est exigée sur /v1/* et /admin/*.

CLI (opérateur):
  python -m coeos.accounts create-user NAME [--admin] [--mode byok|platform]
  python -m coeos.accounts issue-key USER [--name LABEL]
  python -m coeos.accounts revoke PREFIX
  python -m coeos.accounts list
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

KEY_PREFIX = "ck_"          # coeos key
PREFIX_LEN = 12             # affichable: identifie sans révéler

# Compte de la requête en cours, posé par le middleware (S1.2). Les fonctions
# de résolution de clé provider (providers.provider_key) le consultent pour
# servir la clé DU compte — jamais celle de l'opérateur pour un compte byok.
current_account: ContextVar[dict | None] = ContextVar("coeos_account", default=None)


def db_path() -> Path:
    p = os.environ.get("COEOS_AUTH_DB")
    if p:
        return Path(p)
    cfg = os.environ.get("COEOS_CONFIG", "coeos-config.json")
    return Path(cfg).resolve().parent / "coeos-auth.db"


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
      CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        mode TEXT NOT NULL DEFAULT 'byok' CHECK(mode IN ('byok','platform')),
        balance REAL NOT NULL DEFAULT 0,
        admin INTEGER NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS keys(
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        prefix TEXT NOT NULL,
        hash TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL,
        revoked TEXT,
        secret_enc BLOB,
        kind TEXT NOT NULL DEFAULT 'inference',
        disabled INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS meta(
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS keys_hash ON keys(hash);
      CREATE TABLE IF NOT EXISTS provider_keys(
        id INTEGER PRIMARY KEY,
        scope TEXT NOT NULL CHECK(scope IN ('user','platform')),
        owner_id INTEGER NOT NULL DEFAULT 0,
        provider TEXT NOT NULL,
        key_enc BLOB NOT NULL,
        created TEXT NOT NULL,
        updated TEXT NOT NULL,
        UNIQUE(scope, owner_id, provider)
      );
    """)
    # Migration S1.3 : limites par compte (0 = defaut global / illimite).
    cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if "rpm_limit" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN rpm_limit INTEGER NOT NULL DEFAULT 0")
    kcols = {r[1] for r in c.execute("PRAGMA table_info(keys)").fetchall()}
    if "secret_enc" not in kcols:
        c.execute("ALTER TABLE keys ADD COLUMN secret_enc BLOB")
    if "kind" not in kcols:
        c.execute("ALTER TABLE keys ADD COLUMN kind TEXT NOT NULL DEFAULT 'inference'")
    if "disabled" not in kcols:
        c.execute("ALTER TABLE keys ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
    if "day_tokens" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN day_tokens INTEGER NOT NULL DEFAULT 0")
    # Migration W1 : setting de sourcing par user ('' = celui de l'org).
    # C'est le troisieme terme de la resolution `token -> cles + setting + quotas` :
    # un dev en `full`, un stagiaire en `eco`, sans changer de box ni de cle.
    if "setting" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN setting TEXT NOT NULL DEFAULT ''")
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ── opérations ───────────────────────────────────────────────────────────────

def create_user(name: str, admin: bool = False, mode: str = "byok") -> dict:
    with _connect() as c:
        c.execute("INSERT INTO users(name, mode, admin, created) VALUES(?,?,?,?)",
                  (name.strip(), mode, int(admin), _now()))
        row = c.execute("SELECT * FROM users WHERE name=?", (name.strip(),)).fetchone()
        return dict(row)


def issue_key(user_name: str, label: str = "", kind: str = "inference") -> str:
    """Retourne la clé COMPLÈTE. Elle est AUSSI stockée chiffrée (Fernet, vault
    de la box) pour pouvoir la recopier plus tard depuis le dashboard admin —
    le hash sert à l'authentification, le chiffré à la relecture operateur.
    C'est un choix produit (Sophie 2026-08-20) : l'admin de SA box peut relire
    les clés qu'il a émises. Déchiffrable seulement avec la clé maître locale."""
    key = KEY_PREFIX + secrets.token_hex(20)
    enc = _fernet().encrypt(key.encode())
    with _connect() as c:
        u = c.execute("SELECT id FROM users WHERE name=? AND disabled=0",
                      (user_name.strip(),)).fetchone()
        if not u:
            raise SystemExit(f"user inconnu ou desactive: {user_name}")
        c.execute("INSERT INTO keys(user_id, prefix, hash, name, created, secret_enc, kind) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (u["id"], key[:PREFIX_LEN], _hash(key), label, _now(), enc, kind))
    return key


def revoke_key(prefix: str) -> int:
    with _connect() as c:
        cur = c.execute("UPDATE keys SET revoked=? WHERE prefix=? AND revoked IS NULL",
                        (_now(), prefix.strip()))
        return cur.rowcount


def ensure_user(name: str = "default", admin: bool = False) -> int:
    """Renvoie l'id du user, le créant au besoin. Le dashboard émet des clés
    CLIENT (non-admin, typées inference/settings) sous ce user ; l'accès admin
    au dashboard passe par une clé admin émise en CLI, ou le mode ouvert."""
    with _connect() as c:
        row = c.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        c.execute("INSERT INTO users(name, mode, admin, created) VALUES(?,?,?,?)",
                  (name, "byok", int(admin), _now()))
        return c.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()["id"]


def issue_key_for(user_name: str = "default", label: str = "", kind: str = "inference") -> str:
    """Émet une clé, en garantissant le user (dashboard)."""
    ensure_user(user_name)
    return issue_key(user_name, label, kind)


def list_keys(kind: str | None = None) -> list[dict]:
    """Clés non révoquées (actives ou suspendues) avec leur secret en clair —
    pour le dashboard admin. `kind` filtre inference/settings. Le secret vient
    du chiffré Fernet ; une clé pré-2026-08-20 (hash seul) le rend en None."""
    out = []
    q = """SELECT k.prefix, k.name, k.created, k.secret_enc, k.kind, k.disabled,
                  u.name AS user
           FROM keys k JOIN users u ON u.id = k.user_id
           WHERE k.revoked IS NULL"""
    args: tuple = ()
    if kind:
        q += " AND k.kind=?"; args = (kind,)
    q += " ORDER BY k.id DESC"
    with _connect() as c:
        rows = c.execute(q, args).fetchall()
    for r in rows:
        secret = None
        if r["secret_enc"]:
            try:
                secret = _fernet().decrypt(r["secret_enc"]).decode()
            except Exception:
                secret = None
        out.append({"prefix": r["prefix"], "name": r["name"] or "",
                    "created": r["created"], "user": r["user"], "kind": r["kind"],
                    "active": not r["disabled"], "secret": secret})
    return out


def set_key_active(prefix: str, active: bool) -> int:
    """Suspend (active=False) ou réactive (True) une clé, sans la supprimer.
    Le cœur de l'abonnement : impayé -> suspend, paiement -> réactive."""
    with _connect() as c:
        cur = c.execute("UPDATE keys SET disabled=? WHERE prefix=? AND revoked IS NULL",
                        (0 if active else 1, prefix.strip()))
        return cur.rowcount


# ── toggle d'auth client (Settings > API Keys) ───────────────────────────────
# meta['auth_enabled'] : '1' force l'auth /v1, '0' l'ouvre, absent = auto (auth
# dès qu'une clé active existe). Ne touche JAMAIS /admin ni /dashboard : le
# back-office reste protégé même toggle OFF (sinon l'éteindre sur une box
# publique ouvrirait la gestion des clés à Internet).
def auth_enabled() -> bool | None:
    if not db_path().exists():
        return None
    with _connect() as c:
        row = c.execute("SELECT v FROM meta WHERE k='auth_enabled'").fetchone()
    if row is None:
        return None
    return row["v"] == "1"


def set_auth_enabled(on: bool) -> None:
    with _connect() as c:
        c.execute("INSERT INTO meta(k, v) VALUES('auth_enabled', ?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("1" if on else "0",))


def client_enforced() -> bool:
    """Alias de enforced() — le toggle est désormais l'interrupteur maître
    unique (plus de gate séparé /v1 vs admin)."""
    return enforced()


# ── paywall des settings (ce que Sophie vend) ────────────────────────────────
# meta['settings_auth'] : '1' exige une clé kind='settings' sur /settings/* ,
# '0' ou absent = settings PUBLICS (défaut — ne casse aucun pull existant).
# Indépendant du toggle /v1 : OVH reste endpoint d'inférence pendant que les
# settings deviennent la ressource payante.
def settings_auth_enabled() -> bool:
    if not db_path().exists():
        return False
    with _connect() as c:
        row = c.execute("SELECT v FROM meta WHERE k='settings_auth'").fetchone()
    return bool(row) and row["v"] == "1"


def set_settings_auth(on: bool) -> None:
    with _connect() as c:
        c.execute("INSERT INTO meta(k, v) VALUES('settings_auth', ?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("1" if on else "0",))


def resolve(key: str) -> dict | None:
    """Clé → compte, ou None. Le routeur lit `mode` d'ici (décision #4)."""
    if not key or not key.startswith(KEY_PREFIX):
        return None
    with _connect() as c:
        row = c.execute("""
          SELECT u.id AS user_id, u.name, u.mode, u.admin, u.balance,
                 u.rpm_limit, u.day_tokens, u.setting, k.kind AS key_kind
          FROM keys k JOIN users u ON u.id = k.user_id
          WHERE k.hash=? AND k.revoked IS NULL AND k.disabled=0 AND u.disabled=0
        """, (_hash(key),)).fetchone()
        return dict(row) if row else None


def enforced() -> bool:
    """L'auth est-elle exigée ? Le toggle (meta['auth_enabled'], Settings >
    API Keys) est l'interrupteur MAÎTRE : OFF ouvre toute la box (/v1 + admin +
    dashboard), ON l'exige. Absent = auto : auth dès qu'une clé active existe.
    OFF sur une box publique ouvre AUSSI le dashboard et la relecture des clés —
    à réserver à un réseau de confiance (l'UI le dit)."""
    if (os.environ.get("COEOS_API_KEY") or "").strip():
        return True
    flag = auth_enabled()
    if flag is not None:
        return flag
    if not db_path().exists():
        return False
    with _connect() as c:
        n = c.execute("SELECT COUNT(*) FROM keys WHERE revoked IS NULL").fetchone()[0]
        return n > 0


def list_all() -> list[dict]:
    with _connect() as c:
        rows = c.execute("""
          SELECT u.name, u.mode, u.admin, u.disabled, u.setting, k.prefix,
                 k.name AS key_name, k.created, k.revoked
          FROM users u LEFT JOIN keys k ON k.user_id = u.id
          ORDER BY u.name, k.created
        """).fetchall()
        return [dict(r) for r in rows]



def set_setting(name: str, setting: str) -> dict:
    """Assigne un setting de sourcing à un user ('' = celui de l'org).

    Le nom n'est pas validé contre le catalogue ici : un admin peut préparer
    l'assignation avant de publier le setting. La résolution retombe sur
    l'actif tant que le nom ne correspond à rien, et /v1/me le montre."""
    with _connect() as c:
        cur = c.execute("UPDATE users SET setting=? WHERE name=?",
                        (setting.strip(), name.strip()))
        if not cur.rowcount:
            raise SystemExit(f"compte inconnu : {name}")
    return {"name": name.strip(), "setting": setting.strip()}


# ── Coffre des clés provider (S1.2) ─────────────────────────────────────────
# Chiffrement au repos (Fernet). La clé maître vit HORS de la base :
# env COEOS_MASTER_KEY, sinon un fichier 0600 à côté de la base — généré au
# premier besoin. Perdre la clé maître = re-saisir les clés provider, rien
# d'autre : aucun secret n'est dérivable de la base seule.

def _master_key() -> bytes:
    env = (os.environ.get("COEOS_MASTER_KEY") or "").strip()
    if env:
        return env.encode()
    path = db_path().parent / "coeos-master.key"
    if not path.exists():
        from cryptography.fernet import Fernet
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Fernet.generate_key())
        path.chmod(0o600)
    return path.read_bytes().strip()


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(_master_key())


def set_provider_key(scope: str, owner_id: int, provider: str, api_key: str) -> None:
    enc = _fernet().encrypt(api_key.strip().encode())
    with _connect() as c:
        c.execute("""
          INSERT INTO provider_keys(scope, owner_id, provider, key_enc, created, updated)
          VALUES(?,?,?,?,?,?)
          ON CONFLICT(scope, owner_id, provider)
          DO UPDATE SET key_enc=excluded.key_enc, updated=excluded.updated
        """, (scope, owner_id, provider, enc, _now(), _now()))


def get_provider_key(scope: str, owner_id: int, provider: str) -> str | None:
    if not db_path().exists():
        return None
    with _connect() as c:
        row = c.execute("SELECT key_enc FROM provider_keys WHERE scope=? AND owner_id=? AND provider=?",
                        (scope, owner_id, provider)).fetchone()
    if not row:
        return None
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(row["key_enc"]).decode()
    except InvalidToken:
        return None  # cle maitre changee -> la cle stockee est irrecuperable


def delete_provider_key(scope: str, owner_id: int, provider: str) -> int:
    with _connect() as c:
        return c.execute("DELETE FROM provider_keys WHERE scope=? AND owner_id=? AND provider=?",
                         (scope, owner_id, provider)).rowcount


def configured_providers(scope: str, owner_id: int) -> list[str]:
    if not db_path().exists():
        return []
    with _connect() as c:
        return [r["provider"] for r in c.execute(
            "SELECT provider FROM provider_keys WHERE scope=? AND owner_id=?",
            (scope, owner_id)).fetchall()]


def account_provider_key(account: dict, provider: str) -> str | None:
    """La clé amont pour CE compte. byok → SA clé, platform → la clé
    plateforme. JAMAIS l'une pour l'autre (décision #5) : une clé byok
    absente rend None — le routeur 503 avec cause, pas de repli."""
    if account.get("mode") == "platform":
        return get_provider_key("platform", 0, provider)
    return get_provider_key("user", int(account.get("user_id") or 0), provider)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="coeos-se accounts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-user"); p.add_argument("name")
    p.add_argument("--admin", action="store_true")
    p.add_argument("--mode", choices=["byok", "platform"], default="byok")
    p = sub.add_parser("issue-key"); p.add_argument("user")
    p.add_argument("--name", default="")
    p = sub.add_parser("revoke"); p.add_argument("prefix")
    sub.add_parser("list")
    p = sub.add_parser("set-limits"); p.add_argument("user")
    p.add_argument("--rpm", type=int, default=None, help="req/min (0 = defaut global)")
    p.add_argument("--day-tokens", type=int, default=None,
                   help="tokens/jour (0 = defaut global / illimite)")
    a = ap.parse_args(argv)

    if a.cmd == "create-user":
        u = create_user(a.name, admin=a.admin, mode=a.mode)
        print(f"user cree: {u['name']} (mode={u['mode']}, admin={bool(u['admin'])})")
    elif a.cmd == "issue-key":
        key = issue_key(a.user, a.name)
        print(key)
        print(f"# affichee UNE fois — seul le sha256 est stocke (prefixe {key[:PREFIX_LEN]})",
              file=sys.stderr)
    elif a.cmd == "revoke":
        n = revoke_key(a.prefix)
        print(f"{n} cle(s) revoquee(s)")
    elif a.cmd == "set-limits":
        with _connect() as c:
            sets, vals = [], []
            if a.rpm is not None:
                sets.append("rpm_limit=?"); vals.append(a.rpm)
            if a.day_tokens is not None:
                sets.append("day_tokens=?"); vals.append(a.day_tokens)
            if not sets:
                raise SystemExit("rien a changer (--rpm / --day-tokens)")
            vals.append(a.user.strip())
            n = c.execute(f"UPDATE users SET {', '.join(sets)} WHERE name=?", vals).rowcount
        print(f"{n} user(s) mis a jour")
    elif a.cmd == "list":
        for r in list_all():
            state = "revoquee" if r["revoked"] else ("active" if r["prefix"] else "sans cle")
            print(f"{r['name']:20} mode={r['mode']:8} admin={bool(r['admin'])} "
                  f"{r['prefix'] or '-':14} {r['key_name'] or '':16} {state}")


if __name__ == "__main__":
    main()
