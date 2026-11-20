"""Incremental content dedup index — tracks content seen across collection runs.

Separate from festival.sqlite3: no migration needed, no interference with the
main analysis database. The scheduler uses this to avoid re-analyzing content
that was already processed in a previous round.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

_SCHEMA_V2 = """\
CREATE TABLE IF NOT EXISTS seen_content (
    platform    TEXT NOT NULL,
    content_id  TEXT NOT NULL,
    object_type TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    collection_id TEXT,
    PRIMARY KEY (platform, object_type, content_id)
);
"""

_MIGRATION = """\
CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT);
"""


def open_index(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from .db_access import apply_pragmas
    db = sqlite3.connect(path, timeout=10.0)
    try:
        apply_pragmas(db)
        db.execute(_MIGRATION)
        version = db.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()
        if version and version[0] == "2":
            pass
        else:
            db.execute("DROP TABLE IF EXISTS seen_content")
            db.executescript(_SCHEMA_V2)
            db.execute(
                "INSERT OR REPLACE INTO _meta VALUES ('schema_version', '2')"
            )
            db.commit()
    except Exception:
        db.close()
        raise
    return db


def _extract_platform(row):
    if not isinstance(row, dict):
        return ""
    data = row.get("data")
    if not isinstance(data, dict):
        data = row
    p = row.get("platform", "")
    if p:
        return str(p)
    p = data.get("platform", "")
    if p:
        return str(p)
    if "aweme_id" in data:
        return "douyin"
    return ""


def _extract_object_type(row):
    if not isinstance(row, dict):
        return "post"
    data = row.get("data") if isinstance(row.get("data"), dict) else row
    otype = row.get("object_type", data.get("object_type", ""))
    if otype:
        return str(otype)
    if "comment_id" in data:
        return "comment"
    return "post"


def _extract_id(row):
    if not isinstance(row, dict):
        return ""
    data = row.get("data") if isinstance(row.get("data"), dict) else row
    if "content_id" in data and data["content_id"] is not None:
        cid = str(data["content_id"]).strip()
        if cid:
            return cid
    otype = _extract_object_type(row)
    if otype == "comment" and "comment_id" in data and data["comment_id"] is not None:
        cid = str(data["comment_id"]).strip()
        if cid:
            return cid
    if "aweme_id" in data and data["aweme_id"] is not None:
        cid = str(data["aweme_id"]).strip()
        if cid:
            return cid
    return ""


def ingest_raw(db, raw_path, collection_id=None):
    """Read raw.jsonl and mark all content as seen. Returns count of NEW items."""
    now = datetime.now(timezone.utc).isoformat()
    seen_in_batch = set()
    rows = []
    with open(raw_path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = _extract_id(row)
            if not cid:
                continue
            platform = _extract_platform(row)
            if not platform:
                continue
            otype = _extract_object_type(row)
            key = (platform, otype, cid)
            if key in seen_in_batch:
                continue
            seen_in_batch.add(key)
            rows.append((platform, cid, otype, now, collection_id))
    inserted = 0
    with db:
        for r in rows:
            try:
                db.execute("INSERT INTO seen_content VALUES (?,?,?,?,?)", r)
                inserted += 1
            except sqlite3.IntegrityError:
                pass
    return inserted


def filter_unseen(db, raw_path, output_path):
    """Copy only unseen records from raw_path to output_path. Returns (kept, skipped)."""
    kept = skipped = 0
    seen_in_batch = set()
    with open(raw_path, encoding="utf-8") as src, \
         open(output_path, "w", encoding="utf-8") as dst:
        for line in src:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = _extract_id(row)
            platform = _extract_platform(row)
            otype = _extract_object_type(row)
            key = (platform, otype, cid)
            if not cid or not platform:
                dst.write(line if line.endswith("\n") else line + "\n")
                kept += 1
                continue
            if key in seen_in_batch or db.execute(
                "SELECT 1 FROM seen_content WHERE platform=? AND object_type=? AND content_id=?",
                (platform, otype, cid)
            ).fetchone():
                skipped += 1
            else:
                seen_in_batch.add(key)
                dst.write(line if line.endswith("\n") else line + "\n")
                kept += 1
    return kept, skipped


def stats(db):
    total = db.execute("SELECT COUNT(*) FROM seen_content").fetchone()[0]
    by_platform = dict(db.execute(
        "SELECT platform, COUNT(*) FROM seen_content GROUP BY platform"
    ).fetchall())
    return {"total": total, "by_platform": by_platform}
