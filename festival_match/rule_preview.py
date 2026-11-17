"""Bounded subprocess worker for untrusted imported regular expressions."""
from datetime import datetime, timezone
import json
import sys
from .normalization import normalize
from .rules_v2 import validate_policy, local_analyze, apply_decision
from .task_contract import apply_scope

if __name__ == "__main__":
    try:
        obj = json.loads(sys.stdin.read())
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
        print(json.dumps(decided, ensure_ascii=False))
    except (ValueError, TypeError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(1)
