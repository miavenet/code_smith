# Watching headless agents

Verified with Claude Code 2.1.278 and Codex CLI 0.155.1.

Every Claude/Codex invocation has `activity.json` with its run/task identifiers,
log location, and hashes of the project's hook configuration and logger files.
The runner routes the existing loggers into that invocation's `hooks/` directory:

- Claude: `HOOK_LOG_DIR`, producing `hooks.jsonl`, readable `hooks.log`, timings,
  and the existing passive session scorecards.
- Codex: `CODEX_HOOK_LOG`, producing redacted JSONL with session, tool and task IDs.
  Codex's JSON exec events also pass through the same logger, explicitly marked
  `source=codex-exec-stream` and `ExecStream.*`. They remain available in `stdout.log`.

```sh
./runner activity latest
./runner activity latest --task decoder --tail 30
# For continuous display, poll this read-only command:
watch -n 2 ./runner activity latest --tail 12
```

`activity` works while the run lock is held. It does not resume or mutate the run.
Hook output is observational and never changes a gate, verdict, or acceptance.
No events means **no events observed**, not proof the model is idle. Native hooks
may be disabled or awaiting trust. Async events can arrive after an invocation
ends; activity reads the logs afresh. Rotation retains the current and preceding
log. Headless statusline/cost snapshots are not guaranteed; the final CLI usage is the
accounting source. For a call cut short before its final event, usage is read from the
provider's own record (`usage_source: provider-record` in `outcome.json`), and the run's
`STATUS.md` (and `STATUS.html`, the same page with links into the record) shows a running call's
tokens so far under "In flight", refreshed every `status_refresh_s` seconds (default 15) while the
call runs. `runner status --watch` prints the page again whenever it changes. A Codex call's
`outcome.json` also records the account's rate-limit window as the rollout's `token_count` rows
showed it during the call (`limits`; 05, Honest accounting).

The loggers attach `code_smith.run`, `code_smith.task`,
`code_smith.invocation`, and `code_smith.agent_kind`. Each invocation has its
own files, so parallel reviewers and resumed sessions cannot share timing state
or interleave unrelated task logs. Both loggers redact secret-shaped fields and
values, limit long strings, and create private log files. Logger failures are
passive. Existing Claude session scorecards remain observations, not automatic
termination rules.

## Configuration and qualification

Keep the project hook definitions and logger scripts available. Claude's
`ignore_user_config=true` still loads project/local settings. `doctor` copies
project hook assets into its scratch checkout and invalidates capability caches
when these assets change. Its report lists the event kinds actually observed.

Codex project hooks require an active trusted config layer and trust in the exact
hook definition. The runner does not automatically bypass hook trust. Review the
project definitions through Codex's hook controls when native hooks are desired.
The exec-stream bridge continues to provide tool observations without relaxing
that trust requirement. See the [official Codex hook documentation](https://learn.chatgpt.com/docs/hooks)
and [Claude hook reference](https://code.claude.com/docs/en/hooks).

## Claude activity depends on the driven project's own hooks

Native Claude hook activity is not something the runner switches on. It stays
entirely dependent on hook definitions that live in the work tree being
driven (the target project), the same way any other Claude Code project
would configure hooks:

- **The settings file that defines the hooks** is the target project's own
  `.claude/settings.json` or `.claude/settings.local.json`, at the root of
  the repository named on the command line — never a file belonging to the
  code_smith repository itself. `doctor` and every Claude invocation read
  these two paths, and `activity.assets` also returns `.claude/hooks/log-hook.py` (for Codex it
  returns `.codex/config.toml`, `.codex/hooks.json`, `.codex/statusline.py` and the same
  logger) when they exist; `ignore_user_config=true` still loads
  them, it only skips the user's own `~/.claude/settings.json`.
- **How `HOOK_LOG_DIR` reaches the logger:** before each Claude invocation
  the runner (`activity.prepare`) exports `HOOK_LOG_DIR` (and, for Codex,
  `CODEX_HOOK_LOG`) into the child process's environment, pointing at that
  invocation's own `hooks/` directory. That is the *only* wiring the runner
  does. The project's hook command must itself read `HOOK_LOG_DIR` from its
  environment and write its records under that path — a hook script that
  logs to a fixed location instead never produces activity the runner can
  see, even though the hook is correctly configured and firing.
- **What to copy into a target project to get activity:** this repository's
  own `.claude/settings.json` (the hook definitions) and
  `.claude/hooks/log-hook.py` (a logger that already honors `HOOK_LOG_DIR`
  out of the box). Copying both, unmodified, into the driven project's work
  tree is sufficient; `doctor` will pick them up on the next qualification
  because they are hashed into its cache key (`qualification.fingerprint`).

`doctor` distinguishes exactly these possibilities when a Claude profile
shows no observed activity, from cheapest to fix: no `.claude/settings.json`
(or `settings.local.json`) defining hooks at all; hooks defined but none of
them invokes a logger that writes to `HOOK_LOG_DIR`; or definitions that look
right yet nothing was recorded (worth checking the trust prompt, or whether
this profile's `ignore_user_config` or CLI flags cause it to ignore project
settings). Both exec-form (`command`/`args`) and shell-form (a single
`command` string, e.g. `python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/log-hook.py"`)
hook commands are recognized; a hook whose `type` is not `command` (a
`prompt` hook, for example) counts toward "hooks are defined" but never
toward "invokes a logger". If the settings files themselves cannot be
parsed safely (malformed or adversarially deep JSON), doctor says so rather
than guessing or failing qualification. This detection only reads the
project's own files — it never invokes an agent and never changes a
capability, gate, or acceptance.

## Observed live evidence

- A controlled Opus 5 reviewer read a nonce file using its read tool and returned
  the exact value.
- Codex executed `pwd` and completed normally.
  Native Codex hooks were not observed in this CLI build's `exec` path, including
  a separate probe with hook enablement and invocation-scoped trust. This is a
  host observation, not a claim that all Codex versions behave the same way.

Run `doctor` and a
small workflow on the target host before relying on its native hook behavior.

## Agent-written milestones and crash recovery

The [agent-checkpoints skill](../.claude/skills/agent-checkpoints/SKILL.md) adds explicit
progress against task requirements and durable copies of selected work. Give writers
that skill in their task brief and use the existing invocation-scoped
`HOOK_LOG_DIR/checkpoints` store. The helper writes a separate `milestones.jsonl` beside
the native logs; `runner activity` merges these events chronologically and labels them
**agent-reported**. The helper's `status` shows the full saved report and artifact hashes.

Checkpointing is opt-in and is not needed to keep files: the runner never resets the work tree
inside a task, and it tells a fresh call which files are its own unfinished work
([concepts](02-concepts.md)). What a checkpoint adds is the author's own notes (what is done,
what is next) and an earlier version of a file the author later overwrote.

Checkpoints survive worktree rollback because they live in the invocation record. They
are recovery material, not producer acceptance: reconcile the runner, verify the saved
bytes, compare the current base and inspect the diff before recovering any files. Read-only
reviewers retain their restricted tools; a coordinator must persist their emitted
milestones if no dedicated progress-writing channel is provided. Skill availability alone
does not activate checkpointing in an already-running or frozen workflow.
