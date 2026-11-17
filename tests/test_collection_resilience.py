import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from content_screening.control_server import RuleStore
from content_screening.multi_collection import collect_selected
from content_screening.pipeline import fingerprint
from content_screening.runtime_context import RuntimeContext, clear_context, set_context
from content_screening.task_contract import default_task
from content_screening.task_service import atomic_json
from content_screening.task_worker import execute
from scripts import mediacrawler_collect as adapter


ROOT = Path(__file__).resolve().parents[1]


class FakeCommentClient:
    def __init__(self, responses):
        self.responses = list(responses)

    async def get_aweme_comments(self, post_id):
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class CollectionResilienceTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp/tests").mkdir(parents=True, exist_ok=True)

    def test_one_malformed_comment_response_only_skips_its_post(self):
        state = {"comments_done": [], "comments": 0, "warnings": [], "warning_count": 0}
        seen = set()
        saved = []

        async def save_comment(post_id, comment):
            saved.append((post_id, comment))

        client = FakeCommentClient([
            {},  # One malformed response: previous behavior aborted the whole run.
            {"comments": [{"cid": "123", "aweme_id": "p2", "content": "正常评论"}]},
        ])
        settings = {"comments_per_post": 5}

        first = asyncio.run(adapter.fetch_post_comments(
            client, save_comment, "p1", state, settings, seen, lambda: None))
        second = asyncio.run(adapter.fetch_post_comments(
            client, save_comment, "p2", state, settings, seen, lambda: None))

        self.assertFalse(first)
        self.assertTrue(second)
        self.assertEqual(len(saved), 1)
        self.assertEqual(state["comments"], 1)
        self.assertEqual(state["comments_done"], ["p1", "p2"])
        self.assertEqual(len(state["warnings"]), 1)
        self.assertEqual(state["warnings"][0]["code"], "COMMENT_DATA_MISSING")
        self.assertEqual(state["warnings"][0]["post_id"], "p1")
        self.assertEqual(state["warning_count"], 1)

    def test_systemic_comment_failure_still_aborts(self):
        state = {"comments_done": [], "comments": 0, "warnings": [], "warning_count": 0}
        client = FakeCommentClient([ValueError("HTTP_403")])
        with self.assertRaisesRegex(ValueError, "HTTP_403"):
            asyncio.run(adapter.fetch_post_comments(
                client, lambda post_id, comment: asyncio.sleep(0), "p1", state,
                {"comments_per_post": 5}, set(), lambda: None))
        self.assertEqual(state["warnings"], [])

    def _collect_selected_with_child(self, child_status, returncode):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp/tests", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            (root / "data/collections").mkdir(parents=True)
            (root / "output/collections").mkdir(parents=True)
            folder = root / "data/task-runs" / ("a" * 32)
            folder.mkdir(parents=True)
            settings = adapter.load_settings(ROOT / "config/collection.json")
            vendor = types.SimpleNamespace(python=Path(sys.executable), launch_env=lambda: {})
            context = types.SimpleNamespace(deployment={"test_isolated": True})

            def child(command, *args, **kwargs):
                receipt = Path(command[command.index("--receipt") + 1])
                child_dir = root / "data/collections" / "20261111T000000000000Z"
                child_dir.mkdir(parents=True, exist_ok=True)
                raw = child_dir / "raw.jsonl"
                raw.write_text("\n".join(json.dumps({
                    "object_type": "post", "keyword": "徒步",
                    "data": {"aweme_id": str(i), "desc": "公开原文", "author_id": f"u{i}"}
                }, ensure_ascii=False) for i in range(2)) + "\n", encoding="utf-8")
                state = {
                    "status": child_status, "records": 2, "posts": 1, "comments": 1, "requests": 5,
                    "raw_file": str(raw), "settings": settings, "identity_mode": "public_source",
                    "warnings": [{"stage": "comment", "post_id": "p1", "code": "COMMENT_DATA_MISSING"}],
                    "warning_count": 1,
                }
                atomic_json(child_dir / "checkpoint.json", state)
                atomic_json(receipt, state)
                return subprocess.CompletedProcess(command, returncode)

            with patch("content_screening.runtime_context.current_context", return_value=context), \
                 patch("content_screening.multi_collection.resolve_vendor_env", return_value=vendor), \
                 patch("content_screening.multi_collection.selected", return_value=["douyin"]), \
                 patch("content_screening.multi_collection.subprocess.run", side_effect=child):
                result = collect_selected(settings, folder, root)
            merged = Path(result["raw_file"])
            return result, {
                "raw_lines": len(merged.read_text(encoding="utf-8").splitlines()),
                "raw_in_project": merged.is_relative_to(root / "data/collections"),
            }

    def test_completed_child_with_warning_is_merged_and_keeps_warning(self):
        result, raw_info = self._collect_selected_with_child("completed", 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warning_count"], 1)
        self.assertEqual(result["warnings"][0]["platform"], "douyin")
        self.assertEqual(result["platform_statuses"][0]["warning_count"], 1)
        self.assertEqual(raw_info["raw_lines"], 2)
        self.assertTrue(raw_info["raw_in_project"])

    def test_failed_child_is_still_not_merged(self):
        result, _ = self._collect_selected_with_child("partial", 2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["records"], 0)
        self.assertEqual(result["warnings"], [])
        self.assertTrue(result["errors"])

    def test_task_worker_continues_to_analysis_with_collection_warnings(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp/tests", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            for folder in ("profiles", "config", "data/task-runs", "data/collections"):
                (root / folder).mkdir(parents=True, exist_ok=True)
            for relative in ("profiles/default.json", "config/collection.json", "config/connection.json"):
                (root / relative).write_bytes((ROOT / relative).read_bytes())

            context = RuntimeContext(root / "deployment.json", ROOT, root, {
                "test_isolated": True, "database": {"relative_path": "data/festival.sqlite3"}
            })
            set_context(context)
            self.addCleanup(clear_context)

            bundle = RuleStore(root).state()["bundle"]
            bundle.setdefault("task", default_task(bundle))
            bundle["task"]["execution"].update(source="collect", mode="rules", max_api_calls=10)
            run_id = "e" * 32
            folder = root / "data/task-runs" / run_id
            folder.mkdir()
            atomic_json(folder / "request.json", {
                "revision": "baseline",
                "signature": fingerprint({"revision": "baseline", "bundle": bundle}),
                "request_id": "warning-analysis", "bundle": bundle,
            })
            atomic_json(folder / "status.json", {"run_id": run_id, "status": "queued", "created_at": "now"})

            raw = root / "data/collections" / "20261111T000000000001Z" / "raw.jsonl"
            raw.parent.mkdir(parents=True)
            raw.write_text(json.dumps({
                "object_type": "post", "keyword": "徒步",
                "data": {"aweme_id": "1", "desc": "公开原文", "author_id": "u1"}
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            warnings = [{"stage": "comment", "post_id": "p1", "code": "COMMENT_DATA_MISSING"}]
            collection = {
                "status": "completed", "raw_file": str(raw), "warnings": warnings,
                "warning_count": 1, "incomplete_platforms": [],
                "platform_statuses": [{"platform": "douyin", "status": "completed", "warning_count": 1}],
            }

            with patch("content_screening.multi_collection.collect_selected", return_value=collection), \
                 patch("content_screening.workflow.run", return_value={"status": "completed"}) as workflow_run, \
                 patch("content_screening.db_access.resolve_db", return_value=root / "data/festival.sqlite3"):
                state = execute(run_id)

            self.assertTrue(workflow_run.called)
            self.assertEqual(Path(workflow_run.call_args.args[0]), raw)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["phase"], "finished")
            self.assertEqual(state["collection_warning_count"], 1)
            self.assertEqual(state["collection_warnings"], warnings)
            self.assertFalse(state["collection_incomplete"])


if __name__ == "__main__":
    unittest.main()
