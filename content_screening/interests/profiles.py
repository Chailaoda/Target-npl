"""Account-level read models: one account, or one account's evidence.

Split out of the use-case service so the HTTP layer can render a profile page
without dragging in analysis orchestration.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json

from ..interests import repository as repo
from ..interests.scoring import WINDOWS, metrics, normalize_topic, published


def account(service, account_key):
    with closing(service._connect(readonly=True)) as db:
        row = repo.account_row(db, account_key)
        if not row:
            return None
        from ..account_repository import account_overview, attribution_conflicts
        overview = account_overview(db, account_key)
        overview.pop("history", None)
        latest = {}
        for profile in repo.account_profile_rows(db, account_key):
            if repo.active_version(db, profile["topic"]) != profile["profile_version"]:
                continue
            latest.setdefault(profile["topic"], profile)
        now = datetime.now(timezone.utc)
        interests = []
        for profile in latest.values():
            evidence = repo.load_evidence(db, account_key, profile["topic"], profile["profile_version"])
            windows = {str(w): metrics(evidence, now, w) for w in WINDOWS}
            windows["all"] = metrics(evidence, now)
            details = json.loads(profile["details_json"])
            interests.append({"topic": profile["topic"], **windows["all"], "windows": windows,
                              "profile_version": profile["profile_version"], "updated_at": profile["updated_at"],
                              "synthetic": details.get("synthetic", False)})
        interests.sort(key=lambda p: (p["coverage"] == "high", p["interest_score"], p["evidence_count"], p["topic"]), reverse=True)
        return {**overview, "interests": interests,
                "attribution_conflicts": attribution_conflicts(db, account_key=account_key),
                "scope_note": "仅基于已采集且有可靠公开账号 ID 的内容；未观测不等于没有兴趣。"}


def evidence(service, account_key, topic, window="all"):
    if window not in ("all", "30", "90", "180", 30, 90, 180):
        raise ValueError("时间窗口仅支持 30/90/180/all")
    window = str(window)
    key = normalize_topic(topic)
    with closing(service._connect(readonly=True)) as db:
        version = repo.active_version(db, key)
        items = repo.load_evidence(db, account_key, key, version) if version else []
    now = datetime.now(timezone.utc)
    current = metrics(items, now, window)
    visible = []
    for item in items:
        when = published(item.get("published_at"))
        if window != "all" and (when is None or when < now - timedelta(days=int(window))):
            continue
        visible.append(item)
    visible.sort(key=lambda e: (e.get("review_state", "accepted") == "accepted",
                               e.get("contribution", 0), e["raw_score"], e["source_hash"]), reverse=True)
    return {"account_key": account_key, "topic": key, "profile_version": version or "",
            **current, "items": visible,
            "score_note": "兴趣参与指数不是概率或正向好感；它是窗口内独立证据贡献的确定性映射。半衰期用于趋势对比，窗口决定计入哪些证据；态度与证据充分程度单独展示。"}
