"""Version history is indexed once and invalidated by writes (WP7-T3)."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from content_screening.control_server import RuleStore

ROOT = Path(__file__).resolve().parents[1]


class HistoryCacheTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp/tests").mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=ROOT / "tmp/tests"))
        self.addCleanup(self._remove_tree)
        for name in ("profiles/default.json", "config/collection.json"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        self.store = RuleStore(self.root)

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

    def _count_metadata_reads(self, calls=10):
        original = Path.read_text
        reads = {"n": 0}

        def counting(path, *args, **kwargs):
            if path.name == "metadata.json":
                reads["n"] += 1
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", counting):
            for _ in range(calls):
                self.store.history()
        return reads["n"]

    def test_repeated_reads_hit_the_disk_once(self):
        state = self.store.state()
        bundle = copy.deepcopy(state["bundle"])
        bundle["policy"]["name"] = "缓存测试"
        self.store.save(bundle, state["etag"])
        self.assertEqual(self._count_metadata_reads(10), 1)

    def test_save_makes_the_new_version_visible_immediately(self):
        state = self.store.state()
        first = copy.deepcopy(state["bundle"])
        first["policy"]["name"] = "第一版"
        saved = self.store.save(first, state["etag"])
        self.assertEqual(len(self.store.history()), 1)

        second = copy.deepcopy(saved["bundle"])
        second["policy"]["name"] = "第二版"
        self.store.save(second, saved["etag"])
        history = self.store.history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["name"], "第二版")

    def test_returned_entries_are_copies(self):
        state = self.store.state()
        bundle = copy.deepcopy(state["bundle"])
        bundle["policy"]["name"] = "副本测试"
        self.store.save(bundle, state["etag"])
        history = self.store.history()
        history[0]["name"] = "被篡改"
        self.assertEqual(self.store.history()[0]["name"], "副本测试")


if __name__ == "__main__":
    unittest.main()
