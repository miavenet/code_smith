---
name: agent-checkpoints
description: Track long-running agent tasks with milestone progress, durable artifact checkpoints, and crash recovery. Use for headless or multi-stage work, code_smith runs, handoffs, and resuming interrupted agents.
---

# Agent checkpoints

Make progress inspectable and useful work recoverable **before the final answer**.
Keep the record proportional to the task. Store results and decisions, not private
chain-of-thought, credentials, full prompts, or unrelated repository contents.

## Start and record progress

At task intake, create a checkpoint containing the concrete objective, requirement
IDs, current stage, what is accomplished, what remains, and the next actionable step.
Use the task's existing IDs and acceptance criteria. Distinguish implemented work,
verified work, and work awaiting review; never infer completion from tool activity.

Write working artifacts incrementally. Checkpoint at these material boundaries:

- research/contracts sufficient to implement: save findings, decisions, references;
- first useful draft or implementation slice: copy its actual files;
- meaningful test/build/review result: save the result and relevant evidence;
- a blocker, changed approach, handoff, retry, or approaching execution limit;
- before the final response, with precise remaining work and recovery instructions.

During a long stage, aim to go no more than about five minutes without a brief
progress checkpoint. Use judgment for indivisible operations; record the operation
and its evidence path before starting it. Avoid spending an entire invocation composing
an unwritten result. If a task repeatedly cannot reach a useful checkpoint within its
budget, split the task rather than silently repeating the same work.

Use [the report format and commands](references/report-format.md). The bundled
`scripts/checkpoint.py` saves an immutable-by-convention generation with the report,
explicitly selected file copies, hashes, modes, declared deletions, and Git base identity.
It publishes the complete directory atomically after flushing it to disk. `status`
reads the latest valid generation, and `verify` checks stored artifact integrity.
Neither command restores files or executes commands from a checkpoint.

For substantial handoffs or claims about recovery/integration, use the
[evidence boundary checks](references/evidence-boundaries.md), including only the
assigned persona's questions. Record the useful conclusions in the existing report;
this does not add a scoring ceremony or change acceptance authority.

## Storage and ownership

Use a persistent location designated by the coordinator, separate for every run,
task and invocation. For this repository's runner, use `$HOOK_LOG_DIR/checkpoints`;
the helper selects it when `--store` is omitted. Otherwise pass an explicit `--store`
outside files the runner restores. Do not use an ephemeral scratch checkout as the
only copy. Process-crash recovery is not a backup against loss of the storage host.

A checkpoint is written only within the agent's authorized auxiliary storage and
contains only task-relevant artifacts the agent is allowed to read. The agent must
not edit runner state, acceptance records, integrity manifests, other invocations,
or native hook log files. Never commit, stash, reset, widen tool permissions or alter
protected outputs just to create a checkpoint.

Read-only reviewers keep their tool restrictions. If they lack a checkpoint-writing
channel, emit concise milestone messages and have the coordinator persist them through
this helper under a coordinator-owned store. Do not claim their intermediate findings
are durable until the coordinator has saved them. A skill alone cannot make an absent
write capability exist.

## Hooks and status

The helper emits an `AgentCheckpoint` event after the checkpoint is durable. When
using the runner's default store it also appends to the separate
`$HOOK_LOG_DIR/milestones.jsonl`; native hooks continue recording tool/session activity.
Hook logging failure never invalidates an already saved checkpoint. A checkpoint
committed before a logging crash is still discoverable by `status`.

The runner's activity view labels checkpoint events as agent-reported. Correlate run,
task, invocation, checkpoint ID and timestamp with hooks; do not treat a tool call or
self-reported milestone as a passed test or accepted task. Report missing/stale data
as unknown. A useful status update states: stage, accomplishments against requirements,
last durable artifacts, verification performed, blocker/next step and checkpoint time.

Keep the required final-answer schema unchanged. Return a short summary and checkpoint
reference, not the entire report or artifact content. Respect field-size limits.

## Resume after a crash

1. The coordinator settles live/orphaned processes and runner transactions using the
   runner's recovery controls. Inspect status before executing or repeating any effect.
2. Read the latest valid checkpoint and `verify` its artifact copies. Keep a corrupt or
   partial generation for diagnosis; use an older valid one only with an explicit note.
3. Compare the recorded objective, requirements, Git base and workspace with the current
   task. Inspect current files, the retained candidate patch and native logs. If the
   base changed, reuse findings selectively and recheck affected assumptions.
4. Restore useful files only into a fresh recovery directory or through the runner's
   authorized candidate/retry mechanism. Review the diff before applying it to a live
   worktree. Do not replay recorded shell commands or external actions automatically.
5. Resume the first incomplete requirement. Re-run checks invalidated by recovery or
   changed inputs; previous test claims remain historical evidence, not fresh acceptance.
   Start a new checkpoint generation recording the source checkpoint and reconciliation.

Only completed checkpoints survive reliably; unsaved model context and uncheckpointed
edits can still be lost. The runner remains the authority for acceptance and commits.
