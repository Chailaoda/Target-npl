"""汇总发现候选、关键词产出及社交信号覆盖情况。"""
from contextlib import closing
import json
import logging

from .candidate_pool import _DISCOVERY_PATHS
from .db_access import connect_readonly
from .social_signal import _confirmed_account_keys, _cross_path_boosts, _has_candidate_table, _neighbor_scores

logger = logging.getLogger("discovery_dashboard")


def _discovery_funnel(pool_db):
    """按已记录的路径统计各阶段人数，用已判定样本计算确认率。"""
    funnel = {}
    for row in pool_db.execute("""
        SELECT discovery_path, status, COUNT(*) AS count FROM discovery_candidates
        GROUP BY discovery_path, status ORDER BY discovery_path
    """):
        counts = funnel.setdefault(row["discovery_path"], {
            "new": 0, "analyzed": 0, "confirmed": 0, "rejected": 0, "total": 0,
        })
        counts["total"] += row["count"]
        if row["status"] in ("new", "analyzed", "confirmed", "rejected"):
            counts[row["status"]] = row["count"]
    for counts in funnel.values():
        decided = counts["confirmed"] + counts["rejected"]
        counts["confirmation_rate"] = counts["confirmed"] / decided if decided else None
    return funnel


def _path_detail_stats(pool_db):
    """汇总每条路径的合并置信度、优先级及最后更新时间。"""
    return {
        row["discovery_path"]: {
            "avg_confidence": row["avg_confidence"],
            "avg_priority": row["avg_priority"],
            "max_confidence": row["max_confidence"],
            "latest_at": row["latest_at"],
        }
        for row in pool_db.execute("""
            SELECT discovery_path, AVG(merged_confidence) AS avg_confidence,
                   AVG(priority) AS avg_priority, MAX(merged_confidence) AS max_confidence,
                   MAX(updated_at) AS latest_at
            FROM discovery_candidates GROUP BY discovery_path ORDER BY discovery_path
        """)
    }


def _keyword_productivity(db, min_score=50):
    """返回关联达标确认账号最多的 20 个关键词及对应观测数。"""
    return [dict(row) for row in db.execute("""
        WITH eligible_links AS (
            SELECT DISTINCT c.account_key, l.source_hash
            FROM discovery_candidates c
            JOIN account_content_links l ON l.account_key = c.account_key
            WHERE c.status = 'confirmed' AND EXISTS (
                SELECT 1 FROM interest_profiles p WHERE p.account_key = c.account_key AND p.score >= ?
            )
        ), keyword_sources AS (
            SELECT keyword, source_hash, COUNT(*) AS observations
            FROM v2_observations
            WHERE TRIM(keyword) <> '' AND source_hash IN (SELECT source_hash FROM eligible_links)
            GROUP BY keyword, source_hash
        ), keyword_accounts AS (
            SELECT k.keyword, COUNT(DISTINCT l.account_key) AS confirmed_accounts
            FROM keyword_sources k JOIN eligible_links l ON l.source_hash = k.source_hash
            GROUP BY k.keyword
        ), observation_counts AS (
            SELECT keyword, SUM(observations) AS total_observations
            FROM keyword_sources GROUP BY keyword
        )
        SELECT a.keyword, a.confirmed_accounts, o.total_observations
        FROM keyword_accounts a JOIN observation_counts o ON o.keyword = a.keyword
        ORDER BY a.confirmed_accounts DESC, o.total_observations DESC, a.keyword
        LIMIT 20
    """, (min_score,))]


def _multi_path_summary(pool_db):
    """用发现次数统计重复发现，示例只列有记录的路径。"""
    summary = dict(pool_db.execute("""
        SELECT COUNT(*) AS count, COALESCE(AVG(discovery_count), 0.0) AS avg_discovery_count
        FROM discovery_candidates WHERE discovery_count > 1
    """).fetchone())
    examples = []
    for row in pool_db.execute("""
        SELECT platform, account_id, discovery_path, metadata_json, merged_confidence
        FROM discovery_candidates WHERE discovery_count > 1
        ORDER BY discovery_count DESC, merged_confidence DESC, id LIMIT 10
    """):
        paths = [row["discovery_path"]]
        try:
            metadata = json.loads(row["metadata_json"])
        except (ValueError, TypeError):
            metadata = None
        if isinstance(metadata, dict):
            for field in ("paths", "discovery_paths"):
                recorded_paths = metadata.get(field)
                if isinstance(recorded_paths, list):
                    paths.extend(path for path in recorded_paths
                                 if isinstance(path, str) and path in _DISCOVERY_PATHS)
        examples.append({
            "platform": row["platform"], "account_id": row["account_id"],
            "paths": list(dict.fromkeys(paths)), "confidence": row["merged_confidence"],
        })
    summary["top_examples"] = examples
    return summary


def dashboard(context, min_score=50):
    """在同一只读快照中汇总发现数据和新候选的社交信号覆盖率。"""
    with closing(connect_readonly(context=context)) as db:
        db.execute("BEGIN")
        if not _has_candidate_table(db):
            return {
                "funnel": {}, "path_stats": {}, "keyword_productivity": [],
                "multi_path_accounts": {"count": 0, "avg_discovery_count": 0.0, "top_examples": []},
                "signal_coverage": 0.0,
            }
        funnel = _discovery_funnel(db)
        new_count = sum(counts["new"] for counts in funnel.values())
        coverage = 0.0
        if new_count:
            neighbors = _neighbor_scores(db, _confirmed_account_keys(db))
            boosts = _cross_path_boosts(db)
            coverage = len(neighbors.keys() | boosts.keys()) / new_count
        return {
            "funnel": funnel,
            "path_stats": _path_detail_stats(db),
            "keyword_productivity": _keyword_productivity(db, min_score),
            "multi_path_accounts": _multi_path_summary(db),
            "signal_coverage": coverage,
        }
