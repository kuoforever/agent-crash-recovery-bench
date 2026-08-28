# Claude Code repository guide

Read and follow [AGENTS.md](AGENTS.md). Its teaching, coordination, experiment,
and evidence rules are shared by Codex and Claude Code and must not be copied
into a second divergent policy here.

## Required read order

1. `README.md`
2. `docs/DESIGN.md`
3. `docs/HANDOFF.md`
4. `docs/DIFY-STATUS.md` only for Dify work
5. The source and evidence files required by the bounded task

Inspect `git status --short --branch` before editing. Existing uncommitted
changes may belong to the user or Codex; preserve them and require an explicit
handoff before overlapping edits.

At completion, report the outcome, modified files, exact validation results,
limitations, and the single next action. Do not claim production experience or
broader framework coverage from these local experiments.
