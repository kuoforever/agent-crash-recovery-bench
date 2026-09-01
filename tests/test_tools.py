from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from guarded_loop.tools import (
    Ledger,
    ToolFailure,
    WriteNoteResult,
    build_registry,
    idem_key,
    invoke,
    strictly_approved,
)


def _sink_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_only_literal_true_is_approval() -> None:
    assert strictly_approved(True)
    for value in (False, "true", "false", 1, {"approved": True}, None):
        assert not strictly_approved(value)


def test_contract_rejects_extra_args_before_effect(tmp_path: Path) -> None:
    sink = tmp_path / "sink.log"
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        outcome, result, note = invoke(
            build_registry(sink),
            ledger,
            "T",
            0,
            "write_note",
            {"target": "t", "text": "v", "ignored": "not-in-contract"},
        )
    assert (outcome, result) == ("failed", None)
    assert note.startswith("args_rejected:")
    assert _sink_lines(sink) == []


def test_canonical_argument_order_replays_one_effect(tmp_path: Path) -> None:
    sink = tmp_path / "sink.log"
    registry = build_registry(sink)
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        first = invoke(registry, ledger, "T", 0, "write_note", {"target": "t", "text": "v"})
        second = invoke(registry, ledger, "T", 0, "write_note", {"text": "v", "target": "t"})
    assert first[0] == "ok"
    assert second[0:3:2] == ("ok", "replayed_from_ledger")
    assert len(_sink_lines(sink)) == 1


def test_tool_failure_clears_pending_for_safe_retry(tmp_path: Path) -> None:
    sink = tmp_path / "sink.log"
    registry = build_registry(sink)
    spec = registry["write_note"]

    def reject_before_effect(target: str, text: str) -> dict[str, Any]:
        raise ToolFailure(f"reject:{target}:{text}")

    registry["write_note"] = spec.model_copy(update={"fn": reject_before_effect})
    args = {"target": "t", "text": "v"}
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        first = invoke(registry, ledger, "T", 0, "write_note", args)
        second = invoke(registry, ledger, "T", 0, "write_note", args)
        state = ledger.lookup(idem_key("T", 0, "write_note", args))[0]
    assert first[0] == second[0] == "failed"
    assert state == "fresh"
    assert _sink_lines(sink) == []


def test_invalid_result_after_effect_is_uncertain_and_not_replayed(tmp_path: Path) -> None:
    sink = tmp_path / "sink.log"
    registry = build_registry(sink)
    spec = registry["write_note"]

    def apply_with_bad_receipt(target: str, text: str) -> dict[str, Any]:
        with sink.open("a", encoding="utf-8") as handle:
            handle.write(f"note::{target}::{text}\n")
        return {"not_a_receipt": True}

    registry["write_note"] = spec.model_copy(
        update={"fn": apply_with_bad_receipt, "result_model": WriteNoteResult}
    )
    args = {"target": "t", "text": "v"}
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        first = invoke(registry, ledger, "T", 0, "write_note", args)
        second = invoke(registry, ledger, "T", 0, "write_note", args)
    assert first[0] == second[0] == "uncertain"
    assert first[2].startswith("invalid_receipt_after_dispatch:")
    assert second[2] == "pending_intent_found"
    assert len(_sink_lines(sink)) == 1


def test_ledger_close_releases_sqlite_connection(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.close()
    try:
        ledger.conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("closed ledger connection remained usable")
