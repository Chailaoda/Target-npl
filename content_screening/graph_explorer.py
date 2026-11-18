"""从已采集的评论中发现与高兴趣账号参与同一帖子的账号。"""
from contextlib import closing
import json
import logging

from .db_access import connect_readonly
from .candidate_pool import CandidatePool

logger = logging.getLogger("graph_explorer")

_BATCH_SIZE = 200


def seed_accounts(context, min_score=50, limit=200):
    """按各账号的最高兴趣分选取种子账号。"""
    with closing(connect_readonly(context=context)) as db:
        return [row["account_key"] for row in db.execute("""
            SELECT account_key, MAX(score) AS top_score
            FROM interest_profiles
            WHERE score >= ?
            GROUP BY account_key
            ORDER BY top_score DESC, account_key
            LIMIT ?
        """, (min_score, limit))]


def _thread_key(row):
    """优先取根帖 ID，并用平台区分相同的帖子 ID。"""
    try:
        source = json.loads(row["source_json"])
    except (ValueError, TypeError):
        return None
    if isinstance(source, dict):
        for field in ("root_id", "parent_id"):
            value = source.get(field)
            if type(value) in (str, int):
                thread_id = str(value).strip()
                if thread_id and thread_id != "0":
                    return row["platform"], thread_id
    return None


def _build_thread_index(db, seed_account_keys):
    """分批查询种子评论，按帖子线程归集参与的种子。"""
    threads = {}
    seeds = list(seed_account_keys)
    for offset in range(0, len(seeds), _BATCH_SIZE):
        batch = seeds[offset:offset + _BATCH_SIZE]
        placeholders = ", ".join("?" for _ in batch)
        for row in db.execute("""
            SELECT l.account_key, l.platform, s.source_json
            FROM account_content_links l
            JOIN v2_sources s ON s.source_hash = l.source_hash
            WHERE l.object_type = 'comment' AND l.account_key IN ({})
        """.format(placeholders), batch):
            thread = _thread_key(row)
            if thread is not None:
                threads.setdefault(thread, set()).add(row["account_key"])
    return threads


def _find_co_commenters(db, thread_index, seed_account_keys):
    """按账号统计共同线程，每个线程只计一次。"""
    if not thread_index:
        return []
    seeds = set(seed_account_keys)
    commenters = {}
    for row in db.execute("""
        SELECT l.account_key, l.platform, a.account_id, s.source_json
        FROM account_content_links l
        JOIN v2_sources s ON s.source_hash = l.source_hash
        JOIN accounts a ON a.account_key = l.account_key
        WHERE l.object_type = 'comment' AND a.identity_mode = 'public_source'
        ORDER BY l.account_key, l.platform, l.content_id
    """):
        account_key = row["account_key"]
        if account_key in seeds:
            continue
        thread = _thread_key(row)
        if thread not in thread_index:
            continue
        if account_key not in commenters:
            commenters[account_key] = ({
                "platform": row["platform"],
                "account_id": row["account_id"],
                "account_key": account_key,
            }, {})
        commenters[account_key][1][thread] = thread_index[thread]

    return [{
        "platform": info["platform"],
        "account_id": info["account_id"],
        "account_key": info["account_key"],
        "shared_threads": len(threads),
        "seed_account_keys": list(dict.fromkeys(
            seed for thread_seeds in threads.values() for seed in sorted(thread_seeds)
        )),
    } for info, threads in commenters.values()]


def explore_and_register(context, min_score=50, confidence_base=0.25, limit=200,
                         *, extra_seed_account_keys=()):
    """合并两类种子并将共同评论者注册为图谱候选。"""
    seeds = seed_accounts(context, min_score=min_score, limit=limit)
    seeds = list(dict.fromkeys([*seeds, *extra_seed_account_keys]))
    if not seeds:
        return {"added": 0, "updated": 0, "duplicates": 0}, 0, 0

    with closing(connect_readonly(context=context)) as db:
        threads = _build_thread_index(db, seeds)
        commenters = _find_co_commenters(db, threads, seeds)

    if not commenters:
        return {"added": 0, "updated": 0, "duplicates": 0}, len(seeds), 0

    candidates = [{
        "platform": commenter["platform"],
        "account_id": commenter["account_id"],
        "discovery_path": "social_graph",
        "confidence": confidence_base + 0.1 * min(commenter["shared_threads"] - 1, 5),
        "source_account_key": commenter["seed_account_keys"][0],
    } for commenter in commenters]

    pool = CandidatePool(context)
    try:
        reviewed = {tuple(row) for row in pool.db.execute("""
            SELECT platform, account_id
            FROM discovery_candidates
            WHERE status IN ('confirmed', 'rejected')
        """)}
        result = pool.add_batch([
            c for c in candidates
            if (c["platform"], c["account_id"]) not in reviewed
        ])
    finally:
        pool.close()
    logger.info("图谱探索：%d 个种子，%d 个共同评论者，新增 %d 个候选",
                len(seeds), len(commenters), result["added"])
    return result, len(seeds), len(commenters)
