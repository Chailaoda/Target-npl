"""Account repository: durable public-account identity, profile history and
content links, keyed by (platform, account_id).

WP3 of the public-account-profiles plan. Design rules:
- The ONLY account identity key is platform + account_id. Nicknames are
  display metadata and never merge keys.
- Profile updates (nickname/profile URL) UPDATE the accounts row and append
  to account_profile_history; they never touch v2_sources/v2_analyses, so
  cached content analyses stay valid (no model cost from a rename).
- Anonymized/unknown-author rows are not stored here at all; legacy
  anonymous hashes are never re-identified.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
    account_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    profile_url TEXT NOT NULL DEFAULT '',
    identity_source TEXT NOT NULL DEFAULT '',
    identity_mode TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(platform, account_id)
);
CREATE TABLE IF NOT EXISTS account_profile_history(
    id INTEGER PRIMARY KEY,
    account_key TEXT NOT NULL REFERENCES accounts,
    nickname TEXT NOT NULL DEFAULT '',
    profile_url TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_content_links(
    account_key TEXT NOT NULL REFERENCES accounts,
    platform TEXT NOT NULL,
    object_type TEXT NOT NULL,
    content_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(account_key, platform, object_type, content_id)
);
CREATE TABLE IF NOT EXISTS interest_evidence(
    id INTEGER PRIMARY KEY,
    account_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    analysis_key TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    stance TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    UNIQUE(account_key, topic, analysis_key)
);
CREATE TABLE IF NOT EXISTS interest_profiles(
    id INTEGER PRIMARY KEY,
    account_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    content_count INTEGER NOT NULL DEFAULT 0,
    active_days INTEGER NOT NULL DEFAULT 0,
    stance_summary TEXT NOT NULL DEFAULT '',
    trend TEXT NOT NULL DEFAULT '',
    evidence_notes TEXT NOT NULL DEFAULT '',
    profile_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_key, topic, profile_version)
);
CREATE TABLE IF NOT EXISTS interest_profile_versions(
    profile_version TEXT PRIMARY KEY,
    algorithm_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_links_source ON account_content_links(source_hash);
CREATE INDEX IF NOT EXISTS idx_evidence_account_topic ON interest_evidence(account_key, topic);
"""


def account_key(platform, account_id):
    """Canonical account key: platform:account_id. Empty account_id -> no key."""
    account_id = (account_id or "").strip()
    if not account_id:
        return ""
    return f"{platform}:{account_id}"


def migrate(db):
    """Idempotent forward migration (all CREATE IF NOT EXISTS, rollback-safe).

    Atomic: the schema is applied inside one explicit transaction, so an
    interrupted migration either fully applies or leaves the database at the
    previous schema — never a half-created state. (executescript() would
    auto-commit per statement; we split on ';' and wrap in BEGIN/COMMIT.)
    """
    isolation = db.isolation_level
    try:
        db.isolation_level = None  # manual transaction control
        db.execute("BEGIN")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.isolation_level = isolation


@contextmanager
def saving(db):
    """Explicit standalone transaction, for callers that do NOT already own one.

    NOTE: workflow.run() owns one transaction for the whole import batch and
    calls upsert_account/link_content/ingest_observations INSIDE it; those
    functions therefore never open transactions themselves. Calling `with db:`
    inside an outer `with db:` block would commit the outer transaction
    prematurely and destroy batch atomicity. Use this helper only from
    top-level callers (tests, one-off scripts).
    """
    with db:
        yield


def upsert_account(db, profile, seen_at, *, own_transaction=True):
    """Insert or refresh a public account row; record profile changes in history.

    profile: dict from normalization.normalize_profile() or equivalent
    (platform, account_id, nickname, profile_url, identity_source,
    identity_mode). Missing fields default to '' — never invented.

    own_transaction=False when the caller already owns the surrounding
    transaction (workflow.run()); True for standalone top-level calls.
    """
    key = account_key(profile.get("platform"), profile.get("account_id"))
    if not key or profile.get("identity_mode") != "public_source":
        return None
    nickname = profile.get("nickname", "") or ""
    profile_url = profile.get("profile_url", "") or ""
    row = db.execute("SELECT nickname,profile_url FROM accounts WHERE account_key=?", (key,)).fetchone()
    if own_transaction:
        with saving(db):
            _upsert_account_inner(db, key, profile, nickname, profile_url, row, seen_at)
    else:
        _upsert_account_inner(db, key, profile, nickname, profile_url, row, seen_at)
    return key


def _upsert_account_inner(db, key, profile, nickname, profile_url, row, seen_at):
    if row is None:
        db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?)",
                   (key, profile["platform"], profile["account_id"], nickname, profile_url,
                    profile.get("identity_source", ""), profile.get("identity_mode", "public_source"),
                    seen_at, seen_at))
        db.execute("INSERT INTO account_profile_history(account_key,nickname,profile_url,observed_at) VALUES (?,?,?,?)",
                   (key, nickname, profile_url, seen_at))
    else:
        # Profile refresh: never touches content analysis caches.
        # Missing (empty) values from a new batch never erase known values:
        # platform fields the API simply did not return this time are kept.
        new_nick = nickname or row["nickname"]
        new_url = profile_url or row["profile_url"]
        db.execute("UPDATE accounts SET nickname=?,profile_url=?,last_seen_at=? WHERE account_key=?",
                   (new_nick, new_url, seen_at, key))
        if (row["nickname"], row["profile_url"]) != (new_nick, new_url):
            db.execute("INSERT INTO account_profile_history(account_key,nickname,profile_url,observed_at) VALUES (?,?,?,?)",
                       (key, new_nick, new_url, seen_at))


def link_content(db, key, record, source_hash, seen_at, *, own_transaction=True):
    """Associate a normalized record with its author account (idempotent).

    Content version policy: the row points at the CURRENT semantic version
    (source_hash) of each (account, content). When an edit produces a new
    source_hash the link is repointed, so account overviews and later
    interest profiling always read the latest analyzed content — while the
    older v2_sources rows stay intact for audit history.

    Author reattribution: if the same content is later collected under a
    DIFFERENT author (correction/re-upload), the previous account's link is
    removed so one content item never stays credited to two accounts (P1).

    own_transaction=False when the caller already owns the surrounding
    transaction (workflow.run()).
    """
    if not key:
        return
    if own_transaction:
        with saving(db):
            _link_content_inner(db, key, record, source_hash, seen_at)
    else:
        _link_content_inner(db, key, record, source_hash, seen_at)


def _link_content_inner(db, key, record, source_hash, seen_at):
    # Reattribution guard: drop any other account's link to this content first.
    db.execute("""DELETE FROM account_content_links
        WHERE platform=? AND object_type=? AND content_id=? AND account_key<>?""",
        (record["platform"], record["object_type"], record["content_id"], key))
    db.execute("""INSERT INTO account_content_links(account_key,platform,object_type,content_id,source_hash,first_seen_at,last_seen_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(account_key,platform,object_type,content_id) DO UPDATE SET
            source_hash=excluded.source_hash,
            last_seen_at=excluded.last_seen_at""",
        (key, record["platform"], record["object_type"], record["content_id"], source_hash, seen_at, seen_at))


def ingest_observations(db, observations, seen_at, fingerprint_fn):
    """Ingest profile payloads and content links for one import run.

    Runs INSIDE the caller's transaction (workflow.run()'s outer `with db:`);
    repository functions are called with own_transaction=False so the whole
    import batch (sources, tasks, observations, accounts, links) commits or
    rolls back together. Anonymous/unknown rows are skipped entirely.
    """
    keys = {}
    for obs in observations:
        profile = obs.get("profile")
        record = obs["record"]
        if not profile or profile.get("identity_mode") != "public_source":
            continue
        key = upsert_account(db, profile, seen_at, own_transaction=False)
        if key:
            keys[key] = True
            link_content(db, key, record, fingerprint_fn(record), seen_at, own_transaction=False)
    return sorted(keys)


def account_overview(db, key):
    """Current profile + activity stats for one account (WP5/WP6 consume this)."""
    row = db.execute("SELECT * FROM accounts WHERE account_key=?", (key,)).fetchone()
    if row is None:
        return None
    # Activity stats are derived from linked sources' JSON (published_at lives
    # inside v2_sources.source_json, not as a SQL column).
    content_count = db.execute("SELECT count(*) FROM account_content_links WHERE account_key=?", (key,)).fetchone()[0]
    # Active days are distinct UTC calendar dates, not distinct raw timestamps
    # (two posts at 09:00 and 15:00 the same day count as ONE active day).
    # Values may carry non-UTC offsets (+08:00 etc.) or be empty - parse the
    # timezone and normalize to the UTC date instead of slicing the raw text.
    from datetime import datetime as _dt
    days = set()
    for link in db.execute("SELECT source_hash FROM account_content_links WHERE account_key=?", (key,)).fetchall():
        src = db.execute("SELECT source_json FROM v2_sources WHERE source_hash=?", (link["source_hash"],)).fetchone() if _has_column(db) else None
        if src:
            try:
                published = json.loads(src["source_json"]).get("published_at", "")
                if isinstance(published, str) and published:
                    dt = _dt.fromisoformat(published)  # offset-aware ISO-8601
                    days.add(dt.astimezone(timezone.utc).date().isoformat())
            except (ValueError, TypeError):
                pass
    history = db.execute("SELECT nickname,profile_url,observed_at FROM account_profile_history WHERE account_key=? ORDER BY id", (key,)).fetchall()
    return {"account": dict(row), "content_count": content_count, "active_days": len([d for d in days if d]),
            "history": [dict(h) for h in history]}


def _has_column(db):
    return any(r[1] == "source_json" for r in db.execute("PRAGMA table_info(v2_sources)"))


def search_accounts(db, keyword="", limit=50, offset=0):
    """Paged account search by platform/account id/nickname prefix (WP5)."""
    like = f"%{keyword}%"
    rows = db.execute("""SELECT a.*, count(l.content_id) content_count
        FROM accounts a LEFT JOIN account_content_links l ON l.account_key=a.account_key
        WHERE a.account_key LIKE ? OR a.nickname LIKE ?
        GROUP BY a.account_key ORDER BY a.last_seen_at DESC LIMIT ? OFFSET ?""",
        (like, like, int(limit), int(offset))).fetchall()
    total = db.execute("SELECT count(*) FROM accounts WHERE account_key LIKE ? OR nickname LIKE ?", (like, like)).fetchone()[0]
    return [dict(r) for r in rows], total
