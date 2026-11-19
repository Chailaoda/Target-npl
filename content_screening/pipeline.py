import argparse
import csv
import hashlib
import html
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_profile(path):
    profile = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if profile.get("schema_version") != 1:
        raise ValueError("模板 schema_version 必须为 1")
    for field in ("id", "name"):
        if not isinstance(profile.get(field), str) or not profile[field].strip():
            raise ValueError(f"模板缺少 {field}")
    if not isinstance(profile.get("topic_patterns"), list) or not profile["topic_patterns"]:
        raise ValueError("模板需要非空 topic_patterns")
    if not isinstance(profile.get("rules"), list) or not profile["rules"]:
        raise ValueError("模板需要非空 rules")
    labels = set()
    patterns = list(profile["topic_patterns"])
    for rule in profile["rules"]:
        if not isinstance(rule.get("label"), str) or rule["label"] in labels:
            raise ValueError("规则 label 必须为唯一字符串")
        labels.add(rule["label"])
        if not isinstance(rule.get("name"), str):
            raise ValueError("规则需要 name")
        if type(rule.get("score")) is not int or not 0 <= rule["score"] <= 100:
            raise ValueError("规则 score 必须为 0 到 100 的整数")
        if not isinstance(rule.get("patterns"), list) or not rule["patterns"]:
            raise ValueError("规则需要非空 patterns")
        patterns.extend(rule["patterns"])
    if not isinstance(profile.get("accepted_labels"), list) or not set(profile["accepted_labels"]) <= labels:
        raise ValueError("accepted_labels 必须引用已定义规则")
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern or len(pattern) > 500:
            raise ValueError("正则必须为长度不超过 500 的非空字符串")
        compiled = re.compile(pattern, re.I)
        if compiled.search(""):
            raise ValueError("正则不得匹配空字符串")
    return profile


def read_records(path):
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        if path.suffix.lower() == ".csv":
            rows = list(csv.DictReader(stream))
        elif path.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in stream if line.strip()]
        else:
            raise ValueError("仅接受 .csv 或 .jsonl 文件")
    normalized = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条必须为对象")
        for key in ("platform", "content_id", "text"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"第 {index} 条缺少非空字符串 {key}")
        if len(row["text"]) > 20000:
            raise ValueError(f"第 {index} 条正文超过 20000 字符")
        item = {key: row[key] for key in ("platform", "content_id", "text")}
        for key in ("author", "author_url", "url", "published_at", "published_time_text", "parent_id", "object_type"):
            value = row.get(key, "")
            if not isinstance(value, str):
                raise ValueError(f"第 {index} 条 {key} 必须为字符串")
            item[key] = value
        item["object_type"] = item["object_type"] or "post"
        item["synthetic"] = row.get("synthetic", False) in (True, "true", "True", "1")
        normalized.append(item)
    return normalized


def classify(record, profile):
    text = record["text"]
    topic = next((m for p in profile["topic_patterns"] if (m := re.search(p, text, re.I))), None)
    result = {"label": "unrelated", "name": "未命中主题", "score": 0, "accepted": False, "evidence": []}
    if topic is None:
        return result
    result.update(label="mention_only", name="仅提及主题，意向不足")
    spans = [(topic.start(), topic.end())]
    for rule in profile["rules"]:
        match = next((m for p in rule["patterns"] if (m := re.search(p, text, re.I))), None)
        if match is not None:
            result.update({key: rule[key] for key in ("label", "name", "score")})
            result["accepted"] = rule["label"] in profile["accepted_labels"]
            spans.append((match.start(), match.end()))
            break
    result["evidence"] = [{"start": a, "end": b, "quote": text[a:b]} for a, b in sorted(set(spans))]
    return result


def connect(path=None, *, readonly=False):
    """Compatibility shim: legacy tables are created by explicit migration."""
    from .db_access import connect as resolve_connect
    return resolve_connect(path, readonly=readonly)


def safe_url(url):
    try:
        parsed = urlsplit(url)
        return url if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password else ""
    except ValueError:
        return ""


def render_report(items, profile, run_id, profile_hash):
    esc = lambda value: html.escape(str(value), quote=True)
    cards = []
    for record, result in sorted(items, key=lambda x: (-x[1]["score"], x[0]["platform"], x[0]["content_id"])):
        link = safe_url(record["url"])
        source = f'<a href="{esc(link)}" rel="noreferrer noopener">打开来源</a>' if link else "无可用来源链接"
        author_link = safe_url(record.get("author_url", ""))
        if author_link:
            source += f' · <a href="{esc(author_link)}" rel="noreferrer noopener">作者主页</a>'
        quotes = " / ".join(esc(e["quote"]) for e in result["evidence"])
        explanation = f'<p>判定依据：{esc(result["reason"])}</p>' if result.get("reason") else ""
        badge = "候选内容" if result["accepted"] else "未纳入候选"
        cards.append(f'<article><div class="meta">{badge} · {esc(result["name"])} · 排序分 {result["score"]}</div><h2>{esc(record["author"] or "未提供作者")} · {esc(record["content_id"])}</h2><p class="body">{esc(record["text"])}</p><p>匹配原句：{quotes or "无"}</p>{explanation}<footer>{esc(record["platform"])} · {source}</footer></article>')
    synthetic = any(record["synthetic"] for record, _ in items)
    accepted = sum(result["accepted"] for _, result in items)
    notice = "演示数据：本报告含人工编写的虚构样例，不代表找到真实用户。" if synthetic else "结果为公开内容中的分析线索，须人工核对，未确认账号身份或真实参加意向。"
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(profile['name'])}</title><style>body{{font:16px/1.7 system-ui,sans-serif;background:#f3f5f7;color:#183039;margin:0}}main{{max-width:900px;margin:auto;padding:32px 18px}}h1{{font-size:28px}}h2{{font-size:18px}}article{{background:white;border:1px solid #dce4e7;border-radius:12px;padding:22px;margin:18px 0}}.notice{{padding:18px;background:#fff2cf;border-radius:10px}}.meta,footer{{color:#526972;font-size:14px}}.body{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#126b73}}code{{overflow-wrap:anywhere}}</style><main><h1>{esc(profile['name'])}</h1><p class="notice">{notice}</p><p>批次 {run_id} · 去重内容 {len(items)} 条 · 候选 {accepted} 条。分数用于规则排序，不是概率。此页面只读，不保存人工复核决定。</p><p class="meta">模板 SHA-256：<code>{profile_hash}</code></p>{''.join(cards)}</main></html>'''


def run(input_path, profile_path, db_path, output_path, evaluator=None, evaluator_identity=None, max_evaluations=None):
    profile = load_profile(profile_path)
    records = read_records(input_path)  # Validate the complete input before touching the DB.
    snapshot = dict(profile, evaluator=evaluator_identity) if evaluator_identity else profile
    profile_hash = fingerprint(snapshot)
    unique = {fingerprint(record): record for record in records}
    cached = {}
    if Path(db_path).exists():
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as existing:
            if existing.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evaluations'").fetchone():
                for source_hash in unique:
                    row = existing.execute("SELECT result_json FROM evaluations WHERE source_hash=? AND profile_hash=?", (source_hash, profile_hash)).fetchone()
                    if row:
                        cached[source_hash] = json.loads(row[0])
    missing_count = len(unique) - len(cached)
    if max_evaluations is not None and (not 1 <= max_evaluations <= 500 or missing_count > max_evaluations):
        raise ValueError("新增内容超过本批模型调用上限，未调用模型；请缩小输入或显式设置 1–500 的上限")
    items = [(record, cached[source_hash] if source_hash in cached else (evaluator or classify)(record, profile)) for source_hash, record in unique.items()]
    accepted = sum(result["accepted"] for _, result in items)
    with closing(connect(db_path)) as db, db:
        db.execute("INSERT OR IGNORE INTO profiles VALUES (?,?)", (profile_hash, json.dumps(snapshot, ensure_ascii=False)))
        for source_hash, (record, result) in zip(unique, items):
            db.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?)", (source_hash, record["platform"], record["content_id"], record["object_type"], json.dumps(record, ensure_ascii=False)))
            db.execute("INSERT OR IGNORE INTO evaluations VALUES (?,?,?)", (source_hash, profile_hash, json.dumps(result, ensure_ascii=False)))
        cursor = db.execute("INSERT INTO runs(created_at,profile_hash,input_count,unique_count,accepted_count) VALUES (?,?,?,?,?)", (datetime.now(timezone.utc).isoformat(), profile_hash, len(records), len(items), accepted))
        run_id = cursor.lastrowid
    output = Path(output_path)
    output.mkdir(parents=True, exist_ok=True)
    report = output / f"report-{run_id}.html"
    report.write_text(render_report(items, profile, run_id, profile_hash), encoding="utf-8")
    export = output / f"results-{run_id}.jsonl"
    export.write_text("".join(json.dumps({"source": record, "result": result, "profile_hash": profile_hash}, ensure_ascii=False) + "\n" for record, result in items), encoding="utf-8")
    return {"run_id": run_id, "input_count": len(records), "unique_count": len(items), "accepted_count": accepted, "reused_count": len(cached), "new_evaluation_count": missing_count, "profile_hash": profile_hash, "report": str(report.resolve()), "export": str(export.resolve())}


def main():
    parser = argparse.ArgumentParser(description="示例活动公开内容导入、火山视频直连入口、方舟模型分类")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("run", help="导入并输出带原文证据的报告")
    command.add_argument("--input", required=True)
    command.add_argument("--profile", default="profiles/legacy.json")
    command.add_argument("--db", default=None)
    command.add_argument("--output", default="output")
    command.add_argument("--model-mode", choices=["rules", "ark"], default="rules")
    command.add_argument("--connection", default="config/connection.json")
    command.add_argument("--max-api-calls", type=int, default=20)
    fetch = sub.add_parser("fetch-huoshan", help="直连读取单条公开作品分享页；不自动搜索或抓评论")
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--output", default="data/huoshan-public.jsonl")
    douyin = sub.add_parser("fetch-douyin", help="直连读取单条抖音公开作品元数据；不绕过验证或自动抓评论")
    douyin.add_argument("--url", required=True)
    douyin.add_argument("--output", default="data/douyin-public.jsonl")
    sub.add_parser("probe-huoshan", help="直连探测平台官网；不是业务采集成功验证")
    setup = sub.add_parser("configure-ark", help="在本机交互输入密钥并存入项目专用 Windows 凭证")
    setup.add_argument("--connection", default="config/connection.json")
    args = parser.parse_args()
    try:
        if args.command == "configure-ark":
            from .runtime_context import current_context
            context=current_context()
            args.connection=str(context.state_path('config/connection.json'))
            import getpass
            from .connectors import load_connection
            from .credentials import save_key
            if not sys.stdin.isatty():
                raise ValueError("请在本机交互终端运行配置命令；不接受管道或命令参数传入密钥")
            config = load_connection(args.connection)
            save_key(config["ark"]["credential_target"], getpass.getpass("方舟 API Key（输入不显示）："))
            result = {"credential_saved": True, "target": config["ark"]["credential_target"]}
        elif args.command == "probe-huoshan":
            from .connectors import HUOSHAN_HOSTS
            from .network import request
            _, result = request("https://www.huoshan.com/", hosts=HUOSHAN_HOSTS, mode="direct")
            result["business_data_verified"] = False
        elif args.command in ("fetch-huoshan", "fetch-douyin"):
            from .connectors import fetch_public_video
            result = fetch_public_video(args.url, args.output, "douyin" if args.command == "fetch-douyin" else "huoshan")
        else:
            from .runtime_context import current_context,set_context
            context=current_context();set_context(context)
            from .db_access import resolve_db
            args.db=args.db or str(resolve_db(context))
            args.output=str(context.state_path(args.output))
            args.profile=str(context.state_path(args.profile)) if args.profile=='profiles/default.json' else args.profile
            args.connection=str(context.state_path(args.connection))
            evaluator = identity = None
            if args.model_mode == "ark":
                from .connectors import ark_evaluator, load_connection
                config = load_connection(args.connection)
                actual_evaluator = None
                def evaluator(record, profile):
                    nonlocal actual_evaluator
                    if actual_evaluator is None:
                        actual_evaluator = ark_evaluator(config, profile)
                    return actual_evaluator(record, profile)
                identity = {"provider": "volcengine_ark", "model": config["ark"]["model"], "endpoint": config["ark"]["base_url"], "adapter_version": "2"}
            result = run(args.input, args.profile, args.db, args.output, evaluator, identity, args.max_api_calls if args.model_mode == "ark" else None)
    except (ValueError, OSError, sqlite3.Error, re.error) as error:
        print(f"运行失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
