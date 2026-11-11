"""Actual loopback HTTP acceptance for WP5."""
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from interest_fixture import make_fixture, public_row
from content_screening.control_server import make_server, RuleStore


class InterestApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.fixture = make_fixture()
        self.addCleanup(self.fixture.close)
        self.root = self.fixture.root
        self.context = self.fixture.context
        self.service = self.fixture.service
        for relative in ("profiles/default.json", "profiles/music_festival_v2.json", "config/collection.json"):
            target = self.root / relative
            target.parent.mkdir(exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
        self.fixture.import_rows([public_row("a1", "A", "我喜欢胶片摄影，使用胶片相机记录显影参数与拍摄经验。"),
                                  public_row("b1", "B", "偶然看到胶片摄影展览。"),
                                  public_row("c1", "C", "我批评胶片摄影的炒作，不推荐。")])
        self.server = make_server(0, RuleStore(self.root), context=self.context)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with self.opener.open(self.base + "/api/tasks/state") as response:
                self.token = json.load(response)["token"]
        except urllib.error.HTTPError as error:
            raise AssertionError(error.read().decode()) from error

    def get(self, route, **params):
        url = self.base + route + ("?" + urllib.parse.urlencode(params) if params else "")
        with self.opener.open(url) as response:
            return json.load(response)

    def post(self, route, body, token=None):
        request = urllib.request.Request(self.base + route, data=json.dumps(body, ensure_ascii=False).encode(),
                                         headers={"Origin": self.base, "Content-Type": "application/json",
                                                  "X-Control-Token": self.token if token is None else token})
        with self.opener.open(request) as response:
            return json.load(response)

    def test_full_http_flow_filters_details_evidence_and_auth(self):
        self.assertEqual(self.get("/api/interests/search", topic="新主题")["status"], "not_analyzed")
        result = self.post("/api/interests/analyze", {"topic": "胶片摄影", "aliases": ["胶片相机"]})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total"], 3)
        filtered = self.get("/api/interests/search", topic="胶片摄影", stance="negative", platform="bilibili", min_score=1)
        self.assertEqual([i["account_id"] for i in filtered["items"]], ["C"])
        first = self.get("/api/interests/search", topic="胶片摄影", limit=2, offset=0, sort="evidence")
        second = self.get("/api/interests/search", topic="胶片摄影", limit=2, offset=2, sort="evidence")
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 1)
        self.assertTrue({i["account_key"] for i in first["items"]}.isdisjoint({i["account_key"] for i in second["items"]}))
        account = self.get("/api/accounts/bilibili:A")
        self.assertEqual(account["interests"][0]["topic"], "胶片摄影")
        self.assertTrue(account["interests"][0]["synthetic"])
        interests = self.get("/api/accounts/bilibili:A/interests")
        self.assertEqual(interests["items"][0]["topic"], "胶片摄影")
        evidence = self.get("/api/accounts/bilibili:A/interests/" + urllib.parse.quote("胶片摄影") + "/evidence")
        self.assertTrue(evidence["items"])
        self.assertIsInstance(evidence["items"][0]["id"], int)
        self.assertGreater(evidence["items"][0]["contribution"], 0)
        self.assertIn(evidence["items"][0]["quote"], evidence["items"][0]["full_text"])
        with self.assertRaises(urllib.error.HTTPError) as forbidden:
            self.post("/api/interests/analyze", {"topic": "徒步"}, token="invalid")
        self.assertEqual(forbidden.exception.code, 403)
        saved = self.post("/api/interests/evidence-feedback", {"evidence_id": evidence["items"][0]["id"],
                                                               "action": "irrelevant", "note": "合成测试反馈"})
        self.assertEqual(saved["interests"][0]["evidence_count"], 0)
        self.assertEqual(self.get("/api/accounts")["total"], 3)
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.get("/api/accounts/bilibili:missing")
        self.assertEqual(missing.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
