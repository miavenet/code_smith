# Codex statusline and hook audit

The interactive footer uses this build’s native fields from `config.toml`: branch,
model/reasoning, used tokens, input/output token totals, context remaining,
five-hour limit, weekly limit, and current directory. The `/statusline` command
can also edit the same footer interactively.

The script prints branch, input/cached/output/reasoning/total token fields, and
5-hour/weekly percentages plus reset timestamps when Codex or a wrapper exports
the corresponding `CODEX_*` environment variables. Unknown values are shown as
`?`; no limits are guessed.

Lifecycle events are appended as one JSON object per line to
`$CODEX_HOOK_LOG`, defaulting to `$CODEX_HOME/logs/hooks.jsonl`. Each record
includes an ingestion timestamp, session id, turn id, event name, model, cwd,
permission mode, tool identifiers, and the complete event payload.

The hook commands are asynchronous except `SessionEnd`. They only log and do
not block or rewrite operations. See the official Hooks documentation for the
stdin event schema and async-hook limitations.

When launched by code_smith, `CODEX_HOOK_LOG` points to the invocation's private
`hooks/hooks.jsonl`. Records carry `code_smith` correlation fields and use the
Claude hook logger's redaction rules. `ExecStream.*` records come from Codex's
JSON exec stream rather than native hooks; the payload preserves that provenance.
Use `runner activity` to view recent events across parallel tasks.
