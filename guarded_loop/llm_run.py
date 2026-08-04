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

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI

from .tools import Ledger, build_registry, invoke

SYSTEM = (
    "你是一个桌面执行代理。只能使用给定的工具，一次给一个调用。"
    "read_status 用来观察，write_note 用来落笔记，submit_form 需要审批。"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="_llm")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--task", default="先看一下 t0 的状态，然后给 t0 写一条内容为 hello 的笔记。")
    ap.add_argument("--max-turns", type=int, default=4)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    registry = build_registry(run_dir / "sink.log")
    ledger = Ledger(run_dir / "ledger.sqlite", enabled=True)

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
    msgs = [SystemMessage(SYSTEM), HumanMessage(args.task)]
    trace: list[dict] = []

    for turn in range(args.max_turns):
        ai = llm.invoke(msgs)
        msgs.append(ai)
        calls = getattr(ai, "tool_calls", []) or []
        if not calls:
            trace.append({"turn": turn, "type": "final", "text": (ai.content or "")[:200]})
            break
        for call in calls:
            outcome, result, note = invoke(
                registry, ledger, "llm", turn, call["name"], call["args"]
            )
            trace.append({"turn": turn, "type": "tool", "tool": call["name"],
                          "args": call["args"], "outcome": outcome, "note": note})
            msgs.append(ToolMessage(
                content=json.dumps({"outcome": outcome, "result": result},
                                   ensure_ascii=False),
                tool_call_id=call["id"]))
            if outcome == "uncertain":
                trace.append({"turn": turn, "type": "halt", "stop_code": "UNCERTAIN_HALT"})
                break

    out = {"model": args.model, "task": args.task, "trace": trace}
    (run_dir / "trace.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
