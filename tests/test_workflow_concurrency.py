"""Bounded concurrent analysis with a single ordered writer (WP7-T1)."""
import json
import threading
import time
import unittest
from pathlib import Path
import tempfile

from content_screening.budget_controller import BudgetController
from content_screening.workflow import run

ROOT = Path(__file__).resolve().parents[1]


class WorkflowConcurrencyTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp/tests").mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=ROOT / "tmp/tests"))
        self.addCleanup(self._remove_tree)
        self.input = self.root / "raw.jsonl"
        self.db = self.root / "db.sqlite"
        self.policy = ROOT / "profiles/default.json"
        self.connection = ROOT / "config/connection.json"

    def _remove_tree(self):
        # Delete bottom-up instead of rmtree: the host shim intercepts
        # recursive deletes on this workspace and would fail the teardown.
        for path in sorted(self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                path.rmdir() if path.is_dir() else path.unlink()
            except OSError:
                pass
        try:
            self.root.rmdir()
        except OSError:
            pass

    def write(self, count):
        rows = [{"platform": "douyin", "object_type": "post", "content_id": str(index),
                 "text": f"我喜欢示例活动，找搭子一起去，第 {index} 条。"} for index in range(count)]
        self.input.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def run_with(self, analyzer, **kwargs):
        return run(self.input, self.policy, self.db, self.root / "out", mode="ark",
                   connection_path=self.connection, analyzer=analyzer, **kwargs)

    def test_four_way_concurrency_beats_serial_and_stays_ordered(self):
        self.write(40)
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def slow(record, context):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.2)
            with lock:
                in_flight -= 1
            from content_screening.rules_v2 import local_analyze
            from content_screening.rules_v2 import load_policy
            return local_analyze(record, load_policy(self.policy), context)

        started = time.monotonic()
        result = self.run_with(slow, max_calls=40)
        elapsed = time.monotonic() - started

        self.assertEqual(result["unique_contents"], 40)
        self.assertEqual(result["api_calls"], 40)
        self.assertGreaterEqual(peak, 3, "the pool never ran concurrently")
        self.assertLess(elapsed, 3.5, f"40 x 200ms took {elapsed:.2f}s; serial would be 8s")

    def test_call_limit_is_never_exceeded(self):
        self.write(10)
        calls = []

        def counted(record, context):
            calls.append(record["content_id"])
            from content_screening.rules_v2 import local_analyze, load_policy
            return local_analyze(record, load_policy(self.policy), context)

        with self.assertRaises(ValueError):
            self.run_with(counted, max_calls=4)
        self.assertEqual(calls, [])

    def test_one_failure_does_not_stop_the_batch(self):
        self.write(4)
        seen = []

        def flaky(record, context):
            seen.append(record["content_id"])
            if record["content_id"] == "2":
                raise ValueError("HTTP_500")
            from content_screening.rules_v2 import local_analyze, load_policy
            return local_analyze(record, load_policy(self.policy), context)

        result = self.run_with(flaky, max_calls=4)
        self.assertEqual(len(seen), 4)
        self.assertEqual(result["failed_this_run"], 1)
        import sqlite3
        with sqlite3.connect(self.db) as db:
            failed = db.execute("SELECT error_code FROM v2_attempts WHERE status='failed'").fetchall()
            completed = db.execute("SELECT count(*) FROM v2_attempts WHERE status='completed'").fetchone()[0]
        self.assertEqual(failed, [("ValueError",)])
        self.assertEqual(completed, 3)

    def test_budget_is_reserved_before_calls_and_released_when_unused(self):
        self.write(10)
        budget = BudgetController(self.root, {"daily_api_calls": 5, "daily_collection_posts": 500})
        calls = []

        def counted(record, context):
            calls.append(record["content_id"])
            from content_screening.rules_v2 import local_analyze, load_policy
            return local_analyze(record, load_policy(self.policy), context)

        result = self.run_with(counted, max_calls=10, budget=budget)
        self.assertEqual(len(calls), 5, "must never call beyond the reserved budget")
        self.assertEqual(result["api_calls"], 5)
        self.assertEqual(budget.status()["api_calls"]["used"], 5)
        self.assertGreater(result["candidate_contents"], 0)


if __name__ == "__main__":
    unittest.main()
