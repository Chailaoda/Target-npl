"""Evidence-linked, deterministic interest profiling over existing account content.

Rules are the auditable baseline: an arbitrary topic is supplied by the caller,
not selected from a built-in taxonomy. A prior v2 analysis only makes content
eligible; it is never reused as proof of relevance to a new topic.

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
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import re
import sqlite3

from .account_repository import analyzed_content_links
from .db_access import connect
from .pipeline import fingerprint

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

# The most recently finished (or currently started) definition is the active
# one. Re-analysing an older definition A after B must make A active again.
ACTIVE_DEFINITION_SQL = """SELECT * FROM interest_topic_definitions WHERE topic_key=?
    ORDER BY COALESCE(finished_at,started_at,created_at) DESC,
             created_at DESC, definition_hash DESC LIMIT 1"""


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


def _published(value):
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
    stance = _polarity(clauses[index])
    if stance:
        return stance
    for neighbor in (clauses[index - 1] if index else "",
                     clauses[index + 1] if index + 1 < len(clauses) else ""):
        if neighbor and TURN.search(neighbor):
            found = _polarity(neighbor)
            if found:
                return found
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


def _candidate_rows(db, account_key, definition):
    rows = []
    seen = set()
    for link in analyzed_content_links(db, account_key):
        if link["source_hash"] in seen:
            continue
        seen.add(link["source_hash"])
        conflict = db.execute("""SELECT 1 FROM account_attribution_conflicts
            WHERE platform=? AND object_type=? AND content_id=? AND status='pending'
              AND (current_account_key=? OR candidate_account_key=?) LIMIT 1""",
            (link["platform"], link["object_type"], link["content_id"], account_key, account_key)).fetchone()
        if conflict:
            continue
        source = db.execute("SELECT source_json FROM v2_sources WHERE source_hash=?", (link["source_hash"],)).fetchone()
        if not source:
            continue
        record = json.loads(source["source_json"])
        if record.get("identity_mode") != "public_source" or record.get("author_key") != account_key:
            continue
        analyses = db.execute("""SELECT a.analysis_key,a.result_json,r.decision FROM v2_analyses a
            LEFT JOIN v2_reviews r ON r.analysis_key=a.analysis_key
            WHERE a.source_hash=? AND a.result_json IS NOT NULL AND a.result_json<>''""", (link["source_hash"],)).fetchall()
        eligible = False
        for analysis in analyses:
            if analysis["decision"] == "reject":
                continue
            try:
                result = json.loads(analysis["result_json"])
            except (ValueError, TypeError):
                continue
            if result.get("engine") != "quarantined_model_output":
                eligible = True
                break
        if eligible:
            rows.append((link, record))
    return rows


def _metrics(evidence, now, window="all"):
    selected = []
    for item in evidence:
        if item.get("review_state", "accepted") != "accepted":
            continue
        published = _published(item.get("published_at"))
        if window != "all" and (published is None or published < now - timedelta(days=int(window))):
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
        published = _published(item.get("published_at"))
        if published:
            days.add(published.date().isoformat())
            dated.append(published)
            age = max(0.0, (now - published).total_seconds() / 86400)
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


def _raw_evidence(link, record, analyzed, definition):
    published = _published(record.get("published_at"))
    kind = "reply" if record["object_type"] == "comment" and record.get("parent_id") else record["object_type"]
    weight = BEHAVIOR_WEIGHTS.get(kind, 1.0)
    quality = 0.95 if published else 0.55
    flags = list(analyzed.get("quality_flags", []))
    if not published:
        flags.append("published_time_unknown")
    raw = weight * analyzed["relevance"] * analyzed["depth"] * quality
    return {"account_key": link["account_key"], "source_hash": link["source_hash"],
            "analysis_key": link["source_hash"],
            "object_type": kind, "content_id": link["content_id"], "platform": link["platform"],
            "published_at": published.isoformat() if published else None,
            "published_display": published.isoformat() if published else UNKNOWN,
            "relevance": analyzed["relevance"], "depth": analyzed["depth"], "quality": quality,
            "stance": analyzed["stance"], "quote": analyzed["quote"], "source_url": record.get("url", ""),
            "root_id": record.get("root_id", ""), "parent_id": record.get("parent_id", ""),
            "full_text": record["text"], "raw_score": raw, "quality_flags": flags,
            "profile_version": definition["profile_version"],
            "review_state": "accepted", "review_note": ""}


def _representative_from(evidence, window, now):
    """Strongest eligible contribution, computed with the same clock as the score."""
    eligible = []
    for item in evidence:
        if item.get("review_state", "accepted") != "accepted":
            continue
        published = _published(item.get("published_at"))
        if window != "all" and (published is None or published < now - timedelta(days=int(window))):
            continue
        if item.get("contribution", item["raw_score"]) <= 0:
            continue
        eligible.append(item)
    if not eligible:
        return None
    best = max(eligible, key=lambda e: (e.get("contribution", e["raw_score"]), e["raw_score"], e["source_hash"]))
    return {k: best.get(k) for k in ("source_hash", "quote", "published_display", "source_url", "stance", "relevance")}


class InterestService:
    def __init__(self, context):
        self.context = context

    def _connect(self, readonly=False):
        return connect(context=self.context, readonly=readonly)

    # ---- one active definition/version for every read path -----------------
    def _definition_row(self, db, topic):
        return db.execute(ACTIVE_DEFINITION_SQL, (topic,)).fetchone()

    def _resolve_definition(self, db, topic):
        row = self._definition_row(db, topic)
        return json.loads(row["definition_json"]) if row else None

    def _active_version(self, db, topic):
        definition = self._resolve_definition(db, topic)
        if definition:
            return definition["profile_version"]
        row = db.execute("SELECT profile_version FROM interest_profiles WHERE topic=? ORDER BY updated_at DESC LIMIT 1",
                         (topic,)).fetchone()
        return row[0] if row else None

    def _load_evidence(self, db, account_key, topic, version):
        rows = db.execute("""SELECT id,details_json FROM interest_evidence
            WHERE account_key=? AND topic=? AND profile_version=? ORDER BY source_hash""",
                          (account_key, topic, version)).fetchall()
        return [dict(json.loads(r["details_json"]), id=r["id"]) for r in rows]

    # ---- analysis status lives outside the business transaction -----------
    def _set_status(self, definition, status, at, error="", stats=None):
        # Committed separately: a rolled-back analysis must not erase 'failed'.
        with closing(self._connect()) as db, db:
            db.execute("""INSERT INTO interest_topic_definitions(
                topic_key,definition_json,definition_hash,status,created_at,started_at,finished_at,error,stats_json)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(topic_key,definition_hash) DO UPDATE SET status=excluded.status,
                started_at=CASE WHEN excluded.status='running' THEN excluded.started_at
                                ELSE interest_topic_definitions.started_at END,
                finished_at=excluded.finished_at,error=excluded.error,stats_json=excluded.stats_json""",
                (definition["topic"], json.dumps(definition, ensure_ascii=False), definition["definition_hash"],
                 status, at, at, None if status == "running" else at, error,
                 json.dumps(stats or {}, ensure_ascii=False)))

    def _assert_single_account_definition(self, definition):
        """A single-account recompute must reuse the active definition.

        Otherwise the new definition becomes globally active while all other
        accounts still hold profiles for the previous version, so they silently
        disappear from every search result. Switching definitions requires a
        full-topic analysis.
        """
        with closing(self._connect(readonly=True)) as db:
            active = self._resolve_definition(db, definition["topic"])
        if active and active.get("definition_hash") != definition["definition_hash"]:
            raise ValueError("切换主题定义需要对全部账号重新分析；单账号重算只能沿用当前有效定义")

    def analyze(self, topic, aliases=None, exclude_terms=None, description="", account_key=None):
        definition = define_topic(topic, aliases, exclude_terms, description)
        if account_key:
            self._assert_single_account_definition(definition)
        now = datetime.now(timezone.utc)
        observed_at = now.isoformat()
        # Cache key includes the algorithm version so a rules change re-analyses.
        cache_key = definition["profile_version"]
        self._set_status(definition, "running", observed_at)
        try:
            with closing(self._connect()) as db, db:
                if account_key and not db.execute(
                        "SELECT 1 FROM accounts WHERE account_key=? AND identity_mode='public_source'",
                        (account_key,)).fetchone():
                    raise ValueError("账号不存在或没有可靠公开身份")
                keys = [account_key] if account_key else [r[0] for r in db.execute(
                    "SELECT account_key FROM accounts WHERE identity_mode='public_source' ORDER BY account_key")]
                candidate_count = evidence_count = 0
                db.execute("INSERT OR IGNORE INTO interest_profile_versions VALUES (?,?,?)",
                           (definition["profile_version"],
                            "Verbatim literal rules; clause-scoped stance; independent coverage; capped threads; deterministic saturation",
                            observed_at))
                for key in keys:
                    rows = _candidate_rows(db, key, definition)
                    candidate_count += len(rows)
                    current = []
                    for link, record in rows:
                        source_hash = link["source_hash"]
                        cached = db.execute("""SELECT decision FROM interest_content_analysis
                            WHERE account_key=? AND topic_key=? AND source_hash=? AND definition_hash=?""",
                            (key, definition["topic"], source_hash, cache_key)).fetchone()
                        if cached:
                            if cached["decision"] == "accepted":
                                stored = db.execute("""SELECT details_json FROM interest_evidence
                                    WHERE account_key=? AND topic=? AND source_hash=? AND profile_version=?""",
                                    (key, definition["topic"], source_hash, definition["profile_version"])).fetchone()
                                if stored:
                                    current.append(json.loads(stored["details_json"]))
                                else:
                                    analyzed = analyze_text(record, definition)
                                    if analyzed["decision"] == "accepted":
                                        current.append(_raw_evidence(link, record, analyzed, definition))
                            continue
                        analyzed = analyze_text(record, definition)
                        analysis_key = fingerprint({"source": source_hash, "definition": cache_key})
                        db.execute("""INSERT OR REPLACE INTO interest_content_analysis(
                            account_key,topic_key,source_hash,definition_hash,analysis_key,decision,details_json,updated_at)
                            VALUES (?,?,?,?,?,?,?,?)""", (key, definition["topic"], source_hash, cache_key, analysis_key,
                            analyzed["decision"], json.dumps(analyzed, ensure_ascii=False), observed_at))
                        if analyzed["decision"] == "accepted":
                            current.append(_raw_evidence(link, record, analyzed, definition))
                    self._save_profile(db, key, definition, current, now)
                    evidence_count += len(current)
                stats = {"accounts": len(keys), "candidate_contents": candidate_count, "evidence": evidence_count,
                         "empty_public_account_data": not keys}
                db.execute("""UPDATE interest_topic_definitions SET status='completed',finished_at=?,stats_json=?
                    WHERE topic_key=? AND definition_hash=?""",
                    (utcnow(), json.dumps(stats, ensure_ascii=False), definition["topic"], definition["definition_hash"]))
        except Exception as error:
            self._set_status(definition, "failed", utcnow(), str(error)[:500])
            raise
        return self.search(definition["topic"], definition=definition)

    def _save_profile(self, db, account_key, definition, evidence, now):
        """Current version wins: one evidence row per (account, topic, source).

        id is always assigned by SQLite. The business key
        (account_key, topic, analysis_key) is used for upserts, so the same
        source_hash can be evidence for several topics without a PK collision.
        """
        version = definition["profile_version"]
        keep = {item["source_hash"] for item in evidence}
        for old in db.execute("SELECT id,source_hash FROM interest_evidence WHERE account_key=? AND topic=?",
                              (account_key, definition["topic"])).fetchall():
            if old["source_hash"] not in keep:
                db.execute("DELETE FROM interest_evidence WHERE id=?", (old["id"],))
        for item in evidence:
            old = db.execute("""SELECT review_state,review_note FROM interest_evidence
                WHERE account_key=? AND topic=? AND source_hash=?""",
                (account_key, definition["topic"], item["source_hash"])).fetchone()
            if old:
                item.update(review_state=old["review_state"], review_note=old["review_note"])
            db.execute("""INSERT INTO interest_evidence(account_key,topic,analysis_key,source_hash,stance,quote,observed_at,
                object_type,published_at,relevance,depth,quality,source_url,quality_flags,details_json,profile_version,review_state,review_note)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_key,topic,analysis_key) DO UPDATE SET stance=excluded.stance,quote=excluded.quote,
                observed_at=excluded.observed_at,object_type=excluded.object_type,published_at=excluded.published_at,
                relevance=excluded.relevance,depth=excluded.depth,quality=excluded.quality,source_url=excluded.source_url,
                quality_flags=excluded.quality_flags,details_json=excluded.details_json,profile_version=excluded.profile_version""",
                (account_key, definition["topic"], item["source_hash"], item["source_hash"], item["stance"], item["quote"], now.isoformat(),
                 item["object_type"], item["published_at"], item["relevance"], item["depth"], item["quality"], item["source_url"],
                 json.dumps(item["quality_flags"], ensure_ascii=False), json.dumps(item, ensure_ascii=False), version,
                 item.get("review_state", "accepted"), item.get("review_note", "")))
            stored = db.execute("""SELECT id FROM interest_evidence
                WHERE account_key=? AND topic=? AND source_hash=? AND profile_version=?""",
                (account_key, definition["topic"], item["source_hash"], version)).fetchone()
            if stored:
                item["id"] = stored["id"]
                db.execute("UPDATE interest_evidence SET details_json=? WHERE id=?",
                           (json.dumps(item, ensure_ascii=False), stored["id"]))
        valid = [e for e in evidence if e.get("review_state", "accepted") == "accepted"]
        metrics = _metrics(valid, now)
        windows = {str(w): _metrics(valid, now, w) for w in WINDOWS}
        windows["all"] = metrics
        details = {"windows": windows, "algorithm_version": ALGORITHM_VERSION,
                   "definition_hash": definition["definition_hash"], "half_life_days": HALF_LIFE_DAYS,
                   "evidence_refs": [e["analysis_key"] for e in valid], "synthetic": bool(valid) and all(
                       json.loads(r[0]).get("synthetic") is True for r in db.execute(
                           "SELECT source_json FROM v2_sources WHERE source_hash IN (SELECT source_hash FROM account_content_links WHERE account_key=?)",
                           (account_key,)))}
        db.execute("""INSERT INTO interest_profiles(account_key,topic,score,content_count,active_days,stance_summary,trend,
            evidence_notes,profile_version,updated_at,evidence_count,independent_content_count,coverage,last_activity,details_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_key,topic,profile_version) DO UPDATE SET score=excluded.score,content_count=excluded.content_count,
            active_days=excluded.active_days,stance_summary=excluded.stance_summary,trend=excluded.trend,
            evidence_notes=excluded.evidence_notes,updated_at=excluded.updated_at,evidence_count=excluded.evidence_count,
            independent_content_count=excluded.independent_content_count,coverage=excluded.coverage,
            last_activity=excluded.last_activity,details_json=excluded.details_json""",
            (account_key, definition["topic"], metrics["interest_score"], metrics["independent_content_count"], metrics["active_days"],
             metrics["stance"], metrics["trend"],
             "兴趣参与强度，不是概率或人格判断；半衰期用于趋势对比，窗口决定计入证据。",
             version, now.isoformat(), metrics["evidence_count"], metrics["independent_content_count"],
             metrics["coverage"], metrics["last_activity"], json.dumps(details, ensure_ascii=False)))

    def topic_status(self, topic):
        key = normalize_topic(topic)
        with closing(self._connect(readonly=True)) as db:
            row = self._definition_row(db, key)
            if not row:
                legacy = db.execute("SELECT 1 FROM interest_profiles WHERE topic=? LIMIT 1", (key,)).fetchone()
                return {"topic": key, "status": "completed" if legacy else "not_analyzed",
                        "stats": {}, "error": "", "started_at": None, "finished_at": None}
            return {"topic": key, "definition": json.loads(row["definition_json"]), "status": row["status"],
                    "stats": json.loads(row["stats_json"] or "{}"), "error": row["error"],
                    "started_at": row["started_at"], "finished_at": row["finished_at"]}

    def search(self, topic, *, min_score=0, platform="", window="all", min_evidence=1, stance="", sort="score",
               limit=20, offset=0, definition=None):
        if window not in ("all", "30", "90", "180", 30, 90, 180):
            raise ValueError("时间窗口仅支持 30/90/180/all")
        window = str(window)
        limit, offset = int(limit), int(offset)
        if not 1 <= limit <= 100 or offset < 0 or not 0 <= float(min_score) <= 100 or int(min_evidence) < 0:
            raise ValueError("筛选或分页参数无效")
        if sort not in ("score", "recent", "evidence"):
            raise ValueError("排序仅支持 score/recent/evidence")
        if stance and stance not in STANCES:
            raise ValueError("态度筛选无效")
        key = normalize_topic(topic)
        with closing(self._connect(readonly=True)) as db:
            active = definition or self._resolve_definition(db, key)
            if active is None:
                legacy = db.execute("SELECT profile_version FROM interest_profiles WHERE topic=? ORDER BY updated_at DESC LIMIT 1",
                                    (key,)).fetchone()
                if not legacy:
                    return {"topic": key, "status": "not_analyzed", "items": [], "total": 0,
                            "limit": limit, "offset": offset, "message": "该主题尚未分析；不代表没有任何人感兴趣。"}
                active = define_topic(key)
                active["profile_version"] = legacy[0]
            version = active["profile_version"]
            # Static filters stay in SQL; time-dependent metrics are recomputed
            # with the same clock/metric function used by the evidence detail.
            where = ["p.topic=?", "p.profile_version=?", "a.identity_mode='public_source'"]
            params = [key, version]
            if platform:
                where.append("a.platform=?"); params.append(platform)
            rows = db.execute(f"""SELECT a.account_key,a.platform,a.account_id,a.nickname,a.profile_url,
                p.topic,p.profile_version,p.updated_at,p.details_json
                FROM interest_profiles p JOIN accounts a ON a.account_key=p.account_key
                WHERE {" AND ".join(where)} ORDER BY p.account_key""", params).fetchall()
            now = datetime.now(timezone.utc)
            items = []
            for row in rows:
                evidence = self._load_evidence(db, row["account_key"], key, version)
                metrics = _metrics(evidence, now, window)
                if metrics["interest_score"] < float(min_score):
                    continue
                if metrics["evidence_count"] < int(min_evidence):
                    continue
                if stance and metrics["stance"] != stance:
                    continue
                details = json.loads(row["details_json"])
                item = {"account_key": row["account_key"], "platform": row["platform"],
                        "account_id": row["account_id"], "nickname": row["nickname"],
                        "profile_url": row["profile_url"], "topic": key, **metrics,
                        "profile_version": row["profile_version"], "updated_at": row["updated_at"],
                        "synthetic": details.get("synthetic", False)}
                item["representative_evidence"] = _representative_from(evidence, window, now)
                items.append(item)
            if sort == "score":
                items.sort(key=lambda i: (i["interest_score"], i["evidence_count"], i["account_key"]), reverse=True)
            elif sort == "evidence":
                items.sort(key=lambda i: (i["evidence_count"], i["interest_score"], i["account_key"]), reverse=True)
            else:
                items.sort(key=lambda i: (i["last_activity"] or "", i["interest_score"], i["account_key"]), reverse=True)
            total = len(items)
            items = items[offset:offset + limit]
        status = self.topic_status(key)
        return {"topic": key, "definition": active, "status": status["status"],
                "analysis": status, "items": items, "total": total, "limit": limit, "offset": offset,
                "message": "已分析，当前条件下无匹配结果。" if not items else ""}

    def account(self, account_key):
        with closing(self._connect(readonly=True)) as db:
            account = db.execute("SELECT * FROM accounts WHERE account_key=? AND identity_mode='public_source'",
                                 (account_key,)).fetchone()
            if not account:
                return None
            from .account_repository import account_overview, attribution_conflicts
            overview = account_overview(db, account_key)
            overview.pop("history", None)
            rows = db.execute("SELECT * FROM interest_profiles WHERE account_key=? ORDER BY updated_at DESC,profile_version DESC",
                              (account_key,)).fetchall()
            latest = {}
            for row in rows:
                if self._active_version(db, row["topic"]) != row["profile_version"]:
                    continue
                latest.setdefault(row["topic"], row)
            now = datetime.now(timezone.utc)
            interests = []
            for row in latest.values():
                evidence = self._load_evidence(db, account_key, row["topic"], row["profile_version"])
                windows = {str(w): _metrics(evidence, now, w) for w in WINDOWS}
                windows["all"] = _metrics(evidence, now)
                details = json.loads(row["details_json"])
                interests.append({"topic": row["topic"], **windows["all"], "windows": windows,
                                  "profile_version": row["profile_version"], "updated_at": row["updated_at"],
                                  "synthetic": details.get("synthetic", False)})
            interests.sort(key=lambda p: (p["coverage"] == "high", p["interest_score"], p["evidence_count"], p["topic"]), reverse=True)
            return {**overview, "interests": interests, "attribution_conflicts": attribution_conflicts(db, account_key=account_key),
                    "scope_note": "仅基于已采集且有可靠公开账号 ID 的内容；未观测不等于没有兴趣。"}

    def evidence(self, account_key, topic, window="all"):
        if window not in ("all", "30", "90", "180", 30, 90, 180):
            raise ValueError("时间窗口仅支持 30/90/180/all")
        window = str(window)
        key = normalize_topic(topic)
        with closing(self._connect(readonly=True)) as db:
            version = self._active_version(db, key)
            evidence = self._load_evidence(db, account_key, key, version) if version else []
        now = datetime.now(timezone.utc)
        metrics = _metrics(evidence, now, window)
        visible = []
        for item in evidence:
            published = _published(item.get("published_at"))
            if window != "all" and (published is None or published < now - timedelta(days=int(window))):
                continue
            visible.append(item)
        visible.sort(key=lambda e: (e.get("review_state", "accepted") == "accepted",
                                   e.get("contribution", 0), e["raw_score"], e["source_hash"]), reverse=True)
        return {"account_key": account_key, "topic": key, "profile_version": version or "",
                **metrics, "items": visible,
                "score_note": "兴趣参与指数不是概率或正向好感；它是窗口内独立证据贡献的确定性映射。半衰期用于趋势对比，窗口决定计入哪些证据；态度与证据充分程度单独展示。"}

    def feedback(self, evidence_id, action, note="", profile_version=None):
        if action not in ("irrelevant", "stance_error", "attribution_question", "restore"):
            raise ValueError("证据反馈类型无效")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("反馈说明不能超过 1000 字符")
        # The evidence id survives a definition switch (business-key upsert),
        # so an old page must not be able to mutate the new profile with it.
        if not isinstance(profile_version, str) or not profile_version.strip():
            raise ValueError("反馈必须携带证据详情页展示的画像版本，请刷新证据详情后重试")
        state = "accepted" if action == "restore" else action
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT * FROM interest_evidence WHERE id=?", (int(evidence_id),)).fetchone()
            if not row:
                raise ValueError("兴趣证据不存在")
            version = self._active_version(db, row["topic"])
            if version is None or row["profile_version"] != version:
                raise ValueError("证据不属于当前画像版本，请重新分析该主题")
            if profile_version != version or profile_version != row["profile_version"]:
                raise ValueError("证据版本已变化，请刷新证据详情后重新提交反馈")
            definition = self._resolve_definition(db, row["topic"]) or define_topic(row["topic"])
            definition = dict(definition, profile_version=version)
            db.execute("UPDATE interest_evidence SET review_state=?,review_note=? WHERE id=?",
                       (state, note, int(evidence_id)))
            item = json.loads(row["details_json"])
            item.update(review_state=state, review_note=note)
            db.execute("UPDATE interest_evidence SET details_json=? WHERE id=?",
                       (json.dumps(item, ensure_ascii=False), int(evidence_id)))
            evidence = self._load_evidence(db, row["account_key"], row["topic"], version)
            self._save_profile(db, row["account_key"], definition, evidence, datetime.now(timezone.utc))
        return self.account(row["account_key"])

    # ---- attribution conflict review (WP6) --------------------------------
    def list_conflicts(self, status="pending", account_key=None, limit=50):
        """Read-only view of attribution conflicts for the review workbench.

        `current_authority` is read from the live content link, so the operator
        sees what actually owns the content now rather than what the conflict
        row recorded when it was first detected. Snippets come from the stored
        raw sources and are truncated: the workbench shows evidence, not a post
        archive.
        """
        from .account_repository import attribution_conflicts
        if status not in ("pending", "resolved", "all"):
            raise ValueError("冲突状态筛选无效")
        if isinstance(limit, str) and limit.isdigit():
            limit = int(limit)
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("冲突数量上限应为 1–200 的整数")
        if account_key is not None and (not isinstance(account_key, str) or len(account_key) > 200):
            raise ValueError("账号标识无效")
        with closing(self._connect(readonly=True)) as db:
            rows = []
            for state in (("pending", "resolved") if status == "all" else (status,)):
                rows += [dict(r) for r in attribution_conflicts(db, status=state, account_key=account_key)]
            rows.sort(key=lambda r: r["id"], reverse=True)
            pending_total = sum(1 for r in rows if r["status"] == "pending")
            items = [self._conflict_view(db, row) for row in rows[:limit]]
        return {"items": items, "total_pending": pending_total}

    def _conflict_view(self, db, row):
        link = db.execute("""SELECT attribution_authority,source_hash FROM account_content_links
            WHERE platform=? AND object_type=? AND content_id=? AND account_key=?""",
                          (row["platform"], row["object_type"], row["content_id"], row["current_account_key"])).fetchone()
        # Older conflict rows were written before source hashes were recorded;
        # the live link still points at the text the operator has to judge.
        snippet = self._conflict_snippet(
            db, row["candidate_source_hash"], row["current_source_hash"],
            link["source_hash"] if link else "")
        return {
            "conflict_id": row["id"],
            "platform": row["platform"],
            "object_type": row["object_type"],
            "content_id": row["content_id"],
            "current_key": row["current_account_key"],
            "current_authority": link["attribution_authority"] if link else "",
            "candidate_key": row["candidate_account_key"],
            "candidate_authority": row["candidate_authority"],
            "snippet": snippet,
            "detected_at": row["first_seen_at"],
            "status": row["status"],
            "resolution": row["resolution"],
        }

    @staticmethod
    def _conflict_snippet(db, *source_hashes, limit=280):
        for source_hash in source_hashes:
            if not source_hash:
                continue
            stored = db.execute("SELECT source_json FROM v2_sources WHERE source_hash=?", (source_hash,)).fetchone()
            if not stored:
                continue
            try:
                record = json.loads(stored["source_json"])
            except ValueError:
                continue
            text = re.sub(r"\s+", " ", str(record.get("text") or "")).strip()
            if text:
                return text[:limit]
        return ""

    def resolve_conflict(self, platform, object_type, content_id, chosen_key, note=""):
        """Confirm one attribution by hand; authority is always manual_review."""
        from .account_repository import resolve_attribution_conflict
        for name, value in (("platform", platform), ("object_type", object_type),
                            ("content_id", content_id), ("chosen_key", chosen_key)):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError(f"冲突裁决参数无效：{name}")
        if not isinstance(note, str) or len(note) > 500:
            raise ValueError("裁决说明不能超过 500 字符")
        with closing(self._connect()) as db:
            pending = db.execute("""SELECT id FROM account_attribution_conflicts
                WHERE platform=? AND object_type=? AND content_id=? AND status='pending'
                ORDER BY id DESC LIMIT 1""", (platform, object_type, content_id)).fetchone()
            try:
                active = resolve_attribution_conflict(
                    db, platform, object_type, content_id, chosen_key,
                    authority="manual_review", note=note)
            except sqlite3.IntegrityError as error:
                # Moving a link to an account that has no account row would
                # break referential integrity; report it as a reviewable input
                # problem instead of dropping the connection.
                raise ValueError("裁决目标账号没有账号记录，无法完成归属迁移；请先重新采集该账号") from error
            return {"resolved": True,
                    "conflict_id": pending["id"] if pending else None,
                    "active_account_key": active,
                    "authority": "manual_review",
                    "updated_at": utcnow()}

    def recent_topics(self, limit=8):
        limit = max(1, min(int(limit), 30))
        with closing(self._connect(readonly=True)) as db:
            rows = db.execute("""SELECT * FROM interest_topic_definitions d
                WHERE COALESCE(finished_at,started_at,created_at)=(
                    SELECT MAX(COALESCE(finished_at,started_at,created_at))
                    FROM interest_topic_definitions x WHERE x.topic_key=d.topic_key)
                ORDER BY COALESCE(finished_at,started_at,created_at) DESC,topic_key LIMIT ?""", (limit,)).fetchall()
            result = []
            for row in rows:
                definition = json.loads(row["definition_json"])
                count = db.execute("""SELECT count(*) FROM interest_profiles p
                    JOIN accounts a ON a.account_key=p.account_key
                    WHERE p.topic=? AND p.profile_version=? AND a.identity_mode='public_source'""",
                    (row["topic_key"], definition["profile_version"])).fetchone()[0]
                result.append({"topic": row["topic_key"], "display_topic": definition.get("display_topic", row["topic_key"]),
                               "aliases": definition.get("aliases", []), "description": definition.get("description", ""),
                               "status": row["status"], "profile_count": count,
                               "finished_at": row["finished_at"], "stats": json.loads(row["stats_json"] or "{}")})
            return result

    def accounts(self, keyword="", limit=50, offset=0):
        from .account_repository import search_accounts
        limit, offset = int(limit), int(offset)
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("分页参数无效")
        with closing(self._connect(readonly=True)) as db:
            items, total = search_accounts(db, keyword, limit, offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}
