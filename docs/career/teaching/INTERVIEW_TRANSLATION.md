# Interview translation

> **Status: current module of teaching protocol v1.**

Use `question -> controlled variable -> injection -> observed trace/effect ->
conclusion -> limitation`.

Likely questions:

- Why is checkpoint recovery different from side-effect idempotency?
- What does `durability="sync"` guarantee, and what does it not guarantee?
- Why is an `UNCERTAIN_HALT` preferable to replay?
- Why use `os._exit` instead of an exception?
- Which conclusion would change with a transactional database sink?
- What did LangGraph do better than the custom loop?

A strong answer includes the framework's strength, the measured limitation,
and the caller-owned responsibility instead of attacking the framework.
