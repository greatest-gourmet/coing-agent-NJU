"""The hand-written model/tool orchestration loop."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from .llm_client import ModelResponse, ToolCall
from .memory import WorkingMemory
from .lessons import LessonStore
from .trace import TraceRecorder
from .tools import ToolRegistry, ToolResult


# A `tool_calls` block the model emitted but could not parse strictly; used to
# salvage a JSON object wrapped in surrounding prose.
_ARG_RE = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}")


class ChatModel(Protocol):
    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class AgentLimits:
    # Allow longer coding tasks while retaining the other safety limits.
    max_rounds: int = 50
    max_tool_calls: int = 50
    max_seconds: float = 300.0
    max_parse_errors: int = 3
    max_repeated_calls: int = 3
    max_no_tool_rounds: int = 2


@dataclass(frozen=True, slots=True)
class AgentResult:
    ok: bool
    summary: str
    messages: tuple[Mapping[str, Any], ...]
    rounds: int
    tool_calls: int
    stop_reason: str
    usage: Mapping[str, int] = field(default_factory=dict)


ProgressHandler = Callable[[str, Mapping[str, Any]], None]
BeforeToolHook = Callable[[str, Mapping[str, Any]], tuple[Mapping[str, Any], str | None]]
AfterToolHook = Callable[[str, Mapping[str, Any], ToolResult], ToolResult]


class AgentRunner:
    """Run one task; all local effects go through the supplied registry."""

    def __init__(
        self,
        model: ChatModel,
        registry: ToolRegistry,
        *,
        system_prompt: str,
        limits: AgentLimits | None = None,
        trace: TraceRecorder | None = None,
        memory: WorkingMemory | None = None,
        on_progress: ProgressHandler | None = None,
        lesson_store: LessonStore | None = None,
        before_tool: BeforeToolHook | None = None,
        after_tool: AfterToolHook | None = None,
    ) -> None:
        self.model = model
        self.registry = registry
        self.system_prompt = system_prompt
        self.limits = limits or AgentLimits()
        self.trace = trace
        self.memory = memory or WorkingMemory()
        self.on_progress = on_progress
        self.lesson_store = lesson_store
        self.before_tool = before_tool
        self.after_tool = after_tool

    def run(
        self,
        task: str,
        *,
        history: Sequence[Mapping[str, Any]] | None = None,
    ) -> AgentResult:
        if not task.strip():
            raise ValueError("任务不能为空。")
        messages: list[Mapping[str, Any]] = list(history or [])
        if not messages:
            messages.append({"role": "system", "content": self.system_prompt})
        elif history:
            # finish ends one task, not the interactive session. Remove the whole
            # terminal finish exchange — the assistant tool_calls message plus every
            # tool reply that follows it — before the next user request. A final
            # assistant message can bundle finish with other tools, so dropping a
            # fixed number of messages would orphan those other tool_calls.
            if messages and messages[-1].get("role") == "tool":
                try:
                    tool_payload = json.loads(str(messages[-1].get("content", "")))
                except json.JSONDecodeError:
                    tool_payload = {}
                if (tool_payload.get("metadata") or {}).get("finished") is True:
                    end = len(messages)
                    while end > 0 and messages[end - 1].get("role") == "tool":
                        end -= 1
                    if end > 0 and messages[end - 1].get("role") == "assistant":
                        end -= 1
                    messages = messages[:end]
            messages.append(
                {
                    "role": "system",
                    "content": "上一项任务已经结束。下面是同一会话中的新用户请求，请重新判断并处理它。",
                }
            )
        messages.append({"role": "user", "content": task})
        self.memory.start(task)
        prior_lessons = self.lesson_store.search(task) if self.lesson_store else []
        if prior_lessons:
            messages.insert(1, {"role": "system", "content": "相关历史经验（仅供参考，必须以当前工具结果为准）：\n" + json.dumps(prior_lessons, ensure_ascii=False)})
        self._emit(
            "run_started",
            {
                "task": task,
                "innovations": [
                    "执行反馈驱动的自我反省",
                    "结构化工作记忆与持久化经验",
                    "工具生命周期 Hook",
                    "任务文件快照与 rollback",
                    "本地 OCR 辅助通道",
                ],
                "prior_lessons": len(prior_lessons),
            },
        )
        if self.trace:
            self.trace.record("run_started", task=task, limits=self.limits.__dict__ if hasattr(self.limits, "__dict__") else {"max_rounds": self.limits.max_rounds, "max_tool_calls": self.limits.max_tool_calls, "max_seconds": self.limits.max_seconds})
        started = time.monotonic()
        calls = 0
        rounds = 0
        parse_errors = 0
        repeated = 0
        previous_call: tuple[str, str] | None = None
        usage: dict[str, int] = {}
        observations: list[dict[str, Any]] = []
        no_tool_rounds = 0

        while True:
            if rounds >= self.limits.max_rounds:
                return self._stopped(messages, rounds, calls, usage, "达到最大轮数")
            if time.monotonic() - started >= self.limits.max_seconds:
                return self._stopped(messages, rounds, calls, usage, "超过总运行时间")

            rounds += 1
            self._emit("round_started", {"round": rounds})
            try:
                context_info = self.memory.context_info(messages)
                request_messages = self.memory.prepare(messages)
                self._emit("memory_prepared", context_info)
                if self.trace:
                    self.trace.record("memory_prepared", round=rounds, context=context_info)
                if self.trace:
                    self.trace.record("model_request", round=rounds, context=context_info, messages=request_messages)
                response = self.model.complete(request_messages, tools=self.registry.schemas())
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                messages.append({"role": "system", "content": f"模型调用失败：{detail}"})
                self._emit("model_error", {"round": rounds, "error": detail})
                if self.trace:
                    self.trace.record("model_error", round=rounds, error=detail)
                return self._stopped(messages, rounds, calls, usage, f"模型调用失败：{detail}")
            usage.update(response.usage)
            self._emit(
                "model_response",
                {
                    "round": rounds,
                    "content": response.content or "",
                    "tool_calls": [
                        {"name": call.name, "arguments": call.arguments_json}
                        for call in response.tool_calls
                    ],
                },
            )
            if self.trace:
                self.trace.record("model_response", round=rounds, content=response.content, tool_calls=[call.__dict__ if hasattr(call, "__dict__") else {"id": call.id, "name": call.name, "arguments_json": call.arguments_json} for call in response.tool_calls], usage=response.usage)

            assistant_message = self._assistant_message(response)
            messages.append(assistant_message)
            if not response.tool_calls:
                no_tool_rounds += 1
                if no_tool_rounds <= self.limits.max_no_tool_rounds:
                    messages.append({
                        "role": "system",
                        "content": (
                            "你刚才只进行了分析，没有执行工具。请继续完成用户任务："
                            "如果任务要求编写、修改或验证代码，必须立即调用相应工具（如 write_file、"
                            "apply_patch、run_command），不要只返回解释；完成后再调用 finish。"
                        ),
                    })
                    self._emit("no_tool_recovery", {"round": rounds, "attempt": no_tool_rounds})
                    if self.trace:
                        self.trace.record("no_tool_recovery", round=rounds, attempt=no_tool_rounds)
                    continue
                result = AgentResult(
                    ok=False,
                    summary=response.content or "模型未请求工具，也未调用 finish。",
                    messages=tuple(messages),
                    rounds=rounds,
                    tool_calls=calls,
                    stop_reason="模型未调用工具",
                    usage=usage,
                )
                self._record_finished(result)
                return result

            for idx, call in enumerate(response.tool_calls):
                calls += 1
                if calls > self.limits.max_tool_calls:
                    self._seal_tool_calls(messages, response.tool_calls, idx, "达到最大工具调用数，剩余工具调用未执行。")
                    return self._stopped(messages, rounds, calls, usage, "达到最大工具调用数")

                call_key = (call.name, call.arguments_json)
                repeated = repeated + 1 if call_key == previous_call else 1
                previous_call = call_key
                if repeated >= self.limits.max_repeated_calls:
                    self._seal_tool_calls(messages, response.tool_calls, idx, "检测到重复工具调用，剩余工具调用未执行。")
                    return self._stopped(messages, rounds, calls, usage, "检测到重复工具调用")

                arguments, parse_error = self._parse_arguments(call)
                self._emit(
                    "tool_started",
                    {
                        "round": rounds,
                        "tool": call.name,
                        "arguments": arguments if not parse_error else call.arguments_json,
                        "validation": "passed" if not parse_error else "failed",
                    },
                )
                if self.trace:
                    self.trace.record(
                        "tool_started",
                        round=rounds,
                        tool=call.name,
                        arguments=arguments if not parse_error else call.arguments_json,
                        validation="passed" if not parse_error else "failed",
                    )
                if parse_error:
                    parse_errors += 1
                    result = ToolResult(ok=False, error=parse_error, metadata={"error_type": "invalid_arguments"})
                else:
                    effective_args = arguments
                    hook_error = None
                    if self.before_tool:
                        try:
                            effective_args, hook_error = self.before_tool(call.name, arguments)
                        except Exception as exc:
                            hook_error = f"工具执行前 Hook 失败：{type(exc).__name__}: {exc}"
                    result = ToolResult(ok=False, error=hook_error, metadata={"error_type": "hook_blocked"}) if hook_error else self.registry.execute(call.name, effective_args)
                    if self.after_tool:
                        try:
                            result = self.after_tool(call.name, effective_args, result)
                        except Exception as exc:
                            result = ToolResult(ok=False, error=f"工具执行后 Hook 失败：{type(exc).__name__}: {exc}")
                self.memory.observe_tool(call.name, result.to_dict())
                observations.append({"tool": call.name, "ok": result.ok, "error": result.error, "output": result.output[:500], "metadata": dict(result.metadata)})
                self._emit(
                    "tool_finished",
                    {
                        "round": rounds,
                        "tool": call.name,
                        "ok": result.ok,
                        "output": result.output,
                        "error": result.error,
                        "metadata": result.metadata,
                        "memory": self.memory.snapshot(),
                    },
                )
                if self.trace:
                    self.trace.record("tool_result", round=rounds, tool=call.name, arguments=arguments if not parse_error else call.arguments_json, result=result.to_dict())
                messages.append(self._tool_message(call, result))

                if result.ok and result.metadata.get("finished") is True:
                    final = AgentResult(
                        ok=True,
                        summary=result.output,
                        messages=tuple(messages),
                        rounds=rounds,
                        tool_calls=calls,
                        stop_reason="finish",
                        usage=usage,
                    )
                    self._record_finished(final)
                    self._save_lesson(task, final, observations)
                    return final
                if parse_errors >= self.limits.max_parse_errors:
                    self._seal_tool_calls(messages, response.tool_calls, idx + 1, "解析错误次数过多，剩余工具调用未执行。")
                    return self._stopped(messages, rounds, calls, usage, "解析错误次数过多")

    @staticmethod
    def _assistant_message(response: ModelResponse) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": response.content}
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments_json},
                }
                for call in response.tool_calls
            ]
        return message

    @staticmethod
    def _parse_arguments(call: ToolCall) -> tuple[Mapping[str, Any], str | None]:
        raw = (call.arguments_json or "").strip()
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            # Models sometimes wrap the JSON object in prose; salvage the
            # outermost object before giving up.
            match = _ARG_RE.search(raw)
            if not match:
                return {}, f"工具 {call.name} 的参数不是合法 JSON。请只修正本次调用。"
            try:
                arguments = json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}, f"工具 {call.name} 的参数不是合法 JSON。请只修正本次调用。"
        if not isinstance(arguments, dict):
            return {}, f"工具 {call.name} 的参数必须是 JSON 对象。请只修正本次调用。"
        return arguments, None

    @staticmethod
    def _tool_message(call: ToolCall, result: ToolResult) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": json.dumps(result.to_dict(), ensure_ascii=False),
        }

    def _seal_tool_calls(
        self,
        messages: list[Mapping[str, Any]],
        calls: Sequence[ToolCall],
        first_unanswered: int,
        reason: str,
    ) -> None:
        """Append a synthetic tool reply for each unanswered tool call.

        The Chat Completions API requires every assistant ``tool_calls`` message
        to be followed by one ``tool`` message per ``tool_call_id``. Early-exit
        paths (resource limits / repeated calls) would otherwise leave the
        history in a shape the next turn's request gets rejected with HTTP 400.
        """
        for call in calls[first_unanswered:]:
            messages.append(
                self._tool_message(
                    call,
                    ToolResult(ok=False, error=reason, metadata={"error_type": "unanswered"}),
                )
            )

    def _stopped(
        self,
        messages: list[Mapping[str, Any]],
        rounds: int,
        calls: int,
        usage: Mapping[str, int],
        reason: str,
    ) -> AgentResult:
        result = AgentResult(
            ok=False,
            summary=f"Agent 已停止：{reason}。",
            messages=tuple(messages),
            rounds=rounds,
            tool_calls=calls,
            stop_reason=reason,
            usage=dict(usage),
        )
        self._record_finished(result)
        return result

    def _record_finished(self, result: AgentResult) -> None:
        self._emit(
            "run_finished",
            {
                "ok": result.ok,
                "summary": result.summary,
                "rounds": result.rounds,
                "tool_calls": result.tool_calls,
                "stop_reason": result.stop_reason,
            },
        )
        if self.trace:
            self.trace.record(
                "run_finished",
                ok=result.ok,
                summary=result.summary,
                rounds=result.rounds,
                tool_calls=result.tool_calls,
                stop_reason=result.stop_reason,
                usage=result.usage,
            )

    def _save_lesson(self, task: str, result: AgentResult, observations: list[dict[str, Any]]) -> None:
        if self.lesson_store:
            try:
                self.lesson_store.append(task=task, ok=result.ok, summary=result.summary, observations=observations)
            except OSError:
                pass

    def _emit(self, event: str, data: Mapping[str, Any]) -> None:
        if self.on_progress:
            try:
                self.on_progress(event, data)
            except Exception:
                # Terminal presentation must never change Agent behavior.
                pass
