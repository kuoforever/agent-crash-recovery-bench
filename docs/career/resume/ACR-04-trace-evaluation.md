# ACR-04 — Trace-based deterministic evaluation

> **Status: current candidate resume item; 10 frozen local cases.**

- **JD tags:** Agent evaluation, deterministic testing, schema validation,
  manifest, tool calling.
- **Candidate bullet (ZH):** 以调用序列、停止码和副作用条数取代自然语言作为
  Agent 判据，并用 SHA-256 同时冻结 15 个 case 的 expected manifest 与受保护实现，覆盖未知工具、
  参数越界、严格审批、三态账本、重放与 `UNCERTAIN_HALT` 等路径，当前 15/15 通过。
- **Candidate bullet (EN):** Built a deterministic Agent evaluation over call
  sequence, stop code, and side-effect count, freezing 15 expected cases and the
  protected implementation by SHA-256 rather than grading model prose.
- **Sources:** [README](../../../README.md),
  [evaluation manifest](../../../eval_manifest.json), and
  [evaluation runner](../../../guarded_loop/eval_trace.py).
- **Do not claim:** broad model quality, production coverage, statistical
  significance, or evaluation of arbitrary natural-language tasks.
- **Interview expansion:** explain why outcome predicates are more stable than
  prose matching and why updating a manifest must be explicit.
