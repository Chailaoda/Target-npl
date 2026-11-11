"""WP4-WP5 acceptance on synthetic accounts; no production database access."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from content_screening.account_repository import migrate
from content_screening.interest_service import InterestService, define_topic, analyze_text
from content_screening.pipeline import fingerprint
from content_screening.workflow import run
from content_screening.runtime_context import RuntimeContext, set_context, clear_context


def public_row(cid, account_id, text, published="2026-11-01T00:00:00+00:00", platform="bilibili", **extra):
    data = {"content_id": cid, "text": text, "author_id": account_id, "author": "公开账号",
            "author_profile_url": f"https://space.bilibili.com/{account_id}", "anonymized": False,
            "published_at": published, "url": f"https://www.bilibili.com/video/{cid}", "synthetic": True, **extra}
    return {"platform": platform, "object_type": "post", "data": data}


class InterestEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "tmp", ignore_cleanup_errors=True)
        self.tmp.cleanup = lambda: None
        self.tmp._finalizer.detach()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "data/festival.sqlite3"
        self.db_path.parent.mkdir()
        sqlite3.connect(self.db_path).close()
        from content_screening.db_access import V2_SCHEMA, LEGACY_SCHEMA
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(V2_SCHEMA + LEGACY_SCHEMA)
            db.execute("ALTER TABLE v2_jobs ADD COLUMN task_json TEXT")
            db.execute("ALTER TABLE v2_jobs ADD COLUMN config_revision TEXT")
            migrate(db)
        self.context = RuntimeContext(self.root / "deployment.json", ROOT, self.root,
                                      {"test_isolated": True, "database": {"relative_path": "data/festival.sqlite3"}})
        set_context(self.context)
        self.addCleanup(clear_context)
        self.service = InterestService(self.context)

    def import_rows(self, rows):
        source = self.root / f"source-{len(list(self.root.glob('*.jsonl')))}.jsonl"
        source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return run(source, ROOT / "profiles/default.json", self.db_path, self.root / "out", mode="rules")

    def test_six_accounts_and_three_unrelated_topics(self):
        rows = [
            public_row("a1", "A", "我喜欢胶片摄影，使用胶片相机拍摄并记录显影参数和实践经验。", "2026-10-01T00:00:00+00:00"),
            public_row("a4", "A", "我喜欢胶片摄影，这是近期的拍摄实践与显影记录。", "2026-11-10T00:00:00+00:00"),
            public_row("a2", "A", "胶片摄影的日常：对比底片和扫描效果，分享我的拍摄经验。", "2026-11-01T00:00:00+00:00"),
            public_row("a3", "A", "胶片摄影冲洗教程，我的实践参数与扫描流程。", "2026-11-08T00:00:00+00:00"),
            public_row("b1", "B", "路过看到胶片摄影展览。", "2026-08-01T00:00:00+00:00"),
            public_row("c1", "C", "我批评胶片摄影的炒作，反对这种浪费。", "2026-10-01T00:00:00+00:00"),
            public_row("c2", "C", "胶片摄影的价格让我反感，不推荐跟风。", "2026-11-02T00:00:00+00:00"),
            public_row("d1", "D", "我喜欢胶片摄影，持续分享拍摄经验。", "2026-08-01T00:00:00+00:00"),
            public_row("d1", "D", "我喜欢胶片摄影，持续分享拍摄经验。", "2026-08-01T00:00:00+00:00"),
            public_row("e1", "", "我喜欢胶片摄影，但只有昵称没有稳定 ID。"),
            public_row("f1", "F", "胶片摄影的旧拍摄记录，我的实践经验。", "2026-08-01T00:00:00+00:00"),
            public_row("f2", "F", "我喜欢胶片摄影，最近的显影实践。", "2026-11-09T00:00:00+00:00"),
            public_row("h1", "H", "我喜欢徒步露营，分享装备清单和路线经验。"),
            public_row("i1", "I", "我喜欢古典音乐，常听巴赫并记录演奏版本对比。"),
        ]
        self.import_rows(rows)
        result = self.service.analyze("胶片摄影", aliases=["胶片相机"])
        by_id = {item["account_id"]: item for item in result["items"]}
        self.assertGreater(by_id["A"]["interest_score"], by_id["B"]["interest_score"])
        self.assertEqual(by_id["C"]["stance"], "negative")
        self.assertEqual(by_id["D"]["evidence_count"], 1)
        self.assertNotIn("", by_id)
        self.assertGreater(by_id["F"]["interest_score"], 0)
        self.assertEqual(by_id["F"]["trend"], "rising")
        self.assertEqual(self.service.analyze("徒步露营")["items"][0]["account_id"], "H")
        self.assertEqual(self.service.analyze("古典音乐")["items"][0]["account_id"], "I")
        self.assertEqual(self.service.search("尚未分析")["status"], "not_analyzed")
        detail = self.service.account("bilibili:A")
        evidence = self.service.evidence("bilibili:A", "胶片摄影")
        self.assertTrue(detail["interests"])
        self.assertTrue(all(e["quote"] in e["full_text"] for e in evidence["items"]))
        self.assertTrue(all(e["synthetic"] is True for e in detail["interests"] if e["evidence_count"]))
        recent_items = self.service.search("胶片摄影", window=30)["items"]
        self.assertEqual({item["account_id"] for item in recent_items}, {"A", "C", "F"})

    def test_windows_incremental_feedback_and_identity(self):
        self.import_rows([public_row("x1", "A", "我喜欢胶片摄影，这是我的拍摄经验。", "2026-08-01T00:00:00+00:00"),
                          public_row("x2", "A", "我喜欢胶片摄影，这是近期拍摄经验。", "2026-11-09T00:00:00+00:00"),
                          public_row("x3", "A", "胶片摄影，但没有可信时间。", "")])
        self.service.analyze("胶片摄影")
        all_result = self.service.search("胶片摄影")
        self.assertEqual(all_result["items"][0]["evidence_count"], 3)
        recent = self.service.search("胶片摄影", window=30)
        self.assertEqual(recent["items"][0]["evidence_count"], 1)
        evidence = self.service.evidence("bilibili:A", "胶片摄影")
        unknown = next(e for e in evidence["items"] if not e["published_at"])
        self.assertEqual(unknown["published_display"], "时间未知")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            before = db.execute("SELECT count(*) FROM interest_content_analysis").fetchone()[0]
            source_count = db.execute("SELECT count(*) FROM v2_sources").fetchone()[0]
        self.service.analyze("胶片摄影")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM interest_content_analysis").fetchone()[0], before)
            self.assertEqual(db.execute("SELECT count(*) FROM v2_sources").fetchone()[0], source_count)
        evidence_id = self._evidence_id("x1")
        version = self.service.evidence("bilibili:A", "胶片摄影")["profile_version"]
        self.service.feedback(evidence_id, "irrelevant", "合成测试：标记不相关", profile_version=version)
        self.assertEqual(self.service.search("胶片摄影")["items"][0]["evidence_count"], 2)
        self.service.feedback(evidence_id, "attribution_question", "合成测试：待确认归属", profile_version=version)
        self.assertEqual(self.service.search("胶片摄影")["items"][0]["evidence_count"], 2)
        self.service.feedback(evidence_id, "irrelevant", "合成测试：恢复为不相关", profile_version=version)
        self.service.feedback(evidence_id, "restore", profile_version=version)
        self.assertEqual(self.service.search("胶片摄影")["items"][0]["evidence_count"], 3)
        self.service.analyze("胶片摄影")
        self.assertEqual(self.service.search("胶片摄影")["items"][0]["evidence_count"], 3)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT INTO account_attribution_conflicts(platform,object_type,content_id,current_account_key,candidate_account_key,status,first_seen_at,last_seen_at) VALUES ('bilibili','post','x1','bilibili:A','bilibili:OTHER','pending','now','now')")
            db.commit()
        self.service.analyze("胶片摄影")
        self.assertEqual(self.service.search("胶片摄影")["items"][0]["evidence_count"], 2)

    def _evidence_id(self, content_id):
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute("""SELECT e.id FROM interest_evidence e JOIN account_content_links l ON l.source_hash=e.source_hash
                WHERE l.content_id=?""", (content_id,)).fetchone()[0]

    def test_deterministic_score_exclusion_and_quarantine(self):
        record = {"text": "我不喜欢胶片摄影，不推荐。"}
        definition = define_topic("胶片摄影")
        self.assertEqual(analyze_text(record, definition)["stance"], "negative")
        self.assertEqual(define_topic("  胶片   摄影 ")["topic"], "胶片 摄影")
        self.assertEqual(analyze_text(record, define_topic("胶片摄影", exclude_terms=["不推荐"]))["decision"], "excluded")
        self.import_rows([public_row("a1", "A", "我喜欢胶片摄影，分享经验。")])
        first = self.service.analyze("胶片摄影")["items"][0]["interest_score"]
        second = self.service.analyze("胶片摄影")["items"][0]["interest_score"]
        self.assertEqual(first, second)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE v2_analyses SET result_json=?", ('{"engine":"quarantined_model_output"}',))
            db.commit()
        self.service.analyze("胶片摄影")
        self.assertEqual(self.service.search("胶片摄影")["items"], [])
        self.assertEqual(self.service.search("胶片摄影", min_evidence=0)["items"][0]["evidence_count"], 0)

    def test_thread_cap_and_window_boundaries(self):
        rows = [public_row(f"r{i}", "A", f"我喜欢胶片摄影，分享实践经验 {i}。", object_type="comment", root_id="same", parent_id="p") for i in range(12)]
        self.import_rows(rows)
        self.service.analyze("胶片摄影")
        evidence = self.service.evidence("bilibili:A", "胶片摄影")
        self.assertLessEqual(sum(e.get("contribution", 0) for e in evidence["items"]), 6)
        self.assertLess(evidence["interest_score"], 32)
        self.assertEqual(self.service.search("胶片摄影", window=90)["total"], 1)
        self.assertEqual(self.service.search("胶片摄影", platform="douyin")["total"], 0)

    def test_legacy_interest_tables_accept_wp4_writes(self):
        # Production WP3 tables lack the new columns and have only the original
        # (account_key,topic,analysis_key) unique constraint. An explicit
        # migration must still allow evidence/profile writes.
        self.import_rows([public_row("legacy1", "A", "我喜欢胶片摄影，分享拍摄经验。")])
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript("""
                DROP TABLE interest_evidence;
                DROP TABLE interest_profiles;
                CREATE TABLE interest_evidence(
                    id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, topic TEXT NOT NULL,
                    analysis_key TEXT NOT NULL, source_hash TEXT NOT NULL,
                    stance TEXT NOT NULL DEFAULT '', quote TEXT NOT NULL DEFAULT '',
                    observed_at TEXT NOT NULL, UNIQUE(account_key,topic,analysis_key));
                CREATE TABLE interest_profiles(
                    id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, topic TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0, content_count INTEGER NOT NULL DEFAULT 0,
                    active_days INTEGER NOT NULL DEFAULT 0, stance_summary TEXT NOT NULL DEFAULT '',
                    trend TEXT NOT NULL DEFAULT '', evidence_notes TEXT NOT NULL DEFAULT '',
                    profile_version TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(account_key,topic,profile_version));
            """)
            migrate(db)
        result = self.service.analyze("胶片摄影")
        self.assertEqual(result["items"][0]["evidence_count"], 1)
        self.assertEqual(self.service.evidence("bilibili:A", "胶片摄影")["items"][0]["quote"],
                         "我喜欢胶片摄影，分享拍摄经验。")


if __name__ == "__main__":
    unittest.main()
