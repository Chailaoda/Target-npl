"""Durable normalization, multidimensional analysis, review and local reports."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from .normalization import read_export, public_url
from .pipeline import fingerprint
from .rules_v2 import VERSION, load_policy, local_analyze, ark_analyzer, analysis_policy, apply_decision, validate_result
from .task_contract import validate_task,apply_scope

ROOT = Path(__file__).resolve().parents[1]


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS v2_sources(source_hash TEXT PRIMARY KEY, object_key TEXT NOT NULL, source_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v2_policies(policy_hash TEXT PRIMARY KEY, policy_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v2_observations(input_hash TEXT, line INTEGER, source_hash TEXT REFERENCES v2_sources, keyword TEXT, raw_hash TEXT, input_file TEXT,
        PRIMARY KEY(input_hash,line));
    CREATE TABLE IF NOT EXISTS v2_jobs(job_id TEXT PRIMARY KEY, created_at TEXT, finished_at TEXT, status TEXT, input_hash TEXT, policy_hash TEXT, identity_json TEXT);
    CREATE TABLE IF NOT EXISTS v2_tasks(job_id TEXT REFERENCES v2_jobs, source_hash TEXT REFERENCES v2_sources, analysis_key TEXT, status TEXT, error_code TEXT,
        PRIMARY KEY(job_id,source_hash));
    CREATE TABLE IF NOT EXISTS v2_analyses(analysis_key TEXT PRIMARY KEY, source_hash TEXT REFERENCES v2_sources, policy_hash TEXT, context_json TEXT, result_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS v2_reviews(analysis_key TEXT PRIMARY KEY REFERENCES v2_analyses, decision TEXT, note TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS v2_review_history(id INTEGER PRIMARY KEY, analysis_key TEXT REFERENCES v2_analyses, decision TEXT, note TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS v2_attempts(id INTEGER PRIMARY KEY, job_id TEXT REFERENCES v2_jobs, analysis_key TEXT, api_called INTEGER,
        status TEXT, error_code TEXT, usage_json TEXT, created_at TEXT);
    """)
    columns={r[1] for r in db.execute('PRAGMA table_info(v2_jobs)')}
    for column in ('task_json','config_revision'):
        if column not in columns:db.execute(f'ALTER TABLE v2_jobs ADD COLUMN {column} TEXT')
    # WP3: idempotent forward migration for account/interest tables. Pure
    # CREATE IF NOT EXISTS — old code versions ignore the new tables.
    from .account_repository import migrate as _migrate_accounts
    _migrate_accounts(db)
    return db


def build_context(record, versions):
    context = []
    missing = False
    if record["object_type"] == "comment":
        refs = []
        if record["parent_id"]:
            refs.append(("comment", record["parent_id"]))
        if record["root_id"]:
            refs.append(("post", record["root_id"]))
        if not refs:
            missing = True
        for ref in refs:
            candidates = versions.get((record["platform"], *ref), [])
            if len(candidates) != 1:
                missing = True
                continue
            parent = candidates[0]
            context.append({"object_type": parent["object_type"], "text": parent["text"], "source_hash": fingerprint(parent)})
    return context, missing


def abstention(record, policy, context, missing, code):
    result = local_analyze(record, policy, context)
    result.update(accepted=False, score=0, stance="unclear", intent="unclear", promotion="uncertain", author_role="unknown",
                  evidence_level="insufficient", engine="quarantined_model_output", context_missing=missing,
                  reason=f"模型输出未通过 {code} 校验，未采用其个人意向结论；原文保留在待复核队列，无自动重试。",
                  quality_flags=["model_output_rejected", code])
    result["evidence"] = {k: v for k, v in result["evidence"].items() if k == "topic"}
    return result


def save_abstention(db, job_id, source_hash, key, policy_hash, record, policy, context, missing, code):
    result = abstention(record, policy, context, missing, code)
    db.execute("INSERT OR IGNORE INTO v2_analyses VALUES (?,?,?,?,?,?)", (key, source_hash, policy_hash, json.dumps(context, ensure_ascii=False), json.dumps(result, ensure_ascii=False), utcnow()))
    db.execute("UPDATE v2_tasks SET status='needs_review',error_code=? WHERE job_id=? AND source_hash=?", (code, job_id, source_hash))


def run(input_path, policy_path, db_path, output, mode="rules", connection_path="config/connection.json", max_calls=40, analyzer=None, job_id=None,task=None,config_revision=None,on_start=None,attribution_authority="raw_import"):
    policy = load_policy(policy_path)
    if task is not None:validate_task(task)
    observations = read_export(input_path)
    if not observations:
        raise ValueError("输入为空；不将无采集数据报成筛选完成")
    if mode not in ("rules", "ark") or type(max_calls) is not int or not 1 <= max_calls <= 500:
        raise ValueError("Invalid mode/API limit")
    policy_hash = fingerprint(policy)
    interpretation_hash = fingerprint(analysis_policy(policy)) if mode == "ark" else policy_hash
    identity = {"engine": mode, "schema": 2, "analyzer_version": VERSION}
    config = None
    if mode == "ark":
        from .connectors import load_connection
        config = load_connection(connection_path)
        identity.update(model=config["ark"]["model"], endpoint=config["ark"]["base_url"])
    unique = {fingerprint(item["record"]): item["record"] for item in observations}
    input_hash = fingerprint([{"hash": fingerprint(o["record"]), "raw_hash": o["raw_hash"], "keyword": o["keyword"]} for o in observations])
    versions = {}
    for record in unique.values():
        versions.setdefault((record["platform"], record["object_type"], record["content_id"]), []).append(record)
    tasks = {}
    for source_hash, record in unique.items():
        context, missing = build_context(record, versions)
        key = fingerprint({"source": source_hash, "policy": interpretation_hash, "identity": identity, "context": context, "context_missing": missing})
        tasks[source_hash] = (key, context, missing)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as db:
        cached = {key: json.loads(row[0]) for key, _, _ in tasks.values() if (row := db.execute("SELECT result_json FROM v2_analyses WHERE analysis_key=?", (key,)).fetchone())}
        # Migrate existing exact-source/model/context results to the separated
        # interpretation key only after revalidating their evidence locally.
        if mode == "ark":
            compatible_policies = [row[0] for row in db.execute("SELECT policy_hash,policy_json FROM v2_policies") if fingerprint(analysis_policy(json.loads(row[1]))) == interpretation_hash]
            for source_hash, (key, context, missing) in tasks.items():
                if key in cached:
                    continue
                for legacy_hash in compatible_policies:
                    old_key = fingerprint({"source": source_hash, "policy": legacy_hash, "identity": identity, "context": context, "context_missing": missing})
                    row = db.execute("SELECT result_json FROM v2_analyses WHERE analysis_key=?", (old_key,)).fetchone()
                    if not row:
                        continue
                    base = json.loads(row[0])
                    obj = {k: base[k] for k in policy["dimensions"]}
                    obj.update(reason=base["reason"], evidence={d: e["quote"] if e.get("source", "self") == "self" else {"source": e["source"], "quote": e["quote"]} for d, e in base["evidence"].items()})
                    validate_result(obj, unique[source_hash], policy, context)
                    with db:
                        db.execute("INSERT OR IGNORE INTO v2_analyses VALUES (?,?,?,?,?,?)", (key, source_hash, interpretation_hash, json.dumps(context, ensure_ascii=False), json.dumps(base, ensure_ascii=False), utcnow()))
                    cached[key] = base
                    break
        pending = sum(key not in cached and bool(unique[source_hash]["text"].strip()) for source_hash, (key, _, _) in tasks.items())
        if mode == "ark" and pending > max_calls:
            raise ValueError(f"新增评估 {pending} 条超过调用上限 {max_calls}；未读取密钥或调用模型")
        if job_id:
            old = db.execute("SELECT * FROM v2_jobs WHERE job_id=?", (job_id,)).fetchone()
            if old is None or (old["input_hash"], old["policy_hash"], json.loads(old["identity_json"])) != (input_hash, policy_hash, identity):
                raise ValueError("续跑任务的输入、模板或分析器版本已变化，请新建批次")
            if json.loads(old['task_json'] or 'null')!=task:raise ValueError('续跑任务的统一目标模板已变化，请新建批次')
        else:
            job_id = uuid.uuid4().hex
        with db:
            db.execute("INSERT OR IGNORE INTO v2_policies VALUES (?,?)", (policy_hash, json.dumps(policy, ensure_ascii=False)))
            db.execute("INSERT OR IGNORE INTO v2_jobs(job_id,created_at,finished_at,status,input_hash,policy_hash,identity_json,task_json,config_revision) VALUES (?,?,NULL,?,?,?,?,?,?)", (job_id, utcnow(), "running", input_hash, policy_hash, json.dumps(identity),json.dumps(task,ensure_ascii=False) if task else None,config_revision))
            db.execute("UPDATE v2_jobs SET status='running',finished_at=NULL WHERE job_id=?", (job_id,))
            for source_hash, record in unique.items():
                db.execute("INSERT OR IGNORE INTO v2_sources VALUES (?,?,?)", (source_hash, f"{record['platform']}:{record['object_type']}:{record['content_id']}", json.dumps(record, ensure_ascii=False)))
                db.execute("INSERT OR IGNORE INTO v2_tasks VALUES (?,?,?,'pending',NULL)", (job_id, source_hash, tasks[source_hash][0]))
            for obs in observations:
                db.execute("INSERT OR IGNORE INTO v2_observations VALUES (?,?,?,?,?,?)", (input_hash, obs["line"], fingerprint(obs["record"]), obs["keyword"], obs["raw_hash"], str(Path(input_path).resolve())))
            # WP3: ingest public-account profiles and content links. Display
            # profile data lives in account tables only; it never participates
            # in source_hash/analysis_key, so profile updates cannot invalidate
            # cached analyses. Anonymous/unknown rows are skipped untouched.
            # NOTE: these links describe the latest COLLECTED version and are
            # written BEFORE model analysis - WP4 must use
            # account_repository.analyzed_content_links() for verified evidence.
            # attribution_authority is explicit: plain/replayed imports stay at
            # 'raw_import' and cannot silently revert a confirmed attribution.
            from .account_repository import ingest_observations as _ingest_accounts
            _ingest_accounts(db, observations, utcnow(), fingerprint, attribution_authority)
        if on_start is not None:on_start(job_id)
        calls, reused, errors, tokens, consecutive_errors = 0, 0, 0, 0, 0
        try:
            for source_hash, record in unique.items():
                key, context, missing = tasks[source_hash]
                if key in cached:
                    reused += 1
                    with db:
                        state = "needs_review" if cached[key].get("engine") == "quarantined_model_output" else "completed"
                        db.execute("UPDATE v2_tasks SET status=?,error_code=NULL WHERE job_id=? AND source_hash=?", (state, job_id, source_hash))
                    continue
                was_called = False
                try:
                    if analyzer is None and mode == "ark" and record["text"].strip():
                        analyzer = ark_analyzer(config, policy)
                    if mode == "ark" and record["text"].strip():
                        calls += 1
                        was_called = True
                    result = analyzer(record, context) if analyzer and record["text"].strip() else local_analyze(record, policy, context)
                    if not record["text"].strip():
                        result["reason"] = "无文字正文，保留源记录；未执行图片 OCR 或媒体分析，不判为无兴趣。"
                        result["quality_flags"].append("empty_text_no_ocr")
                    result["context_missing"] = missing
                    consecutive_errors = 0
                    tokens += result.get("usage", {}).get("total_tokens", 0)
                    # Each success is committed independently: interrupted jobs
                    # can resume using completed analyses without duplicate calls.
                    with db:
                        db.execute("INSERT INTO v2_attempts(job_id,analysis_key,api_called,status,error_code,usage_json,created_at) VALUES (?,?,?,'completed',NULL,?,?)", (job_id, key, int(was_called), json.dumps(result.get("usage", {})), utcnow()))
                        db.execute("INSERT OR IGNORE INTO v2_analyses VALUES (?,?,?,?,?,?)", (key, source_hash, interpretation_hash, json.dumps(context, ensure_ascii=False), json.dumps(result, ensure_ascii=False), utcnow()))
                        db.execute("UPDATE v2_tasks SET status='completed',error_code=NULL WHERE job_id=? AND source_hash=?", (job_id, source_hash))
                except (ValueError, OSError) as error:
                    errors += 1
                    consecutive_errors += 1
                    code = getattr(error, "validation_code", type(error).__name__)
                    usage = getattr(error, "usage", {})
                    tokens += usage.get("total_tokens", 0)
                    # Never store arbitrary exception bodies or model responses.
                    with db:
                        db.execute("INSERT INTO v2_attempts(job_id,analysis_key,api_called,status,error_code,usage_json,created_at) VALUES (?,?,?,'failed',?,?,?)", (job_id, key, int(was_called), code, json.dumps(usage), utcnow()))
                        db.execute("UPDATE v2_tasks SET status='failed',error_code=? WHERE job_id=? AND source_hash=?", (code, job_id, source_hash))
                        if getattr(error, "validation_code", None):
                            save_abstention(db, job_id, source_hash, key, interpretation_hash, record, policy, context, missing, code)
                    if consecutive_errors >= 10:
                        break  # 提升容错弹性，只有连续10条系统性严重异常才熔断保护
        finally:
            with db:
                remaining = db.execute("SELECT count(*) FROM v2_tasks WHERE job_id=? AND status NOT IN ('completed','needs_review')", (job_id,)).fetchone()[0]
                review_count = db.execute("SELECT count(*) FROM v2_tasks WHERE job_id=? AND status='needs_review'", (job_id,)).fetchone()[0]
                db.execute("UPDATE v2_jobs SET status=?,finished_at=? WHERE job_id=?", ("partial" if remaining else "completed_with_review" if review_count else "completed", utcnow(), job_id))
        result = export_job(db, job_id, out, policy)
        result.update(api_calls=calls, reused_count=reused, failed_this_run=errors, new_tokens=tokens)
        (out / f"manifest-{job_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


def job_items(db, job_id):
    rows = db.execute("""SELECT s.source_json,t.analysis_key,t.status,t.error_code,a.result_json,a.context_json,r.decision,r.note
        FROM v2_tasks t JOIN v2_sources s ON s.source_hash=t.source_hash
        LEFT JOIN v2_analyses a ON a.analysis_key=t.analysis_key LEFT JOIN v2_reviews r ON r.analysis_key=t.analysis_key
        WHERE t.job_id=? ORDER BY s.object_key""", (job_id,)).fetchall()
    return [{"source": json.loads(r["source_json"]), "analysis_key": r["analysis_key"], "status": r["status"], "error_code": r["error_code"],
             "result": json.loads(r["result_json"]) if r["result_json"] else None,'analysis_context':json.loads(r['context_json'] or '[]'), "review": {"decision": r["decision"], "note": r["note"]}} for r in rows]


def projected_items(db, job, policy=None):
    items = job_items(db, job["job_id"])
    task=json.loads(job['task_json'] or 'null')
    if policy is None:
        snapshot = db.execute("SELECT policy_json FROM v2_policies WHERE policy_hash=?", (job["policy_hash"],)).fetchone()
        policy = json.loads(snapshot[0]) if snapshot else None
    if policy:
        for item in items:
            if item["result"]:
                item["result"] = apply_decision(item["result"], item["source"], policy)
                item['result']=apply_scope(item['result'],item['source'],item['analysis_context'],task,job['created_at'])
    return items


def export_job(db, job_id, output, policy=None):
    job = db.execute("SELECT * FROM v2_jobs WHERE job_id=?", (job_id,)).fetchone()
    if job is None:
        raise ValueError("Job not found")
    items = projected_items(db, job, policy)
    task=json.loads(job["task_json"] or "null")
    groups = {}
    for item in items:
        source = item["source"]
        # Missing stable author keys never collapse different contents by nickname.
        group_key = (source["platform"], source["author_key"] or f"unknown:{source['object_type']}:{source['content_id']}")
        groups.setdefault(group_key, []).append(item)
    candidates = sum(bool(i["result"] and i["result"]["accepted"]) for i in items)
    related = sum(bool(i["result"] and i["result"]["topic"] == "relevant" and not i["result"]["accepted"] and i["status"] != "needs_review" and i['result'].get('scope_check',{}).get('status') not in ('excluded','unknown')) for i in items)
    candidate_groups = [g for g in groups.values() if any(i["result"] and i["result"]["accepted"] for i in g)]
    counts = {"candidate_public_accounts": sum(g[0]["source"]["identity_mode"] == "public_source" for g in candidate_groups),
              "candidate_anonymous_groups": sum(g[0]["source"]["identity_mode"] == "upstream_anonymized" for g in candidate_groups),
              "candidate_unknown_author_contents": sum(g[0]["source"]["identity_mode"] == "unknown" for g in candidate_groups)}
    summary = {"job_id": job_id, "status": job["status"], "unique_contents": len(items), "candidate_contents": candidates, "related_only_contents": related,
               "failed_contents": sum(i["status"] not in ("completed", "needs_review") for i in items), "needs_review_contents": sum(i["status"] == "needs_review" for i in items), **counts, "policy_hash": job["policy_hash"]}
    summary.update(config_revision=job['config_revision'],task=task,scope_excluded_contents=sum((i['result'] or {}).get('scope_check',{}).get('status')=='excluded' for i in items),scope_unknown_contents=sum((i['result'] or {}).get('scope_check',{}).get('status')=='unknown' for i in items))
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    report = output / f"report-{job_id}.html"
    export = output / f"results-{job_id}.jsonl"
    export.write_text(''.join(json.dumps(i, ensure_ascii=False) + '\n' for i in items), encoding="utf-8")
    esc = lambda value: html.escape(str(value), quote=True)
    names = {"topic": "相关性", "stance": "态度", "intent": "意向", "promotion": "推广", "author_role": "作者角色"}
    values = {"relevant": "相关", "unrelated": "无关", "uncertain": "不确定", "positive": "正向", "negative": "负向", "neutral": "中性", "unclear": "未明确",
              "seeking_company": "寻找同行", "planning": "参加计划", "attended": "参与经历", "none": "未表达", "yes": "是", "no": "否", "individual": "个人自述", "organizer": "组织者自述", "unknown": "未知"}
    states = {"candidate": "明确线索", "related": "相关弱线索", "needs_review": "待复核", "failed": "处理失败", "other": "其他内容",'excluded':'范围不符或排除','scope_unknown':'范围待核对'}
    sections = []
    for group in sorted(groups.values(), key=lambda g: -max((i["result"] or {}).get("score", 0) for i in g)):
        first = group[0]["source"]
        label = "匿名作者分组" if first["identity_mode"] == "upstream_anonymized" else "公开来源作者" if first["author_key"] else "作者未知（不合并昵称）"
        cards = []
        for item in sorted(group, key=lambda i: -(i["result"] or {}).get("score", 0)):
            source, result = item["source"], item["result"]
            state = "needs_review" if item["status"] == "needs_review" else "failed" if not result else "candidate" if result["accepted"] else "related" if result["topic"] == "relevant" else "other"
            if result and item['status']!='needs_review' and result.get('scope_check',{}).get('status') in ('excluded','unknown'):state='excluded' if result['scope_check']['status']=='excluded' else 'scope_unknown'
            dims = ' · '.join(f'{names[k]}：{values.get(result[k],esc(result[k]))}' for k in names) if result else "处理失败，不能算未命中"
            evidence = ''.join(f'<li>{names[d]}（{"上下文主题" if e.get("source_hash") else "本人原文"}）：{esc(e["quote"])} [{e["start"]}:{e["end"]}]' + (f' <a href="#source-{esc(e["source_hash"])}">查看上下文来源</a>' if e.get("source_hash") else '') + '</li>' for d, e in (result or {}).get("evidence", {}).items())
            # A07 fix: validate the source URL against the record's own platform,
            # not the Douyin default — cross-platform links must not be dropped.
            url = public_url(source["url"], source["platform"])
            link = f'<a href="{esc(url)}" rel="noreferrer noopener" target="_blank">打开来源作品</a>' if url else "无可用来源链接"
            review = item["review"]
            cards.append(f'<article id="source-{fingerprint(source)}" data-state="{state}"><p><b>{states[state]}</b> · {"评论" if source["object_type"] == "comment" else "作品"} · 平台 {esc(source["platform"])} · {esc(source["content_id"])}</p><p class="body">{esc(source["text"])}</p><p>{dims}</p><ul>{evidence}</ul><p>{esc((result or {}).get("reason", item["error_code"] or "待处理"))}</p><p>发布时间：{esc(source["published_at"] or source["published_time_text"] or "未知")} · 历史表达不代表当前邀约有效</p><p>上下文缺失：{esc((result or {}).get("context_missing", "未知"))} · {link}</p><p>复核：{esc(review["decision"] or "未复核")} {esc(review["note"] or "")}</p><details><summary>分析标识（命令行复核使用）</summary><code>{esc(item["analysis_key"])}</code></details></article>')
        sections.append(f'<section><h2>{esc(first["author"] or "未提供昵称")} · {label} · {len(group)} 条</h2>{"".join(cards)}</section>')
    synthetic = any(i["source"]["synthetic"] for i in items)
    notice = "含虚构样例，不能作为真实账号结果。" if synthetic else "公开内容分析线索，未确认身份或实际参加意愿。匿名分组不能算可定位账号或确认人数；同一人可能有多个账号。"
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>公开兴趣线索工作台</title><style>body{{font:16px/1.7 system-ui;background:#f4f6f8;color:#20333b}}main{{max-width:1000px;margin:auto;padding:24px}}article{{background:white;border:1px solid #cfd9de;padding:18px;border-radius:10px;margin:12px 0}}.body,code{{white-space:pre-wrap;overflow-wrap:anywhere}}.notice{{background:#fff1c9;padding:16px}}button{{padding:8px 14px;margin:4px}}h2{{font-size:19px}}[hidden]{{display:none}}</style><main><h1>公开兴趣线索 · 多维分析</h1><p class="notice">{notice}</p><p>批次 {esc(job_id)} · 状态 {esc(job['status'])} · 内容 {len(items)} · 明确线索 {candidates} · 相关弱线索 {related} · 失败 {summary['failed_contents']}</p><p>候选公开账号 {counts['candidate_public_accounts']} · 候选匿名分组 {counts['candidate_anonymous_groups']} · 作者未知候选内容 {counts['candidate_unknown_author_contents']}。排序分不是概率。</p><p>模板哈希 {esc(job['policy_hash'])}</p><nav><button data-filter="all">全部</button><button data-filter="candidate">明确线索</button><button data-filter="related">相关弱线索</button><button data-filter="failed">处理失败</button></nav><p>此文件可筛选查看；复核决定通过本机 workflow review 命令保存，重新 export 后显示。</p>{''.join(sections)}</main><script>document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{{document.querySelectorAll('article').forEach(a=>a.hidden=b.dataset.filter!=='all'&&a.dataset.state!==b.dataset.filter);document.querySelectorAll('section').forEach(s=>s.hidden=![...s.querySelectorAll('article')].some(a=>!a.hidden));}}));</script></html>'''
    page = page.replace('</nav>', '<button data-filter="needs_review">待复核</button></nav>')
    if task:
        page=page.replace('</nav>','<button data-filter="excluded">范围不符或排除</button><button data-filter="scope_unknown">范围待核对</button></nav>')
        page=page.replace('<nav>',f'<p>任务目标：{esc(task["description"])} · 配置版本：{esc(job["config_revision"] or "显式文件")}。地域和时间条件为后处理检查，未宣称平台原生过滤。</p><nav>')
    page = page.replace('</script>', "document.querySelectorAll('a[href^=\"#source-\"]').forEach(a=>a.addEventListener('click',()=>{const target=document.querySelector(a.getAttribute('href'));if(target){target.hidden=false;target.closest('section').hidden=false;}}));</script>")
    page = page.replace('<p>候选公开账号', f'<p>模型输出隔离待复核 {summary["needs_review_contents"]} 条；未并入明确线索，也不等于未命中。</p><p>候选公开账号')
    report.write_text(page, encoding="utf-8")
    summary.update(report=str(report.resolve()), export=str(export.resolve()))
    return summary


def review(db_path, key, decision, note):
    if decision not in ("confirm", "reject", "uncertain") or len(note) > 1000:
        raise ValueError("Invalid review decision/note")
    with closing(connect(db_path)) as db, db:
        if not db.execute("SELECT 1 FROM v2_analyses WHERE analysis_key=?", (key,)).fetchone():
            raise ValueError("Analysis not found")
        values = (key, decision, note, utcnow())
        db.execute("INSERT OR REPLACE INTO v2_reviews VALUES (?,?,?,?)", values)
        db.execute("INSERT INTO v2_review_history(analysis_key,decision,note,updated_at) VALUES (?,?,?,?)", values)
    return {"review_saved": True, "analysis_key": key, "decision": decision}


def main():
    parser = argparse.ArgumentParser(description="MediaCrawler 采集与多维分析流水线")
    subs = parser.add_subparsers(dest="command", required=True)
    command = subs.add_parser("run")
    command.add_argument("--input", required=True)
    command.add_argument("--policy")
    command.add_argument('--task-file')
    command.add_argument("--db", default="data/screening.sqlite3")
    command.add_argument("--output", default="output/workflow")
    command.add_argument("--mode", choices=["rules", "ark"])
    command.add_argument("--connection", default="config/connection.json")
    command.add_argument("--max-api-calls", type=int)
    command.add_argument("--resume-job")
    pipe = subs.add_parser("collect-run")
    pipe.add_argument("--collection-config")
    pipe.add_argument("--policy")
    pipe.add_argument('--task-file')
    pipe.add_argument("--mode", choices=["rules", "ark"])
    pipe.add_argument("--max-api-calls", type=int)
    pipe.add_argument("--resume-collection", help="Resume/reuse a collection directory instead of starting a new fetch")
    r = subs.add_parser("review")
    r.add_argument("--analysis-key", required=True)
    r.add_argument("--decision", choices=["confirm", "reject", "uncertain"], required=True)
    r.add_argument("--note", default="")
    r.add_argument("--db", default="data/screening.sqlite3")
    ex = subs.add_parser("export")
    ex.add_argument("--job-id", required=True)
    ex.add_argument("--db", default="data/screening.sqlite3")
    ex.add_argument("--output", default="output/workflow")
    args = parser.parse_args()
    runtime_task=None;config_revision=None
    try:
        if args.command in ('run','collect-run'):
            from .active_config import active_paths
            selected_policy,selected_collection=active_paths()
            default_policy=args.policy is None
            args.policy=args.policy or str(selected_policy)
            if args.command=='collect-run':args.collection_config=args.collection_config or str(selected_collection)
            if args.task_file is None and default_policy and (Path(args.policy).parent/'task.json').exists():args.task_file=str(Path(args.policy).parent/'task.json')
            if args.task_file:
                runtime_task=validate_task(json.loads(Path(args.task_file).read_text(encoding='utf-8-sig')))
                config_revision=Path(args.task_file).parent.name
            args.mode=args.mode or (runtime_task['execution']['mode'] if runtime_task else 'ark' if args.command=='collect-run' else 'rules')
            args.max_api_calls=args.max_api_calls if args.max_api_calls is not None else runtime_task['execution']['max_api_calls'] if runtime_task else 40
            if runtime_task:
                runtime_task['execution'].update(mode=args.mode,max_api_calls=args.max_api_calls,source='collect' if args.command=='collect-run' else 'existing')
        if args.command == "review":
            result = review(args.db, args.analysis_key, args.decision, args.note)
        elif args.command == "export":
            with closing(connect(args.db)) as db:
                result = export_job(db, args.job_id, args.output)
        elif args.command == "collect-run":
            # Check maximum planned volume before issuing any collection/API call.
            settings = json.loads(Path(args.collection_config).read_text(encoding="utf-8-sig"))
            load_policy(args.policy)
            if args.resume_collection:
                checkpoint=Path(args.resume_collection)/'checkpoint.json'
                receipt=json.loads(checkpoint.read_text(encoding='utf-8'))
                if receipt.get('status') not in ('completed','completed_empty') or receipt.get('settings')!=settings:raise ValueError('只复用已完成且配置相同的采集；部分采集不自动续跑')
                raw=Path(receipt['raw_file'])
                if not raw.resolve().is_relative_to((ROOT/'data/collections').resolve()) or hashlib.sha256(raw.read_bytes()).hexdigest()!=receipt.get('raw_sha256'):raise ValueError('采集原文哈希/路径不一致')
            else:
                from .multi_collection import collect_selected
                monitor=ROOT/'data/task-runs'/uuid.uuid4().hex;monitor.mkdir(parents=True)
                receipt=collect_selected(settings,monitor,ROOT)
                if receipt['status'] not in ('completed','completed_empty'):raise ValueError('采集未完整完成；保留各平台检查点，请完成登录或排查失败，不自动分析部分批次')
            result = run(receipt["raw_file"], args.policy, ROOT / "data/screening.sqlite3", ROOT / "output/workflow", args.mode, ROOT / "config/connection.json", args.max_api_calls,task=runtime_task,config_revision=config_revision)
        else:
            result = run(args.input, args.policy, args.db, args.output, args.mode, args.connection, args.max_api_calls, job_id=args.resume_job,task=runtime_task,config_revision=config_revision)
    except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(f"流水线失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status", "completed") in ("completed", "completed_with_review") else 2


if __name__ == "__main__":
    raise SystemExit(main())
