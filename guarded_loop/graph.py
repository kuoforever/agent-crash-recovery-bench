"""用 LangGraph 重建受控执行回路。

对照目标是我自己那套 runtime 的四条约束：
  1. 工具集固定，参数与结果双向校验
  2. 结果判三态，不确定不重放
  3. 崩溃后能从检查点接着走
  4. 高风险动作先过人工审批

这个文件只关心"LangGraph 能表达其中哪几条"。
"""

from __future__ import annotations

import operator
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .tools import Ledger, build_registry, invoke, strictly_approved


class StepRecord(TypedDict):
    idx: int
    tool: str
    outcome: str
    note: str


class LoopState(TypedDict):
    task: str
    plan: list[dict[str, Any]]
    cursor: int
    # journal 用 operator.add 累加：LangGraph 的 reducer 机制在这里正好对上
    # 我原来手写的 append-only 账本。
    journal: Annotated[list[StepRecord], operator.add]
    stop_code: str
    approvals: dict[str, bool]


@dataclass
class GraphResources:
    """Own the SQLite handles used by one compiled graph."""

    saver: SqliteSaver
    checkpoint_conn: sqlite3.Connection
    ledger: Ledger
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.checkpoint_conn.close()
        finally:
            self.ledger.close()

    def __enter__(self) -> GraphResources:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def build_graph(
    thread_id: str,
    sink: Path,
    ledger_path: Path,
    ledger_enabled: bool,
    checkpoint_path: Path,
    auto_approve: bool = False,
) -> tuple[Any, GraphResources]:
    registry = build_registry(sink)
    ledger = Ledger(ledger_path, enabled=ledger_enabled)

    def plan_node(state: LoopState) -> dict[str, Any]:
        # 计划在这个基准里是确定性的：崩溃恢复语义要单独量，
        # 不能让模型的随机性混进来当噪声。真实模型的那条链路在 llm_run.py。
        return {}

    def gate_node(state: LoopState) -> dict[str, Any]:
        step = state["plan"][state["cursor"]]
        spec = registry.get(step["tool"])
        # Let act_node/invoke record an unknown tool as a deterministic, fail-closed
        # contract violation instead of raising KeyError before a journal entry exists.
        if spec is None:
            return {}
        if not spec.needs_approval:
            return {}
        if strictly_approved(auto_approve):
            return {"approvals": {**state["approvals"], str(state["cursor"]): True}}
        # 真·人工闸门：LangGraph 会在这里把图挂起，状态落进检查点，
        # 进程可以直接退出，之后用 Command(resume=...) 接着走。
        decision = interrupt({"ask": "approve?", "tool": step["tool"], "args": step["args"]})
        return {
            "approvals": {
                **state["approvals"],
                str(state["cursor"]): strictly_approved(decision),
            }
        }

    def act_node(state: LoopState) -> dict[str, Any]:
        i = state["cursor"]
        step = state["plan"][i]
        spec = registry.get(step["tool"])

        if (
            spec is not None
            and spec.needs_approval
            and not strictly_approved(state["approvals"].get(str(i), False))
        ):
            return {
                "stop_code": "APPROVAL_DENIED",
                "journal": [StepRecord(idx=i, tool=step["tool"], outcome="failed", note="denied")],
            }

        outcome, _result, note = invoke(registry, ledger, thread_id, i, step["tool"], step["args"])
        rec = StepRecord(idx=i, tool=step["tool"], outcome=outcome, note=note)

        if outcome == "uncertain":
            # 核心一条：不确定就停机，绝不换个姿势重来。
            return {"journal": [rec], "stop_code": "UNCERTAIN_HALT"}
        if outcome == "failed":
            return {"journal": [rec], "stop_code": "FAILED"}
        return {"journal": [rec], "cursor": i + 1}

    def route(state: LoopState) -> Literal["gate", "__end__"]:
        if state["stop_code"]:
            return "__end__"
        if state["cursor"] >= len(state["plan"]):
            return "__end__"
        return "gate"

    g = StateGraph(LoopState)
    g.add_node("plan", plan_node)
    g.add_node("gate", gate_node)
    g.add_node("act", act_node)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", route, {"gate": "gate", END: END})
    g.add_edge("gate", "act")
    g.add_conditional_edges("act", route, {"gate": "gate", END: END})

    # 不用 from_conn_string：它返回的是上下文管理器，出了作用域连接会被关掉。
    try:
        conn = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    except Exception:
        ledger.close()
        raise
    try:
        saver = SqliteSaver(conn)
        compiled = g.compile(checkpointer=saver)
    except Exception:
        try:
            conn.close()
        finally:
            ledger.close()
        raise
    return compiled, GraphResources(saver=saver, checkpoint_conn=conn, ledger=ledger)


def make_plan(n_steps: int) -> list[dict[str, Any]]:
    """交替读/写的计划：只有写入那半会产生副作用，是崩溃注入要盯的部分。"""
    plan: list[dict[str, Any]] = []
    for i in range(n_steps):
        if i % 2 == 0:
            plan.append({"tool": "write_note", "args": {"target": f"t{i}", "text": f"v{i}"}})
        else:
            plan.append({"tool": "read_status", "args": {"target": f"t{i}"}})
    return plan
