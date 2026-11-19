"""跨平台、跨批次的全局日预算控制器。

每日预算以 UTC 日期为周期，通过 JSON 文件持久化。
采集和分析组件各自实例化，共享同一天的预算文件。
每次读写均从磁盘加载最新数据，避免多实例缓存覆盖。
"""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

from .task_service import atomic_json

logger = logging.getLogger("budget")

_DEFAULT_BUDGET = {
    "daily_api_calls": 2000,
    "daily_collection_posts": 500,
}


def _utctoday():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class BudgetController:
    def __init__(self, root, config=None):
        self.root = Path(root).resolve()
        self.config = {**_DEFAULT_BUDGET, **(config or {})}
        self._budget_dir = self.root / "data/budget"
        self._budget_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, date):
        return self._budget_dir / f"{date}.json"

    def _load(self):
        today = _utctoday()
        path = self._path(today)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("预算文件损坏，重新初始化")
        return self._new(today)

    def _new(self, date):
        return {
            "date": date,
            "api_calls": 0,
            "collection_posts": 0,
            "by_platform": {},
            "rounds": 0,
            "created_at": _utcnow(),
        }

    def _save(self, data):
        data["updated_at"] = _utcnow()
        atomic_json(self._path(data["date"]), data)

    def available_api_calls(self):
        """返回今日剩余 API 调用配额。"""
        data = self._load()
        return max(0, self.config["daily_api_calls"] - data["api_calls"])

    def record_api_calls(self, count):
        """记录已消耗的 API 调用数。"""
        if count <= 0:
            return
        data = self._load()
        data["api_calls"] += count
        self._save(data)
        remaining = self.config["daily_api_calls"] - data["api_calls"]
        if remaining <= 0:
            logger.warning("今日 API 预算已耗尽（%d/%d）",
                           data["api_calls"], self.config["daily_api_calls"])

    def available_collection_posts(self):
        """返回今日剩余采集内容配额。"""
        data = self._load()
        return max(0, self.config["daily_collection_posts"]
                   - data["collection_posts"])

    def record_collection(self, platform, new_posts):
        """记录平台采集消耗。"""
        if new_posts <= 0:
            return
        data = self._load()
        data["collection_posts"] += new_posts
        bp = data.setdefault("by_platform", {})
        bp[platform] = bp.get(platform, 0) + new_posts
        self._save(data)

    def record_round(self):
        """记录完成的编排轮次。"""
        data = self._load()
        data["rounds"] += 1
        self._save(data)

    def record_tick(self, collections, round_increment=1):
        """批量记录一轮编排的所有采集消耗和轮次增量，单次磁盘写入。

        collections: list of (platform, new_posts) tuples
        """
        data = self._load()
        for platform, new_posts in collections:
            if new_posts <= 0:
                continue
            data["collection_posts"] += new_posts
            bp = data.setdefault("by_platform", {})
            bp[platform] = bp.get(platform, 0) + new_posts
        data["rounds"] += round_increment
        self._save(data)

    def status(self):
        """返回当日预算使用摘要。"""
        data = self._load()
        api_limit = self.config["daily_api_calls"]
        post_limit = self.config["daily_collection_posts"]
        return {
            "date": data["date"],
            "api_calls": {
                "used": data["api_calls"],
                "limit": api_limit,
                "remaining": max(0, api_limit - data["api_calls"]),
            },
            "collection_posts": {
                "used": data["collection_posts"],
                "limit": post_limit,
                "remaining": max(0, post_limit - data["collection_posts"]),
            },
            "rounds": data["rounds"],
            "by_platform": data.get("by_platform", {}),
            "updated_at": data.get("updated_at"),
        }
