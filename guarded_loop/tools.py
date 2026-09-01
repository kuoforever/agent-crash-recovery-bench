"""工具层：固定注册表 + 参数/结果双向校验 + 意图账本。

这一层刻意不依赖 LangGraph，目的是能单独回答一个问题：
框架换掉之后，"哪些保证是框架给的、哪些是我自己给的"能不能分清。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Outcome = Literal["ok", "failed", "uncertain"]
LedgerState = Literal["fresh", "pending", "done"]


class ToolFailure(Exception):
    """确定性失败：工具明确拒绝，且保证没有产生副作用。可以安全中止。"""


class ToolUncertain(Exception):
    """派发出去了但没拿到回执。副作用是否发生未知——这类**不允许重放**。"""


# --- 崩溃注入钩子 -------------------------------------------------------------
# GL_CRASH="<step>:<phase>"，命中就硬退出。用 os._exit 而不是 raise，
# 是为了不跑 finally / atexit —— 真崩溃不会给你清理的机会。
def maybe_crash(phase: str, step: int) -> None:
    spec = os.environ.get("GL_CRASH")
    if not spec:
        return
    at_step, at_phase = spec.split(":")
    if int(at_step) == step and at_phase == phase:
        os._exit(70)


# --- 意图账本 -----------------------------------------------------------------
class Ledger:
    """副作用的意图账本。

    顺序是 记意图(pending) -> 执行副作用 -> 标记完成(done)。
    崩溃落在中间时，恢复后读到 pending 而不是 done，
    这时**唯一正确的动作是停下来报不确定**，不是重试。
    """

    def __init__(self, path: Path, enabled: bool = True):
        self.enabled = enabled
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS applied("
            " idem_key TEXT PRIMARY KEY, state TEXT NOT NULL, receipt TEXT)"
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def lookup(self, idem_key: str) -> tuple[LedgerState, str | None]:
        if not self.enabled:
            return ("fresh", None)
        row = self.conn.execute(
            "SELECT state, receipt FROM applied WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        return (row[0], row[1]) if row else ("fresh", None)

    def mark_pending(self, idem_key: str) -> None:
        if self.enabled:
            self.conn.execute(
                "INSERT OR IGNORE INTO applied(idem_key, state) VALUES(?, 'pending')",
                (idem_key,),
            )

    def mark_done(self, idem_key: str, receipt: str) -> None:
        if self.enabled:
            self.conn.execute(
                "UPDATE applied SET state='done', receipt=? WHERE idem_key=?",
                (receipt, idem_key),
            )

    def clear_pending(self, idem_key: str) -> None:
        """Forget an intent only when the tool guarantees no effect was applied."""
        if self.enabled:
            self.conn.execute(
                "DELETE FROM applied WHERE idem_key=? AND state='pending'", (idem_key,)
            )


# --- 工具契约 -----------------------------------------------------------------
class ContractModel(BaseModel):
    """Tool contracts reject fields that are not part of the frozen schema."""

    model_config = ConfigDict(extra="forbid")


class ReadStatusArgs(ContractModel):
    target: str = Field(min_length=1)


class ReadStatusResult(ContractModel):
    target: str
    status: str


class WriteNoteArgs(ContractModel):
    target: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=200)


class WriteNoteResult(ContractModel):
    receipt: str


class SubmitFormArgs(ContractModel):
    target: str = Field(min_length=1)
    payload: dict[str, Any]


class SubmitFormResult(ContractModel):
    receipt: str


class ToolSpec(BaseModel):
    name: str
    args_model: type[BaseModel]
    result_model: type[BaseModel]
    fn: Callable[..., dict[str, Any]]
    side_effect: bool
    needs_approval: bool

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")


def _sink_append(sink: Path, line: str) -> None:
    with open(sink, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def build_registry(sink: Path) -> dict[str, ToolSpec]:
    """固定注册表。运行期不允许增删——工具集是契约的一部分，不是配置。"""

    def read_status(target: str) -> dict[str, Any]:
        return {"target": target, "status": "idle"}

    def write_note(target: str, text: str) -> dict[str, Any]:
        _sink_append(sink, f"note::{target}::{text}")
        return {"receipt": f"note-{target}"}

    def submit_form(target: str, payload: dict[str, Any]) -> dict[str, Any]:
        _sink_append(sink, f"form::{target}::{json.dumps(payload, sort_keys=True)}")
        return {"receipt": f"form-{target}"}

    specs = [
        ToolSpec(
            name="read_status",
            args_model=ReadStatusArgs,
            result_model=ReadStatusResult,
            fn=read_status,
            side_effect=False,
            needs_approval=False,
        ),
        ToolSpec(
            name="write_note",
            args_model=WriteNoteArgs,
            result_model=WriteNoteResult,
            fn=write_note,
            side_effect=True,
            needs_approval=False,
        ),
        ToolSpec(
            name="submit_form",
            args_model=SubmitFormArgs,
            result_model=SubmitFormResult,
            fn=submit_form,
            side_effect=True,
            needs_approval=True,
        ),
    ]
    return {s.name: s for s in specs}


def idem_key(thread_id: str, step: int, tool: str, args: dict[str, Any]) -> str:
    """Hash schema-validated, JSON-compatible arguments for one logical step."""
    blob = json.dumps({"t": thread_id, "s": step, "n": tool, "a": args}, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def invoke(
    registry: dict[str, ToolSpec],
    ledger: Ledger,
    thread_id: str,
    step: int,
    tool: str,
    raw_args: dict[str, Any],
) -> tuple[Outcome, dict[str, Any] | None, str]:
    """执行一次工具调用，返回 (三态, 结果, 说明)。

    调用方拿到 uncertain 时**必须停机**，不允许换个参数重来。
    """
    spec = registry.get(tool)
    if spec is None:
        # 注册表之外的名字直接拒绝——模型编出一个工具名不算失败重试，算契约违例。
        return ("failed", None, f"unknown_tool:{tool}")

    # 入参校验：模型给的参数逐字段过 schema，不合规不进执行层。
    try:
        args = spec.args_model.model_validate(raw_args)
    except (TypeError, ValidationError) as e:
        return ("failed", None, f"args_rejected:{type(e).__name__}")

    canonical_args = args.model_dump(mode="json")
    key = idem_key(thread_id, step, tool, canonical_args)

    if spec.side_effect:
        state, receipt = ledger.lookup(key)
        if state == "done":
            # 已经做过且拿到回执，直接复用，不再触发副作用。
            return ("ok", {"receipt": receipt}, "replayed_from_ledger")
        if state == "pending":
            # 上次崩在"副作用已发出、回执未落账"之间。做没做过不知道。
            return ("uncertain", None, "pending_intent_found")
        ledger.mark_pending(key)

    maybe_crash("pre_apply", step)

    try:
        raw_result = spec.fn(**canonical_args)
    except ToolUncertain as e:
        return ("uncertain", None, f"dispatch_no_receipt:{e}")
    except ToolFailure as e:
        if spec.side_effect:
            # ToolFailure's contract guarantees that dispatch produced no effect, so the
            # pending intent can be removed and a later retry remains safe.
            ledger.clear_pending(key)
        return ("failed", None, f"tool_failed:{e}")

    # 副作用已经落地、账本还没标 done —— 崩溃注入的关键窗口就在这一行。
    maybe_crash("post_apply", step)

    # 出参校验：工具返回的东西也要过 schema，不能直接塞回状态里。
    try:
        result = spec.result_model.model_validate(raw_result)
    except (TypeError, ValidationError) as e:
        if spec.side_effect:
            # Dispatch returned, but without a valid receipt we cannot prove what happened.
            # Keep the pending intent so recovery cannot replay the side effect.
            return ("uncertain", None, f"invalid_receipt_after_dispatch:{type(e).__name__}")
        return ("failed", None, f"result_rejected:{type(e).__name__}")

    if spec.side_effect:
        ledger.mark_done(key, str(result.model_dump(mode="json").get("receipt", "")))

    maybe_crash("post_commit", step)
    return ("ok", result.model_dump(mode="json"), "ok")


def strictly_approved(decision: object) -> bool:
    """Accept only the literal JSON/Python boolean ``true`` as approval."""

    return type(decision) is bool and decision is True
