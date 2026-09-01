"""崩溃注入基准：在 LangGraph 的检查点之上，量"副作用会不会被重复执行"。

做法和我自己 runtime 里那套一致——
子进程跑到指定步骤硬退出（os._exit，不跑 finally），父进程用同一个 thread_id 恢复，
最后数落地的副作用有没有重复。

两组对照：
  ledger=off  只有 LangGraph 自带的检查点
  ledger=on   检查点 + 我自己的意图账本（pending/done 两段式）
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PHASES = ["pre_apply", "post_apply", "post_commit"]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class WorkerRun:
    returncode: int | None
    output: str
    timed_out: bool
    duration_seconds: float


def _parse_worker_result(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "stop_code" in candidate:
            return candidate
    raise ValueError("worker output contains no result object")


def run_worker(
    run_dir: Path,
    thread: str,
    steps: int,
    ledger: str,
    resume: bool,
    crash: str | None,
    durability: str = "async",
    timeout: float = 60.0,
) -> WorkerRun:
    env = dict(os.environ)
    if crash:
        env["GL_CRASH"] = crash
    else:
        env.pop("GL_CRASH", None)
    cmd = [
        sys.executable,
        "-m",
        "guarded_loop.worker",
        "--run-dir",
        str(run_dir),
        "--thread",
        thread,
        "--steps",
        str(steps),
        "--ledger",
        ledger,
        "--durability",
        durability,
    ]
    if resume:
        cmd.append("--resume")
    started = time.monotonic()
    try:
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(PROJECT_ROOT),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return WorkerRun(
            returncode=None,
            output="\n".join(part.strip() for part in (stdout, stderr) if part),
            timed_out=True,
            duration_seconds=time.monotonic() - started,
        )
    return WorkerRun(
        returncode=process.returncode,
        output="\n".join(
            part.strip() for part in (process.stdout or "", process.stderr or "") if part
        ),
        timed_out=False,
        duration_seconds=time.monotonic() - started,
    )


def trial(
    root: Path,
    idx: int,
    steps: int,
    ledger: str,
    durability: str = "async",
    worker_timeout: float = 60.0,
) -> dict[str, Any]:
    run_dir = root / f"run{idx:03d}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    # 只在有副作用的步骤上注入（偶数步是 write_note）
    side_steps = [i for i in range(steps) if i % 2 == 0]
    at_step = side_steps[idx % len(side_steps)]
    phase = PHASES[idx % len(PHASES)]

    crash_run = run_worker(
        run_dir,
        "T",
        steps,
        ledger,
        resume=False,
        crash=f"{at_step}:{phase}",
        durability=durability,
        timeout=worker_timeout,
    )
    crashed = crash_run.returncode == 70 and not crash_run.timed_out

    resume_run = run_worker(
        run_dir,
        "T",
        steps,
        ledger,
        resume=True,
        crash=None,
        durability=durability,
        timeout=worker_timeout,
    )

    sink = run_dir / "sink.log"
    lines = sink.read_text(encoding="utf-8").splitlines() if sink.exists() else []
    dupes = sum(c - 1 for c in Counter(lines).values() if c > 1)

    stop_code = ""
    invalid_reasons: list[str] = []
    if crash_run.timed_out:
        invalid_reasons.append("crash_worker_timeout")
    elif crash_run.returncode != 70:
        invalid_reasons.append(f"crash_worker_rc:{crash_run.returncode}")
    if resume_run.timed_out:
        invalid_reasons.append("resume_worker_timeout")
    elif resume_run.returncode != 0:
        invalid_reasons.append(f"resume_worker_rc:{resume_run.returncode}")

    try:
        worker_result = _parse_worker_result(resume_run.output)
        stop_code = str(worker_result.get("stop_code", ""))
    except (TypeError, ValueError) as exc:
        stop_code = f"<unparsed:{type(exc).__name__}>"
        invalid_reasons.append(f"resume_output_invalid:{type(exc).__name__}")

    if ledger == "on":
        expected_stop = "UNCERTAIN_HALT" if phase in {"pre_apply", "post_apply"} else ""
        if stop_code != expected_stop:
            invalid_reasons.append(
                f"ledger_stop_code:{stop_code or '<empty>'}!={expected_stop or '<empty>'}"
            )
        if dupes != 0:
            invalid_reasons.append(f"ledger_duplicate_effects:{dupes}")
    elif stop_code != "":
        invalid_reasons.append(f"checkpoint_only_stop_code:{stop_code}")

    return {
        "idx": idx,
        "crash_at": f"{at_step}:{phase}",
        "crashed": crashed,
        "dupes": dupes,
        "stop_code": stop_code,
        "sink_lines": len(lines),
        "valid": not invalid_reasons,
        "invalid_reasons": invalid_reasons,
        "crash_worker": asdict(crash_run),
        "resume_worker": asdict(resume_run),
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _implementation_hash() -> str:
    digest = hashlib.sha256()
    for relative in (
        "guarded_loop/tools.py",
        "guarded_loop/graph.py",
        "guarded_loop/worker.py",
        "guarded_loop/crash_bench.py",
    ):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        normalized = "\n".join(source.splitlines()) + "\n"
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(normalized.encode("utf-8") + b"\0")
    return digest.hexdigest()


def _git_metadata() -> dict[str, Any]:
    result: dict[str, Any] = {"commit": None, "dirty": None}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        result = {"commit": commit.stdout.strip(), "dirty": bool(status.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def _dependency_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for distribution in ("langgraph", "langgraph-checkpoint-sqlite", "pydantic"):
        try:
            result[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            result[distribution] = None
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=_positive_int, default=30)
    ap.add_argument("--steps", type=_positive_int, default=20)
    ap.add_argument("--out", default="_bench")
    ap.add_argument("--worker-timeout", type=_positive_float, default=60.0)
    args = ap.parse_args()

    started = time.monotonic()
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "runs": args.runs,
        "steps_per_run": args.steps,
        "worker_timeout_seconds": args.worker_timeout,
        "implementation_sha256": _implementation_hash(),
        "environment": {
            "python_executable": sys.executable,
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "dependencies": _dependency_versions(),
            "git": _git_metadata(),
        },
    }
    invalid_trials = 0
    # 三组对照：默认 async / 最强持久化 sync / 加自建账本。
    # sync 那组是为了排除"是不是我没开够持久化"这个反问。
    arms = [("off", "async"), ("off", "sync"), ("on", "async")]
    for ledger, durability in arms:
        arm = f"ledger_{ledger}__durability_{durability}"
        root = Path(args.out) / arm
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        trials = [
            trial(
                root,
                i,
                args.steps,
                ledger,
                durability,
                worker_timeout=args.worker_timeout,
            )
            for i in range(args.runs)
        ]
        arm_invalid = sum(1 for item in trials if not item["valid"])
        invalid_trials += arm_invalid
        report[arm] = {
            "crash_confirmed": sum(t["crashed"] for t in trials),
            "runs_with_duplicate_side_effect": sum(1 for t in trials if t["dupes"] > 0),
            "total_duplicate_side_effects": sum(t["dupes"] for t in trials),
            "stop_codes": dict(Counter(t["stop_code"] for t in trials)),
            "valid": arm_invalid == 0,
            "invalid_trials": arm_invalid,
            "trials": trials,
        }
        r = report[arm]
        print(
            f"[{arm}] crash={r['crash_confirmed']}/{args.runs} "
            f"runs_with_dupes={r['runs_with_duplicate_side_effect']} "
            f"dupes={r['total_duplicate_side_effects']} "
            f"stop_codes={r['stop_codes']} invalid={arm_invalid}"
        )

    report["duration_seconds"] = time.monotonic() - started
    report["valid"] = invalid_trials == 0
    report["invalid_trials"] = invalid_trials
    Path(args.out, "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n报告写入 {Path(args.out, 'report.json')}")
    if invalid_trials:
        print(f"基准无效：{invalid_trials} 个 trial 未满足不变量", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
