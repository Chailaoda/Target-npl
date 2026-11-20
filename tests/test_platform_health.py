"""Offline pre-flight health for the seven platforms (WP6-T4)."""
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from content_screening.platforms import catalog, health


class PlatformHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        run = self.root / "data/task-runs" / ("a" * 32)
        run.mkdir(parents=True)
        (run / "status.json").write_text(json.dumps({
            "run_id": "a" * 32, "status": "running", "created_at": "2026-11-20T00:00:00+00:00",
            "active_platform": "xhs", "dataset_id": ""
        }), encoding="utf-8")
        budget = self.root / "data/budget"
        budget.mkdir(parents=True)
        (budget / "2026-11-20.json").write_text(json.dumps({
            "date": "2026-11-20", "api_calls": 37, "collection_posts": 12,
            "by_platform": {"xhs": 12}, "rounds": 1
        }), encoding="utf-8")

    def test_last_lane_state_is_read_from_local_run_artifacts(self):
        items = {item["id"]: item for item in catalog(self.root)}
        self.assertEqual(len(items), 7)
        self.assertEqual(items["xhs"]["last_status"], "running")
        self.assertEqual(items["xhs"]["last_run_at"], "2026-11-20T00:00:00+00:00")
        self.assertFalse(items["xhs"]["needs_login"])
        self.assertEqual(items["douyin"]["last_status"], "")

    def test_health_exposes_platforms_budget_and_vendor_preflight(self):
        body = health(self.root)
        self.assertEqual(len(body["platforms"]), 7)
        self.assertEqual(set(body["budget"]), {"posts_used", "posts_limit", "api_calls_used", "api_calls_limit"})
        self.assertIn("ok", body["vendor_preflight"])
        self.assertTrue(body["budget"]["api_calls_limit"] > 0)

    def test_health_never_opens_a_socket(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("pre-flight must stay offline")
        with patch.object(socket.socket, "connect", forbidden), patch.object(socket.socket, "connect_ex", forbidden):
            body = health(self.root)
        self.assertEqual(len(body["platforms"]), 7)

    def test_missing_registry_reports_a_code_instead_of_raising(self):
        body = health(self.root)
        self.assertFalse(body["vendor_preflight"]["ok"])
        self.assertTrue(body["vendor_preflight"].get("code"))


if __name__ == "__main__":
    unittest.main()
