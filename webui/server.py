"""Zero-dependency local web UI for the coding agent.

This runs the exact same ``AgentRunner`` the CLI uses, but exposes it behind a
tiny ``http.server`` + Server-Sent Events page so a run can be watched live in
the browser — model replies, tool calls, results, memory — with the project's
design innovations highlighted as they actually fire.

Run from the project root with::

    python -m webui.server --open

The agent core (``coding_agent/*``) is not modified; this module only adds an
``on_progress`` callback that forwards the CLI's event stream to the browser.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

# Make `coding_agent` and `main` importable no matter how this file is launched.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coding_agent import (  # noqa: E402
    AgentRunner,
    AppConfig,
    ConfigError,
    LLMClientError,
    LessonStore,
    OpenAICompatibleClient,
    ToolContext,
    TraceRecorder,
    create_default_registry,
)
from coding_agent.trace import redact  # noqa: E402
from main import SYSTEM_PROMPT  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"


class CancellableClient:
    """Stop a run cooperatively at the next model-call boundary.

    ``AgentRunner`` treats a raised ``LLMClientError`` from ``complete`` as a
    stop condition ("模型调用失败"), so flipping the flag causes a clean stop
    without touching the agent loop.
    """

    def __init__(self, inner: OpenAICompatibleClient, cancel: threading.Event) -> None:
        self._inner = inner
        self._cancel = cancel

    def complete(self, messages, *, tools=None):
        if self._cancel.is_set():
            raise LLMClientError("任务已被用户取消。")
        return self._inner.complete(messages, tools=tools)


class Run:
    """One in-flight agent run: its event queue and a cooperative cancel flag."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.events: "queue.Queue[tuple[str, Mapping[str, Any]]]" = queue.Queue()
        self.cancel = threading.Event()


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        self._sessions: dict[str, list[Mapping[str, Any]]] = {}

    def create(self) -> Run:
        run = Run(uuid.uuid4().hex[:12])
        with self._lock:
            self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def session(self, session_id: str) -> list[Mapping[str, Any]] | None:
        with self._lock:
            return self._sessions.get(session_id)

    def set_session(self, session_id: str, messages: list[Mapping[str, Any]]) -> None:
        with self._lock:
            self._sessions[session_id] = messages


manager = RunManager()


def build_config(
    workdir: Path,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> AppConfig:
    """Merge UI-provided settings over the process environment."""
    env = dict(os.environ)
    if api_key:
        env["OPENAI_API_KEY"] = api_key
    if base_url:
        env["OPENAI_BASE_URL"] = base_url
    if model:
        env["OPENAI_MODEL"] = model
    return AppConfig.from_env(workdir=workdir, environ=env)


def run_agent(
    run: Run,
    *,
    task: str,
    workdir: Path,
    mode: str,
    session_id: str,
    trace: bool,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> None:
    try:
        config = build_config(workdir, api_key, base_url, model)
    except (ConfigError, OSError, ValueError) as exc:
        run.events.put(("__error__", {"error": f"{type(exc).__name__}: {exc}"}))
        return

    client = CancellableClient(OpenAICompatibleClient(config), run.cancel)
    registry = create_default_registry(ToolContext(config.workdir))
    trace_path = config.workdir / "trace" / f"session-{run.run_id}.jsonl"
    recorder = TraceRecorder(trace_path, secrets=(config.api_key,)) if trace else None
    lesson_store = LessonStore(config.workdir / ".agent_memory" / "lessons.jsonl")

    def on_progress(event: str, data: Mapping[str, Any]) -> None:
        # Redact exactly like the CLI printer before anything reaches a browser.
        run.events.put((event, redact(data, secrets=(config.api_key,))))

    runner = AgentRunner(
        client,
        registry,
        system_prompt=SYSTEM_PROMPT,
        trace=recorder,
        on_progress=on_progress,
        lesson_store=lesson_store,
    )

    history = manager.session(session_id) if mode == "chat" else None
    try:
        result = runner.run(task, history=history)
    except Exception as exc:  # defensive: never let a thread die silently
        run.events.put(("__error__", {"error": f"内部错误：{type(exc).__name__}: {exc}"}))
        return

    if mode == "chat":
        manager.set_session(session_id, list(result.messages))

    run.events.put(
        (
            "__done__",
            {
                "ok": result.ok,
                "summary": result.summary,
                "rounds": result.rounds,
                "tool_calls": result.tool_calls,
                "stop_reason": result.stop_reason,
                "usage": dict(result.usage),
                "trace_path": str(trace_path) if trace else None,
            },
        ),
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "CodingAgentWebUI/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        return

    # -- helpers ------------------------------------------------------------
    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name: str, content_type: str) -> None:
        path = STATIC_DIR / name
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._file("index.html", "text/html; charset=utf-8")
        elif path == "/style.css":
            self._file("style.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._file("app.js", "application/javascript; charset=utf-8")
        elif path == "/config":
            self._json(
                {
                    "has_api_key": bool(os.environ.get("OPENAI_API_KEY")),
                    "base_url": os.environ.get("OPENAI_BASE_URL", ""),
                    "model": os.environ.get("OPENAI_MODEL", ""),
                }
            )
        elif path == "/stream":
            self._stream(qs)
        elif path == "/replay":
            self._replay(qs)
        else:
            self.send_error(404)

    def _stream(self, qs: Mapping[str, list[str]]) -> None:
        run_id = (qs.get("run_id") or [""])[0]
        run = manager.get(run_id)
        if run is None:
            self._json({"error": "未知 run_id（可能已过期）"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self._sse({"type": "connected", "data": {"run_id": run_id}})
            while True:
                try:
                    event, data = run.events.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if event in {"__done__", "__error__"}:
                    self._sse({"type": event, "data": data})
                    self.close_connection = True
                    break
                self._sse({"type": event, "data": data})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse(self, obj: Any) -> None:
        line = json.dumps(obj, ensure_ascii=False, default=str)
        self.wfile.write(("data: " + line + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _replay(self, qs: Mapping[str, list[str]]) -> None:
        raw = (qs.get("path") or [""])[0]
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            self._json({"error": f"轨迹文件不存在：{raw}"}, 404)
            return
        if path.suffix.lower() not in {".jsonl", ".json", ".txt"}:
            self._json({"error": "仅支持 .jsonl / .json 轨迹文件"}, 400)
            return
        events: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError as exc:
            self._json({"error": f"读取失败：{exc}"}, 500)
            return
        self._json({"path": str(path), "events": events})

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/run":
            self._run()
        elif parsed.path == "/stop":
            self._stop()
        else:
            self.send_error(404)

    def _run(self) -> None:
        payload = self._body_json()
        task = str(payload.get("task") or "").strip()
        if not task:
            self._json({"error": "任务不能为空"}, 400)
            return

        workdir_raw = str(payload.get("workdir") or "").strip()
        workdir = Path(workdir_raw).expanduser().resolve() if workdir_raw else Path.cwd()
        try:
            workdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._json({"error": f"无法创建工作目录：{exc}"}, 400)
            return

        mode = payload.get("mode") or "single"
        if mode not in {"single", "chat"}:
            mode = "single"
        session_id = str(payload.get("session_id") or uuid.uuid4().hex[:12])
        trace = bool(payload.get("trace"))
        api_key = str(payload.get("api_key") or "").strip() or None
        base_url = str(payload.get("base_url") or "").strip() or None
        model = str(payload.get("model") or "").strip() or None

        run = manager.create()
        thread = threading.Thread(
            target=run_agent,
            kwargs={
                "run": run,
                "task": task,
                "workdir": workdir,
                "mode": mode,
                "session_id": session_id,
                "trace": trace,
                "api_key": api_key,
                "base_url": base_url,
                "model": model,
            },
            daemon=True,
        )
        thread.start()
        self._json({"run_id": run.run_id, "session_id": session_id})

    def _stop(self) -> None:
        payload = self._body_json()
        run_id = str(payload.get("run_id") or "")
        run = manager.get(run_id)
        if run is None:
            self._json({"error": "未知 run_id"}, 404)
            return
        run.cancel.set()
        self._json({"ok": True})


def main() -> int:
    parser = argparse.ArgumentParser(description="自研 Coding Agent 本地可视化运行台")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print(f"自研 Coding Agent 可视化运行台：{url}")
    print("按 Ctrl+C 停止服务。")
    if args.open:
        threading.Thread(
            target=lambda: (time.sleep(0.5), webbrowser.open(url)), daemon=True
        ).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
