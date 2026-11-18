"""汇总系统各组件运行状态，供 CLI 和未来 API 使用。"""
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

logger = logging.getLogger("status_report")


def system_status(root, context=None, budget_config=None):
    """返回系统运行状态的完整摘要。"""
    root = Path(root).resolve()
    result = {}

    # 调度器状态
    state_path = root / "data/scheduler/state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            platforms = state.get("platforms", {})
            result["scheduler"] = {
                "round": state.get("round", 0),
                "updated_at": state.get("updated_at"),
                "platforms": {
                    name: {
                        "last_run_at": ps.get("last_run_at"),
                        "consecutive_failures": ps.get("consecutive_failures", 0),
                        "total_runs": ps.get("total_runs", 0),
                        "total_new_records": ps.get("total_new_records", 0),
                        "healthy": ps.get("consecutive_failures", 0) == 0,
                    }
                    for name, ps in platforms.items()
                },
            }
        except (json.JSONDecodeError, OSError):
            result["scheduler"] = {"error": "状态文件损坏"}
    else:
        result["scheduler"] = {"status": "未启动"}

    # 队列状态
    queue_path = root / "data/content_queue.sqlite3"
    if queue_path.exists():
        try:
            from .content_queue import open_queue, stats as queue_stats
            with closing(open_queue(queue_path)) as db:
                result["queue"] = queue_stats(db)
        except Exception as e:
            result["queue"] = {"error": str(e)}

    # 去重索引
    dedup_path = root / "data/dedup.sqlite3"
    if dedup_path.exists():
        try:
            from .dedup import open_index, stats as dedup_stats
            with closing(open_index(dedup_path)) as db:
                result["dedup"] = dedup_stats(db)
        except Exception as e:
            result["dedup"] = {"error": str(e)}

    # 预算状态
    try:
        from .budget_controller import BudgetController
        budget = BudgetController(root, budget_config)
        result["budget"] = budget.status()
    except Exception as e:
        result["budget"] = {"error": str(e)}

    # 候选账号池
    if context:
        try:
            from .candidate_pool import CandidatePool
            pool = CandidatePool(context)
            try:
                result["candidates"] = pool.stats()
            finally:
                pool.close()
        except Exception as e:
            result["candidates"] = {"error": str(e)}

        # 反馈统计独立于候选池报告。
        try:
            from .feedback_loop import feedback_stats
            result["feedback"] = feedback_stats(context)
        except Exception as e:
            result["feedback"] = {"error": str(e)}

        # 发现仪表盘
        try:
            from .discovery_dashboard import dashboard
            result["discovery_dashboard"] = dashboard(context, min_score=50)
        except Exception as e:
            result["discovery_dashboard"] = {"error": str(e)}

        try:
            from .social_signal import signal_summary
            result["social_signals"] = signal_summary(context)
        except Exception as e:
            result["social_signals"] = {"error": str(e)}

    # 数据库分析统计
    if context:
        try:
            from .db_access import connect
            with closing(connect(context=context, readonly=True)) as db:
                try:
                    result["analysis"] = _analysis_stats(db)
                except Exception as e:
                    result["analysis"] = {"error": str(e)}
                try:
                    result["profiles"] = _profile_stats(db)
                except Exception as e:
                    result["profiles"] = {"error": str(e)}
        except Exception as e:
            result["analysis"] = {"error": str(e)}
            result["profiles"] = {"error": str(e)}

    return result


def _analysis_stats(db):
    """从主数据库提取分析统计。"""
    jobs = db.execute(
        "SELECT count(*) as total, "
        "sum(CASE WHEN status IN ('completed','completed_with_review') THEN 1 ELSE 0 END) as completed "
        "FROM v2_jobs"
    ).fetchone()
    sources = db.execute("SELECT count(*) FROM v2_sources").fetchone()[0]
    analyses = db.execute("SELECT count(*) FROM v2_analyses").fetchone()[0]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_calls = db.execute(
        "SELECT count(*) FROM v2_attempts WHERE api_called=1 AND created_at >= ?",
        (today,),
    ).fetchone()[0]
    today_tokens = 0
    for row in db.execute(
        "SELECT usage_json FROM v2_attempts WHERE api_called=1 AND created_at >= ?",
        (today,),
    ):
        try:
            usage = json.loads(row[0]) if row[0] else {}
            today_tokens += usage.get("total_tokens", 0)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "jobs": {"total": jobs["total"], "completed": jobs["completed"]},
        "sources": sources,
        "analyses": analyses,
        "today": {
            "api_calls": today_calls,
            "tokens": today_tokens,
        },
    }


def _profile_stats(db):
    """兴趣画像统计。"""
    accounts = db.execute(
        "SELECT count(*) FROM accounts WHERE identity_mode='public_source'"
    ).fetchone()[0]
    profiles = db.execute("SELECT count(*) FROM interest_profiles").fetchone()[0]
    topics = db.execute(
        "SELECT count(DISTINCT topic) FROM interest_profiles"
    ).fetchone()[0]
    evidence = db.execute(
        "SELECT count(*) FROM interest_evidence"
    ).fetchone()[0]
    return {
        "accounts": accounts,
        "profiles": profiles,
        "topics": topics,
        "evidence_items": evidence,
    }
