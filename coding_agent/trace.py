"""JSONL execution trace with conservative secret redaction."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


_SECRET_KEY = re.compile(r"(api[_-]?key|token|password|secret|authorization|credential)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(?i)(sk-[a-z0-9_-]{10,}|bearer\s+[a-z0-9._-]{10,})")


def redact(value: Any, secrets: Sequence[str] = ()) -> Any:
    """Redact secret-looking mappings and values recursively."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if _SECRET_KEY.search(str(key))
            else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return _SECRET_VALUE.sub("[REDACTED]", result)
    return value


class TraceRecorder:
    """Append bounded, sanitized events so a run can be replayed later."""

    def __init__(
        self,
        path: str | Path,
        *,
        secrets: Sequence[str] = (),
        max_text_chars: int = 20_000,
    ) -> None:
        if max_text_chars < 100:
            raise ValueError("max_text_chars 必须至少为 100。")
        self.path = Path(path)
        self.secrets = tuple(secrets)
        self.max_text_chars = max_text_chars
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **data: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        safe = redact(payload, self.secrets)
        text = json.dumps(safe, ensure_ascii=False, default=str)
        if len(text) > self.max_text_chars:
            safe = {
                "timestamp": payload["timestamp"],
                "event": event,
                "truncated": True,
                "preview": text[: max(1, self.max_text_chars - 100)],
            }
            text = json.dumps(safe, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
