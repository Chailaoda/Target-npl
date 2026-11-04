import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from content_screening.pipeline import classify, load_profile, render_report, run


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.profile_path = Path(__file__).resolve().parents[1] / "profiles/legacy.json"
        self.profile = load_profile(self.profile_path)
        self.temp_root = self.profile_path.parents[1] / "tmp" / "tests"
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def classify(self, text):
        return classify({"text": text}, self.profile)

    def test_explicit_interest_and_exact_evidence(self):
        text = "我喜欢示例活动，想去看乐队。"
        result = self.classify(text)
        self.assertTrue(result["accepted"])
        for evidence in result["evidence"]:
            self.assertEqual(text[evidence["start"]:evidence["end"]], evidence["quote"])

    def test_negative_spam_and_unrelated(self):
        for text in ["我不喜欢示例活动", "示例活动低价出票", "我喜欢甲类蛋糕", "示例活动新闻发布了"]:
            with self.subTest(text=text):
                self.assertFalse(self.classify(text)["accepted"])

    def test_short_reply_does_not_inherit_parent_interest(self):
        self.assertFalse(self.classify("我也是")["accepted"])

    def test_html_and_unsafe_url_are_not_executable(self):
        record = {"text": "<script>alert(1)</script>示例活动", "author": "<img onerror=alert(1)>", "url": "javascript:alert(1)", "platform": "demo", "content_id": "1", "synthetic": True}
        page = render_report([(record, classify(record, self.profile))], self.profile, 1, "hash")
        self.assertNotIn("<script>", page)
        self.assertNotIn('href="javascript:', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("虚构样例", page)

    def test_reimport_and_changed_template_keep_versions(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as folder:
            root = Path(folder)
            source = root / "input.jsonl"
            source.write_text(json.dumps({"platform": "test", "content_id": "1", "text": "我喜欢示例活动"}, ensure_ascii=False), encoding="utf-8")
            db = root / "data.sqlite"
            for _ in range(2):
                result = run(source, self.profile_path, db, root / "out")
                self.assertEqual(result["accepted_count"], 1)
            changed = dict(self.profile, accepted_labels=[])
            profile_path = root / "changed.json"
            profile_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(run(source, profile_path, db, root / "out")["accepted_count"], 0)
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sources").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count(*) FROM evaluations").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 3)

    def test_invalid_input_does_not_create_database(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as folder:
            root = Path(folder)
            source = root / "broken.jsonl"
            source.write_text('{"platform":"test","content_id":"1","text":"valid"}\n{"text":"missing ID"}', encoding="utf-8")
            db = root / "data.sqlite"
            with self.assertRaises(ValueError):
                run(source, self.profile_path, db, root / "out")
            self.assertFalse(db.exists())

    def test_cache_reuse_and_model_identity_changes(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as folder:
            root = Path(folder)
            source = root / "input.jsonl"
            source.write_text(json.dumps({"platform": "test", "content_id": "1", "text": "我喜欢示例活动"}, ensure_ascii=False), encoding="utf-8")
            calls = []
            def evaluate(record, profile):
                calls.append(record["content_id"])
                return classify(record, profile)
            for model, expected_reused in (("a", 0), ("a", 1), ("b", 0)):
                result = run(source, self.profile_path, root / "data.sqlite", root / "out", evaluate, {"model": model}, 1)
                self.assertEqual(result["reused_count"], expected_reused)
            self.assertEqual(calls, ["1", "1"])

    def test_call_limit_counts_only_new_records_and_prevents_partial_calls(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as folder:
            root = Path(folder)
            source = root / "input.jsonl"
            rows = [{"platform": "test", "content_id": str(i), "text": "我喜欢示例活动"} for i in range(3)]
            source.write_text(json.dumps(rows[0], ensure_ascii=False), encoding="utf-8")
            run(source, self.profile_path, root / "data.sqlite", root / "out", classify, {"model": "a"}, 1)
            source.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
            def forbidden(*args):
                self.fail("No API call is allowed above the limit")
            with self.assertRaisesRegex(ValueError, "新增内容"):
                run(source, self.profile_path, root / "data.sqlite", root / "out", forbidden, {"model": "a"}, 1)
            result = run(source, self.profile_path, root / "data.sqlite", root / "out", classify, {"model": "a"}, 2)
            self.assertEqual(result["reused_count"], 1)
            self.assertEqual(result["new_evaluation_count"], 2)

    def test_csv_import_and_duplicate_rows(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as folder:
            root = Path(folder)
            source = root / "input.csv"
            source.write_text('platform,content_id,text\ntest,1,我喜欢示例活动\ntest,1,我喜欢示例活动\n', encoding="utf-8-sig")
            result = run(source, self.profile_path, root / "data.sqlite", root / "out")
            self.assertEqual(result["input_count"], 2)
            self.assertEqual(result["unique_count"], 1)


if __name__ == "__main__":
    unittest.main()
