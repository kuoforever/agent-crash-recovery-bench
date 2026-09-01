from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from guarded_loop.dify_sink import EffectServer, EffectStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _serving(
    store: EffectStore, *, max_request_bytes: int = 16_384, max_concurrency: int = 4
) -> Iterator[EffectServer]:
    server = EffectServer(
        ("127.0.0.1", 0),
        store,
        max_request_bytes=max_request_bytes,
        max_concurrency=max_concurrency,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(server: EffectServer, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request(
            "POST", "/effect", body=body, headers={"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_store_serializes_concurrent_attempts_and_writes_atomic_marker(tmp_path: Path) -> None:
    store = EffectStore(tmp_path)
    payload = {"key": "same-key", "token": "same-token"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(pool.map(lambda _index: store.apply(payload), range(24)))

    assert sorted(event["attempt"] for event in events) == list(range(1, 25))
    stored = store.events()
    assert sorted(event["attempt"] for event in stored) == list(range(1, 25))
    marker = json.loads((tmp_path / "same-token.applied").read_text(encoding="utf-8"))
    assert marker["attempt"] == 24
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.integration
def test_http_sink_rejects_oversized_body_before_effect(tmp_path: Path) -> None:
    store = EffectStore(tmp_path)
    with _serving(store, max_request_bytes=32) as server:
        status, response = _post(server, {"key": "x" * 120})
    assert status == 413
    assert response["error"] == "request_too_large"
    assert store.events() == []


@pytest.mark.integration
def test_http_sink_bounds_concurrency_with_503(tmp_path: Path) -> None:
    store = EffectStore(tmp_path)
    first_result: list[tuple[int, dict[str, object]]] = []
    with _serving(store, max_concurrency=1) as server:
        first = threading.Thread(
            target=lambda: first_result.append(
                _post(
                    server,
                    {
                        "key": "blocked",
                        "token": "blocked",
                        "block": True,
                        "block_timeout": 5,
                    },
                )
            )
        )
        first.start()
        marker = tmp_path / "blocked.applied"
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()

        status, response = _post(server, {"key": "overload"})
        assert status == 503
        assert response["error"] == "server_busy"

        (tmp_path / "blocked.release").write_text("release", encoding="utf-8")
        first.join(timeout=5)
    assert first_result[0][0] == 200
    assert [event["key"] for event in store.events()] == ["blocked"]


@pytest.mark.integration
def test_non_loopback_bind_requires_explicit_unsafe_switch(tmp_path: Path) -> None:
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "guarded_loop.dify_sink",
            "--host",
            "0.0.0.0",
            "--port",
            "0",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 2
    assert "--unsafe-allow-non-loopback" in process.stderr
    assert not (tmp_path / "state").exists()
