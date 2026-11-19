import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from content_screening.control_server import RuleStore
from content_screening.runtime_context import RuntimeContext, clear_context, set_context
from content_screening.task_service import TaskService


ROOT = Path(__file__).resolve().parents[1]


class TaskLaunchStatusTests(unittest.TestCase):
    def test_queued_record_without_worker_launch_is_interrupted(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temp:
            root = Path(temp)
            for folder in ("profiles", "config"):
                (root / folder).mkdir()
            for relative in ("profiles/music_festival_v2.json", "config/collection.json"):
                (root / relative).write_bytes((ROOT / relative).read_bytes())
            service = TaskService(RuleStore(root))
            run_id = "a" * 32
            folder = service.runs / run_id
            folder.mkdir(parents=True)
            (folder / "status.json").write_text(json.dumps({
                "run_id": run_id, "status": "queued", "created_at": "2026-11-11T00:00:00+00:00"
            }), encoding="utf-8")

            state = service.status(run_id)

            self.assertEqual(state["status"], "interrupted")
            self.assertEqual(state["phase"], "launch_failed")
            self.assertIn("没有开始采集或模型分析", state["message"])

    def test_worker_log_stays_open_until_process_is_spawned(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temp:
            root = Path(temp)
            for folder in ("profiles", "config"):
                (root / folder).mkdir()
            for relative in ("profiles/default.json", "config/collection.json"):
                (root / relative).write_bytes((ROOT / relative).read_bytes())
            service = TaskService(RuleStore(root))
            service.runs.mkdir(parents=True)
            context = RuntimeContext(root / "deployment.json", ROOT, root, {
                "test_isolated": True, "database": {"relative_path": "data/festival.sqlite3"}
            })
            set_context(context)
            self.addCleanup(clear_context)

            def spawn(*args, **kwargs):
                if kwargs["stdout"].closed or kwargs["stderr"].closed:
                    raise ValueError("I/O operation on closed file")
                return type("Child", (), {"pid": 424242})()

            with patch.object(service, "dataset", return_value=ROOT / "raw.jsonl"), \
                    patch("content_screening.task_service.subprocess.Popen", side_effect=spawn) as popen:
                state = service.run(service.get()["revision"], "launch-stream-open")

            self.assertEqual(state["status"], "queued")
            popen.assert_called_once()
            launch = service.runs / state["run_id"] / "launch.json"
            self.assertEqual(json.loads(launch.read_text(encoding="utf-8"))["pid"], 424242)


if __name__ == "__main__":
    unittest.main()
