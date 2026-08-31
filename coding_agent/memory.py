"""Deterministic working memory and bounded conversation history."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


class WorkingMemory:
    """Keep task state explicitly and compact old messages when needed."""

    def __init__(self, *, max_context_chars: int = 80_000, recent_messages: int = 8) -> None:
        if max_context_chars < 1_000 or recent_messages < 2:
            raise ValueError("上下文限制或 recent_messages 设置过小。")
        self.max_context_chars = max_context_chars
        self.recent_messages = recent_messages
        self.state: dict[str, Any] = {
            "task": "",
            "constraints": [],
            "changed_files": [],
            "tests": [],
            "current_error": None,
            "next_step": "",
        }

    def start(self, task: str) -> None:
        self.state["task"] = task

    def observe_tool(self, name: str, result: Mapping[str, Any]) -> None:
        metadata = result.get("metadata") or {}
        path = metadata.get("path")
        if path and name in {"write_file", "apply_patch"} and path not in self.state["changed_files"]:
            self.state["changed_files"].append(path)
        if name == "run_command":
            self.state["tests"].append({
                "exit_code": metadata.get("exit_code"),
                "result": result.get("output", "")[:500],
            })
        if result.get("ok") is False:
            self.state["current_error"] = {
                "tool": name,
                "message": result.get("error") or "工具执行失败",
            }
        elif name == "run_command":
            self.state["current_error"] = None

    def summary_message(self) -> dict[str, str]:
        return {
            "role": "system",
            "content": "工作记忆（由程序维护，请以当前文件和测试结果为准）：\n"
            + json.dumps(self.state, ensure_ascii=False),
        }

    def snapshot(self) -> dict[str, Any]:
        """Return a display-safe copy of the current deterministic state."""
        return json.loads(json.dumps(self.state, ensure_ascii=False))

    def context_info(self, messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        raw_chars = len(json.dumps(list(messages), ensure_ascii=False, default=str))
        compressed = raw_chars > self.max_context_chars
        prepared = self.prepare(messages)
        last_observation = self._last_tool_observation(messages)
        return {
            "raw_messages": len(messages),
            "sent_messages": len(prepared),
            "raw_chars": raw_chars,
            "sent_chars": len(json.dumps(prepared, ensure_ascii=False, default=str)),
            "compressed": compressed,
            "structured_memory_injected": compressed,
            "last_observation": last_observation,
            "memory": self.snapshot(),
        }

    @staticmethod
    def _last_tool_observation(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        """Return a bounded description of the newest tool result in history."""
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            raw_content = str(message.get("content", ""))
            try:
                payload = json.loads(raw_content)
            except json.JSONDecodeError:
                payload = {"ok": None, "output": raw_content}
            metadata = payload.get("metadata") or {}
            text = payload.get("error") or payload.get("output") or ""
            return {
                "tool": message.get("name", "未知工具"),
                "ok": payload.get("ok"),
                "exit_code": metadata.get("exit_code"),
                "preview": str(text)[:500],
            }
        return None

    @staticmethod
    def _one_line(message: Mapping[str, Any]) -> str:
        """Condense a single message into one summary line."""
        role = message.get("role")
        content = str(message.get("content") or "").strip().replace("\n", " ")[:120]
        calls = message.get("tool_calls")
        if calls:
            names = [
                str((call.get("function") or {}).get("name", "?"))
                for call in calls
            ]
            return f"[assistant 调用了 {', '.join(names)}]"
        if role == "tool":
            return f"[工具 {message.get('name', '?')} 返回: {content[:120]}]"
        return f"[{role}: {content}]"

    def prepare(self, messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Return a bounded view; the original full history remains available in the result."""
        total = len(json.dumps(list(messages), ensure_ascii=False, default=str))
        if total <= self.max_context_chars:
            return list(messages)
        system = list(messages[:1])
        user = list(messages[1:2])
        recent = list(messages[-self.recent_messages :])
        while recent and recent[0].get("role") == "tool":
            recent.pop(0)
        dropped = messages[len(system) + len(user) : len(messages) - len(recent)]
        compact = system + user + [self.summary_message()]
        if dropped:
            lines = [self._one_line(message) for message in dropped]
            compact.append(
                {
                    "role": "system",
                    "content": "此前对话的过程摘要（由程序压缩，仅供回顾，请以当前文件和测试结果为准）：\n"
                    + "\n".join("  - " + line for line in lines),
                }
            )
        if recent:
            compact.extend(recent)
        return compact
