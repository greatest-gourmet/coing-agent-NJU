"""Local tools exposed to the model.

The model can request an operation, but this module owns validation and execution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Mapping


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    output: str = ""
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ToolContext:
    workdir: Path
    command_timeout: float = 60.0
    max_output_chars: int = 20_000


ToolHandler = Callable[[ToolContext, Mapping[str, Any]], ToolResult]


class ToolRegistry:
    """Registry, schema exporter and safe dispatcher for local tools."""

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._handlers: dict[str, ToolHandler] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[str, str | None] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: Mapping[str, Any],
        handler: ToolHandler,
    ) -> None:
        if name in self._handlers:
            raise ValueError(f"工具已注册：{name}")
        self._handlers[name] = handler
        self._schemas[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": dict(parameters),
            },
        }

    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas.values())

    def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(ok=False, error=f"未知工具：{name}")
        if not isinstance(arguments, Mapping):
            return ToolResult(ok=False, error="工具参数必须是 JSON 对象。")
        validation_error = self._validate_arguments(name, arguments)
        if validation_error:
            return ToolResult(ok=False, error=validation_error, metadata={"error_type": "invalid_arguments"})

        snapshot_path = None
        if name in {"write_file", "apply_patch"}:
            try:
                raw_path = _required_string(arguments, "path")
                snapshot_path = _safe_path(self.context, raw_path)
                key = str(snapshot_path)
                self._snapshots[key] = snapshot_path.read_text(encoding="utf-8") if snapshot_path.is_file() else None
            except (ValueError, OSError):
                pass

        started = time.perf_counter()
        try:
            result = handler(self.context, arguments)
            if not isinstance(result, ToolResult):
                raise TypeError("工具处理函数必须返回 ToolResult。")
        except Exception as exc:
            result = ToolResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}\n",
            )
        metadata = dict(result.metadata)
        if result.ok and snapshot_path is not None:
            metadata["revision"] = str(snapshot_path)
            metadata["rollback_available"] = True
        metadata.setdefault("duration_ms", round((time.perf_counter() - started) * 1000, 2))
        return ToolResult(result.ok, result.output, result.error, metadata)

    def _validate_arguments(self, name: str, arguments: Mapping[str, Any]) -> str | None:
        schema = self._schemas[name]["function"]["parameters"]
        required = schema.get("required", [])
        missing = [key for key in required if key not in arguments]
        if missing:
            return f"工具 {name} 缺少必填参数：{', '.join(missing)}。"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = [key for key in arguments if key not in properties]
            if unknown:
                return f"工具 {name} 包含未知参数：{', '.join(unknown)}。"
        for key, value in arguments.items():
            expected = properties.get(key, {}).get("type")
            valid = (
                expected is None
                or (expected == "string" and isinstance(value, str))
                or (expected == "boolean" and isinstance(value, bool))
                or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool))
                or (expected == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
            )
            if not valid:
                return f"参数 {key} 类型错误，应为 {expected}。"
        return None


def _required_string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"参数 {key} 必须是非空字符串。")
    return value


def _safe_path(context: ToolContext, raw_path: str, *, must_exist: bool = False) -> Path:
    candidate = (context.workdir / raw_path).resolve()
    try:
        candidate.relative_to(context.workdir)
    except ValueError as exc:
        raise ValueError("路径必须位于工作目录内。") from exc
    if must_exist and not candidate.exists():
        raise FileNotFoundError(f"文件或目录不存在：{raw_path}")
    return candidate


def _safe_ocr_input(raw_path: str) -> Path:
    """Resolve an explicitly supplied OCR input, which may be outside workdir.

    This is read-only and limited to image/PDF files. Write/edit/command tools
    continue to use the stricter workdir sandbox above.
    """
    candidate = Path(raw_path).expanduser().resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"图片或 PDF 不存在：{raw_path}")
    if candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif", ".pdf"}:
        raise ValueError("ocr_image 只允许读取 PNG/JPG/BMP/WebP/TIFF/GIF 图片或 PDF。")
    return candidate


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[输出已截断]", True


def _list_files(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    raw_path = arguments.get("path", ".")
    if not isinstance(raw_path, str):
        return ToolResult(ok=False, error="参数 path 必须是字符串。")
    directory = _safe_path(context, raw_path, must_exist=True)
    if not directory.is_dir():
        return ToolResult(ok=False, error="path 不是目录。")
    entries = sorted(
        (
            f"{item.name}{'/' if item.is_dir() else ''}"
            for item in directory.iterdir()
            if item.name not in {".git", "__pycache__", ".pytest_cache"}
        ),
        key=str.lower,
    )
    output, truncated = _clip("\n".join(entries), context.max_output_chars)
    return ToolResult(ok=True, output=output or "（空目录）", metadata={"truncated": truncated})


def _read_file(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    path = _safe_path(context, _required_string(arguments, "path"), must_exist=True)
    if not path.is_file():
        return ToolResult(ok=False, error="path 不是文件。")
    start = arguments.get("start_line", 1)
    end = arguments.get("end_line")
    if not isinstance(start, int) or start < 1 or (end is not None and (not isinstance(end, int) or end < start)):
        return ToolResult(ok=False, error="行号范围无效。")
    lines = path.read_text(encoding="utf-8").splitlines()
    selected = lines[start - 1 : end]
    joined = "\n".join(selected)
    if len(joined) > context.max_output_chars:
        # For large files, keep both ends: the head holds imports/declarations
        # and the tail often holds the entry point, both of which matter.
        half = context.max_output_chars // 2
        output = joined[:half] + "\n...[中间内容已截断]...\n" + joined[-half:]
        truncated = True
    else:
        output = joined
        truncated = False
    return ToolResult(
        ok=True,
        output=output,
        metadata={"path": str(path.relative_to(context.workdir)), "start_line": start, "end_line": end, "truncated": truncated},
    )


def _write_file(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    path = _safe_path(context, _required_string(arguments, "path"))
    content = arguments.get("content")
    if not isinstance(content, str):
        return ToolResult(ok=False, error="参数 content 必须是字符串。")
    if path.exists() and not arguments.get("overwrite", False):
        return ToolResult(ok=False, error="文件已存在；如需覆盖必须显式设置 overwrite=true。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return ToolResult(ok=True, output=f"已写入 {path.relative_to(context.workdir)}", metadata={"path": str(path.relative_to(context.workdir)), "bytes": len(content.encode('utf-8'))})


def _apply_patch(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    path = _safe_path(context, _required_string(arguments, "path"), must_exist=True)
    if not path.is_file():
        return ToolResult(ok=False, error="path 不是文件。")
    old_text = arguments.get("old_text")
    new_text = arguments.get("new_text")
    if not isinstance(old_text, str) or not isinstance(new_text, str) or not old_text:
        return ToolResult(ok=False, error="old_text 和 new_text 必须是非空字符串。")
    current = path.read_text(encoding="utf-8")
    occurrences = current.count(old_text)
    if occurrences != 1:
        return ToolResult(ok=False, error=f"old_text 应恰好匹配 1 次，实际匹配 {occurrences} 次。")
    path.write_text(current.replace(old_text, new_text, 1), encoding="utf-8")
    return ToolResult(ok=True, output=f"已修改 {path.relative_to(context.workdir)}", metadata={"path": str(path.relative_to(context.workdir)), "replacements": 1})


_DANGEROUS_COMMANDS = re.compile(r"(?:\bformat\b|\bshutdown\b|\brestart\b|\brm\s+-rf\b|\bdel\s+/[fsq]+\b)", re.IGNORECASE)


def _clean_environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(word in key.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET", "AUTH"))
    }
    # Make the current interpreter's bin dir resolvable inside the subprocess,
    # so `python`/`pytest` resolve to the same environment the agent itself
    # runs in even when that environment is not on the machine-wide PATH.
    self_bin = str(Path(sys.executable).resolve().parent)
    env["PATH"] = self_bin + os.pathsep + env.get("PATH", "")
    return env


def _run_command(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    command = _required_string(arguments, "command")
    if _DANGEROUS_COMMANDS.search(command):
        return ToolResult(ok=False, error="命令包含高风险操作，已拒绝执行。")
    timeout = arguments.get("timeout_seconds", context.command_timeout)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        return ToolResult(ok=False, error="timeout_seconds 必须是正数。")
    timeout = min(float(timeout), context.command_timeout)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=context.workdir,
            env=_clean_environment(),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        combined = completed.stdout
        if completed.stderr:
            combined += ("\n" if combined else "") + completed.stderr
        output, truncated = _clip(combined, context.max_output_chars)
        return ToolResult(
            ok=completed.returncode == 0,
            output=output,
            error=None if completed.returncode == 0 else f"命令退出码：{completed.returncode}",
            metadata={"exit_code": completed.returncode, "truncated": truncated, "duration_ms": round((time.perf_counter() - started) * 1000, 2)},
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        return ToolResult(ok=False, output=str(partial), error=f"命令执行超时（上限 {timeout:g} 秒）。", metadata={"timeout_seconds": timeout})


def _ocr_image(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    """Run the bundled local OCR skill and return structured UTF-8 JSON."""
    raw_path = _required_string(arguments, "path")
    candidate = Path(raw_path).expanduser()
    # Relative paths remain workspace-confined; an absolute path is treated as
    # an explicitly supplied, read-only OCR input.
    path = _safe_ocr_input(raw_path) if candidate.is_absolute() else _safe_path(context, raw_path, must_exist=True)
    if not path.is_file():
        return ToolResult(ok=False, error="path 不是图片或 PDF 文件。")
    engine = arguments.get("engine", "auto")
    preprocess = arguments.get("preprocess", False)
    if engine not in {"auto", "win", "rapid", "tess"}:
        return ToolResult(ok=False, error="engine 必须是 auto、win、rapid 或 tess。")
    if not isinstance(preprocess, bool):
        return ToolResult(ok=False, error="preprocess 必须是布尔值。")
    script = Path(__file__).resolve().parent.parent / "ocr" / "scripts" / "ocr_tool.py"
    if not script.is_file():
        return ToolResult(ok=False, error=f"OCR Skill 脚本不存在：{script}")
    command = [sys.executable, str(script), str(path), "--json", "--engine", engine]
    if preprocess:
        command.append("--preprocess")
    try:
        completed = subprocess.run(
            command,
            cwd=context.workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=context.command_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, error=f"OCR 识别超时（上限 {context.command_timeout:g} 秒）。")
    except OSError as exc:
        return ToolResult(ok=False, error=f"OCR 进程无法启动：{type(exc).__name__}: {exc}")
    raw = completed.stdout.strip()
    if not raw:
        return ToolResult(ok=False, error=f"OCR 没有返回结果：{completed.stderr.strip()[:500]}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ToolResult(ok=False, output=raw[:context.max_output_chars], error="OCR 返回的不是合法 JSON。")
    output, truncated = _clip(json.dumps(payload, ensure_ascii=False, indent=2), context.max_output_chars)
    ok = completed.returncode == 0 and bool(payload.get("ok"))
    return ToolResult(
        ok=ok,
        output=output,
        error=None if ok else str(payload.get("error") or f"OCR 进程退出码：{completed.returncode}"),
        metadata={"path": str(path.relative_to(context.workdir)) if path.is_relative_to(context.workdir) else str(path), "external_read_only": not path.is_relative_to(context.workdir), "engine": payload.get("engine", engine), "truncated": truncated},
    )


def _finish(_context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    summary = _required_string(arguments, "summary")
    tests = arguments.get("tests", "")
    if tests is not None and not isinstance(tests, str):
        return ToolResult(ok=False, error="参数 tests 必须是字符串。")
    return ToolResult(ok=True, output=summary, metadata={"finished": True, "tests": tests})


def _make_rollback(registry: ToolRegistry) -> ToolHandler:
    def rollback(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        path = _safe_path(context, _required_string(arguments, "path"))
        key = str(path)
        if key not in registry._snapshots:
            return ToolResult(ok=False, error="没有找到该文件的可回滚快照。")
        previous = registry._snapshots[key]
        if previous is None:
            if path.exists():
                path.unlink()
            return ToolResult(ok=True, output=f"已删除本次创建的文件 {path.relative_to(context.workdir)}", metadata={"rolled_back": True})
        path.write_text(previous, encoding="utf-8")
        return ToolResult(ok=True, output=f"已恢复 {path.relative_to(context.workdir)}", metadata={"rolled_back": True})
    return rollback


def create_default_registry(context: ToolContext) -> ToolRegistry:
    registry = ToolRegistry(context)
    registry.register("list_files", "列出工作目录下指定目录的直接子项。", {"type": "object", "properties": {"path": {"type": "string", "description": "相对工作目录的路径，默认 ."}}, "additionalProperties": False}, _list_files)
    registry.register("read_file", "读取文本文件，可选地限定起止行号。", {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, "required": ["path"], "additionalProperties": False}, _read_file)
    registry.register("write_file", "创建文件或在显式允许时覆盖文件。", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean"}}, "required": ["path", "content"], "additionalProperties": False}, _write_file)
    registry.register("apply_patch", "用唯一匹配的旧文本替换为新文本，执行局部修改。", {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False}, _apply_patch)
    registry.register("run_command", "在工作目录中执行命令并返回输出与退出码。", {"type": "object", "properties": {"command": {"type": "string"}, "timeout_seconds": {"type": "number", "exclusiveMinimum": 0}}, "required": ["command"], "additionalProperties": False}, _run_command)
    registry.register("ocr_image", "调用本地 OCR Skill 提取图片或 PDF 文字。相对路径限工作目录；也可读取用户明确提供的工作目录外绝对路径，但仅允许只读 OCR。", {"type": "object", "properties": {"path": {"type": "string", "description": "工作目录内相对路径，或用户明确提供的图片/PDF绝对路径"}, "engine": {"type": "string", "enum": ["auto", "win", "rapid", "tess"]}, "preprocess": {"type": "boolean"}}, "required": ["path"], "additionalProperties": False}, _ocr_image)
    registry.register("finish", "明确结束任务并报告摘要和测试结果。", {"type": "object", "properties": {"summary": {"type": "string"}, "tests": {"type": "string"}}, "required": ["summary"], "additionalProperties": False}, _finish)
    registry.register("rollback", "恢复指定文件最近一次修改前的快照。", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}, _make_rollback(registry))
    return registry
