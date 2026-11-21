"""Opaque per-source cursors for the live monitor.

The monitor reads several append-only ``raw.jsonl`` files that grow
independently. A single global row counter cannot describe that shape: when an
earlier file gains a row, every later file shifts by one and the "next N rows"
slice silently skips content. The cursor is therefore a small, versioned map of
relative source path -> (byte offset, parsed row count).

Only complete newline-terminated records are consumed. A trailing partial write
stays outside the cursor and is retried on the next poll.
"""
from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 16_384


class CursorStale(ValueError):
    """The source was truncated or replaced; the caller must resend a snapshot."""


def encode_cursor(sources, candidates):
    """Encode {relative_path: (offset, rows)} plus the candidate count."""
    payload = {
        "v": CURSOR_VERSION,
        "s": {name: [int(offset), int(rows)] for name, (offset, rows) in sorted(sources.items())},
        "c": int(candidates),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token):
    """Return ({path: (offset, rows)}, candidates); ValueError when malformed."""
    if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_LENGTH:
        raise ValueError("增量游标无效")
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
    except (binascii.Error, UnicodeError, ValueError, TypeError) as error:
        raise ValueError("增量游标无效") from error
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise ValueError("增量游标版本不支持")
    sources = payload.get("s")
    candidates = payload.get("c")
    if not isinstance(sources, dict) or type(candidates) is not int or candidates < 0:
        raise ValueError("增量游标无效")
    decoded = {}
    for name, value in sources.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 500:
            raise ValueError("增量游标路径无效")
        if (not isinstance(value, list) or len(value) != 2
                or any(type(item) is not int or item < 0 for item in value)):
            raise ValueError("增量游标位置无效")
        decoded[name] = (value[0], value[1])
    return decoded, candidates


def read_source(path, offset, parse_row):
    """Read complete records from *offset* (or from the start when None).

    ``parse_row`` receives a decoded JSON object and returns the public capture
    item, or None to ignore the line. Malformed/partial lines never advance into
    a row count; their bytes do advance the offset only when newline-terminated,
    so permanently malformed lines are not reparsed forever.
    """
    path = Path(path)
    data = path.read_bytes()
    start = 0 if offset is None else offset
    if type(start) is not int or start < 0 or start > len(data):
        raise CursorStale("增量游标超出文件范围")
    chunk = data[start:]
    boundary = chunk.rfind(b"\n")
    chunk = chunk[: boundary + 1] if boundary >= 0 else b""
    end = start + len(chunk)
    encoding = "utf-8-sig" if start == 0 else "utf-8"
    text = chunk.decode(encoding, errors="replace")
    rows = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(value, dict):
            continue
        try:
            parsed = parse_row(value)
        except (KeyError, TypeError, ValueError):
            continue
        if parsed is not None:
            rows.append(parsed)
    return end, rows
