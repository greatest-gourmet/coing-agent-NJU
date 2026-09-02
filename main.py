"""Command-line entry point for the coding agent project."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from coding_agent import (
    AgentRunner,
    AppConfig,
    ConfigError,
    LLMClientError,
    OpenAICompatibleClient,
    ToolContext,
    TraceRecorder,
    LessonStore,
    create_default_registry,
)
from coding_agent.trace import redact


SYSTEM_PROMPT = """你是一个本地编程智能体。
请先观察项目，再通过工具完成用户任务；不要臆测文件内容。
只能使用提供的工具，工具失败时根据错误调整操作。
文件读写、修改和回滚的 path 必须使用工作目录内的相对路径；不要越过文件沙箱。
ocr_image 是只读例外：用户明确提供工作目录外的图片/PDF绝对路径时，可以直接 OCR，但不得写入或执行该路径。
完成并验证任务后必须调用 finish，报告修改内容和测试结果。
不要读取、打印或提交 API Key、密码、Token 等凭据。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从零实现的编程智能体（当前已接入本地工具主循环）。"
    )
    parser.add_argument("task", nargs="?", help="需要处理的编程任务（--chat 模式下可省略）")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="进入可连续输入多条消息的交互模式",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path.cwd(),
        help="Agent 工作目录（默认：当前目录）",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="将脱敏运行轨迹写入 workdir/trace/",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="实时显示模型响应、工具调用和执行结果",
    )
    return parser


def ensure_workdir(path: Path) -> Path:
    """Create the requested workspace and return its normalized path."""
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise ValueError(f"工作目录路径不是目录：{resolved}")
    return resolved


def make_progress_printer(api_key: str):
    """Create a bounded, secret-safe terminal event renderer."""
    def print_event(event: str, data: dict[str, object]) -> None:
        safe = redact(data, secrets=(api_key,))
        if event == "run_started":
            print("[开始] 接收任务")
            print("[架构] 模型只提出动作；ToolRegistry、参数校验和本地执行由本项目自行完成")
            print("[架构] 未调用任何 Agent 框架或 SDK")
            innovations = safe.get("innovations") or []
            if innovations:
                print("[创新点] " + "；".join(map(str, innovations)))
            print(f"[经验召回] 找到 {safe.get('prior_lessons', 0)} 条相关历史经验")
        elif event == "round_started":
            print(f"\n[第 {safe['round']} 轮] 调用模型")
        elif event == "memory_prepared":
            memory = safe.get("memory") or {}
            files = memory.get("changed_files") or []
            tests = memory.get("tests") or []
            error = memory.get("current_error")
            status = "已压缩" if safe.get("compressed") else "未压缩"
            print(
                f"[记忆] 原始 {safe.get('raw_messages')} 条/{safe.get('raw_chars')} 字符 "
                f"→ 发送 {safe.get('sent_messages')} 条/{safe.get('sent_chars')} 字符，{status}"
            )
            print("[对话记忆] 本轮模型可看到系统规则、用户任务、模型决策和工具返回结果")
            injected = safe.get("structured_memory_injected")
            injection_text = "已作为摘要注入模型" if injected else "已维护，但本轮未单独注入（完整历史仍在）"
            print(f"[工作记忆] {injection_text}；已修改文件：{', '.join(map(str, files)) if files else '无'}")
            print(f"[工作记忆] 运行/测试观察：{len(tests)} 条；当前失败：{error or '无'}")
            observation = safe.get("last_observation")
            if isinstance(observation, dict):
                preview = str(observation.get("preview") or "").strip().replace("\n", " | ")
                print(
                    f"[反省依据] 上一工具={observation.get('tool')}，成功={observation.get('ok')}，"
                    f"exit_code={observation.get('exit_code')}，观察={preview[:500] or '无输出'}"
                )
                print("[反省过程] 上述真实观察将随对话历史发送给模型，由模型决定修复、重试或 finish")
            else:
                print("[反省依据] 尚无工具执行结果，本轮主要依据用户任务和已有对话制定行动")
        elif event == "model_response":
            content = str(safe.get("content") or "").strip()
            calls = safe.get("tool_calls") or []
            if content:
                print(f"[模型] {content[:1000]}")
            if calls:
                print(f"[模型] 请求工具：{', '.join(str(item.get('name')) for item in calls)}")
        elif event == "model_error":
            print(f"[模型错误] 第 {safe.get('round')} 轮请求失败：{safe.get('error')}")
        elif event == "no_tool_recovery":
            print(f"[执行提醒] 第 {safe.get('round')} 轮模型只分析未调用工具，已追加执行提醒（第 {safe.get('attempt')} 次）")
        elif event == "tool_started":
            args = json.dumps(safe.get("arguments"), ensure_ascii=False, default=str)
            validation = "参数校验通过" if safe.get("validation") == "passed" else "参数校验失败"
            print(f"[工具开始] {safe.get('tool')}（{validation}）参数：{args[:600]}")
        elif event == "tool_finished":
            status = "成功" if safe.get("ok") else "失败"
            output = str(safe.get("output") or safe.get("error") or "").strip()
            metadata = safe.get("metadata") or {}
            duration = metadata.get("duration_ms", "?") if isinstance(metadata, dict) else "?"
            print(f"[工具结束] {status}，耗时 {duration} ms：{output[:1000]}")
            if isinstance(metadata, dict):
                if metadata.get("rollback_available"):
                    print(f"[版本控制] 已保存修改前快照，可调用 rollback 恢复：{metadata.get('revision')}")
                if metadata.get("rolled_back"):
                    print("[版本控制] 已完成本地快照恢复")
                if metadata.get("exit_code") is not None:
                    print(f"[执行审计] cwd=工作目录，exit_code={metadata.get('exit_code')}，输出已受长度限制")
            if safe.get("memory"):
                memory = safe["memory"]
                print(f"[记忆更新] 修改文件={memory.get('changed_files') or '无'}，测试数={len(memory.get('tests') or [])}，错误={memory.get('current_error') or '无'}")
                print("[记忆写入] 完整工具结果已写入对话历史，下一轮模型可据此反省")
                if safe.get("tool") == "run_command":
                    print("[记忆写入] 本次命令输出和退出码也已追加到结构化运行/测试观察；即使退出码为 0，异常输出仍会保留")
        elif event == "run_finished":
            print(f"[结束] {safe.get('summary')} 原因：{safe.get('stop_reason')}")
    return print_event


def run_chat(config, client, registry, *, trace, verbose: bool, lesson_store: LessonStore | None = None) -> int:
    """Keep one message history across multiple user turns."""
    runner = AgentRunner(
        client,
        registry,
        system_prompt=SYSTEM_PROMPT,
        trace=trace,
        on_progress=make_progress_printer(config.api_key) if verbose else None,
        lesson_store=lesson_store,
    )
    history = None
    print("已进入交互模式。输入 exit 或 quit 退出。")
    if trace:
        print("[轨迹] 已启用脱敏 JSONL 记录")
    while True:
        try:
            task = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0
        if task.lower() in {"exit", "quit"}:
            print("已退出。")
            return 0
        if not task:
            continue
        try:
            result = runner.run(task, history=history)
        except KeyboardInterrupt:
            if trace:
                trace.record("run_cancelled", reason="用户按下 Ctrl+C", task=task)
            print("\n已取消当前任务（Ctrl+C），记忆保留，继续输入下一条消息。")
            continue
        history = result.messages
        print(f"\nAgent> {result.summary}")
        print(f"本轮轮数：{result.rounds}；工具调用：{result.tool_calls}；状态：{result.stop_reason}")


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.chat and not args.task:
        print("错误：请提供任务，或使用 --chat 进入交互模式。", file=sys.stderr)
        return 2
    try:
        workdir = ensure_workdir(args.workdir)
        config = AppConfig.from_env(workdir=workdir)
        client = OpenAICompatibleClient(config)
        registry = create_default_registry(ToolContext(config.workdir))
        recorder = (
            TraceRecorder(
                config.workdir / "trace" / "session.jsonl",
                secrets=(config.api_key,),
            )
            if args.trace
            else None
        )
        lesson_store = LessonStore(config.workdir / ".agent_memory" / "lessons.jsonl")
        if args.chat:
            return run_chat(config, client, registry, trace=recorder, verbose=args.verbose, lesson_store=lesson_store)
        try:
            result = AgentRunner(
                client,
                registry,
                system_prompt=SYSTEM_PROMPT,
                trace=recorder,
                on_progress=make_progress_printer(config.api_key) if args.verbose else None,
                lesson_store=lesson_store,
            ).run(args.task)
        except KeyboardInterrupt:
            if recorder:
                recorder.record("run_cancelled", reason="用户按下 Ctrl+C")
            print("\n已取消当前任务（Ctrl+C）。已完成的文件修改不会自动撤销。", file=sys.stderr)
            return 130
    except (ConfigError, LLMClientError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.trace:
        print("轨迹已写入：" + str(config.workdir / "trace" / "session.jsonl"), file=sys.stderr)
    print(result.summary)
    if result.usage:
        print(f"运行轮数：{result.rounds}；工具调用：{result.tool_calls}；停止原因：{result.stop_reason}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
