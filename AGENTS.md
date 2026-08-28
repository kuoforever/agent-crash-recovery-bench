# Coding-agent instructions

## Start here

For every Codex, Claude Code, or other coding-agent session:

1. Read `README.md` for the measured conclusions and their boundaries.
2. Read `docs/DESIGN.md` for the experiment design and invariants.
3. Read `docs/HANDOFF.md` for durable current state and the next safe action.
4. Read `docs/DIFY-STATUS.md` only for Dify work.
5. Inspect `git status --short --branch` before editing.

`docs/HANDOFF.md` owns durable handoff state. Do not create a competing
tracker or infer current work from chat history alone.

## Teaching-oriented collaboration

Treat implementation as both project delivery and guided learning for the
user's skill development, interview preparation, and evidence-backed resume.

- Before each non-trivial step, explain its objective, required concepts, why
  it is next, the expected evidence, and what failure would mean.
- During execution, explain consequential code, commands, parameters, design
  choices, and trade-offs. Summarize repetitive mechanical operations instead
  of turning them into noise.
- After each step, interpret the result, distinguish what it proves from what
  it does not prove, cover relevant failure modes and alternatives, and connect
  the work to likely interview questions and defensible resume evidence.
- Prefer clear Chinese explanations while retaining exact English technical
  terms, identifiers, commands, metrics, and file names needed for industry
  communication and reproducibility.
- Never turn a framework demo, a local benchmark, or unverified behavior into
  a production-experience claim.

## Codex and Claude Code coordination

`AGENTS.md` is the shared source of truth for coding-agent behavior.
`CLAUDE.md` is a lightweight Claude Code entry point that follows this file;
do not duplicate the full policy there.

- Re-read the handoff and inspect `git status` before editing, including when
  taking over from the other coding agent.
- Treat existing uncommitted changes as user- or peer-owned. Do not overwrite,
  revert, or silently rework them; coordinate scope and preserve unrelated
  edits.
- Prefer one implementation owner for a bounded slice. A second agent may
  review or independently validate it, but must not make overlapping edits
  without an explicit handoff.
- Every handoff must name the outcome, modified files, exact validation and
  results, unresolved risks or limitations, and the single next action.
- A reviewing agent must inspect the implementation and raw evidence and run
  proportionate checks itself before treating a claim as verified.

## Experiment invariants

- Judge recovery semantics by trace, stop code, and side-effect count, not by
  model prose.
- Preserve the framework-independent contract boundary in
  `guarded_loop/tools.py`.
- Distinguish `ok`, `failed`, and `uncertain`; never silently treat an unknown
  side-effect outcome as safe to replay.
- Describe documented framework semantics accurately rather than presenting
  them as framework defects.
- Keep LangGraph, Dify, and Guarded Desktop Agent measurements separate. Do not
  mix their run sizes, test counts, or evidence.
- Networked model runs and external-system experiments require explicit scope;
  deterministic local evaluation remains the default validation path.

## Completion response

Report the outcome, modified files, exact validation commands and results,
limitations, and the single next action. Update `docs/HANDOFF.md` only when
real project state or reproducible evidence changes.
