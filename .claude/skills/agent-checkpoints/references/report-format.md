# Milestone report and commands

Use one small JSON report per milestone. Every required field appears below. Requirement
states are `pending`, `in_progress`, `implemented`, `verified`, `blocked`, or `deferred`.
A deferred requirement needs an explicit scope decision in its evidence. Milestone states
are `working`, `blocked`, or `ready_for_review`; none means runner acceptance.

```json
{
  "task_id": "decoder-framing",
  "objective": "Reject malformed packets before any message is applied",
  "stage": "framing tests written",
  "status": "working",
  "requirements": [
    {"id": "DEC-02", "status": "implemented", "evidence": "short-packet BDD case written; not yet executed"},
    {"id": "DEC-05", "status": "pending", "evidence": "overrun case remains"}
  ],
  "accomplished": ["Wrote the short-packet Given/When/Then case"],
  "remaining": ["Add overrun case", "Run the decoder test selection"],
  "next_action": "Add DEC-05 against the reviewed framing API",
  "verification": [
    {"command": "decoder test selection", "result": "not_run", "evidence": "implementation not available yet"}
  ],
  "blockers": [],
  "recovery": "Reuse the saved test draft; confirm the API against the current base before continuing."
}
```

Write this input report into the authorized checkpoint store, or another authorized
auxiliary directory. It is input scratch data; only the published generation is durable.
Use real task identifiers and evidence, not the example's claims.

```sh
# SKILL_DIR is the installed agent-checkpoints skill directory.
# A runner writer already receives HOOK_LOG_DIR. Otherwise set CHECKPOINT_STORE to
# an explicitly authorized persistent directory and pass --store to every command.
python3 "$SKILL_DIR/scripts/checkpoint.py" save \
  --workspace "$PWD" --report "$HOOK_LOG_DIR/checkpoint-input.json" \
  --artifact path/to/test.cpp --artifact path/to/test-output.txt
python3 "$SKILL_DIR/scripts/checkpoint.py" status
python3 "$SKILL_DIR/scripts/checkpoint.py" verify

# Standalone use; this example only prints status and does not create a directory.
python3 "$SKILL_DIR/scripts/checkpoint.py" status --store "$CHECKPOINT_STORE"
```

`--artifact` accepts a workspace-relative regular file; repeat it for each deliverable or
piece of evidence. Include drafts even when tests fail. Files are copied, not merely
linked or listed. The helper rejects symlinks, directories, outside-workspace paths,
`.git`/`.runs` metadata, and its own storage. Do not snapshot secret-bearing files.
For an intentional removal use `--deleted path/to/file`; it must already be absent in
the workspace. Renames are an explicit old-path deletion plus a new-path artifact.

`checkpoint.json` maps original paths to collision-free `.blob` files and records SHA-256
hashes and original modes. This prevents runner-generated `index.json` files from
overwriting an artifact with that name. A `COMMITTED` marker hashes
the manifest. Readers ignore `.pending-*` directories left by interrupted saves. Status
falls back to an earlier valid generation with a visible warning if the newest is corrupt;
`verify` returns nonzero in that situation. Neither hashes nor the marker are signatures
or proof against a malicious writer. Do not copy these claims into acceptance records.

Milestones retain full selected files and are not automatically deleted. Keep the selection
small; coordinator-owned retention can prune superseded generations after acceptance while
preserving the latest recoverable work. The helper requires a local filesystem supporting
atomic directory rename and file/directory fsync; do not assume identical guarantees from
an arbitrary network mount. All agents write separate stores, even for the same task.
