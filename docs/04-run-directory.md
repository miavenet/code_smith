# 04 — The run directory

One directory per run. It is laid out like a build directory: everything produced along
the way is kept, in a fixed hierarchy, and nothing in it is needed to understand the repository
itself. Deliverables live in the repository; this is the record of how they came to be.

## Where it lives

By default the record is `.runs/` at the top of the repository. Three settings put it elsewhere,
in this order of precedence: `runner --runs-dir DIR COMMAND` (the option comes before the command;
after it, the runner refuses with a hint), the environment variable `CODE_SMITH_RUNS_DIR`, and
`runs_dir` under the workflow's `[defaults]`. A relative path is **relative to the top of the
repository** in all three, whatever the current directory or the workflow file's directory. The
directory ignores itself wherever it is, so it may sit inside the repository under a visible name
such as `runs/`. A location taken from the workflow is remembered by `start` in the repository's
own git config, `codesmith.runsdir` (a `start` from the default or from the option or variable
clears it), and every command on a RUN (`status`, `resume`, `approve`, `runs`, `prune`, …) looks
there when neither the option nor the variable is given. The option and the variable are not
remembered: every later command on those runs must be given the same directory. The runner does
not pass `CODE_SMITH_RUNS_DIR` on to
the agents, gates and checks it starts, so a command that runs another runner never writes into this
record. A run directory carries its workflow's name and the
first eight characters of its UUID, the same pair as its branch `run/<workflow>-<uuid8>`.

## Layout

```
.runs/
  .gitignore                          "*": the record ignores itself
  README.md                           explains this layout, once, for any person or agent
  lock                                only while a runner works in this repository: the run id and the
                                      process, and an flock held for as long as that runner lives.
                                      The lock that counts is its twin in the git directory,
                                      .git/code-smith/run-<digest of the runs directory>.lock,
                                      which `git clean -fdx` and `rm -rf .runs` cannot remove; this
                                      one is kept for older runners and readers, and a runner is
                                      refused if either is held (W-02).
                                      A command that changes a run or starts work also flocks
                                      code-smith.lock in the work tree's git directory (.git/, or
                                      .git/worktrees/NAME/), which names the runs directory too: one
                                      runner per checkout whatever --runs-dir says (W-01)
  qualification-cache.json            what `doctor` established, fingerprinted (see the last section)
  qualification.json                  the latest `doctor` report
  doctor/  check-gates/               operational records of those two commands
  <workflow>/
    latest                            text file: the newest run's directory name
    book-module-20260919T201500Z-1a2b3c4d/   <workflow>-<UTC start>-<first 8 of the run UUID>
      run.json                        identity and totals. Written last when the run is created
      pause.requested                 only between `runner pause` and the stop it asks for
      STATUS.md                       the run in words. Regenerated on every state change; while a runner works,
                                      a heartbeat also refreshes it every `status_refresh_s` (default 15 s),
                                      with each call's age, attempt and step and the tokens the provider's
                                      record shows so far. Its first line says when the run last
                                      changed and the status; see "STATUS.md, by example" below
      STATUS.html                     the same page as static HTML (inline CSS, no script, nothing fetched),
                                      written with STATUS.md from the same model, plus relative links to the
                                      record: the workflow file, state.json, events.jsonl, integrity.json,
                                      the lock (only while a runner holds it), each task directory and every
                                      attempt's, round's and invocation's files. It reloads itself every 15 s
                                      while the run is `running`. `runner status --open` opens it
      index.json                      what every file and directory here is
      state.json                      the engine's state. The single source of truth. A task with a diff budget
                                      keeps its latest measurement as `diff_budget`: `files`, `lines`, the
                                      limits and `result`, from which its STATUS.md line is written.
                                      A producer keeps its transaction's `lease` (starting commit, branch,
                                      .git/info files, pinned refs) and any `git_repairs` (W-01). Every
                                      save checks the state's invariants (02, "The record").
                                      Optional `pending_repair` keeps the short prompt, pinned candidate,
                                      session and read-only mode; `repair_qualifications` names qualified
                                      read-only profiles. `pending_author_result` keeps a settled call's
                                      answer until validation advances the attempt. Older states omit these.
                                      `schema_version` is the state's shape (3; an older state, without
                                      it, is migrated on load, 05 "The schema of state.json").
                                      `save_seq` counts the saves (absent in an older state: 0);
                                      `pin_stale` ({since, error}) is present while the pinned
                                      copy could not be brought up to date (K1). The
                                      `pending_*` keys live only inside an attempt (`pending_set_aside`
                                      inside step `set-aside`): `pending_calls`, `pending_call` (the call
                                      in flight), `pending_protocol_tries` (charged when a call returns
                                      invalid or failed at the provider) and `pending_interruptions`
                                      (calls that never returned; five stop the attempt). A panel job
                                      keeps the same for a reviewer: `tries` (charged when a call
                                      returns), `in_flight` and `interruptions`. Steps and
                                      statuses move as 05 "The producer's state machine" says.
      events.jsonl                    append-only log, one event per line; `git-repaired`,
                                      `invariant-violation`, `call-interrupted` and `state-migrated`
                                      among them
      workflow.toml                   frozen copy of the workflow as started
      workflow.expanded.json          every task after types, personas, panels and defaults are applied
      library/                        frozen copies of the type and persona files this run uses
      briefs/                         frozen content of every prompt_file, and of every rules_file as <id>.rules.md
      qualification.json              what doctor established for each agent profile, per capability
      integrity.json                  hashes of the decision-bearing files and the frozen definitions, checked around every job
      git-index                       the run's scratch index, reused so snapshots use git's stat cache
      replans/
        001/                          one per replan, numbered
          before/                     the definitions the run had: workflow.toml, workflow.expanded.json,
                                      library/, briefs/
          after/                      the revised ones, the same four
          plan.json                   changes, affected tasks, commits to revert, new and removed tasks
          result.json                 the reverts made and the branch tip afterwards, once applied.
                                      A replan refused after `after/` was written leaves it alone
      tasks/
        010-design/                   <order>-<task id>; order leaves gaps for replanned tasks
          task.json                   the resolved task definition
          STATUS.md                   this task in words. With a diff budget, a line such as `Diff budget of the
                                      latest candidate: 1 file and 12 lines changed from its base, against
                                      limits of 5 files and 150 lines: within budget.` (or `: over budget.`)
          index.json
          findings.json               the findings ledger for this producer (all reviewers, all rounds)
          attempt-1/                  numbered once per task and never reused, even after `retry`
            prompt.md                 exactly what the agent was sent
            invocation-1/             one per agent call. Created exclusively, so nothing stale is ever read
              argv.json               exactly how it was invoked. Never holds credentials
              prompt.md               the prompt of this call; a producer response repair contains only the
                                      rejected answer, diagnostic, required IDs, schema and repair instructions
              repair.json             response repairs only: before/after tree, read_only, edited_files,
                                      ordinary_authoring; before is recorded before the call starts
              stdout.log  stderr.log  streamed as they arrive, so a crash loses nothing
              last-message.txt
              schema.json
              outcome.json            ok | protocol-error | agent-error | timed-out | interrupted | environment | quota | transient;
                                      usage_source terminal | provider-record | unknown;
                                      completed says the provider ended successfully, even if answer validation failed;
                                      the status that was used, including a rejection by the ledger check.
                                      `usage` may add `cached_tokens_in` and `reasoning_tokens_out` (Codex).
                                      A Codex call whose rollout has `token_count` rows adds `limits`:
                                      kind, plan_type, limit_id, window_minutes, used_percent_before,
                                      used_percent_after, delta_percent, resets_at, credits, and `secondary`
                                      (the same for a second window). A profile with `price_per_mtok` adds
                                      `estimated_usd` and `estimated_counts`
              activity.json  hooks/   hook routing and the native hook logs (hooks.jsonl, hooks.log,
                                      milestones.jsonl); see headless-observability.md
              evidence.json  review-mode.json    only for an explicit text-only review call
            invocation-2/             only after a protocol retry
            result.json               the validated answer, plus cost, usage, seconds, session id
            inputs.json               hashes of the upstream outputs this attempt was given
            outputs.json              manifest: each declared output with hash, mode and size; the candidate tree id;
                                      `ignored_since_base` (`paths`, at most 50, and `count`) when files git ignores
                                      appeared since the task's base (W-03)
            changes.diff              readable diff of this attempt, capped. For reading, never for recovery
            reverted.json             only if the runner put back files outside `writes`, or protected or frozen ones
            embedded.json             only if the runner removed embedded git repositories from this attempt's work
            gate.log                  only if gates ran
            replay.log                only if the acceptance replay ran (W-03)
            verification.json         each gate, check and verdict with the candidate tree id and config hash it judged.
                                      An expected-failure gate's entry adds `expect: "fail"` and its `fail_pattern`;
                                      its `result` is the judgement (pass = it failed as stated), its `runs` the raw
                                      exit, and a failed one a `note` (`passed (expected a failure)`, ...).
                                      A task with a diff budget adds `diff_budget`: `files`, `lines`, the limits
                                      (`max_changed_files`, `max_changed_lines`), the five `largest` files and
                                      `result`; over budget, `results` is empty: nothing else ran.
                                      The acceptance replay adds `replay` (W-03): `where`, `dir` (removed
                                      afterwards), the candidate `tree`, the `head` it stood on, the `cache` paths
                                      copied in, one entry per gate in `results`, and `result`; a failed one adds
                                      `ignored_in_work_tree`, the ignored paths the checkout lacked (at most 20).
                                      Beside a panel (N3) it is `{"beside_panel": true, "result": "pending"}`
                                      until the replay ends, then the same record with `beside_panel`; a
                                      failed one also sets the file's `result` to `fail`. When this
                                      candidate's replay already passed on the same starting commit
                                      (a ruled `retry --apply-patch`) it is not run again and the record
                                      is `{"beside_panel": true, "result": "pass", "reused_from":
                                      "<attempt dir>"}`, naming the attempt holding the evidence (ACC-53)
          attempt-2/                  a rework. Also holds:
            feedback.md               the consolidated findings the author was sent
            responses.json            the author's answer to each finding
          failed.patch                only if the task was set aside: a complete binary-capable patch.
                                      Its candidate tree is also pinned under refs/code-smith/<run>/
          set-aside.json              beside failed.patch: the record `retry --apply-patch` reads back —
                                      attempt, base, candidate, paths — and that survives a replan
          commit.json                 only when accepted: sha, files, message
        011-design.review.principled-priya/
          task.json  STATUS.md  index.json
          round-1/                    counted per reviewer: its first sight of a candidate
            prompt.md
            invocation-1/             as for a producer; invocation-2/ after a protocol retry
            verdict.json              the validated answer, the candidate it judged, the diff base it was shown,
                                      plus `repair` when the runner dropped meaningless `resolutions` entries,
                                      and `diff_from` (task, commit) when the base is `review_diff_from`'s commit
          round-2/
        012-design.review.clause-by-clause-chen/
        020-implement/
        030-signoff/
          task.json  STATUS.md
          decision.json               who approved or rejected, when, and the comment
```

## Rules of the record

1. **`state.json` is the only thing the engine reads back.** Everything else is written for readers.
   Deleting every `STATUS.md`, `STATUS.html`, `index.json` and `follow-ups.json` loses nothing; `runner status --rebuild`
   regenerates them. Regenerated, they are the same bytes again except where the clock appears:
   the first line's age, a running run's elapsed time and the in-flight ages.
2. **Write once.** An attempt, round or invocation directory is never modified after it is finished.
   A retry, a rework or a protocol retry makes a new directory, with a number that is never reused.
   So the record of what happened cannot be rewritten by what happened next. This is enforced by
   hashes (rule 4), **not by file modes**: read-only directories would stop `status --rebuild`
   from regenerating the `index.json` inside them, and would make `rm -rf` of an old run fail.
   `STATUS.md`, `STATUS.html`, `index.json` and `follow-ups.json` are derived files and are outside the write-once rule.
   Engine saves render the run pages and `follow-ups.json`, and only task directories whose state or ledger changed.
   Per-task fingerprints live in memory, never in `state.json`; a failed render is retried.
   `status --rebuild` and resume reconciliation rebuild all directories. Engine and recovery
   rendering failures leave a `render-failed` event per failing page and work continues; each task
   page and index has its own guard, so one broken page does not stop the others. `status` reports
   the error as a runner error naming the page (exit 2).
   The engine publishes changed `findings.json` through `write_decision` after saving state,
   independently of rendering; reconciliation completes publication interrupted by a crash.
3. **Durable state, and intents before effects.** `state.json` is written to a temporary file,
   flushed and synced, renamed, and its directory synced. `run.json`, `STATUS.md`, `STATUS.html` and
   `index.json` are replaced whole the same way, and a torn last line of `events.jsonl` is ended before the next
   event is appended. Before any external effect (an agent call,
   a commit, a restore, a revert) the state records the **intent** with a unique operation id; after
   it, the **outcome**. `resume` reconciles every intent that has no outcome. Agent and command
   intents now store `process.group` (the pgid and supervisor pid/start identity), alongside the
   existing process fields. The supervisor waits on a pipe until the identity save has completed
   this fsync discipline; EOF before release exits without running task code. After spawning the
   CLI, the intent is amended with `process.group.leader` (its pid/start identity) and `members`
   (known member identities) before task code is released, so recovery can stop writers even after
   supervisor death. The group record
   distinguishes supervised invocations from old leader-only records. See
   [05, Crash recovery](05-architecture.md#crash-recovery). The intent is gone once the
   operation ends, so its `outcome` event in `events.jsonl` keeps what it knew: `task`,
   `invocation`, `process` (the group id and the supervisor and leader identities as recorded,
   without the member list; historical, never read to find a live process), `token_reservation`,
   `token_remainder`, and `ended_at`, when the operation ended (a parallel call's own end, not the
   panel's collection). Intents are stamped to the microsecond, as events are (REC-50).
4. **The record protects itself.** `.runs/` is ignored by git, so work-tree snapshots cannot
   see an agent tampering with it. The runner therefore keeps `integrity.json`: the hashes of every
   **decision-bearing file**, which are the run's `qualification.json`, every `findings.json`, and every `result.json`,
   `verdict.json`, `verification.json`, `outputs.json`, `inputs.json` and `decision.json` in a finished directory
   (and the files under a replan's `before/` and `after/`), and the **frozen definitions** the
   engine executes from: `workflow.toml`, `workflow.expanded.json` and every file under `library/`
   and `briefs/`. Those are registered when the run is created and again, with their new hashes,
   when a replan installs new ones, so a producer that edits its reviewer's copied persona
   or the expanded workflow inside `.runs/` fails its job, and `resume` refuses the changed record.
   `inputs.json` is protected from creation. Missing or malformed accepted input evidence is an
   error when regression verification or replan reads it.
   A run recorded before this protection gets its definitions registered, as they are then, at
   its next `resume`. `state.json` is not in the manifest: it
   is rewritten constantly, so each job is guarded by a before-and-after hash of it instead. A
   decision file that is rewritten goes through a `decision` intent recorded in `events.jsonl`, so a
   crash between the file and its manifest entry is repaired on `resume`; rewriting identical bytes
   is skipped.
   It checks them before and after every agent call and every command, and treats a change it did
   not make as a failure of that job. The ledger decides every verdict, so it is covered from the
   first finding, not only once something is closed.
   **The record is also pinned in git (W-02, K).** `state.json`, `run.json`, `integrity.json`,
   `events.jsonl` and every file the manifest covers (among them each counted attempt's
   `result.json` and each prepared review round's `prompt.md` and `diff.patch`, protected before
   the save that relies on them) are kept as one git tree under `refs/code-smith/<run>/_record`,
   moved before each save of the state or the manifest. Git writes the objects (`hash-object -w`,
   `mktree --batch`), so the repository's object format, `core.sharedRepository` and fsync policy
   apply (`core.fsync=committed` unless the owner set one); the ref moves by compare-and-swap
   (`update-ref ref new old`). A save that changes only `state.json` runs three git processes.
   Only the runner's own bytes are pinned: the state it saves, the manifest it keeps in memory (a
   manifest an author changed on disk is replaced, with an `integrity-manifest-replaced` event,
   never adopted), a covered file only while its hash matches that manifest, `run.json` with the
   totals of the state being pinned and only while its identity is unchanged, `events.jsonl` while
   it grows with its pinned prefix unchanged. An edit of that prefix keeps the earlier
   `events.jsonl` pinned while the rest of the save is still pinned, is logged once as
   `record-events-changed`, and is refused as `events.jsonl was changed` until a repair puts the
   prefix back in front of the later events (REC-57, REC-58). Every save counts in the state's
   `save_seq`, so a repair can tell the later of the pin and the disk.
   A pin that fails (a ref lock, a permission, a corrupt object) never stops a save: it is logged
   once per distinct error as `record-pin-failed`, kept in the state as `pin_stale` (shown in
   STATUS.md under "Needs attention") until a pin succeeds, and retried at every save. A ref that
   is not the tree the runner last made it (or holds another run's state, or a state later than
   the disk's by more than the one save a kill can leave) is **moved**: the run stops `failed`
   with `record-pin-moved` and a reason naming the ref and both values, and the ref is left as it
   is. A deleted ref is pinned again (`record-pin-restored`). The pin defends against accidents
   (`git clean`, `rm`, an edit) and makes a forged move visible; it does not defend against a
   hostile writer with the runner's uid that also kills the runner.
   The integrity check compares each covered file with the trusted manifest (this process's,
   else the pinned copy), never with the disk's, so a paired edit of a file and its hash still
   names the file (REC-55). A disk manifest that differs is not itself refused, since a kill in
   `protect` between the pin and the manifest write leaves exactly that: `resume` writes the
   trusted one back with `integrity-manifest-replaced` (REC-59).
   Recovery permits pending decision publications and replan installations matching their
   protected staging copies, so a kill between a file write and its manifest entry is resumable.
   `state.cleanup_obligations` retains each invocation's `cleanup = {status, error, group}` until
   resume confirms the group empty (`closed`; a live group is stopped only with `--stop-orphans`)
   or a person rules it abandoned (`resume --abandon-cleanup`: `abandoned`, with the entry's
   `ruling` holding `by`, `by_source`, `interactive`, `agent_markers` and `at`; its
   `cleanup-abandoned` event carries that time as `ruled_at`, the event's own `at` being the
   record's stamp, REC-60). STATUS names open
   obligations in the stop reason; a command refused for one names that remedy, not
   `repair-record`, since the record is intact (PROC-23). A serial replay keeps its checkout and
   its `replay` intent while a cleanup is open, for `resume` to remove after it (PROC-22); STATUS
   says so instead of counting it interrupted. A reader cancelled before release whose own group
   stop failed, the group still alive, is finished with its cancelled outcome and that `cleanup`,
   so it is an open obligation like any other, with the same remedies; a replay keeps its intent
   and checkout as above (PROC-25, PROC-29). An intent the first version-3 runner kept with
   `cancelled` (the outcome it was settled to) is read as that obligation on `resume`. A
   cancelled operation's outcome event has `cancelled: true`, `ended_at` (the cancel time) and no
   `wall_s`; a check or replay that ran commands first has `result: "cancelled"` and their
   `completed` runs and seconds (PROC-26, PROC-29).
   Saved panel jobs retain `guard_problems`, found against the whole protected set before each
   answer is saved (REC-56); rejection diagnostics are rebuilt from `raw_outcome`.
   An integrity failure, a `state.json` that differs from its pinned copy of the same save, or
   an edited pinned history of `events.jsonl`, therefore names its way back, and every command that writes into the record (`approve`,
   `reject`, `retry`, `resolve`, `replan`, `resume`) refuses until then: `runner repair-record RUN`
   (`--dry-run` only reports) puts back every pinned file that is missing or changed, writes a
   `record-repaired` event with each path, `missing` or `changed` and both SHA-256 hashes,
   rebuilds a missing `task.json` from the frozen `workflow.expanded.json`, says which attempt
   and round directories were lost with their logs (not restorable; their outcome is in the state
   and the intents), and checks the manifest again. A `state.json` on disk of the same run and a
   later `save_seq` than the pin (a pin failed after it) is **kept**, with its `integrity.json`,
   and only the files that agree with that manifest are put back; `--accept-older` puts the
   pinned ones back instead, as the last resort. `events.jsonl` is put back as the pinned history
   followed by whatever was logged since. A malformed `state.json` is simply restored; the lock
   takes the run id from the pin. A pinned object git cannot read is an error naming the file. A
   run recorded before this has no pinned record: its files are put back by hand, as before.
   If the record is removed while a runner works (`git clean -fdx` by an author), the runner
   notices at its next write into it, saves its state into a recreated run directory and stops
   `stopped` (not `failed`: repair and resume continue it) with a `record-lost` event and a stop
   reason naming `repair-record`; no page is rendered into the removed record. A `resume` or
   other command in the meantime names the live runner that holds the work tree's lock.
5. **No secrets by intent.** The runner passes no credentials. Agent output is stored as the agent
   printed it, after token/key/bearer-header redaction in `proc.py`. Lines over 64 KiB are replaced
   with `[overlong line omitted for safe redaction]`: a token split at a read boundary must not leak,
   and log buffering must stay bounded. The runner still parses Claude's final result object and
   Codex's events when they are longer, up to 16 MiB, after redacting them; the log keeps only the
   placeholder. Command answers need a final JSON object on
   lines within this limit and within the 256 KiB retained output tail.
6. **Self-ignoring.** `.runs/.gitignore` holds `*`. Committing a run record is the owner's choice.
   Every save writes it again if it is missing (a `record-ignore-restored` event), and a record
   path that still reaches a snapshot is read as "the ignore file was removed", put right the same
   way, and never restored as the author's change (W-02).
7. **Linked to the hook log.** The run UUID and task id are exported to every agent call as
   `CODE_SMITH_RUN` and `CODE_SMITH_TASK`, so the repository's hook logger ties each driven
   session to its task. `CODE_SMITH_RUN_DIR` is exported **only to tasks whose type sets
   `needs_run_dir = true`**, which in the starter library is `summarize` alone. Gates and checks
   never receive it.
8. **A rejected review answer is summarised, not lost.** Whatever a panel rejects — at
   collection or at final application — is redacted at capture and appended into `state.json`, then
   pointed at from the producer's `STATUS.md`. The raw text stays where it always did, in that
   invocation's `last-message.txt`.

## `run.json`

```json
{
  "run_id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
  "workflow": "book-module",
  "workflow_sha256": "…",
  "root": "/path/to/repo",
  "workflow_file": "/path/to/repo/workflows/book-module.toml",   // resolved paths are recorded
  "branch": "run/book-module-1a2b3c4d",      // with branch = "current": the branch that was checked out; none is created
  "original_branch": "main",                 // what was checked out before `start`
  "base_commit": "61d0921",
  "started": "2026-09-19T20:15:00Z",
  "runner_version": "0.1.0",
  "runner_source": {"version": "0.1.0", "src_sha256": "…", "commit": "…", "dirty": false},  // commit and dirty only in a Git checkout (RUN-49)
  "agents": {                                // keyed by the profile's fingerprint (sha256), as in the qualification cache
    "49290d55…": {"profile": {"kind": "command", "argv": ["python3", "command_agent.py"]}, "version": "…",
                  "model": "", "capabilities": ["answer", "read", "execute", "write", "resume", "boundary"],
                  "host": "…", "binaries": {"…": "…"}, "config_hashes": {}, "adapter_sha256": "…",
                  "observer_sha256": "…", "cache_version": 2, "read_only": false, "root": "/path/to/repo"}
  },
  "status": "running",
  "spend": {
    "known_usd": 3.12,                       // reported by agents that report cost
    "reserved_usd": 5.00,                    // caps of calls in flight
    "unpriced": {"calls": 2, "tokens_in": 48211, "tokens_out": 1930, "unknown_calls": 0},
    "unsettled": {"usd": 5.00, "calls": 1, "tokens_in": 0, "tokens_out": 0},  // absent in older runs
    "estimated_usd": 0.42,                   // only with a profile's price_per_mtok: never known spend
    "estimated_counted_usd": 0.0,            // the part of it whose profile set estimated_counts = true
    "windows": {                             // only when a provider reported rate-limit windows (Codex)
      "codex:codex": {"window_minutes": 10080, "used_percent_first": 2.0, "used_percent_last": 3.0,
                      "delta_percent": 3.0, "plan_type": "plus",
                      "observations": [{"at": "2026-10-07T12:20:01.123456Z", "before": 2.0,
                                        "after": 3.0, "resets_at": "1791936000"}],   // the latest 50
                      "ranges": {"1791936000": {"low": 2.0, "high": 3.0,
                                                "first_at": "2026-10-07T12:20:01.123456Z"}}}
    }
  },
  "seconds": 1840
}
```

`status` is one of `running`, `done`, `failed`, `needs_human`, `stopped`. `stopped` is a budget
stop, a token stop or a `pause`; an environment, network or provider stop gives `failed`.

`spend.windows` is keyed `<kind>:<limit_id>` (and `<kind>:<limit_id>:secondary` for a second
window). The window belongs to the account, not the run, so the record keeps what was observed
(BUD-21): `observations` (when each call settled, the percentage before and after, and the reset
identity `resets_at`) and `ranges`, per reset, the lowest and highest percentage seen, which does
not depend on the order the calls ended in. STATUS shows the ranges ("Observed Codex weekly account
window: 2.0% → 3.0% (shared-account observations, not attributable run usage)"). `used_percent_first`
and `used_percent_last` are the first and latest readings; `delta_percent` is the sum of the calls'
own deltas, kept for older readers and not shown, since calls that ran side by side each count the
others' share (05, Honest accounting). `estimated_usd` and `windows` are absent in runs recorded
before them and in runs that never had one; a window without `ranges` shows its first and last
readings. `spend.unpriced.cached_tokens_in` is the part of `tokens_in` the provider read from its
cache, shown as a share ("1040000 tokens in, 66% cached"); absent until a call reports one.

`spend.unsettled` holds the caps kept for calls of an agent that reports cost and ended without one
(a timeout, an error, an interruption): the call may have spent up to its cap, so the dollars count
against the run's dollar limit, and those calls' tokens are recorded here, not under `unpriced`. A
priced call refused for quota or ended by the environment counts as nothing spent only with positive
no-work evidence and no usage; with usage in the provider's record it is kept here like any other.

`spend.unpriced.counted_calls` and `counted_tokens` appear only under `run_budget_tokens`, once a
call of an agent that reports no cost ended with unknown usage: such a call is held at its
bounded reservation (its intent's `token_reservation`), or, when it ended without its final event,
at what was left under the line when it started (its intent's `token_remainder`; an intent recorded
before has none), so the stop line is reached rather than the call left out (BUD-12, BUD-17,
BUD-20, 05 Honest accounting). `held` names the last ten such calls (task,
invocation, seconds, status, error, tokens) for the stop reason and STATUS (BUD-16), and
`largest_call` is the largest measured unpriced call, which sizes the next reservation.

`state.json` also keeps, where they apply: `unpriced_call_reserve` (from `[defaults]`; a run
recorded before BUD-17 had none, and is migrated to `-1`, which keeps its old reservations); `gate_seconds` and
`replay_seconds` (measured time of gates and checks, and of acceptance replays); `stopped_at` and
`stopped_seconds` (when the run last stopped, and the time it stood stopped before each `resume`);
`clock_gap` (operations whose wall-clock span ran a minute or more past their measured time, by
how much in all, and in `where` the last three of them: op, kind, task, seconds) (RUN-50);
`agent_spans` (the agent calls' [intent, end] wall intervals: `summed_s` their sum, `folded_s` the
length of the union of those no call in flight can still overlap, `open` the rest), from which the
Time line says how much of the summed agent time overlapped (REC-51). Each `outcome` event carries
`wall_s`, and an agent call, a command or a panel check its measured `seconds`. `start` and every `resume` write a `runner-source`
event with the runner's source identity (RUN-49).

The identity fields are written once at `start`. `status`, `spend`, `seconds`, `run_budget_usd` and `run_budget_tokens` are **copied in
from `state.json`** whenever the derived files are regenerated, so rule 1 holds: the engine never
reads them back. `run.json` also records `name`, `git_toplevel`, `library` and `branch_mode`.
Task order numbers step by 10 for tasks written in the workflow; generated panel members take the
producer's number plus 1, 2, … . `state.json` is guarded by a before-and-after hash around each job
rather than by `integrity.json`, because the runner itself rewrites it constantly.

## Task status values

| Status | Meaning |
|---|---|
| `pending` | Waiting for its `needs` |
| `ready` | Could run now |
| `running` | An agent or a command is working |
| `verifying` | A producer's attempt is with its checks or reviewers |
| `rework` | A producer is about to run another attempt |
| `accepted` | Done: verified, and for a producer, committed and frozen. For a review, check or human task: passed |
| `objected` | A verifier whose latest round did not pass. It runs again if its producer is reworked; it is final if the producer ends `blocked` or `failed` |
| `waiting_human` | Stopped for a person: an approval, or an escalated finding. The tree is held |
| `blocked` | A person is needed: the agent said it cannot do this properly, attempts ran out with findings open, a panel cannot produce a valid answer, or a standalone `human` task was rejected |
| `failed` | Attempts used up on gates, no progress, a reviewer changed the tree, or a standalone check did not pass |
| `skipped` | It could not run: something it `needs` failed or is blocked, or the producer it `reviews` or `verifies` ended before it ran. A dependent of an `objected` check whose producer is dead is skipped too, with a reason such as `'words' is objected and 'make' is failed` |

## Result schemas

Every agent answer ends with one JSON object. All schemas are strict (`additionalProperties: false`,
every property required), which Codex needs and the others accept.

**Produce**

```json
{
  "outcome": "done",                        // or "blocked"
  "summary": "What was made, in a few sentences. Shown to downstream tasks.",
  "blocked_reason": "",
  "responses": [                            // on rework: exactly the findings feedback.md lists as needing a response
    {"finding": "implement/PE-2", "action": "fixed", "note": "…"}   // or "disputed"
  ],
  "advisories_addressed": [                 // optional, author reports only; never a verdict input
    {"finding": "implement/PE-3", "note": "Changed the diagnostic"}
  ]
}
```

**Review**

```json
{
  "verdict": "block",                       // or "pass". "block" requires at least one blocking finding
  "summary": "Overall assessment.",
  "findings": [                             // new findings this round. caused_by: later rounds only
    {"severity": "blocking", "title": "…", "detail": "…", "location": "src/book/side.hpp:41", "caused_by": ""}
  ],
  "resolutions": [                          // later rounds: one per open BLOCKING finding of this reviewer; else empty
    {"finding": "implement/PE-2", "status": "resolved", "note": "…"}   // or "unresolved"
  ]
}
```

The runner, not the agent, assigns finding ids and enforces that an advisory reviewer cannot block.
**Whether a reviewer blocks is computed from the ledger** once its answer is applied: an old finding
left `unresolved` blocks even when `findings` is empty, and a `verdict` that disagrees with the
computed result makes the answer invalid. In a later round, a new blocking finding stands if its
`location` is inside the rework diff, or if its `caused_by` names a location that is; the runner
checks the named location against the diff. Otherwise it is recorded as advisory. A location may be
written relative to `root` or to the repository; a point where lines were deleted counts as the line
before it and the line after it; a bare path with no line counts only for a file the rework added,
deleted or changed as a binary.

Every answer is validated by the runner itself before it is used: shape first, then meaning.
An invalid answer is a protocol error and is retried; it never becomes a finding or a rework. A
reviewer whose three calls were all interrupted and never gave an answer to judge ends `interrupted`,
not as a protocol error.

## `findings.json` (the ledger)

```json
{
  "producer": "implement",
  "reviewers": {                             // the base of each reviewer's next rework diff
    "implement.review.principled-priya": {"round": 2, "last_seen_candidate": "f8d1c79…"}
  },
  "findings": [
    {
      "id": "implement/PE-2", "reviewer": "implement.review.principled-priya", "persona": "principled-priya",
      "severity": "blocking", "title": "…", "detail": "…", "location": "src/book/side.hpp:41",
      "status": "resolved",
      "history": [
        {"round": 1, "attempt": 1, "event": "raised"},
        {"attempt": 2, "event": "author:fixed", "note": "…"},      // author events carry the attempt only
        {"round": 2, "attempt": 2, "event": "reviewer:resolved", "note": "…"}
      ]
    }
  ]
}
```

## `STATUS.md` (run level), by example

```markdown
Updated 2026-09-19 20:46:12 UTC (3s ago at render). Status: needs a person (exit 255).

# book-module — run 1a2b3c4d — needs a person

Started 2026-09-19 20:15 UTC. 31 min of agent time. Branch run/book-module-1a2b3c4d.
Spend: $3.12 known of $50.00, $0.00 reserved, plus 2 unpriced calls (48211 tokens in, 66% cached, 1930 out; 0 with unknown usage), ≈$0.42 estimated from profile rates. Observed Codex weekly account window: 2.0% → 3.0% (shared-account observations, not attributable run usage). By model: claude / claude-opus-5 $2.49 (2 tasks); codex / gpt-6-astra unpriced (2 tasks); claude / claude-sonnet-5 $0.69 (2 tasks).
(With unsettled spend the line also reads `, $5.00 unsettled (1 call ended without a price)` after the reserved figure.
The estimate appears only with a profile's `price_per_mtok`, and a window only when a provider reported one. "By model" breaks the known dollars down by `profile / model` from the tasks' own records; a task of an unpriced kind shows as unpriced. The table's "Agent / model" column is the planned agent until routing records what ran.)

| # | Task | Type | Agent / model | Status | Attempts | Cost | Commit |
|---|---|---|---|---|---|---|---|
| 010 | design | design | claude / claude-opus-5 | accepted | 2 | $0.84 | 3b098a6 |
| 011 | design.review.principled-priya | design-review | codex / gpt-6-astra | accepted | 2 rounds | | |
| 012 | design.review.clause-by-clause-chen | design-review | claude / claude-sonnet-5 | accepted | 1 round | $0.22 | |
| 020 | implement | implement | claude / claude-opus-5 | waiting_human | 2 | $1.65 | |
| 021 | implement.review.principled-priya | code-review | codex / gpt-6-astra | accepted | 2 rounds | | |
| 022 | implement.review.clause-by-clause-chen | code-review | claude / claude-sonnet-5 | objected | 2 rounds | $0.47 | |
| 030 | signoff | human | | pending | | | |

## Progress
- Tasks: 4 of 7 accepted (1 objected, 1 pending, 1 waiting_human).
- Attempts used: 4.
- Budget used: $3.12 known of $50.00; 50141 unpriced tokens; ≈$0.42 estimated.
- Elapsed: 31 min 12 s since start, to the last event.
- Time: 31 min 00 s in agent calls (summed; 4 min 01 s of it overlapped); 2 min 05 s in gates and checks; 1 min 53 s in acceptance replays.

## Providers
- **design**: claude / provider default (standard; configured preference).
- **implement**: claude / provider default (standard; configured preference).

## Needs attention
- **implement** is waiting for a person: finding implement/SC-1 is disputed by the author and kept
  open by clause-by-clause-chen. See tasks/020-implement/findings.json. The candidate is still in the work
  tree, which the run is holding; do not edit it.

## Recent events
- 20:46:10 outcome (took 2 min 31 s) kind=agent op=op-0042-1c2d3e4f status=ok
- 20:46:12 run-stopped status=needs_human
(the last ten events of events.jsonl, one per line; an outcome's task, invocation, process,
reservation and end time stay in the file. An outcome shows how long it took; when the
wall clock ran a minute or more past the measured time it shows both, and Progress adds "The wall
clock ran … ahead of the monotonic clock during op-… (implement): the machine or its VM was
suspended, or the clock was stepped; the record does not say which". "Time" appears once the run
has measured a gate or stopped for a person, with "… stopped, waiting for a person" after a stop;
the agent figure is a sum over calls that may run side by side, and says how much overlapped.)

## Next
    runner resolve RUN implement/SC-1 --as resolved|advisory|upheld -m "why"
    runner resume RUN
```

The first line says when the run last changed (the time of the newest event in `events.jsonl`)
and how long before the page was rendered that was, then the status with the exit code it gives
and, for `failed` and `stopped`, the reason. A `running` run that no runner is working on says so
(`running, but no runner is working on it`). While a call is in flight an "In flight" section sits
after "Providers": one line per agent call or command with its attempt and step, its start, its
age and the provider's tokens so far, the refresh interval, and `Then, in workflow order: …`, the
pending tasks that come next.

A finished run's "Next" section says whether its last accepted commit is in the branch the run came
from; when it is not, it says `Not merged: <branch> does not contain the last accepted commit <sha>`
and that merging is the owner's call.

## `index.json`, by example

```json
{
  "path": "tasks/020-implement/attempt-2",
  "about": "Second attempt of task 'implement': a rework after review round 1",
  "files": {
    "prompt.md": "The prompt sent to the agent",
    "feedback.md": "The consolidated findings the author was asked to address",
    "result.json": "The agent's structured answer, with cost and session id",
    "responses.json": "The author's answer to each finding",
    "changes.diff": "Everything this attempt changed in the repository",
    "outputs.json": "Manifest of the declared output files after this attempt",
    "gate.log": "Output of the gate commands",
    "invocation-1/": "Raw invocation and output of the agent process"
  }
}
```

## Qualification and preflight records

At repository level, `.runs/qualification-cache.json` holds fingerprinted entries and
`.runs/qualification.json` holds the latest report. `.runs/doctor/<uuid>/<fingerprint>/invocation-N/`
keeps each probe prompt, command, streamed output and outcome. `.runs/check-gates/<uuid>/` keeps
command logs and `results.json`. These are operational records, not workflow runs.

Each workflow run has `qualification.json`, with the observed capabilities and their provenance;
`run.json.agents` records version, profile, model, host and configuration hashes. Task state records
the qualification key and capabilities used. Explicit text-only review calls record `evidence.json`
and `review-mode.json` alongside their invocation logs.

A human finding ruling records `event: human`, `decision`, `by`, `at`, `note` and `candidate`
in the finding history. A fully ruled set-aside candidate restored by `retry --apply-patch`
gets a new attempt directory with `agent_calls: 0` and `recovered_candidate` in `result.json`;
inspection and verification evidence are written there. Recovery intents carry `ruled_candidate`
so crash recovery takes the same path. Older intents without it still begin with an author call.

Review targets include `gate_evidence`: the current candidate, attempt, gate-log path, a bounded
8192-byte output tail and a truncation flag. Missing logs in old records are labelled unavailable.
New `raised` finding history records the producer attempt as well as the candidate. Optional
`advisories_addressed` in the producer result adds `author_addressed` history with attempt,
candidate and note; replay does not duplicate it. Unknown or blocking ids in this optional list
are ignored. The blocking `responses` contract is unchanged. Feedback and task STATUS label
these as historical advisories and unverified author reports; missing legacy provenance is unknown.

Task and run STATUS mark **accepted with owner rulings** and link candidate manifests and the
findings ledger with owner notes. Downstream inputs add `accepted_candidate`, `rulings_record`
and `owner_rulings` when an accepted upstream task has human history. Each ruling retains its
original candidate, finding id, person and note. Rendered notes take at most 8192 bytes per input,
further limited to half the inputs cap divided among upstream tasks; truncation is explicit and
the full-record pointer stays. Identity, candidate and file metadata stay as with existing inputs.


`follow-ups.json` is a derived run-level index, refreshed with STATUS (including at run end).
It groups gateable findings and their review round numbers by task, together with owner rulings
whose note contains “the brief will say so” (case-insensitive). Rebuilding pages recreates it
from the ledgers; deleting it loses no decision. Task STATUS includes Lab follow-ups, overlapping
reviewer disagreements, standing-ruling counts with each supplied ruling and its provenance, the
reasons entries were dropped, and any latest reviewer reach-audit tables.
Standing entries supplied to a panel are retained in that panel's state (`standing_rulings`,
`standing_dropped`, `standing_dropped_why`) and frozen prompts; the source TOML remains
project-owned. Its committed entries are frozen in `workflow.expanded.json` with the blob id and
commit they were read from (`defaults._standing_rulings_blob`, `_standing_rulings_commit`;
provenance for a reader of the record: the runner itself never reads them back), and
the file is protected in every task, so an author's edit cannot become a supplied owner ruling.
`resolve --standing` queues its entry in `state.json` (`standing_pending`), saved with the
ruling; the run page lists queued entries under "Standing rulings from this run", and when the
run is done the runner appends them to the rulings file once (an entry already there, from an
export a crash cut short, is not appended again), marks them `written`, and records a
`standing-rulings-written` event. Each entry keeps the rulings file it was bound to when it
was queued (`file`), which the export uses; it is checked by the file's own reader when queued
and again before the write. A failed write leaves the run `failed` with the reason and no
remembered tree, so the owner's fix is no change `resume` refuses; `resume` writes it again. A
`failed` run's page lists each queued entry in full with `runner export-rulings RUN`, which
writes them the same way (same idempotence, same event) only when nothing in the run can touch
the tree again: every task terminal, no transaction, intent or open cleanup (RUN-81). The
remembered tree takes the written file, and `resume` and `replan` take its commit. After a
failed write at `done` the page says `resume` writes it once the cause is fixed (keyed on the
stop reason).
Every durable write uses a new temporary file beside its target, created exclusively under a
unique name (`.<name>.<random>.tmp`); a failure removes it, and one a kill leaves is never
reused. `events.jsonl` is checked and pinned in bounded pieces: an unchanged file is only
`stat`ed.
