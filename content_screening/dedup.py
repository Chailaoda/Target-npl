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

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS seen_content (
    platform    TEXT NOT NULL,
    content_id  TEXT NOT NULL,
    object_type TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    collection_id TEXT,
    PRIMARY KEY (platform, content_id)
);
"""


def open_index(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    for stmt in _SCHEMA.split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    return db


def _extract_platform(row):
    p = row.get("platform", "")
    if p:
        return p
    p = row.get("data", {}).get("platform", "")
    if p:
        return p
    if "aweme_id" in row.get("data", row):
        return "douyin"
    return ""


def _extract_id(row):
    data = row.get("data", row)
    if "content_id" in data:
        return str(data["content_id"])
    if row.get("object_type") == "comment" and "comment_id" in data:
        return str(data["comment_id"])
    if "aweme_id" in data:
        return str(data["aweme_id"])
    return ""


def ingest_raw(db, raw_path, collection_id=None):
    """Read raw.jsonl and mark all content as seen. Returns count of NEW items."""
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    with open(raw_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            cid = _extract_id(row)
            if not cid:
                continue
            platform = _extract_platform(row)
            otype = row.get("object_type", row.get("data", {}).get("object_type", "post"))
            rows.append((platform, cid, otype, now, collection_id))
    before = db.execute("SELECT COUNT(*) FROM seen_content").fetchone()[0]
    with db:
        db.executemany("INSERT OR IGNORE INTO seen_content VALUES (?,?,?,?,?)", rows)
    after = db.execute("SELECT COUNT(*) FROM seen_content").fetchone()[0]
    return after - before


def filter_unseen(db, raw_path, output_path):
    """Copy only unseen records from raw_path to output_path. Returns (kept, skipped)."""
    kept = skipped = 0
    with open(raw_path, encoding="utf-8") as src, \
         open(output_path, "w", encoding="utf-8") as dst:
        for line in src:
            if not line.strip():
                continue
            row = json.loads(line)
            cid = _extract_id(row)
            platform = _extract_platform(row)
            if cid and db.execute(
                "SELECT 1 FROM seen_content WHERE platform=? AND content_id=?",
                (platform, cid)
            ).fetchone():
                skipped += 1
            else:
                dst.write(line if line.endswith("\n") else line + "\n")
                kept += 1
    return kept, skipped


def stats(db):
    total = db.execute("SELECT COUNT(*) FROM seen_content").fetchone()[0]
    by_platform = dict(db.execute(
        "SELECT platform, COUNT(*) FROM seen_content GROUP BY platform"
    ).fetchall())
    return {"total": total, "by_platform": by_platform}
