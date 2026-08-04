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
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

PY = str(Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe")
PHASES = ["pre_apply", "post_apply", "post_commit"]


def run_worker(run_dir: Path, thread: str, steps: int, ledger: str,
               resume: bool, crash: str | None, durability: str = "async") -> tuple[int, str]:
    env = dict(os.environ)
    if crash:
        env["GL_CRASH"] = crash
    else:
        env.pop("GL_CRASH", None)
    cmd = [PY, "-m", "guarded_loop.worker", "--run-dir", str(run_dir),
           "--thread", thread, "--steps", str(steps), "--ledger", ledger,
           "--durability", durability]
    if resume:
        cmd.append("--resume")
    p = subprocess.run(cmd, capture_output=True, text=True, env=env,
                       cwd=str(Path(__file__).resolve().parents[1]))
    return p.returncode, (p.stdout or "").strip() + (p.stderr or "").strip()


def trial(root: Path, idx: int, steps: int, ledger: str,
          durability: str = "async") -> dict:
    run_dir = root / f"run{idx:03d}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    # 只在有副作用的步骤上注入（偶数步是 write_note）
    side_steps = [i for i in range(steps) if i % 2 == 0]
    at_step = side_steps[idx % len(side_steps)]
    phase = PHASES[idx % len(PHASES)]

    rc1, _ = run_worker(run_dir, "T", steps, ledger, resume=False,
                        crash=f"{at_step}:{phase}", durability=durability)
    crashed = (rc1 == 70)

    rc2, out2 = run_worker(run_dir, "T", steps, ledger, resume=True,
                           crash=None, durability=durability)

    sink = run_dir / "sink.log"
    lines = sink.read_text(encoding="utf-8").splitlines() if sink.exists() else []
    dupes = sum(c - 1 for c in Counter(lines).values() if c > 1)

    stop_code = ""
    try:
        stop_code = json.loads(out2.splitlines()[-1]).get("stop_code", "")
    except Exception:
        stop_code = f"<resume_rc={rc2}>"

    return {"idx": idx, "crash_at": f"{at_step}:{phase}", "crashed": crashed,
            "dupes": dupes, "stop_code": stop_code, "sink_lines": len(lines)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--out", default="_bench")
    args = ap.parse_args()

    report: dict[str, object] = {"runs": args.runs, "steps_per_run": args.steps}
    # 三组对照：默认 async / 最强持久化 sync / 加自建账本。
    # sync 那组是为了排除"是不是我没开够持久化"这个反问。
    arms = [("off", "async"), ("off", "sync"), ("on", "async")]
    for ledger, durability in arms:
        arm = f"ledger_{ledger}__durability_{durability}"
        root = Path(args.out) / arm
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        trials = [trial(root, i, args.steps, ledger, durability)
                  for i in range(args.runs)]
        report[arm] = {
            "crash_confirmed": sum(t["crashed"] for t in trials),
            "runs_with_duplicate_side_effect": sum(1 for t in trials if t["dupes"] > 0),
            "total_duplicate_side_effects": sum(t["dupes"] for t in trials),
            "stop_codes": dict(Counter(t["stop_code"] for t in trials)),
            "trials": trials,
        }
        r = report[arm]
        print(f"[{arm}] crash={r['crash_confirmed']}/{args.runs} "
              f"runs_with_dupes={r['runs_with_duplicate_side_effect']} "
              f"dupes={r['total_duplicate_side_effects']} "
              f"stop_codes={r['stop_codes']}")

    Path(args.out, "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告写入 {Path(args.out, 'report.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
