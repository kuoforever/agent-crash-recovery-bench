# ACR-05 — Dify recovery semantics experiment

> **Status: current candidate resume item; local Dify 1.16.1 debugger plus
> isolated Published API single-worker early-ACK, late-ACK, and experiment-only
> prefork no-fault control and exact pool-child-loss scopes.**

- **JD tags:** workflow platform, Dify, retries, HITL, Docker, Celery, failure attribution.
- **Candidate bullet (ZH):** 为本地 Dify 1.16.1 构造可 `fsync`、可阻塞的 HTTP
  side-effect sink，覆盖 debugger 重试/HITL 与隔离 Published API 故障注入；用 fixed-checkout
  语义参照、runtime image 身份、worker log 和 `celery inspect active` 归因 `blocking` 在 API thread、`streaming` 才进
  Celery；对比实测 early-ACK worker exit 137 后约 3 分钟内 effect 保持 1 次但 run 未收敛，
  与 experiment-only late ACK 在冷启动 broker 重投后 run 成功但 effect 从 1 次增至 2 次；另以两次稳定
  prefork parent/child 拓扑和 exact task PID / Redis delivery 绑定证明真实 OS pool child，再只杀即时重验的
  exact child，观测 surviving parent 的 `WorkerLostError`、replacement child 与 same-task/tag
  `redelivered=true`；同一 run 恢复成功但 effect 再次从 1 次增至 2 次，证明 recovery 与 exactly-once 分离。
- **Candidate bullet (EN):** Built a controllable fsync-backed HTTP sink for a
  local Dify 1.16.1 debugger and isolated Published API experiment; attributed
  blocking versus streaming executors from a fixed-checkout semantic reference
  and runtime active-task evidence, then
  compared an early-ACK stale run with a late-ACK broker redelivery that recovered
  workflow progress but increased the persisted effect count from one to two;
  then killed only a revalidated prefork pool child and observed the surviving
  parent replace it and redeliver the same task/tag, recovering the same workflow
  run while again duplicating the persisted effect.
- **Sources:** [Dify status](../../DIFY-STATUS.md),
  [debugger summary](../../../evidence/dify-semantics-report.json),
  [debugger raw snapshot](../../../evidence/dify-raw-snapshot.json),
  [Published API early-ACK summary](../../../evidence/dify-published-crash-report.json),
  [Published API early-ACK raw snapshot](../../../evidence/dify-published-crash-raw.json),
  [Published API late-ACK summary](../../../evidence/dify-published-late-ack-report.json),
  [Published API late-ACK raw snapshot](../../../evidence/dify-published-late-ack-raw.json),
  [prefork control summary](../../../evidence/dify-prefork-control-report.json), and
  [prefork control raw snapshot](../../../evidence/dify-prefork-control-raw.json),
  [prefork child-loss summary](../../../evidence/dify-prefork-child-loss-report.json),
  [prefork child-loss raw snapshot](../../../evidence/dify-prefork-child-loss-raw.json), and
  [prefork child-loss manifest](../../../evidence/dify-prefork-child-loss-manifest.json).
- **Do not claim:** production, cluster, exactly-once delivery, stable 120-second
  redelivery latency, a child-loss latency distribution, visibility-timeout recovery timing,
  general Dify deployment behavior, or prefork stability/performance. The whole-container
  late-ACK run had an external observation gap and used
  an experiment-only overlay; its strongest result is same-task broker redelivery
  plus two persisted effect attempts on a later cold worker start. The separate
  pool-child run isolated one surviving-parent worker-loss requeue path, not a
  general guarantee.
- **Interview expansion:** first prove the fault hit the actual executor; then
  separate HTTP retry count from effect count, response mode from execution
  location, task ACK state from broker recovery, and a bounded snapshot from a
  general guarantee. Explain why “final workflow succeeded” and “the side effect
  happened exactly once” are separate claims, and why a replayed successful effect
  row does not reconcile the original `running` row.
