"""Loopback acceptance for the conflict/review HTTP contract (WP6-T1)."""
import json
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from interest_fixture import make_fixture, public_row
from content_screening.control_server import RuleStore, make_server


class ConflictReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.fixture = make_fixture()
        self.addCleanup(self.fixture.close)
        self.root = self.fixture.root
        self.db_path = self.fixture.db_path
        for relative in ("profiles/default.json", "config/collection.json"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
        self.fixture.import_rows([
            public_row("a1", "A", "我喜欢胶片摄影，使用胶片相机记录显影参数。"),
            public_row("b9", "B", "我记录暗房冲洗时间，欢迎交流。"),
        ])
        self.add_conflict()
        self.server = make_server(0, RuleStore(self.root), context=self.fixture.context)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.opener.open(self.base + "/api/tasks/state") as response:
            self.token = json.load(response)["token"]

    def add_conflict(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("""INSERT INTO account_attribution_conflicts(
                platform,object_type,content_id,current_account_key,candidate_account_key,
                current_source_hash,candidate_source_hash,candidate_authority,status,resolution,
                first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,'pending','',?,?)
                ON CONFLICT DO NOTHING""",
                       ("bilibili", "post", "a1", "bilibili:A", "bilibili:B", "", "",
                        "platform_recheck", "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"))

    def get(self, path):
        return json.load(self.opener.open(self.base + path))

    def post(self, path, payload, token=True):
        headers = {"Origin": self.base, "Content-Type": "application/json"}
        if token:
            headers["X-Control-Token"] = self.token
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with self.opener.open(request) as response:
            return json.load(response)

    def status_of(self, content_id):
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute("""SELECT account_key,attribution_authority FROM account_content_links
                WHERE platform='bilibili' AND object_type='post' AND content_id=?""", (content_id,)).fetchall()
        return rows

    def test_pending_conflicts_are_listed_with_snippet_and_authority(self):
        body = self.get("/api/accounts/conflicts?status=pending&limit=10")
        self.assertEqual(body["total_pending"], 1)
        item = body["items"][0]
        self.assertEqual((item["current_key"], item["candidate_key"]), ("bilibili:A", "bilibili:B"))
        self.assertEqual(item["candidate_authority"], "platform_recheck")
        self.assertIn("胶片摄影", item["snippet"])
        self.assertIn(item["current_authority"], {"raw_import", ""})

    def test_invalid_status_and_limit_are_rejected(self):
        for query in ("?status=nope", "?limit=0", "?limit=999"):
            with self.subTest(query=query):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.get("/api/accounts/conflicts" + query)
                self.assertEqual(caught.exception.code, 400)

    def test_resolve_requires_token_and_origin(self):
        payload = {"platform": "bilibili", "object_type": "post", "content_id": "a1",
                   "chosen_key": "bilibili:B"}
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/accounts/conflicts/resolve", payload, token=False)
        self.assertEqual(caught.exception.code, 403)

    def test_unknown_chosen_key_is_rejected_without_writing(self):
        before = self.status_of("a1")
        payload = {"platform": "bilibili", "object_type": "post", "content_id": "a1",
                   "chosen_key": "bilibili:ZZZ"}
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/accounts/conflicts/resolve", payload)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.status_of("a1"), before)
        self.assertEqual(self.get("/api/accounts/conflicts?status=pending")["total_pending"], 1)

    def test_resolution_is_manual_review_and_survives_import_replay(self):
        payload = {"platform": "bilibili", "object_type": "post", "content_id": "a1",
                   "chosen_key": "bilibili:B", "note": "人工核对主页"}
        body = self.post("/api/accounts/conflicts/resolve", payload)
        self.assertTrue(body["resolved"])
        self.assertEqual(body["authority"], "manual_review")
        self.assertEqual(self.status_of("a1"), [("bilibili:B", "manual_review")])
        self.assertEqual(self.get("/api/accounts/conflicts?status=pending")["total_pending"], 0)

        # INV-02: replaying a raw_import batch must not revert the confirmed owner.
        self.fixture.import_rows([public_row("a1", "A", "我喜欢胶片摄影，使用胶片相机记录显影参数。")])
        self.assertEqual(self.status_of("a1"), [("bilibili:B", "manual_review")])

    def test_unknown_field_is_rejected(self):
        payload = {"platform": "bilibili", "object_type": "post", "content_id": "a1",
                   "chosen_key": "bilibili:B", "authority": "raw_import"}
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/accounts/conflicts/resolve", payload)
        self.assertEqual(caught.exception.code, 400)

    def analysis_key(self):
        with sqlite3.connect(self.db_path) as db:
            return db.execute("SELECT analysis_key FROM v2_analyses LIMIT 1").fetchone()[0]

    def test_review_batch_writes_decisions_and_rejects_unknown_keys(self):
        key = self.analysis_key()
        body = self.post("/api/reviews", {"items": [{"analysis_key": key, "decision": "accept", "note": "确认"}]})
        self.assertEqual(body["updated"], 1)
        self.assertEqual(body["results"][0]["decision"], "accept")
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT decision FROM v2_reviews WHERE analysis_key=?", (key,)).fetchone()[0], "confirm")
            history = db.execute("SELECT count(*) FROM v2_review_history WHERE analysis_key=?", (key,)).fetchone()[0]
        self.assertEqual(history, 1)

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/reviews", {"items": [{"analysis_key": "f" * 64, "decision": "accept"}]})
        self.assertEqual(caught.exception.code, 400)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM v2_review_history").fetchone()[0], history)

    def test_review_rejects_invalid_decision_and_shape(self):
        key = self.analysis_key()
        for payload in ({"items": [{"analysis_key": key, "decision": "maybe"}]},
                        {"items": []},
                        {"items": [{"analysis_key": key, "decision": "accept", "extra": 1}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.post("/api/reviews", payload)
                self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
