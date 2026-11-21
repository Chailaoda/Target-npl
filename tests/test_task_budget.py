"""Every task path, including manual runs, obeys the shared daily model budget."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from content_screening.budget_controller import BudgetController
from content_screening.control_server import RuleStore
from content_screening.pipeline import fingerprint
from content_screening.task_contract import default_task
from content_screening.task_service import TaskService, atomic_json
from content_screening.task_worker import execute

ROOT = Path(__file__).resolve().parents[1]


class TaskBudgetTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp/tests").mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=ROOT / "tmp/tests"))
        self.addCleanup(self._remove_tree)
        for relative in ("config/collection.json", "profiles/default.json"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
        self.service = TaskService(RuleStore(self.root))
        self.run_id = "d" * 32
        self.folder = self.service.runs / self.run_id
        self.folder.mkdir(parents=True)
        policy = json.loads((self.root / "profiles/default.json").read_text(encoding="utf-8-sig"))
        collection = json.loads((self.root / "config/collection.json").read_text(encoding="utf-8-sig"))
        bundle = {"policy": policy, "collection": collection,
                  "task": default_task({"policy": policy})}
        bundle["task"]["execution"].update(source="collect", mode="ark", max_api_calls=2)
        request = {"revision": "baseline", "bundle": bundle,
                   "signature": fingerprint({"revision": "baseline", "bundle": bundle})}
        atomic_json(self.folder / "request.json", request)
        atomic_json(self.folder / "status.json", {"run_id": self.run_id, "status": "queued"})
        self.raw = self.folder / "raw.jsonl"
        self.raw.write_text(json.dumps({"platform": "douyin", "content_id": "1",
                                        "text": "示例活动找搭子"}) + "\n", encoding="utf-8")

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

    def _collect_result(self):
        return {"status": "completed", "raw_file": str(self.raw),
                "platform_statuses": [{"platform": "douyin", "status": "completed"}]}

    def test_manual_task_passes_the_shared_budget_into_analysis(self):
        with patch("content_screening.multi_collection.collect_selected", return_value=self._collect_result()), \
             patch("content_screening.workflow.run", return_value={"status": "completed", "api_calls": 1}) as run:
            state = execute(self.run_id, root=self.root)
        self.assertEqual(state["status"], "completed")
        budget = run.call_args.kwargs.get("budget")
        self.assertIsInstance(budget, BudgetController)
        self.assertEqual(budget.root, self.root.resolve())

    def test_exhausted_daily_budget_stops_before_collection(self):
        BudgetController(self.root).record_api_calls(2000)
        collect = MagicMock()
        with patch("content_screening.multi_collection.collect_selected", collect):
            state = execute(self.run_id, root=self.root)
        self.assertEqual(state["status"], "not_started_budget")
        self.assertEqual(state["phase"], "stopped")
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
