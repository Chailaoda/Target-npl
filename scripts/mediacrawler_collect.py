"""Isolated adapter for the pinned MediaCrawler Douyin client and stores.

Use vendor/MediaCrawler/.venv. No upstream source edits, automatic login,
browser cleanup, proxy fallback, cookie export, profile scraping or media downloads.
"""
import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import socket
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
PINNED_COMMIT = "68d9ef1c1da22eab75940dfd7cec259907006538"
# Target NPL 受控 fork 分支（feature/public-account-profiles，WP1）。
# 相对上游 68d9ef1 为纯增量公开账号字段透传，请求限制/登录控制不变。
FORK_COMMIT = "c15e46b15f32e5257ae139f59390fa4efa42ecba"
# 采集适配层声明兼容的受控依赖提交集合；完整 diff 校验保证运行树未被
# 未记录地改动（禁止通过改白名单来掩盖变更，新版本必须显式登记并评审）。
ALLOWED_UPSTREAM_COMMITS = (PINNED_COMMIT, FORK_COMMIT)
# 公开账号字段透传开关：fork 提交下默认启用；上游原版不透传公开账号。
def _public_accounts_flag():
    return "1" if os.environ.get("TARGET_NPL_PUBLIC_ACCOUNTS", "auto") in ("1", "auto") else ""


def _is_pycache_entry(status_line):
    """True only when the changed path lives INSIDE a __pycache__ directory.

    Matches by path COMPONENTS (PurePosixPath.parts), so a file merely named
    "__pycache__-notes.md" or "evil__pycache__.py" is NOT tolerated - only
    real bytecode cache directories (e.g. "tools/__pycache__/x.pyc") are.
    """
    from pathlib import PurePosixPath
    raw = status_line[3:].strip()
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    # git quotes non-ascii/special chars with C-style escapes
    raw = raw.encode().decode("unicode_escape") if "\\u" in raw or "\\x" in raw else raw
    return "__pycache__" in PurePosixPath(raw.replace("\\\\", "\\")).parts


def verify_upstream(upstream, expected=None):
    """检查 vendor 目录 HEAD 在兼容提交白名单内且工作树干净。

    完整性检查使用 git status --porcelain --untracked-files=all：同时覆盖
    已暂存修改、未暂存修改与未跟踪文件（git diff --name-only 会漏掉后两者）。
    唯一允许的例外是位于 __pycache__ 目录内的 Python 运行时产物
    （按路径组成部分精确判断，文件名伪装不算）。
    """
    sha = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    allowed = ALLOWED_UPSTREAM_COMMITS if expected is None else (expected,)
    if sha not in allowed:
        raise ValueError("Pinned upstream changed; review adapter compatibility first")
    status = subprocess.check_output(
        ["git", "-C", str(upstream), "status", "--porcelain", "--untracked-files=all"],
        text=True).splitlines()
    dirty = [line for line in status if line and not _is_pycache_entry(line)]
    if dirty:
        raise ValueError("Pinned upstream working tree is dirty; review adapter compatibility first")
    return sha


def publish_result(result, receipt=None):
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    (ROOT / "output").mkdir(parents=True, exist_ok=True)
    if receipt:
        receipt = Path(receipt).resolve()
        if not receipt.is_relative_to(ROOT / "output"):
            raise ValueError("Receipt must stay in project output")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.open("x", encoding="utf-8") as stream:
            stream.write(payload)
    (ROOT / "output/mediacrawler-latest.json").write_text(payload, encoding="utf-8")
    print(payload)


def load_settings(file):
    c = json.loads(Path(file).read_text(encoding="utf-8-sig"))
    return validate_settings(c)


def validate_settings(c):
    expected = {"schema_version","platform","keywords","pages_per_keyword","posts_per_keyword","max_posts","comments_per_post","request_interval_seconds","max_requests","timeout_seconds","proxy_mode"}
    if not isinstance(c,dict) or (set(c)-{"platforms"})!=expected:
        raise ValueError("采集配置字段缺失或包含不支持的字段")
    if c.get("schema_version") != 1 or c.get("platform") not in ("douyin","xhs","kuaishou","bilibili","weibo","tieba","zhihu") or c.get("proxy_mode") != "direct":
        raise ValueError("Invalid source configuration")
    if "platforms" in c:
        platforms=c["platforms"]
        if not isinstance(platforms,list) or not 1<=len(platforms)<=7 or any(p not in ("douyin","xhs","kuaishou","bilibili","weibo","tieba","zhihu") for p in platforms) or len(set(platforms))!=len(platforms) or c["platform"] not in platforms:
            raise ValueError("Invalid platform selection")
    keys = c.get("keywords")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 20 or any(not isinstance(k, str) or not k.strip() or len(k) > 80 for k in keys):
        raise ValueError("Invalid keywords")
    for name, low, high in (("pages_per_keyword", 1, 20), ("posts_per_keyword", 1, 100), ("max_posts", 1, 500),
                            ("comments_per_post", 0, 100), ("max_requests", 1, 500), ("timeout_seconds", 20, 1800), ("request_interval_seconds", 1, 30)):
        if type(c.get(name)) is not int or not low <= c[name] <= high:
            raise ValueError("Invalid collection limit: " + name)
    return c


def validate_session(session,platform="douyin"):
    from content_screening.platforms import PLATFORMS
    if platform not in PLATFORMS:raise ValueError("Invalid platform")
    if (Path(session["profile_path"]).resolve() != ROOT / f"cache/{platform}-browser" or session["debug_host"] != "127.0.0.1"
            or session["proxy_mode"] != "direct" or type(session["debug_port"]) is not int or not 1024 <= session["debug_port"] <= 65535):
        raise ValueError("Dedicated browser metadata mismatch")
def verify_browser(session,platform="douyin"):
    validate_session(session,platform)
    pid = session["pid"]
    if type(pid) is not int or pid < 1:
        raise ValueError("Invalid browser PID")
    # Read only this process's routing flags. Never return its full command line.
    command = f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; if ($p -and $p.Name -eq 'chrome.exe' -and $p.CommandLine.Contains('--user-data-dir={ROOT / f'cache/{platform}-browser'}') -and $p.CommandLine.Contains('--no-proxy-server') -and $p.CommandLine.Contains('--remote-debugging-port={session['debug_port']}') -and $p.CommandLine.Contains('--remote-debugging-address=127.0.0.1')) {{'verified'}}"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, timeout=15)
    if result.returncode != 0 or result.stdout.strip() != b"verified":
        raise ValueError("Dedicated direct browser process not verified")


def ensure_browser(session,platform="douyin",visible=False):
    """Start or reuse the dedicated browser. Hidden by default; visible only for user action."""
    from content_screening.platforms import PLATFORMS
    from content_screening import browser_window
    validate_session(session,platform)
    try:
        verify_browser(session,platform)
        if visible:browser_window.reveal(session["pid"])
        else:browser_window.hide(session["pid"])
        return
    except ValueError:
        pass
    # A closed dedicated browser can be reopened; an occupied endpoint must not
    # be silently attached or replaced.
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", session["debug_port"])) == 0:
            raise ValueError("Unverified occupied browser endpoint")
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    flags = [str(chrome), f"--user-data-dir={ROOT / f'cache/{platform}-browser'}", "--profile-directory=Default", "--no-proxy-server",
             "--disable-sync", "--no-first-run", "--no-default-browser-check", "--autoplay-policy=user-gesture-required",
             # Hidden windows must keep full speed: no timer/renderer/occlusion throttling.
             "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
             "--disable-features=CalculateNativeWinOcclusion",
             "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={session['debug_port']}", "--new-window"]
    startup = None
    if not visible:
        flags += ["--window-position=-32000,-32000", "--window-size=1366,900"]
        if os.name == "nt":
            startup = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=0)
    process = subprocess.Popen(flags + [PLATFORMS[platform]["home"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startup)
    session["pid"] = process.pid
    if not visible:browser_window.hide(process.pid, wait=3)
    for _ in range(30):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", session["debug_port"])) == 0:
                verify_browser(session,platform)
                (ROOT / f"cache/{platform}-browser-session.json").write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
                if visible:browser_window.reveal(process.pid, wait=5)
                else:browser_window.hide(process.pid, wait=5)
                return
        time.sleep(0.5)
    raise ValueError("Dedicated browser did not start")


# Failures the user can resolve in the browser itself (login, captcha, risk check).
NEEDS_USER_CODES = {"LOGIN_REQUIRED", "PLATFORM_RESTRICTION", "NON_JSON_OR_VERIFICATION", "PLATFORM_STATUS_ERROR", "HTTP_403", "HTTP_461", "HTTP_471"}


async def release_browser(browser, page, pid, keep):
    """Reveal the window when the user must act; otherwise close the hidden browser gracefully."""
    from content_screening import browser_window
    if keep:
        try:
            if page:await page.bring_to_front()
        except Exception:
            pass
        browser_window.reveal(pid)
        return
    try:
        if page:await page.close()
        # Browser.close flushes cookies to the profile, unlike killing the process.
        cdp = await browser.new_browser_cdp_session()
        await cdp.send("Browser.close")
    except Exception:
        pass


def make_direct_client(**kwargs):
    if kwargs.pop("proxy", None):
        raise ValueError("Proxy rejected")
    import httpx
    kwargs.update(trust_env=False, verify=True, follow_redirects=False)
    return httpx.AsyncClient(**kwargs)


async def collect(settings, job, resume=False):
    import httpx
    from playwright.async_api import async_playwright
    import config
    from tools import httpx_util
    # Override only the adapter process's client factory, before platform imports.
    direct_client = make_direct_client
    httpx_util.make_async_client = direct_client
    config.ENABLE_IP_PROXY = False
    config.ENABLE_GET_MEDIA = False
    config.ENABLE_GET_SUB_COMMENTS = False
    config.AUTO_CLOSE_BROWSER = False
    from media_platform.douyin.core import DouYinCrawler
    from store import douyin as store
    from var import source_keyword_var
    # Silence upstream raw response/body logs, including unexpected errors.
    logging.disable(logging.CRITICAL)
    session_path = ROOT / "cache/douyin-browser-session.json"
    if not session_path.exists():
        return {"status": "needs_user_login", "records": 0, "posts": 0, "comments": 0, "requests": 0, "settings": settings,
                "raw_file": str(job / "raw.jsonl"), "errors": [{"stage": "login", "code": "LOGIN_NOT_PREPARED"}]}
    session = json.loads(session_path.read_text(encoding="utf-8-sig"))
    ensure_browser(session)
    checkpoint = job / "checkpoint.json"
    raw_path = job / "raw.jsonl"
    if not resume:
        raw_path.touch(exist_ok=False)
    state = json.loads(checkpoint.read_text(encoding="utf-8")) if resume else {"status": "running", "adapter_version": "1", "upstream_commit": PINNED_COMMIT, "settings": settings, "pages_completed": [], "comments_done": [], "records": 0,
             "posts": 0, "comments": 0, "requests": 0, "errors": [], "proxy_mode": "direct", "cookies_exported": False,
             "identity_mode": "upstream_anonymized", "raw_file": str(raw_path), "created_at_utc": datetime.now(timezone.utc).isoformat()}
    if state["settings"] != settings or state["upstream_commit"] not in ALLOWED_UPSTREAM_COMMITS or state.get("adapter_version") != "1":
        raise ValueError("Collection resume configuration/version mismatch")
    state.update(status="running", errors=[])
    state.pop("finished_at_utc", None)
    initial_requests = state["requests"]
    def save():
        temp = checkpoint.with_suffix('.tmp')
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(checkpoint)
    seen_posts, seen_comments, post_keywords = set(), set(), {}
    if resume:
        raw_rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in raw_rows:
            if row["object_type"] == "post":
                post_id = str(row["data"]["aweme_id"])
                seen_posts.add(post_id); post_keywords[post_id] = row["keyword"]
            else:
                seen_comments.add(str(row["data"]["comment_id"]))
        state.update(records=len(raw_rows), posts=len(seen_posts), comments=len(seen_comments))
    class Sink:
        async def store_content(self, content_item):
            self.write("post", content_item)
        async def store_comment(self, comment_item):
            self.write("comment", comment_item)
        def write(self, kind, item):
            with raw_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"object_type": kind, "keyword": source_keyword_var.get(), "data": item}, ensure_ascii=False) + "\n")
            state["records"] += 1
            save()
    sink = Sink()
    store.DouyinStoreFactory.create_store = staticmethod(lambda: sink)
    save()
    last_request = 0.0
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{session['debug_port']}", timeout=10000)
            if len(browser.contexts) != 1:
                raise ValueError("Ambiguous dedicated browser context")
            context = browser.contexts[0]
            page = await context.new_page()  # Close only this adapter-created tab.
            from content_screening import browser_window
            browser_window.hide(session["pid"])
            keep = False
            try:
                await page.goto("https://www.douyin.com/jingxuan", wait_until="domcontentloaded", timeout=25000)
                crawler = DouYinCrawler()
                crawler.context_page, crawler.browser_context = page, context
                client = await crawler.create_douyin_client(None)
                if not await client.pong(context):
                    state["status"] = "needs_user_login"
                    state["errors"].append({"stage": "login", "code": "LOGIN_REQUIRED"})
                    # Keep the created public page open and show it so the user can log in.
                    keep = True
                    return state
                async def safe_request(method, url, **kwargs):
                    nonlocal last_request
                    from urllib.parse import urlsplit
                    u = urlsplit(url)
                    if method != "GET" or u.scheme != "https" or u.netloc != "www.douyin.com":
                        raise ValueError("Request host/method rejected")
                    if state["requests"] >= settings["max_requests"]:
                        raise ValueError("REQUEST_LIMIT")
                    await asyncio.sleep(max(0, settings["request_interval_seconds"] - (time.monotonic() - last_request)))
                    last_request = time.monotonic()
                    state["requests"] += 1
                    save()
                    async with direct_client(timeout=20) as hc:
                        response = await hc.request(method, url, **kwargs)
                    if response.status_code != 200:
                        raise ValueError(f"HTTP_{response.status_code}")
                    if len(response.content) > 5_000_000:
                        raise ValueError("RESPONSE_TOO_LARGE")
                    try:
                        obj = response.json()
                    except ValueError:
                        raise ValueError("NON_JSON_OR_VERIFICATION") from None
                    if not isinstance(obj, dict) or obj.get("status_code", 0) != 0:
                        raise ValueError("PLATFORM_STATUS_ERROR")
                    return obj
                client.request = safe_request
                async def fetch_comments(post_id):
                    if not settings["comments_per_post"] or post_id in state["comments_done"]:
                        return
                    comments = await client.get_aweme_comments(post_id)
                    if not isinstance(comments.get("comments"), list):
                        raise ValueError("COMMENT_DATA_MISSING")
                    for comment in comments["comments"][:settings["comments_per_post"]]:
                        cid = str(comment.get("cid", ""))
                        if cid.isdigit() and cid not in seen_comments and str(comment.get("aweme_id")) == post_id:
                            await store.update_dy_aweme_comment(post_id, comment)
                            seen_comments.add(cid)
                    state["comments"] = len(seen_comments)
                    state["comments_done"].append(post_id)
                    save()
                for post_id in sorted(seen_posts):
                    source_keyword_var.set(post_keywords[post_id])
                    await fetch_comments(post_id)
                for keyword in settings["keywords"]:
                    source_keyword_var.set(keyword)
                    search_id, count = "", sum(k == keyword for k in post_keywords.values())
                    for page_index in range(settings["pages_per_keyword"]):
                        completed_page = next((p for p in state["pages_completed"] if p["keyword"] == keyword and p["page_index"] == page_index), None)
                        if completed_page:
                            search_id = completed_page.get("next_search_id", "")
                            continue
                        if count >= settings["posts_per_keyword"] or len(seen_posts) >= settings["max_posts"]:
                            break
                        response = await client.search_info_by_keyword(keyword, offset=page_index * 15, search_id=search_id)
                        if not isinstance(response.get("data"), list):
                            raise ValueError("SEARCH_DATA_MISSING")
                        for item in response["data"]:
                            post = item.get("aweme_info")
                            if not post:
                                mix = (item.get("aweme_mix_info") or {}).get("mix_items") or []
                                post = mix[0] if mix else None
                            if not isinstance(post, dict) or not str(post.get("aweme_id", "")).isdigit():
                                continue
                            post_id = str(post["aweme_id"])
                            if post_id in seen_posts:
                                # Retain keyword provenance even for duplicate content.
                                await store.update_douyin_aweme(post)
                                continue
                            if count >= settings["posts_per_keyword"] or len(seen_posts) >= settings["max_posts"]:
                                break
                            await store.update_douyin_aweme(post)
                            seen_posts.add(post_id); count += 1
                            post_keywords[post_id] = keyword
                            state["posts"] = len(seen_posts)
                            await fetch_comments(post_id)
                        state["pages_completed"].append({"keyword": keyword, "page_index": page_index, "next_search_id": response.get("extra", {}).get("logid", "")})
                        save()
                        print(f"采集进度：已完成 {len(state['pages_completed'])} 页，作品 {state['posts']}，评论 {state['comments']}", flush=True)
                        search_id = response.get("extra", {}).get("logid", "")
                        if not response["data"] or not response.get("has_more", False):
                            break
                state["status"] = "completed" if state["posts"] else "completed_empty"
            except Exception as error:
                keep = str(error) in NEEDS_USER_CODES
                raise
            finally:
                await release_browser(browser, page, session["pid"], keep)
    except Exception as error:
        safe = str(error)
        allowed = {"REQUEST_LIMIT", "RESPONSE_TOO_LARGE", "NON_JSON_OR_VERIFICATION", "PLATFORM_STATUS_ERROR", "SEARCH_DATA_MISSING", "COMMENT_DATA_MISSING"}
        code = safe if safe in allowed or safe.startswith("HTTP_") and safe[5:].isdigit() else type(error).__name__
        state["status"] = "partial" if state["records"] else "failed"
        state["errors"].append({"stage": "collection", "code": code})
    finally:
        state["new_requests"] = state["requests"] - initial_requests
        state["raw_sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        state["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save()
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--resume-job", help="Existing collection directory under data/collections")
    parser.add_argument("--receipt", help="New immutable per-invocation output receipt")
    parser.add_argument("--progress-file")
    args = parser.parse_args()
    if args.config is None:
        sys.path.insert(0,str(ROOT))
        from content_screening.active_config import active_paths
        args.config=str(active_paths()[1])
    settings = load_settings(args.config)
    if settings["platform"]!="douyin":raise ValueError("Use platform_collect.py for non-Douyin source")
    if args.receipt:
        receipt = Path(args.receipt).resolve()
        if not receipt.is_relative_to(ROOT / "output") or receipt.exists():
            raise ValueError("Invalid/existing receipt path")
    upstream = ROOT / "vendor/MediaCrawler"
    sha = verify_upstream(upstream)
    # 公开账号字段透传的启用条件（三层，评审要求明确化）：
    # 1) 外部显式设置 TARGET_NPL_PUBLIC_ACCOUNTS=0 -> 强制关闭（尊重操作者意愿）
    # 2) 外部显式设置 =1 -> 开启（且仅当运行的是受控 fork 时实际生效）
    # 3) 未设置（默认 auto）-> 检测到受控 fork 提交时自动开启，上游原版保持关闭
    # 依据：third_party/mediacrawler.json 中登记的字段授权范围仅覆盖受控 fork。
    override = os.environ.get("TARGET_NPL_PUBLIC_ACCOUNTS")
    if override == "0":
        os.environ["TARGET_NPL_PUBLIC_ACCOUNTS"] = ""
    else:
        os.environ["TARGET_NPL_PUBLIC_ACCOUNTS"] = "1" if sha == FORK_COMMIT else ""
    job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    job = Path(args.resume_job).resolve() if args.resume_job else ROOT / "data/collections" / job_id
    if not job.is_relative_to(ROOT / "data/collections") or job == ROOT / "data/collections":
        raise ValueError("Invalid collection directory")
    if not args.resume_job:
        job.mkdir(parents=True, exist_ok=False)
    else:
        previous = json.loads((job / "checkpoint.json").read_text(encoding="utf-8"))
        if previous["settings"] != settings or previous.get("adapter_version") != "1":
            raise ValueError("Resume settings/version mismatch")
        if previous.get("raw_sha256") and hashlib.sha256((job / "raw.jsonl").read_bytes()).hexdigest() != previous["raw_sha256"]:
            raise ValueError("Raw collection file changed; resume refused")
        if previous["status"] in ("completed", "completed_empty"):
            previous["raw_sha256"] = hashlib.sha256((job / "raw.jsonl").read_bytes()).hexdigest()
            previous.update(new_requests=0, job_directory=str(job), reused_completed_collection=True)
            temporary = job / "checkpoint.tmp"
            temporary.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(job / "checkpoint.json")
            publish_result(previous, args.receipt)
            return 0
    if args.progress_file:
        progress = Path(args.progress_file).resolve()
        if not progress.is_relative_to(ROOT / "data/task-runs") or progress.name != "collection-progress.json":
            raise ValueError("Invalid progress path")
        temporary = progress.with_suffix(".tmp")
        temporary.write_text(json.dumps({"dataset_id": job.name}), encoding="utf-8")
        temporary.replace(progress)
    sys.path.insert(0, str(upstream))
    os.chdir(upstream)
    try:
        result = asyncio.run(asyncio.wait_for(collect(settings, job, bool(args.resume_job)), settings["timeout_seconds"]))
    except TimeoutError:
        result = json.loads((job / "checkpoint.json").read_text(encoding="utf-8"))
        result["status"] = "partial" if result["records"] else "failed"
        result["errors"].append({"stage": "collection", "code": "TIME_LIMIT"})
        (job / "checkpoint.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as error:
        result = {"status": "failed", "records": 0, "errors": [{"stage": "setup", "code": type(error).__name__}], "job_directory": str(job)}
        (job / "setup-error.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["job_directory"] = str(job)
    publish_result(result, args.receipt)
    return 0 if result["status"] in ("completed", "completed_empty") else 2


if __name__ == "__main__":
    raise SystemExit(main())
