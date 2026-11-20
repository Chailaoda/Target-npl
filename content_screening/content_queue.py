"""用 SQLite 持久化采集原文和分析状态。"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from .dedup import _extract_platform, _extract_object_type, _extract_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,
    object_type   TEXT NOT NULL,
    content_id    TEXT NOT NULL,
    raw_json      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    collection_id TEXT,
    enqueued_at   TEXT NOT NULL,
    claimed_at    TEXT,
    completed_at  TEXT,
    worker_id     TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    UNIQUE (platform, object_type, content_id)
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def open_queue(path):
    """打开队列数据库并在事务中初始化表和索引。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from .db_access import apply_pragmas
    db = sqlite3.connect(path, timeout=30)
    try:
        db.row_factory = sqlite3.Row
        apply_pragmas(db)
        if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise sqlite3.OperationalError("队列数据库无法启用 WAL")
        with db:
            db.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
    except BaseException:
        db.close()
        raise
    return db


def enqueue_from_raw(db, raw_path, collection_id=None):
    """保留原始 JSON 行，按平台、类型和内容 ID 去重入队。"""
    now = _utcnow()
    rows = []
    with Path(raw_path).open("rb") as raw:
        for lineno, line in enumerate(raw, 1):
            try:
                raw_json = line.decode("utf-8-sig" if lineno == 1 else "utf-8").rstrip("\r\n")
                if not raw_json.strip():
                    continue
                row = json.loads(raw_json)
                platform = _extract_platform(row)
                content_id = _extract_id(row)
                if not platform or not content_id:
                    continue
                object_type = _extract_object_type(row)
                # 提取值可能含有 JSON 转义的孤立代理字符，SQLite 无法写入这些键。
                (platform + object_type + content_id).encode("utf-8")
            except (ValueError, RecursionError):
                continue
            rows.append((platform, object_type, content_id, raw_json, collection_id, now))

    with db:
        return db.executemany(
            """INSERT OR IGNORE INTO queue
               (platform, object_type, content_id, raw_json, collection_id, enqueued_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        ).rowcount


def claim(db, limit, worker_id):
    """按入队顺序认领，写入和读取认领结果共用同一事务。"""
    if limit <= 0:
        return []
    with db:
        db.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        previous = db.execute(
            "SELECT MAX(claimed_at) FROM queue WHERE worker_id=? AND status='claimed'",
            (worker_id,),
        ).fetchone()[0]
        if previous and previous >= now:
            # 同名 worker 在时钟回拨或时间戳相同时也能区分批次。
            now = (datetime.fromisoformat(previous) + timedelta(microseconds=1)).isoformat(timespec="microseconds")
        updated = db.execute(
            """UPDATE queue SET status='claimed', claimed_at=?, worker_id=?
               WHERE id IN (SELECT id FROM queue WHERE status='pending' ORDER BY id LIMIT ?)""",
            (now, worker_id, limit),
        )
        if not updated.rowcount:
            return []
        return [dict(row) for row in db.execute(
            """SELECT * FROM queue WHERE worker_id=? AND status='claimed' AND claimed_at=?
               ORDER BY id""",
            (worker_id, now),
        )]


def finish(db, done, failures):
    """在一个事务中提交同批记录的完成和失败状态。"""
    now = _utcnow()
    with db:
        db.executemany(
            """UPDATE queue SET status='done', completed_at=?, error=NULL
               WHERE id=? AND status='claimed'""",
            ((now, item_id) for item_id in done),
        )
        db.executemany(
            """UPDATE queue SET status='failed', completed_at=?, attempts=attempts+1, error=?
               WHERE id=? AND status='claimed'""",
            ((now, error, item_id) for item_id, error in failures.items()),
        )


def complete(db, ids):
    """将指定的已认领记录标记为完成。"""
    finish(db, ids, {})


def fail(db, ids, error):
    """将指定的已认领记录标记为失败并累计尝试次数。"""
    finish(db, [], dict.fromkeys(ids, error))


def retry_failed(db, max_attempts=3):
    """将仍有尝试次数的失败记录放回队列。"""
    with db:
        return db.execute(
            """UPDATE queue SET status='pending', claimed_at=NULL, completed_at=NULL,
                                worker_id=NULL, error=NULL
               WHERE status='failed' AND attempts < ?""",
            (max_attempts,),
        ).rowcount


def stats(db):
    """返回总数、各状态数量和各平台总数。"""
    result = {"total": 0, "pending": 0, "claimed": 0, "done": 0, "failed": 0, "by_platform": {}}
    for platform, status, count in db.execute(
        "SELECT platform, status, COUNT(*) FROM queue GROUP BY platform, status ORDER BY platform"
    ):
        result["total"] += count
        result[status] += count
        result["by_platform"][platform] = result["by_platform"].get(platform, 0) + count
    return result


def unclaim_stale(db, timeout_minutes=30):
    """将超时未完成的已认领记录放回队列。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)).isoformat(timespec="microseconds")
    with db:
        return db.execute(
            """UPDATE queue SET status='pending', claimed_at=NULL, worker_id=NULL
               WHERE status='claimed' AND claimed_at < ?""",
            (cutoff,),
        ).rowcount


def is_seen(db, platform, object_type, content_id):
    """查询指定内容是否已经入队。"""
    return db.execute(
        "SELECT 1 FROM queue WHERE platform=? AND object_type=? AND content_id=?",
        (platform, object_type, content_id),
    ).fetchone() is not None
