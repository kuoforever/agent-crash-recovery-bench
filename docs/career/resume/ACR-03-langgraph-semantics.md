# ACR-03 — LangGraph checkpoint and HITL semantics

> **Status: current candidate resume item; bounded framework surface.**

- **JD tags:** LangGraph, state machine, persistence, human-in-the-loop,
  checkpoint, Agent framework evaluation.
- **Candidate bullet (ZH):** 用同一受控回路对齐自研 Runtime 与 LangGraph
  StateGraph/checkpointer/`interrupt` 语义，验证 checkpoint 提供可恢复状态但不
  自动保证副作用幂等，同时确认 `interrupt + Command(resume=...)` 支持进程退出后
  恢复审批，优于同步阻塞式闸门。
- **Candidate bullet (EN):** Reconstructed one guarded loop on LangGraph to
  separate checkpoint recovery from side-effect idempotency and verified that
  `interrupt` plus persisted state supports approval resumption after process exit.
- **Sources:** [README](../../../README.md) and [design](../../DESIGN.md).
- **Do not claim:** use of LangChain RAG, vector stores, prebuilt agents,
  subgraphs, streaming, or multi-agent orchestration.
- **Interview expansion:** name one framework strength and one caller-owned
  responsibility; avoid presenting documented durability semantics as a bug.
