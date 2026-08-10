# ACR-02 — Tri-state effect semantics and intent ledger

> **Status: current candidate resume item; deterministic local sink only.**

- **JD tags:** distributed systems, side-effect safety, intent log, recovery,
  fail-closed, unknown outcome.
- **Candidate bullet (ZH):** 将工具结果建模为 `ok/failed/uncertain` 三态，并以
  `pending -> side effect -> done(receipt)` 两段式账本阻止未知结果自动重放；
  明确保留 20 次 `UNCERTAIN_HALT`，其中 10 次为无法区分 `pre_apply` 的安全误停。
- **Candidate bullet (EN):** Modeled tool outcomes as `ok/failed/uncertain` and
  used a `pending -> effect -> done(receipt)` ledger to stop unknown results
  from replay, explicitly retaining the false-stop cost of non-transactional
  local effects.
- **Sources:** [design](../../DESIGN.md), [README](../../../README.md), and
  [handoff](../../HANDOFF.md).
- **Do not claim:** exactly-once delivery, resolution of uncertain outcomes, or
  atomicity between the file ledger and arbitrary external systems.
- **Interview expansion:** explain why pre-effect and post-effect crashes can
  produce the same pending record and when a shared database transaction could
  remove that ambiguity.
