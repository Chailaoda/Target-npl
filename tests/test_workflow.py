import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from festival_match.normalization import normalize
from festival_match.rules_v2 import load_policy, local_analyze, validate_result, apply_decision
from festival_match.workflow import run, review, connect, export_job

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.policy_path = ROOT / "profiles/music_festival_v2.json"
        self.policy = load_policy(self.policy_path)
        (ROOT / "tmp/tests").mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "tmp/tests")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input = self.root / "raw.jsonl"
        self.db = self.root / "db.sqlite"

    def write(self, rows):
        self.input.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding="utf-8")

    def post(self, text="我喜欢音乐节", cid="1", **kwargs):
        return dict(platform="douyin", content_id=cid, text=text, object_type="post", **kwargs)

    def process(self, mode="rules", analyzer=None, **kwargs):
        return run(self.input, self.policy_path, self.db, self.root / "out", mode=mode,
                   connection_path=ROOT / "config/connection.json", analyzer=analyzer, **kwargs)

    def test_upstream_comment_relation_and_anonymous_identity(self):
        row = {"object_type": "comment", "data": {"comment_id": "12", "aweme_id": "3", "content": "想去", "parent_comment_id": "10", "creator_hash": "abc", "nickname": "示***例", "create_time": 0}}
        result = normalize(row)
        self.assertEqual((result["content_id"], result["parent_id"], result["root_id"]), ("12", "10", "3"))
        self.assertEqual(result["author_key"], "anonymous:abc")
        self.assertEqual(result["published_at"], "")
        self.assertEqual(result["author_url"], "")

    def test_promotion_and_interest_are_independent(self):
        source = normalize(self.post("我喜欢音乐节，也做票务代理"))
        result = local_analyze(source, self.policy)
        self.assertEqual(result["promotion"], "yes")
        self.assertEqual(result["stance"], "positive")
        self.assertTrue(result["accepted"])

    def test_negative_overrides_positive_stance(self):
        source = normalize(self.post("我喜欢音乐节这句话是假的，我不喜欢音乐节"))
        self.assertFalse(local_analyze(source, self.policy)["accepted"])

    def test_parent_evidence_cannot_be_attributed_to_reply(self):
        source = normalize(self.post("我也是"))
        obj = dict(topic="relevant", stance="positive", intent="none", promotion="no", author_role="unknown", evidence={"topic": "音乐节", "stance": "我喜欢音乐节"}, reason="错误继承父作者")
        with self.assertRaises(ValueError):
            validate_result(obj, source, self.policy)
        obj.update(evidence={"topic": "我也是", "stance": "我也是"})
        self.assertFalse(validate_result(obj, source, self.policy)["accepted"])

    def test_hashtags_not_intent_and_attendance_not_future_plan(self):
        text = "#音乐节搭子"
        obj = dict(topic="relevant", stance="unclear", intent="seeking_company", promotion="uncertain", author_role="unknown", evidence={"topic": text, "intent": text}, reason="标签")
        self.assertFalse(validate_result(obj, normalize(self.post(text)), self.policy)["accepted"])
        result = local_analyze(normalize(self.post("我参加了草莓音乐节")), self.policy)
        self.assertEqual(result["intent"], "attended")

    def test_topic_context_origin_and_evidence_array_compatibility(self):
        source = normalize(self.post("我也想去，有结伴的吗"))
        obj = dict(topic="relevant", stance="unclear", intent="seeking_company", promotion="no", author_role="unknown",
                   evidence={"topic": {"source": "context:0", "quote": "音乐节"}, "intent": ["有结伴的吗", "我也想去"]}, reason="当前作者明确找同行")
        context = [{"text": "有人去音乐节吗", "source_hash": "parent-hash"}]
        result = validate_result(obj, source, self.policy, context)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["evidence"]["topic"]["source_hash"], "parent-hash")
        obj["evidence"]["intent"] = {"source": "context:0", "quote": "有人去音乐节吗"}
        with self.assertRaises(ValueError):
            validate_result(obj, source, self.policy, context)

    def test_multiple_keywords_do_not_repeat_analysis(self):
        raw = self.post(author="同名")
        self.write([dict(raw, keyword="a"), dict(raw, keyword="b")])
        first = self.process()
        self.assertEqual(first["unique_contents"], 1)
        second = self.process()
        self.assertEqual(second["reused_count"], 1)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM v2_observations").fetchone()[0], 2)

    def test_decision_gate_changes_and_sorting_reuse_model_analysis(self):
        self.write([self.post()])
        self.process(mode="ark", analyzer=lambda r,c: local_analyze(r,self.policy))
        changed = json.loads(json.dumps(self.policy))
        changed["display"]["sort_weights"]["positive"] = 7
        changed["decision_gates"]["stance_positive"] = ["我喜欢音乐节"]
        self.policy_path = self.root / "changed-policy.json"
        self.policy_path.write_text(json.dumps(changed,ensure_ascii=False),encoding="utf-8")
        result = self.process(mode="ark", analyzer=lambda *a: self.fail("decision change called model"))
        self.assertEqual(result["reused_count"], 1)
        row=json.loads(Path(result["export"]).read_text(encoding="utf-8"))
        self.assertEqual(row["result"]["score"],7)

    def test_generic_praise_and_being_alone_are_not_explicit_companion_requests(self):
        source=normalize(self.post("一个人"))
        obj=dict(topic="relevant",stance="unclear",intent="seeking_company",promotion="no",author_role="unknown",
                 evidence={"topic":{"source":"context:0","quote":"音乐节"},"intent":"一个人"},reason="原模型误判")
        result=validate_result(obj,source,self.policy,[{"text":"音乐节", "source_hash":"parent"}])
        projected=apply_decision(result,source,self.policy)
        self.assertFalse(projected["accepted"])
        self.assertEqual(projected["intent"],"unclear")
        self.assertIn('intent',projected['decision_rejections'])

    def test_context_changes_invalidate_child_cache(self):
        parent = self.post()
        comment = self.post("我也喜欢音乐节", cid="2", root_id="1")
        comment["object_type"] = "comment"
        self.write([parent, comment]); self.process()
        self.write([dict(parent, text="我想去音乐节"), comment])
        result = self.process()
        self.assertEqual(result["reused_count"], 0)

    def test_empty_text_preserved_without_api_or_key(self):
        self.write([self.post("")])
        with patch("festival_match.rules_v2.ark_analyzer", side_effect=AssertionError("must not read key")):
            result = self.process(mode="ark")
        self.assertEqual(result["api_calls"], 0)
        self.assertEqual(result["unique_contents"], 1)

    def test_call_limit_before_any_model_calls(self):
        self.write([self.post(cid=str(i)) for i in range(3)])
        with self.assertRaises(ValueError):
            self.process(mode="ark", max_calls=2, analyzer=lambda *a: self.fail("called above budget"))

    def test_resume_commits_successes_and_retries_only_failed_items(self):
        self.write([self.post(cid="1"), self.post(cid="2")])
        seen = []
        def failing(record, context):
            seen.append(record["content_id"])
            if record["content_id"] == "2":
                raise ValueError("fake failure")
            return local_analyze(record, self.policy)
        first = self.process(mode="ark", analyzer=failing)
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["failed_contents"], 1)
        def repaired(record, context):
            seen.append(record["content_id"])
            return local_analyze(record, self.policy)
        second = self.process(mode="ark", analyzer=repaired, job_id=first["job_id"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["reused_count"], 1)
        self.assertEqual(seen, ["1", "2", "2"])

    def test_invalid_model_output_is_quarantined_and_not_repeated(self):
        self.write([self.post()])
        def rejected(record, context):
            error = ValueError("Schema rejected")
            error.validation_code = "EVIDENCE_NOT_IN_SOURCE"
            error.usage = {"total_tokens": 12}
            raise error
        result = self.process(mode="ark", analyzer=rejected)
        self.assertEqual(result["status"], "completed_with_review")
        self.assertEqual(result["needs_review_contents"], 1)
        self.assertEqual(result["candidate_contents"], 0)
        self.assertEqual(result["new_tokens"], 12)
        result = self.process(mode="ark", analyzer=lambda *a: self.fail("quarantined item was retried"))
        self.assertEqual(result["api_calls"], 0)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM v2_policies").fetchone()[0], 1)

    def test_review_persists_without_altering_model_result_and_unknown_names_not_merged(self):
        self.write([self.post(author="同名", cid="1"), self.post(author="同名", cid="2")])
        result = self.process()
        self.assertEqual(result["candidate_unknown_author_contents"], 2)
        rows = [json.loads(l) for l in Path(result["export"]).read_text(encoding="utf-8").splitlines()]
        key = rows[0]["analysis_key"]
        review(self.db, key, "reject", "测试：仅人工复核")
        with closing(connect(self.db)) as db:
            refreshed = export_job(db, result["job_id"], self.root / "refreshed")
            self.assertEqual(db.execute("SELECT count(*) FROM v2_review_history").fetchone()[0], 1)
        records = [json.loads(l) for l in Path(refreshed["export"]).read_text(encoding="utf-8").splitlines()]
        row = next(r for r in records if r["analysis_key"] == key)
        self.assertEqual(row["review"]["decision"], "reject")
        self.assertTrue(row["result"]["accepted"])

    def test_hostile_public_text_is_escaped_in_report(self):
        self.write([self.post('我喜欢音乐节<script>alert(1)</script>', author='<img onerror=alert(1)>')])
        result = self.process()
        page = Path(result["report"]).read_text(encoding="utf-8")
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('&lt;script&gt;', page)


if __name__ == "__main__":
    unittest.main()
