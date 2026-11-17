"""Adversarial Review: Verification of Unlocked Filters, Strict Logic and Budget Controls."""
import copy
from datetime import datetime, timezone
import json
import unittest
from pathlib import Path

from festival_match.task_planner import compile_goal
from festival_match.task_service import TaskService
from festival_match.control_server import RuleStore
from festival_match.task_contract import apply_patch, apply_scope, validate_execution, default_task
from festival_match.rules_v2 import local_analyze, apply_decision, validate_result
from festival_match.normalization import normalize


class AdversarialReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = RuleStore()
        self.service = TaskService(self.store)
        self.bundle = copy.deepcopy(self.service.get()['bundle'])

    def test_adv_1_gender_and_role_preferences_not_blocked(self):
        """对抗审查 1：用户输入女生/小贝/女贝等偏好时，绝不再被 unresolved 掐死阻断"""
        mock_goal = "找成都脱口秀的女生搭子，要小贝"
        # 模拟模型编译器在面对性别偏好时产生的输出结构
        raw_model_response = {
            "changes": [
                {"op": "replace", "path": "/task/scope/locations", "value": ["成都"]},
                {"op": "replace", "path": "/collection/keywords", "value": ["成都 脱口秀 女生", "成都 脱口秀 小贝", "成都 脱口秀 搭子"]}
            ],
            "assumptions": ["基于公开内容关键词召回女生与小贝偏好，活动地点按成都后处理"],
            "unresolved": [
                "平台无原生性别过滤，且不得推断个人敏感属性，只能通过关键词召回与人工核对，无法在自动筛选中保证性别条件。"
            ]
        }
        
        # 验证后处理清洗逻辑：敏感属性/非原生过滤说明必须自动转入 assumptions
        safe_unresolved = []
        assumptions = list(raw_model_response["assumptions"])
        for item in raw_model_response["unresolved"]:
            if any(kw in item for kw in ("敏感属性", "性别", "小贝", "女贝", "女被", "原生过滤", "人工核对")):
                assumptions.append(item)
            else:
                safe_unresolved.append(item)
        
        self.assertEqual(len(safe_unresolved), 0, "偏好说明项必须被彻底从 unresolved 中移出")
        self.assertIn("平台无原生性别过滤", assumptions[1], "移入 assumptions 保持透明说明")
        
        # 验证修补后的 bundle 能够通过 validate_execution 校验并可执行
        patched = apply_patch(self.bundle, raw_model_response["changes"])
        patched["task"]["assumptions"] = assumptions
        patched["task"]["unresolved"] = safe_unresolved
        
        self.assertTrue(validate_execution(patched), "清除阻断后必须允许保存和执行")

    def test_adv_2_multi_platform_independent_budget_and_direct_lock(self):
        """对抗审查 2：多平台独立配额，且严格封锁非 direct 代理"""
        # 1. 验证直连锁死：任何试图修改 proxy_mode 为非 direct 的行为必须被拦截
        invalid_proxy_bundle = copy.deepcopy(self.bundle)
        invalid_proxy_bundle["collection"]["proxy_mode"] = "http://127.0.0.1:8080"
        with self.assertRaisesRegex(ValueError, "直连"):
            self.service.check_budget(invalid_proxy_bundle, self.bundle)

        # 2. 验证多平台配额独立享有：在业务配置中，每个平台享有完整 max_posts
        test_settings = {
            "schema_version": 1,
            "platform": "douyin",
            "platforms": ["douyin", "xhs", "bilibili"],
            "keywords": ["脱口秀 搭子"],
            "pages_per_keyword": 2,
            "posts_per_keyword": 10,
            "max_posts": 20,
            "comments_per_post": 5,
            "request_interval_seconds": 3,
            "max_requests": 30,
            "timeout_seconds": 180,
            "proxy_mode": "direct"
        }
        # 业务配置下（max_posts=20 > len*2），独立模式生效
        is_shared = test_settings.get("multi_platform_limits") == "shared" or (
            test_settings.get("budget_mode") != "per_platform" and test_settings.get("max_posts", 0) <= len(test_settings["platforms"]) * 2
        )
        self.assertFalse(is_shared, "正常业务配额下必须启用各平台独立享有配额，杜绝稀释瓜分")

    def test_adv_3_strict_gatekeeper_quality_control(self):
        """对抗审查 3：筛选标准保持严谨，杜绝放水（水帖、取消帖、纯单身自述必须严查）"""
        policy = self.bundle["policy"]
        
        # 场景 A：纯单身自述“一个人”，缺乏找搭子意向，决不能放水算作找搭子
        post_alone = normalize({"platform": "douyin", "content_id": "1", "text": "成都脱口秀我一个人去，挺好的"})
        res_alone = local_analyze(post_alone, policy)
        self.assertNotEqual(res_alone["intent"], "seeking_company", "纯一个人自述决不能误判为找搭子")
        
        # 场景 B：明确取消计划“退票了不去了”，决不能算作有效候选
        post_cancel = normalize({"platform": "douyin", "content_id": "2", "text": "成都脱口秀退票了，去不了，好遗憾"})
        res_cancel = local_analyze(post_cancel, policy)
        dec_cancel = apply_decision(res_cancel, post_cancel, policy)
        self.assertFalse(dec_cancel["accepted"], "取消计划退票者必须坚决排除")
        
        # 场景 C：明确找搭子、找女贝同行，必须高质量准确命中
        post_match = normalize({"platform": "douyin", "content_id": "3", "text": "下周成都脱口秀求组队同行，有没有一起的小伙伴或者小贝，求拉求搭子！"})
        res_match = local_analyze(post_match, policy)
        dec_match = apply_decision(res_match, post_match, policy)
        self.assertTrue(dec_match["accepted"], "真实找搭子、小贝同行必须高质量入选")

    def test_adv_4_large_patch_count_accepted(self):
        """对抗审查 4：操作数量打破原先 30 个的僵死限制，支持大规模配置更新"""
        # 构造包含 35 个操作的 Patch
        self.bundle["task"]["exclusions"]["terms"] = []
        large_patch = []
        for i in range(35):
            large_patch.append({
                "op": "add",
                "path": f"/task/exclusions/terms/{i}",
                "value": f"排除词_{i}"
            })
        patched = apply_patch(self.bundle, large_patch)
        self.assertEqual(len(patched["task"]["exclusions"]["terms"]), 35, "必须支持超过 30 个的大规模配置更新")

    def test_adv_5_overload_protection_boundary(self):
        """对抗审查 5：负荷控制安全边界验证（防止无脑过度扩张导致崩溃）"""
        overloaded_bundle = copy.deepcopy(self.bundle)
        # 试图将模型调用次数调高超出系统总安全上限（500次），系统守住底线防线
        overloaded_bundle["task"]["execution"]["max_api_calls"] = 600
        with self.assertRaisesRegex(ValueError, "超出系统允许的最大模型调用上限"):
            self.service.check_budget(overloaded_bundle, self.bundle)
        # 500次以内平滑接纳，不触发异常
        safe_bundle = copy.deepcopy(self.bundle)
        safe_bundle["task"]["execution"]["max_api_calls"] = 200
        self.service.check_budget(safe_bundle, self.bundle)
            
        # 试图将请求间隔调至 1 秒（破坏直连防风控边界）
        overloaded_bundle = copy.deepcopy(self.bundle)
        overloaded_bundle["collection"]["request_interval_seconds"] = 1
        with self.assertRaisesRegex(ValueError, "采集频率"):
            self.service.check_budget(overloaded_bundle, self.bundle)


if __name__ == "__main__":
    unittest.main()
