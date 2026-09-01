from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from guarded_loop import crash_bench, eval_trace

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
def test_eval_missing_manifest_fails_without_creating_baseline(tmp_path: Path) -> None:
    manifest = tmp_path / "missing-manifest.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "guarded_loop.eval_trace",
            "--tmp",
            str(tmp_path / "eval"),
            "--manifest",
            str(manifest),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert process.returncode == 1, process.stdout + process.stderr
    assert "manifest 缺失" in process.stderr
    assert not manifest.exists()


@pytest.mark.integration
def test_eval_refuses_unowned_nonempty_tmp_without_deleting_it(tmp_path: Path) -> None:
    unowned = tmp_path / "not-owned"
    unowned.mkdir()
    sentinel = unowned / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "guarded_loop.eval_trace",
            "--tmp",
            str(unowned),
            "--manifest",
            str(PROJECT_ROOT / "eval_manifest.json"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 2
    assert "refusing to delete" in process.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_benchmark_returns_nonzero_when_any_trial_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid_trial(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "crashed": False,
            "dupes": 0,
            "stop_code": "<unparsed>",
            "valid": False,
            "invalid_reasons": ["crash_worker_rc:1"],
        }

    monkeypatch.setattr(crash_bench, "trial", invalid_trial)
    monkeypatch.setattr(
        sys,
        "argv",
        ["crash-bench", "--runs", "1", "--steps", "2", "--out", str(tmp_path / "out")],
    )
    assert crash_bench.main() == 1
    report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert report["invalid_trials"] == 3


def test_eval_refuses_to_freeze_a_failing_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        eval_trace,
        "CASES",
        [{"name": "forced failure", "fn": lambda _path: {"ok": False}, "expect": {"ok": True}}],
    )
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval-trace",
            "--tmp",
            str(tmp_path / "eval"),
            "--manifest",
            str(manifest),
            "--update-manifest",
        ],
    )
    assert eval_trace.main() == 1
    assert not manifest.exists()


def test_worker_result_parser_ignores_non_json_diagnostics() -> None:
    parsed = crash_bench._parse_worker_result(
        'warning before\n{"stop_code":"","cursor":2}\nwarning after'
    )
    assert parsed == {"stop_code": "", "cursor": 2}


@pytest.mark.integration
def test_benchmark_smoke_uses_current_interpreter_and_emits_metadata(tmp_path: Path) -> None:
    out = tmp_path / "bench"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "guarded_loop.crash_bench",
            "--runs",
            "3",
            "--steps",
            "2",
            "--out",
            str(out),
            "--worker-timeout",
            "30",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 2
    assert report["environment"]["python_executable"] == sys.executable
    assert report["implementation_sha256"]
    assert report["valid"] is True
    assert report["invalid_trials"] == 0


@pytest.mark.integration
def test_benchmark_rejects_invalid_dimensions(tmp_path: Path) -> None:
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "guarded_loop.crash_bench",
            "--runs",
            "0",
            "--out",
            str(tmp_path / "out"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 2
    assert not (tmp_path / "out" / "report.json").exists()
