"""Per-client routing only. Never mutates proxy environment or OS settings."""
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NetworkError(ValueError):
    pass


def check_url(url, hosts):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in hosts or
            parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise NetworkError("仅允许已配置平台的 HTTPS 公网入口")


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check_url(newurl, self.hosts)
        except ValueError:
            target = urlsplit(newurl)
            raise NetworkError(f"平台跳转已阻断：目标协议 {target.scheme}，主机 {target.hostname}；未放宽入口或切换代理") from None
        if req.has_header("Authorization"):
            raise NetworkError("携带模型凭证的请求不允许重定向")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_opener(mode, hosts):
    if mode not in ("direct", "system"):
        raise ValueError("未知网络模式")
    # An explicit empty mapping bypasses environment AND Windows registry proxies.
    proxy = urllib.request.ProxyHandler({}) if mode == "direct" else urllib.request.ProxyHandler()
    return urllib.request.build_opener(proxy, RestrictedRedirect(hosts))


def request(url, *, hosts, mode, payload=None, key=None, timeout=20, max_bytes=2_000_000):
    from .runtime_context import current_context
    context=current_context()
    if not context.deployment.get("test_isolated"):
        context.require_external('network')
    check_url(url, hosts)
    headers = {"User-Agent": "FestivalMatch/0.1", "Accept": "application/json,text/html"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with make_opener(mode, hosts).open(req, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise NetworkError("响应超过读取上限，已停止；没有切换代理重试")
            return body, {"status": response.status, "content_type": response.headers.get("Content-Type", ""), "final_url": response.url, "proxy_mode": mode}
    except urllib.error.HTTPError as error:
        safe_code = ""
        try:
            detail = json.loads(error.read(8192))
            code = detail.get("error", {}).get("code", "")
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", code):
                safe_code = f"，服务端错误码 {code}"
        except (ValueError, AttributeError, OSError):
            pass
        raise NetworkError(f"远端返回 HTTP {error.code}{safe_code}；未记录响应正文或凭证") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise NetworkError("连接失败或超时；没有切换代理重试") from None
