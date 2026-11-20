"""Bounded worker for untrusted imported regular expressions.

Two modes:
  * one-shot (default): read one JSON object from stdin, print one result line.
  * --daemon: read one JSON object per line and answer per line, so the control
    server stops paying a 300-600ms interpreter start per preview request.

Regex evaluation stays in a separate process on purpose: a catastrophic
backtracking pattern is killed by the caller instead of freezing the UI.
"""
from datetime import datetime, timezone
import json
import sys

from .normalization import normalize
from .rules_v2 import validate_policy, local_analyze, apply_decision
from .task_contract import apply_scope


def evaluate(obj):
    """Pure evaluation of one preview payload; raises ValueError on bad input."""
    policy = validate_policy(obj["policy"])
    text = obj["text"]
    if not isinstance(text, str) or not 1 <= len(text) <= 4000:
        raise ValueError("测试文本长度应为 1–4000 字符")
    context_text = obj.get("context_text")
    context = None
    if isinstance(context_text, str) and context_text.strip():
        context = [{"text": context_text, "source_hash": "preview_ctx"}]
    record = normalize({"platform": "douyin", "object_type": "post", "content_id": "preview", "text": text, "synthetic": True, "published_at": datetime.now(timezone.utc).isoformat()})
    base = local_analyze(record, policy, context=context)
    decided = apply_decision(base, record, policy)
    task = obj.get("task")
    if task:
        ref_time = datetime.now(timezone.utc).isoformat()
        decided = apply_scope(decided, record, context or [], task, ref_time)
    return decided


def _answer(raw):
    try:
        return json.dumps(evaluate(json.loads(raw)), ensure_ascii=False), 0
    except (ValueError, TypeError, KeyError) as error:
        return json.dumps({"error": str(error)}, ensure_ascii=False), 1
    except Exception:  # noqa: BLE001 - never leak a traceback into the pipe
        return json.dumps({"error": "规则预览失败"}, ensure_ascii=False), 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--daemon" in argv:
        for line in sys.stdin:
            if not line.strip():
                continue
            payload, _ = _answer(line)
            print(payload, flush=True)
        return 0
    payload, code = _answer(sys.stdin.read())
    print(payload)
    return code


if __name__ == "__main__":
    sys.exit(main())
