"""Incremental DB migration for the public-account-profiles upgrade (WP3).

Forward:  creates account/interest tables (all IF NOT EXISTS — safe to rerun).
Rollback: drops the new tables only; v2_* tables and all legacy data are never
touched by either direction, so the previous code version keeps working on a
migrated database (new tables are transparent to it).

Usage:
  python scripts/db_migrate.py [--db data/screening.sqlite3] [--rollback]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from content_screening.account_repository import SCHEMA, migrate  # noqa: E402

NEW_TABLES = ("accounts", "account_profile_history", "account_content_links",
              "interest_evidence", "interest_profiles", "interest_profile_versions")


def rollback(db):
    for table in NEW_TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")
    db.execute("DROP INDEX IF EXISTS idx_content_links_source")
    db.execute("DROP INDEX IF EXISTS idx_evidence_account_topic")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data/screening.sqlite3"))
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    db = sqlite3.connect(args.db)
    try:
        if args.rollback:
            with db:
                rollback(db)
            print("回滚完成：已删除新增账号/画像表；v2_* 原表未动。")
        else:
            with db:
                migrate(db)
            present = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            missing = [t for t in NEW_TABLES if t not in present]
            if missing:
                raise RuntimeError(f"迁移后仍缺表: {missing}")
            print("迁移完成：账号/画像表就绪（幂等，可重复执行）。")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
