"""Regression tests for WP0-WP3 of the public-account-profiles plan.

Covers the plan's section 6 scenarios relevant to this round:
- same platform + same account id, different nicknames -> one account
- same account id on different platforms -> never merged
- same nickname, different ids -> never merged
- nickname/profile updates -> no duplicate semantic analysis
- missing account id -> unknown, never invented
- legacy anonymized data imports keep working
- duplicate collection of the same comment -> no double interest accrual
- multi-platform source links validated per platform (A07)
"""
import json
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from content_screening.normalization import normalize, normalize_profile, public_url, read_export
from content_screening.pipeline import fingerprint
from content_screening.workflow import run, connect
from content_screening.account_repository import account_key, search_accounts, account_overview

POLICY = ROOT / "profiles/default.json"


def pub_row(platform="bilibili", cid="p1", aid="42", nick="UP主", text="开源Agent讨论，找搭子",
            published="2026-10-30T00:00:00+00:00"):
    return {"platform": platform, "object_type": "post",
            "data": {"content_id": cid, "text": text, "author_id": aid, "author": nick,
                     "author_profile_url": f"https://space.bilibili.com/{aid}" if platform == "bilibili" else "",
                     "anonymized": False, "published_at": published, "published_time_text": "", "url": ""}}


def anon_row(cid="c1", chash="abc", text="匿名内容示例活动"):
    return {"object_type": "comment",
            "data": {"comment_id": cid, "aweme_id": "1", "content": text, "parent_comment_id": "",
                     "creator_hash": chash, "nickname": "示***例", "create_time": 1793352000}}


class PublicAccountNormalizationTests(unittest.TestCase):
    def test_same_id_different_platforms_never_merge(self):
        bili = normalize(pub_row(platform="bilibili"))
        xhs = normalize(pub_row(platform="xhs", aid="42", nick="同名不同人"))
        self.assertEqual(bili["author_key"], "bilibili:42")
        self.assertEqual(xhs["author_key"], "xhs:42")
        self.assertNotEqual(bili["author_key"], xhs["author_key"])

    def test_same_nickname_different_ids_never_merge(self):
        a = normalize(pub_row(aid="1", nick="同一个名字"))
        b = normalize(pub_row(cid="p2", aid="2", nick="同一个名字"))
        self.assertEqual(a["author_key"], "bilibili:1")
        self.assertEqual(b["author_key"], "bilibili:2")
        self.assertNotEqual(a["author_key"], b["author_key"])

    def test_missing_id_stays_unknown_and_never_invented(self):
        row = pub_row(aid="", nick="随便起的名字")
        row["data"]["author_id"] = ""
        result = normalize(row)
        self.assertEqual(result["author_key"], "")
        self.assertEqual(result["identity_mode"], "unknown")
        self.assertIsNone(normalize_profile(row))

    def test_nickname_update_keeps_semantic_fingerprint(self):
        a = normalize(pub_row(nick="旧名字"))
        b = normalize(pub_row(nick="新名字"))
        self.assertEqual(fingerprint(a), fingerprint(b))
        pa, pb = normalize_profile(pub_row(nick="旧名字")), normalize_profile(pub_row(nick="新名字"))
        self.assertEqual(pa["nickname"], "旧名字")
        self.assertEqual(pb["nickname"], "新名字")

    def test_profile_url_update_keeps_semantic_fingerprint(self):
        row_b = pub_row(); row_b["data"]["author_profile_url"] = "https://space.bilibili.com/other"
        self.assertEqual(fingerprint(normalize(pub_row())), fingerprint(normalize(row_b)))

    def test_legacy_anonymized_record_normalizes_byte_identically(self):
        result = normalize(anon_row())
        expected = {"platform": "douyin", "object_type": "comment", "content_id": "c1", "text": "匿名内容示例活动",
                    "author": "示***例", "author_key": "anonymous:abc", "author_url": "",
                    "identity_mode": "upstream_anonymized", "url": "https://www.douyin.com/video/1",
                    "root_id": "1", "parent_id": "", "published_at": "2026-10-30T09:20:00+00:00",
                    "published_time_text": "1793352000", "normalization_version": "1",
                    "text_status": "available", "synthetic": False}
        self.assertEqual(result, expected)

    def test_public_url_per_platform_validation(self):
        # A07: cross-platform links validated against their own platform hosts
        self.assertEqual(public_url("https://space.bilibili.com/42", "bilibili"), "https://space.bilibili.com/42")
        self.assertEqual(public_url("https://space.bilibili.com/42", "xhs"), "")
        self.assertEqual(public_url("https://www.zhihu.com/people/tok", "zhihu"), "https://www.zhihu.com/people/tok")
        self.assertEqual(public_url("javascript:alert(1)", "bilibili"), "")

    def test_tokens_never_reach_normalized_output(self):
        row = pub_row()
        row["data"]["xsec_token"] = "never-persist-token"
        result = normalize(row)
        self.assertNotIn("never-persist-token", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("xsec", json.dumps(result))


class AccountRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "db.sqlite"
        self._connections = []
        self.addCleanup(self._close_connections)

    def _close_connections(self):
        for db in self._connections:
            try:
                db.close()
            except sqlite3.Error:
                pass

    def _db(self):
        db = connect(self.db_path)
        self._connections.append(db)
        return db

    def _import(self, rows):
        source = self.root / f"in{len(list(self.root.glob('in*.jsonl')))}.jsonl"
        source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return run(source, POLICY, self.db_path, self.root / "out", mode="rules")

    def test_same_account_multiple_contents_aggregate(self):
        self._import([pub_row(cid="p1", aid="42", nick="UP主"), pub_row(cid="p2", aid="42", nick="UP主")])
        db = self._db()
        accounts = db.execute("SELECT account_key FROM accounts").fetchall()
        self.assertEqual(len(accounts), 1)
        links = db.execute("SELECT content_id FROM account_content_links").fetchall()
        self.assertEqual(sorted(l["content_id"] for l in links), ["p1", "p2"])

    def test_text_change_triggers_reanalysis(self):
        # Inverse of the rename test: when the actual content text changes the
        # semantic fingerprint must change, so a NEW analysis row is produced.
        # Otherwise the cache optimization would serve stale results.
        self._import([pub_row(cid="p1", aid="42", text="开源Agent讨论，找搭子")])
        self._import([pub_row(cid="p1", aid="42", text="完全不同的正文，讲的是别的话题")])
        db = self._db()
        sources = db.execute("SELECT count(*) FROM v2_sources").fetchone()[0]
        self.assertEqual(sources, 2)  # two distinct semantic records
        analyses = db.execute("SELECT count(*) FROM v2_analyses").fetchone()[0]
        self.assertEqual(analyses, 2)  # both were analyzed, no stale reuse
        accounts = db.execute("SELECT account_key FROM accounts").fetchall()
        self.assertEqual(len(accounts), 1)  # same account either way

    def test_rename_updates_account_without_reanalysis(self):
        self._import([pub_row(cid="p1", aid="42", nick="旧名字")])
        db = self._db()
        before = (db.execute("SELECT count(*) FROM v2_sources").fetchone()[0],
                  db.execute("SELECT count(*) FROM v2_analyses").fetchone()[0],                  sorted(r[0] for r in db.execute("SELECT source_hash FROM v2_sources")))
        self._import([pub_row(cid="p1", aid="42", nick="新名字")])
        after = (db.execute("SELECT count(*) FROM v2_sources").fetchone()[0],
                 db.execute("SELECT count(*) FROM v2_analyses").fetchone()[0],
                 sorted(r[0] for r in db.execute("SELECT source_hash FROM v2_sources")))
        self.assertEqual(before, after)  # zero re-analysis: cache intact
        account = db.execute("SELECT nickname FROM accounts WHERE account_key='bilibili:42'").fetchone()
        self.assertEqual(account["nickname"], "新名字")
        history = db.execute("SELECT nickname FROM account_profile_history WHERE account_key='bilibili:42' ORDER BY id").fetchall()
        self.assertEqual([h["nickname"] for h in history], ["旧名字", "新名字"])

    def test_duplicate_collection_does_not_double_link(self):
        # Same comment re-collected in a later batch: links stay unique.
        self._import([pub_row(cid="p1", aid="42")])
        self._import([pub_row(cid="p1", aid="42")])
        db = self._db()
        links = db.execute("SELECT count(*) FROM account_content_links").fetchone()[0]
        self.assertEqual(links, 1)

    def test_anonymous_rows_never_create_accounts(self):
        self._import([anon_row(cid="c1", chash="abc"), anon_row(cid="c2", chash="abc")])
        db = self._db()
        self.assertEqual(db.execute("SELECT count(*) FROM accounts").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM account_content_links").fetchone()[0], 0)

    def test_overview_and_search(self):
        self._import([pub_row(cid="p1", aid="42"), pub_row(cid="p2", aid="42")])
        db = self._db()
        overview = account_overview(db, "bilibili:42")
        self.assertEqual(overview["account"]["nickname"], "UP主")
        self.assertEqual(overview["content_count"], 2)
        self.assertEqual(overview["active_days"], 1)
        found, total = search_accounts(db, "UP主")
        self.assertEqual((total, found[0]["account_key"]), (1, "bilibili:42"))


class MigrationTests(unittest.TestCase):
    def test_migrate_rollback_and_reapply(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite"
            sqlite3.connect(db_path).close()
            for expect in ("迁移完成", "回滚完成", "迁移完成"):
                args = [sys.executable, "scripts/db_migrate.py", "--db", str(db_path)]
                if expect == "回滚完成":
                    args.append("--rollback")
                result = subprocess.run(args, capture_output=True, text=True)
                self.assertIn(expect, result.stdout, result.stderr)
            with closing(sqlite3.connect(db_path)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("accounts", tables)
                self.assertIn("interest_profiles", tables)

    def test_interrupted_migration_recovers_and_keeps_data(self):
        # P1: a half-created schema (migration interrupted midway) must be
        # repairable by re-running the migration, and existing v2_* rows must
        # survive untouched.
        import subprocess
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "db.sqlite"
            # Build a partially migrated database: full migration first, then
            # rollback, then simulate a crash by dropping only one table.
            sqlite3.connect(db_path).close()
            def migrate(*extra):
                return subprocess.run([sys.executable, "scripts/db_migrate.py",
                                       "--db", str(db_path), *extra],
                                      capture_output=True, text=True)
            self.assertIn("迁移完成", migrate().stdout)
            # Seed a row in a migrated table, then simulate an interrupted
            # migration where a later table is missing.
            with closing(sqlite3.connect(db_path)) as db:
                db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?)",
                           ("bilibili:42", "bilibili", "42", "UP主",
                            "https://space.bilibili.com/42", "platform_public_content_api",
                            "public_source", "2026-11-05T00:00:00+00:00",
                            "2026-11-05T00:00:00+00:00"))
                db.commit()
            with closing(sqlite3.connect(db_path)) as db:
                db.execute("DROP TABLE interest_evidence")
                db.commit()
            # Re-running the migration repairs the schema without harming data.
            self.assertIn("迁移完成", migrate().stdout)
            with closing(sqlite3.connect(db_path)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("interest_evidence", tables)  # repaired
                rows = db.execute("SELECT nickname FROM accounts").fetchall()
                self.assertEqual([r[0] for r in rows], ["UP主"])  # untouched


if __name__ == "__main__":
    unittest.main()
