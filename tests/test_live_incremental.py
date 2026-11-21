"""Incremental live polling: only new rows cross the wire (WP7-T3)."""
import json
import tempfile
import unittest
from pathlib import Path

from content_screening.control_server import RuleStore
from content_screening.live_cursor import decode_cursor, encode_cursor
from content_screening.live_view import live
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

    def append(self, start, count, path=None, platform=None):
        target = path or self.raw
        with target.open("a", encoding="utf-8") as stream:
            for index in range(start, start + count):
                row = {
                    "object_type": "post",
                    "data": {"aweme_id": str(index), "desc": f"示例活动第 {index} 条公开内容，找搭子一起去"}
                }
                if platform:
                    row["platform"] = platform
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

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
        self.assertNotEqual(delta["next_seq"], full["next_seq"])
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
        relative = self.raw.relative_to(self.root).as_posix()
        ahead = live(self.service, self.run_id,
                     since_seq=encode_cursor({relative: (1_000_000, 3)}, 0))
        self.assertFalse(ahead["incremental"])
        self.assertEqual(ahead["captured_total"], 3)
        with self.assertRaises(ValueError):
            live(self.service, self.run_id, since_seq=-1)
        with self.assertRaises(ValueError):
            live(self.service, self.run_id, since_seq="abc")

    def test_candidates_get_their_own_channel_in_the_same_cursor(self):
        self.append(0, 2)
        full = live(self.service, self.run_id)
        sources, candidates = decode_cursor(full["next_seq"])
        self.assertEqual(sum(rows for _, rows in sources.values()), full["captured_total"])
        self.assertEqual(candidates, full["candidate_total"])

    def test_earlier_lane_appending_does_not_hide_its_new_rows(self):
        """Multiple raw files grow independently; a total-row cursor shifted."""
        lane_a = self.root / "data/collections/20261120T000000000001Z"
        lane_b = self.root / "data/collections/20261120T000000000002Z"
        for platform, lane in (("douyin", lane_a), ("xhs", lane_b)):
            lane.mkdir(parents=True)
            atomic_json(self.folder / "platform-live" / platform / "collection-progress.json",
                        {"dataset_id": lane.name})
            (lane / "raw.jsonl").write_text("", encoding="utf-8")
        atomic_json(self.source / "checkpoint.json", {
            "status": "running", "aggregate": True,
            "platform_statuses": [
                {"platform": "douyin", "status": "completed"},
                {"platform": "xhs", "status": "completed"},
            ],
        })
        self.append(0, 2, lane_a / "raw.jsonl", "douyin")
        self.append(0, 2, lane_b / "raw.jsonl", "xhs")
        full = live(self.service, self.run_id)
        self.assertEqual([row["id"] for row in full["captured"]],
                         ["douyin:post:0", "douyin:post:1", "xhs:post:0", "xhs:post:1"])

        self.append(2, 1, lane_a / "raw.jsonl", "douyin")
        delta = live(self.service, self.run_id, since_seq=full["next_seq"])
        self.assertTrue(delta["incremental"])
        self.assertEqual([row["id"] for row in delta["captured_delta"]], ["douyin:post:2"])
        self.assertEqual(delta["captured_total"], 5)

    def test_trailing_partial_line_is_retried_and_truncation_resyncs(self):
        self.raw.write_text('{"object_type":"post","data":{"aweme_id":"1","desc":"完整"}}\n'
                            '{"object_type":"post","data":{"aweme_id":"2","desc":"',
                            encoding="utf-8")
        full = live(self.service, self.run_id)
        self.assertEqual(full["captured_total"], 1)
        with self.raw.open("a", encoding="utf-8") as stream:
            stream.write('补全"}}\n')
        delta = live(self.service, self.run_id, since_seq=full["next_seq"])
        self.assertEqual([row["id"] for row in delta["captured_delta"]], ["douyin:post:2"])

        # Replacing with a shorter file must invalidate the byte cursor.
        self.raw.write_text('{"object_type":"post","data":{"aweme_id":"9","desc":"新文件"}}\n',
                            encoding="utf-8")
        reset = live(self.service, self.run_id, since_seq=delta["next_seq"])
        self.assertFalse(reset["incremental"])
        self.assertEqual([row["id"] for row in reset["captured"]], ["douyin:post:9"])


if __name__ == "__main__":
    unittest.main()
