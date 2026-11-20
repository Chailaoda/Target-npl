"""Pure scoring and evidence construction for interest profiles.

No database, no filesystem, no runtime context: this module turns one record
plus one topic definition into literal evidence, and turns evidence into the
reported metrics. That keeps the scoring contract unit-testable in
milliseconds and impossible to accidentally "fix" by touching storage.

Scoring contract (keep UI wording aligned):
  * interest_score is a deterministic saturation mapping of the independent,
    quality-weighted evidence contributions inside the selected window. It is
    NOT a probability, a personality judgement, or positive preference.
  * stance, evidence_count and coverage are reported separately.
  * The half-life is used for recent/previous participation comparison (trend)
    and the window decides which evidence is counted; the cumulative score is
    not additionally decayed by age.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import re

ALGORITHM_VERSION = "interest-v2"
WINDOWS = (30, 90, 180)
BEHAVIOR_WEIGHTS = {"post": 4.0, "comment": 3.0, "reply": 2.0, "repost": 2.0}
HALF_LIFE_DAYS = 180
POSITIVE = re.compile(r"喜欢|热爱|推荐|好用|好玩|享受|种草|坚持|入坑|上瘾|不错|支持|爱用|期待|感兴趣")
NEGATIVE = re.compile(r"讨厌|反感|批评|反对|踩坑|避雷|不推荐|无聊|浪费|差评|质疑|抵制|失望|劝退")
PRACTICE = re.compile(r"我用|我的|实践|经验|教程|对比|参数|路线|装备|实测|拍摄|训练|保养|入手|玩了|去过|做过|每次|通常")
NEGATION = re.compile(r"(?:不|没|没有|并非|不是)(?:太|很|真|特别)?(?:喜欢|推荐|感兴趣|支持|好用|好玩)")
TURN = re.compile(r"但|不过|然而|却|只是|可惜|反而")
UNKNOWN = "时间未知"
STANCES = ("positive", "negative", "neutral", "mixed", "unclear")


def fingerprint(value):
    """Same canonical hash as pipeline.fingerprint, kept local to stay I/O-free."""
    import hashlib
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def normalize_topic(value):
    if not isinstance(value, str):
        raise ValueError("兴趣主题必须是字符串")
    return re.sub(r"\s+", " ", value).strip().casefold()


def define_topic(topic, aliases=None, exclude_terms=None, description=""):
    topic_key = normalize_topic(topic)
    if not 1 <= len(topic_key) <= 120:
        raise ValueError("兴趣主题长度应为 1–120 字符")
    def words(values, name):
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > 30:
            raise ValueError(f"{name} 必须是不超过 30 项的列表")
        result = sorted({normalize_topic(v) for v in values if normalize_topic(v)})
        if any(len(v) > 120 for v in result):
            raise ValueError(f"{name} 单项过长")
        return result
    if not isinstance(description, str) or len(description) > 1000:
        raise ValueError("主题说明长度不能超过 1000 字符")
    aliases = [v for v in words(aliases, "别名") if v != topic_key]
    definition = {"topic": topic_key, "display_topic": re.sub(r"\s+", " ", topic).strip(),
                  "aliases": aliases, "exclude_terms": words(exclude_terms, "排除词"),
                  "description": description}
    definition["definition_hash"] = fingerprint(definition)
    definition["profile_version"] = ALGORITHM_VERSION + ":" + definition["definition_hash"][:16]
    return definition


def _contains(text, term):
    if re.fullmatch(r"[a-zA-Z0-9_ .+-]+", term, re.I | re.ASCII):
        return re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", text, re.I | re.ASCII)
    return term.casefold() in text.casefold()


def _match(text, terms):
    return next((term for term in terms if _contains(text, term)), None)


def published(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _polarity(clause):
    if NEGATIVE.search(clause) or NEGATION.search(clause):
        return "negative"
    if POSITIVE.search(clause):
        return "positive"
    return None


def _stance_in_topic_clause(text, matched):
    """Judge attitude from the clause containing the topic, not the whole text.

    '我喜欢摄影，但讨厌露营' must not make the photography topic negative.
    An adjacent adversative clause is consulted only when the topic clause
    expresses no attitude at all.
    """
    clauses = [c.strip() for c in re.split(r"[，,；;。！？!?\n]+", text) if c.strip()]
    index = next((i for i, clause in enumerate(clauses) if _contains(clause, matched)), None)
    if index is None:
        return "neutral"
    attitude = _polarity(clauses[index])
    if attitude:
        return attitude
    neighbours = []
    if index > 0 and TURN.search(clauses[index]):
        neighbours.append(clauses[index - 1])
    if index + 1 < len(clauses) and TURN.search(clauses[index + 1]):
        neighbours.append(clauses[index + 1])
    for clause in neighbours:
        attitude = _polarity(clause)
        if attitude:
            return attitude
    return "neutral"


def analyze_text(record, definition):
    """Return literal, verbatim evidence or an explicit non-match/uncertain result."""
    text = record.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return {"decision": "uncertain", "quality_flags": ["empty_text_no_ocr"]}
    terms = [definition["topic"]] + definition["aliases"]
    excluded = _match(text, definition["exclude_terms"])
    if excluded:
        return {"decision": "excluded", "quality_flags": ["user_excluded_term"]}
    matched = _match(text, terms)
    if not matched:
        # Context and previous model labels do not establish the author's own
        # interest. Semantic/model analysis may be added behind this interface.
        return {"decision": "not_relevant", "quality_flags": ["no_literal_topic_evidence"]}
    if re.match(r"^\s*(?:转载|转发原文|引用他人)[:：]", text):
        return {"decision": "uncertain", "quality_flags": ["not_confirmed_self_expression"]}
    start = text.casefold().index(matched.casefold())
    quote_start = max(0, text.rfind("\n", 0, start), text.rfind("。", 0, start), text.rfind("！", 0, start))
    quote_end_candidates = [i for i in (text.find("\n", start), text.find("。", start), text.find("！", start)) if i >= 0]
    quote_end = min(quote_end_candidates) + 1 if quote_end_candidates else len(text)
    quote = text[quote_start:quote_end].strip() or matched
    if matched.casefold() not in quote.casefold():
        quote = text[start:start + len(matched)]
    stance = _stance_in_topic_clause(text, matched)
    if len(text.strip()) <= 16 and not PRACTICE.search(text):
        depth = 0.25
    else:
        depth = 0.5 + (0.2 if len(text.strip()) >= 40 else 0.0) + (0.3 if PRACTICE.search(text) else 0.0)
    return {"decision": "accepted", "relevance": 1.0 if matched == definition["topic"] else 0.85,
            "depth": min(depth, 1.0), "stance": stance, "quote": quote,
            "quality_flags": [] if stance != "neutral" else ["stance_not_expressed"],
            "matched_term": matched, "engine": "literal_rules"}


def metrics(evidence, now, window="all"):
    selected = []
    for item in evidence:
        if item.get("review_state", "accepted") != "accepted":
            continue
        when = published(item.get("published_at"))
        if window != "all" and (when is None or when < now - timedelta(days=int(window))):
            continue
        selected.append(item)
    threads = defaultdict(float)
    texts = set()
    independent = set()
    days = set()
    positive = negative = 0.0
    total = 0.0
    dated = []
    recent = previous = 0.0
    for index, original in enumerate(selected):
        item = original
        normalized_text = re.sub(r"\s+", "", item.get("full_text", item["quote"])).casefold()
        if normalized_text in texts:
            continue
        texts.add(normalized_text)
        thread = item.get("root_id") or item.get("parent_id") or item["source_hash"]
        allowed = max(0.0, 6.0 - threads[thread])
        contribution = min(item["raw_score"], allowed)
        threads[thread] += item["raw_score"]
        if contribution <= 0:
            continue
        item["contribution"] = round(contribution, 4)
        selected[index] = item
        total += contribution
        independent.add(item["source_hash"])
        stance = item["stance"]
        positive += contribution if stance == "positive" else 0
        negative += contribution if stance == "negative" else 0
        when = published(item.get("published_at"))
        if when:
            days.add(when.date().isoformat())
            dated.append(when)
            age = max(0.0, (now - when).total_seconds() / 86400)
            decayed = contribution * 2 ** (-age / HALF_LIFE_DAYS)
            if age <= 30:
                recent += decayed
            elif age <= 60:
                previous += decayed
    score = round(100 * (1 - math.exp(-total / 16)))
    count = len(independent)
    coverage = "insufficient" if not count else "low" if count == 1 or len(days) < 2 else "medium" if count < 4 else "high"
    stance = "mixed" if positive and negative else "positive" if positive else "negative" if negative else "neutral" if count else "unclear"
    if len(dated) < 2 or (not recent and not previous):
        trend = "insufficient_data"
    elif recent >= max(2.0, previous * 1.25):
        trend = "rising"
    elif previous >= 2.0 and recent <= previous * 0.5:
        trend = "declining"
    else:
        trend = "stable"
    return {"interest_score": score, "score": score, "stance": stance,
            "evidence_count": count, "independent_content_count": count, "active_days": len(days),
            "coverage": coverage, "trend": trend,
            "last_activity": max(dated).isoformat() if dated else None,
            "recent_participation": round(recent, 4), "previous_participation": round(previous, 4)}


def raw_evidence(link, record, analyzed, definition):
    when = published(record.get("published_at"))
    kind = "reply" if record["object_type"] == "comment" and record.get("parent_id") else record["object_type"]
    weight = BEHAVIOR_WEIGHTS.get(kind, 1.0)
    quality = 0.95 if when else 0.55
    flags = list(analyzed.get("quality_flags", []))
    if not when:
        flags.append("published_time_unknown")
    raw = weight * analyzed["relevance"] * analyzed["depth"] * quality
    return {"account_key": link["account_key"], "source_hash": link["source_hash"],
            "analysis_key": link["source_hash"],
            "object_type": kind, "content_id": link["content_id"], "platform": link["platform"],
            "published_at": when.isoformat() if when else None,
            "published_display": when.isoformat() if when else UNKNOWN,
            "relevance": analyzed["relevance"], "depth": analyzed["depth"], "quality": quality,
            "stance": analyzed["stance"], "quote": analyzed["quote"], "source_url": record.get("url", ""),
            "root_id": record.get("root_id", ""), "parent_id": record.get("parent_id", ""),
            "full_text": record["text"], "raw_score": raw, "quality_flags": flags,
            "profile_version": definition["profile_version"],
            "review_state": "accepted", "review_note": ""}


def representative_from(evidence, window, now):
    """Strongest eligible contribution, computed with the same clock as the score."""
    eligible = []
    for item in evidence:
        if item.get("review_state", "accepted") != "accepted":
            continue
        when = published(item.get("published_at"))
        if window != "all" and (when is None or when < now - timedelta(days=int(window))):
            continue
        if item.get("contribution", item["raw_score"]) <= 0:
            continue
        eligible.append(item)
    if not eligible:
        return None
    best = max(eligible, key=lambda e: (e.get("contribution", e["raw_score"]), e["raw_score"], e["source_hash"]))
    return {k: best.get(k) for k in ("source_hash", "quote", "published_display", "source_url", "stance", "relevance")}
