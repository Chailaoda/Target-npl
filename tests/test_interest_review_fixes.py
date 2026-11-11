"""Regression tests for the independent source review of PR #1.

Each case reproduces a concrete finding instead of asserting a happy path.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from interest_fixture import make_fixture, public_row
from content_screening.db_access import _schema_state
from content_screening.interest_service import InterestService, analyze_text, define_topic


class ReviewFixesTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.fixture = make_fixture()
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.db_path = self.fixture.db_path

    def rows(self, *rows):
        self.fixture.import_rows(list(rows))

    def evidence_ids(self, topic):
        with closing(sqlite3.connect(self.db_path)) as db:
            return [r[0] for r in db.execute(
                "SELECT id FROM interest_evidence WHERE topic=? ORDER BY id", (topic,))]

    # P0: one source_hash may be evidence for several topics; id is a global PK.
    def test_same_content_can_be_evidence_for_multiple_topics(self):
        self.rows(public_row("shared1", "A", "我喜欢胶片摄影，也经常去徒步露营，分享装备清单和实践经验。"))
        film = self.service.analyze("胶片摄影")
        hike = self.service.analyze("徒步露营")
        self.assertEqual(film["status"], "completed")
        self.assertEqual(hike["status"], "completed")
        self.assertEqual(film["items"][0]["evidence_count"], 1)
        self.assertEqual(hike["items"][0]["evidence_count"], 1)
        film_evidence = self.service.evidence("bilibili:A", "胶片摄影")["items"]
        hike_evidence = self.service.evidence("bilibili:A", "徒步露营")["items"]
        self.assertEqual(len(film_evidence), 1)
        self.assertEqual(len(hike_evidence), 1)
        self.assertNotEqual(film_evidence[0]["id"], hike_evidence[0]["id"])
        self.assertEqual(self.service.analyze("胶片摄影")["items"][0]["evidence_count"], 1)

    # P1: min_score must always mean interest_score, regardless of ORDER BY.
    def test_min_score_is_independent_of_sort_order(self):
        self.rows(public_row("a1", "A", "我喜欢胶片摄影，使用胶片相机记录显影参数与拍摄经验。"),
                  public_row("a2", "A", "胶片摄影的色彩对比值得研究，我分享扫描的实践经验。"),
                  public_row("a3", "A", "我喜欢胶片摄影，刚完成底片冲洗，记录我的实践。"),
                  public_row("b1", "B", "路过看到胶片摄影展览。"))
        self.service.analyze("胶片摄影")
        baseline = {i["account_key"] for i in self.service.search("胶片摄影", min_score=20)["items"]}
        self.assertEqual(baseline, {"bilibili:A"})
        for sort in ("score", "evidence", "recent"):
            with self.subTest(sort=sort):
                result = self.service.search("胶片摄影", min_score=20, sort=sort)
                self.assertEqual({i["account_key"] for i in result["items"]}, baseline)
                self.assertEqual(result["total"], len(baseline))

    # P1: A -> B -> A must query A, not the most recently created definition B.
    def test_reanalyzing_earlier_definition_becomes_the_active_version(self):
        self.rows(public_row("a1", "A", "我喜欢胶片摄影，分享拍摄实践经验。"))
        self.service.analyze("胶片摄影", aliases=["胶片相机"])
        self.service.analyze("胶片摄影", aliases=["底片摄影"])
        self.service.analyze("胶片摄影", aliases=["胶片相机"])
        status = self.service.topic_status("胶片摄影")
        self.assertEqual(status["definition"]["aliases"], ["胶片相机"])
        search = self.service.search("胶片摄影")
        self.assertEqual(search["definition"]["aliases"], ["胶片相机"])
        self.assertEqual(search["definition"]["profile_version"], status["definition"]["profile_version"])
        self.assertEqual(self.service.evidence("bilibili:A", "胶片摄影")["items"][0]["profile_version"],
                         status["definition"]["profile_version"])

    # P1: old-version evidence must not leak into current evidence or feedback.
    def test_stale_definition_evidence_is_not_visible_or_reusable(self):
        self.rows(public_row("x1", "A", "路过看到胶片摄影展览，我分享一些实践经验。"))
        first = self.service.analyze("胶片摄影")
        self.assertEqual(first["items"][0]["evidence_count"], 1)
        old_version = first["definition"]["profile_version"]
        second = self.service.analyze("胶片摄影", exclude_terms=["路过"])
        self.assertEqual(second["definition"]["profile_version"] != old_version, True)
        self.assertEqual(self.service.search("胶片摄影", min_evidence=0)["items"][0]["evidence_count"], 0)
        self.assertEqual(self.service.evidence("bilibili:A", "胶片摄影")["items"], [])
        stale_ids = self.evidence_ids("胶片摄影")
        self.assertEqual(len(stale_ids), 0)
        # Feedback on an already-visible id cannot resurrect stale evidence.
        if stale_ids:
            with self.assertRaises(ValueError):
                self.service.feedback(stale_ids[0], "irrelevant")
        self.assertEqual(self.service.evidence("bilibili:A", "胶片摄影")["items"], [])

    # P1: window metrics cached at analysis time must not disagree with detail view.
    def test_window_metrics_agree_after_time_advances(self):
        self.rows(public_row("x1", "A", "我喜欢胶片摄影，这是我的拍摄经验。", "2026-11-09T00:00:00+00:00"))
        self.service.analyze("胶片摄影")
        self.assertEqual(self.service.search("胶片摄影", window=30)["items"][0]["evidence_count"], 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute("SELECT id,details_json FROM interest_evidence").fetchall():
                item = json.loads(row["details_json"])
                item["published_at"] = "2026-01-01T00:00:00+00:00"
                item["published_display"] = item["published_at"]
                db.execute("UPDATE interest_evidence SET published_at=?,details_json=? WHERE id=?",
                           (item["published_at"], json.dumps(item, ensure_ascii=False), row["id"]))
            db.commit()
        listed = self.service.search("胶片摄影", window=30)
        listed_count = listed["items"][0]["evidence_count"] if listed["items"] else 0
        detail_count = self.service.evidence("bilibili:A", "胶片摄影", window=30)["evidence_count"]
        self.assertEqual(listed_count, detail_count)
        self.assertEqual(listed["total"], detail_count > 0)

    # P2: stance must be derived from the clause containing the topic.
    def test_stance_is_scoped_to_the_topic_clause(self):
        film = define_topic("摄影")
        self.assertEqual(analyze_text({"text": "我喜欢摄影，但讨厌露营，不推荐跟风。"}, film)["stance"], "positive")
        self.assertEqual(analyze_text({"text": "我讨厌摄影，但喜欢露营，推荐大家去。"}, film)["stance"], "negative")
        self.assertEqual(analyze_text({"text": "我不喜欢摄影，也不推荐。"}, film)["stance"], "negative")

    # P2: a failed analysis must persist its status outside the rolled-back transaction.
    def test_failed_analysis_status_survives_rollback(self):
        self.rows(public_row("a1", "A", "我喜欢胶片摄影，分享经验。"))
        with patch.object(InterestService, "_save_profile", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.service.analyze("胶片摄影")
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute("SELECT status,error FROM interest_topic_definitions WHERE topic_key='胶片摄影'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "failed")
        self.assertIn("synthetic failure", row[1])

    # P2: preflight must reject a WP3 database missing WP4 tables.
    def test_schema_preflight_requires_wp4_tables(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(_schema_state(db), "migrated")
            db.execute("DROP TABLE interest_topic_definitions")
            db.commit()
            self.assertEqual(_schema_state(db), "migration_required")


if __name__ == "__main__":
    unittest.main()
