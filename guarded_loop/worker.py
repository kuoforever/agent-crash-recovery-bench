"""一次运行 = 一个子进程。崩溃注入靠杀掉这个进程实现，不是抛异常。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .graph import build_graph, make_plan


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--thread", required=True)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--ledger", choices=["on", "off"], default="on")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--durability", choices=["sync", "async", "exit"], default="async")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    app, resources = build_graph(
        thread_id=args.thread,
        sink=run_dir / "sink.log",
        ledger_path=run_dir / "ledger.sqlite",
        ledger_enabled=(args.ledger == "on"),
        checkpoint_path=run_dir / "checkpoints.sqlite",
    )
    cfg = {"configurable": {"thread_id": args.thread}}

    try:
        if args.resume:
            # 恢复：不传新输入，LangGraph 从检查点里的状态接着跑。
            result = app.invoke(None, cfg, durability=args.durability)
        else:
            result = app.invoke(
                {
                    "task": "bench",
                    "plan": make_plan(args.steps),
                    "cursor": 0,
                    "journal": [],
                    "stop_code": "",
                    "approvals": {},
                },
                cfg,
                durability=args.durability,
            )
    finally:
        # A deliberate os._exit crash bypasses this block by design. Normal and resumed
        # workers close both SQLite handles explicitly so Windows can clean run dirs.
        resources.close()

    print(
        json.dumps(
            {
                "stop_code": result.get("stop_code", ""),
                "cursor": result.get("cursor", -1),
                "steps_done": len(result.get("journal", [])),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
