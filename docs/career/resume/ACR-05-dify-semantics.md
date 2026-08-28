# ACR-05 — Dify recovery semantics experiment

> **Status: current candidate resume item; local Dify 1.16.1 debugger plus one
> isolated Published API single-worker scope.**

- **JD tags:** workflow platform, Dify, retries, HITL, Docker, Celery, failure attribution.
- **Candidate bullet (ZH):** 为本地 Dify 1.16.1 构造可 `fsync`、可阻塞的 HTTP
  side-effect sink，覆盖 debugger 重试/HITL 与隔离 Published API 故障注入；用源码、
  worker log 和 `celery inspect active` 证明 `blocking` 在 API thread、`streaming` 才进
  Celery，并实测 early-ACK task 在 effect 落盘后 worker exit 137，约 3 分钟内副作用
  保持 1 次但 run 未收敛。
- **Candidate bullet (EN):** Built a controllable fsync-backed HTTP sink for a
  local Dify 1.16.1 debugger and isolated Published API experiment; attributed
  blocking versus streaming executors from source and active-task evidence, then
  measured a hard worker loss after an early-acknowledged task had applied one effect.
- **Sources:** [Dify status](../../DIFY-STATUS.md),
  [debugger summary](../../../evidence/dify-semantics-report.json),
  [debugger raw snapshot](../../../evidence/dify-raw-snapshot.json),
  [Published API summary](../../../evidence/dify-published-crash-report.json), and
  [Published API raw snapshot](../../../evidence/dify-published-crash-raw.json).
- **Do not claim:** production, cluster, late-ACK behavior, long-term recovery,
  exactly-once delivery, or general Dify deployment behavior. Redis visibility
  timeout was shown **not applicable to the measured early-acknowledged task**;
  it was not benchmarked as a delayed recovery mechanism.
- **Interview expansion:** first prove the fault hit the actual executor; then
  separate HTTP retry count from effect count, response mode from execution
  location, task ACK state from broker recovery, and a bounded snapshot from a
  general guarantee.
