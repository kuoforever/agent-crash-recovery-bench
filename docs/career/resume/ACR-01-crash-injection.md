# ACR-01 — Crash-injection comparison

> **Status: current candidate resume item; local experiment, not production.**

- **JD tags:** reliability, fault injection, checkpointing, idempotency,
  LangGraph, recovery semantics.
- **Candidate bullet (ZH):** 使用 `os._exit(70)` 构造不执行 `finally/atexit` 的
  真实进程硬崩溃窗口，在 30 次×20 步对照实验中量化 LangGraph checkpoint：
  `async`/`sync` 分别产生 128/20 条重复副作用，而 checkpoint + 两段式 intent
  ledger 保持 0 duplicate effects。
- **Candidate bullet (EN):** Built a hard-crash fault-injection benchmark with
  `os._exit(70)`; across 30 local 20-step runs, LangGraph async/sync checkpoint
  paths recorded 128/20 duplicate effects while a two-phase intent ledger
  recorded zero duplicates.
- **Sources:** [README conclusions](../../../README.md),
  [design](../../DESIGN.md), and
  [machine-readable report](../../../evidence/crash-bench-report.json).
- **Do not claim:** production traffic, network/database transactions, framework
  defect, concurrency, or general LangGraph behavior.
- **Interview expansion:** distinguish node replay from side-effect idempotency,
  and explain why `raise` would be a weaker crash injection than `os._exit`.
