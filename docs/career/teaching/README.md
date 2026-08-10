# Teaching-oriented collaboration

> **Status: current collaboration contract. Protocol version: `1`.**

| Module | Purpose |
| --- | --- |
| [Step protocol](STEP_PROTOCOL.md) | Before/during/after explanation contract |
| [Evidence discipline](EVIDENCE_DISCIPLINE.md) | Experiment attribution and claim boundaries |
| [Interview translation](INTERVIEW_TRANSLATION.md) | Turning measured semantics into defensible answers |

Prefer Chinese explanations with exact English framework terms, commands,
versions, run sizes, and stop codes.

## Project-specific boundaries

- Judge recovery by trace, stop code, and side-effect count, not model prose.
- Preserve the framework-independent contract in `guarded_loop/tools.py`.
- Distinguish `ok`, `failed`, and `uncertain`; never treat unknown as retryable.
- Describe documented framework semantics accurately instead of calling them defects.
- Keep LangGraph, Dify, and Guarded Desktop Agent measurements separate.
- This repository is a local comparison experiment with no deployment or users.
- Networked model and external-system runs require explicit scope; deterministic
  local evaluation is the default.
