import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("project_mc_adapter", ROOT / "scripts/mediacrawler_collect.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class AdapterTests(unittest.TestCase):
    def test_direct_http_client_forces_no_environment_proxy_and_keeps_tls(self):
        fake_httpx = types.SimpleNamespace(AsyncClient=lambda **kwargs: kwargs)
        with patch.dict("sys.modules", {"httpx": fake_httpx}):
            result = adapter.make_direct_client(proxy=None, trust_env=True, verify=False, follow_redirects=True)
        self.assertEqual(result, {"trust_env": False, "verify": True, "follow_redirects": False})

    def test_proxy_and_unrelated_browser_metadata_fail_closed(self):
        with self.assertRaises(ValueError):
            adapter.make_direct_client(proxy="http://proxy.example")
        session = {"profile_path": str(ROOT / "cache/other"), "debug_host": "127.0.0.1", "proxy_mode": "direct", "debug_port": 3302}
        with patch.object(adapter.subprocess, "Popen") as launch, self.assertRaises(ValueError):
            adapter.ensure_browser(session)
        launch.assert_not_called()

    def test_collection_config_is_bounded(self):
        # Validate the shipped default, not the operator's personal copy of
        # config/collection.json (which is gitignored and locally tuned).
        settings = adapter.load_settings(ROOT / "examples/collection.example.json")
        self.assertEqual(settings["keywords"], ["示例活动"])
        self.assertLessEqual(settings["max_posts"], 60)


if __name__ == "__main__":
    unittest.main()
