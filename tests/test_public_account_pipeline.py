"""Format rehearsal for the real public-account trial. NOT real people.

Uses the exact field shape emitted by the controlled MediaCrawler fork
(aweme_id/desc/create_time plus author_id/author_nickname/author_profile_url)
to prove that the existing import -> account identity -> WP4 profile ->
evidence -> feedback chain accepts it before any online collection.
Every row is synthetic and explicitly tagged as a format probe; no anonymous
source is ever reverse-mapped to an account.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from interest_fixture import make_fixture


def fork_post(aweme_id, author_id, text, published, *, nickname=None):
    data = {"aweme_id": aweme_id, "desc": text,
            "create_time": str(int(published.timestamp())),
            "author_id": author_id, "author_nickname": nickname or f"公开账号{author_id}",
            "author_profile_url": f"https://www.douyin.com/user/{author_id}",
            "aweme_url": f"https://www.douyin.com/video/{aweme_id}",
            "creator_hash": f"legacy-hash-{aweme_id}", "nickname": "masked",
            "synthetic": True, "format_probe": True}
    return {"platform": "douyin", "object_type": "post", "data": data}


def fork_comment(comment_id, root_id, author_id, text, published):
    data = {"aweme_id": root_id, "comment_id": comment_id, "parent_comment_id": "0",
            "content": text, "create_time": str(int(published.timestamp())),
            "author_id": author_id, "author_nickname": f"公开账号{author_id}",
            "author_profile_url": f"https://www.douyin.com/user/{author_id}",
            "creator_hash": f"legacy-hash-{comment_id}", "nickname": "masked",
            "synthetic": True, "format_probe": True}
    return {"platform": "douyin", "object_type": "comment", "data": data}


def anonymous_post(aweme_id, text, published):
    """Legacy upstream output without a stable author id."""
    data = {"aweme_id": aweme_id, "desc": text,
            "create_time": str(int(published.timestamp())),
            "creator_hash": f"anon-{aweme_id}", "nickname": "masked",
            "anonymized": True, "synthetic": True, "format_probe": True}
    return {"platform": "douyin", "object_type": "post", "data": data}


class PublicAccountPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.fixture = make_fixture()
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.db_path = self.fixture.db_path
        self.recent = datetime.now(timezone.utc) - timedelta(days=3)
        self.older = datetime.now(timezone.utc) - timedelta(days=90)

    def accounts(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM accounts ORDER BY account_key")]

    def test_controlled_fork_fields_form_real_account_keys_and_evidence(self):
        rows = [
            fork_post("7500000000000000001", "user_001",
                       "我喜欢胶片摄影，这次用胶片相机拍了一卷，记录显影参数和拍摄经验。",
                       self.older, nickname="胶片甲"),
            fork_post("7500000000000000002", "user_001",
                       "胶片摄影的底片扫描对比，我分享这次的实测结果和实践经验。", self.recent,
                       nickname="胶片甲"),
            fork_comment("7500000000000000101", "7500000000000000001", "user_002",
                         "胶片摄影的色彩确实值得研究，我也分享过显影经验。", self.recent),
            fork_post("7500000000000000003", "user_002",
                       "我批评胶片摄影的炒作，不推荐跟风消费。", self.recent),
            anonymous_post("7500000000000000004", "我喜欢胶片摄影，但这条没有稳定作者 ID。", self.recent),
        ]
        self.fixture.import_rows(rows)
        accounts = self.accounts()
        self.assertEqual([a["account_key"] for a in accounts], ["douyin:user_001", "douyin:user_002"])
        self.assertTrue(all(a["identity_mode"] == "public_source" for a in accounts))
        self.assertEqual(accounts[0]["nickname"], "胶片甲")
        self.assertEqual(accounts[0]["profile_url"], "https://www.douyin.com/user/user_001")
        with closing(sqlite3.connect(self.db_path)) as db:
            links = db.execute("SELECT count(*) FROM account_content_links").fetchone()[0]
            analyses = db.execute("SELECT count(*) FROM v2_analyses WHERE result_json IS NOT NULL AND result_json<>''").fetchone()[0]
        self.assertGreaterEqual(links, 4)
        self.assertGreater(analyses, 0)

        result = self.service.analyze("胶片摄影", aliases=["胶片相机"])
        self.assertEqual(result["status"], "completed")
        by_key = {i["account_key"]: i for i in result["items"]}
        self.assertIn("douyin:user_001", by_key)
        self.assertIn("douyin:user_002", by_key)
        self.assertGreater(by_key["douyin:user_001"]["interest_score"], 0)

        detail = self.service.evidence("douyin:user_001", "胶片摄影")
        self.assertTrue(detail["items"])
        self.assertTrue(all(e["quote"] in e["full_text"] for e in detail["items"]))
        self.assertTrue(all(e["source_url"].startswith("https://") for e in detail["items"]))
        self.assertTrue(all(e["platform"] == "douyin" for e in detail["items"]))
        self.assertTrue(detail["profile_version"])

        evidence_id = detail["items"][0]["id"]
        changed = self.service.feedback(evidence_id, "irrelevant", "格式验证：人工标记",
                                        profile_version=detail["profile_version"])
        film = next(i for i in changed["interests"] if i["topic"] == "胶片摄影")
        self.assertLess(film["evidence_count"], by_key["douyin:user_001"]["evidence_count"])

    def test_same_author_id_on_two_platforms_never_merges(self):
        rows = [
            fork_post("7500000000000000011", "shared_id", "我喜欢古典音乐，常听巴赫并比较演奏版本。", self.recent),
            {"platform": "bilibili", "object_type": "post",
             "data": {"content_id": "BV1format0001", "text": "我喜欢古典音乐，常听巴赫并比较演奏版本。",
                      "author_id": "shared_id", "author": "另一个平台同名 ID",
                      "author_profile_url": "https://space.bilibili.com/shared_id",
                      "url": "https://www.bilibili.com/video/BV1format0001",
                      "published_at": self.recent.isoformat(), "synthetic": True, "format_probe": True}},
            anonymous_post("7500000000000000012", "我喜欢古典音乐，但采集源只给了匿名标识。", self.recent),
        ]
        self.fixture.import_rows(rows)
        keys = {a["account_key"] for a in self.accounts()}
        self.assertEqual(keys, {"douyin:shared_id", "bilibili:shared_id"})
        result = self.service.analyze("古典音乐")
        self.assertEqual({i["account_key"] for i in result["items"]}, keys)
        self.assertEqual(self.service.accounts()["total"], 2)
    def test_receipt_path_is_anchored_to_project_after_vendor_chdir(self):
        import os
        from scripts.mediacrawler_collect import publish_result
        previous = Path.cwd()
        target = ROOT / "output/collections/public-account-trial-receipt-path-test.json"
        self.addCleanup(target.unlink, missing_ok=True)
        try:
            os.chdir(ROOT / "vendor/MediaCrawler-controlled")
            publish_result({"status": "completed", "records": 0},
                           target.relative_to(ROOT).as_posix())
        finally:
            os.chdir(previous)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "completed")


if __name__ == "__main__":
    unittest.main()
