import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .brain import decide

LOGGER = logging.getLogger(__name__)
DECIDE_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
            # 决策器含跨回合记忆；必须按请求串行更新，避免状态交叉污染。
            with DECIDE_LOCK:
                decision = decide(payload)
            if "roleCommandMap" not in decision:
                decision = {
                    "roleCommandMap": decision,
                    "prompt": "",
                    "executeCmd": "",
                }
            decision.setdefault("prompt", "")
            decision.setdefault("executeCmd", "")
            LOGGER.info(
                "round %s -> %s",
                payload.get("roundNo"),
                decision.get("roleCommandMap"),
            )
            body = json.dumps(decision, ensure_ascii=False).encode("utf-8")
        except Exception:
            LOGGER.exception("decision failed")
            body = b'{"roleCommandMap":{},"prompt":"","executeCmd":""}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(port: int) -> None:
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
