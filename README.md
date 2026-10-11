# code_smith

A generic runner for agent work. It takes a **workflow** (a list of tasks that forms a DAG), runs
it to completion with headless coding agents, and leaves a complete, navigable record of everything
that was done. Tasks can be of any type: design, implementation, tests, code review, design review,
summaries, plain commands, human sign-off. The output of one stage feeds the next stage or stages.

New here? The [quick start](docs/quickstart.md) takes you from a clean checkout to a first run in about 30 minutes.

`start` and `resume` execute one producer transaction at a
time using command, Claude Code, Codex or GitHub Copilot CLI agents, verified by gates, checks, review panels and
human decisions. Panels run read-only jobs up to `max_parallel`, apply findings in workflow order,
and consolidate rework. `resolve` records a person's ruling on an escalated finding, or on any open
blocking finding of a held or blocked task; `resume --add-budget USD` (or `--add-tokens N`) continues a
budget pause without repeating completed reviews. `doctor` qualifies profiles using observed
effects; `check-gates` tests commands in clean clones of the committed files. Before acceptance a
producer's gates run once more on a clean checkout of its candidate (the acceptance replay), so a
file git ignores cannot make a gate pass. Accepted commits contain
exactly the verified candidate. `./runner --help` lists every command; the
[runbook](docs/runbook.md) says when to use each.

A producer's git state is a lease: a commit, branch switch or `.git/info` edit by an agent or a
gate is put back, with the files left to be judged. The record's decision files are also pinned in
git, so `runner repair-record` puts back what an edit or a `git clean -fdx` changed or removed, and
the run lock lives in the git directory, out of the writer's reach. One runner works in a checkout at a time
whatever `--runs-dir` says, every agent process is recorded before it starts and tracked as a
whole process group, and a call interrupted by a kill or `pause --now` is called again without
using an attempt. `state.json` is versioned and migrated on load, its steps and statuses move only
by an explicit table, and its invariants are checked on every save.

While a run works, `STATUS.md` and `STATUS.html` (the same page with links into the record) are
refreshed every 15 s with the calls in flight, progress and the latest events; `runner status
--watch` follows them and `runner status --open` opens the HTML page. Codex calls record their
subscription rate-limit window, and a profile may give `price_per_mtok` rates for an estimate that
is never counted as known spend unless the owner says so (05, Honest accounting).

[Provider routing](docs/provider-routing.md) adds automatic quota fallback between qualified
profiles and configurable `mechanical` / `standard` / `high` complexity-to-model mappings.
Provider changes retain task progress, review findings and spending history.

Run the model-free tests from the repository root:

```sh
python3 tools/run_tests.py
```

That runs the suite in small parallel shards (about four minutes on eight cores);
`python3 -m unittest discover -s tests -q` runs the same tests serially.

To run one module, go to the tests directory first: `cd tests && python3 -m unittest
test_runbook`.

The runner needs Python 3.11+ and Git; no installation or third-party Python packages are required.
Run `./runner --help` from the checkout for the available commands. For a model-free example, copy both
[`examples/command-demo.toml`](examples/command-demo.toml) and
[`examples/command_agent.py`](examples/command_agent.py) into a clean scratch Git repository,
commit them, then, from that repository, run `/path/to/code_smith/runner doctor command-demo.toml`
and `/path/to/code_smith/runner start command-demo.toml`.
The run stops at the human `signoff` task (exit 255); `/path/to/code_smith/runner approve latest signoff`
and `/path/to/code_smith/runner resume` finish it. `start` leaves the run branch checked out.
(The examples below write `runner` for short.)

Real-agent `doctor` probes can incur model costs. Qualification records report known spend and
unpriced calls separately; Codex and Copilot have no dollar cap. The adapter unit tests use recorded output and
CLI help (Copilot's fixtures are constructed from its help and shipped event schema, never captured). The known Codex namespace startup
failure is correctly classified as an environment failure, not successful or blocked task work.
See [agent compatibility and limits](docs/agent-compatibility.md).

For a review-panel example, copy [`panel-demo.toml`](examples/panel-demo.toml),
[`panel_agent.py`](examples/panel_agent.py), and `command_agent.py` into a clean scratch Git
repository. The whole test suite uses scripted agents only. `replan` installs revised definitions;
`--reopen` undoes affected acceptances with resumable revert commits.
Live agent execution requires successful qualification on the host and profile used.

A new workflow can start from a template instead of a blank file: `runner new TEMPLATE -o wf.toml
--params p.toml` writes an ordinary workflow file with the project's paths, commands and reviewers
filled in, and keeps it only if it validates. Three templates ship: `implementation` (design →
tests → implement → report → sign-off), `debugging` (a regression test accepted while it fails
with the reported symptom → diagnosis → a fix within a diff budget under which the same frozen test
passes → the same pattern elsewhere → sign-off) and `refactoring` (characterisation tests and
baselines → N bounded steps, one commit each, all held to them → final review → sign-off). Each
stage has a fixed review panel; a project adds its language or domain bench (for example the
`packet-trace-pradip` for a defect that starts from a packet capture) through per-stage
`*_reviewers` parameters. Example parameter files are in [`examples/templates/`](examples/templates/);
`runner new --list` shows the templates; the format is in
[03 — Workflow file](docs/03-workflow-file.md#templates-and-runner-new).

## Read in this order

| File | Contents |
|---|---|
| [`docs/quickstart.md`](docs/quickstart.md) | From a clean checkout to a first run |
| [`docs/01-requirements.md`](docs/01-requirements.md) | What the runner must do, and what it will not do |
| [`docs/02-concepts.md`](docs/02-concepts.md) | Kinds, types, personas, acceptance, rework, findings, runs. The model in full |
| [`docs/03-workflow-file.md`](docs/03-workflow-file.md) | Reference for the workflow file, type files and persona files |
| [`docs/04-run-directory.md`](docs/04-run-directory.md) | Reference for the run record: layout, every file, the result schemas |
| [`docs/05-architecture.md`](docs/05-architecture.md) | Modules, the scheduler, the agent interface, git, state, command line |
| [`docs/06-scenarios.md`](docs/06-scenarios.md) | WHEN/THEN scenarios, each naming the test that will prove it |
| [`docs/tutorial/`](docs/tutorial/README.md) | A guided tour with diagrams: the idea, a task end to end, panels and findings, safety, architecture, writing a workflow |
| [`docs/runbook.md`](docs/runbook.md) | Operating a run: commands, stops, recovery. The recovery rows are run by `tests/test_runbook.py` |
| [`library/`](library/) | Starter library (shipped, used by the examples): task types, reviewer personas and workflow templates (`workflows/`, rendered by `runner new`). Content, not code. See [`library/README.md`](library/README.md) |
| [`examples/book-module.toml`](examples/book-module.toml) | A worked example workflow; `runner validate` and `runner graph` run on it |
| [`examples/`](examples/) | Model-free demos (`command-demo.toml`, `panel-demo.toml`) and the [live panel example](examples/live-panel/README.md) |
| [`docs/headless-observability.md`](docs/headless-observability.md) | Per-invocation hook logs and `runner activity` |
| [`docs/provider-routing.md`](docs/provider-routing.md) | Quota fallback between qualified profiles; complexity-to-model mapping |
| [`docs/agent-compatibility.md`](docs/agent-compatibility.md) | The agent CLI versions the adapters were built against, and what only `doctor` on the host can confirm |
| [`docs/reviewer-families.md`](docs/reviewer-families.md) | Reviewers from another model family than the author's: the inventory (`doctor`), the assignment, and why a stage or per-round rotation was not the answer |
| [`tools/README.md`](tools/README.md) | `tools/lint_mermaid.py`, the diagram checker |
| [`.claude/skills/agent-checkpoints/`](.claude/skills/agent-checkpoints/SKILL.md), [`.codex/`](.codex/STATUSLINE.md) | A skill for durable milestone checkpoints of agent work; Codex hook and status-line configuration |

## The idea in five lines

1. A workflow is an acyclic DAG of tasks. Each task has one of four **kinds**: `produce`, `review`,
   `check`, `human`. A **type** (design, implement, code-review, …) is a template over a kind.
2. Work is accepted only by **verifiers**: commands that exit 0, reviewers that return a structured
   verdict, or a person. Never by the agent's own report. A producer with no verifier is rejected.
3. A review panel of **personas** (Principled Priya, Clause-by-clause Chen, Dependable Diego, …) reviews the same
   work in parallel. Blocking findings go back to the author once, consolidated, for bounded rework.
4. Downstream tasks start only when upstream work is **accepted**. Accepted work is committed and
   frozen. A producer owns the work tree from its first edit until it is committed or set aside, and
   exactly the candidate that was verified is what gets committed.
5. Every run has a UUID and a directory laid out like a build directory, which explains itself to
   any person or agent who opens it later.

## Observability

Headless agents expose per-invocation hook logs and `runner activity`; see
[observability](docs/headless-observability.md) for verified Claude/Codex behavior.
Opening this repository itself in Claude Code logs hook events to the git-ignored
`.claude/hook-logs/`, because `.claude/settings.json` wires every hook event to
`.claude/hooks/log-hook.py`.

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE): use, copy, modify and share it for any noncommercial
purpose (personal, educational, research, non-profit) under the terms in `LICENSE`; commercial
use needs a licence from the copyright holder.

Required Notice: Copyright miavenet (https://github.com/miavenet)
