"""Dify 对照实验用的可观测 HTTP 副作用 sink。

每次 ``POST /effect`` 都先把一条 JSON 记录 fsync 到 ``events.jsonl``，再返回调用方指定的
HTTP 状态。传入 ``block=true`` 时，副作用落地后会阻塞到对应 release 文件出现，便于在
"副作用已发生、节点尚未返回"的窗口杀掉 Dify 进程。

这个服务只用于本地实验，不提供认证，也不应暴露到公网。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import threading
import time
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


class EffectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = root / "events.jsonl"
        self._lock = threading.Lock()
        self._attempts: Counter[str] = Counter()
        if self.events_path.exists():
            for line in self.events_path.read_text(encoding="utf-8").splitlines():
                try:
                    self._attempts[str(json.loads(line)["key"])] += 1
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("key", "")).strip()
        if not TOKEN_RE.fullmatch(key):
            raise ValueError("key must match [A-Za-z0-9_.-]{1,120}")
        token = str(payload.get("token") or key)
        if not TOKEN_RE.fullmatch(token):
            raise ValueError("token must match [A-Za-z0-9_.-]{1,120}")

        with self._lock:
            self._attempts[key] += 1
            event = {
                "key": key,
                "token": token,
                "attempt": self._attempts[key],
                "applied_at": time.time(),
                "pid": os.getpid(),
            }
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

        marker = self.root / f"{token}.applied"
        marker.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        return event

    def wait_for_release(self, token: str, timeout: float) -> bool:
        release = self.root / f"{token}.release"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if release.exists():
                return True
            time.sleep(0.05)
        return False

    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        result: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result


class EffectHandler(BaseHTTPRequestHandler):
    server: "EffectServer"

    def _json(self, status: int, body: dict[str, Any] | list[dict[str, Any]]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path == "/events":
            self._json(HTTPStatus.OK, self.server.store.events())
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/effect":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")

            # Validate every response-control field before applying the effect.  Otherwise a
            # malformed status/timeout could commit the effect and then return a misleading 400.
            block = _parse_bool(payload.get("block", False))
            timeout = float(payload.get("block_timeout", 300))
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("block_timeout must be a finite non-negative number")
            response_status = int(payload.get("status", 200))
            if response_status < 100 or response_status > 599:
                raise ValueError("status must be between 100 and 599")

            event = self.server.store.apply(payload)
            if block:
                event["released"] = self.server.store.wait_for_release(event["token"], timeout)
            self._json(response_status, event)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        return


class EffectServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: EffectStore) -> None:
        super().__init__(address, EffectHandler)
        self.store = store


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("block must be a boolean")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()

    server = EffectServer((args.host, args.port), EffectStore(Path(args.state_dir)))
    print(json.dumps({"listening": f"http://{args.host}:{args.port}", "state_dir": args.state_dir}))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
