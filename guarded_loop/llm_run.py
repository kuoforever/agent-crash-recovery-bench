"""真实模型链路：ChatOpenAI + bind_tools，在同一套契约下跑。

崩溃基准刻意用确定性计划（模型的随机性会污染恢复语义的测量）；
这个文件负责的是另一半——证明工具契约层能挂真模型，且模型给的参数
一样要逐字段过 schema 才准进执行层。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI

from .tools import Ledger, ToolSpec, build_registry, invoke, strictly_approved

SYSTEM = (
    "你是一个桌面执行代理。只能使用给定的工具，一次给一个调用。"
    "read_status 用来观察，write_note 用来落笔记，submit_form 需要审批。"
)


def run_model_loop(
    llm: Any,
    msgs: list[BaseMessage],
    registry: dict[str, ToolSpec],
    ledger: Ledger,
    *,
    max_turns: int,
    approved_tools: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Execute model tool calls through the same contract and approval boundary."""

    trace: list[dict[str, Any]] = []
    for turn in range(max_turns):
        ai = llm.invoke(msgs)
        msgs.append(ai)
        calls = getattr(ai, "tool_calls", []) or []
        if not calls:
            trace.append({"turn": turn, "type": "final", "text": (ai.content or "")[:200]})
            return trace

        for call in calls:
            tool = str(call["name"])
            raw_args = call["args"]
            spec = registry.get(tool)
            if spec is not None and spec.needs_approval:
                approved = strictly_approved(tool in approved_tools)
                if not approved:
                    trace.append(
                        {
                            "turn": turn,
                            "type": "tool",
                            "tool": tool,
                            "args": raw_args,
                            "outcome": "failed",
                            "note": "approval_denied",
                        }
                    )
                    trace.append({"turn": turn, "type": "halt", "stop_code": "APPROVAL_DENIED"})
                    return trace

            outcome, result, note = invoke(registry, ledger, "llm", turn, tool, raw_args)
            trace.append(
                {
                    "turn": turn,
                    "type": "tool",
                    "tool": tool,
                    "args": raw_args,
                    "outcome": outcome,
                    "note": note,
                }
            )
            msgs.append(
                ToolMessage(
                    content=json.dumps({"outcome": outcome, "result": result}, ensure_ascii=False),
                    tool_call_id=call["id"],
                )
            )
            if outcome == "uncertain":
                trace.append({"turn": turn, "type": "halt", "stop_code": "UNCERTAIN_HALT"})
                # Return from the whole model loop, not merely the current tool-call loop.
                return trace

    return trace


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="_llm")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--task", default="先看一下 t0 的状态，然后给 t0 写一条内容为 hello 的笔记。")
    ap.add_argument("--max-turns", type=int, default=4)
    ap.add_argument(
        "--approve-tool",
        action="append",
        default=[],
        metavar="NAME",
        help="explicitly approve one needs_approval tool for this run (repeatable)",
    )
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    registry = build_registry(run_dir / "sink.log")
    ledger = Ledger(run_dir / "ledger.sqlite", enabled=True)

    approved_tools = frozenset(args.approve_tool)
    invalid_approvals = sorted(
        name for name in approved_tools if name not in registry or not registry[name].needs_approval
    )
    if invalid_approvals:
        ledger.close()
        ap.error(
            "--approve-tool only accepts registered needs_approval tools: "
            + ", ".join(invalid_approvals)
        )

    # 把固定注册表原样暴露给模型：工具集是契约，不因为换了模型就变。
    lc_tools = [
        StructuredTool.from_function(
            name=spec.name,
            description=f"{spec.name} (side_effect={spec.side_effect})",
            args_schema=spec.args_model,
            func=lambda **kw: "dispatched",  # 真正的执行走下面的 invoke，不交给 LangChain
        )
        for spec in registry.values()
    ]

    llm = ChatOpenAI(model=args.model, temperature=0).bind_tools(lc_tools)
    msgs: list[BaseMessage] = [SystemMessage(SYSTEM), HumanMessage(args.task)]
    try:
        trace = run_model_loop(
            llm,
            msgs,
            registry,
            ledger,
            max_turns=args.max_turns,
            approved_tools=approved_tools,
        )
    finally:
        ledger.close()

    out = {
        "model": args.model,
        "task": args.task,
        "approved_tools": sorted(approved_tools),
        "trace": trace,
    }
    (run_dir / "trace.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Console output may be redirected through a legacy Windows code page. Keep the
    # persisted trace human-readable while making stdout ASCII-safe and machine-readable.
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
