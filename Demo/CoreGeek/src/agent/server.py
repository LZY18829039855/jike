import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .brain import decide

LOGGER = logging.getLogger(__name__)
DECIDE_LOCK = threading.Lock()


def _clip(text: str, limit: int = 1200) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...<truncated>...\n" + text[-20:]


def _log_task_debug(payload: dict[str, Any], decision: dict[str, Any]) -> None:
    """把自进化相关上下文打进 stdout，便于从我方日志反查 check 报错。"""
    round_no = payload.get("roundNo")
    phase = (payload.get("phaseTask") or "").strip()
    last = (payload.get("lastCmdResult") or "").strip()
    execute = (decision.get("executeCmd") or "").strip()
    prompt = (decision.get("prompt") or "").strip()
    if not (phase or last or execute or prompt):
        return
    if phase:
        tip = phase.replace("\n", " ")
        LOGGER.info("round %s task %s", round_no, tip[:160])
    if last:
        # 工程题重点：CHECK# / FIX# / TOKEN / FAIL
        LOGGER.info("round %s sandbox %s", round_no, _clip(last, 1500))
    if execute:
        LOGGER.info("round %s exec %s", round_no, _clip(execute, 500))
    if prompt:
        LOGGER.info("round %s prompt_len=%s", round_no, len(prompt))


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
            try:
                _log_task_debug(payload, decision)
            except Exception:
                LOGGER.exception("task debug log failed")
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
