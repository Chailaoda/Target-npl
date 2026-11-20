"""Interest profiling is layered: pure scoring, SQL repository, thin service (WP8-T1)."""
import ast
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from content_screening.interests import scoring
from content_screening.interest_service import InterestService, analyze_text, define_topic

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "content_screening" / "interests"


class LayeringTests(unittest.TestCase):
    def test_scoring_layer_has_no_storage_imports(self):
        source = (PACKAGE / "scoring.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("sqlite3", imported)
        self.assertNotIn("pathlib", imported)
        self.assertNotIn("os", imported)
        self.assertNotIn("content_screening", imported)

    def test_no_layer_is_a_boulder(self):
        for name in ("scoring.py", "repository.py", "service.py", "conflicts.py", "profiles.py"):
            with self.subTest(file=name):
                self.assertLess(len((PACKAGE / name).read_text(encoding="utf-8").splitlines()), 350)
        self.assertLess(len((ROOT / "content_screening" / "interest_service.py").read_text(encoding="utf-8").splitlines()), 40)

    def test_scoring_runs_without_a_database(self):
        definition = define_topic("胶片摄影")
        record = {"text": "我喜欢胶片摄影，最近在记录显影参数，经验分享。", "object_type": "post", "platform": "bilibili"}
        analyzed = analyze_text(record, definition)
        self.assertEqual(analyzed["decision"], "accepted")
        evidence = [scoring.raw_evidence(
            {"account_key": "bilibili:A", "source_hash": "h" * 64, "content_id": "1", "platform": "bilibili"},
            dict(record, published_at="2026-11-01T00:00:00+00:00"),
            analyzed, definition)]
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        started = time.perf_counter()
        for _ in range(200):
            metrics = scoring.metrics(evidence, now)
        elapsed = time.perf_counter() - started
        self.assertEqual(metrics["evidence_count"], 1)
        self.assertLess(elapsed, 0.05, f"200 pure evaluations took {elapsed * 1000:.1f}ms")

    def test_facade_still_exports_the_public_contract(self):
        self.assertTrue(callable(analyze_text))
        self.assertTrue(callable(define_topic))
        self.assertTrue(callable(scoring.metrics))
        for name in ("analyze", "search", "account", "evidence", "feedback", "list_conflicts", "resolve_conflict"):
            self.assertTrue(callable(getattr(InterestService, name)), name)


if __name__ == "__main__":
    unittest.main()
