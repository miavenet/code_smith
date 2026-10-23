# Quick start

Every model-free command below was run, and
its output is quoted (trimmed, with the scratch path shortened to `/tmp/demo`). Commands that call
a model (`doctor` and `start` on `claude`, `codex` or `copilot` agents) were checked by reading the
code, not run.

## What you get, and what this costs you

The runner takes a **workflow** (a TOML file of tasks forming a DAG), runs each task with a
headless coding agent (Claude Code, Codex, GitHub Copilot CLI, or a scripted command), accepts work
only when its gates, checks, review panel or a person say so, commits each accepted task on a run
branch `run/<workflow>-<id>`, and leaves a full record of every prompt, answer and verdict under
`.runs/`. It never pushes or merges.

Time: **5 minutes** for the model-free demo (section 2), **20 more** to render, check and start a
real workflow on your repository (section 3). Sections 4 to 7 are reference; read them when you
need them.

## 1. Prerequisites

Required, everywhere:

| Tool | Minimum | Why | Checked here with |
|---|---|---|---|
| Python | 3.11 | standard library only (`tomllib`); no install step, no packages. An older one gets `code_smith needs Python 3.11 or newer (found 3.9.6)` and exit 2 | `python3 --version` → Python 3.12.2 |
| Git | any recent | the target must be a Git repository with a clean work tree | `git --version` → 2.50.1 |

The runner is not installed: run `runner` from a checkout. Below, `TR` is the path to
the `code_smith` directory:

```sh
export TR=/path/to/code_smith
$TR/runner --version
$TR/runner --help
```

The agent CLIs are needed **only for the agent kinds your workflow uses**. A workflow names agents
in `[defaults] agent = ...` or per task; a rendered template uses `claude` unless you render it with
`--set agent=codex` (or `copilot`, or a profile you add) or edit the file.
The runner finds each CLI on `PATH` by its kind's name (`claude`, `codex`, `copilot`), or runs the
`argv` you give the profile.

| Kind | CLI | Install | Log in (no model call) | Seen here |
|---|---|---|---|---|
| `claude` | Claude Code | see Anthropic's Claude Code install page | `claude auth login`; check with `claude auth status` | 2.1.288 |
| `codex` | Codex CLI | see OpenAI's Codex CLI install page | `codex login`; check with `codex login status` | 0.160.0 |
| `copilot` | GitHub Copilot CLI | see GitHub's Copilot CLI install page | `copilot login` | 1.0.75 |
| `command` | any program you name in `argv` | yours | none | — |

The versions are what this guide was checked on, not minimums. The adapters were built against
these CLIs' flags; a much older or newer CLI may reject a flag, which the runner reports as an
environment failure, not as failed work.

**What `runner doctor WORKFLOW` checks.** For each agent profile, model and mode (writer or
read-only reviewer) the workflow uses, it runs probes in a throwaway Git repository and grants only
what it observes: `answer`, `read` (returns a secret from a file), `execute` (runs a script that
writes a hash), `write` (writers), `resume` (a second turn remembers the first) and `boundary`
(a reviewer tries to change a file and must fail). For Codex it first checks, without a model call,
that the Codex sandbox can start. Results are cached in `.runs/qualification-cache.json`, keyed on
the profile, model, CLI binary and version, its settings files and the host. `start` qualifies any
profile that is not cached, so `doctor` is optional, but it fails faster and cheaper. `doctor`
is also the run's **model inventory**: a generated workflow names a model for Claude, Codex and
Copilot, its reviewers are dealt across the families other than the author's, and the families
this box cannot run (no CLI, no login, a sandbox that cannot start) fail their probe here and are
skipped by routing, which records why; a box with all three gets a mixed panel without any edit
to the file. Pass `--force` after you fix a CLI or a login: a failed probe is cached too.

**What costs money.** `doctor` and `start` on `claude`, `codex` or `copilot` profiles make real
model calls: up to five probes per profile, model and mode, each Claude probe capped at $1, plus
every task of the run. Claude reports dollars, and the runner holds runs to them. Codex and Copilot
report no price: their calls are recorded as unpriced tokens and only `run_budget_tokens` stops
them (section 5). `validate`, `new`, `graph`, `check-gates`, `status`, `runs` and everything on a
`command` agent are free.

Optional: `mmdc` (Mermaid CLI) is used only by `tools/lint_mermaid.py` to check the diagrams in
the runner's own documents. You do not need it to run workflows.

**Platforms.** macOS and Linux. The run lock is an `flock` on `.runs/lock`, and a second one on
`code-smith.lock` in the checkout's git directory keeps a second runner out of the same checkout
even with another `--runs-dir`; a lock left by a dead runner is released by the kernel. Every agent
call runs under a small supervisor in its own process group, recorded before the agent starts.
Processes are identified beyond their pid (which is reused) by start ticks and boot id on Linux and
by start time and boot session on macOS, so `resume` can tell a live orphaned agent, or a child it
left running, from a reused pid; elsewhere it falls back to `ps` and is weaker. On Linux
the Codex sandbox needs unprivileged user namespaces (`bwrap`); `doctor` reports it if they are
off. Windows is not supported (the runner imports `fcntl`).

## 2. Five-minute run, no model

The demo's agent is `examples/command_agent.py`, a Python script that writes `result.txt`. Nothing
calls a model.

```sh
mkdir /tmp/demo && cd /tmp/demo && git init -q -b main
cp $TR/examples/command-demo.toml $TR/examples/command_agent.py .
git add . && git commit -qm demo        # check-gates and start need committed files and a clean tree
```

The workflow has three tasks: `write` (produce, with a gate), `check` (a read-only command that
verifies `write`) and `signoff` (a person).

```text
$ $TR/runner validate command-demo.toml                         # exit 0
warning: agent 'scripted' reports no dollar cost: dollar limits do not bind on it; ...
workflow command-demo: 3 tasks, in execution order
  1  write    produce/implement      agent scripted
              outputs: result.txt
              gate: grep -qx 'Verified candidate' result.txt
  2  check    check                  verifies write; read-only
  3  signoff  human                  verifies write

$ $TR/runner check-gates command-demo.toml                      # exit 0
check-gates runs each command in a clean clone of the committed files: ...
write:gate:1: fails as intended
check:check:1: fails as intended
record: /tmp/demo/.runs/check-gates/c96653d6-...

$ $TR/runner doctor command-demo.toml                           # exit 0
command (default model) writer: answer, read, execute, write
  observed activity: none expected (a command agent has no hooks)
qualification: /tmp/demo/.runs/qualification.json

$ $TR/runner start command-demo.toml                            # exit 255: a person is needed
write: started
write: waiting for a person at 'signoff'. The work tree is held.
run command-demo-20261006T124321Z-0ec2e2c1: needs_human. See /tmp/demo/.runs/command-demo/command-demo-20261006T124321Z-0ec2e2c1/STATUS.md
run command-demo-20261006T124321Z-0ec2e2c1  (0ec2e2c1-9300-4ab9-8a6f-25c633774abe)
record  /tmp/demo/.runs/command-demo/command-demo-20261006T124321Z-0ec2e2c1
branch  run/command-demo-0ec2e2c1  (checked out; was main)
```

The gates "fail as intended" because `result.txt` does not exist yet: a gate must fail before the
work and pass after it. A command agent has no hooks, so `doctor` expects no hook activity from it.

**`start` checks out the run branch and leaves the checkout there**, whether the run finishes,
stops or waits for you: from here on `git status` shows `run/command-demo-0ec2e2c1`, not `main`.

```text
$ $TR/runner status
Updated 2026-10-06 12:43:24 UTC (0s ago at render). Status: needs a person (exit 255).

# command-demo — run 0ec2e2c1 — needs a person

Started 2026-10-06 12:43:21 UTC. 0 min of agent time. Branch run/command-demo-0ec2e2c1.
Spend: $0.00 known of $50.00, $0.00 reserved, plus 1 unpriced calls (0 tokens in, 0 out; 1 with unknown usage). By model: scripted unpriced (1 task).

| # | Task | Type | Agent / model | Status | Attempts | Cost | Commit |
|---|---|---|---|---|---|---|---|
| 010 | write | implement | scripted | waiting_human | 1 |  |  |
| 020 | check | check |  | accepted |  |  |  |
| 030 | signoff | human |  | waiting_human |  |  |  |

## Progress
- Tasks: 1 of 3 accepted (2 waiting_human).
- Attempts used: 1.
- Budget used: $0.00 known of $50.00.
- Elapsed: 0 min 03 s since start, to the last event.
- Time: 0 min 00 s in agent calls; 0 min 00 s in gates and checks; 0 min 00 s in acceptance replays.
...
## Recent events
...
- 12:43:23 intent kind=replay op=op-0007-5c950fe2
- 12:43:23 intent kind=command op=op-0008-0929d093
- 12:43:24 outcome (took 0 min 00 s) kind=command op=op-0008-0929d093 result=pass
- 12:43:24 outcome (took 0 min 01 s) kind=replay op=op-0007-5c950fe2 result=pass
- 12:43:24 run-stopped status=needs_human

## Next
    runner approve command-demo-20261006T124321Z-0ec2e2c1 signoff
    runner reject command-demo-20261006T124321Z-0ec2e2c1 signoff -m "why"
    runner resume command-demo-20261006T124321Z-0ec2e2c1

$ cat result.txt                       # the candidate is in the work tree: look before approving
Verified candidate

$ $TR/runner approve latest signoff                             # exit 0
signoff: approved. Continue with: runner resume command-demo-20261006T124321Z-0ec2e2c1

$ $TR/runner resume                                             # exit 0
write: accepted as f7be7d6
run command-demo-20261006T124321Z-0ec2e2c1: done. See /tmp/demo/.runs/command-demo/command-demo-20261006T124321Z-0ec2e2c1/STATUS.md

$ $TR/runner runs                                               # every run; or `runs command-demo`
run                                    status       known spend  started
command-demo-20261006T124321Z-0ec2e2c1 done               $0.00  2026-10-06T12:43:21Z

$ git log --oneline
f7be7d6 Write the demonstration result
d0398ee demo
```

The `kind=replay` events are the **acceptance replay**: after the gate passed in your work tree,
the runner ran it once more in a throwaway clean checkout of the candidate, so a file git ignores
cannot make it pass (`acceptance_replay = false` turns it off).

You are still on the run branch. `git checkout main` to go back; merging is your call.
`runner status --open` opens `STATUS.html`, the same page with a link to every prompt, log and
verdict of the run (`open` on macOS, `xdg-open` on Linux; elsewhere it prints the path).

## 3. Your first real workflow

Work in your own repository, on a clean checkout. Start from a template:

```sh
cd /path/to/your/repo
$TR/runner new --list
$TR/runner new debugging --describe     # or implementation, refactoring: every parameter, required or default
```

| Template | Shape |
|---|---|
| `implementation` | design → tests from the design → implementation → report → sign-off |
| `debugging` | a regression test accepted only while it fails with the symptom → diagnosis → fix within a diff budget → same pattern elsewhere → sign-off |
| `refactoring` | characterisation tests and baselines → N bounded steps, one commit each → final review → sign-off |

Write a parameters file. This is the shipped `examples/templates/debugging.params.toml`, for a
Python project with a division-by-zero bug:

```toml
bug = "div-zero-17"
report = "briefs/div-zero-17.md"                 # relative to the generated workflow file
symptom = "ZeroDivisionError"                    # the failing test must fail for THIS reason
area = "src/calc/**"                             # what the fix may change
regression_test = "tests/regression/test_div_zero_17.py"
test_one = "python3 -m unittest tests/regression/test_div_zero_17.py"
suite = "python3 -m unittest discover -s tests -t ."
invariants = ["python3 -m compileall -q src"]
```

`examples/templates/debugging-network.params.toml` is the alternative for a domain defect (a feed
receiver dropping packets): it adds the `packet-trace-pradip` reviewer per stage, for example
`fix_reviewers = ["packet-trace-pradip"]`, and tightens `fix_max_files = 3`.
`debugging-sanitizer.params.toml` does the same for a defect whose evidence is a ThreadSanitizer
report, with the `memory-model-mei` bench on every stage, `concerned-carlos` on the fix, and the
project's C++ rules (`rules_file = "benches/cpp-standards.md"`, copy `examples/templates/benches/`
beside the workflow too) shown to every author and reviewer of the run.

Render, commit, and check without a model:

```sh
mkdir -p briefs && $EDITOR briefs/div-zero-17.md          # the bug report the agents read
$TR/runner new debugging -o bug-17.toml --params bug-17.params.toml
git add briefs bug-17.toml && git commit -qm "bug-17 workflow"
$TR/runner validate bug-17.toml          # 12 tasks: reproduce, diagnose, fix, siblings, their reviews, signoff
$TR/runner check-gates bug-17.toml
```

`new` writes the file only if it loads. The result is an ordinary workflow file: edit it freely.
In a toy repository, `check-gates` printed:

```text
reproduce:gate:1: pass
reproduce:gate:2: fails as intended
fix:gate:1: pass (same command as reproduce:gate:1)
fix:gate:2: pass
fix:gate:3: fails as intended (waits for reproduce)
```

A plain `fail` on a gate that should pass (the suite, the build) means your commands are wrong on a
clean clone: fix them before spending money. Gates run on the **committed** files only, and a gate
that leaves untracked files behind is an error, so ignore build products (`__pycache__/`, etc.).

**Choose the agent and its permissions.** The template uses `claude` with Claude Code's default
`auto` permission mode. To change it, edit `[defaults]` in the generated file and add a profile:

```toml
[defaults]
agent = "claude-yolo"
run_budget_usd = 30.0

[agents.claude-yolo]
kind = "claude"
permission_mode = "bypassPermissions"
```

| Kind | Writer, by default | YOLO (explicit) | Reviewer (always read-only) |
|---|---|---|---|
| `claude` | `--permission-mode auto` | `permission_mode = "bypassPermissions"` | tools `Read,Glob,Grep` only, no MCP servers |
| `codex` | `sandbox_mode = "workspace-write"`, approvals never | `sandbox = "danger-full-access"` | `sandbox_mode = "read-only"` |
| `copilot` | `--allow-all-tools` (path checks stay on) | `permission_mode = "bypassPermissions"`: adds `--allow-all-paths --allow-all-urls` | tools `view,glob,grep`; shell, write, url, memory denied |

A YOLO profile makes `validate` print:

```text
warning: agent profile 'claude-yolo' is configured to bypass its sandbox or permissions
```

Codex and Copilot profiles also print `agent 'codex' reports no dollar cost: dollar limits do not
bind on it; ...`: set `run_budget_tokens` (section 5).

**Spend money: qualify and start.**

```sh
$TR/runner doctor bug-17.toml            # REAL MODEL CALLS: probes each profile; cached afterwards
$TR/runner start bug-17.toml             # REAL MODEL CALLS; checks out run/debug-div-zero-17-<id>
```

`start` checks out `run/<workflow>-<id>` and leaves the checkout there; `main` is untouched.

**Watch** from another terminal while it runs:

```sh
$TR/runner status                         # STATUS.md: tasks, spend, progress, what is in flight, what to type next
$TR/runner status --watch                 # the same, printed again whenever it changes (every 5 s check); Ctrl-C ends it
$TR/runner status --open                  # STATUS.html in the browser: the page plus links to every prompt and log
$TR/runner activity latest --tail 20      # recent tool/hook events of the agents
```

While a call runs, the page is refreshed every 15 s (`status_refresh_s` in `[defaults]`): its first
line says when the run last changed, and "In flight" shows each call's age, attempt, step and the
tokens the provider's record shows so far.

**Exit codes** of `start` and `resume`: `0` every task accepted; `255` a person is needed (a human
task, an escalated finding, a blocked task); `2` something failed, the budget ran out, the run was
paused, or the workflow or environment is wrong. `STATUS.md`'s "Next" section prints the exact
commands for the situation.

| To | Run |
|---|---|
| stop at the next safe point (nothing lost) | `runner pause [RUN]` (`--now` interrupts the call in flight; `resume` calls it again in the same attempt, using no attempt) |
| continue | `runner resume [RUN]` (default: the latest unfinished run) |
| sign off or send back a human task | `runner approve RUN TASK` / `runner reject RUN TASK -m "why"`, then `resume` |
| rule on an escalated finding, or any open blocking finding of a held or blocked task | `runner resolve RUN FINDING --as resolved\|advisory\|upheld -m "why"` for each one STATUS lists, then `resume` |
| give a failed or blocked task fresh attempts | `runner retry RUN TASK` (`--apply-patch` keeps the set-aside work), then `resume` |
| change the plan mid-run | edit and commit the workflow, `runner replan RUN`; `--reopen TASK` reverts accepted work with revert commits |

`RUN` is a run directory name, a UUID prefix, or `latest`. Do not edit the work tree or the run
branch while a run is active or paused: `resume` refuses if you did.

## 4. Where everything is stored

By default the record is **`.runs/` at the top of the repository** that holds the workflow's
`root`. Each run gets `.runs/<workflow>/<workflow>-<UTC start>-<uuid8>/`, named like its branch
`run/<workflow>-<uuid8>`.

| Path | What |
|---|---|
| `.runs/README.md` | how to read the record, cold |
| `.runs/lock` | present while a runner works in this repository (`flock`) |
| `.runs/qualification-cache.json`, `.runs/doctor/`, `.runs/check-gates/` | what `doctor` and `check-gates` established |
| `<run>/STATUS.md` | the run in words: status, spend, tasks, what it needs, what to type next |
| `<run>/STATUS.html` | the same page as static HTML, with links to every file of the record (`runner status --open`) |
| `<run>/state.json` | the engine's state, the single source of truth, with its `schema_version` (do not edit) |
| `<run>/run.json`, `events.jsonl`, `workflow.toml` | identity and totals, the event log, the workflow as started |
| `<run>/integrity.json` | hashes of the decision-bearing files, checked around every call |
| `<run>/tasks/NNN-task/` | `STATUS.md`, `task.json`, `findings.json`, `commit.json` when accepted, `failed.patch` when set aside |
| `.../attempt-N/` | `prompt.md`, `gate.log`, `replay.log` (the acceptance replay), `changes.diff`, `result.json`, `verification.json` |
| `.../attempt-N/invocation-N/` | one agent call: `argv.json`, `prompt.md`, `stdout.log`, `stderr.log`, `outcome.json` |

The runner also pins the trees it verified as Git refs under `refs/code-smith/<run>/`.

The directory writes its own `.gitignore` (`*`), so it never dirties your tree; listing `.runs/` in
the project's `.gitignore` too is harmless. Nothing is committed unless you commit it.

**To keep the record elsewhere**, for example outside the repository, name it in the workflow:

```toml
[defaults]
runs_dir = "../records"       # relative to the top of the repository
```

`start` then remembers the location in the repository's git config (`codesmith.runsdir`), so
`status`, `resume`, `approve` and the rest find it with no option. For one command, or to override
the workflow:

```sh
export CODE_SMITH_RUNS_DIR=/path/to/records      # every command then agrees
# or, per command, BEFORE the subcommand:
$TR/runner --runs-dir ../records start bug-17.toml
$TR/runner --runs-dir ../records status
```

The option and the variable are **not remembered**: every later command on those runs (`status`,
`resume`, `approve`, `prune`, ..., and `doctor`, whose cache lives there) needs the same setting, or
it answers `no runs directory in the repository that holds '.'`. A relative path is relative to the
repository's top in all three places. `runner status --runs-dir DIR` (option after the subcommand)
is refused with `--runs-dir is an option of the runner, not of 'status': put it before the command`.

`runner runs` lists every run with status, known spend and start time; `runner runs WORKFLOW` those
of one workflow (a file or its name). `runner prune` deletes the pinned refs of finished runs. To delete a run, remove
its directory, then `runner prune --orphans` removes its refs.

## 5. Budgets

Dollars: `run_budget_usd` in `[defaults]` (default $50; the templates set $30 for debugging) caps
the run, and `budget_usd` (default $5) caps each Claude call, passed to Claude as
`--max-budget-usd` and reserved against the run before the call starts. When the next call cannot
be covered, the run stops with exit 2 and `STATUS.md` says so; `runner resume RUN --add-budget 20`
raises the cap and continues without repeating finished work.

Tokens: Codex, Copilot and command agents report no price, so dollar limits cannot stop them.
`run_budget_tokens` (default 0, no cap) is a stop line on their recorded tokens: no new call starts
once it is reached, and the calls that cross it complete. Each such call reserves tokens before it
starts (`unpriced_call_reserve`, or by default twice the run's largest call and at least 500,000,
never more than is left), and unpriced reviewers run side by side only while their reservations
fit. A call whose usage stays unknown (a command agent's, a killed call's) is held at its
reservation, so the run still reaches the line; the stop reason says "N measured + M held" and
names those calls. A provider refusal before any work counts nothing. Qualification probes are
held to a probe-sized bound (`probe_reserve_tokens`, 20,000 each by default), so a 3,000,000 cap
starts without `runner doctor` first. Continue with
`runner resume RUN --add-tokens 2000000`. Spend shows on the `Spend:` line at the top of
`STATUS.md` (known dollars of the budget, reserved, and unpriced calls with their tokens and the
cap), in `run.json`, and in the `known spend` column of `runner runs`.

Estimates: give an unpriced profile your rates and the runner turns its tokens into an estimate,
shown as `≈$X estimated from profile rates` and never counted as known spend:

```toml
[agents.codex]
price_per_mtok = { input = 2.0, cached_input = 0.5, output = 8.0 }   # USD per million tokens
# estimated_counts = true     # your choice: hold the estimate against run_budget_usd too
```

Subscription windows: after every Codex call the runner reads the rate-limit rows of its session
rollout, records them in the call's `outcome.json` (`limits`) and keeps what the run observed, so
the Spend line ends with, for example, `Observed Codex weekly account window: 2.0% → 3.0%
(shared-account observations, not attributable run usage)`: the window is the account's, not the
run's.

## 6. When something goes wrong

| Symptom | Do |
|---|---|
| `the work tree is not clean, and a run starts only from a clean tree` | commit or remove the listed files, then `start` again |
| `no runs directory in the repository that holds '.'` | give the same `--runs-dir` or `CODE_SMITH_RUNS_DIR` the run started with |
| `--runs-dir is an option of the runner, not of 'status'` | put it before the subcommand: `runner --runs-dir DIR status` |
| `check-gates` reports `fail` for a gate that should pass | the command does not work on a clean clone of the committed files; fix it and commit |
| `doctor` grants fewer capabilities than a task type requires | read `.runs/doctor/.../invocation-N/stderr.log`; log in, fix the CLI or sandbox, `runner doctor WORKFLOW --force` |
| exit 255, waiting for a person | `runner status`, then the `approve` / `reject` / `resolve` / `retry` it prints, then `resume` |
| stopped: out of budget, or token cap used up | `runner resume RUN --add-budget USD` or `--add-tokens N` |
| a task failed | read its last `gate.log`; `runner retry RUN TASK [--apply-patch]`, then `resume` |
| environment failure (login, sandbox, unknown flag) | fix the machine, `runner doctor WORKFLOW --force`, `resume` |
| the runner was killed | `runner resume` (it reconciles first and calls the interrupted agent again, using no attempt; `--stop-orphans` if a process of the old agent's group is still running) |
| `another runner holds this repository` | wait, or `runner pause RUN`; if it names `.git/code-smith.lock`, that runner uses another runs directory: give the same `--runs-dir` |

Everything else, including the decision chart for every stop: [runbook](runbook.md).

## 7. Where to read next

- [Tutorial](tutorial/README.md): the ideas, with diagrams; [chapter 6](tutorial/06-writing-a-workflow.md)
  writes workflows from scratch.
- [03 — Workflow file](03-workflow-file.md): every key of the workflow, type and persona files, and
  the templates.
- [Runbook](runbook.md): operating a run, every stop and recovery.
- [AGENTS.md](../AGENTS.md): what the runner can do, in one table, and the rules for changing it.
