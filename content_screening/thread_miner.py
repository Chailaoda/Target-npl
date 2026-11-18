"""深度评论区挖掘器：从高价值帖子的评论中发现候选账号。"""
from contextlib import closing
import json
import logging

from .db_access import connect_readonly
from .candidate_pool import CandidatePool

logger = logging.getLogger("thread_miner")

_DEFAULT_MIN_SCORE = 30


def high_value_posts(context, min_score=_DEFAULT_MIN_SCORE, limit=100):
    """找到关联高兴趣分账号的帖子 source_hash 列表。"""
    with closing(connect_readonly(context=context)) as db:
        return [dict(r) for r in db.execute("""
            SELECT DISTINCT l.source_hash, l.account_key, l.platform, p.score
            FROM interest_profiles p
            JOIN account_content_links l ON l.account_key = p.account_key
            JOIN v2_sources s ON s.source_hash = l.source_hash
            WHERE p.score >= ?
              AND l.object_type = 'post'
            ORDER BY p.score DESC
            LIMIT ?
        """, (min_score, limit)).fetchall()]


def extract_commenters(context, post_source_hashes):
    """从已采集评论中提取互动者账号。

    策略：在 v2_sources 的 source_json 中查找 parent_id 或 root_id 匹配
    高价值帖子 content_id 的评论记录，取其关联账号。
    """
    if not post_source_hashes:
        return []
    with closing(connect_readonly(context=context)) as db:
        post_content_ids = set()
        post_hash_to_owner = {}
        for ph in post_source_hashes:
            row = db.execute(
                "SELECT source_json FROM v2_sources WHERE source_hash=?",
                (ph,)).fetchone()
            if row:
                try:
                    src = json.loads(row[0])
                    cid = src.get("content_id", "")
                    if cid:
                        post_content_ids.add(cid)
                        post_hash_to_owner[cid] = ph
                except (json.JSONDecodeError, TypeError):
                    pass
        if not post_content_ids:
            return []

        results = []
        seen = set()
        for row in db.execute(
            "SELECT s.source_hash, s.source_json, l.account_key, l.platform, a.account_id "
            "FROM v2_sources s "
            "JOIN account_content_links l ON l.source_hash = s.source_hash "
            "JOIN accounts a ON a.account_key = l.account_key "
            "WHERE l.object_type = 'comment' "
            "AND a.identity_mode = 'public_source'"
        ):
            try:
                src = json.loads(row["source_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            parent = src.get("parent_id", "") or src.get("root_id", "")
            if parent in post_content_ids and row["account_key"] not in seen:
                seen.add(row["account_key"])
                results.append({
                    "platform": row["platform"],
                    "account_id": row["account_id"],
                    "account_key": row["account_key"],
                    "source_content_hash": row["source_hash"],
                    "parent_content_id": parent,
                })
    return results


def mine_and_register(context, min_score=_DEFAULT_MIN_SCORE,
                      confidence=0.3, limit=100):
    """主入口：找高价值帖子 → 提取评论者 → 注册为候选。"""
    posts = high_value_posts(context, min_score=min_score, limit=limit)
    if not posts:
        logger.info("未找到评分 >= %d 的高价值帖子", min_score)
        return {"added": 0, "updated": 0, "duplicates": 0}, 0, 0

    post_hashes = [p["source_hash"] for p in posts]
    commenters = extract_commenters(context, post_hashes)
    if not commenters:
        logger.info("扫描 %d 个帖子，未发现新评论者", len(posts))
        return {"added": 0, "updated": 0, "duplicates": 0}, len(posts), 0

    post_hash_owners = {p["source_hash"]: p["account_key"] for p in posts}
    candidates = []
    for c in commenters:
        candidates.append({
            "platform": c["platform"],
            "account_id": c["account_id"],
            "discovery_path": "deep_thread",
            "confidence": confidence,
            "source_account_key": post_hash_owners.get(
                c["source_content_hash"], ""),
            "source_content_hash": c["source_content_hash"],
        })

    pool = CandidatePool(context)
    try:
        result = pool.add_batch(candidates)
    finally:
        pool.close()

    logger.info("深度挖掘：扫描 %d 帖 → %d 评论者 → 新增 %d 候选",
                len(posts), len(commenters), result["added"])
    return result, len(posts), len(commenters)
