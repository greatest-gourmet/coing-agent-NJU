"""Persistent store for verified task experience."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class LessonStore:
    """Append-only JSONL lessons; this is retrieval, not model training."""

    def __init__(self, path: str | Path, *, max_results: int = 3) -> None:
        self.path = Path(path)
        self.max_results = max(1, max_results)

    def search(self, task: str) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        terms = {word.lower() for word in task.split() if len(word) >= 2}
        scored: list[tuple[int, dict[str, Any]]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            haystack = json.dumps(item, ensure_ascii=False).lower()
            score = sum(term in haystack for term in terms)
            if score:
                scored.append((score, item))
        return [item for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[: self.max_results]]

    def append(self, *, task: str, ok: bool, summary: str, observations: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"task": task, "ok": ok, "summary": summary, "observations": observations[-10:]}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
