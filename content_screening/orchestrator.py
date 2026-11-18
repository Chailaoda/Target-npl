"""多平台并行采集编排器，统一管理采集池和分析进程。

用法：python -m content_screening.orchestrator [--interval 900] [--max-collectors 3]
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import threading
import time

from .active_config import active_snapshot
from .content_queue import enqueue_from_raw, open_queue, stats as queue_stats
from .dedup import open_index, ingest_raw, stats as dedup_stats
from .scheduler import Scheduler, _any_task_running, _utcnow

logger = logging.getLogger("orchestrator")

_DEFAULT_CONFIG = {
    "interval_seconds": 900,
    "max_collectors": 3,
    "batch_max_posts": 30,
    "batch_max_requests": 60,
    "enable_analyzer": True,
    "analyzer_batch_size": 30,
    "analyzer_max_calls": 500,
    "analyzer_interval": 60,
    "analyzer_restart_delay": 30,
}


def _collect_in_thread(context, settings, folder, root):
    """工作线程函数：设置 RuntimeContext 后执行采集子进程。"""
    from .runtime_context import set_context
    set_context(context)
    from .multi_collection import collect_selected
    return collect_selected(settings, folder, root)


class Orchestrator:
    def __init__(self, root, config=None):
        self.root = Path(root).resolve()
        self.config = {**_DEFAULT_CONFIG, **(config or {})}
        self._last_config_revision = None
        self.scheduler = Scheduler(root, {
            k: self.config[k]
            for k in ("interval_seconds", "batch_max_posts", "batch_max_requests")
            if k in self.config
        })

    def tick(self):
        """并行采集所有就绪平台，结果在主线程入队。"""
        self.scheduler.state["round"] += 1
        round_num = self.scheduler.state["round"]
        logger.info("编排第 %d 轮", round_num)

        if _any_task_running(self.root):
            logger.info("手动任务运行中，跳过本轮")
            self.scheduler._save()
            return {"round": round_num, "skipped": "manual_task_running"}

        snapshot = active_snapshot(self.root)
        revision = snapshot["revision"]
        if self._last_config_revision and revision != self._last_config_revision:
            logger.info("检测到配置变更：%s → %s",
                        self._last_config_revision[:8], revision[:8])
        self._last_config_revision = revision

        candidates = self.scheduler.available_platforms(snapshot=snapshot)
        if not candidates:
            logger.info("本轮无可用采集平台")
            self.scheduler._save()
            return {"round": round_num, "collections": {}}

        candidates.sort(key=lambda x: x[1]["last_run_at"] or "")
        platforms = [p for p, _ in candidates[:self.config["max_collectors"]]]
        logger.info("本轮并行采集 %d 个平台：%s", len(platforms), ", ".join(platforms))
        tasks = {}
        for platform in platforms:
            settings = dict(snapshot["bundle"]["collection"])
            settings["platform"] = platform
            settings["platforms"] = [platform]
            settings["max_posts"] = min(
                settings.get("max_posts", 60), self.config["batch_max_posts"])
            settings["max_requests"] = min(
                settings.get("max_requests", 120), self.config["batch_max_requests"])
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            folder = self.scheduler.base / "runs" / stamp
            folder.mkdir(parents=True, exist_ok=True)
            tasks[platform] = (settings, folder, stamp)

        from .runtime_context import current_context
        context = current_context()
        raw_results = {}

        with ThreadPoolExecutor(max_workers=self.config["max_collectors"]) as pool:
            futures = {}
            for platform, (settings, folder, _stamp) in tasks.items():
                fut = pool.submit(
                    _collect_in_thread, context, settings, folder, self.root)
                futures[fut] = platform

            for fut in as_completed(futures):
                platform = futures[fut]
                try:
                    raw_results[platform] = fut.result()
                except Exception:
                    logger.exception("%s 采集子进程异常", platform)
                    raw_results[platform] = {
                        "status": "error", "raw_file": None, "records": 0}

        summary = {}
        for platform in platforms:
            result = raw_results.get(platform)
            if result is None:
                continue
            stamp = tasks[platform][2]
            info = self._process_result(platform, result, stamp)
            summary[platform] = info

        self.scheduler._save()
        return {
            "round": round_num,
            "collections": summary,
            "dedup": dedup_stats(self.scheduler.dedup),
        }

    def _process_result(self, platform, result, stamp):
        """在主线程中处理采集结果：入队、去重、更新平台状态。"""
        ok = result["status"] in ("completed", "completed_empty")
        raw_path = Path(result["raw_file"]) if result.get("raw_file") else None
        new_count = 0
        if ok and raw_path and raw_path.is_file() and raw_path.stat().st_size > 0:
            new_count = enqueue_from_raw(self.scheduler.queue, raw_path, stamp)
            ingest_raw(self.scheduler.dedup, raw_path, stamp)
            logger.info("%s 新增入队 %d 条", platform, new_count)

        ps = self.scheduler._ps(platform)
        ps["last_run_at"] = _utcnow()
        ps["total_runs"] += 1
        ps["total_new_records"] += new_count

        if ok:
            ps["consecutive_failures"] = 0
            ps["last_success_at"] = ps["last_run_at"]
        else:
            ps["consecutive_failures"] += 1
            if ps["consecutive_failures"] >= self.scheduler.config["max_consecutive_failures"]:
                ps["cooldown_until_round"] = (
                    self.scheduler.state["round"]
                    + self.scheduler.config["cooldown_rounds"])
                logger.warning(
                    "%s 连续失败 %d 次，进入冷却",
                    platform, ps["consecutive_failures"])

        return {
            "platform": platform,
            "status": result["status"],
            "new_records": new_count,
            "total_records": result.get("records", 0),
        }

    def run(self, interval=None):
        """启动编排器主循环，含后台分析进程。"""
        interval = interval or self.config["interval_seconds"]

        from .runtime_context import current_context, set_context
        set_context(current_context())

        analyzer_thread = None
        if self.config.get("enable_analyzer", True):
            ctx = current_context()
            analyzer_thread = threading.Thread(
                target=self._run_analyzer, args=(ctx,),
                daemon=True, name="analyzer")
            analyzer_thread.start()

        logger.info(
            "编排器启动：采集间隔 %d 秒，最多 %d 平台并行，分析进程 %s",
            interval, self.config["max_collectors"],
            "已启动" if analyzer_thread else "未启用")
        logger.info("去重索引统计：%s", dedup_stats(self.scheduler.dedup))

        try:
            while True:
                try:
                    result = self.tick()
                    logger.info("第 %d 轮编排结束", result["round"])
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logger.exception("编排轮次失败")
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("编排器已停止")
        finally:
            self.close()

    def _run_analyzer(self, context):
        """后台分析线程：异常时自动重启。守护线程不会收到 KeyboardInterrupt，退出依赖主进程终止。"""
        from .runtime_context import set_context
        set_context(context)
        restart_delay = self.config.get("analyzer_restart_delay", 30)
        while True:
            try:
                from .analyzer_worker import AnalyzerWorker
                worker = AnalyzerWorker(self.root, {
                    "batch_size": self.config.get("analyzer_batch_size", 30),
                    "max_calls": self.config.get("analyzer_max_calls", 500),
                    "interval": self.config.get("analyzer_interval", 60),
                })
                worker.run()
                break
            except KeyboardInterrupt:
                break
            except Exception:
                logger.exception("分析进程异常退出，%d 秒后重启", restart_delay)
                time.sleep(restart_delay)

    def close(self):
        self.scheduler.close()


def main():
    parser = argparse.ArgumentParser(description="多平台并行采集编排器")
    parser.add_argument("--interval", type=int, default=900,
                        help="采集间隔（秒，默认 900）")
    parser.add_argument("--max-collectors", type=int, default=3,
                        help="最大并行采集数（默认 3）")
    parser.add_argument("--batch-max-posts", type=int, default=30,
                        help="每平台每轮最多采集作品数")
    parser.add_argument("--no-analyzer", action="store_true",
                        help="不启动后台分析进程")
    parser.add_argument("--status", action="store_true",
                        help="显示编排器状态后退出")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)])

    from .runtime_context import current_context, set_context
    ctx = current_context()
    set_context(ctx)
    root = Path(ctx.state_root)

    if args.status:
        state_path = root / "data/scheduler/state.json"
        output = {}
        if state_path.exists():
            output["state"] = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            output["state"] = "调度器尚未运行"
        dedup_path = root / "data/dedup.sqlite3"
        if dedup_path.exists():
            with closing(open_index(dedup_path)) as db:
                output["dedup"] = dedup_stats(db)
        queue_path = root / "data/content_queue.sqlite3"
        if queue_path.exists():
            with closing(open_queue(queue_path)) as db:
                output["queue"] = queue_stats(db)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    config = {
        "interval_seconds": args.interval,
        "max_collectors": args.max_collectors,
        "batch_max_posts": args.batch_max_posts,
        "enable_analyzer": not args.no_analyzer,
    }
    orchestrator = Orchestrator(root, config)
    orchestrator.run(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
