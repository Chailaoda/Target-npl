"""Incremental live polling: only new rows cross the wire (WP7-T3)."""
import json
import tempfile
import unittest
from pathlib import Path

from content_screening.control_server import RuleStore
from content_screening.live_view import live, SEQ_SCALE
from content_screening.task_service import TaskService, atomic_json

ROOT = Path(__file__).resolve().parents[1]


class LiveIncrementalTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp/tests").mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=ROOT / "tmp/tests"))
        self.addCleanup(self._remove_tree)
        for name in ("config/collection.json", "profiles/default.json"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        self.store = RuleStore(self.root)
        self.service = TaskService(self.store)
        self.run_id = "b" * 32
        self.folder = self.service.runs / self.run_id
        self.folder.mkdir(parents=True, exist_ok=True)
        self.dataset = "20261120T000000000000Z"
        self.source = self.root / "data/collections" / self.dataset
        self.source.mkdir(parents=True, exist_ok=True)
        atomic_json(self.folder / "status.json", {
            "run_id": self.run_id, "status": "running", "phase": "collecting",
            "created_at": "2026-11-20T00:00:00Z"
        })
        atomic_json(self.folder / "collection-progress.json", {"dataset_id": self.dataset})
        self.raw = self.source / "raw.jsonl"
        self.raw.write_text("", encoding="utf-8")

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

    def append(self, start, count):
        with self.raw.open("a", encoding="utf-8") as stream:
            for index in range(start, start + count):
                stream.write(json.dumps({
                    "object_type": "post",
                    "data": {"aweme_id": str(index), "desc": f"示例活动第 {index} 条公开内容，找搭子一起去"}
                }, ensure_ascii=False) + "\n")

    def test_delta_only_carries_new_rows(self):
        self.append(0, 500)
        full = live(self.service, self.run_id)
        self.assertEqual(full["captured_total"], 500)
        self.assertFalse(full["incremental"])
        self.assertEqual(len(full["captured"]), 200)

        self.append(500, 3)
        delta = live(self.service, self.run_id, since_seq=full["next_seq"])
        self.assertTrue(delta["incremental"])
        self.assertEqual([row["id"] for row in delta["captured_delta"]],
                         [f"douyin:post:{index}" for index in (500, 501, 502)])
        self.assertEqual(delta["captured_delta"], delta["captured_delta"])
        self.assertGreater(delta["next_seq"], full["next_seq"])
        saved = len(json.dumps(full, ensure_ascii=False).encode()) / max(1, len(json.dumps(delta, ensure_ascii=False).encode()))
        self.assertGreater(saved, 5, "delta payload should be far smaller than the full payload")

    def test_client_that_fell_behind_gets_a_full_resync(self):
        self.append(0, 500)
        full = live(self.service, self.run_id)
        stale = live(self.service, self.run_id, since_seq=0)
        self.assertFalse(stale["incremental"])
        self.assertEqual(len(stale["captured"]), 200)
        self.assertEqual(stale["captured_total"], 500)

    def test_cursor_from_the_future_and_negative_values_are_handled(self):
        self.append(0, 3)
        full = live(self.service, self.run_id)
        ahead = live(self.service, self.run_id, since_seq=full["next_seq"] + SEQ_SCALE)
        self.assertFalse(ahead["incremental"])
        with self.assertRaises(ValueError):
            live(self.service, self.run_id, since_seq=-1)
        with self.assertRaises(ValueError):
            live(self.service, self.run_id, since_seq="abc")

    def test_candidates_get_their_own_channel_in_the_same_cursor(self):
        self.append(0, 2)
        full = live(self.service, self.run_id)
        self.assertEqual(full["next_seq"] // SEQ_SCALE, full["captured_total"])
        self.assertEqual(full["next_seq"] % SEQ_SCALE, full["candidate_total"])


if __name__ == "__main__":
    unittest.main()
