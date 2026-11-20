"""The only business database resolver. Ordinary connects never run DDL."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from .runtime_context import RuntimeContext, RuntimeErrorBase, _is_relative_to, current_context


SQLITE_SUFFIXES = (".sqlite3", ".sqlite", ".db")
SQLITE_HEADER = b"SQLite format 3\x00"
V2_TABLES = ("v2_sources", "v2_policies", "v2_observations", "v2_jobs", "v2_tasks", "v2_analyses", "v2_reviews", "v2_review_history", "v2_attempts")
LEGACY_TABLES = ("sources", "profiles", "evaluations", "runs", "run_annotations")
ACCOUNT_TABLES = ("accounts", "account_profile_history", "account_content_links", "interest_evidence", "interest_profiles", "interest_profile_versions", "account_attribution_conflicts")
# WP4 adds two tables and extends two legacy interest tables. A WP3 database
# without them would pass the old preflight and fail only during analysis.
WP4_TABLES = ("interest_topic_definitions", "interest_content_analysis")
WP4_COLUMNS = {
    "interest_evidence": {"source_hash", "profile_version", "review_state", "review_note", "details_json"},
    "interest_profiles": {"evidence_count", "independent_content_count", "coverage", "last_activity", "details_json"},
}
V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS v2_sources(source_hash TEXT PRIMARY KEY, object_key TEXT NOT NULL, source_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v2_policies(policy_hash TEXT PRIMARY KEY, policy_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v2_observations(input_hash TEXT, line INTEGER, source_hash TEXT REFERENCES v2_sources, keyword TEXT, raw_hash TEXT, input_file TEXT, PRIMARY KEY(input_hash,line));
CREATE TABLE IF NOT EXISTS v2_jobs(job_id TEXT PRIMARY KEY, created_at TEXT, finished_at TEXT, status TEXT, input_hash TEXT, policy_hash TEXT, identity_json TEXT);
CREATE TABLE IF NOT EXISTS v2_tasks(job_id TEXT REFERENCES v2_jobs, source_hash TEXT REFERENCES v2_sources, analysis_key TEXT, status TEXT, error_code TEXT, PRIMARY KEY(job_id,source_hash));
CREATE TABLE IF NOT EXISTS v2_analyses(analysis_key TEXT PRIMARY KEY, source_hash TEXT REFERENCES v2_sources, policy_hash TEXT, context_json TEXT, result_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS v2_reviews(analysis_key TEXT PRIMARY KEY REFERENCES v2_analyses, decision TEXT, note TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS v2_review_history(id INTEGER PRIMARY KEY, analysis_key TEXT REFERENCES v2_analyses, decision TEXT, note TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS v2_attempts(id INTEGER PRIMARY KEY, job_id TEXT REFERENCES v2_jobs, analysis_key TEXT, api_called INTEGER, status TEXT, error_code TEXT, usage_json TEXT, created_at TEXT);
"""
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(source_hash TEXT PRIMARY KEY, platform TEXT NOT NULL, content_id TEXT NOT NULL, object_type TEXT NOT NULL, raw_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles(profile_hash TEXT PRIMARY KEY, profile_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations(source_hash TEXT REFERENCES sources, profile_hash TEXT REFERENCES profiles, result_json TEXT NOT NULL, PRIMARY KEY(source_hash,profile_hash));
CREATE TABLE IF NOT EXISTS runs(run_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, profile_hash TEXT REFERENCES profiles, input_count INTEGER NOT NULL, unique_count INTEGER NOT NULL, accepted_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS run_annotations(run_id INTEGER PRIMARY KEY, note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
"""


def _configured_relative_path(context):
    """Normalize and validate the deployment-declared database location.

    The manifest is portable: it declares a path relative to STATE_ROOT. A
    drive-letter path, a parent-directory escape or a non-SQLite extension is
    rejected here instead of being silently rewritten.
    """
    configured = context.deployment.get("database", {}).get("relative_path")
    if not isinstance(configured, str) or not configured.strip():
        raise RuntimeErrorBase("DB_BINDING_MISMATCH", "database.relative_path is missing")
    candidate = Path(configured.strip())
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeErrorBase("DB_BINDING_MISMATCH", f"database path must stay inside state_root: {configured}")
    if candidate.suffix.lower() not in SQLITE_SUFFIXES:
        raise RuntimeErrorBase("DB_BINDING_MISMATCH", f"database file must use a SQLite extension: {configured}")
    return candidate


def resolve_db(context=None):
    context = context or current_context()
    configured = _configured_relative_path(context)
    resolved = (context.state_root / configured).resolve()
    data_root = (context.state_root / "data").resolve()
    if not _is_relative_to(resolved, data_root):
        raise RuntimeErrorBase("DB_BINDING_MISMATCH", f"database must live under state_root/data: {configured}")
    return resolved


def resolve_db_for(state_root, context=None):
    """Resolve the deployment-relative database under an explicit state root.

    Readers that serve one store (the live monitor, tests, an integration
    instance) must read that store's database instead of whatever the process
    default happens to be. The relative layout is identical, so the deployment
    still declares a single canonical file name.
    """
    context = context or current_context()
    configured = _configured_relative_path(context)
    return (Path(state_root) / configured).resolve()


def _explicit_db_path(path, context):
    """Validate a caller-supplied database path (legacy CLI, tests, tooling).

    Ordinary connects are bound to the deployment database; this branch is
    reached only when a caller passes an explicit path, so it enforces shape
    (a real SQLite file inside an existing directory) instead of an identity
    match. Directory traversal is already normalized away by Path.resolve().
    """
    resolved = Path(path).resolve()
    if resolved == resolve_db(context):
        return resolved
    if resolved.suffix.lower() not in SQLITE_SUFFIXES:
        raise RuntimeErrorBase("DB_BINDING_MISMATCH", f"not a SQLite database path: {resolved}")
    if resolved.is_dir():
        raise RuntimeErrorBase("DB_BINDING_MISMATCH", f"database path is a directory: {resolved}")
    if resolved.is_file() and resolved.stat().st_size:
        with resolved.open("rb") as stream:
            if stream.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                raise RuntimeErrorBase("DB_BINDING_MISMATCH", f"file is not a SQLite database: {resolved}")
    return resolved


def provision_legacy(path):
    """Create the legacy + v2 schema for an explicit, caller-owned database.

    Only the legacy CLI shims (pipeline / workflow) and their tests opt into
    this. The deployed database is never provisioned implicitly: a missing or
    empty authority store must fail preflight instead of being invented.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = _open(path)
    try:
        for schema in (V2_SCHEMA, LEGACY_SCHEMA):
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
        columns = {row[1] for row in db.execute("PRAGMA table_info(v2_jobs)")}
        for column in ("task_json", "config_revision"):
            if column not in columns:
                db.execute(f"ALTER TABLE v2_jobs ADD COLUMN {column} TEXT")
        from .account_repository import migrate as migrate_accounts
        migrate_accounts(db, own_transaction=False)
        db.commit()
    finally:
        db.close()
    return path


def apply_pragmas(conn, *, readonly=False):
    """Apply the single concurrency policy used by every project database.

    Writers need WAL so readers never block them and a 10s retry budget so a
    short burst of concurrent writers queues instead of failing. Read-only
    handles must skip the journal-mode switch (SQLite rejects it) and use
    query_only to make accidental writes fail loudly.
    """
    if readonly:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA query_only = ON")
        return conn
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _open(path, *, readonly=False):
    """Open the file and apply the shared pragma profile."""
    if readonly:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
    else:
        conn = sqlite3.connect(path, timeout=10.0)
    return apply_pragmas(conn, readonly=readonly)


def _schema_state(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables:
        return "empty"
    if not set(V2_TABLES) <= tables:
        return "legacy_unmigrated"
    columns = {r[1] for r in db.execute("PRAGMA table_info(v2_jobs)")}
    if not {"task_json", "config_revision"} <= columns or not set(ACCOUNT_TABLES) <= tables:
        return "migration_required"
    if not set(WP4_TABLES) <= tables:
        return "migration_required"
    for table, required in WP4_COLUMNS.items():
        present = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if not required <= present:
            return "migration_required"
    return "migrated"


def preflight_db(context=None):
    path = resolve_db(context)
    if not path.exists():
        raise RuntimeErrorBase("DB_MISSING", str(path))
    if path.stat().st_size == 0:
        raise RuntimeErrorBase("DB_EMPTY_FILE", str(path))
    try:
        with closing(_open(path, readonly=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeErrorBase("DB_INVALID_SQLITE", str(path))
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                raise RuntimeErrorBase("DB_INVALID_SQLITE", "empty database cannot masquerade as the authority")
            if not set(LEGACY_TABLES) <= tables and not set(V2_TABLES) <= tables:
                raise RuntimeErrorBase("DB_LEGACY_SCHEMA_MISSING", sorted(tables))
            state = _schema_state(db)
            if state != "migrated":
                raise RuntimeErrorBase("DB_SCHEMA_NOT_READY", state)
            return state
    except sqlite3.Error as error:
        raise RuntimeErrorBase("DB_INVALID_SQLITE", str(error)) from error


def connect(path=None, *, readonly=False, context=None, provision=False):
    """Open the business database.

    ``path=None`` always binds to the deployment database and must pass
    preflight. An explicit path belongs to a caller that owns that file
    (legacy CLI, migration tooling, tests); it is validated for shape and may
    be provisioned on request, but it can never masquerade as the deployed
    authority because preflight and release identity keep reading the
    deployment binding.
    """
    context = context or current_context()
    expected = resolve_db(context)
    explicit = path is not None and Path(path).resolve() != expected
    if explicit:
        resolved = _explicit_db_path(path, context)
        if not readonly:
            if provision:
                provision_legacy(resolved)
            elif not resolved.is_file():
                raise RuntimeErrorBase("DB_MISSING", str(resolved))
    else:
        resolved = expected
        if not readonly:
            preflight_db(context)
    db = _open(resolved, readonly=readonly)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def connect_readonly(path=None, *, context=None):
    return connect(path, readonly=True, context=context)


def migrate(context=None):
    """Explicit, transactional migration; never called by connect/preflight."""
    context = context or current_context()
    path = resolve_db(context)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeErrorBase("DB_MISSING", str(path))
    from .account_repository import migrate as migrate_accounts
    db = _open(path)
    old_isolation = db.isolation_level
    try:
        db.isolation_level = None
        db.execute("BEGIN IMMEDIATE")
        for schema in (V2_SCHEMA, LEGACY_SCHEMA):
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
        columns = {r[1] for r in db.execute("PRAGMA table_info(v2_jobs)")}
        for column in ("task_json", "config_revision"):
            if column not in columns:
                db.execute(f"ALTER TABLE v2_jobs ADD COLUMN {column} TEXT")
        migrate_accounts(db, own_transaction=False)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.isolation_level = old_isolation
        db.close()
    return preflight_db(context)
