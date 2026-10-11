# 05 — Architecture

How the runner is put together. The model it implements is in [02 — Concepts](02-concepts.md).

## Modules

```
 workflow.toml + library/
          │
          ▼
    ┌───────────┐    expanded tasks     ┌────────────┐
    │  workflow │──────────────────────►│            │      ┌──────────┐
    └───────────┘                       │            │◄────►│  record  │ state.json, events,
    ┌───────────┐    prompts            │   engine   │      └──────────┘ STATUS.md, index.json
    │  prompts  │◄─────────────────────►│            │
    └───────────┘                       │ scheduler  │
                                        │ lifecycle  │
    ┌───────────┐ ┌────────┐ ┌────────┐ │ findings   │
    │  agents   │ │ checks │ │ gitops │◄┤ limits     │
    └───────────┘ └────────┘ └────────┘ └─────┬──────┘
    (also: panels, budgets, providers, qualification,
     preflight, replan, proc, activity: see the table)
                                              │
                                           ┌──┴──┐
                                           │ cli │
                                           └─────┘
```

| Module | Responsibility |
|---|---|
| `workflow` | Load the workflow, types and personas. Apply precedence. Expand panels. Validate. Give the fixed task order |
| `record` | The run directory: create it, save state durably, record intents and outcomes, append events, publish ledgers, write attempt, round and invocation files; coordinate derived-file writes |
| `status` | Render task pages, run pages and directory indexes from the loaded record; both run pages use one model, `run_status_model` |
| `prompts` | Render a type's template for a task in one pass, with fenced data blocks and size caps. Pure functions of the expanded task, the state and the upstream results |
| `patterns` | The one path-pattern matcher, and the conservative cover and overlap tests |
| `agents` | One interface, four adapters (Claude Code, Codex, GitHub Copilot CLI, any command). Build the command line, run it under the runner's clock with streamed logs, decide whether the call completed properly, and classify how it failed (`environment`, `quota`, `transient`, …) |
| `qualification` | `doctor`'s capability probes, the qualification cache, and attaching each run's qualification record |
| `preflight` | `doctor`'s free sandbox and feature checks, and `check-gates` in clean clones of the committed files |
| `panels` | Review panels: bounded parallel readers, their results applied in workflow order, voiding a panel when a reader writes to the tree |
| `budgets` | Reservations, settlement (known, reserved, unpriced and unsettled spend), the token stop line, and the pause and exhaustion stops |
| `providers` | Routing: qualified fallback profiles, quota cooldown, complexity-to-model choice |
| `replan` | `replan` and `--reopen`: before and after copies, the plan, resumable reverts |
| `proc` | Process groups, streamed and redacted output, shutdown |
| `activity` | Hook-log routing for headless agents and `runner activity` |
| `validate` | The runner's own small validator for its result schemas, and the semantic checks against the ledger |
| `checks` | Run gate and check commands with a timeout. Return pass or fail and the output |
| `gitops` | Work-tree snapshots pinned under private refs, changed paths, diffs, restoring paths by type and mode, full binary patches, the run branch, commits with operation ids, reverts |
| `findings` | The ledger: assign ids, apply responses and resolutions, enforce the later-round rule, decide whether any blocking finding is open |
| `transaction` | The producer's state machine: its steps, every task's statuses, the moves allowed between them, and the keys each boundary clears (W-04) |
| `schema` | The `schema_version` of `state.json` and `migrate`, which `Run.load` applies to an older state |
| `engine` | The scheduler and the task lifecycle. **The only module that decides anything**, and only from exit codes, verdict fields, ledger status and counters |
| `templates` | `runner new`: find a template in the libraries, check its parameters, render it in one pass with `for_each` copies, write the TOML, and keep the file only if the loader accepts it |
| `cli` | Commands, exit codes, printing |

`agents`, `checks`, `gitops` and `record` report facts. `workflow` and `prompts` are pure. That keeps
the part that must be deterministic (`engine`, `findings`) small and free of I/O details, so it can
be tested exhaustively with scripted agents.

## The engine loop
```
load state; reconcile (see Crash recovery)
repeat:
    apply finished work           (results are applied in workflow order, never arrival order)
    if nothing is running:
        if there is an active producer:
            start its next step: a gate or writing check (alone), its ready readers (up to
            max_parallel), its rework, its commit, or its set-aside
        else:
            start standalone ready readers (up to max_parallel), if any; when none are running,
            make the first ready producer in workflow order the active producer
    if nothing is running and nothing can start: stop
    wait for any running job to finish
    save state; publish changed ledgers through write_decision
    try rendering changed task directories and run pages; record render-failed and continue on error
```

Run status when the loop stops: `done` if every task is accepted (a verifier counts as accepted when
it passed); `stopped` if the budget ran out, the token stop line was reached or the owner paused
the run; `needs_human` if any task is `waiting_human` or `blocked`; otherwise `failed`. An
environment, network or provider stop, and a reconciliation or record error, also give `failed`.
Exit codes 0, 2, 255, 2.

Parallel jobs are subprocesses (agents and commands already are), supervised from one thread with
the standard library's `concurrent.futures`. There are no threads inside the decision logic.

One more thread only reads: while the loop waits (one agent call can last half an hour, and nothing
changes in the state meanwhile), a heartbeat rewrites `STATUS.md` and `STATUS.html` every
`status_refresh_s` seconds (default 15; 0 turns it off) from the state in memory, with each call's
age, attempt and step, the tokens the provider's record shows so far, and the tasks that come
next. It never writes the state, holds no lock the call needs, replaces the pages whole (a
temporary file, then a rename), and skips a beat whose rendering fails. The pages are derived
files, so it changes nothing that the durability rules govern.

### The producer transaction

Serial processes are not serial transactions. If producer A finished writing and independent
producer B started before A's panel ran, B would build on A's unaccepted changes, and B's changes
would appear in A's review and A's commit. So the unit of exclusion is not a process but the whole
life of a producer: **produce, gates, checks, panel, human decision, then commit or set-aside.**
While a producer is active, only its own steps are scheduled. The transaction ends when the work
tree is verified, by snapshot, to equal a clean accepted state. Anything that stops
the run while a producer is active keeps the transaction open, with the expected tree recorded so
`resume` can check it: a pending human verification, an escalated finding waiting for `resolve`, a budget or token stop, a `pause`, and an environment, network or provider stop. See
[02](02-concepts.md#scheduling).

A standalone check that writes is a small transaction of its own: BASE snapshot, run, restore to
BASE, verify.

### Reader–writer rule

Inside a transaction, either one writer runs or any number of readers run, never both. A `check` is
a writer unless marked `read_only`, because a build writes files. The mark is verified: a
`read_only` check that changes the snapshot fails as a check, voids the reviews that ran beside it
without using a producer attempt, and is a writer for the rest of the run.

### Producer lifecycle

```
become the active producer; snapshot BASE; pin it under refs/code-smith/<run>/
if `retry --apply-patch` queued a recovery: put the set-aside work back from its pinned candidate
tree (a `recover` intent of its own; the transaction opens with it already in the tree, and the
next attempt's author is told where it came from)
attempt n (own directory, numbered once, never reused):
  run the agent (continue the session on rework if qualified for `resume`; otherwise, and after an
                 error, timeout or interruption, a new session with the full prompt plus feedback)
  ├─ environment failure .............. stop the run; no attempt is used
  ├─ protocol failure ................. retry the call, at most twice, with no attempt used; if the
  │                                     third call is still invalid the attempt is spent: feedback,
  │                                     next attempt
  ├─ interrupted (the runner stopped) . call again on `resume`, same attempt, no try used; the
  │                                     fifth interruption sets the task aside BLOCKED
  ├─ quota (429, usage limit) ......... switch to the next qualified `fallback_agents` profile and
  │                                     put the provider on a five-minute cooldown; no attempt is used
  ├─ transient (5xx, overload) ........ call again; on a fallback profile from the second failure on;
  │                                     no attempt is used. When the retries run out: stop the run
  ├─ the answer is in; the attempt is counted in the same save that moves the step to `inspect`.
  │  A kill from here on resumes at `inspect` and costs no attempt (crash points `attempt:counted`,
  │  `inspect:after-pin`)
  ├─ outcome "blocked" ................ BLOCKED
  ├─ an output missing, a `removes` path present ... feedback, next attempt
  ├─ change outside `writes`, or to a protected or frozen file ... runner reverts it; feedback, next attempt
  ├─ an embedded repository, a submodule entry, a symlinked parent, or a declared path that git
  │  now ignores ...................... runner removes or reverts it; feedback, next attempt
  snapshot CANDIDATE; pin it
  ├─ over the task's diff budget, counted from BASE to CANDIDATE ... feedback with both counts,
  │                                     the limits and the five largest files; no gate runs; next attempt
  run own gates, then verifying checks, then regression gates of accepted tasks this one touched
  (expected-failure gates excluded: they are never re-run as regression gates)
  (a command already run against this candidate is not run again; the record says whose run it shares)
  │   after each: snapshot; the whole snapshot must still equal CANDIDATE. If not, the runner
  │   restores CANDIDATE; all results are void unless the check is marked `restores`
  ├─ fails ............................ feedback = output tail
  │                                     same failure AND same candidate tree as last attempt -> FAILED
  status = verifying; the panel becomes ready (review round r for each member, counted per reviewer)
  when the panel has finished (snapshot again: a reviewer that changed the tree fails the task):
  ├─ no member could give a valid answer BLOCKED (a person must repair the panel)
  ├─ an open finding is escalated ..... waiting_human; the transaction stays open
  │                                     resolve as resolved|advisory -> continue verifying, no attempt used
  │                                     resolve as upheld -> feedback, next attempt
  ├─ open blocking findings in ledger . feedback = cause + findings needing a response, next attempt
  verifying human tasks, if any ....... waiting_human; the transaction stays open
  │                                     reject -> feedback, next attempt
  ACCEPTED: write intent; one commit of exactly CANDIDATE, whatever the attempt count; sync the index;
            record; freeze
attempts used up -> FAILED if a gate or check sent it back last; BLOCKED, with open findings, if review did
FAILED or BLOCKED -> pin CANDIDATE, write failed.patch, restore the task's paths by type and mode,
                     verify the tree equals BASE, mark dependants skipped, end the transaction
```

### The producer's state machine (W-04)

The steps of the lifecycle above, and the statuses of every task, move only as these tables say
(`transaction.py`). Every step and status write goes through one of four calls: `move` (a step,
with a status or not), `set_status` (a status alone), `open_` (the transaction opens: no step to
`attempt`, `pending` to `running`) and `reset` (`retry`: a task outside any transaction back to
`pending`, no step). A move the tables do not allow raises a `RunnerError` naming the task and its
current and asked step or status, before anything is written; the run stops as `failed`. The
tables are what the runner does, checked by the whole suite, not what it ought to do.

| From step | To steps |
|---|---|
| (none) | `attempt` |
| `attempt` | `attempt`, `inspect`, `set-aside` |
| `inspect` | `attempt`, `verify`, `set-aside` |
| `verify` | `attempt`, `panel`, `set-aside` |
| `panel` | `attempt`, `verify`, `escalation`, `human`, `set-aside` |
| `escalation` | `attempt`, `human` |
| `human` | `attempt`, `commit` |
| `commit` | (none) |
| `set-aside` | (none) |

A move to `attempt` is a send-back (the next attempt), to `set-aside` an unsuccessful end, and
`panel` to `verify` a panel voided because a reader wrote. `commit` and `set-aside` end the
transaction. `open_` replayed over a half-installed opening (`resume` finishing a `recover`
intent) finds step `attempt` already, and is the same move.

| From status | To statuses |
|---|---|
| `pending` | `pending`, `running`, `waiting_human`, `accepted`, `objected`, `skipped` |
| `running` | `running`, `rework`, `verifying`, `accepted`, `failed`, `blocked`, `pending`, `skipped` |
| `rework` | `rework`, `verifying`, `failed`, `blocked` |
| `verifying` | `verifying`, `waiting_human`, `rework`, `running`, `accepted`, `failed`, `blocked` |
| `waiting_human` | `waiting_human`, `verifying`, `rework`, `running`, `accepted`, `objected`, `blocked`, `skipped` |
| `accepted` | `accepted`, `objected`, `pending` |
| `objected` | `objected`, `accepted`, `pending`, `waiting_human` |
| `blocked` | `pending` |
| `failed` | `pending` |
| `skipped` | `pending` |

One table serves every kind: a verifier (check, reviewer, person) moves between `accepted` and
`objected` as each candidate is judged, and back to `pending` for the next round or a `retry`; an
interrupted standalone check goes from `running` back to `pending`. A producer goes from
`verifying` or `waiting_human` to `running` when it is sent back while no attempt is counted: a
ruled, restored candidate is reverified without an attempt, so a check or a person who rejects it
sends it to the first attempt, not to rework. `failed`, `blocked` and
`skipped` leave only through `retry` (or a `replan`, which rebuilds the task's state).

Each boundary clears the keys that belong to the step it leaves, by rule rather than by a list at
each call site:

| Keys | Live | Cleared by |
|---|---|---|
| `pending_*` (attempt directory, protocol tries, calls, interruptions, the call in flight, a settled answer, a repair, a quota outcome), `provider_switched` | inside one attempt | every step move, so the move that counts, sends back or ends the attempt |
| `pending_set_aside` | inside step `set-aside` | the move out of it |
| `ruled_candidate` | from the opening to the reverifying attempt | every move out of `attempt`; the opening installs it again |
| `recover`, `git_repairs`, `ignored_since_base` | between transactions, or for one | the opening |
| `pending_*`, `panel`, `block_kind`, `block_reviewers`, `recover` | after an end | `retry` (which queues a new `recover` itself with `--apply-patch`) |

The invariants checked on every save (`invariants.py`) use the same scopes: a step outside the
table, or a `pending_*` key held outside its step, is a violation.

### Interrupted calls and protocol tries

An attempt has three protocol tries: one call and two retries. A try is charged when a call
**returns** with an invalid answer or a provider failure (`transient`), once per returned call
also when its settled answer is read again after a kill. A call that never returns is an
interruption: the call in flight is recorded with its intent (`pending_call`), and when `resume`
finds it with no settled answer it adds one to `pending_interruptions` (event `call-interrupted`)
and calls again in the same attempt. So `pause --now` or a crash never spends the attempt. Five
interruptions in one attempt (`MAX_INTERRUPTIONS`) stop it for a person: the task is set aside as
`blocked`, with no attempt used and a reason naming the count; `runner retry` starts it again.
`result.json` counts every call the attempt made in `agent_calls` (`pending_calls`).

A reviewer's call follows the same rule. Its try (one of the job's three) is charged when the call
returns; the call in flight is marked on the panel's job (`in_flight`, durable with its intent),
and when `resume` finds it with no settled answer it counts `interruptions` on the job (event
`call-interrupted`) and calls the reviewer again. At `MAX_INTERRUPTIONS` the job ends
`interrupted` and the producer is set aside as `blocked`, its reason naming the reviewer and the
count. A job saved by an older runner charged its try before the call and keeps it.

### The schema of `state.json`

`state.json` carries `schema_version`, the shape it was written in: 3 for this runner, absent (0)
in every state written before it. `Run.load` calls `schema.migrate` once, so the engine reads one
shape only; the migration writes, for each old shape, the value that means what it meant:

| Older shape | Migrated to |
|---|---|
| no `unpriced_call_reserve` (before BUD-17) | `-1` (`budgets.SERIAL_RESERVE`): unpriced calls reserve the whole remainder under the cap, one at a time, as they did |
| an agent intent without `token_reservation` (before BUD-12) | `token_reservation: 0`: the call counts nothing extra if its usage stays unknown, as before |
| a bare `apply_patch: true` (an older `retry --apply-patch`) | the queued `recover` it asked for, from the task's set-aside record (derived from the pinned tree if never published) |
| `pending_*` keys outside their step | dropped |
| a producer in an attempt, its tries charged before each call | `pending_calls` set to that count; a settled answer marked as paid; a call still in flight given its try back and marked in flight, so `resume` counts it as an interruption |
| version 1 (before `save_seq`, `pin_stale`, `agent_spans`, `token_remainder`, the `replay` intent and job, `replay_passed`, `standing_rulings`, a job's `in_flight` and `interruptions`, `before_work`, a repair's `hints`, a raised finding's `seat`) | unchanged: it holds none of them. Version 2 exists so that a version-1 runner refuses a state that has them instead of resuming it (it would send a `replay` job down the check branch) (REC-53) |
| version 3 (before a queued standing entry's `file`) | unchanged: its entries go to the workflow's `rulings_file`, as they did. Version 4 exists so that a version-3 runner, which would write `file` into the rulings file its reader then refuses, refuses the state instead (REC-53) |
| version 2 (before `cleanup_obligations` and their `abandoned` status and `ruling`, an outcome's `cleanup`, a job's `guard_problems`, an intent's `cancelled`, `standing_dropped_why`, `standing_pending`) | unchanged: it holds none of them. Version 3 exists so that every version-2 runner, which knew none or only some of them, refuses a state that may have them instead of resuming it (it would dispatch beside an open cleanup) (REC-53) |

The migration is made in memory; the first save writes it and a `state-migrated` event with
`from_version` and `to_version`, so a command that only reads (`status`, `runs`) writes nothing.
A state with a newer `schema_version` than the runner knows is refused.

### Review rounds

Producer attempts and review rounds are counted separately. A reviewer's **round 1 is its first
sight of a candidate**, on whichever attempt that happens, and is always a full review with the
full BASE-to-CANDIDATE diff; with `review_diff_from = "<task>"` the diff starts instead at the tree
of that task's accepted commit, read from the state when the panel is prepared and kept as the
job's `base` (with `diff_from` naming the task and commit), so the diff, the changed locations
and a `provided_context` reviewer's evidence all use that one range and a resume rereads it. In a later round, members with open blocking findings run in
**judge-the-fix** mode, and members who had passed run on the rework diff only (or not at all, with
`recheck_passed = "never"`). **The rework diff is per reviewer:** from the candidate that
reviewer last saw, kept in the ledger as `last_seen_candidate`, to the current one. After a plain
`retry` every member starts again at round 1, and old open findings are closed as `superseded`;
`retry --apply-patch` keeps them open and continues the rounds when every member last saw the
candidate it restores. A reviewer always runs in a new session, read-only. Whether a reviewer
blocks is computed from the ledger after its answer is applied; the model's `verdict` field must
agree or the answer is a protocol failure. **One narrow repair is allowed:** an answer whose
only defect is `resolutions` entries naming no finding in the ledger, in a round that requires none,
is accepted with those entries dropped — but only when the repaired answer still derives a block, so
the repair can never turn a pass into acceptance. A reader that writes to the tree voids the whole
panel in one saved transition (crash point `panel:void-recorded`); a resume continues the void and
never reuses that batch's answers. When one worker of a batch hits a fault of the runner's own, the
answers its siblings completed are still settled and kept; the faulted job's intent stays open for
`resume`, and then the fault stops the run. Each answer of a batch is settled as it returns (its
outcome, try and spend saved, crash point `panel:answer-saved`), not after the slowest sibling.
**The acceptance replay is a job of the panel (N3):** `prepare_panel` adds a `replay` job beside
the readers of a candidate whose replay has not passed (unless `replay_beside_panel = false`); it
takes no reader's place under `max_parallel`, runs the gates in its throwaway checkout in a worker
of the first batch, and `panel()` settles it first: a failure voids the panel and sends the attempt
back, and no further batch is dispatched. Under `run_budget_tokens`, reviews by an agent that
reports no cost join a panel batch while their bounded token reservations fit under the line
together (BUD-17); priced reviewers and checks still run alongside them. Because the coordinator applies a panel's members
to the ledger sequentially, in workflow order, eligibility is decided by the ledger at each
reviewer's own turn, not by the ledger a concurrent call happened to see.

## Agent interface
```python
class Agent:
    def run(self, prompt, *, cwd, invocation_dir, schema, session_id, model,
            timeout_s, budget_usd, read_only, env, on_start=None) -> AgentResult
    def capabilities(self) -> set   # what doctor has qualified for this agent, model and profile

AgentResult: status, text, structured, session_id, cost_usd | None, usage, error, seconds, usage_source
  status: ok | environment | protocol-error | agent-error | timed-out | interrupted | quota | transient
  usage_source: terminal | provider-record | unknown
```

A blocked producer's `block_kind` distinguishes a panel that failed at the protocol level
(every broken reviewer's status was `protocol-error`) from one that timed out or errored (`mixed`,
or absent when there was no protocol failure at all), classified from the broken reviewers' actual
statuses above, never assumed from the call site that reports the panel exhausted.

| | Claude Code | Codex | GitHub Copilot CLI | Any command |
|---|---|---|---|---|
| Invocation | `claude -p --output-format json` | `codex exec --json -` | `copilot --output-format json --stream off --no-ask-user --no-auto-update --no-remote`, prompt on standard input | configured `argv` |
| Structured answer | `--json-schema` | `--output-schema FILE`, `-o FILE` | none in the CLI: last JSON object in the main agent's final message | last JSON object in stdout |
| Continue a session | `--resume ID` | `exec resume ID`, never `--last` | `--resume=ID`; a new call is given `--session-id UUID` | not supported |
| Money limit | `--max-budget-usd` | none. Usage is reported at the end of a turn only | none. It bills premium requests (AI credits), not dollars | none |
| Unattended | `--permission-mode auto --permission-prompts none` | `-c approval_policy="never"` `-c sandbox_mode="workspace-write"` | `--allow-all-tools`; path checks (work tree, temp dir) stay on, URLs are not pre-approved. A YOLO profile adds `--allow-all-paths --allow-all-urls` | its own |
| Read-only | read/search tools only, no shell or MCP tools, plus the snapshot check | `sandbox_mode="read-only"` | `--available-tools=view,glob,grep --deny-tool=shell,write,url,memory --disable-builtin-mcps`, no `--allow-all-tools` | `read_only_args` |
| Proper completion | exit 0 and a `result` object that is not an error | exit 0 **and** a `turn.completed` event for this invocation | exit 0 **and** a closing `result` record with `exitCode` 0, nothing after it | exit 0 |
| Tool events in the output | **none**: print mode with JSON output is one result object | yes: `command_execution` items | yes: `tool.execution_*` session events | none |

Codex is given its sandbox as a config override because `exec resume` has no `--sandbox` flag, and
`--skip-git-repo-check` is not passed, since the runner requires a repository.

Copilot reads a piped prompt when `-p` is absent, so a large prompt never meets the argument
length limit. In prompt mode it approves reads, and denies every other permission request it
cannot ask about; `--allow-all-tools` is what lets a writer's tools run. A reviewer instead sees
only the read tools, the deny rules win over any approval stored in the user's configuration, and
`COPILOT_ALLOW_ALL` (the same switch as an environment variable) is removed from the call's
environment for both profiles, so the command line alone decides. For the same reason
`COPILOT_MODEL`, `COPILOT_OFFLINE` and every `COPILOT_PROVIDER_*` variable (a custom, "BYOK" model
provider) are removed: none of them is in the argv or the qualification fingerprint, so each could
make a profile run a model or provider that `doctor` never qualified. A profile names its model
with `model`; a custom provider is not supported. `--no-remote` keeps the session from being
remote-controlled from GitHub's web or mobile apps while it runs. Its `session.error` events carry
an `errorType`: `quota` and `rate_limit` (or HTTP 429) are a quota failure, `authentication` and
`authorization` an environment failure, a 5xx or the transient wording a transient one, anything
else an agent error; a `model_call` error the CLI recovered from decides nothing, as with Codex's
`error` events. A missing login or an unavailable `--model` is reported on standard error before
any event and is an environment failure. What is unverified without a live call is listed in the
[compatibility notes](agent-compatibility.md#github-copilot-cli); `doctor` proves a profile on
its host before any run uses it.

**YOLO profiles.** A Claude profile is made YOLO with `permission_mode = "bypassPermissions"`,
passed through as `--permission-mode bypassPermissions`. The same key on a `copilot` profile is
its YOLO switch: a writer call then gets `--allow-all-tools --allow-all-paths --allow-all-urls`
(what `copilot help permissions` says `--allow-all` and `--yolo` stand for, spelled out so
`argv.json` names each) in place of `--allow-all-tools` alone, so path verification and URL
approval are off too. No other `permission_mode` is accepted on a `copilot` profile. For both
kinds the mode governs writers only: a read-only call of a YOLO profile keeps the reviewer's
restrictions (Claude's `--tools`/`--disallowedTools`, Copilot's `--available-tools`/`--deny-tool`,
with no allow-all flag), and `doctor`'s `boundary` probe checks it. `validate` warns that the
profile bypasses its sandbox or permissions, and a fallback may not be YOLO unless its primary is.

### What counts as a result

An agent call has succeeded only when **all four** hold:

1. the process exited normally;
2. the stream holds a successful terminal event **belonging to this invocation**;
3. the final answer was written **by this invocation**: every call gets a fresh directory created
   exclusively, so a final-message file left by an earlier call can never be read;
4. the answer passes the **runner's own validation**, whichever agent produced it.

Validation is a small, deliberately limited checker for the runner's own schemas: types, enums,
required keys, no unknown keys. It does not claim to implement JSON Schema. On top of the shape it
checks the meaning: a verdict consistent with the ledger; resolutions that name only this
reviewer's own open **blocking** findings, each exactly once, none missing (advisory findings are
closed as `noted` when raised and are never asked about); responses that cover exactly the
findings that attempt's `feedback.md` lists as needing one; a `summary` within its length limit. A provider's schema feature is a convenience that makes valid answers
likelier. It is never the check.

A failed shell command inside an agent's session is not a failure of the call: a failing test is
ordinary work. What the adapter looks for is a failed **capability**, such as a sandbox that cannot
start.

### Failure classes and what follows

| Failure | Action |
|---|---|
| A failed Claude call (non-zero exit, `error_max_budget_usd`, `error_max_turns`, `error_during_execution`) | Its reported cost, usage and session id are recorded and charged. A turn or budget limit of the call itself is a task failure, not a provider failure |
| Sandbox or namespace startup, missing binary | **Environment failure.** Stop the run with the cause. No attempt is used |
| Authentication or configuration | Environment failure, before any producer attempt |
| Quota: a 429 `api_error_status`, `rate_limit_error`, "Claude AI usage limit reached" | Switch to the next qualified `fallback_agents` profile and put that provider on a five-minute cooldown. Not a producer attempt; a priced call refused for quota counts as nothing spent only when the adapter saw it end before any work and no usage contradicts that (`before_work`, BUD-15); otherwise it keeps its reservation as unsettled (Honest accounting). With no fallback left the run stops, saved work retained |
| Provider capacity, overload, a dropped connection (`transient`: 5xx, 529, `api_error`, "internal server error"), or a time-out of a review call | Another call within the same try budget, on a `fallback_agents` profile from the second failure on. Not a producer attempt; a review panel is not blocked by it. Every invocation's record is kept |
| The API cannot be reached (DNS, no network) | Environment failure: stop with the cause, no attempt used |
| No terminal event, or an invalid answer | **Protocol retry**, at most twice, fresh invocation directory. Not a producer attempt, not a finding |
| Three interrupted calls of one reviewer, none with an answer | That reviewer's result is `interrupted`, not a protocol error |
| A valid answer with `blocked` | The engine records `blocked` with the reason |
| A valid review leaving an open blocker | Producer rework, which uses a producer attempt |
| Timeout or interruption | Stop the process group, reconcile, abandon the session |

### Capabilities and `doctor`

Answering a greeting proves nothing about reading files. `doctor` qualifies each agent, model and
permission profile the workflow uses, **per capability**, in a scratch repository. **Every probe is
judged by an effect the runner observes itself, never by the agent's event stream:** Claude
Code's print mode emits no tool events at all, and a `command` agent has no stream, so evidence
"in the stream" would exist for Codex only.

| Capability | Needed by | How the runner checks it, without trusting the agent's word |
|---|---|---|
| `answer` | every task | The exact expected object comes back with a proper completion |
| `read` | repository-reading review | A random value exists only in a scratch file, not in the prompt; the answer must contain it |
| `execute` | authors, reviewers who run things | The agent is asked to run a probe script. The script writes the SHA-256 of a random value and a key to a file. A model cannot produce that digest without running the script; the runner reads the file |
| `write` | authors | The runner reads the expected change back from the scratch file |
| `resume` | rework, as an optimisation | A second call on the explicit session id returns a value given only in the first call |
| `boundary` | read-only reviewers | The agent is asked to change a sentinel file under the read-only profile; the runner checks the file is unchanged |

`doctor` first runs the free preflight (the Codex sandbox and the enabled features), printing
`note:` lines, before any model call. `write` is probed only for writer profiles and `boundary`
only for read-only ones. A task type states what it needs: `implement` needs `read`, `write`, `execute`; `code-review` needs
`read`. `resume` is never required. `doctor` refuses a workflow whose profiles lack a needed
capability, and `start` makes the same check against the cached qualification before it creates a
run; `validate` does not, because it must work with no agent installed.
Qualification costs money, so it is recorded in the run's accounting and cached by binary version,
profile hash, host identity and capability; `doctor --force` repeats it. **Host identity** is the
content of `/etc/machine-id` where it exists, otherwise the host name, joined with `uname -srm`. A
container that shares a machine id with its host but cannot start a sandbox differs in profile
behaviour, not identity, so a cached result is also discarded whenever a run meets an environment
failure, except one that says the network was down (the API could not be reached, `ENOTFOUND`):
that says nothing about the profile, and `resume` continues without probing again (PROV-16). The
cache lives in `.runs/qualification-cache.json` and is written like state: temporary
file, sync, rename.

Probes made for a run are held to its limits like any other call. `start` qualifies
against the workflow's `run_budget_usd` and `run_budget_tokens`, and `resume`, when a fingerprint
changed, against what the run has left: before each probe the dollar limit must cover its
reservation and, for an agent that reports no cost, the token stop line must not have been
reached. So the probe that crosses the line completes and no further probe starts; the overshoot
is at most one call. A qualification stopped this way is never cached. On `resume` the probes made
are charged to the run and the run stops as a budget stop (`stopped`, exit 2, `resume --add-budget`
or `--add-tokens`); a qualification that fails for any other reason is charged to the run too
before `resume` refuses. `start` has no run to charge yet: it creates none, and the probes' spend
is in `.runs/qualification.json`. `runner doctor` on its own belongs to no run and is not held to a
run's limits; its probes are capped at $1 each. A probe for a run is settled by the run's one
evidence rule: it spends nothing only with positive `before_work` and no usage; otherwise a probe
without a price holds its dollar cap as unsettled spend, and one of an agent that reports no cost
holds its probe-sized token bound while its usage stays unknown: what is left under the line, at
most `probe_reserve_tokens` (20,000 by default; a probe's prompt is a few hundred tokens), in
every run, one recorded before BUD-17 included, never the call reserve (BUD-24). A probe whose
own group outlived its stop is a problem naming the group, for `doctor` on its own too: the
command fails, the probes' spend is charged, and the profile is not cached (PROC-31).

**Review modes.** A reviewer with `read` does a repository review. A reviewer with only `answer`
may be used for **text-only review**, if the workflow says so explicitly
(`review_mode = "provided_context"`): the runner puts the complete evidence in the prompt and records
an evidence manifest, the record labels the review as text-only, and if the evidence does not fit
the context budget the review fails instead of proceeding on part of it. One mode is never silently
substituted for the other.

### Profiles and reproducibility

A frozen workflow does not by itself reproduce a call, because agents also read user and project
configuration: hooks, MCP servers, model defaults. Each agent entry in a workflow is therefore a
**named profile**, and the run records the agent's version, the profile's non-secret effective
settings and their hash. Ignoring user configuration is an explicit profile setting, never a
default, since it can drop integrations the owner wants. Credentials are never written to
`argv.json`, prompts or environment dumps. Gates and checks run with an environment that does not
include the agents' authentication variables.

### Processes and logs

Every agent and command has a supervisor in its own session/process group. Its standalone source
is captured when the process layer is imported and passed to Python with `-c`; it never imports
the runner checkout. Startup failure returns `not-started`, with the supervisor's stderr.
The supervisor waits on a release pipe; `on_start` saves its pid, pgid and start identity durably,
and both log sinks open, before release. EOF before release exits without executing the task.
After spawning the CLI, a short child barrier lets the supervisor capture its pid/start identity
before exec, even for instant commands. It reports that identity first; the runner amends the intent
with it before releasing task code. This second save cannot overwrite a concurrent task edit to the
record. Identity callbacks receive independent snapshots; runtime tracking cannot mutate another
reader's saved state. Once the leader save is acknowledged, abrupt runner death releases a recorded
orphan; caught interruptions explicitly cancel the waiting CLI. Every caller (an author's call, a
gate, a panel's reader or replay) hashes the record once, on the first call, and returns its crash
point to run after release, so a drill at `reader:running` or `replay:running` leaves a live
process `resume` must find (PROC-19, PROC-20). The supervisor reports the result
separately and remains while writers survive.
A live runner kills stragglers after collecting the result; cleanup errors accompany the finished
result in the process result's error and the invocation log. They also create a durable
`cleanup` obligation: status, error and group identity. Dispatch and acceptance stop until
resume confirms the group empty. The saved answer and its settlement are retained. A killed runner leaves a recognizable
invocation for resume. After a broken result pipe, the supervisor polls once per second.
On Linux the supervisor adopts orphan descendants, including those that call `setsid`; liveness
and shutdown include its descendants by ancestry as well as group membership. This is best effort:
a detached child cannot be rediscovered after both its supervisor and ancestry evidence disappear.
On macOS a `setsid` daemon escapes tracking. Claude's and Codex's detach behavior is unverified.
Standard output and error are
**streamed to files in the invocation directory as they arrive**, with a bounded tail kept in memory
for feedback, and the event stream is parsed incrementally. A crash of the runner therefore loses
no output, and a command that prints without end cannot exhaust memory. Unknown event types are
kept, for forward compatibility. At a deadline, and when the runner itself is shutting down, the
group is sent SIGINT, then SIGTERM, then SIGKILL. `env` carries `CODE_SMITH_RUN` and
`CODE_SMITH_TASK`, and `CODE_SMITH_RUN_DIR` only for types that set `needs_run_dir`.

## Git
The root must be a git repository; `validate` and `start` refuse one that is not.

One primitive does most of the work: `snapshot()` returns a git tree id for the whole work tree,
untracked-but-not-ignored files included, built in a scratch index so the real index is never
touched. The scratch index is **one file per run, reused** (`git-index` in the run directory), so
`git add -A` benefits from git's stat cache instead of hashing every file each time. Every snapshot the run relies on (each BASE, each CANDIDATE, each set-aside) is **pinned
under `refs/code-smith/<run>/…`**, so git's garbage collection cannot remove it while the run
exists.

| Need | Operation |
|---|---|
| What did this attempt change? | names between two snapshots |
| Changed outside `writes`, or a protected or frozen file? | match those names against the globs |
| Put paths back | With a separate index loaded from the target tree, `git checkout-index --force` for those paths. Git recreates each entry by its recorded **type and mode**: it unlinks first, so a symbolic link is replaced and never written through, and the executable bit is restored. Paths the target did not have are removed with `lstat` and `unlink`, and directories emptied by that are removed up to the root. Before either, every parent directory is checked to be a real directory inside the repository; a parent that is a symbolic link is removed as a link first. Embedded repositories and submodule entries never reach a restore, because they are removed after the attempt that made them. If a restore still cannot complete, it is an **environment failure**: stop, print the paths, leave the tree alone |
| Verify a restore | snapshot again: the tree id must equal the base's |
| The reviewer's diff | text diff BASE to CANDIDATE, capped for the prompt. **For reading only** |
| Recovery artifact | the pinned candidate tree, plus `git diff --binary --full-index` written in full. Never the capped text |
| Did a reviewer, gate or check change the tree? | snapshots before and after must be equal: the whole snapshot, untracked-unignored files included |
| Is a declared path ignored? | `git check-ignore -v`, at `validate` and after each attempt |
| Embedded repository or submodule entry in a candidate? | `git ls-tree -r` of the snapshot, looking for mode 160000 |
| Accept | see the recipe below |

**The commit recipe.** The run branch is checked out, so HEAD is its tip.

1. In a separate index: `git read-tree HEAD`, then `git update-index --cacheinfo` for each path the
   task changed, taken from CANDIDATE (or `--force-remove` for a deleted one); `git write-tree`.
   By then every change outside the task's `writes` has been reverted, so this tree equals
   CANDIDATE; the runner checks that it does and refuses to commit otherwise.
2. `git commit-tree` with `Run:`, `Task:` and `Operation:` trailers, parent HEAD.
3. `git update-ref HEAD <commit> <expected parent>`, which fails if the tip moved.
4. **`git read-tree HEAD` on the real index.** Without it the real index still describes the old
   tip: `git status` shows phantom changes and a later `git revert` refuses to run. This step writes
   nothing to the work tree. It is part of the commit operation's intent, so a crash between steps 3
   and 4 is repaired on `resume`.

The runner never runs `reset --hard`, `clean`, `stash`, `push` or `merge`. It touches only paths a
task changed, and only to return them to a state it recorded. `replan --reopen` undoes commits with
`git revert`, which adds commits and removes none.

`start` refuses a dirty work tree **without exception**, creates `run/<workflow>-<uuid8>` from
HEAD, **checks it out** (the tree is clean and identical, so nothing in it changes), and records the
base commit and the branch that was checked out before. With `branch = "current"` no branch is
created and commits land on the checked-out branch; `start` refuses a detached HEAD. When a run
ends, the run branch stays checked out.

Pinned refs are the runner's namespace, and they are cleaned up: `runner prune` deletes
`refs/code-smith/<run>/` for every run that is `done`, after checking that each set-aside task
still has its `failed.patch`. Refs of unfinished runs are kept. A run whose directory is not found in
the runs directory in use may live under another `--runs-dir`, so its refs are kept and named, and
deleted only by `runner prune --orphans`. Recovery patches and review diffs ignore the owner's
diff settings (prefixes, colour, textconv) and never run an external diff program.

### Clarifications

- **`.runs/` (or the directory named with `--runs-dir` / `CODE_SMITH_RUNS_DIR`), the lock and the
  qualification cache belong to the git repository, by default at its top**, even
  when a workflow's `root` is a subdirectory of it. The lock, the pinned refs and `prune` are per
  repository. The run lock lives in the common git directory, `code-smith/run-<digest>.lock`
  keyed by the runs directory's real path, so linked work trees sharing one runs directory exclude
  each other and `git clean` cannot remove it; `.runs/lock` is taken beside it for older runners
  (W-02). The record itself stays at `.runs/`: every document, tool and habit points there, and
  what a writer can do to it is covered by the pinned record and `repair-record` instead.
- **The pinned record (W-02, K).** `refs/code-smith/<run>/_record` is one tree of the decision
  files, moved before every save of `state.json` or `integrity.json`. Git writes the objects: the
  new blobs in one `git hash-object -w` (`--stdin-paths`; `events.jsonl` only when it grew and
  still begins with its pinned copy, read once so its size and id agree; an edited prefix keeps
  the earlier copy and pins the rest, REC-58), the changed trees in one `git mktree --batch` (children first; the runner
  computes the ids too and requires git's to match), then `git update-ref ref new old` moves the
  ref by compare-and-swap: at most three processes a save, and `core.fsync` (`committed` unless
  the owner set it), `core.sharedRepository` and the object format are git's. A covered file is
  hashed only when its manifest entry changes. A failed pin is logged and retried, never fatal; a
  moved ref stops the run and is never overwritten (`PinMoved`). At the first pin of every process
  the pinned state must be this run's and no more than one save ahead of the disk's. It is not
  part of a producer's lease (it moves at every save), and
  `prune` deletes it with the run's other refs. The superseded trees and state blobs are
  unreachable and left to git's garbage collection. Paths inside `gitops` are relative to the repository top; the engine translates task
  paths, which are relative to `root`.
- `refs/code-smith/<run>/` is keyed by the run **directory name** (`<workflow>-<stamp>-<uuid8>`), so `prune`
  can match a ref to its directory.
- **The owner's git hooks never run**: every git call the runner makes sets `core.hooksPath` to
  `/dev/null`. A pre-commit hook must not be able to change or block an accepted commit.
- An embedded repository **with no commit** makes `git add -A` fail outright, so the mode-160000
  scan alone is not enough. The runner first walks the work tree for nested `.git` entries and
  removes those directories, then takes the candidate snapshot.
- A revert is made with `git revert --no-commit` followed by a commit carrying `Operation: <op>/<n>`
  and `Reverts:` trailers, which is how a resumed `--reopen` recognises the reverts already done.
- A detached HEAD is refused only with `branch = "current"`. With a run branch, `original_branch`
  is recorded as null.
- **Holding the lock's `flock` is what owns it.** The lock is an `flock` on `.runs/lock`, held for
  the runner's lifetime, so a crashed runner's lock is released by the kernel, and the file it
  leaves behind is taken over. A runner writes the file only while holding that flock, and never in
  place: a new lock, or a takeover of a stale one, is written and locked under a name of its own and
  then linked (or renamed) to `.runs/lock`. So a kill at any point leaves either the old holder or
  the new one, whole. An empty or unreadable file whose flock is free is nobody's write in progress
  (an older runner killed half-way through a takeover) and is taken over too; a file that
  names a live process is still respected.
- **A revert never sweeps in the owner's changes.** If the index or a tracked file holds changes of
  the owner's when `resume` or `replan` replays a reopen revert, it stops with `reverting <sha>
  conflicts: the index or the work tree has changes of its own: …; commit or stash them, then
  resume`. Nothing is committed or discarded.
- Sparse checkouts keep their skip-worktree bits after the runner commits, and a committed file that
  `.gitignore` also matches does not break restore verification.
- Orphan detection enumerates live group members, excluding zombies, using `/proc` on Linux and
  libproc on macOS. The supervisor preserves the group identity beyond the CLI leader. Recovery
  checks all supervised invocations before touching the work tree; `--stop-orphans` escalates
  until the invocation is empty. A live supervisor authorizes group signals; if it was killed,
  a surviving CLI or member with a saved start identity authorizes recovery to capture the other
  group members' start identities and signal verified members individually. A reused supervisor pid never authorizes
  a numeric group signal. Shutdown retains member identities across escalation, including Linux
  descendants outside the group. Old records without `process.group` keep their leader-only
  behavior; group records with no surviving saved identity require manual inspection.
- Checking out the run branch at `start` is an effect with its own intent and reconciliation.

## Crash recovery

An atomic `state.json` protects the state file. It cannot make a subprocess, a file write, a commit
and a state update happen together. So every external effect follows **intent, effect, outcome**:

1. **Intent.** Before the effect, the state records an operation with a unique id, its kind, and
   what is expected: for an agent call, the invocation directory; for a commit, the expected parent,
   the candidate tree, the task and the attempt. State is written to a temporary file, flushed and
   synced, renamed, and the directory synced.
2. **Effect.** The agent runs, or the commit is made with the operation id in its trailer.
3. **Outcome.** The state records the result and clears the intent.

**Every external effect has an intent**, not only agent calls and commits. Most are made
idempotent, so reconciliation is "do it again":

| Effect | Intent records | Reconciliation |
|---|---|---|
| Pin a ref | ref name, tree id | Run `update-ref` again. Same result |
| Restore paths (set-aside, out-of-`writes` revert, restoring a candidate after a verifier) | the target tree id and the path list | Run the restore again, then verify by snapshot against the target tree. Restoring is idempotent because it always writes from the pinned target |
| Write `failed.patch` | candidate and base tree ids | Regenerate from the two pinned trees |
| Put set-aside work back (`recover`) | the task, attempt, the commit HEAD was on, the target (candidate) tree, the base tree, the expected result tree and the paths | Refuse unless `git.head()` still equals the recorded commit (the branch moved); otherwise restore the paths from the pinned candidate tree again, verify by snapshot against the expected tree, and open the producer's transaction from the intent, whatever the state on disk already showed |
| Replay the gates on a clean checkout (`replay`, W-03) | the task, the candidate tree and the checkout's directory, named before it exists; each replayed gate has its own `command` intent (beside a panel, N3: one `command` intent for the replay, amended with each gate as it starts) | Settle the commands first (a live one is an orphan, as for any command), then remove the directory and record the replay as interrupted. `verify` runs again whole on resume; beside a panel, the panel step runs the replay again with the readers not yet answered, keeping the answers saved |
| Write or replace a decision file: `set-aside.json`, `verification.json`, `decision.json`, `findings.json` (`decision`) | the whole payload (for `set-aside.json`, including `at` and `reason`) | Write the file and its integrity-manifest entry again from the payload, before any integrity check runs. Rewriting identical bytes is skipped |
| Count an attempt | — (the step `inspect` is saved with the counter) | Resume at `inspect`: read `result.json` and look at the tree again. No attempt is spent twice |
| Void a panel because a reader wrote | the panel's void record, saved before the tree is restored | Continue the void; do not reuse that batch's answers |
| Record which paths were put back (`reverted.json`) | — | Written before each restore and merged across a crash |
| Create the run branch at `start` | the `branch` intent, in the run's first saved state | A run interrupted before its branch existed is resumed by creating it |
| Index sync after a commit | the commit id | `git read-tree HEAD` again |
| A revert during `--reopen` | the ordered list of commits, and how many are done | If `REVERT_HEAD` exists, `git revert --abort`. Then continue from the first commit whose revert is not on the branch, recognised by its `Operation:` trailer |
| Take the repository lock | run id, process identity | See process identity below |
| Close a directory (add its files to `integrity.json`) | the directory | Hash again. A finished directory does not change, so the result is the same |
| Write the qualification cache | — | Written like state: temporary file, sync, rename. A leftover temporary file is ignored |

On `resume`, every intent without an outcome is reconciled before anything else happens:

| Found | Meaning | Action |
|---|---|---|
| A commit intent, and the branch tip carries that operation id and the expected tree | The commit happened; the crash came before recording it | Record acceptance. The author is not run again |
| A commit intent, and the tip is still the expected parent | The commit did not happen | Verify the tree still equals the candidate, then commit |
| A commit intent, and the tip is anything else | Someone changed the branch | Stop with a reconciliation error that says what was expected and what was found |
| An agent or command intent whose process group is still alive | The previous runner died and left invocation processes running | Refuse to continue until it is stopped; `resume --stop-orphans` stops it. Never start a second author beside it |
| An open cleanup obligation (a finished call whose group was not confirmed empty) | Its cleanup failed; the answer is saved | A group already gone is closed without a signal; a live one refuses a plain `resume` and is stopped only with `--stop-orphans`; `--abandon-cleanup` closes it as a person's ruling, signalling nothing (with `--stop-orphans` too, only the groups of interrupted calls are then stopped, PROC-28). The jobs of its commands run again; a replay checkout it ran in is removed after it (PROC-22, PROC-23) |
| An open cleanup obligation of a reader cancelled before release (its own group stop failed, the group alive) | The call or command did not run; its cancelled outcome is settled, with nothing spent | The same as any open cleanup: no interruption is counted, and the job runs again once it closes; a replay keeps its intent and checkout until then. An intent the first version-3 runner kept `cancelled` for it becomes this obligation (PROC-25, PROC-29) |
| An agent intent with no live process and no terminal event | Completion unknown | Mark the invocation `interrupted`, never successful. Abandon the session. The interrupted invocation keeps its directory; the retry gets a new one. For a producer the call is made again in the same attempt and charges no try ("Interrupted calls and protocol tries") |

An invocation is identified by its supervisor pid, group id **and its start time**, not by a pid alone, since pids are
reused. The repository lock in `.runs/` keeps the runner's own pid/start identity. **How the start time is read:** on Linux, field 22 of `/proc/<pid>/stat` (clock ticks since boot), read after the last `)`
because the command name may contain spaces, together with the boot id from
`/proc/sys/kernel/random/boot_id`, so a reboot cannot produce a false match. On macOS it is the start
time to the microsecond from libproc, with the boot session UUID (`kern.bootsessionuuid`); both are
recorded as `start_ticks` and `boot_id`. Elsewhere, and for a process libproc may not describe, the
runner falls back to `ps -o lstart= -p <pid>` and says in `doctor` that orphan detection is weaker
there; an older `lstart` identity is still recognised. The lock is an `flock` on the lock file,
held for as long as the runner lives; a lock whose recorded process is gone is stale and is taken
over, and two runners that find the same stale lock cannot both take it. Commands that change a run
(`resume`, `retry`, `approve`, `replan`, …) take the lock first and read the state afterwards.

While a run is paused, for a person or for budget, its expected branch tip and tree are recorded,
and `resume` checks them first.

## Limits, all checked before a call is made
| Limit | Default | Enforced by |
|---|---|---|
| `max_attempts` per producer | 3 | engine |
| Protocol retries per call | 2 | engine. Separate from attempts |
| No-progress stop | same gate failure **and** same candidate tree | engine |
| `timeout_min` per agent call | 30 | runner's clock |
| `gate_timeout_min` per command | 20 | runner's clock |
| Diff budget: `max_changed_files`, `max_changed_lines` per produce task | none; the `fix` type 5 / 150, `refactor-step` 15 / 300 | engine, step 3b, from the pinned BASE and CANDIDATE trees, before any gate |
| `budget_usd` per agent call | 5 | the agent, where it can |
| `run_budget_usd` | 50 | engine, **by reservation** |
| `run_budget_tokens` | 0 (no cap) | engine, as a **stop line** on unpriced usage: no call of an agent that reports no cost starts once the run's unpriced tokens (in plus out, plus the reservations of calls whose usage stayed unknown, BUD-12) reached it |
| `max_parallel` readers | 4 | scheduler |
| Provider failures per call | retried, never charged to an attempt | engine and `providers`; a fallback profile from the second failure, a five-minute cooldown after quota |
| One run at a time per repository | | an `flock` on `.runs/lock`, which also holds run id, process group and start time |

**Reservation.** For an agent that enforces a per-call cap, the engine reserves that cap from
the run budget before starting the call and replaces the reservation with the actual cost when it
ends. A call is not started unless its whole reservation fits. So four reviewers cannot each start a
$5 call with $1 left: none starts, and the run stops as out of budget.

**Honest accounting.** `STATUS.md` and `run.json` report separate numbers, never one: **known spend**,
**reserved**, **unpriced usage** (tokens from agents that report no cost, and calls that ended
without the provider's final event), and **unsettled spend**: the cap held for a call of an agent
that reports cost and ended without a price (a timeout, an error, an interruption). Unsettled
dollars count against the run's dollar limit, and those calls' tokens are recorded with them rather
than under unpriced usage. Observed usage is never discarded. A call refused for quota, or ended by
the environment (an outage, failed authentication), counts as nothing spent only with positive evidence of no work
and no usage contradicting it. Absence of usage alone proves nothing. When the provider's record says the
session used tokens before it failed, they are kept: a priced call keeps its reservation as unsettled
with those tokens, an unpriced call adds them to unpriced usage.
`STATUS.md` shows it in the Spend line when it is not zero, as `$X unsettled (N calls ended
without a price)`. Unpriced usage never counts as zero dollars against a limit,
and is never converted into invented dollars. A call that ended without its final event (the runner
was stopped with `pause --now` or died, the call timed out, the agent was killed as an orphan) is
not written off as unknown when the provider's own record says what it used: every new Claude call
is given a session id of its own, so its transcript under `~/.claude/projects/<cwd>/` can be read;
a Codex call's thread id is in the `thread.started` event streamed to `stdout.log`, and its rollout
under `$CODEX_HOME/sessions/` carries a `token_usage_record` per response, or (the current CLI) an
`event_msg` row of type `token_count` after every model response, whose `total_token_usage` is the
session's running total: the call's usage is the last such row within the call less the last one
before it, with cached input and reasoning output tokens kept as `cached_tokens_in` and
`reasoning_tokens_out`. Only rows stamped after
the call began count, once per response. Copilot's JSON stream carries no token counts at all, so
every Copilot call, successful or not, takes its usage from the `session.shutdown` row the CLI
writes to `$COPILOT_HOME/session-state/<session>/events.jsonl` as the call ends: per-model input
and output tokens, and the premium requests used (kept as `premium_requests`, never turned into
dollars). A call killed before that row is written has unknown usage. The tokens go to unpriced usage with `usage_source:
provider-record` in `outcome.json`; the dollars of such a Claude call stay unknown, since only its
final event prices it. A call whose record cannot be found is still `unknown usage`. The same
record feeds the heartbeat: while a call runs, its "In flight" line in `STATUS.md` shows the tokens
used so far. It is a readout of the provider's record, not a cost: the runner has no price sheet of
its own, cache reads and writes are priced differently per model, and a subscription bills no tokens
at all.

**Rate-limit windows.** A subscription is limited by windows, not dollars. After every Codex call,
successful, failed or interrupted, the runner reads the `token_count` rows its rollout stamped
between the call's start and end; their `rate_limits` give the plan, the `limit_id` and, per window
(`primary`, and `secondary` when there is one), the length in minutes, the percentage used and
when it resets. The call's `outcome.json` records `limits` from the first and the last of those rows
(`used_percent_before`, `used_percent_after`, `delta_percent`, `resets_at`, `credits`). The first row
is written after the call's first response, so the delta leaves out that response's share; a window
that reset during the call counts as its new percentage. No rows: no `limits`, and usage stays as
it was. The window is the account's, shared with all its other use, and calls that ran side by
side (a panel) each see the others' share, so no figure of it is the run's own consumption. The run
keeps what it observed per window in `spend.windows` ("<kind>:<limit_id>"): timestamped
observations and, per reset of the window (`resets_at`), the lowest and highest percentage seen,
whatever order the calls ended in. The Spend line and `runner runs` show those ranges: `Observed
Codex weekly account window: 2.0% → 3.0% (shared-account observations, not attributable run
usage)`, with "; reset, then 0.0% → 1.0%" after a reset (BUD-21). It is a reading, never a limit.
The Spend line also gives the share of the unpriced input tokens the provider read from its cache
("1040000 tokens in, 66% cached"): the token cap counts them at par, and the share says how much
of the figure is cache reads.

**Estimates from the owner's rates.** A profile of an agent that reports no cost may give
`price_per_mtok` (03). Each of its calls with observed tokens then records `estimated_usd`, and the
run sums them in `spend.estimated_usd`, shown as `≈$X estimated from profile rates`. An estimate is
not known spend: it is never added to `known_usd`, and it does not count against
`run_budget_usd`, because the rates are the owner's assumption, not the provider's bill. A profile
that also sets `estimated_counts = true` is the owner choosing otherwise: its estimates are held in
`spend.estimated_counted_usd` and count against the dollar limit exactly like known spend, so no
call starts once known, unsettled and counted dollars reach `run_budget_usd`. Calls with unknown
usage have no estimate either way. Codex reports usage only when a turn completes, so it offers no
in-call token or money cap: a Codex call is bounded by time and attempts, and the documentation of a
workflow that uses it must not promise a dollar ceiling. What a workflow can promise is a **stop
line**: with `run_budget_tokens` set, no call of an unpriced agent starts once the run's unpriced
tokens (in plus out, the run's qualification probes included) reached it. The calls that cross the line complete,
so the overshoot is at most one call's overrun per call that ran side by side (BUD-17). The stop is a budget stop like the dollar one: `stopped`,
exit 2, the tree held, `resume --add-tokens N` raises the line and is an event.

**Unknown usage under the stop line (BUD-12).** A call whose usage stays unknown, with no usage in
its final event and none in the provider's own record (a Copilot call killed before
`session.shutdown`, an interruption with no transcript, every call of a `command` agent, which
reports none), cannot be measured, and leaving it out could let the run go on past the line
indefinitely. So under `run_budget_tokens` it is held at its **reservation**, recorded in the
call's intent as `token_reservation` and so the same after a crash and `resume`. The reservation
is bounded (BUD-17): what is left under the line after the reservations of the calls starting
beside it, at most `[defaults] unpriced_call_reserve`, or, when that is 0, twice the largest
measured unpriced call of the run (`spend.unpriced.largest_call`) and at least 500,000 tokens.
Unpriced reviewers join a panel batch while their reservations fit under the line together. The
bound decides admission only (BUD-20): a call that ended without its final event (a time-out, a
kill, a provider failure after work began) with unknown usage may have used all that was left, so
it counts `token_remainder`, the rest of the line when it started (recorded in its intent), but
never more than is left when it settles: calls started together share one remainder, so two such
reviewers hold it once between them, not twice; the run then stops and `--add-tokens` decides. A call that completed without reporting usage (a `command` agent) counts
its reservation, and `largest_call` grows only from measured calls. An intent recorded before this
has no `token_remainder` and counts its reservation. A run
recorded before this (no `unpriced_call_reserve` in its state) keeps the old rule: the whole rest
of the line, one unpriced call at a time. Held tokens count against the line but are kept apart
from measured ones, in `spend.unpriced.counted_calls`, `counted_tokens` and `held` (the last ten
held calls: task, invocation, seconds, status, error, tokens); the stop reason and the Spend line
say `N measured + M held` and name the calls (BUD-16), and the stop writes `run-stopped` like
every stop. Quota, environment and transient failures are free only when the adapter saw them
end before any work (`before_work` in `outcome.json`, kept when the
answer is read back after a kill: Codex's whole stream holds no `item.*` event, no completed turn
and no malformed or withheld line, so an item after a retryable error is work; Claude's error
envelope holds `num_turns` or `duration_api_ms` and every one it holds is 0, and its transcript,
when the runner can find it, has no assistant message since the call began; a transcript it cannot
find proves nothing, so then only `duration_api_ms` 0 is free) (BUD-15, BUD-19, BUD-22). Any of these failures after work began, with no usage, stays held. With no cap
nothing is held. Records written before this have none of these fields, and an intent without
`token_reservation` holds nothing.
Qualification probes for a run are counted the same way (PROC-31); `doctor` on its own is not.

## Command line

```
runner validate WORKFLOW              check everything; print the expanded DAG in execution order, each gate
                                      marked `(new)` or `(must FAIL, matching /…/)` as it applies
runner graph WORKFLOW [-o FILE]       write the DAG as Graphviz DOT
runner new TEMPLATE -o FILE [--params P.toml] [--set NAME=VALUE]... [--library DIR]... [--root DIR] [--force]
runner new --list [--library DIR]...  runner new TEMPLATE --describe [--library DIR]...
                                      write a workflow file from a template in library/workflows/ (implementation,
                                      debugging, refactoring); kept only if it loads with no error. `--list` lists the
                                      templates, `--describe` a template's parameters
runner doctor WORKFLOW [--force]      qualify each agent, model and profile per capability; refuse a workflow that needs more
runner check-gates WORKFLOW           run every gate and check in a clean clone of the committed files; report
                                      pass, fail, error or objection for each; `fails as intended` where a failure is the
                                      expected one (`(waits for TASK)` when it names an upstream task's output that
                                      does not exist yet, a glob output by its literal prefix, also when it cannot
                                      run because the script it runs is that output), and `fails (no fail_pattern to
                                      confirm the reason)` for a `new` gate with no `fail_pattern`. An `expect =
                                      "fail"` gate that already fails with its pattern is `pass: the defect already
                                      reproduces through existing files`; one that passes is `pass: it passes on the
                                      untouched tree; the task must make it fail`; a timeout is always `error`
runner start WORKFLOW                 create a run and execute it
runner resume [RUN] [--stop-orphans] [--abandon-cleanup [--by NAME]] [--add-budget USD] [--add-tokens N]
                                      reconcile, then continue a run (default: the latest unfinished one).
                                      --abandon-cleanup closes an open cleanup without a signal, as a person's ruling (PROC-23),
                                      checked after the record's integrity; --by only with it (PROC-27); with --stop-orphans
                                      too, the cleanups are abandoned and the groups of interrupted calls stopped (PROC-28);
                                      --add-budget raises the run budget and is recorded as an event;
                                      --add-tokens raises run_budget_tokens the same way (refused when the run has no cap)
runner status [RUN] [--rebuild] [--watch [N]] [--open] [-C DIR]
                                      print STATUS.md; --rebuild regenerates all derived files and is refused
                                      (exit 2) while a runner works on that run. Plain `status` only reads
                                      while a runner works; otherwise it takes the repository lock briefly
                                      to regenerate files. --watch prints the page again whenever the file
                                      changes (its mtime, checked every N s, default 5) until Ctrl-C or
                                      SIGTERM, then exits 0. --open opens STATUS.html with `open` (macOS)
                                      or `xdg-open` (Linux), or prints its path
runner activity [RUN] [--task TASK] [--tail N]
                                      recent native hook events of the headless agents
runner pause [RUN] [--now] [--wait MIN]
                                      stop a running run before its next call (or interrupt the call in flight
                                      with --now); prints the resume command
runner runs [WORKFLOW]                list runs with status, known spend (and unsettled spend, estimates and
                                      rate-limit windows, when any) and date; WORKFLOW is a file or a name,
                                      and without it every run in the runs directory is listed
runner approve RUN TASK [-m NOTE]     a person approves a human task
runner reject RUN TASK -m NOTE        a person rejects; the note becomes feedback
runner retry RUN TASK [--apply-patch] fresh attempts for a failed or blocked task, or for a pending task
                                      that a replan left with an unconsumed set-aside record. Refused
                                      while another task's transaction is open, naming the task the run
                                      waits on
runner resolve RUN FINDING --as resolved|advisory|upheld [-m NOTE]
                                      a person settles an escalated finding; then `resume`
runner replan [RUN] [--workflow FILE] [--reopen TASK]   bring an edited workflow into the run, where safe
runner prune [--orphans]              delete the pinned refs of finished runs; --orphans also those of
                                      runs whose directory is not found in the runs directory in use
```

Every command except `validate`, `graph`, `new`, `doctor`, `check-gates` and `start` also takes `-C DIR`, a
directory inside the repository. `runner --version` prints the version. Any `OSError` prints
`runner: <strerror>: <filename>` and exits 2.

`RUN` is a directory name, a UUID prefix, or `latest`.

`runner --runs-dir DIR COMMAND …` keeps the record in `DIR` instead of `.runs/`; the option comes
before the command (after it, the runner refuses with a hint) and every later command on those runs
needs it too. `CODE_SMITH_RUNS_DIR` does the same, and a workflow's `[defaults] runs_dir` is used
when neither is given and remembered by `start`. A relative path is relative to the repository top
in all three (see 04, Where it lives). The `runner` script checks the Python version before any
import that needs 3.11 and says `code_smith needs Python 3.11 or newer (found X)`.

| Exit | Meaning |
|---|---|
| 0 | Every task is accepted |
| 2 | Something failed, the budget ran out, or the workflow or environment is wrong |
| 255 | A person is needed |

## What is deliberately left out of the first version

| Left out | How it will fit later |
|---|---|
| Parallel writers | One git worktree per running producer, merged in workflow order. The scheduler's reader–writer rule becomes per worktree |
| Conditional routing | A `when` expression on a task over upstream results. The DAG stays acyclic |
| Dynamic tasks (a planning task that emits tasks) | `replan` already adds tasks to a live run; a `plan` type would feed it, behind a human approval |
| The hook logger's session scorecard as a stuck signal | The engine reads it after a producer's attempt; red means abandon the session |
| Importing Attractor DOT | `graph` exports DOT now; importing needs only a parser, since the engine's model is a superset of a linear Attractor pipeline |

## Implementation notes

`proc.py` is the shared process and redaction layer under `agents.py` and `checks.py`. Both agent
calls and gate/check commands have durable intents and recorded process identities. Resume refuses
a live child unless `--stop-orphans` is supplied, then reruns incomplete verification. An interrupted
standalone check first restores its pinned base. Verifier prerequisites are scheduled before the
producer starts so they cannot require another writer while it holds the tree.

`qualification.py` owns capability probes and their cache; `preflight.py` runs gates in disposable
local clones. `agents.py` implements the Claude Code, Codex, Copilot and command adapters, plus the explicit
text-only single-review helper. `proc.py` feeds redacted lines to the incremental Codex reducer.
CLI shutdown catches SIGTERM, stops the active child group and leaves durable intents for resume.
Known CLI versions and qualification limitations are in [agent compatibility](agent-compatibility.md).


Standing rulings are validated project TOML, read from the run's starting commit (never the
work tree), protected in every task, frozen in the expanded workflow with their blob id before
authoring, and matched by task, known location and optional hash of the effective brief
(`Engine.effective_brief`: prompt, project rules, gates, outputs, upstream ids and outputs)
during panel preparation. The prepared panel records which entries were supplied and why any
were dropped; downstream input assembly carries matching entries as owner context. An export
from `resolve --standing` is queued in the state with the ruling, bound to its rulings file,
and written at `finish_run` when the run is done, or by `runner export-rulings` for a run that
never is (`engine.export_queued`, one writer for both). The command takes only a run where
nothing can touch the work tree again (RUN-81): every task terminal, no transaction, no
operation to reconcile, no open cleanup. A running check would restore its base over the file
on `resume`, and a producer left needs a clean tree. The owner then commits the file; `resume`
(`record._export_committed`) and `replan` take a tip moved only by the exported files, and
`retry` adopts it. The export's message and STATUS take their next steps from one helper
(`status.export_next_steps`, `status.export_ways_on`).
Review answer validation accepts optional gate hints and a bounded reach audit.
The ledger retains hints on findings and the latest audit on each reviewer. STATUS and
`follow-ups.json` derive review lessons from this data without influencing acceptance.
