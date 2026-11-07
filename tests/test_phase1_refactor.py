import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from content_screening.active_config import ROOT
from content_screening.task_schema import task_schema, tool_definitions
from content_screening.task_contract import default_task, validate_execution, capabilities
from content_screening.rules_v2 import (
    load_policy, local_analyze, apply_decision, POSITIVE_PRAISE_PATTERNS
)
from content_screening.normalization import normalize


class Phase1RefactorTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(ROOT / "profiles/default.json")
        self.collection = json.loads((ROOT / "config/collection.json").read_text(encoding="utf-8-sig"))

    def test_1_relaxed_bounds_in_schema_and_json(self):
        """测试 1：统一放宽全链路参数边界与 Schema 契约"""
        schema = task_schema()
        c_props = schema["properties"]["collection"]["properties"]
        t_exec = schema["properties"]["task"]["properties"]["execution"]["properties"]

        self.assertEqual(t_exec["max_api_calls"]["maximum"], 500)
        self.assertEqual(c_props["keywords"]["maxItems"], 20)
        self.assertEqual(c_props["pages_per_keyword"]["maximum"], 20)
        self.assertEqual(c_props["posts_per_keyword"]["maximum"], 100)
        self.assertEqual(c_props["max_posts"]["maximum"], 500)
        self.assertEqual(c_props["comments_per_post"]["maximum"], 100)
        self.assertEqual(c_props["request_interval_seconds"]["minimum"], 1)
        self.assertEqual(c_props["max_requests"]["maximum"], 500)
        self.assertEqual(c_props["timeout_seconds"]["maximum"], 1800)

        # 检查静态磁盘 json
        disk_schema = json.loads((ROOT / "schemas/screening-task-v1.schema.json").read_text(encoding="utf-8"))
        d_c_props = disk_schema["properties"]["collection"]["properties"]
        self.assertEqual(d_c_props["max_posts"]["maximum"], 500)
        self.assertEqual(d_c_props["request_interval_seconds"]["minimum"], 1)

    def test_2_task_contract_relaxed_and_shared_budget(self):
        """测试 2：任务契约默认 100 额度、移除死板拦截、语义统一为 shared_pool_budget"""
        bundle = {"policy": self.policy, "collection": self.collection}
        t = default_task(bundle)
        self.assertEqual(t["execution"]["max_api_calls"], 100)

        # 验证移除死板拦截：大入口采集量即使大于 max_api_calls 也允许启动
        bundle["task"] = t
        bundle["task"]["execution"].update(source="collect", mode="ark", max_api_calls=5)
        bundle["collection"]["max_posts"] = 60
        bundle["collection"]["comments_per_post"] = 20
        # 此时 60 * (1 + 20) = 1260 > 5，不应报错
        self.assertTrue(validate_execution(bundle))

        caps = capabilities()
        self.assertEqual(caps["multi_platform"]["limits"], "shared_pool_budget")

    def test_3_context_topic_inheritance(self):
        """测试 3.1：子评论继承父帖上下文主题"""
        record = normalize({"platform": "douyin", "object_type": "comment", "content_id": "c1", "text": "我也想去，求拉群带带我！"})
        ctx = [{"text": "周末一起去示例活动馆运动打球呀", "source_hash": "ctx_0"}]
        base = local_analyze(record, self.policy, context=ctx)
        self.assertEqual(base["topic"], "relevant")
        self.assertEqual(base["evidence"]["topic"]["source"], "context:0")

    def test_3_intent_weight_priority(self):
        """测试 3.2：意向按权重择优 (seeking_company:90 > planning:80 > attended:70)"""
        # 文本同时包含“去过/体验过”(attended:70) 和 “求搭子”(seeking_company:90)
        record = normalize({"platform": "douyin", "object_type": "post", "content_id": "p1", "text": "以前去过示例活动体验过，这次打算去求搭子一起冲！"})
        base = local_analyze(record, self.policy)
        self.assertEqual(base["intent"], "seeking_company")

    def test_3_short_praise_identification_and_potential_tier(self):
        """测试 3.3 & 3.4：短评正向赞美识别与潜在沾边 (tier_potential) 分栏标记"""
        record = normalize({"platform": "douyin", "object_type": "comment", "content_id": "c2", "text": "太帅了！好喜欢这个作品，绝了"})
        ctx = [{"text": "最新示例活动摄影返图", "source_hash": "ctx_1"}]
        base = local_analyze(record, self.policy, context=ctx)
        self.assertEqual(base["topic"], "relevant")
        self.assertEqual(base["stance"], "positive")

        decided = apply_decision(base, record, self.policy)
        self.assertTrue(decided["accepted"])
        self.assertEqual(decided["evidence_level"], "related_only")
        self.assertIn("tier_potential", decided["quality_flags"])
        self.assertGreaterEqual(decided["score"], 60)

    def test_3_exact_tier_for_companion_seeking(self):
        """测试 3.5：精准同好/找搭子识别为 tier_exact"""
        record = normalize({"platform": "douyin", "object_type": "post", "content_id": "p2", "text": "周末示例活动约搭子，有人一起组队吗？"})
        base = local_analyze(record, self.policy)
        decided = apply_decision(base, record, self.policy)
        self.assertTrue(decided["accepted"])
        self.assertEqual(decided["evidence_level"], "explicit")
        self.assertIn("tier_exact", decided["quality_flags"])

    def test_3_anti_killing_and_negative_interception(self):
        """测试 3.6：反向否定词拦截与退坑/骗子拦截"""
        record = normalize({"platform": "douyin", "object_type": "post", "content_id": "p3", "text": "示例活动找搭子，算了不去了一堆骗子避雷"})
        base = local_analyze(record, self.policy)
        decided = apply_decision(base, record, self.policy)
        self.assertEqual(decided["intent"], "none")
        self.assertEqual(decided["stance"], "negative")
        self.assertFalse(decided["accepted"])
        self.assertIn("cancelled_plan", decided["quality_flags"])

    def test_3_commercial_and_organizer_interception(self):
        """测试 3.7：商业推广与组织者强拦截"""
        rec_promo = normalize({"platform": "douyin", "object_type": "post", "content_id": "p4", "text": "示例活动找搭子，低价出票代抢加微私信我"})
        base_promo = local_analyze(rec_promo, self.policy)
        dec_promo = apply_decision(base_promo, rec_promo, self.policy)
        self.assertFalse(dec_promo["accepted"])

        rec_org = normalize({"platform": "douyin", "object_type": "post", "content_id": "p5", "text": "我们是主办方，示例活动找搭子同好交流"})
        base_org = local_analyze(rec_org, self.policy)
        dec_org = apply_decision(base_org, rec_org, self.policy)
        self.assertFalse(dec_org["accepted"])

    def test_4_rule_preview_with_context_and_scope(self):
        """测试 4：rule_preview 进程支持 context_text 与 task 范围过滤计算"""
        payload = {
            "policy": self.policy,
            "text": "求一起带带我呀！",
            "context_text": "示例城市丙示例活动活动信息",
            "task": {
                "scope": {"locations": ["示例城市乙"], "recent_days": None, "published_after": None, "published_before": None},
                "exclusions": {"promotions": True, "terms": ["微商"]}
            }
        }
        res = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "content_screening.rule_preview"],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            cwd=ROOT
        )
        self.assertEqual(res.returncode, 0)
        output = json.loads(res.stdout.decode("utf-8"))
        self.assertIn("scope_check", output)
        self.assertEqual(output["evidence"]["topic"]["source"], "context:0")
        # 活动地点指定示例城市乙，而内容是示例城市丙，因此 scope_check 判定为 excluded
        self.assertEqual(output["scope_check"]["status"], "excluded")

    def test_6_task_worker_login_popup_and_graceful_proceed(self):
        """测试 6：平台未登录时唤醒登录窗口且不阻塞有数据的平台"""
        from content_screening.task_worker import execute
        from content_screening.task_service import atomic_json

        run_id = "a" * 32
        folder = ROOT / "data/task-runs" / run_id
        folder.mkdir(parents=True, exist_ok=True)
        import shutil
        if not folder.resolve().is_relative_to((ROOT / 'data/task-runs').resolve()):
            raise ValueError('Test cleanup must stay inside task-runs')
        self.addCleanup(shutil.rmtree, folder)

        bundle = {
            "policy": self.policy,
            "collection": self.collection,
            "task": default_task({"policy": self.policy})
        }
        bundle["task"]["execution"].update(source="collect", mode="rules")

        from content_screening.pipeline import fingerprint
        req = {
            "revision": "baseline",
            "bundle": bundle,
            "signature": fingerprint({"revision": "baseline", "bundle": bundle})
        }
        atomic_json(folder / "request.json", req)
        atomic_json(folder / "status.json", {"status": "pending"})

        # 模拟：xhs 需要登录，但 douyin 抓到了数据
        raw_mock = folder / "mock_raw.jsonl"
        raw_mock.write_text(json.dumps({"platform": "douyin", "content_id": "1", "text": "找搭子示例活动"}) + "\n", encoding="utf-8")

        mock_collect_result = {
            "status": "needs_user_login",
            "raw_file": str(raw_mock),
            "platform_statuses": [
                {"platform": "xhs", "status": "needs_user_login"},
                {"platform": "douyin", "status": "completed", "records": 1}
            ]
        }

        mock_login = MagicMock()
        with patch("content_screening.multi_collection.collect_selected", return_value=mock_collect_result), \
             patch("content_screening.platforms.open_login", mock_login), \
             patch("content_screening.workflow.run", return_value={"status": "completed", "total": 1}):
            state = execute(run_id, root=ROOT)

        # 验证自动弹出了 xhs 的登录窗口
        mock_login.assert_called_once_with("xhs")
        # 验证有数据时没有直接中止，而是完成了分析并提示
        self.assertEqual(state["phase"], "finished")
        self.assertIn("部分未登录平台已自动弹出登录窗并跳过", state.get("message", ""))


    def test_7_task_worker_marks_partial_collection_in_final_state(self):
        """P1：部分平台失败时最终状态必须显式留痕，不能被当成完整任务。"""
        from content_screening.task_worker import execute
        from content_screening.task_service import atomic_json
        from content_screening.pipeline import fingerprint
        import shutil
        run_id = "b" * 32
        folder = ROOT / "data/task-runs" / run_id
        folder.mkdir(parents=True, exist_ok=True)
        if not folder.resolve().is_relative_to((ROOT / 'data/task-runs').resolve()):
            raise ValueError('Test cleanup must stay inside task-runs')
        self.addCleanup(shutil.rmtree, folder)

        bundle = {
            "policy": self.policy,
            "collection": self.collection,
            "task": default_task({"policy": self.policy})
        }
        bundle["task"]["execution"].update(source="collect", mode="rules")
        req = {
            "revision": "baseline", "bundle": bundle,
            "signature": fingerprint({"revision": "baseline", "bundle": bundle})
        }
        atomic_json(folder / "request.json", req)
        atomic_json(folder / "status.json", {"status": "pending"})
        raw_mock = folder / "mock_raw.jsonl"
        raw_mock.write_text(json.dumps({"platform": "bilibili", "content_id": "1", "text": "示例活动找搭子"}) + "\n", encoding="utf-8")
        mock_result = {
            "status": "partial", "raw_file": str(raw_mock),
            "incomplete_platforms": ["xhs"],
            "platform_statuses": [
                {"platform": "bilibili", "status": "completed", "records": 1},
                {"platform": "xhs", "status": "failed", "records": 0}
            ]
        }
        mock_login = MagicMock()
        with patch("content_screening.multi_collection.collect_selected", return_value=mock_result), \
             patch("content_screening.platforms.open_login", mock_login), \
             patch("content_screening.workflow.run", return_value={"status": "completed", "total": 1}):
            state = execute(run_id, root=ROOT)

        self.assertEqual(state["phase"], "finished")
        self.assertTrue(state["collection_incomplete"])
        self.assertEqual(state["collection_status"], "partial")
        self.assertEqual(state["incomplete_platforms"], ["xhs"])
        self.assertIn("部分平台采集未完成", state.get("message", ""))
        self.assertNotIn("登录窗", state.get("message", ""))
        mock_login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
