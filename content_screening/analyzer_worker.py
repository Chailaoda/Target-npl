"""独立认领和分析采集队列中的原始记录。"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import time
import uuid

from .active_config import active_snapshot
from .content_queue import finish, claim, fail, open_queue, stats, unclaim_stale
from .db_access import connect_readonly, resolve_db
from .normalization import normalize, normalize_profile
from .pipeline import fingerprint
from .workflow import run as workflow_run

logger = logging.getLogger("analyzer_worker")

_DEFAULT_CONFIG = {"batch_size": 30, "max_calls": 500, "interval": 60, "worker_id": "auto"}
_COMPLETED = {"completed", "completed_with_review"}


class AnalyzerWorker:
    def __init__(self, root, config=None):
        self.root = Path(root).resolve()
        self.config = {**_DEFAULT_CONFIG, **(config or {})}
        for key, label in (("batch_size", "批次大小"), ("interval", "轮询间隔")):
            if type(self.config[key]) is not int or self.config[key] < 1:
                raise ValueError(f"{label}必须为正整数")
        max_calls = self.config["max_calls"]
        if type(max_calls) is not int or not 1 <= max_calls <= 500:
            raise ValueError("每批模型调用上限必须为 1 至 500 的整数")
        worker_id = self.config["worker_id"]
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("分析进程标识必须为非空字符串")
        self.worker_id = uuid.uuid4().hex if worker_id == "auto" else worker_id
        self.queue = open_queue(self.root / "data/content_queue.sqlite3")

    def _write_raw(self, records):
        base = self.root / "data/scheduler/analysis"
        stamp = datetime.now(timezone.utc)
        while True:
            folder = base / stamp.strftime("%Y%m%dT%H%M%S%fZ")
            try:
                folder.mkdir(parents=True)
                break
            except FileExistsError:
                stamp += timedelta(microseconds=1)

        raw_path = folder / "raw.jsonl"
        with raw_path.open("w", encoding="utf-8", newline="\n") as raw:
            for item in records:
                raw.write(item["raw_json"] + "\n")
        return raw_path

    def process_batch(self):
        """处理一个队列批次，提交每条记录的最终状态。"""
        unclaim_stale(self.queue)
        if self.queue.execute("SELECT 1 FROM queue WHERE status='pending' LIMIT 1").fetchone() is None:
            return {"claimed": 0}

        snapshot = active_snapshot(self.root)
        task = snapshot["bundle"].get("task")
        execution = task.get("execution", {}) if task else {}
        mode = execution.get("mode", "ark")
        max_calls = min(self.config["max_calls"], execution.get("max_api_calls", 500))
        limit = self.config["batch_size"]
        if mode == "ark":
            # 每条原文最多触发一次调用，额度外的记录继续排队。
            limit = min(limit, max_calls)
        items = claim(self.queue, limit, self.worker_id)
        if not items:
            return {"claimed": 0}

        failures = {}
        valid = []
        hashes = {}
        done = []
        api_calls = 0
        result = {}
        try:
            for item in items:
                item_id = item["id"]
                try:
                    raw_json = item["raw_json"]
                    if len(raw_json.splitlines()) != 1:
                        raise ValueError("原文包含导入流程无法解析的换行符")
                    row = json.loads(raw_json)
                    # 提前执行导入的逐条校验，避免坏记录阻塞同批其他记录。
                    source_hash = fingerprint(normalize(row))
                    normalize_profile(row)
                    fingerprint(row)
                    if isinstance(row.get("keyword"), (dict, list)):
                        raise ValueError("来源关键词字段无效")
                except (ValueError, TypeError, KeyError, OverflowError, OSError, RecursionError) as error:
                    failures[item_id] = f"原始记录校验失败（{type(error).__name__}）"
                    continue
                valid.append(item)
                hashes[item_id] = source_hash

            if valid:
                raw_path = self._write_raw(valid)
                db_path = resolve_db()
                result = workflow_run(
                    raw_path, snapshot["policy_path"], db_path,
                    self.root / "output/scheduler", mode=mode,
                    connection_path=self.root / "config/connection.json", max_calls=max_calls,
                    task=task, config_revision=snapshot["revision"],
                )
                api_calls = result.get("api_calls", 0)
                status = result.get("status")
                if status in _COMPLETED:
                    done = list(hashes)
                elif status == "partial":
                    with closing(connect_readonly(db_path)) as db:
                        outcomes = {source_hash: (state, error_code) for source_hash, state, error_code in db.execute(
                            "SELECT source_hash, status, error_code FROM v2_tasks WHERE job_id=?",
                            (result["job_id"],),
                        )}
                    for item_id, source_hash in hashes.items():
                        state, error = outcomes.get(source_hash, (None, None))
                        if state in ("completed", "needs_review"):
                            done.append(item_id)
                        else:
                            failures[item_id] = f"分析任务未完成（{error or state or '缺少任务记录'}）"
                else:
                    raise ValueError("分析流程返回未知状态")
        except KeyboardInterrupt:
            fail(self.queue, [item["id"] for item in items], "分析进程收到退出信号")
            raise
        except Exception as error:
            logger.warning("批次分析失败（%s）", type(error).__name__)
            for item in items:
                failures.setdefault(item["id"], f"批次分析失败（{type(error).__name__}）")
            done = []

        finish(self.queue, done, failures)
        if done and result.get("job_id"):
            try:
                from .profile_refresh import refresh_for_job
                from .runtime_context import current_context
                refreshed, topics = refresh_for_job(
                    current_context(), result["job_id"])
            except Exception:
                logger.warning("画像增量刷新失败", exc_info=True)
        return {"claimed": len(items), "done": len(done), "failed": len(failures), "api_calls": api_calls}

    def run(self, interval=None):
        """持续轮询队列，收到退出信号后关闭连接。"""
        interval = self.config["interval"] if interval is None else interval
        if type(interval) is not int or interval < 1:
            raise ValueError("轮询间隔必须为正整数")
        logger.info("分析进程启动，每 %d 秒轮询一次", interval)
        try:
            while True:
                try:
                    result = self.process_batch()
                    if result["claimed"]:
                        logger.info("本批认领 %d 条，完成 %d 条，失败 %d 条，模型调用 %d 次",
                                    result["claimed"], result["done"], result["failed"], result["api_calls"])
                except Exception as error:
                    logger.warning("处理队列批次时发生异常（%s）", type(error).__name__)
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("分析进程已停止")
        finally:
            self.close()

    def close(self):
        """关闭队列连接。"""
        self.queue.close()


def main():
    parser = argparse.ArgumentParser(description="独立消费采集内容队列")
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_CONFIG["batch_size"], help="每批最多认领的记录数")
    parser.add_argument("--max-calls", type=int, default=_DEFAULT_CONFIG["max_calls"], help="每批模型调用上限")
    parser.add_argument("--interval", type=int, default=_DEFAULT_CONFIG["interval"], help="轮询间隔（秒）")
    parser.add_argument("--status", action="store_true", help="显示队列统计后退出")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from .runtime_context import current_context
    root = current_context().state_root
    if args.status:
        with closing(open_queue(root / "data/content_queue.sqlite3")) as db:
            print(json.dumps(stats(db), ensure_ascii=False, indent=2))
        return 0

    try:
        worker = AnalyzerWorker(root, {"batch_size": args.batch_size, "max_calls": args.max_calls,
                                       "interval": args.interval})
    except ValueError as error:
        parser.error(str(error))
    worker.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
