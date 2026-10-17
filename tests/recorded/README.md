# Adapter fixtures

The Claude and Codex files are unchanged copies of stdout from an earlier recorded live run of a
prototype; the run itself is not included. Their original paths in that run were:

- `claude-success.json`: `slug/attempt-1/implement/stdout.log`
- `codex-review.jsonl`: `slug/attempt-1/review/stdout.log`
- `codex-sandbox-failure.jsonl`: `cli/attempt-1/implement/stdout.log`

Their result schemas predate this runner: adapter parsing tests preserve those answers; current-schema validation is
tested separately. Failure variations are constructed in tests, not presented as live captures.

Two GitHub Copilot CLI fixtures are **constructed, not captured**: no Copilot prompt was run. Their
event shapes follow the session-event schema shipped with Copilot CLI 1.0.75 and the closing
`result` record of its prompt-mode code (see docs/agent-compatibility.md, GitHub Copilot CLI):

- `copilot-review.jsonl`: a review that reads one file, then answers; ends with `result` exitCode 0.
- `copilot-quota-failure.jsonl`: a `session.error` of type `quota`, then `result` exitCode 1. Its
  message wording is invented; classification uses `errorType`.

One Codex fixture is **constructed, not captured**: `codex-rollout-token-count.jsonl` holds the
rows of a session rollout (`~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl`) in the shape
a 16-minute review session showed on this host: `event_msg` rows of type `token_count` after each
model response, with the session's running `total_token_usage` and the account's `rate_limits`
(`limit_id`, `primary` and `secondary` windows with `used_percent`, `window_minutes`, `resets_at`,
`credits`, `plan_type`). Its numbers are invented. `tests/fake_codex.py` writes it into a scratch
`CODEX_HOME`, stamping rows marked `@before` an hour ago (an earlier turn) and `@now` as it runs.
