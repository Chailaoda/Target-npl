import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from festival_match.connectors import ARK_HOSTS, HUOSHAN_HOSTS, DOUYIN_HOSTS, ark_evaluator, fetch_huoshan, fetch_public_video, load_connection
from festival_match.network import NetworkError, RestrictedRedirect, check_url, make_opener
from festival_match.pipeline import load_profile


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.config = load_connection(root / "config/connection.json")
        self.profile = load_profile(root / "profiles/music_festival.json")

    def test_data_direct_ignores_all_proxy_discovery(self):
        fake = {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "socks5://127.0.0.1:1"}
        with patch.dict(os.environ, fake), patch("urllib.request.getproxies", side_effect=AssertionError("must not discover proxy")):
            opener = make_opener("direct", HUOSHAN_HOSTS)
            self.assertTrue(opener)
            self.assertEqual(os.environ["HTTPS_PROXY"], fake["HTTPS_PROXY"])

    def test_model_channel_keeps_own_proxy_policy(self):
        with patch("urllib.request.getproxies", return_value={"https": "http://127.0.0.1:1"}) as discovery:
            make_opener("system", ARK_HOSTS)
            discovery.assert_called_once()

    def test_external_http_and_credential_redirects_rejected(self):
        import urllib.request
        for url in ["http://www.huoshan.com/", "https://evil.test/", "https://www.huoshan.com@evil.test/", "https://www.huoshan.com:444/"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                check_url(url, HUOSHAN_HOSTS)
        req = urllib.request.Request("https://ark.cn-beijing.volces.com/api/v3/chat/completions", headers={"Authorization": "Bearer test"})
        with self.assertRaises(NetworkError):
            RestrictedRedirect(ARK_HOSTS).redirect_request(req, None, 302, "", {}, "https://ark.cn-beijing.volces.com/elsewhere")

    def test_app_shell_is_not_imported_as_a_post(self):
        with patch("festival_match.connectors.request", return_value=(b'<meta property="og:type" content="website">', {})):
            with self.assertRaises(ValueError):
                fetch_huoshan("https://www.huoshan.com/", "unused-output")

    def test_model_response_is_bound_to_text_and_model(self):
        self.config["ark"]["credential_kind"] = "pay_as_you_go"
        self.config["ark"]["base_url"] = "https://ark.cn-beijing.volces.com/api/v3"
        self.config["ark"]["model"] = "deepseek-v4-1-flash-261017"
        envelope = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"label": "positive_interest", "evidence_quote": "我喜欢音乐节", "reason": "明确自述"}, ensure_ascii=False)}}], "usage": {"total_tokens": 100}}
        with patch("festival_match.connectors.read_key", return_value="not-a-real-key"), patch("festival_match.connectors.request", return_value=(json.dumps(envelope).encode(), {})) as transport:
            evaluate = ark_evaluator(self.config, self.profile)
            result = evaluate({"text": "我喜欢音乐节"}, self.profile)
            self.assertTrue(result["accepted"])
            self.assertEqual(result["usage"]["total_tokens"], 100)
            self.assertEqual(transport.call_args.kwargs["payload"]["model"], "deepseek-v4-1-flash-261017")
            with self.assertRaises(ValueError):
                evaluate({"text": "这只是新闻报道"}, self.profile)

    def test_no_model_calls_without_project_key(self):
        self.config["ark"]["credential_kind"] = "pay_as_you_go"
        self.config["ark"]["base_url"] = "https://ark.cn-beijing.volces.com/api/v3"
        self.config["ark"]["model"] = "deepseek-v4-1-flash-261017"
        with patch("festival_match.connectors.read_key", side_effect=ValueError("missing")), patch("festival_match.connectors.request") as transport:
            with self.assertRaises(ValueError):
                ark_evaluator(self.config, self.profile)
            transport.assert_not_called()

    def test_hashtag_only_evidence_is_not_confirmed_intent(self):
        for text, quote in (("#音乐节搭子 #青春音乐节", "#音乐节搭子 #青春音乐节"), ("快乐周末 #音乐节搭子", "音乐节搭子")):
            with self.subTest(text=text):
                envelope = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"label": "seeking_company", "evidence_quote": quote, "reason": "模型误把标签视为邀请"}, ensure_ascii=False)}}]}
                with patch("festival_match.connectors.read_key", return_value="test-key"), patch("festival_match.connectors.request", return_value=(json.dumps(envelope).encode(), {})):
                    result = ark_evaluator(self.config, self.profile)({"text": text}, self.profile)
                self.assertFalse(result["accepted"])
                self.assertEqual(result["label"], "mention_only")

    def test_coding_plan_is_not_sent_to_wrong_api_route(self):
        self.config["ark"]["base_url"] = "https://ark.cn-beijing.volces.com/api/v3"
        with patch("festival_match.connectors.read_key") as secret, patch("festival_match.connectors.request") as transport:
            with self.assertRaisesRegex(ValueError, "Coding Plan"):
                ark_evaluator(self.config, self.profile)
            secret.assert_not_called()
            transport.assert_not_called()

    def test_selected_platform_is_douyin_and_requires_direct(self):
        self.assertEqual(self.config["source"], {"platform": "douyin", "proxy_mode": "direct"})
        check_url("https://www.douyin.com/search/test", DOUYIN_HOSTS)
        with self.assertRaises(ValueError):
            check_url("https://www.douyin.com/search/test", HUOSHAN_HOSTS)

    def test_douyin_shell_fails_without_output(self):
        with patch("festival_match.connectors.request", return_value=(b'<html><script>/* app shell */</script></html>', {})) as transport:
            with self.assertRaises(ValueError):
                fetch_public_video("https://www.douyin.com/search/test", "unused-output", "douyin")
            self.assertEqual(transport.call_args.kwargs["mode"], "direct")
