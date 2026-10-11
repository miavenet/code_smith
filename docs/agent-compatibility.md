# Agent compatibility and limits

The adapters were verified against `codex-cli 0.155.1` and
`2.1.278 (Claude Code)`. Their local `--help` output confirmed the command-line flags used by the
adapters, including Codex's explicit `exec resume ID`, schema/output options and configuration
overrides. No real-model calls were made during implementation or verification.

## Completion and environment failures

Claude must exit successfully and return a successful terminal `result` with a valid answer.
Codex must exit successfully, complete the current turn, and provide a valid final agent message.
A stale output file, incomplete stream, malformed field or failed turn cannot produce success.
Codex event parsing is incremental; an early sandbox failure cannot disappear beyond the retained
log tail. Ordinary failed shell tests inside a successful session are not call failures.

The three [recorded fixtures](../tests/recorded/README.md) preserve a recorded live run's actual
stdout. The Codex author fixture contains a `bwrap` namespace startup failure followed by a
blocked answer and `turn.completed`. The adapter correctly returns an environment failure.
This host also reports the same namespace error when ordinary sandboxed shell tools start.
These observations do not qualify any live model/profile for repository access; run `doctor`
on the intended host before use. No adapter automatically bypasses sandboxing.

## Qualification

`doctor` uses a scratch Git repository and observes answers, hidden file contents, script-written
digests, file changes, explicit-session memory and an unchanged read-only sentinel. Events claiming
that a tool ran are not evidence. The boundary probe is a practical observed test, not a proof of
isolation against arbitrary hostile code. Gates and command agents retain the permissions of their
configured execution environment.

The cache key includes the profile, model, read-only mode, host, executable/argument-file hashes,
CLI version and known user/project settings-file hashes. Project settings files are copied into
the scratch repository; profile-specific external integrations still need the same dependencies
and access on the host. Configuration contents and environment dumps are not included in the
qualification metadata. Hashes cannot capture every external service or dynamic configuration
change; a run-time environment failure invalidates the cached entry (a network outage does not,
PROV-16), and `doctor --force` always repeats the probes. Resume rechecks the fingerprint before making calls.

The latest report is `.runs/qualification.json`; individual probe logs are under `.runs/doctor/`.
A run receives its qualification report and profile metadata. Probe costs incurred by that start
or resume are added to its known/unpriced accounting, also when qualification fails, and each
probe must fit the run's remaining dollar and token limits before it starts (05, Capabilities and `doctor`).
Cached probes are not charged again.
An explicit earlier `doctor` keeps its own spend in its report. Monetary reservation applies across agent
calls; Codex does not offer an in-call dollar limit.

## GitHub Copilot CLI

Built against `GitHub Copilot CLI 1.0.75` (Homebrew cask, `copilot --version`). No
Copilot prompt was ever run while building or testing the adapter: every fixture and the fake CLI
in `tests/fake_copilot.py` are **constructed**, not captured. The sources, in order of weight:

- `copilot --help` and `copilot help permissions | environment | config | billing | limits`:
  `--output-format json` ("JSONL, one JSON object per line"), `--stream off`, `--no-ask-user`,
  `--no-auto-update`, `--no-remote` ("disable remote control of your session from GitHub web and
  mobile"), `--allow-all-tools` ("required for non-interactive mode"),
  `--available-tools`, `--deny-tool` (denial beats every allow), `--disable-builtin-mcps`,
  `--model`, `--session-id` ("set the UUID for a new session"), `--resume=ID`, `COPILOT_ALLOW_ALL`,
  `COPILOT_HOME`, and the variables that choose the model or provider outside argv:
  `COPILOT_MODEL`, `COPILOT_OFFLINE` and `COPILOT_PROVIDER_*`. The adapter removes these and
  `COPILOT_ALLOW_ALL` from every call's environment, since none is part of the qualification
  fingerprint.
- GitHub's documentation (docs.github.com, Copilot CLI): a prompt can be piped to `copilot`
  without `-p`; the built-in tool names (`view`, `glob`, `grep`, `bash`, `create`, `edit`,
  `apply_patch`, `web_fetch`, `task`, ...) for `--available-tools`; the permission kinds `shell`,
  `write`, `read`, `url`, `memory`.
- The CLI's shipped files under `~/.copilot/pkg/<platform>/1.0.75/`: `schemas/session-events.schema.json`
  (the event envelope `id`/`timestamp`/`parentId`/`type`/`data`, `assistant.message`,
  `session.error` with `errorType`, `session.shutdown` with `modelMetrics`), `changelog.json`
  ("Exit with nonzero code when `-p` mode fails due to LLM backend errors"; usage metrics are
  persisted to `events.jsonl` when a session ends), and the prompt-mode code in `app.js`: each
  session event is written as one line except a fixed set (`assistant.usage`,
  `session.shutdown`, `permission.*` and other UI events); a closing `{"type":"result",
  "sessionId", "exitCode", "usage":{"premiumRequests", ...}}` record; exit 1 on a `session.error`
  other than `model_call` or a `policy_blocked`/`compaction_static_context_blocked` warning;
  read permissions approved and other unapproved requests denied rather than prompted; the
  stderr wording of a missing login or an unavailable model.
- One real `session.shutdown` row of an earlier interactive session on this host, for the shape of
  `modelMetrics.*.usage` (`inputTokens` includes cached input).

Unverified until `doctor` (or a first run) on the host shows it:

- that the events of a real call match the schema as parsed (field names, the final message being
  the main agent's last `assistant.message` without `toolRequests`), and that nothing else is
  written to standard output in JSON mode; the parser treats any other line as a malformed stream;
- the exact `session.error` types and wording of quota, rate-limit and outage failures in prompt
  mode; classification falls back to the shared quota/transient/environment text patterns;
- that `--available-tools=view,glob,grep` hides MCP and every other tool, and that a model can
  still answer with only those tools; `doctor`'s `boundary` probe is the check;
- that `session.shutdown` is written for a prompt-mode call before the process exits, and whether a
  resumed session's shutdown counts include its earlier calls (if they do, usage over-counts and the
  token stop line comes early, never late);
- whether a `copilot update` channel or a policy (`allowed_models`, organization MCP policy)
  changes any of the above. `--no-auto-update` keeps the binary, and so the qualification
  fingerprint, fixed during a run.

One live attempt was made with Copilot CLI 1.0.75: `runner doctor` on a profile
used as writer and reviewer. Every probe, and the CLI run by hand with a one-word prompt, ended
with exit 1 and `Error: Access denied by policy settings` (the account's Copilot CLI policy is
off), so none of the points above was verified. The runner classified that answer as an agent
error; it is now an environment failure, so `doctor` names the cause and no attempt is charged.
The constructed fixtures stay the only evidence until the policy is enabled and `doctor` is run
again.

Copilot repository hooks (`.github/hooks/*.json`) are part of the fingerprint, but the CLI loads
them in prompt mode only for a trusted folder, so no native hook telemetry is claimed for it.

## Gate preflight

`check-gates` requires a clean source tree and runs every command against a fresh local clone.
It includes standalone checks, distinguishes intended `new`-gate failures from missing commands,
timeouts and unmatched failure patterns, and lists changed or newly created files. A passing
`new` gate is an objection. Results and logs are retained under `.runs/check-gates/`. Ignored local
build products and virtual environments are not copied, so gates must create their own build
outputs or use host-installed dependencies. The source work tree is never used for gate execution.

## Review scope

Text-only review is explicit (`review_mode = "provided_context"`), with full file versions, the
complete diff and an evidence manifest. Binary blobs use base64. Oversized evidence or a prompt
that exceeds the supplied context budget is refused before the agent runs; nothing is silently
truncated.
