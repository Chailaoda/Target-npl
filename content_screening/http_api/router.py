"""Declarative route table with one auth/body pipeline.

Before this, 25+ routes lived in nested if/elif blocks inside do_GET/do_POST
and every new POST had to remember the token check by hand. Routes are now
registered once, matched by pattern, and every POST body is validated against
a declared key set before the handler runs.
"""
import re


class Route:
    __slots__ = ("method", "pattern", "regex", "handler", "schema")

    def __init__(self, method, pattern, handler, schema=None):
        self.method = method
        self.pattern = pattern
        self.regex = _compile(pattern)
        self.handler = handler
        self.schema = schema


def _compile(pattern):
    parts = []
    for segment in pattern.strip("/").split("/"):
        if segment.startswith("<") and segment.endswith(">"):
            parts.append(f"(?P<{segment[1:-1]}>[^/]+)")
        else:
            parts.append(re.escape(segment))
    return re.compile("/" + "/".join(parts))


class Router:
    def __init__(self):
        self.routes = []
        self._by_method = {"GET": [], "POST": []}

    def get(self, pattern, handler, schema=None):
        return self._add("GET", pattern, handler, schema)

    def post(self, pattern, handler, schema=None):
        return self._add("POST", pattern, handler, schema)

    def _add(self, method, pattern, handler, schema):
        route = Route(method, pattern, handler, schema)
        self.routes.append(route)
        self._by_method[method].append(route)
        return route

    def resolve(self, method, path):
        """Return (handler, path params, schema) or (None, {}, None)."""
        for route in self._by_method.get(method, ()):
            match = route.regex.fullmatch(path)
            if match:
                return route.handler, match.groupdict(), route.schema
        return None, {}, None


def required_keys(schema):
    return set(schema.get("required", ()))


def optional_keys(schema):
    return set(schema.get("optional", ()))


def validate_body(schema, body):
    """Reject unknown keys and missing required ones before any handler runs."""
    if schema is None:
        return
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    allowed = required_keys(schema) | optional_keys(schema)
    if set(body) - allowed:
        raise ValueError("请求包含未定义字段")
    if not required_keys(schema) <= set(body):
        raise ValueError("请求缺少必填字段")
