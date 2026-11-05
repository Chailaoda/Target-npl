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
    attribution_authority TEXT NOT NULL DEFAULT 'raw_import',
    PRIMARY KEY(account_key, platform, object_type, content_id)
);
CREATE TABLE IF NOT EXISTS account_attribution_conflicts(
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    object_type TEXT NOT NULL,
    content_id TEXT NOT NULL,
    current_account_key TEXT NOT NULL,
    candidate_account_key TEXT NOT NULL,
    current_source_hash TEXT NOT NULL DEFAULT '',
    candidate_source_hash TEXT NOT NULL DEFAULT '',
    candidate_authority TEXT NOT NULL DEFAULT 'raw_import',
    status TEXT NOT NULL DEFAULT 'pending',
    resolution TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(platform, object_type, content_id, current_account_key, candidate_account_key)
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
        # WP3 databases already have account_content_links WITHOUT the
        # attribution_authority column. CREATE TABLE IF NOT EXISTS cannot add
        # it, so upgrade in place inside the same transaction.
        cols = {r[1] for r in db.execute("PRAGMA table_info(account_content_links)")}
        if "attribution_authority" not in cols:
            db.execute("ALTER TABLE account_content_links ADD COLUMN "
                       "attribution_authority TEXT NOT NULL DEFAULT 'raw_import'")
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


# Source priority for author attribution. A plain (replayed) import batch is
# the weakest evidence; only an explicitly declared higher-authority source
# may change an existing owner. Never invented from import order or wall clock.
ATTRIBUTION_AUTHORITY = {"raw_import": 0, "platform_recheck": 1, "manual_review": 2}


def _authority_rank(name):
    if name not in ATTRIBUTION_AUTHORITY:
        raise ValueError(f"Unknown attribution authority: {name!r}")
    return ATTRIBUTION_AUTHORITY[name]


def link_content(db, key, record, source_hash, seen_at, *,
                 authority="raw_import", own_transaction=True):
    """Associate a normalized record with its author account (idempotent).

    Content version policy: the row points at the CURRENT semantic version
    (source_hash). An edit produces a new hash and repoints the link, while
    older v2_sources rows stay intact for audit history.

    Author attribution policy (P1, review round 4): an import NEVER silently
    overwrites a different owner. A conflicting author is recorded as a
    pending conflict and the confirmed owner is kept, so replaying an OLD
    batch (A -> B -> replay A) cannot revert a correction. Automatic
    reattribution is allowed ONLY when the caller explicitly declares a
    higher source priority (authority > current owner's authority), and is
    always recorded. Explicit human/source confirmation uses
    resolve_attribution_conflict().
    """
    if not key:
        return
    if own_transaction:
        with saving(db):
            _link_content_inner(db, key, record, source_hash, seen_at, authority)
    else:
        _link_content_inner(db, key, record, source_hash, seen_at, authority)


def _insert_link(db, key, record, source_hash, seen_at, authority):
    db.execute("""INSERT INTO account_content_links(
        account_key,platform,object_type,content_id,source_hash,
        first_seen_at,last_seen_at,attribution_authority)
        VALUES (?,?,?,?,?,?,?,?)""",
        (key, record["platform"], record["object_type"], record["content_id"],
         source_hash, seen_at, seen_at, authority))


def _record_conflict(db, platform, otype, cid, current, candidate, cur_hash,
                     cand_hash, authority, seen_at, status, resolution):
    db.execute("""INSERT INTO account_attribution_conflicts(
        platform,object_type,content_id,current_account_key,candidate_account_key,
        current_source_hash,candidate_source_hash,candidate_authority,
        status,resolution,first_seen_at,last_seen_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(platform,object_type,content_id,current_account_key,
                    candidate_account_key) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            candidate_source_hash=excluded.candidate_source_hash,
            candidate_authority=excluded.candidate_authority,
            status=excluded.status,
            resolution=excluded.resolution""",
        (platform, otype, cid, current, candidate, cur_hash, cand_hash,
         authority, status, resolution, seen_at, seen_at))


def _link_content_inner(db, key, record, source_hash, seen_at, authority="raw_import"):
    rank = _authority_rank(authority)
    platform, otype, cid = record["platform"], record["object_type"], record["content_id"]
    where = (platform, otype, cid)
    existing = db.execute("""SELECT account_key,source_hash,attribution_authority
        FROM account_content_links WHERE platform=? AND object_type=? AND content_id=?""",
        where).fetchall()
    if not existing:
        _insert_link(db, key, record, source_hash, seen_at, authority)
        return
    if len(existing) > 1:
        # Historical dirty data: never guess which owner is right.
        _record_conflict(db, platform, otype, cid, existing[0]["account_key"], key,
                         existing[0]["source_hash"], source_hash, authority, seen_at,
                         "pending", "multiple existing owners; manual review required")
        return
    cur = existing[0]
    if cur["account_key"] == key:
        # Idempotent replay / content edit: refresh the version only.
        # A higher-authority batch may also raise the owner's confidence level.
        effective = authority if rank > _authority_rank(cur["attribution_authority"]) \
            else cur["attribution_authority"]
        db.execute("""UPDATE account_content_links SET source_hash=?,last_seen_at=?,
            attribution_authority=? WHERE account_key=? AND platform=? AND object_type=? AND content_id=?""",
            (source_hash, seen_at, effective, key, platform, otype, cid))
        return
    cur_rank = _authority_rank(cur["attribution_authority"])
    if rank > cur_rank:
        # The ONLY automatic reattribution path: explicitly declared higher
        # source priority. Move the link and leave a resolved audit record.
        db.execute("DELETE FROM account_content_links WHERE platform=? AND object_type=? AND content_id=?", where)
        _insert_link(db, key, record, source_hash, seen_at, authority)
        db.execute("""UPDATE account_attribution_conflicts SET status='resolved',resolution=?,last_seen_at=?
            WHERE platform=? AND object_type=? AND content_id=? AND status='pending'""",
            (f"auto: {authority} outranks {cur['attribution_authority']}", seen_at) + where)
        _record_conflict(db, platform, otype, cid, cur["account_key"], key,
                         cur["source_hash"], source_hash, authority, seen_at,
                         "resolved", f"auto: {authority} outranks {cur['attribution_authority']}")
        return
    # Default: keep the confirmed owner, record the conflict for review.
    # Replaying an old batch lands here - no silent revert, no last-writer-wins.
    _record_conflict(db, platform, otype, cid, cur["account_key"], key,
                     cur["source_hash"], source_hash, authority, seen_at, "pending", "")


def resolve_attribution_conflict(db, platform, object_type, content_id, chosen_key, *,
                                 authority="manual_review", note="", own_transaction=True):
    """Explicitly confirm an author attribution (never called by import batches).

    chosen_key must be the current owner or a recorded pending candidate.
    The resolution cannot DOWNGRADE an owner confirmed by a higher authority.
    """
    rank = _authority_rank(authority)
    seen_at = datetime.now(timezone.utc).isoformat()
    where = (platform, object_type, content_id)

    def _resolve():
        cur = db.execute("""SELECT * FROM account_content_links
            WHERE platform=? AND object_type=? AND content_id=?""", where).fetchall()
        if not cur:
            raise ValueError("No content link to resolve")
        pending = db.execute("""SELECT * FROM account_attribution_conflicts
            WHERE platform=? AND object_type=? AND content_id=? AND status='pending'""", where).fetchall()
        allowed = {r["account_key"] for r in cur} | {r["candidate_account_key"] for r in pending}
        if chosen_key not in allowed:
            raise ValueError("Chosen account is neither the current owner nor a recorded conflict candidate")
        cur_rank = max(_authority_rank(r["attribution_authority"]) for r in cur)
        if rank < cur_rank:
            raise ValueError("Resolution authority is lower than the current attribution")
        owner = next((r for r in cur if r["account_key"] == chosen_key), None)
        source_hash = owner["source_hash"] if owner else \
            next(r["candidate_source_hash"] for r in pending if r["candidate_account_key"] == chosen_key)
        db.execute("DELETE FROM account_content_links WHERE platform=? AND object_type=? AND content_id=?", where)
        db.execute("""INSERT INTO account_content_links(
            account_key,platform,object_type,content_id,source_hash,
            first_seen_at,last_seen_at,attribution_authority)
            VALUES (?,?,?,?,?,?,?,?)""",
            (chosen_key, platform, object_type, content_id, source_hash, seen_at, seen_at, authority))
        db.execute("""UPDATE account_attribution_conflicts SET status='resolved',resolution=?,last_seen_at=?
            WHERE platform=? AND object_type=? AND content_id=? AND status='pending'""",
            (f"manual: {note or authority}", seen_at) + where)
        return chosen_key

    if own_transaction:
        with saving(db):
            return _resolve()
    return _resolve()


def attribution_conflicts(db, *, status="pending", account_key=None):
    """Query attribution conflicts awaiting (or after) confirmation (WP5/WP6)."""
    rows = db.execute("""SELECT * FROM account_attribution_conflicts
        WHERE status=? ORDER BY id""", (status,)).fetchall()
    result = [dict(r) for r in rows]
    if account_key:
        result = [r for r in result if account_key in
                  (r["current_account_key"], r["candidate_account_key"])]
    return result


def analyzed_content_links(db, key):
    """WP4: verified interest evidence ONLY.

    account_content_links is written by workflow.run() BEFORE model analysis;
    a failed model call leaves a link pointing at content with no successful
    analysis. WP4 must never treat that link as validated evidence. This
    helper returns only links whose source_hash has a stored v2_analyses
    result; consumers should additionally honour review/quarantine state.
    """
    try:
        return [dict(r) for r in db.execute("""SELECT l.* FROM account_content_links l
            JOIN v2_analyses a ON a.source_hash = l.source_hash
            WHERE l.account_key=? AND a.result_json IS NOT NULL AND a.result_json <> ''
            ORDER BY l.content_id""", (key,))]
    except sqlite3.OperationalError:
        return []  # analysis tables not created yet (fresh/legacy database)


def ingest_observations(db, observations, seen_at, fingerprint_fn,
                        attribution_authority="raw_import"):
    """Ingest profile payloads and content links for one import run.

    Runs INSIDE the caller's transaction (workflow.run()'s outer `with db:`);
    repository functions are called with own_transaction=False so the whole
    import batch (sources, tasks, observations, accounts, links) commits or
    rolls back together. Anonymous/unknown rows are skipped entirely.

    attribution_authority is an EXPLICIT declaration by the caller about the
    trust level of this batch. Plain/replayed imports keep 'raw_import' and
    can never overwrite a confirmed owner.
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
            link_content(db, key, record, fingerprint_fn(record), seen_at,
                         authority=attribution_authority, own_transaction=False)
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
