# ACR-05 — Dify recovery semantics experiment

> **Status: current candidate resume item; one local Dify 1.16.1 debugger scope.**

- **JD tags:** workflow platform, Dify, retries, HITL, Docker, failure analysis.
- **Candidate bullet (ZH):** 在本地 Dify 1.16.1 debugger 中构造可 `fsync`、可阻塞
  的 HTTP side-effect sink，保留 HTTP 500、3 次重试、Human Input/API 重启和
  worker 硬崩溃证据；实测重试路径执行 4 次副作用，worker 崩溃运行在约 14 分
  24 秒观察窗内保持 `running` 且未重投。
- **Candidate bullet (EN):** Built a controllable fsync-backed HTTP sink for a
  local Dify 1.16.1 debugger experiment, retaining retry, Human Input/API restart,
  and worker-crash observations with exact time-window limits.
- **Sources:** [Dify status](../../DIFY-STATUS.md),
  [summary report](../../../evidence/dify-semantics-report.json), and
  [redacted raw snapshot](../../../evidence/dify-raw-snapshot.json).
- **Do not claim:** published-API, cluster, broker visibility-timeout, long-term
  recovery, or general Dify deployment behavior. The precise worker kill time
  and pre-restart HITL screenshot were not independently retained.
- **Interview expansion:** separate API-process restart from worker failure,
  retry count from side-effect count, and observed time window from general behavior.
