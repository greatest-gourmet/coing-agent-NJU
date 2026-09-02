"""让「人」在浏览器里亲自玩的猜数字小游戏（网页版·人工游玩）。

注意：这里运行的是由玩家自己在页面里输入数字、你来猜，
绝对不是 AI（coding agent）来玩、也不是 agent 替你 input()。

运行方式（项目根目录下执行）：:

    python game_webui/server.py --open

然后浏览器打开 http://127.0.0.1:8080/ 即可开始玩。
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

STATIC_DIR = Path(__file__).resolve().parent / "static"


class GameState:
    """一次会话的猜数字游戏状态。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.lo = 1
        self.hi = 100
        self.target = random.randint(self.lo, self.hi)
        self.attempts = 0
        self.active = True
        self.won = False
        self.hint = "我选好了 1~100 之间的一个数字，开始猜吧！"

    def guess(self, n: int) -> None:
        self.attempts += 1
        if n < self.target:
            self.hint = f"⬆ {n} 太小了，再猜大一点 ↑"
        elif n > self.target:
            self.hint = f"⬇ {n} 太大了，再猜小一点 ↓"
        else:
            self.hint = f"🎉 恭喜猜对！答案就是 {n}，你用了 {self.attempts} 次！"
            self.active = False
            self.won = True

    def snapshot(self) -> Dict[str, Any]:
        return {
            "lo": self.lo,
            "hi": self.hi,
            "attempts": self.attempts,
            "active": self.active,
            "won": self.won,
            "hint": self.hint,
        }


# 会话存储：session_id -> GameState
_sessions: Dict[str, GameState] = {}
_sessions_lock = threading.Lock()


def get_state(session_id: Optional[str]) -> tuple[str, GameState]:
    """取(或新建)会话状态，返回 (session_id, state)。"""
    with _sessions_lock:
        if session_id:
            st = _sessions.get(session_id)
            if st is not None:
                return session_id, st
        sid = uuid.uuid4().hex[:16]
        _sessions[sid] = GameState()
        return sid, _sessions[sid]


class Handler(BaseHTTPRequestHandler):
    server_version = "GuessGameWebUI/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

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

    def _parse_query_sid(self, qs: Dict[str, list[str]]) -> Optional[str]:
        return (qs.get("sid") or [None])[0]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._file("index.html", "text/html; charset=utf-8")
        elif path == "/style.css":
            self._file("style.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._file("app.js", "application/javascript; charset=utf-8")
        elif path == "/state":
            qs = parse_qs(parsed.query)
            sid, st = get_state(self._parse_query_sid(qs))
            snap = st.snapshot()
            snap["sid"] = sid
            self._json(snap)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query) if parsed.query else {}
        sid, st = get_state(self._parse_query_sid(qs))

        if parsed.path == "/guess":
            raw = str(self._body_json().get("guess") or "").strip()
            if st.active:
                try:
                    n = int(raw)
                except ValueError:
                    st.hint = "那不是整数呀，请输入像 50 这样的整数。"
                else:
                    if n < st.lo or n > st.hi:
                        st.hint = f"请在 {st.lo} 到 {st.hi} 之间输入。"
                    else:
                        st.guess(n)
            else:
                st.hint = "本局已结束，点「再来一局」开始新对局吧。"
        elif parsed.path == "/new":
            st.reset()
        else:
            self.send_error(404)
            return

        snap = st.snapshot()
        snap["sid"] = sid
        self._json(snap)

    def _body_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="供人工游玩的猜数字小游戏（网页版）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print("=" * 50)
    print("  🎮 猜数字小游戏（网页版·人工游玩）")
    print(f"  打开浏览器访问：{url}")
    print("  你来输入数字，我来判断大小 —— 全程由你操作！")
    print("  按 Ctrl+C 停止服务。")
    print("=" * 50)
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
