"""按 trace 判定的确定性评测。

判据是**调用序列 + 停止码 + 副作用条数**，不判模型说了什么。
每个 case 的期望值用 SHA-256 冻进 manifest：改实现的时候如果顺手把
不过的用例调松，manifest 对不上会直接报出来。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langgraph.types import Command

from .graph import build_graph
from .tools import (
    Ledger,
    ToolFailure,
    ToolSpec,
    WriteNoteResult,
    build_registry,
    idem_key,
    invoke,
)

CaseFn = Callable[[Path], dict[str, Any]]
CASES: list[dict[str, Any]] = []
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_FILES = (
    "guarded_loop/tools.py",
    "guarded_loop/graph.py",
    "guarded_loop/llm_run.py",
    "guarded_loop/eval_trace.py",
)
EVAL_MARKER = ".guarded-loop-eval-root"
EVAL_MARKER_CONTENT = "guarded_loop.eval_trace:v1\n"


def _configure_safe_stdio() -> None:
    """Keep redirected Windows consoles from crashing on Chinese diagnostics."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


def case(name: str, expect: dict[str, Any]) -> Callable[[CaseFn], CaseFn]:
    def deco(fn: CaseFn) -> CaseFn:
        CASES.append({"name": name, "fn": fn, "expect": expect})
        return fn

    return deco


def _fresh(tmp: Path, name: str) -> Path:
    if not re.fullmatch(r"c\d{2}", name):
        raise ValueError(f"unsafe case directory name: {name}")
    d = tmp / name
    if d.resolve().parent != tmp.resolve():
        raise ValueError(f"case directory escapes eval root: {d}")
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


def _prepare_eval_root(tmp: Path) -> Path:
    """Claim an eval directory before deleting any generated case subtree."""

    resolved = tmp.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"eval tmp is not a directory: {resolved}")
    if not resolved.exists():
        resolved.mkdir(parents=True)

    marker = resolved / EVAL_MARKER
    if marker.exists():
        if marker.read_text(encoding="utf-8") != EVAL_MARKER_CONTENT:
            raise ValueError(f"eval tmp has an invalid ownership marker: {resolved}")
    else:
        existing = list(resolved.iterdir())
        if existing:
            raise ValueError(f"refusing to delete from unowned non-empty eval tmp: {resolved}")
        marker.write_text(EVAL_MARKER_CONTENT, encoding="utf-8")
    return resolved


@contextmanager
def _contracts(d: Path) -> Iterator[tuple[dict[str, ToolSpec], Ledger]]:
    ledger = Ledger(d / "l.sqlite")
    try:
        yield build_registry(d / "sink.log"), ledger
    finally:
        ledger.close()


def _sink_lines(d: Path) -> int:
    p = d / "sink.log"
    return len(p.read_text(encoding="utf-8").splitlines()) if p.exists() else 0


# --- 契约层 case ---------------------------------------------------------------
@case("注册表之外的工具名被拒", {"outcome": "failed", "note_prefix": "unknown_tool", "sink": 0})
def c_unknown_tool(d: Path) -> dict[str, Any]:
    with _contracts(d) as (reg, led):
        o, _, n = invoke(reg, led, "T", 0, "delete_everything", {"target": "x"})
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


@case("参数不合契约时不进执行层", {"outcome": "failed", "note_prefix": "args_rejected", "sink": 0})
def c_bad_args(d: Path) -> dict[str, Any]:
    with _contracts(d) as (reg, led):
        o, _, n = invoke(reg, led, "T", 0, "write_note", {"target": "", "text": "x"})
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


@case("超长文本被参数契约挡下", {"outcome": "failed", "note_prefix": "args_rejected", "sink": 0})
def c_too_long(d: Path) -> dict[str, Any]:
    with _contracts(d) as (reg, led):
        o, _, n = invoke(reg, led, "T", 0, "write_note", {"target": "t", "text": "x" * 500})
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


@case(
    "契约外字段被拒且不影响幂等键", {"outcome": "failed", "note_prefix": "args_rejected", "sink": 0}
)
def c_extra_args(d: Path) -> dict[str, Any]:
    with _contracts(d) as (reg, led):
        o, _, n = invoke(
            reg,
            led,
            "T",
            0,
            "write_note",
            {"target": "t", "text": "v", "ignored": "must-not-pass"},
        )
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


@case(
    "同一调用重放不产生第二次副作用",
    {"outcome": "ok", "note_prefix": "replayed_from_ledger", "sink": 1},
)
def c_replay(d: Path) -> dict[str, Any]:
    with _contracts(d) as (reg, led):
        invoke(reg, led, "T", 0, "write_note", {"target": "t", "text": "v"})
        o, _, n = invoke(reg, led, "T", 0, "write_note", {"target": "t", "text": "v"})
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


@case(
    "留下未完成意图时判不确定而不是重试",
    {"outcome": "uncertain", "note_prefix": "pending_intent_found", "sink": 0},
)
def c_pending(d: Path) -> dict[str, Any]:
    args = {"target": "t", "text": "v"}
    with _contracts(d) as (reg, led):
        led.mark_pending(idem_key("T", 0, "write_note", args))  # 模拟上次崩在中间
        o, _, n = invoke(reg, led, "T", 0, "write_note", args)
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


@case(
    "确定无副作用失败会清除 pending",
    {"first": "failed", "second": "failed", "state": "fresh", "sink": 0},
)
def c_tool_failure_clears_pending(d: Path) -> dict[str, Any]:
    with _contracts(d) as (reg, led):
        spec = reg["write_note"]

        def reject_before_effect(target: str, text: str) -> dict[str, Any]:
            raise ToolFailure("rejected-before-effect")

        reg["write_note"] = spec.model_copy(update={"fn": reject_before_effect})
        args = {"target": "t", "text": "v"}
        first, _, _ = invoke(reg, led, "T", 0, "write_note", args)
        second, _, _ = invoke(reg, led, "T", 0, "write_note", args)
        state, _ = led.lookup(idem_key("T", 0, "write_note", args))
    return {"first": first, "second": second, "state": state, "sink": _sink_lines(d)}


@case(
    "副作用后无效回执判不确定并阻止重放", {"first": "uncertain", "second": "uncertain", "sink": 1}
)
def c_invalid_receipt_is_uncertain(d: Path) -> dict[str, Any]:
    with _contracts(d) as (reg, led):
        spec = reg["write_note"]

        def effect_with_bad_receipt(target: str, text: str) -> dict[str, Any]:
            with (d / "sink.log").open("a", encoding="utf-8") as handle:
                handle.write(f"note::{target}::{text}\n")
            return {"wrong": "shape"}

        reg["write_note"] = spec.model_copy(
            update={"fn": effect_with_bad_receipt, "result_model": WriteNoteResult}
        )
        args = {"target": "t", "text": "v"}
        first, _, _ = invoke(reg, led, "T", 0, "write_note", args)
        second, _, _ = invoke(reg, led, "T", 0, "write_note", args)
    return {"first": first, "second": second, "sink": _sink_lines(d)}


# --- 图层 case -----------------------------------------------------------------
@case("正常回路走完且副作用条数正确", {"stop_code": "", "sink": 3})
def c_happy(d: Path) -> dict[str, Any]:
    app, resources = build_graph("T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite")
    from .graph import make_plan

    try:
        r = app.invoke(
            {
                "task": "t",
                "plan": make_plan(6),
                "cursor": 0,
                "journal": [],
                "stop_code": "",
                "approvals": {},
            },
            {"configurable": {"thread_id": "T"}},
        )
    finally:
        resources.close()
    return {"stop_code": r.get("stop_code", ""), "sink": _sink_lines(d)}


@case("高风险动作在审批前挂起且未落副作用", {"interrupted": True, "sink": 0})
def c_gate_pauses(d: Path) -> dict[str, Any]:
    app, resources = build_graph(
        "T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite", auto_approve=False
    )
    plan = [{"tool": "submit_form", "args": {"target": "t", "payload": {"a": 1}}}]
    try:
        r = app.invoke(
            {
                "task": "t",
                "plan": plan,
                "cursor": 0,
                "journal": [],
                "stop_code": "",
                "approvals": {},
            },
            {"configurable": {"thread_id": "T"}},
        )
    finally:
        resources.close()
    return {"interrupted": "__interrupt__" in r, "sink": _sink_lines(d)}


@case("审批被拒时动作不执行", {"stop_code": "APPROVAL_DENIED", "sink": 0})
def c_gate_denied(d: Path) -> dict[str, Any]:
    app, resources = build_graph(
        "T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite", auto_approve=False
    )
    cfg = {"configurable": {"thread_id": "T"}}
    plan = [{"tool": "submit_form", "args": {"target": "t", "payload": {"a": 1}}}]
    try:
        app.invoke(
            {
                "task": "t",
                "plan": plan,
                "cursor": 0,
                "journal": [],
                "stop_code": "",
                "approvals": {},
            },
            cfg,
        )
        r = app.invoke(Command(resume=False), cfg)
    finally:
        resources.close()
    return {"stop_code": r.get("stop_code", ""), "sink": _sink_lines(d)}


@case("字符串 false 不能绕过审批", {"stop_code": "APPROVAL_DENIED", "sink": 0})
def c_gate_string_false(d: Path) -> dict[str, Any]:
    app, resources = build_graph(
        "T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite", auto_approve=False
    )
    cfg = {"configurable": {"thread_id": "T"}}
    plan = [{"tool": "submit_form", "args": {"target": "t", "payload": {"a": 1}}}]
    try:
        app.invoke(
            {
                "task": "t",
                "plan": plan,
                "cursor": 0,
                "journal": [],
                "stop_code": "",
                "approvals": {},
            },
            cfg,
        )
        r = app.invoke(Command(resume="false"), cfg)
    finally:
        resources.close()
    return {"stop_code": r.get("stop_code", ""), "sink": _sink_lines(d)}


@case("审批通过后动作恰好执行一次", {"stop_code": "", "sink": 1})
def c_gate_approved(d: Path) -> dict[str, Any]:
    app, resources = build_graph(
        "T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite", auto_approve=False
    )
    cfg = {"configurable": {"thread_id": "T"}}
    plan = [{"tool": "submit_form", "args": {"target": "t", "payload": {"a": 1}}}]
    try:
        app.invoke(
            {
                "task": "t",
                "plan": plan,
                "cursor": 0,
                "journal": [],
                "stop_code": "",
                "approvals": {},
            },
            cfg,
        )
        r = app.invoke(Command(resume=True), cfg)
    finally:
        resources.close()
    return {"stop_code": r.get("stop_code", ""), "sink": _sink_lines(d)}


@case("图层未知工具 fail closed", {"stop_code": "FAILED", "note_prefix": "unknown_tool", "sink": 0})
def c_graph_unknown_tool(d: Path) -> dict[str, Any]:
    app, resources = build_graph("T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite")
    plan = [{"tool": "delete_everything", "args": {"target": "t"}}]
    try:
        r = app.invoke(
            {
                "task": "t",
                "plan": plan,
                "cursor": 0,
                "journal": [],
                "stop_code": "",
                "approvals": {},
            },
            {"configurable": {"thread_id": "T"}},
        )
    finally:
        resources.close()
    journal = r.get("journal", [])
    note = journal[-1]["note"] if journal else ""
    return {
        "stop_code": r.get("stop_code", ""),
        "note_prefix": note.split(":")[0],
        "sink": _sink_lines(d),
    }


@case("不确定停机后不继续推进游标", {"stop_code": "UNCERTAIN_HALT", "cursor": 2})
def c_halt_stops(d: Path) -> dict[str, Any]:
    from .graph import make_plan

    plan = make_plan(6)
    with Ledger(d / "l.sqlite") as led:
        led.mark_pending(idem_key("T", 2, plan[2]["tool"], plan[2]["args"]))
    app, resources = build_graph("T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite")
    try:
        r = app.invoke(
            {
                "task": "t",
                "plan": plan,
                "cursor": 0,
                "journal": [],
                "stop_code": "",
                "approvals": {},
            },
            {"configurable": {"thread_id": "T"}},
        )
    finally:
        resources.close()
    return {"stop_code": r.get("stop_code", ""), "cursor": r.get("cursor", -1)}


def _criteria_hash() -> str:
    blob = json.dumps(
        [{"name": c["name"], "expect": c["expect"]} for c in CASES],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(blob).hexdigest()


def _implementation_hash() -> str:
    digest = hashlib.sha256()
    for relative in PROTECTED_FILES:
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        normalized = "\n".join(source.splitlines()) + "\n"
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(normalized.encode("utf-8") + b"\0")
    return digest.hexdigest()


def manifest_payload() -> dict[str, Any]:
    criteria = _criteria_hash()
    implementation = _implementation_hash()
    combined = hashlib.sha256(f"v2:{criteria}:{implementation}".encode()).hexdigest()
    return {
        "schema_version": 2,
        "sha256": combined,
        "criteria_sha256": criteria,
        "implementation_sha256": implementation,
        "protected_files": list(PROTECTED_FILES),
        "n_cases": len(CASES),
    }


def main() -> int:
    _configure_safe_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--tmp", default="_eval")
    ap.add_argument("--manifest", default="eval_manifest.json")
    ap.add_argument("--update-manifest", action="store_true")
    args = ap.parse_args()

    try:
        tmp = _prepare_eval_root(Path(args.tmp))
    except (OSError, ValueError) as exc:
        print(f"[eval_tmp_rejected] {exc}", file=sys.stderr)
        return 2

    case_failures = 0
    for i, c in enumerate(CASES):
        d = _fresh(tmp, f"c{i:02d}")
        try:
            got = c["fn"](d)
            ok = all(got.get(k) == v for k, v in c["expect"].items())
        except Exception as e:
            got, ok = {"error": f"{type(e).__name__}: {e}"}, False
        case_failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {c['name']}")
        if not ok:
            print(f"      期望 {c['expect']}\n      实际 {got}")

    current_manifest = manifest_payload()
    h = current_manifest["sha256"]
    mpath = Path(args.manifest)
    manifest_ok = True
    if args.update_manifest:
        if case_failures:
            print(
                "\n[manifest_update_rejected] 有失败 case，拒绝冻结失败实现",
                file=sys.stderr,
            )
            manifest_ok = False
        else:
            mpath.write_text(
                json.dumps(current_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"\nmanifest 已写入：{h[:16]}...  ({len(CASES)} cases)")
    elif not mpath.exists():
        print(
            "\n[manifest_missing] 不会自动创建基线；确认判据与实现后显式使用 --update-manifest",
            file=sys.stderr,
        )
        manifest_ok = False
    else:
        try:
            old = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"\n[manifest_invalid] {type(exc).__name__}: {exc}", file=sys.stderr)
            manifest_ok = False
            old = None
        if old is not None and old != current_manifest:
            print(
                f"\n[manifest_mismatch] 冻结值 {old.get('sha256', '')[:16]}... "
                f"当前 {h[:16]}...  —— 判据被改过，确认是有意的再 --update-manifest"
            )
            manifest_ok = False
        elif old is not None:
            print(f"\nmanifest 一致：{h[:16]}...  ({len(CASES)} cases)")

    print(f"\n{len(CASES) - case_failures}/{len(CASES)} cases 通过")
    return 1 if case_failures or not manifest_ok else 0


if __name__ == "__main__":
    sys.exit(main())
