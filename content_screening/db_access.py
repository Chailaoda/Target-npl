"""The only business database resolver. Ordinary connects never run DDL."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from .runtime_context import RuntimeContext, RuntimeErrorBase, current_context


V2_TABLES = ("v2_sources", "v2_policies", "v2_observations", "v2_jobs", "v2_tasks", "v2_analyses", "v2_reviews", "v2_review_history", "v2_attempts")
LEGACY_TABLES = ("sources", "profiles", "evaluations", "runs", "run_annotations")
ACCOUNT_TABLES = ("accounts", "account_profile_history", "account_content_links", "interest_evidence", "interest_profiles", "interest_profile_versions", "account_attribution_conflicts")
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


def resolve_db(context=None):
    context = context or current_context()
    configured = context.deployment["database"]["relative_path"]
    if context.deployment.get("test_isolated") and configured == "data/festival.sqlite3":
        return context.state_path(configured)
    if configured != "data/festival.sqlite3":
        raise RuntimeErrorBase("DB_BINDING_MISMATCH", "only the unified festival.sqlite3 is accepted")
    return context.state_path(configured)


def _open(path, *, readonly=False):
    if readonly:
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    return sqlite3.connect(path)


def _schema_state(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables:
        return "empty"
    if not set(V2_TABLES) <= tables:
        return "legacy_unmigrated"
    columns = {r[1] for r in db.execute("PRAGMA table_info(v2_jobs)")}
    if not {"task_json", "config_revision"} <= columns or not set(ACCOUNT_TABLES) <= tables:
        return "migration_required"
    return "migrated"


def preflight_db(context=None):
    path = resolve_db(context)
    if not path.exists():
        raise RuntimeErrorBase("DB_MISSING", str(path))
    if path.stat().st_size == 0:
        raise RuntimeErrorBase("DB_EMPTY_FILE", str(path))
    try:
        with _open(path, readonly=True) as db:
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


def connect(path=None, *, readonly=False, context=None):
    context = context or current_context()
    resolved = resolve_db(context) if path is None else Path(path).resolve()
    expected = resolve_db(context)
    if resolved != expected:
        # Explicit test deployments may bind an isolated temporary state root.
        # Never migrate implicitly; only a DB already migrated by the test's
        # explicit fixture can be opened.
        if not context.deployment.get("test_isolated"):
            raise RuntimeErrorBase("DB_BINDING_MISMATCH", str(resolved))
        if not resolved.is_relative_to(context.state_root):
            raise RuntimeErrorBase("DB_PATH_OUTSIDE_STATE", str(resolved))
        if not readonly and not resolved.is_file():
            raise RuntimeErrorBase("DB_MISSING", str(resolved))
        if not readonly:
            with _open(resolved, readonly=True) as probe:
                if _schema_state(probe) != "migrated":
                    raise RuntimeErrorBase("DB_SCHEMA_NOT_READY", str(resolved))
    if readonly:
        db = _open(resolved, readonly=True)
    else:
        preflight_db(context)
        db = _open(resolved)
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
