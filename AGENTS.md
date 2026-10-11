# Working in code_smith — notes for agents and people

Read this first. It says what the runner is, what it can do, how to use it, and the rules that
keep work on it safe. The documents it points at are the authority where they go deeper.

## What this is

A generic runner for agent work. It takes a **workflow** (a TOML file: tasks forming a DAG), runs
it to completion with headless coding agents (`claude`, `codex`, `copilot`, or a scripted
`command`), verifies every result with **gates** (commands of the task itself), **checks**
(commands that verify a producer, as tasks of their own) and **review panels** (personas with a
blocking or advisory say), asks a **person** where the workflow says so, commits each accepted producer as exactly its verified candidate on a run branch, and
leaves a complete record under `.runs/`. Python 3.11+ standard library and Git only; no install.

## Capabilities, in one table

| Capability | Where it is defined |
|---|---|
| Task kinds: produce, check, review (panel), human; types and personas from a library (16 personas: a general bench, a C++ bench with voices, two domain benches and a security bench, each bench a parameter; personas are rendered as prose with the panel's effective mode) | `docs/02-concepts.md`, `library/README.md` |
| Workflow file: tasks, needs, outputs/writes/protected, gates (`new`, `expect = "fail"`, `fail_pattern`), checks, panels, `[defaults]` (incl. `runs_dir`, `status_refresh_s`, `rules_file`: the project's rules shown to every author and reviewer), `[agents.NAME]` (incl. `price_per_mtok`, `estimated_counts`), `[model_policy]`, diff budgets | `docs/03-workflow-file.md` |
| Workflow templates: `implementation`, `debugging`, `refactoring`, rendered by `runner new`; each takes `agent` (default `claude`) | `docs/03-workflow-file.md` (templates section) |
| Acceptance ladder: writes check → diff budget → gates → checks → acceptance replay (the gates again on a clean checkout; `acceptance_replay`, `gate_cache`) → panel → person; rework with feedback; no-progress rule; attempts | `docs/02-concepts.md` |
| Findings lifecycle: blocking/advisory, rework rounds, escalation, `resolve` on any open blocker of a held or blocked task, owner rulings carried downstream, one ownership rule with an absent-owner exception, advisories with their attempt, repair of malformed answers (response-only for a completed author) | `docs/02-concepts.md` |
| Agents: Claude Code, Codex, Copilot, command; qualification by `doctor`; YOLO via `permission_mode = "bypassPermissions"`; read-only reviewers | `docs/05-architecture.md`, `docs/agent-compatibility.md` |
| Provider routing: complexity → model, `fallback_agents`, quota and transient failures never charged as attempts; reviewer families: reviewers are dealt across the other model kinds (claude, codex, copilot all in the inventory; `doctor` says which this box runs) | `docs/provider-routing.md`, `docs/reviewer-families.md` |
| Budgets: dollars, reserved, unsettled, unpriced tokens; `run_budget_tokens` stop line with bounded per-call reservations (`unpriced_call_reserve`) and unknown usage held apart from measured; `resume --add-budget/--add-tokens`; estimates from profile rates; Codex rate-limit windows | `docs/05-architecture.md` ("Honest accounting") |
| Record and recovery: `.runs/<workflow>/<run>/`, intents, integrity manifest, the decision files pinned in git and `repair-record`, flock run lock in the git directory and one runner per work tree (`code-smith.lock`), the producer's git lease, supervised process groups (`resume --stop-orphans`), crash-resume at any point with interrupted calls charged nothing, `state.json` schema version, transition table and invariants, `pause`, `replan --reopen`, `retry --apply-patch` | `docs/04-run-directory.md`, `docs/runbook.md` |
| Observability: native hook telemetry per invocation, `runner activity`, STATUS.md and STATUS.html (refreshed during calls), `status --watch` / `--open` | `docs/headless-observability.md`, `docs/04-run-directory.md` |

## How to use it

First run, step by step, with prerequisites, costs and where the record goes: [docs/quickstart.md](docs/quickstart.md).

```sh
TR=$PWD                                           # run from this directory: the runner's own
$TR/runner --help                                 # every command
$TR/runner new --list                             # shipped templates
cd /path/to/target                                # the target repository, a clean Git checkout
mkdir -p briefs benches && cp $TR/examples/templates/briefs/*.md briefs/ && cp $TR/examples/templates/benches/*.md benches/   # the files the examples name
$TR/runner new debugging -o bug-17.toml --params $TR/examples/templates/debugging.params.toml
git add briefs bug-17.toml && git commit -qm "bug-17 workflow"      # check-gates and start need a clean tree
$TR/runner validate bug-17.toml                   # loads, expands, prints the DAG; no model call
$TR/runner check-gates bug-17.toml                # runs every gate on a clean copy; no model call
$TR/runner doctor bug-17.toml                     # qualifies agent profiles: REAL MODEL CALLS, costs money
$TR/runner start bug-17.toml                      # REAL MODEL CALLS; checks out run/<name>-<id> and stays there; stops at a human task (exit 255)
$TR/runner status [--watch [N] | --open] / activity / runs [WORKFLOW]   # watch
$TR/runner approve RUN TASK | reject RUN TASK -m NOTE | resolve RUN FINDING --as ...
$TR/runner resume [RUN] | pause [RUN] | retry RUN TASK | replan [RUN] [--workflow FILE] [--reopen TASK]
$TR/runner repair-record [RUN] [--dry-run]        # put back record files an edit or `git clean` changed
$TR/runner export-rulings RUN                     # write the standing rulings of a run where every task is terminal
```

The workflow file lives in the target repository, which must be a clean Git checkout. `start`
creates branch `run/<name>-<id>`, **checks it out and leaves the checkout there** (also after the
run ends); it never touches `main`. `git checkout main` to go back. The record goes to `.runs/` at
the repository top, or to `runner --runs-dir DIR` (before the command), `CODE_SMITH_RUNS_DIR`, or
`[defaults] runs_dir` (remembered by `start`), all relative to the repository top. The `runner`
script needs Python 3.11 and says so on an older one. A model-free end-to-end example
is `examples/command-demo.toml` with `examples/command_agent.py` (see `README.md`).

## Rules for anyone changing this code

1. **Never make a live model call from a test or a casual check.** Tests use scripted agents only
   (`tests/fake_agent.py`, `examples/command_agent.py`). `doctor` and `start` on a workflow whose
   agents are `claude`, `codex` or `copilot` spend real money; run them only when the owner asks.
2. **Every behaviour has a scenario and a test.** `docs/06-scenarios.md` lists WHEN/THEN rows with
   the test's docstring label; add a row with your change. Run the suite from this directory:
   `python3 tools/run_tests.py` (about four minutes: shards of ten tests, eight processes at a
   time; `python3 -m unittest discover -s tests` is the same suite serially).
3. **Invariants that must survive any change:** acceptance is decided only by verifiers, never by
   an agent's own claim; frozen and protected files stay as they are; one commit per accepted
   producer, of exactly the verified candidate; every write to the record is durable and
   resumable (intents, `write_durable`, the integrity manifest); unpriced usage never counts as
   zero dollars; a reviewer is read-only.
4. **Documents stay true.** `README.md`, `docs/0x-*.md`, `docs/runbook.md` (parsed by
   `tests/test_runbook.py`), `library/README.md` and `docs/tutorial/` describe the code as it is.
   Mermaid diagrams are linted (`tools/lint_mermaid.py`). Change the document in the same change
   as the code.
5. **Style:** plain sentences, decisions with reasons, no marketing; small functions; comments
   only where the reason is not obvious; standard library only.
6. **Self-contained.** Nothing here may depend on anything outside this repository: do not add
   links or paths that leave it, and keep the names of other projects out of the documents.

## Layout

```
runner                 entry point (no install)
src/codesmith/         cli, workflow (loader + validation), templates, engine (scheduler + ladder),
                       panels, findings, budgets, prompts, agents, providers, qualification,
                       preflight, checks, gitops, record, replan, proc, activity, patterns, validate,
                       transaction (the producer's state machine), schema (state.json versions), invariants
library/               types/, personas/, workflows/ (templates)
examples/              model-free demos, book-module, templates/*.params.toml
tests/                 unittest, scripted agents, recorded CLI output
docs/                  01 requirements … 06 scenarios, quickstart, runbook, tutorial/, references
```
