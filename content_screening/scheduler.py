"""轮转采集平台并将采集原文写入分析队列。

用法：python -m content_screening.scheduler [--interval 900]
每轮运行至多一个平台的采集。有手动任务运行时跳过该轮。
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import socket
import sys
import time

from .active_config import active_snapshot
from .content_queue import enqueue_from_raw, open_queue
from .dedup import open_index, ingest_raw, stats as dedup_stats
from .platforms import selected
from .task_service import atomic_json, process_alive

logger = logging.getLogger("scheduler")

_DEFAULT_CONFIG = {
    "interval_seconds": 900,
    "max_consecutive_failures": 3,
    "cooldown_rounds": 4,
    "batch_max_posts": 30,
    "batch_max_requests": 60,
}
_LEGACY_ANALYSIS_KEYS = {"pending_records", "pending_raw_files", "last_analysis_round"}


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _check_browser(root, platform):
    path = root / f"cache/{platform}-browser-session.json"
    if not path.exists():
        return False
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
        pid = session.get("pid")
        port = session.get("debug_port")
        if not port:
            return False
        if pid and not process_alive(pid):
            return False
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _any_task_running(root):
    runs = root / "data/task-runs"
    if not runs.is_dir():
        return False
    for state_file in runs.glob("*/status.json"):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if state.get("status") in ("queued", "running"):
            launch = state_file.parent / "launch.json"
            pid = state.get("pid")
            if not pid and launch.exists():
                try:
                    pid = json.loads(launch.read_text(encoding="utf-8")).get("pid")
                except (OSError, json.JSONDecodeError):
                    pass
            if process_alive(pid):
                return True
    return False


class Scheduler:
    def __init__(self, root, config=None):
        self.root = Path(root).resolve()
        self.config = {**_DEFAULT_CONFIG, **(config or {})}
        self.base = self.root / "data/scheduler"
        self.base.mkdir(parents=True, exist_ok=True)
        self.state_path = self.base / "state.json"
        with ExitStack() as cleanup:
            self.dedup = cleanup.enter_context(closing(open_index(self.root / "data/dedup.sqlite3")))
            self.queue = cleanup.enter_context(closing(open_queue(self.root / "data/content_queue.sqlite3")))
            self.state = self._load_state()
            if _LEGACY_ANALYSIS_KEYS & self.state.keys():
                self._migrate_pending()
            cleanup.pop_all()

    _REQUIRED_KEYS = {"platforms", "round"}

    def _new_state(self):
        return {
            "platforms": {},
            "round": 0,
            "created_at": _utcnow(),
        }

    def _load_state(self):
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("读取调度状态失败，重新初始化：%s", e)
                return self._new_state()
            if not isinstance(data, dict) or not self._REQUIRED_KEYS.issubset(data):
                logger.warning("调度状态缺少必要字段，重新初始化")
                return self._new_state()
            return data
        return self._new_state()

    def _migrate_pending(self):
        """一次性导入旧版待分析文件，成功后移除旧分析字段。"""
        runs = (self.base / "runs").resolve()
        pending = self.state.get("pending_raw_files", [])
        if not isinstance(pending, list):
            logger.warning("旧版调度状态的待分析列表无效")
            pending = []
        for value in pending:
            if not isinstance(value, str):
                logger.warning("旧版调度状态包含无效的待分析文件路径")
                continue
            raw_path = Path(value).resolve()
            if not raw_path.is_relative_to(runs) or not raw_path.is_file():
                logger.warning("无法导入旧版待分析文件：%s", raw_path)
                continue
            enqueue_from_raw(self.queue, raw_path, raw_path.parent.name)
        for key in _LEGACY_ANALYSIS_KEYS:
            self.state.pop(key, None)
        self._save()

    def _save(self):
        self.state["updated_at"] = _utcnow()
        atomic_json(self.state_path, self.state)

    def _ps(self, platform):
        return self.state["platforms"].setdefault(platform, {
            "last_run_at": None,
            "last_success_at": None,
            "consecutive_failures": 0,
            "total_runs": 0,
            "total_new_records": 0,
            "cooldown_until_round": 0,
        })

    def available_platforms(self, snapshot=None):
        if snapshot is None:
            try:
                snapshot = active_snapshot(self.root)
            except (OSError, ValueError) as e:
                logger.warning("无法读取当前配置：%s", e)
                return []
        settings = snapshot["bundle"]["collection"]
        platforms = selected(settings)
        candidates = []
        for p in platforms:
            ps = self._ps(p)
            if ps["cooldown_until_round"] > self.state["round"]:
                logger.debug("跳过 %s，冷却至第 %d 轮", p, ps["cooldown_until_round"])
                continue
            if not _check_browser(self.root, p):
                logger.debug("跳过 %s，浏览器尚未就绪", p)
                continue
            candidates.append((p, ps))
        return candidates

    def next_platform(self):
        candidates = self.available_platforms()
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1]["last_run_at"] or "")
        return candidates[0][0]

    def run_collection(self, platform):
        snapshot = active_snapshot(self.root)
        settings = dict(snapshot["bundle"]["collection"])
        settings["platform"] = platform
        settings["platforms"] = [platform]
        settings["max_posts"] = min(settings.get("max_posts", 60), self.config["batch_max_posts"])
        settings["max_requests"] = min(settings.get("max_requests", 120), self.config["batch_max_requests"])

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder = self.base / "runs" / stamp
        folder.mkdir(parents=True, exist_ok=True)

        from .multi_collection import collect_selected
        result = collect_selected(settings, folder, self.root)

        ok = result["status"] in ("completed", "completed_empty")
        raw_path = Path(result["raw_file"]) if result.get("raw_file") else None
        new_count = 0
        if ok and raw_path and raw_path.is_file() and raw_path.stat().st_size > 0:
            # 优先持久化队列，去重索引同步失败时原文仍可独立消费。
            new_count = enqueue_from_raw(self.queue, raw_path, stamp)
            ingest_raw(self.dedup, raw_path, stamp)
            logger.info("%s 本轮新增入队 %d 条", platform, new_count)

        ps = self._ps(platform)
        ps["last_run_at"] = _utcnow()
        ps["total_runs"] += 1
        ps["total_new_records"] += new_count

        if ok:
            ps["consecutive_failures"] = 0
            ps["last_success_at"] = ps["last_run_at"]
        else:
            ps["consecutive_failures"] += 1
            if ps["consecutive_failures"] >= self.config["max_consecutive_failures"]:
                ps["cooldown_until_round"] = self.state["round"] + self.config["cooldown_rounds"]
                logger.warning("%s 连续失败 %d 次，进入冷却",
                               platform, ps["consecutive_failures"])

        self._save()
        return {"platform": platform, "status": result["status"],
                "new_records": new_count, "total_records": result.get("records", 0)}

    def tick(self):
        self.state["round"] += 1
        logger.info("开始第 %d 轮采集", self.state["round"])

        if _any_task_running(self.root):
            logger.info("手动任务正在运行，跳过本轮采集")
            self._save()
            return {"round": self.state["round"], "skipped": "manual_task_running"}

        platform = self.next_platform()
        collection = None

        if platform:
            try:
                collection = self.run_collection(platform)
            except Exception:
                logger.exception("%s 采集失败", platform)
                ps = self._ps(platform)
                ps["last_run_at"] = _utcnow()
                ps["consecutive_failures"] += 1
                if ps["consecutive_failures"] >= self.config["max_consecutive_failures"]:
                    ps["cooldown_until_round"] = self.state["round"] + self.config["cooldown_rounds"]
                    logger.warning("%s 连续失败 %d 次，进入冷却",
                                   platform, ps["consecutive_failures"])
                self._save()
        else:
            logger.info("本轮无可用采集平台")

        self._save()
        return {"round": self.state["round"], "platform": platform,
                "collection": collection, "dedup": dedup_stats(self.dedup)}

    def close(self):
        try:
            self.queue.close()
        finally:
            try:
                self.dedup.close()
            finally:
                self._save()

    def run(self, interval=None):
        interval = interval or self.config["interval_seconds"]

        from .runtime_context import current_context, set_context
        set_context(current_context())

        logger.info("调度器启动，每 %d 秒采集一轮", interval)
        logger.info("去重索引统计：%s", dedup_stats(self.dedup))

        try:
            while True:
                try:
                    result = self.tick()
                    logger.info("第 %d 轮调度结束", result["round"])
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logger.exception("调度轮次失败")
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("调度器已停止")
        finally:
            self.close()


def main():
    parser = argparse.ArgumentParser(description="轮转采集平台并写入内容队列")
    parser.add_argument("--interval", type=int, default=900, help="调度间隔（秒，默认 900）")
    parser.add_argument("--batch-max-posts", type=int, default=30, help="每轮最多采集的作品数")
    parser.add_argument("--status", action="store_true", help="显示当前调度状态后退出")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr)])

    from .runtime_context import current_context, set_context
    ctx = current_context()
    set_context(ctx)
    root = Path(ctx.state_root)

    if args.status:
        state_path = root / "data/scheduler/state.json"
        if not state_path.exists():
            print("调度器尚未运行")
            return 0
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            for key in _LEGACY_ANALYSIS_KEYS:
                state.pop(key, None)
        dedup_path = root / "data/dedup.sqlite3"
        ds = {}
        if dedup_path.exists():
            with closing(open_index(dedup_path)) as db:
                ds = dedup_stats(db)
        print(json.dumps({"state": state, "dedup": ds}, ensure_ascii=False, indent=2))
        return 0

    config = {
        "interval_seconds": args.interval,
        "batch_max_posts": args.batch_max_posts,
    }
    scheduler = Scheduler(root, config)
    scheduler.run(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
