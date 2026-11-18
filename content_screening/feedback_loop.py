"""统计候选发现路径的有效性，并将已确认账号反馈为种子。"""
import logging

from .candidate_pool import CandidatePool, _DISCOVERY_PATHS

logger = logging.getLogger("feedback_loop")

_MIN_SAMPLES = 5


def path_effectiveness(context):
    """按发现路径统计候选状态，仅用已判定样本计算确认率。"""
    pool = CandidatePool(context)
    try:
        result = {}
        for row in pool.db.execute("""
            SELECT discovery_path, COUNT(*) AS total,
                   SUM(status = 'confirmed') AS confirmed,
                   SUM(status = 'rejected') AS rejected,
                   SUM(status = 'analyzed') AS analyzed
            FROM discovery_candidates
            GROUP BY discovery_path
            ORDER BY discovery_path
        """):
            stats = dict(row)
            path = stats.pop("discovery_path")
            decided = stats["confirmed"] + stats["rejected"]
            stats["confirmation_rate"] = (
                stats["confirmed"] / decided if decided >= _MIN_SAMPLES else None
            )
            result[path] = stats
        return result
    finally:
        pool.close()


def _multipliers(effectiveness):
    result = dict.fromkeys(sorted(_DISCOVERY_PATHS), 1.0)
    for path, stats in effectiveness.items():
        rate = stats["confirmation_rate"]
        if rate is None:
            result[path] = 1.0
        elif rate >= 0.5:
            result[path] = 1.0 + (rate - 0.5)
        else:
            result[path] = max(0.5, rate * 2)
    return result


def confidence_multipliers(context):
    """返回各发现路径的乘数，缺少数据时使用默认值。"""
    return _multipliers(path_effectiveness(context))


def confirmed_seed_accounts(context, min_score=50):
    """查询已确认且具有达标兴趣画像的账号。"""
    pool = CandidatePool(context)
    try:
        return [row["account_key"] for row in pool.db.execute("""
            SELECT DISTINCT c.account_key
            FROM discovery_candidates c
            WHERE c.status = 'confirmed' AND EXISTS (
                SELECT 1
                FROM interest_profiles p
                WHERE p.account_key = c.account_key AND p.score >= ?
            )
            ORDER BY c.account_key
        """, (min_score,))]
    finally:
        pool.close()


def feed_confirmed_seeds(context, min_score=50):
    """将已确认的高分账号作为额外种子，并返回新增候选数。"""
    seeds = confirmed_seed_accounts(context, min_score=min_score)
    if not seeds:
        return 0

    from .graph_explorer import explore_and_register

    result, _, _ = explore_and_register(
        context, min_score=min_score, extra_seed_account_keys=seeds
    )
    logger.info("反馈循环：纳入 %d 个已确认种子，新增 %d 个候选",
                len(seeds), result["added"])
    return result["added"]


def feedback_stats(context):
    """返回路径有效性、置信度乘数和达标的已确认种子数。"""
    effectiveness = path_effectiveness(context)
    return {
        "path_effectiveness": effectiveness,
        "multipliers": _multipliers(effectiveness),
        "confirmed_seeds": len(confirmed_seed_accounts(context)),
    }
