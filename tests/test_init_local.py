"""A fresh checkout can create its authority database explicitly."""
import json
from pathlib import Path
import tempfile
import unittest

from content_screening.db_access import preflight_db
from content_screening.runtime_context import RuntimeContext
from scripts.init_local import _ensure_database

ROOT = Path(__file__).resolve().parents[1]


class InitLocalTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp/tests").mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=ROOT / "tmp/tests"))
        self.addCleanup(self._remove_tree)
        deployment = json.loads((ROOT / "examples/deployment.example.json").read_text(encoding="utf-8"))
        (self.root / "deployment.json").write_text(json.dumps(deployment), encoding="utf-8")

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

    def test_installer_creates_a_migrated_database_once(self):
        path = _ensure_database(self.root)
        self.assertTrue(path.is_file())
        self.assertEqual(preflight_db(RuntimeContext.load(self.root / "deployment.json")), "migrated")

        before = path.stat().st_size
        self.assertEqual(_ensure_database(self.root), path)
        self.assertEqual(path.stat().st_size, before)


if __name__ == "__main__":
    unittest.main()
