"""从已确认邻居和重复发现记录计算候选账号的优先级。"""
from contextlib import closing
import json
import logging

from .candidate_pool import CandidatePool
from .db_access import connect_readonly
from .graph_explorer import _build_thread_index, _thread_key

logger = logging.getLogger("social_signal")

_BATCH_SIZE = 200


def _has_candidate_table(db):
    """判断候选池是否已完成初始化。"""
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'discovery_candidates'"
    ).fetchone() is not None


def _confirmed_account_keys(pool_db):
    """返回所有已确认候选的账号键。"""
    return {row["account_key"] for row in pool_db.execute(
        "SELECT account_key FROM discovery_candidates WHERE status = 'confirmed'"
    )}


def _neighbor_scores(pool_db, confirmed_keys):
    """归集新候选的确认邻居，按账号去重后归一化。"""
    if not confirmed_keys:
        return {}

    # 图谱探索器已按每批 200 个账号查询评论，并用平台和根帖 ID 归集。
    threads = _build_thread_index(pool_db, confirmed_keys)
    neighbors = {}
    cursor = pool_db.execute("""
        SELECT id, account_key, platform, discovery_path, source_account_key, metadata_json
        FROM discovery_candidates WHERE status = 'new'
    """)
    while batch := cursor.fetchmany(_BATCH_SIZE):
        by_account = {}
        for row in batch:
            account_key = row["account_key"]
            related = set()
            source_key = row["source_account_key"]
            if row["discovery_path"] == "social_graph" and source_key in confirmed_keys:
                related.add(source_key)
            try:
                metadata = json.loads(row["metadata_json"])
            except (ValueError, TypeError):
                metadata = None
            if isinstance(metadata, dict):
                seeds = metadata.get("seed_account_keys")
                if isinstance(seeds, list):
                    related.update(key for key in seeds if isinstance(key, str) and key in confirmed_keys)
                thread = _thread_key({"platform": row["platform"], "source_json": row["metadata_json"]})
                related.update(threads.get(thread, ()))
            by_account[account_key] = row["id"], related

        if threads:
            keys = tuple(by_account)
            placeholders = ", ".join("?" for _ in keys)
            for row in pool_db.execute("""
                SELECT l.account_key, l.platform, s.source_json
                FROM account_content_links l
                JOIN v2_sources s ON s.source_hash = l.source_hash
                WHERE l.object_type = 'comment' AND l.account_key IN ({})
            """.format(placeholders), keys):
                by_account[row["account_key"]][1].update(threads.get(_thread_key(row), ()))

        for account_key, (candidate_id, related) in by_account.items():
            related.discard(account_key)
            if related:
                neighbors[candidate_id] = min(len(related) / 5, 1.0)
    return neighbors


def _cross_path_boosts(pool_db):
    """按新候选的发现次数计算加分。"""
    return {
        row["id"]: min(0.1 * (row["discovery_count"] - 1), 0.3)
        for row in pool_db.execute("""
            SELECT id, discovery_count FROM discovery_candidates
            WHERE status = 'new' AND discovery_count > 1
        """)
    }


def enhance_priorities(context):
    """按综合信号提高新候选的优先级，返回实际更新数。"""
    pool = CandidatePool(context)
    try:
        with pool.db:
            pool.db.execute("BEGIN IMMEDIATE")
            neighbors = _neighbor_scores(pool.db, _confirmed_account_keys(pool.db))
            boosts = _cross_path_boosts(pool.db)
            updates = []
            for row in pool.db.execute("""
                SELECT id, merged_confidence, priority FROM discovery_candidates WHERE status = 'new'
            """):
                candidate_id = row["id"]
                composite = row["merged_confidence"] + neighbors.get(candidate_id, 0) * 0.2
                composite += boosts.get(candidate_id, 0)
                priority = round(composite * 100)
                if priority > row["priority"]:
                    updates.append((priority, candidate_id))
            pool.db.executemany("""
                UPDATE discovery_candidates SET priority = ? WHERE id = ?
            """, updates)
            return len(updates)
    finally:
        pool.close()


def signal_summary(context):
    """汇总已确认账号及两类信号，分别对具有对应信号的新候选取平均。"""
    with closing(connect_readonly(context=context)) as db:
        db.execute("BEGIN")
        confirmed = set()
        neighbors = {}
        boosts = {}
        if _has_candidate_table(db):
            confirmed = _confirmed_account_keys(db)
            neighbors = _neighbor_scores(db, confirmed)
            boosts = _cross_path_boosts(db)
        return {
            "confirmed_count": len(confirmed),
            "new_with_neighbors": len(neighbors),
            "multi_path_count": len(boosts),
            "avg_neighbor_score": sum(neighbors.values()) / len(neighbors) if neighbors else 0.0,
            "avg_cross_path_boost": sum(boosts.values()) / len(boosts) if boosts else 0.0,
        }
