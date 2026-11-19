"""Versioned projection of upstream exports; raw files are never overwritten."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

VERSION = "1"


def text_id(value):
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def public_url(value,platform="douyin"):
    from .platforms import PLATFORMS
    if platform not in PLATFORMS:return ""
    if not isinstance(value, str):
        return ""
    try:
        u = urlsplit(value)
        if u.scheme == "https" and u.hostname in PLATFORMS[platform]["hosts"] and not u.username and not u.password and u.port in (None, 443):
            return u.scheme + "://" + u.netloc + u.path
    except ValueError:
        pass
    return ""


def normalize(row):
    if not isinstance(row, dict):
        raise ValueError("Input row must be an object")
    source = row.get("data", row)
    if not isinstance(source, dict):
        raise ValueError("Invalid upstream object")
    platform=row.get("platform","douyin")
    from .platforms import PLATFORMS
    if platform not in PLATFORMS:raise ValueError("Unsupported source platform")
    upstream = "aweme_id" in source
    kind = row.get("object_type", source.get("object_type", "comment" if "comment_id" in source else "post"))
    if kind not in ("post", "comment"):
        raise ValueError("Unsupported object type")
    if upstream:
        cid = text_id(source.get("comment_id" if kind == "comment" else "aweme_id"))
        text = source.get("content" if kind == "comment" else "desc", source.get("title", ""))
        root = text_id(source.get("aweme_id")) if kind == "comment" else ""
        parent = text_id(source.get("parent_comment_id")) if kind == "comment" else ""
        parent = "" if parent == "0" else parent
        author_key = "anonymous:" + source["creator_hash"] if source.get("creator_hash") else ""
        author, author_url = source.get("nickname", ""), ""
        url = public_url(source.get("aweme_url", "")) or (f"https://www.douyin.com/video/{root or cid}" if (root or cid).isdigit() else "")
        published_text = text_id(source.get("create_time"))
        published_at = ""
        if published_text.isdigit() and 0 < int(published_text) < 4_102_444_800:
            published_at = datetime.fromtimestamp(int(published_text), timezone.utc).isoformat()
        identity_mode = "upstream_anonymized"
    else:
        cid = text_id(source.get("content_id"))
        text = source.get("text")
        root, parent = text_id(source.get("root_id")), text_id(source.get("parent_id"))
        author_url = public_url(source.get("author_url", ""),platform)
        author_key = text_id(source.get("author_id")) or author_url
        author = source.get("author", "")
        url = public_url(source.get("url", ""),platform)
        published_at, published_text = source.get("published_at", ""), source.get("published_time_text", "")
        identity_mode = "public_source" if author_key else "unknown"
        if source.get("anonymized") is True:
            author_key="anonymous:"+source["creator_hash"] if source.get("creator_hash") else ""
            author_url="";author="";identity_mode="upstream_anonymized"
    if not cid or not isinstance(text, str) or len(text) > 20000:
        raise ValueError("Missing ID or valid original text")
    if not all(isinstance(v, str) for v in (author, published_at, published_text)):
        raise ValueError("Invalid source field type")
    return {"platform": platform, "object_type": kind, "content_id": cid, "text": text, "author": author,
            "author_key": author_key, "author_url": author_url, "identity_mode": identity_mode, "url": url,
            "root_id": root, "parent_id": parent, "published_at": published_at, "published_time_text": published_text,
            "normalization_version": VERSION, "text_status": "available" if text.strip() else "empty_text_no_ocr", "synthetic": source.get("synthetic") is True}


def read_export(file):
    path = Path(file)
    # Fail the import before any model call if a required input record is malformed.
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    result = []
    for index, row in enumerate(rows, 1):
        record = normalize(row)
        result.append({"record": record, "keyword": row.get("keyword", ""), "line": index,
                       "raw_hash": hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()})
    return result
