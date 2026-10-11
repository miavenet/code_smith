# 01 — Requirements

## Purpose

Take a workflow of varied tasks and carry it to completion with headless agents, unattended,
leaving a record that a person or an agent can review afterwards. "Completion" means every task is
accepted by something other than the agent that did it.

**The goal behind it: drive a project autonomously using agents.** The runner is
a means to that.

**The order of priorities: accuracy and safety are paramount. Autonomy is
secondary to them.** Where they conflict, accuracy and safety win, every time:

- A stop that protects accuracy or safety is the runner working correctly. It is never removed,
  shortened or made optional to get a longer unattended run.
- "Nothing is accepted on an agent's word" is the first rule. No feature may let work be accepted
  with less verification than a supervised run would have required.
- Anything that widens what agents may decide on their own (planning tasks, continuing past a
  waiting task, looping across runs) keeps a human gate by default, and removing that gate is an
  explicit, recorded choice by the owner, per workflow.
- Within those limits, needless stops are avoided: people are asked for genuine decisions only.

**The challenge is the optimal trade-off (owner, same day)**, not maximal caution. A runner that
asks a person about everything is accurate and useless; one that asks about nothing is autonomous
and untrustworthy. The design separates what is traded from what is not:

| | Treated as | Examples |
|---|---|---|
| **Safety invariants** | Hard constraints. Never traded | The repository is never corrupted; work is never lost; nothing is accepted unverified; an agent never decides beyond its task; the record cannot be rewritten |
| **Accuracy, autonomy, cost** | Balanced against each other, per task | How many reviewers, which model, whether a person signs off, how many attempts, how much is spent |

The balance is set by **risk**, not by habit: human attention goes where a mistake is costly or hard
to undo **and** machine verification is weak (a design decision, a hand-decoded fixture, a plan);
agents run alone where verification is strong (code behind property tests, mutation checks and a
frozen test suite). And it is set from **evidence**: the run record already holds what is needed to
measure it, so a gate is added or removed because of what the numbers show, never by guess.

## From the owner

| # | Requirement |
|---|---|
| R1 | **Generic.** Tasks can be design, implementation, tests, code review, design review, summaries, commands, human sign-off, and kinds not yet thought of. A new type of work needs a file, not a code change |
| R2 | **Takes a task list and runs it to completion.** The list forms a **DAG**: a stage may feed one next stage or several |
| R3 | **Stage outputs feed later stages.** The runner passes them on; the author does not paste them |
| R4 | **Reviews are tasks**, and a review type takes a **reviewer perspective** as a parameter: Principled Priya, Dependable Diego, process manager, Clause-by-clause Chen, technical or project manager, and others |
| R5 | **One directory per run, with a UUID**, holding all work for every task and every re-attempt, structured like a build directory and friendly to an agent that later reviews status or summarises |
| R6 | **Model agnostic**, with reasonable defaults. Claude Code and Codex must work in headless mode; any other agent is configuration |
| R7 | **Accurate and efficient** |

## Derived

| # | Requirement | From |
|---|---|---|
| R8 | **Deterministic control.** Task order, routing, acceptance and state are fixed by the workflow and by recorded results. Only the agents' own work varies | R2, R7 |
| R9 | **Nothing is accepted on an agent's word.** Acceptance comes from exit codes, structured verdicts, or a person | R7 |
| R10 | **Bounded.** Every agent call has a time limit and, where the agent allows, a money limit. Rework is limited. The run has a budget | R7 |
| R11 | **Resumable.** A crash, a budget stop, a reboot or a human pause loses nothing | R2 |
| R12 | **Auditable.** For every attempt: the exact prompt, command line, agent output, check output, verdict, findings, cost | R5 |
| R13 | **The runner spends nothing itself.** Every model call goes through the configured agent CLI; `doctor` probes are such calls and can cost money | R7, R8 |
| R14 | **No dependencies.** Python 3.11+ standard library. The container cannot install packages | environment |
| R15 | **Safe by default.** No agent's permission system or sandbox is bypassed unless the workflow says so, in a place a reviewer sees | research |

## Not goals

- An agent loop or an LLM client. The agents are these already.
- Breaking a project into tasks. That is done by a person with an agent's help, and reviewed, before
  a run. The runner checks the result is well formed.
- Arbitrary cyclic workflows or conditional routing. One built-in loop (rework) covers the need.
- Parallel writers in the first version.
- Pushing, merging or opening pull requests. The run branch is handed to the owner.
- Scheduling, notifications, a server or a UI. It is a command-line program that exits.
