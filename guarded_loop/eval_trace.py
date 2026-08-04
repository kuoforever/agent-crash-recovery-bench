"""按 trace 判定的确定性评测。

判据是**调用序列 + 停止码 + 副作用条数**，不判模型说了什么。
每个 case 的期望值用 SHA-256 冻进 manifest：改实现的时候如果顺手把
不过的用例调松，manifest 对不上会直接报出来。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from langgraph.types import Command

from .graph import build_graph
from .tools import Ledger, build_registry, idem_key, invoke

CASES: list[dict] = []


def case(name: str, expect: dict):
    def deco(fn):
        CASES.append({"name": name, "fn": fn, "expect": expect})
        return fn
    return deco


def _fresh(tmp: Path, name: str) -> Path:
    d = tmp / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


def _sink_lines(d: Path) -> int:
    p = d / "sink.log"
    return len(p.read_text(encoding="utf-8").splitlines()) if p.exists() else 0


# --- 契约层 case ---------------------------------------------------------------
@case("注册表之外的工具名被拒", {"outcome": "failed", "note_prefix": "unknown_tool", "sink": 0})
def c_unknown_tool(d: Path) -> dict:
    reg, led = build_registry(d / "sink.log"), Ledger(d / "l.sqlite")
    o, _, n = invoke(reg, led, "T", 0, "delete_everything", {"target": "x"})
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


@case("参数不合契约时不进执行层", {"outcome": "failed", "note_prefix": "args_rejected", "sink": 0})
def c_bad_args(d: Path) -> dict:
    reg, led = build_registry(d / "sink.log"), Ledger(d / "l.sqlite")
    o, _, n = invoke(reg, led, "T", 0, "write_note", {"target": "", "text": "x"})
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


@case("超长文本被参数契约挡下", {"outcome": "failed", "note_prefix": "args_rejected", "sink": 0})
def c_too_long(d: Path) -> dict:
    reg, led = build_registry(d / "sink.log"), Ledger(d / "l.sqlite")
    o, _, n = invoke(reg, led, "T", 0, "write_note", {"target": "t", "text": "x" * 500})
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


@case("同一调用重放不产生第二次副作用", {"outcome": "ok", "note_prefix": "replayed_from_ledger", "sink": 1})
def c_replay(d: Path) -> dict:
    reg, led = build_registry(d / "sink.log"), Ledger(d / "l.sqlite")
    invoke(reg, led, "T", 0, "write_note", {"target": "t", "text": "v"})
    o, _, n = invoke(reg, led, "T", 0, "write_note", {"target": "t", "text": "v"})
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


@case("留下未完成意图时判不确定而不是重试", {"outcome": "uncertain", "note_prefix": "pending_intent_found", "sink": 0})
def c_pending(d: Path) -> dict:
    reg, led = build_registry(d / "sink.log"), Ledger(d / "l.sqlite")
    args = {"target": "t", "text": "v"}
    led.mark_pending(idem_key("T", 0, "write_note", args))  # 模拟上次崩在中间
    o, _, n = invoke(reg, led, "T", 0, "write_note", args)
    return {"outcome": o, "note_prefix": n.split(":")[0], "sink": _sink_lines(d)}


# --- 图层 case -----------------------------------------------------------------
@case("正常回路走完且副作用条数正确", {"stop_code": "", "sink": 3})
def c_happy(d: Path) -> dict:
    app, _ = build_graph("T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite")
    from .graph import make_plan
    r = app.invoke({"task": "t", "plan": make_plan(6), "cursor": 0, "journal": [],
                    "stop_code": "", "approvals": {}},
                   {"configurable": {"thread_id": "T"}})
    return {"stop_code": r.get("stop_code", ""), "sink": _sink_lines(d)}


@case("高风险动作在审批前挂起且未落副作用", {"interrupted": True, "sink": 0})
def c_gate_pauses(d: Path) -> dict:
    app, _ = build_graph("T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite",
                         auto_approve=False)
    plan = [{"tool": "submit_form", "args": {"target": "t", "payload": {"a": 1}}}]
    r = app.invoke({"task": "t", "plan": plan, "cursor": 0, "journal": [],
                    "stop_code": "", "approvals": {}},
                   {"configurable": {"thread_id": "T"}})
    return {"interrupted": "__interrupt__" in r, "sink": _sink_lines(d)}


@case("审批被拒时动作不执行", {"stop_code": "APPROVAL_DENIED", "sink": 0})
def c_gate_denied(d: Path) -> dict:
    app, _ = build_graph("T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite",
                         auto_approve=False)
    cfg = {"configurable": {"thread_id": "T"}}
    plan = [{"tool": "submit_form", "args": {"target": "t", "payload": {"a": 1}}}]
    app.invoke({"task": "t", "plan": plan, "cursor": 0, "journal": [],
                "stop_code": "", "approvals": {}}, cfg)
    r = app.invoke(Command(resume=False), cfg)
    return {"stop_code": r.get("stop_code", ""), "sink": _sink_lines(d)}


@case("审批通过后动作恰好执行一次", {"stop_code": "", "sink": 1})
def c_gate_approved(d: Path) -> dict:
    app, _ = build_graph("T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite",
                         auto_approve=False)
    cfg = {"configurable": {"thread_id": "T"}}
    plan = [{"tool": "submit_form", "args": {"target": "t", "payload": {"a": 1}}}]
    app.invoke({"task": "t", "plan": plan, "cursor": 0, "journal": [],
                "stop_code": "", "approvals": {}}, cfg)
    r = app.invoke(Command(resume=True), cfg)
    return {"stop_code": r.get("stop_code", ""), "sink": _sink_lines(d)}


@case("不确定停机后不继续推进游标", {"stop_code": "UNCERTAIN_HALT", "cursor": 2})
def c_halt_stops(d: Path) -> dict:
    from .graph import make_plan
    led = Ledger(d / "l.sqlite")
    plan = make_plan(6)
    led.mark_pending(idem_key("T", 2, plan[2]["tool"], plan[2]["args"]))
    app, _ = build_graph("T", d / "sink.log", d / "l.sqlite", True, d / "ck.sqlite")
    r = app.invoke({"task": "t", "plan": plan, "cursor": 0, "journal": [],
                    "stop_code": "", "approvals": {}},
                   {"configurable": {"thread_id": "T"}})
    return {"stop_code": r.get("stop_code", ""), "cursor": r.get("cursor", -1)}


def manifest_hash() -> str:
    blob = json.dumps([{"name": c["name"], "expect": c["expect"]} for c in CASES],
                      sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tmp", default="_eval")
    ap.add_argument("--manifest", default="eval_manifest.json")
    ap.add_argument("--update-manifest", action="store_true")
    args = ap.parse_args()

    tmp = Path(args.tmp)
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    results, failed = [], 0
    for i, c in enumerate(CASES):
        d = _fresh(tmp, f"c{i:02d}")
        try:
            got = c["fn"](d)
            ok = all(got.get(k) == v for k, v in c["expect"].items())
        except Exception as e:
            got, ok = {"error": f"{type(e).__name__}: {e}"}, False
        failed += (not ok)
        results.append({"name": c["name"], "pass": ok, "expect": c["expect"], "got": got})
        print(f"{'PASS' if ok else 'FAIL'}  {c['name']}")
        if not ok:
            print(f"      期望 {c['expect']}\n      实际 {got}")

    h = manifest_hash()
    mpath = Path(args.manifest)
    if args.update_manifest or not mpath.exists():
        mpath.write_text(json.dumps({"sha256": h, "n_cases": len(CASES)},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nmanifest 已写入：{h[:16]}...  ({len(CASES)} cases)")
    else:
        old = json.loads(mpath.read_text(encoding="utf-8"))
        if old.get("sha256") != h:
            print(f"\n[manifest 不一致] 冻结值 {old.get('sha256','')[:16]}... "
                  f"当前 {h[:16]}...  —— 判据被改过，确认是有意的再 --update-manifest")
            failed += 1
        else:
            print(f"\nmanifest 一致：{h[:16]}...  ({len(CASES)} cases)")

    print(f"\n{len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
