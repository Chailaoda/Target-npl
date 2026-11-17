"""Persistent scheduler: rotates platforms, deduplicates, decouples collection/analysis.

Usage:
    python -m content_screening.scheduler [--interval 900] [--analysis-threshold 50]

The scheduler runs as a long-lived process. Each "tick" picks one platform,
runs a small collection batch, deduplicates the results against the global
index, and queues the new records for analysis. Analysis runs when enough new
records accumulate or after a configurable number of collection rounds.

The scheduler does NOT go through TaskService — it directly uses
collect_selected() and workflow.run(), managing its own lifecycle.
It respects the existing single-task mutex: if a manual task is running
(via Web UI or CLI), the scheduler skips that tick.

Known limitations (deferred to future iterations):
- #1: PID-based mutex has race windows; needs shared atomic lock with TaskService
- #2: Dedup commit and pending queue save are not atomic; crash between them
      loses records; needs unified SQLite transaction or recovery log
- #4: Large backlogs exceeding max_calls are retried whole; needs budget-based
      batch splitting
- #7: filter_unseen → ingest_raw TOCTOU under concurrent callers; needs
      atomic claim mechanism
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import socket
import sys
import time

from .active_config import active_snapshot
from .dedup import open_index, ingest_raw, filter_unseen, stats as dedup_stats
from .platforms import PLATFORMS, selected
from .task_service import atomic_json, process_alive

logger = logging.getLogger("scheduler")

_DEFAULT_CONFIG = {
    "interval_seconds": 900,
    "analysis_threshold": 50,
    "analysis_interval_rounds": 6,
    "max_consecutive_failures": 3,
    "cooldown_rounds": 4,
    "batch_max_posts": 30,
    "batch_max_requests": 60,
}


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
        self.state = self._load_state()
        self.dedup = open_index(self.root / "data/dedup.sqlite3")

    _REQUIRED_KEYS = {"platforms", "round", "pending_records", "pending_raw_files",
                       "last_analysis_round"}

    def _new_state(self):
        return {
            "platforms": {},
            "round": 0,
            "pending_records": 0,
            "pending_raw_files": [],
            "last_analysis_round": 0,
            "created_at": _utcnow(),
        }

    def _load_state(self):
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("corrupt state file, starting fresh: %s", e)
                return self._new_state()
            if not isinstance(data, dict) or not self._REQUIRED_KEYS.issubset(data):
                logger.warning("state file missing required keys, starting fresh")
                return self._new_state()
            return data
        return self._new_state()

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

    # ---- platform selection ------------------------------------------------

    def next_platform(self):
        try:
            snapshot = active_snapshot(self.root)
        except (OSError, ValueError) as e:
            logger.warning("cannot read active config: %s", e)
            return None
        settings = snapshot["bundle"]["collection"]
        platforms = selected(settings)
        candidates = []
        for p in platforms:
            ps = self._ps(p)
            if ps["cooldown_until_round"] > self.state["round"]:
                logger.debug("skip %s (cooldown until round %d)", p, ps["cooldown_until_round"])
                continue
            if not _check_browser(self.root, p):
                logger.debug("skip %s (browser not ready)", p)
                continue
            candidates.append((p, ps))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1]["last_run_at"] or "")
        return candidates[0][0]

    # ---- collection --------------------------------------------------------

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

        ps = self._ps(platform)
        ps["last_run_at"] = _utcnow()
        ps["total_runs"] += 1

        ok = result["status"] in ("completed", "completed_empty")
        if ok:
            ps["consecutive_failures"] = 0
            ps["last_success_at"] = ps["last_run_at"]
        else:
            ps["consecutive_failures"] += 1
            if ps["consecutive_failures"] >= self.config["max_consecutive_failures"]:
                ps["cooldown_until_round"] = self.state["round"] + self.config["cooldown_rounds"]
                logger.warning("%s entered cooldown after %d consecutive failures",
                               platform, ps["consecutive_failures"])

        raw_path = result.get("raw_file")
        new_count = 0
        if ok and raw_path and Path(raw_path).exists() and Path(raw_path).stat().st_size > 0:
            filtered = folder / "filtered.jsonl"
            kept, skipped = filter_unseen(self.dedup, raw_path, filtered)
            if kept:
                new_count = ingest_raw(self.dedup, filtered, stamp)
                ps["total_new_records"] += new_count
                self.state["pending_records"] += new_count
                self.state["pending_raw_files"].append(str(filtered))
                logger.info("%s: %d new, %d duplicate skipped", platform, kept, skipped)
            else:
                logger.info("%s: all %d records already seen", platform, skipped)
            ingest_raw(self.dedup, raw_path, stamp)
        self._save()
        return {"platform": platform, "status": result["status"],
                "new_records": new_count, "total_records": result.get("records", 0)}

    # ---- analysis ----------------------------------------------------------

    def should_analyze(self):
        if self.state["pending_records"] >= self.config["analysis_threshold"]:
            return True
        rounds_since = self.state["round"] - self.state["last_analysis_round"]
        return self.state["pending_records"] > 0 and rounds_since >= self.config["analysis_interval_rounds"]

    def run_analysis(self):
        pending = self.state["pending_raw_files"]
        if not pending:
            return None

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        merged_dir = self.base / "analysis" / stamp
        merged_dir.mkdir(parents=True, exist_ok=True)
        merged_raw = merged_dir / "raw.jsonl"

        missing = []
        with merged_raw.open("w", encoding="utf-8") as out:
            for raw_path in pending:
                p = Path(raw_path)
                if not p.exists() or p.stat().st_size == 0:
                    missing.append(raw_path)
                    continue
                content = p.read_text(encoding="utf-8")
                out.write(content)
                if not content.endswith("\n"):
                    out.write("\n")
        if missing:
            logger.warning("missing or empty pending files: %s", missing)

        if merged_raw.stat().st_size == 0:
            self.state["pending_raw_files"] = []
            self.state["pending_records"] = 0
            self._save()
            return None

        snapshot = active_snapshot(self.root)
        policy_path = snapshot["policy_path"]
        task = snapshot["bundle"].get("task")
        if task:
            mode = task["execution"].get("mode", "ark")
            max_calls = task["execution"].get("max_api_calls", 500)
        else:
            mode = "ark"
            max_calls = 500

        from .workflow import run as workflow_run
        from .db_access import resolve_db
        result = workflow_run(
            merged_raw, policy_path, resolve_db(),
            self.root / "output/scheduler",
            mode, self.root / "config/connection.json", max_calls,
            task=task, config_revision=snapshot["revision"],
        )

        status = result.get("status", "")
        if status in ("completed", ""):
            self.state["pending_raw_files"] = []
            self.state["pending_records"] = 0
        else:
            logger.warning("analysis returned status=%s, keeping pending queue for retry", status)

        self.state["last_analysis_round"] = self.state["round"]
        self._save()
        logger.info("analysis done: status=%s, %d candidates, %d API calls",
                     status, result.get("candidate_contents", 0), result.get("api_calls", 0))
        return result

    # ---- main loop ---------------------------------------------------------

    def tick(self):
        self.state["round"] += 1
        logger.info("=== round %d ===", self.state["round"])

        if _any_task_running(self.root):
            logger.info("manual task running, skipping this round")
            self._save()
            return {"round": self.state["round"], "skipped": "manual_task_running"}

        platform = self.next_platform()
        collection = None
        analysis = None

        if platform:
            try:
                collection = self.run_collection(platform)
            except Exception:
                logger.exception("collection failed for %s", platform)
                ps = self._ps(platform)
                ps["last_run_at"] = _utcnow()
                ps["consecutive_failures"] += 1
                if ps["consecutive_failures"] >= self.config["max_consecutive_failures"]:
                    ps["cooldown_until_round"] = self.state["round"] + self.config["cooldown_rounds"]
                    logger.warning("%s entered cooldown after %d consecutive failures",
                                   platform, ps["consecutive_failures"])
                self._save()
        else:
            logger.info("no platform available this round")

        if self.should_analyze():
            try:
                analysis = self.run_analysis()
            except Exception as e:
                logger.error("analysis failed: %s", e)

        self._save()
        return {"round": self.state["round"], "platform": platform,
                "collection": collection, "analysis": analysis,
                "pending": self.state["pending_records"],
                "dedup": dedup_stats(self.dedup)}

    def close(self):
        try:
            self.dedup.close()
        except Exception:
            pass
        self._save()

    def run(self, interval=None):
        interval = interval or self.config["interval_seconds"]

        from .runtime_context import current_context, set_context
        set_context(current_context())

        logger.info("scheduler starting (interval=%ds, analysis_threshold=%d)",
                     interval, self.config["analysis_threshold"])
        logger.info("dedup index: %s", dedup_stats(self.dedup))

        try:
            while True:
                try:
                    result = self.tick()
                    logger.info("round %d done: pending=%d",
                                result["round"], result.get("pending", self.state["pending_records"]))
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logger.exception("tick failed")
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("scheduler stopped")
        finally:
            self.close()


def main():
    parser = argparse.ArgumentParser(description="Persistent collection/analysis scheduler")
    parser.add_argument("--interval", type=int, default=900, help="seconds between rounds (default 900)")
    parser.add_argument("--analysis-threshold", type=int, default=50, help="new records before analysis (default 50)")
    parser.add_argument("--batch-max-posts", type=int, default=30, help="max posts per collection batch")
    parser.add_argument("--status", action="store_true", help="show current state and exit")
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
            print("scheduler has never run")
            return 0
        state = json.loads(state_path.read_text(encoding="utf-8"))
        dedup_path = root / "data/dedup.sqlite3"
        ds = {}
        if dedup_path.exists():
            with closing(open_index(dedup_path)) as db:
                ds = dedup_stats(db)
        print(json.dumps({"state": state, "dedup": ds}, ensure_ascii=False, indent=2))
        return 0

    config = {
        "interval_seconds": args.interval,
        "analysis_threshold": args.analysis_threshold,
        "batch_max_posts": args.batch_max_posts,
    }
    scheduler = Scheduler(root, config)
    scheduler.run(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
