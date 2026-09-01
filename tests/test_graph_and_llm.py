from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langgraph.types import Command

from guarded_loop.graph import build_graph
from guarded_loop.llm_run import run_model_loop
from guarded_loop.tools import Ledger, ToolUncertain, build_registry


class FakeLlm:
    def __init__(self, replies: list[AIMessage]) -> None:
        self.replies = replies
        self.calls = 0

    def invoke(self, _messages: list[BaseMessage]) -> AIMessage:
        reply = self.replies[self.calls]
        self.calls += 1
        return reply


def _initial_state(plan: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "task": "test",
        "plan": plan,
        "cursor": 0,
        "journal": [],
        "stop_code": "",
        "approvals": {},
    }


def test_graph_string_false_denies_approval(tmp_path: Path) -> None:
    app, resources = build_graph(
        "T",
        tmp_path / "sink.log",
        tmp_path / "ledger.sqlite",
        True,
        tmp_path / "checkpoint.sqlite",
        auto_approve=False,
    )
    config = {"configurable": {"thread_id": "T"}}
    plan = [{"tool": "submit_form", "args": {"target": "t", "payload": {"a": 1}}}]
    try:
        paused = app.invoke(_initial_state(plan), config)
        assert "__interrupt__" in paused
        result = app.invoke(Command(resume="false"), config)
    finally:
        resources.close()
    assert result["stop_code"] == "APPROVAL_DENIED"
    assert not (tmp_path / "sink.log").exists()


def test_graph_unknown_tool_fails_closed_without_key_error(tmp_path: Path) -> None:
    app, resources = build_graph(
        "T",
        tmp_path / "sink.log",
        tmp_path / "ledger.sqlite",
        True,
        tmp_path / "checkpoint.sqlite",
    )
    try:
        result = app.invoke(
            _initial_state([{"tool": "delete_everything", "args": {"target": "t"}}]),
            {"configurable": {"thread_id": "T"}},
        )
    finally:
        resources.close()
    assert result["stop_code"] == "FAILED"
    assert result["journal"][-1]["note"] == "unknown_tool:delete_everything"
    assert not (tmp_path / "sink.log").exists()


def test_llm_path_denies_high_risk_tool_by_default(tmp_path: Path) -> None:
    sink = tmp_path / "sink.log"
    llm = FakeLlm(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_form",
                        "args": {"target": "t", "payload": {"a": 1}},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        trace = run_model_loop(
            llm,
            [SystemMessage("test")],
            build_registry(sink),
            ledger,
            max_turns=3,
        )
    assert trace[-1] == {"turn": 0, "type": "halt", "stop_code": "APPROVAL_DENIED"}
    assert llm.calls == 1
    assert not sink.exists()


def test_llm_path_executes_explicitly_approved_tool_once(tmp_path: Path) -> None:
    sink = tmp_path / "sink.log"
    llm = FakeLlm(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_form",
                        "args": {"target": "t", "payload": {"a": 1}},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        trace = run_model_loop(
            llm,
            [SystemMessage("test")],
            build_registry(sink),
            ledger,
            max_turns=3,
            approved_tools=frozenset({"submit_form"}),
        )
    assert [item["outcome"] for item in trace if item["type"] == "tool"] == ["ok"]
    assert len(sink.read_text(encoding="utf-8").splitlines()) == 1


def test_llm_uncertain_halts_entire_outer_loop_and_skips_later_calls(tmp_path: Path) -> None:
    sink = tmp_path / "sink.log"
    registry = build_registry(sink)
    write_spec = registry["write_note"]

    def dispatch_without_receipt(target: str, text: str) -> dict[str, Any]:
        raise ToolUncertain(f"lost:{target}:{text}")

    registry["write_note"] = write_spec.model_copy(update={"fn": dispatch_without_receipt})
    llm = FakeLlm(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_note",
                        "args": {"target": "t", "text": "v"},
                        "id": "call-1",
                        "type": "tool_call",
                    },
                    {
                        "name": "read_status",
                        "args": {"target": "must-not-run"},
                        "id": "call-2",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="must-not-be-requested"),
        ]
    )
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        trace = run_model_loop(
            llm,
            [SystemMessage("test")],
            registry,
            ledger,
            max_turns=3,
        )
    assert [item.get("tool") for item in trace if item["type"] == "tool"] == ["write_note"]
    assert trace[-1]["stop_code"] == "UNCERTAIN_HALT"
    assert llm.calls == 1
