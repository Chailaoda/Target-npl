"""Server-side observability: error ids, redacted request log (WP7-T4)."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from content_screening.control_server import RuleStore, make_server
from content_screening.runtime_context import RuntimeContext, clear_context, set_context

ROOT = Path(__file__).resolve().parents[1]


class ServerLoggingTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp/tests").mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=ROOT / "tmp/tests"))
        self.addCleanup(self._remove_tree)
        for name in ("profiles/default.json", "config/collection.json"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        self.store = RuleStore(self.root)
        self.context = RuntimeContext(self.root / "deployment.json", ROOT, self.root,
                                      {"test_isolated": True, "database": {"relative_path": "data/festival.sqlite3"}})
        set_context(self.context)
        self.addCleanup(clear_context)
        self.server = make_server(0, self.store, context=self.context)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.opener.open(self.base + "/api/state") as response:
            state = json.load(response)
        self.token = state["token"]
        self.bundle = state["bundle"]
        self.log = self.root / "output/control/server.log"

    def _remove_tree(self):
        for path in sorted(self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                path.rmdir() if path.is_dir() else path.unlink()
            except OSError:
                pass
        try:
            self.root.rmdir()
        except OSError:
            pass

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Origin": self.base, "Content-Type": "application/json", "X-Control-Token": self.token})
        return self.opener.open(request)

    def test_unhandled_error_returns_error_id_and_writes_the_stack(self):
        with patch("content_screening.http_api.handlers_task.analysis_policy", side_effect=RuntimeError("boom")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("/api/validate", {"bundle": self.bundle})
        self.assertEqual(caught.exception.code, 500)
        body = json.loads(caught.exception.read())
        self.assertIn("error_id", body)
        err_id = body["error_id"]
        self.assertIn(err_id, body["error"])
        text = self.log.read_text(encoding="utf-8")
        self.assertIn(err_id, text)
        self.assertIn("boom", text)

    def test_log_never_records_the_control_token(self):
        with patch("content_screening.http_api.handlers_task.analysis_policy", side_effect=RuntimeError("boom")):
            with self.assertRaises(urllib.error.HTTPError):
                self.post("/api/validate?token=%s" % self.token, {"bundle": self.bundle})
        text = self.log.read_text(encoding="utf-8")
        self.assertNotIn(self.token, text)
        self.assertNotIn("X-Control-Token", text)

    def test_successful_reads_are_not_logged(self):
        before = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        self.opener.open(self.base + "/api/state").read()
        after = self.log.read_text(encoding="utf-8")
        self.assertEqual(before, after, "successful GETs must not churn the log")

    def test_failed_requests_are_logged_without_query_string(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.opener.open(self.base + "/api/accounts/conflicts?status=bad")
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("/api/accounts/conflicts", text)
        self.assertNotIn("status=bad", text)


if __name__ == "__main__":
    unittest.main()
