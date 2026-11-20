"""Persistent preview sandbox: latency, ReDoS kill and clean shutdown (WP7-T2)."""
import json
import statistics
import subprocess
import time
import unittest
from pathlib import Path

from content_screening.control_server import PreviewWorker
from content_screening.runtime_context import RuntimeContext

ROOT = Path(__file__).resolve().parents[1]


class PreviewWorkerTests(unittest.TestCase):
    def setUp(self):
        self.context = RuntimeContext.load(ROOT / "deployment.json")
        self.worker = PreviewWorker(self.context)
        self.addCleanup(self.worker.close)
        self.policy = json.loads((ROOT / "profiles/default.json").read_text(encoding="utf-8-sig"))

    def payload(self, text="我喜欢示例活动，找搭子一起去", policy=None):
        return {"policy": policy or self.policy, "text": text}

    def test_warm_requests_are_far_below_the_cold_start_cost(self):
        self.assertIn("accepted", self.worker.evaluate(self.payload()))  # warm up
        durations = []
        for _ in range(20):
            started = time.perf_counter()
            result = self.worker.evaluate(self.payload("我周末想去示例活动，求搭子"))
            durations.append(time.perf_counter() - started)
            self.assertIn("score", result)
        durations.sort()
        p95 = durations[int(len(durations) * 0.95) - 1]
        self.assertLess(p95, 0.04, f"p95={p95 * 1000:.1f}ms (cold start is 300-600ms)")
        self.assertLess(statistics.median(durations), 0.02)

    def test_catastrophic_regex_is_killed_and_the_next_request_recovers(self):
        hostile = json.loads(json.dumps(self.policy))
        hostile.setdefault("rules", {})["promotion_yes"] = ["(a+)+$"]
        with self.assertRaises(subprocess.TimeoutExpired):
            self.worker.evaluate(self.payload("a" * 40 + "b", hostile), timeout=0.5)
        self.assertIsNone(self.worker._proc, "the runaway process must be killed")
        recovered = self.worker.evaluate(self.payload("正常工作"))
        self.assertIn("score", recovered)

    def test_invalid_payload_answers_with_an_error_and_keeps_serving(self):
        bad = self.worker.evaluate({"policy": self.policy, "text": ""})
        self.assertIn("error", bad)
        self.assertIn("score", self.worker.evaluate(self.payload()))

    def test_close_terminates_the_child_process(self):
        self.worker.evaluate(self.payload())
        process = self.worker._proc
        self.assertIsNotNone(process)
        self.worker.close()
        self.assertIsNotNone(process.poll(), "child must not outlive the server")


if __name__ == "__main__":
    unittest.main()
