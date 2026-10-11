# 02 — Concepts

The whole model, in the order the pieces depend on each other.

## Workflow, task, run

A **workflow** is a TOML file: a named list of **tasks** joined by `needs` into an acyclic graph.
A **run** is one execution of a workflow, with a UUID and a directory. A workflow can have many
runs. A run works from its own frozen copy of the workflow.

## Kinds: the four behaviours the engine knows

| Kind | What it does | Who acts | Result |
|---|---|---|---|
| `produce` | Creates or changes files in the repository | An agent, with write access | `done` or `blocked`, a summary, and on rework a response to each finding |
| `review` | Judges the work of one producer, from one perspective. Read-only | An agent, read-only, in a new session | `pass` or `block`, a summary, findings |
| `check` | Runs commands. Passes if all exit 0 | The runner. No agent | pass or fail, with the output |
| `human` | Pauses until a person approves or rejects | A person | approve, or reject with a comment |

`review`, `check` and `human` are the **verifiers**. A `check` or `human` task verifies a producer
when it says `verifies = "<task>"`; otherwise it is a **standalone step** in the DAG:

- A standalone `check` that does not pass is `failed`. There is no author to send it back to, so it
  is not retried on its own; `retry` re-runs it after the person has changed something.
- A standalone `human` task that is rejected is `blocked`, and the note is kept in `decision.json`.
- In both cases dependants are `skipped`, and the run ends with exit 2 or 255.
- A standalone check that is not `read_only` is a writer, and gets the same protection as a
  producer: a BASE snapshot before it, and afterwards the tree is returned to BASE and verified.
  Whatever it builds belongs in ignored paths. A change it leaves in the snapshot fails the check.

## Types: templates over a kind

A **type** is a file in the library: a kind, a prompt template, parameters, and defaults for agent,
model, gates and limits. `design`, `specify`, `implement`, `test`, `code-review`, `design-review`,
`report-review` and `summarize` ship as a starter library, with `reproduce`, `diagnose` and `fix`
for debugging and `characterise` and `refactor-step` for refactoring. A task names a type and fills its parameters. Adding a type
of work means adding a file. The bare kinds `produce` and `review` are themselves small type files
in the library, so a one-off task goes through the same code path as any other.

## Personas: the reviewer's perspective

A **persona** is also a file: a title, what that reviewer cares about, what counts as blocking for
them, and what is out of their scope. A review task's `perspective` parameter names one. The starter
library has `principled-priya`, `clause-by-clause-chen`, `dependable-diego`, `protocol-petra` and
`timeline-tanaka`, and a five-persona C++ review bench (`api-aurelie`, `concerned-carlos`,
`meticulous-mira`, `neckbeard-nate`, `picky-paola`), `root-cause-rosa`, `gatekeeper-gao` and
`steadfast-stefan` for debugging and refactoring, two domain benches, `packet-trace-pradip` and
`memory-model-mei`, and a security bench, `sentinel-sato`, for work that reads input it
does not control; see
[`library/README.md`](../library/README.md). A persona can carry its own agent and model, so a
perspective can be given to a different model from the author's, and a `voice` for how it
phrases its summary and advisory findings. The reviewer never sees the persona file: it is
rendered as prose, with the panel's effective mode (a file's `advisory` is only a default the
workflow may override), the producer's type and parameters, and the project's `rules_file`,
the same text the author was shown.

Telling a persona what is **out of scope** matters as much as its focus: each such item names
who owns it; the review target lists who else sits on the panel, and a reviewer who sees such a
specialist matter with its owner absent raises it once, advisory. A directly checkable violation
of a requirement the brief or project rules state, quoted in the finding's detail, is blocking
when its owner is absent; specialist judgement without such a quote is advisory. Say which it is. A project's rules live in one `rules_file` the personas cite rather than restate, so
five reviewers do not each file the same violation; where two still land on one line, the rework
feedback says so and the author fixes once and answers each. Overlapping locations from different
reviewers on the same attempt with different severities are grouped in feedback and task STATUS;
the runner does not change their severities because they overlap.

## Workflow templates: a starting file, not a runtime feature

A **workflow template** (`library/workflows/<name>.toml`) is a workflow file with parameters.
`runner new` fills it in and writes an ordinary workflow file, which the owner reads, edits and
commits; the engine never sees the template, so what runs is what the owner read. Three ship, one
per shape of work: `implementation` (design, tests from the design, implementation, report),
`debugging` (a regression test accepted while it *fails* with the reported symptom, a diagnosis,
a fix within a diff budget under which the same frozen test passes, the same pattern elsewhere) and
`refactoring` (characterisation tests and baselines, then the owner's steps, each one bounded
commit held to the same frozen safety net, then one final review). Each stage's review panel is
fixed in the template, one blocking reviewer per question; a project adds its language or domain
bench through the per-stage `*_reviewers` parameters. See
[03 — Workflow file](03-workflow-file.md#templates-and-runner-new).

## Outputs and inputs
A producer declares two path sets:

| Key | Meaning | Default |
|---|---|---|
| `outputs` | Its **deliverables**: what downstream tasks are pointed at, what the manifest lists, what is frozen on acceptance | required |
| `writes` | Every path it **may change**, deliverables and helpers alike. A change outside this set is reverted by the runner and the attempt does not pass | the same as `outputs` |
| `removes` | Paths that must **not** exist afterwards, for a task whose job is to delete something | none |

An output is normally required to exist and be non-empty. An entry written as
`{ path = "pkg/__init__.py", may_be_empty = true }` lifts the second condition. Paths under `.git`
or `.runs` are rejected in all three sets.

**The repository must be able to see the work.** Everything the runner guarantees rests on git
tree snapshots, so the root must be a git repository, and:

- A path in `outputs`, `writes` or `removes` that git ignores is refused, at `validate` and again
  after every attempt (an agent may have edited an ignore file). An ignored output would pass the
  existence check, be invisible to reviewers, and never be committed.
- `.gitignore`, `.gitattributes` and `.gitmodules`, at any depth, are protected by default. A task
  may change one only by listing that exact path in its `writes`.
- After every attempt the candidate tree is scanned for **embedded repositories and submodule
  entries** (mode 160000) and for changed paths whose parent has become a symbolic link. These are
  treated like a change outside `writes`: the runner removes them while it still can, the attempt
  does not pass, and the feedback names them. So a restore never meets a path it must refuse.
- The guarantee is at **tree level**: two work trees are equal when git hashes them to the same
  tree. With end-of-line or clean filters in `.gitattributes`, bytes that git normalises away are
  outside the guarantee, exactly as they are outside a commit.

Path patterns have one meaning everywhere; see
[03, Path patterns](03-workflow-file.md#path-patterns).

After each attempt the runner writes a **manifest**: each declared output that exists, with its
hash, mode and size, plus the id of the **candidate tree** (a git tree of the whole work tree) the
attempt produced. It also records the task's **input manifest**: the hashes of the upstream outputs
it was given. That is what later lets the runner say which accepted work relied on which version of
a file.

A task's **inputs** are derived, not written by hand. For every task in its `needs`, and for the
task a review targets, the runner gives the agent:

- the upstream task's id, type and title,
- its summary (a few sentences from its result),
- its output file list from the manifest,
- for a review: the diff of the work under review.

The agent reads the files itself. Prompts carry pointers and summaries, not file contents, so they
stay small however large the deliverables are. The exception is a reviewer qualified only for
**text-only review**: it cannot read the repository, so the runner must give it the complete
evidence in the prompt, and the review is recorded as such.

## Acceptance
A producer's attempt passes through verifiers in a fixed order, cheapest first. The first failure
stops the sequence and sends the work back.

| Step | Verifier | Cost |
|---|---|---|
| 1 | The agent's invocation completed properly and its answer is valid, and it did not answer `blocked` | free |
| 2 | Every declared output exists (and is non-empty unless `may_be_empty`); every `removes` path is gone | free |
| 3 | Nothing outside the task's `writes` changed, and no protected or frozen file changed. Any such change is reverted by the runner | free |
| 3b | The candidate is within the task's **diff budget**, if it has one (`max_changed_files`, `max_changed_lines`) | free |
| 4 | The task's own `gate` commands exit 0, or, for an **expected failure** (`expect = "fail"`), fail as stated | seconds to minutes |
| 5 | Every `check` that `verifies` this task passes | seconds to minutes |
| 5b | The **acceptance replay**: the task's own gates pass once more on a clean checkout of the candidate (W-03); it runs beside step 6's readers when there are any (N3) | one cold gate run |
| 6 | The review panel leaves no open blocking finding in the ledger | agent calls |
| 7 | Every `human` task that `verifies` this task approves | a person's time |

A producer must have at least one of steps 4 to 7, or the workflow is rejected when it loads. Steps
1 to 3 always apply. The agent's own report is recorded and never used.

**Every result is bound to the exact candidate it judged.** After step 3 the runner records the
candidate tree id. Each gate, check, verdict and approval is stored with that id and a hash of the
verifier's configuration. The runner takes a new snapshot after every verifier and before the
commit. The comparison is of the **whole snapshot**: tracked files and untracked files git does not
ignore. If it differs from the candidate, a gate or a check rewrote or littered something: the
runner **puts the tree back to the candidate** by the restore mechanism, all earlier results are
void, the attempt does not pass, and the feedback names the command and the files. Build products
belong in paths the repository ignores; those are outside the snapshot, and outside the rollback
guarantee; the acceptance replay (step 5b) is what keeps them from deciding a gate. `check-gates` finds a littering gate before any money is spent.

Two refinements:

- A check marked `restores = true` is one whose job is to change the source and put it back: a
  mutation tester, a formatter dry run. The runner expects the tree to differ while it runs, and
  afterwards restores the candidate itself instead of trusting the tool to have done so, even when
  the tool crashed or was killed. A difference is then not a failure.
- `read_only` on a check is a claim, and claims are verified. If the snapshot after a `read_only`
  check differs, the **check** fails with "declared `read_only` but wrote: …", the reviews that ran
  beside it are void and run again, no producer attempt is used, and that check is scheduled as a
  writer for the rest of the run.

**Step 3b, the diff budget.** `writes` bounds *where* a task may change things, not *how much*.
A task or its type may set `max_changed_files` and `max_changed_lines` (never `[defaults]`: a budget
describes one kind of work, such as a minimal fix or a reviewable refactoring step). The runner
measures the change itself, from the transaction's pinned BASE tree to the pinned CANDIDATE tree,
never from `HEAD`, the work tree or the agent's report: `git diff --numstat` with renames found at a
fixed 50% similarity and the owner's diff configuration, external drivers and textconv off, so the
same two trees always give the same count. Lines are added plus deleted; a binary file counts as one
file and zero lines. What is binary is decided by the file's content alone (git's test: a NUL byte
in its first 8000 bytes), never by attributes: the diff runs in a scratch repository that borrows the
objects and has no work tree, so `* -diff` or `binary` in the user's attributes file, the system
file, `.git/info/attributes` or a `.gitattributes` the author wrote cannot turn a changed text file
into zero lines. The same rule shapes the review diff and the recovery patch, so an
attribute cannot hide a hunk from the panel either. A pure rename counts one file and zero lines. Over budget, the attempt does not
pass, **no gate, check or reviewer runs**, and the feedback gives both counts, the limits and the
five largest files. The author and the reviewers are told the budget beside the gates in `{gates}`.
A rework is measured from BASE again, not from the previous attempt, so small
increments cannot add up to a large change. The no-progress rule applies as for a gate. Nothing is
recorded before it: a resumed run measures the two pinned trees again and gets the same answer.

**Expected failures.** A gate table may say `expect = "fail"` with a `fail_pattern`: the task's
deliverable is a test that fails, such as a reproduction of a reported bug. It passes only when the
command exits non-zero, other than 126 or 127, not by a timeout and not killed by a signal (a
negative exit, or 129 to 192, the shell's 128 + signal), and its output matches the pattern. A
missing binary, a missing file, a hang or a killed process is not the reported bug, whatever it
printed before it died. The feedback says which way it was wrong: `passed (expected a failure)`,
`failed without matching /…/`, or `could not run` (`killed by signal N` for a signal death).
The result is recorded and bound to the candidate like every gate's. It is **never re-run as a
regression gate** of a later task: it is a claim about the candidate it was accepted on, and after a
fix it would fail because the fix worked. The author and the reviewers see it in `{gates}` as
`<command>   (must FAIL, matching /…/)`.

**Step 5b, the acceptance replay (W-03).** Gates run in the live work tree, which also holds every
file git ignores: a stale `build/`, a package in an ignored `.venv`, a generated file. Such a file
can make a gate pass that a fresh clone of the commit would fail, and the reviewers never see it.
So, once the live gates and checks have passed, the runner runs the task's own gates again, in
their order, in a throwaway checkout of exactly the candidate: a shared clone of the repository
(no object is copied) outside the work tree, holding the candidate's files with `HEAD` and the index
at the transaction's starting commit (the git state the live gates see), and the repository's
`user.name` and `user.email`. The work tree, the index, `HEAD` and the lease
are not touched. Each gate keeps its timeout and its meaning: an expected failure must fail as
stated there too. If one does not pass, the attempt is sent back like a gate failure: the feedback
says the gate passed in the work tree and failed on a clean checkout, and names the ignored paths
of the work tree that the checkout lacked (those new since the task's base first) as the likely
cause. The replay is recorded under `replay` in `verification.json`; the checkout is removed
afterwards, and its directory is named in an intent first, so `resume` after a kill removes it and
verifies again.

**The replay runs beside the panel (N3).** When the task has a panel (reviewers, or read-only
checks run beside them), the replay does not wait in front of it: it starts as one more job of the
panel's first batch, alongside the review calls, and the panel decides only once it has ended. It
touches neither the work tree nor the lease, so the readers see what they always saw, and their
gate evidence is still the live gates' output. A replay that fails decides first: the panel's
verdicts on that candidate are void (as when a reader wrote), no further reader of it is called,
and the attempt is sent back with feedback that names the replay and its gate first and says the
panel was set aside; the reviewers judge the next candidate. A replay that passes changes nothing
in the decision, and is not run again for that candidate. Each answer of the batch is saved as it
comes, so a kill while the replay still runs keeps the answers already returned; `resume` removes
the checkout and runs the replay again, beside the readers still to be called, before any verdict
is applied. `verification.json` holds the replay as `pending` until it ends; a reverified
candidate whose replay passed on the same starting commit says `pass` with `reused_from`, the
attempt holding the evidence (ACC-53). Without a panel the
replay runs in step 5b as before. `[defaults] replay_beside_panel = false` keeps the serial order
for a workflow whose gates are heavy local builds or test suites that would compete with the
reviewers' tools for the same CPU; a run frozen before the key existed keeps it too.

It is on by default (`[defaults] acceptance_replay = true`); `acceptance_replay = false` turns it
off for the workflow or for one task. A run frozen before the key existed replays nothing, as
before. Where a cold build is too slow, `gate_cache = ["build/"]` (in `[defaults]` or on a task)
names paths copied from the work tree into the checkout before the gates run: the owner vouches for
them, and the replay records what was copied. A cache that stores absolute paths (a CMake
`CMakeCache.txt`) does not survive the move; a compiler cache outside the tree does.

After every attempt the runner also records the ignored paths that appeared since the task's base
(`git ls-files --others --ignored --directory`, the run record left out, at most 50): in
`outputs.json` and in the review target as `ignored_since_base`, so a reviewer can see files that
are in no diff.

When all verifiers pass on one unchanged candidate, the task is **accepted**: exactly that candidate
is committed and its outputs are **frozen**.

## Rework: the one loop
```
                    ┌──────────── consolidated feedback ─────────────┐
                    ▼                                                │
 pending ─► produce (attempt n) ─► gates/checks ─► review panel ─► open blocking findings?
                    │                   │                                │ no
                    │ blocked           │ no progress                    ▼
                    ▼                   ▼                             accepted ─► commit ─► frozen
              needs a person          failed
```

- Gate or check failure: its output goes back to the author at once. Reviewers are not called for
  work that does not pass its checks.
- Review: **the whole panel runs before anything goes back.** The author receives one list of all
  blocking findings (advisory ones are attached for information) and does one rework.
- **What a rework prompt holds.** Always two parts: (a) the immediate cause, which is the gate
  output, the check output, the rejection note or the new findings; and (b) every open blocking
  finding that has **no author response since it was last raised or kept open**. The author's
  `responses` must cover exactly set (b), each once. A finding the author already answered `fixed`,
  which no reviewer has judged yet because a gate failed in between, is listed for information and
  needs no second answer. Responses are recorded against the attempt number.
- Four counters are kept apart:
  - **Producer attempts.** A rework is a new attempt. `max_attempts` (default 3) counts attempts,
    whatever sent the work back. Attempt directories are numbered once and never reused, even after
    `retry` resets the counter.
  - **Review rounds.** A reviewer's round 1 is the first time it sees a candidate, whichever attempt
    that is. If attempt 1 fails its gates, the panel's round 1 happens on attempt 2, and it is a full
    review.
  - **Protocol retries.** An agent call that ends without a proper terminal event, or with an answer
    that fails validation, is retried up to twice with a fresh invocation. This is a fault of the
    call, not of the work: it uses no producer attempt and creates no finding, while retries
    remain. If the third call is still invalid, the attempt is spent and the author is told that the
    call did not end with a valid answer. A try is charged only when a call **returns**: a call
    that never returned, because the runner was stopped (`pause --now`, a kill), is counted as an
    interruption on its own and called again on `resume` in the same attempt, so interruptions
    never spend the attempt. After five interruptions in one attempt the task is set aside as
    `blocked` for a person, with no attempt used (05, "Interrupted calls and protocol tries").
    For a producer whose call completed successfully but whose answer failed validation, the
    retry repairs only the answer: the original answer, diagnostic, required finding IDs and
    schema, with instructions to change no files. It continues the returned session when
    `resume` is qualified; otherwise it uses a fresh read-only call if that exact profile and
    model already have a qualified read-only boundary (from doctor or a reviewer). No extra
    probes are made for this optional mode. Without either qualification, or without trustworthy
    completion, the existing authoring retry applies. Snapshots surround every repair. A writer
    that edits files is recorded as ordinary authoring and its actual candidate goes through
    all verifiers; a read-only repair that writes is restored and the task fails. A kill resumes
    the saved repair. Reviewers keep their existing protocol retry behavior.
  - **Provider failures.** A call that fails because the provider did (an error status such as
    5xx or 529, or an overloaded service) is retried and never charged to the task. From the second
    such failure on, the task moves to its next qualified fallback profile, if it has one. When the
    retries are used up the run stops (status `failed`, exit 2, no attempt used) and `resume` tries again.
    For a reviewer this holds too: the candidate stays held, the other reviewers' answers are kept,
    the reviewer stays pending with its provider tries given back, and `resume` calls only it. A quota failure (a
    429 or "usage limit reached") switches profile at once and puts that provider on a five-minute
    cooldown; with no fallback the run stops with its saved work retained.
    See [provider routing](provider-routing.md).
- When attempts run out, the task is `failed` if a gate or check sent it back last, and `blocked`
  (a person is needed, with the open findings listed) if review did.
- The author's session is continued for a rework **when its profile is qualified for `resume`**, so
  it keeps its understanding and the provider's cache is reused. Otherwise, and after an agent
  error, a timeout or an interruption, the next attempt starts a new session with the full prompt
  plus the feedback. `resume` is an optimisation, never a requirement, and the rule for
  `responses` is the same either way.
- **The work tree is kept through every interruption inside a task**: a timeout, an agent error,
  a provider switch, a rejected answer and a killed runner all leave the author's files where
  they are (the tree goes back to base only when the task ends failed or blocked, after
  `failed.patch` is written). What is lost is the session. So a call that starts a new session
  over a tree that differs from the task's base is told so by the runner: "Work of this task is
  already in the work tree", the changed paths, read them and continue, do not start over. No
  model cooperation is needed and nothing is restored. Agent-written checkpoints
  ([observability](headless-observability.md)) stay opt-in, through the task's brief: they add
  the author's notes, not files the runner would otherwise lose.
- **No progress:** the task fails early only when the gate fails the same way **and the
  candidate tree is identical to the previous attempt's**, meaning the author changed nothing that
  matters. The same failure text after a real change to the source is not "no progress"; the attempt
  limit covers that case. A failing read-only check inside a panel follows the same rule, with the
  same cause text (`$ command`, then `[result, exit N]`).

## Findings
A finding is the unit of review feedback.

| Field | Meaning |
|---|---|
| `id` | Given by the runner: producer, persona code and a number, such as `implement/PE-2`. Unique in the run, so two producers reviewed by the same persona never collide |
| `severity` | `blocking` or `advisory`. A reviewer marked `advisory` in the workflow can only produce advisory findings |
| `title`, `detail` | What is wrong and why it matters |
| `location` | File and line or section, where that applies |
| `caused_by` | Later rounds only: the changed location that introduced the problem |
| `status` | Blocking: `open`, `resolved`, `disputed`, `escalated`, `superseded`. Advisory: `noted` |

**Advisory findings never stay open.** An advisory finding is recorded as `noted` at the end of
the round that raised it. It is shown to the author for information, needs no response and no
resolution, and no later round is asked about it. Feedback labels it by the candidate it was
raised on: "Raised on your previous attempt (N)" when that is the candidate being reworked,
"Historically noted on attempt N, candidate …" when it is older (unknown when an old record lacks
them), and "Raised blocking on attempt N; ruled advisory by … on candidate …" for a blocker a
person demoted. The feedback also says that an experiment, a diagnostic or an extra test an
advisory suggests is not a requested change: an advisory change should be small and support a
blocking fix. An author may optionally put a finding id
and note in `advisories_addressed`. This is an attributed report, not reviewer confirmation;
it changes no verdict, needs no extra call, and appears in later feedback and task STATUS.
The task STATUS lists "Advisories on the accepted candidate" ("Raised on this candidate") apart
from "Earlier advisories", and the run STATUS gives one line per accepted task with advisories on
its accepted commit (REC-48).

Repository inspection is available during code review. Building, testing or running the product
is not authorised; supplied gate results are the execution evidence. The target includes up to
8192 bytes from the current attempt's gate log, with candidate, attempt, path and truncation flag.
This is a review policy, not an execution sandbox.

**The verdict is derived from the ledger, not taken from the model.** After a reviewer's answer
is applied, the runner computes whether any blocking finding of that reviewer is open. An old
finding left `unresolved` blocks even if the reviewer raised nothing new. The model's own `verdict`
field must agree with the computed one, or the answer is invalid and is retried as a protocol error.

**One narrow repair, without a model call.** An answer whose only defect is `resolutions`
entries that name no finding in this producer's ledger, in a round where the required set is empty,
is accepted with those entries dropped rather than retried: the round asked for nothing, so `[]` is
the only correct value and dropping is not a guess about what the reviewer meant. The repair never
fires if any entry names a real ledger id — that is a reviewer confused about a real finding, and
stays a protocol error — and it is allowed **only when the repaired answer still derives a block**,
so it can never turn a would-be pass into an acceptance and can never manufacture one; a passing
answer with junk `resolutions` is still a protocol error. Eligibility is decided by the ledger the
answer is finally applied to, not the one a concurrent reviewer's call happened to see, because
reviewers on the same panel are applied to the ledger sequentially, in workflow order.

**Round 1** is a full review of the whole candidate, with the diff from the transaction's base.
A producer may set **`review_diff_from = "<task>"`** to start that diff earlier: at the accepted
commit of a producer among its needs (direct or transitive). The reviewers then see the cumulative
change since that task, every task accepted in between and this candidate together, and the prompt
tells them the diff spans from `<task>`. The refactoring template's `final` uses it to judge the
whole refactoring from `characterise` on. It changes only round 1: later rounds see the rework
diff below, because a fix is judged by what the rework changed. **Later rounds judge the fix.** A reviewer who
blocked is given its own open **blocking** findings, the author's response to each, and the rework
diff. It must mark each of those `resolved` or `unresolved`, each exactly once. A reviewer with no
open blocking finding returns an empty `resolutions` list.

**The rework diff is per reviewer.** For reviewer R it is the diff from the candidate R last
saw to the current candidate. The ledger stores `last_seen_candidate` for each reviewer. If an
attempt in between failed its gates and no panel ran, R's diff spans that attempt too, so nothing
changed there escapes review. After a plain `retry`, the work is a new line of work: every
reviewer's next round is a round 1, and blocking findings still open from the old line are closed
as `superseded`. `retry --apply-patch` continues the line when every reviewer last saw the
candidate it puts back: the open findings stay open and go to the author first, with the
reviewer's latest reason for keeping each one, and the next round judges the rework diff. (Before
this, the restored candidate came back as new work: the author was not told what was open, and
the panel found the same defect again at full price; lab 04 re-run.) When a reviewer last saw
some other candidate, the retry starts a new line and says so.

The rework feedback shows each open finding with the author's earlier answer and the reviewer's
latest word on it, tells the author the attempt number, and says when to answer `disputed`: a
requirement the brief, the design or the rules do not state, a case none of them makes
reachable, or a brief that conflicts with the finding. A kept-open dispute stops the task for a
person; that is the intended route, and cheaper than a round of machinery for a case nobody has.

A reviewer may raise a new blocking finding in two cases:

1. its `location` lies inside the rework diff, or
2. its `location` lies outside, and it names in `caused_by` a location that is inside the rework
   diff: the rework changed a function and broke an unchanged caller. The runner checks mechanically
   that the named location really is in the diff.

A new concern that meets neither case is recorded as advisory. Reviewers who passed are, by default,
shown the rework diff only, under the same two rules.

The author answers each blocking finding with `fixed` or `disputed` and a note. A finding the author
disputes and the reviewer keeps open becomes `escalated`: the task stops for a person, because two
agents disagreeing is not something a third loop settles. A `caused_by` claim the author thinks is
false is settled the same way.

**An escalation is a human decision, so it holds the tree like one.** The producer becomes
`waiting_human` with its candidate still in place, and the run stops with exit 255. The person runs
`resolve FINDING --as resolved | advisory | upheld` and then `resume`:

- `resolved` or `advisory`: the finding no longer blocks. If the ledger now has no open blocker, the
  runner checks the tree still equals the candidate, and acceptance continues from where it stopped.
  Every earlier verifier result is bound to that same candidate, so none is repeated and no producer
  attempt is used.
- `upheld`: the person sides with the reviewer. The finding stays open and blocking, the author may
  not dispute it again, and a rework follows if attempts remain.

Every open blocker of the held producer needs a ruling, not only the escalated one: STATUS names
them all, and its "Next" section lists a `resolve` for each. After the last ruling it says the
rulings are complete and that `resume` continues acceptance (up to a person who verifies the task,
if one does); no approval is asked for until such a person is reached (RUN-48). A ruling never
erases the rounds before it: the task's Review rounds table counts each finding by the severity it
had at the time, and counts the rulings and the blockers open now apart (RUN-47).

A person can also rule on any open blocking finding of a **blocked** producer, without an
escalation. `resolve FINDING --as resolved|advisory|upheld --note "why"` records the person,
time, note and candidate in that finding's history. The person is `--by NAME` when given
(`by_source` "flag"), else the login name (`by_source` "env"); the ruling also records whether
standard input was a terminal (`interactive`) and the names of agent-session variables present
(`agent_markers`, e.g. `CLAUDECODE`; never their values), and STATUS prints them ("ruled by owner
(non-interactive, name from the environment; agent session markers: CLAUDECODE)"), because a
ruling the operating agent typed under the owner's login otherwise reads as the owner's.
`approve` and `reject` record the same fields. `[defaults] rulings_require_interactive = true`
makes `resolve` (also with `--standing`), `approve` and `reject` refuse, recording nothing, when
standard input is not a terminal or an agent-session variable is set; `--by` is recorded but does
not replace the terminal (REC-49, REC-52). It is a speed bump against an agent ruling in the
owner's name, not authentication: a caller that fakes a terminal gets past it. The committed
rulings file is a ruling path too: with the flag on, a standing entry not recorded as ruled at a
terminal without agent-session variables is dropped ("not ruled at a terminal", RUN-74); the
same speed bump, since a hand-written `interactive = true` gets past it. The note stays
free text; it reads best when it keeps apart the decision, the accepted scope, the evidence and
the claims left unproven (`resolve --help`): a scope exception is not proof that a requirement is
impossible. Unknown or closed findings, running tasks,
and incompatible candidate histories are refused. For set-aside work, follow with
`retry TASK --apply-patch` and `resume`. If no blocker remains and the restored candidate is
unchanged, inspection and gates run in a fresh attempt directory without an author call.
Reviewers whose findings were settled on that candidate are satisfied by the ruling; other
review obligations still run. Later review prompts carry the human rulings. An upheld finding
stays open, with the person's note in the author's next feedback.

Acceptance with owner rulings is marked in both task and run STATUS, with links to the candidate,
findings and owner notes. A candidate id is a tree, so it is printed as "tree d3cf29b… (commit
d524bde)" once the commit that holds it is known, here and in the downstream prompt. Downstream inputs carry those rulings beside the author's summary,
attributed to the owner and bound to the upstream task, accepted candidate and each ruling's
original candidate. They do not grant a general exception. Notes are bounded with a full-record
pointer; old records without rulings add nothing. In a dispute, distinguish a false finding,
conflicting requirements and a requested scope exception. A reviewer keeping a dispute open must
show any proposed remedy (where it compiles or is specified), or leave the reading to a person.


Standing rulings carry a settled reading between runs. They are owner input: set `[defaults]
rulings_file` to a tracked, committed TOML file (an ignored one is refused). A run reads the copy
committed on its starting commit, never the work tree, pins its blob id, and protects the file in
every task. `resolve --standing` queues the entry in the run's state with the ruling, and the
run writes it to the file when it is done, for the owner to commit (or `runner export-rulings
RUN` does, for a run where nothing can touch the tree again). The entry carries the ruling's
provenance (`by_source`, `interactive`, `agent_markers`, `run`, `finding`) and the hash of the
task's effective brief: the prompt, the project rules, the gates, the declared outputs and the
upstream tasks with their declared outputs. Reviewers receive matching task entries in
`settled_by_person`, with `standing = true` and their provenance; a known finding location must
match the glob, and a changed brief drops a hashed entry. STATUS counts and lists supplied
rulings and names the part of the brief that changed. Downstream
implement inputs carry the matching standing rulings with owner rulings. Reviewers still decide;
no finding is automatically suppressed or demoted.

A blocking finding can suggest a gate in `gateable.command_hint`; on a finding not reported
blocking the hint is dropped, and a `reach_audit` larger than `findings_cap_bytes` is cut to the
entries that fit, each a repair (`review-repair` with `hints`), never a rejection that costs the
reviewer a try (FND-48). Task STATUS and the derived
`follow-ups.json` retain the hint and review rounds in which the finding was raised or reconsidered,
even after resolution. Rulings whose note contains “the brief will say so” also become lab follow-ups.
An optional `reach_audit` separates a test's claimed state, reach evidence and oracle; task STATUS
shows the latest supplied reviewer answer as a table. Its absence changes no acceptance rule.


Each producer has one **findings ledger** in the run record, holding every finding from every
reviewer and round, with its history. "What did review find, and was it fixed?" is answered by
reading one file.

## Dependencies and freezing
`needs = ["design"]` means *design is accepted*. Nothing starts on unreviewed work.

**Two milestones per producer.** Internally a producer has a *candidate* milestone (an attempt
has passed steps 1 to 3) and an *accepted* milestone. `needs` points at accepted. `reviews` and
`verifies` point at the candidate. Validation builds this expanded graph and looks for cycles in it,
so these are rejected at load, each with the dependency trace that shows the deadlock:

- a verifier that also lists its target in `needs` (it would wait for an acceptance that waits for it);
- a verifier that needs anything downstream of its target (V verifies A, V needs B, B needs A).

A `check` or `human` task that only `needs` an accepted task is a standalone step and is fine.

Once accepted, a task's outputs are **frozen**: they join the protected set of every later task, and
a change to them is reverted by the runner like any protected file. `protected` sets are a **union**
across built-in, workflow, type and task level: a task can add to the protected set and can never
narrow it. A later task B may change a
frozen file of A only by **claiming** it in its own `writes` (not merely `outputs`), and then:

- B must depend on A, directly or through other tasks. An overlapping claim between tasks with no
  order between them is rejected at load.
- `validate` lists every claim, so "task B will modify the approved design" is visible in the plan,
  and B's reviewers see the diff.
- B's verification also re-runs the gates and verifying checks of every accepted task whose outputs
  or inputs B touched. If B breaks accepted work, B does not pass.
- Accepted tasks that consumed the old version and were verified only by review cannot be re-checked
  mechanically. They are marked **stale against** the new version in the record and in `STATUS.md`,
  with the file and both hashes. The run does not pretend their acceptance covers the new content.

## Scheduling
A task is **ready** when everything in its `needs` is accepted. A verifier is ready when its
target's current candidate has passed the cheaper steps before it.

**A producer owns the work tree for its whole life cycle.** From the moment a producer starts
until it is committed or set aside, it is the run's **active producer**. While there is one, the
only things scheduled are its own gates, its verifying checks, its review panel and its rework.
No other producer starts, and no unrelated check runs, because the tree holds work nobody has
accepted: another task would build on it, and its changes would leak into the first task's review
and commit. Ownership is released only after the tree is back at a clean accepted state, which the
runner verifies by snapshot.

Within that rule:

- **Readers** are `review` tasks and `read_only` checks. The active producer's ready readers run
  together, up to `max_parallel`. This is where the parallelism is.
- A `check` not marked `read_only` is a writer and runs alone.
- A panel's results are gathered and applied together, in workflow order, when its last member
  finishes. So the decision does not depend on which reviewer happened to finish first.
- With no active producer, standalone readers may run together; the next producer is the first ready
  one in workflow order.
- **A pending human verification holds the work tree.** The run stops with exit 255 and nothing else
  starts until the person answers. Continuing other branches over a candidate awaiting approval
  would need the candidate to be stored away and later restored and re-verified; that is deferred.
  A standalone `human` task, which only `needs` accepted work, holds nothing.

Given the same workflow and the same results from agents, the same things happen in the same order.

## Failure
A task ends unsuccessfully as **failed** (attempts used up on gates, no progress, a reviewer changed
the tree) or **blocked** (the agent said it cannot do the task properly, a verifier's calls kept
failing at the protocol level, or attempts ran out with blocking findings still open). An escalated
finding is neither: it is a pause for a person, described under Findings. An **environment failure** is a third thing: a sandbox that cannot start, a
missing binary, failed authentication, an unreachable provider API. It stops the run at once with its cause and uses no attempt,
because retrying the work cannot fix the machine. (After an unreachable API the cached qualification is kept, so `resume` is enough.)

`failed` and `blocked` are statuses of any task. A verifier that did not pass its latest round is
`objected`; a verifier whose producer ended before it could run is `skipped`. The skip closure
follows `needs`, `reviews` and `verifies` together, so every task always has a status. A panel
whose members cannot produce a valid answer leaves its producer `blocked`, not `failed`: a person
is needed to repair the panel. The record distinguishes, per reviewer, a panel that could not
answer in the required form from one that simply did not finish (a timeout, an agent error, an
interruption), and every rejected answer is summarised — its claimed verdict and finding titles,
never guessed at from a field that happened to be malformed — in the producer's `STATUS.md`.

When a task fails or is blocked:

1. The candidate is kept in full: its tree is pinned under a private git ref so it cannot be garbage
   collected, and a complete binary-capable patch against the base is written to `failed.patch`.
   The capped, readable diff that reviewers saw is a separate file and is never used for recovery.
2. The paths it changed are returned to the last accepted state, **by type and mode**: regular
   files with their executable bit, symbolic links replaced as links and never written through, new
   files removed, parent paths checked so nothing outside the repository is touched. The runner then
   verifies by snapshot that the tree equals the accepted one. Only tracked and untracked-unignored
   paths are covered; ignored build output is not. Directories the restore emptied are removed, up
   to but not including the root. If a restore cannot be completed, that is an **environment
   failure**: the run stops, prints the paths, and leaves the tree alone.
3. Every task downstream of it is marked `skipped`, with the reason.
4. Independent branches continue.

The run ends with exit 2 (failed) or 255 (a person is needed). `retry TASK` gives fresh attempts,
either clean or with `--apply-patch`. `--apply-patch` checks the current tree only at the paths the
set-aside work touched, not the whole tree, so a commit that touches only the workflow or the brief
does not block it; it also refuses unless the branch is still a descendant of where the work was set
aside, no runner-made commit (an acceptance, or a `--reopen` revert) landed since, the pinned
candidate tree still exists, and every touched path is still inside the task's current `writes`. If
every check passes, the work is restored from the **pinned candidate tree**, by type and mode, before
the next attempt runs; `failed.patch` stays the portable copy for applying by hand and is never read
on this path. A refusal changes nothing and says why. While a producer transaction is open, `retry`
is refused for any other task and says which task the run is waiting on.

**Running out of budget is a pause, not a failure.** No new call starts, calls in flight
finish, and the run stops as `stopped` with exit 2. If a producer is active, its transaction stays
open and the expected tree is recorded, exactly as for a human pause. `resume --add-budget USD`
raises the run budget, is recorded as an event, and the run continues where it stopped. Agents
that report no cost (Codex, a command) count their estimates against the dollar stop line only
when their profile sets `estimated_counts = true`; panel batches use the same admission policy
as authors, including estimates and reservations. `run_budget_tokens` gives
them a stop line on tokens instead, with the same stop and `resume --add-tokens N`. Under that
line, a call whose usage stays unknown (no usage in its final event and none in the provider's own
record: a killed Copilot call, any `command` agent call) is held at its reservation, so the run
stops rather than leaving the call out (BUD-12). A reservation is bounded: `[defaults]
unpriced_call_reserve`, or twice the largest measured unpriced call of the run and at least 500,000
tokens, never more than is left under the line. Unpriced reviewers start side by side while their
reservations fit under the line together, so the line can be passed by up to one call's overrun
per call in the batch (BUD-17). The bound is for admission only: a call that ends without its
final event (a time-out, a kill, a provider failure after work began) with unknown usage counts
all that was left under the line when it started, so the run stops and `--add-tokens` decides
(BUD-20). Held tokens are never shown as measured: the stop reason and STATUS
say "N measured + M held" and name each held call (BUD-16). Quota, environment and transient
refusals all require positive no-work evidence to be free. A provider refusal seen to end before
any work spends nothing, and the panel's transient retry calls again (BUD-15). That needs positive
evidence: a Codex stream with no item event and no completed turn anywhere in it, a Claude error
envelope that says no turn ran; work after a retryable error, or a Claude transcript the runner
cannot find, is held (BUD-19).

## Runs
- `start` makes a run: a UUID, a directory, a branch `run/<workflow>-<uuid8>` **which it checks
  out**, and a **frozen copy of
  everything that defines the work**: the workflow, the library files it uses, the content of
  every `prompt_file`, and `root` resolved to an absolute path. Editing a brief on disk does not
  change a run.
- **A run starts only from a clean work tree.** There is no option to start dirty: a commit
  limited to a task's paths would still take the owner's uncommitted edits in those same files.
- `pause` stops a running run on purpose. The engine reads the request before it starts any
  agent call, command or review batch and stops there, so nothing is in flight and nothing is lost;
  `--now` interrupts instead, addressing the runner through the run lock. Both end in `stopped`.
  An author's or a reviewer's call that `--now` interrupts is called again on `resume` and
  charges no try (five interruptions block the producer, RUN-55, RUN-59).
- `resume` continues a run. Nothing is kept in memory between steps, so resuming is the normal way
  the runner works. Before doing anything it **reconciles**: it checks that the branch tip, the
  index and the work tree are what the state expects, completes or rolls back any operation whose
  intent was recorded but whose outcome was not, and stops with an explicit reconciliation error if
  someone changed the branch or the tree while the run was paused.
- Each invocation waits until its process identity is durable before task code starts. Its
  supervisor stays alive after a CLI leader exits while descendants remain in the group (and,
  on Linux, while adopted detached descendants remain). A saved CLI identity allows stopping
  surviving group members even if the supervisor was killed; macOS cannot track a `setsid` daemon.
  `resume` refuses beside those writers and names their pids; `resume --stop-orphans` stops the
  whole group before dispatching anything. A finished call whose own cleanup failed leaves an
  obligation that holds every dispatch and acceptance: the same flag stops its group, and when
  the stop keeps failing, `resume --abandon-cleanup` records a person's ruling that closes it
  (PROC-23). A reader whose sibling opened one before it was released is cancelled: the cancelled
  call or command did not run, so no try, no interruption, nothing spent (PROC-24); commands of a
  check or replay that ran before it are recorded with their seconds and the operation ends
  `cancelled` (PROC-26, PROC-29); if its own group's stop fails and the group lives on, that is a
  failed cleanup like any other: an open obligation with the same remedies, a replay keeping its
  checkout until it closes (PROC-25, PROC-29). `--abandon-cleanup --stop-orphans` abandons the
  open cleanups and stops the groups of interrupted calls in one resume (PROC-28). Older records
  retain leader-only recovery.
- `replan` brings an edited workflow into a run where that is safe. Changing an accepted task needs
  `--reopen`, including changes to the frozen project rules at the same `rules_file` path:
  the runner computes everything affected (its dependants, and every task that
  consumed its outputs), shows the list, and undoes those tasks' commits with **new revert commits**,
  newest first, so files a reopened task no longer produces do not linger. The branch is never
  reset. If a revert conflicts, the replan stops and changes nothing further.
- Each accepted producer is one commit on the run branch. The run branch stays checked out when the
  run ends; going back to the original branch, and merging, are the owner's call.
- **One runner per work tree (W-01, W-02).** The run lock is an flock in the git directory
  (`.git/code-smith/run-<digest>.lock`, one per runs directory), where `git clean -fdx` and
  `rm -rf .runs` cannot remove it; `.runs/lock` is still taken beside it for older runners, and a
  runner is refused if either is held. Every command that changes a run or starts work also takes
  an flock on `code-smith.lock` in the work tree's own git directory, so two shells with different
  `--runs-dir` cannot run two writers on one checkout.

### What a producer owns in git (W-01)

"Do not commit, branch, stash, reset or push" is a line of the author's prompt; it is also checked.
When a producer's transaction opens, the runner records its **lease**: the commit and the branch it
starts on, the bytes of `.git/info/exclude` and `.git/info/attributes`, and the run's refs under
`refs/code-smith/` (refs the runner pins later are added). After every author call, after every
gate and check, and before every step, it compares. If the author or a verifier committed, switched
or created a branch, moved or deleted the run branch, edited one of those files or deleted a ref,
the runner puts it back: the run branch at the starting commit and checked out, the index at its
tree, the files and refs as recorded. **The work tree is not touched**, so what the author left
there is judged as any candidate is: a file outside `writes` that it committed is reverted, a file
it hid through `info/exclude` becomes visible and is judged too. The attempt is **not** sent back for
it: nothing it did to git can reach the commit any more, and continuing costs no call. The repair is
an event (`git-repaired`) and a section "Git state put back" in the task's STATUS.md; the author's
own commits stay reachable from the reflog. Only a merge, rebase, cherry-pick, revert or bisect left
in progress stops the run, because finishing or aborting it is its author's decision: abort it,
then `resume`. The runner's commit takes the starting commit as its parent and refuses any other
HEAD; it never adopts one. A run recorded before the lease existed is not checked, as before.

## The record

Everything about a run is under one directory, regenerated summaries included. Any directory in it
can be opened cold: `index.json` says what each file is, `STATUS.md` says what happened in words,
and the `README.md` at the top of `.runs/` explains the layout once. See
[04 — Run directory](04-run-directory.md).

Every save of `state.json` checks the state's cross-field invariants (`invariants.py`): at most one
active producer, and it has a step and has not ended; an accepted producer has a commit and a
candidate and no blocking finding open; a pending attempt only at step `attempt`; and a few more. A
real run only reports a broken one, as an `invariant-violation` event and a line under "Needs
attention"; it never stops or changes the run for it. The test suite turns the report into an
error, so every scenario proves the invariants hold.

**The record survives its writers (W-02).** The runner's restores never touch the runs directory.
Every save writes the runs directory's `.gitignore` again if an author removed it, and a record
path that shows up in a snapshot is read as that, put right with a `record-ignore-restored` event,
never as the author's change. The decision files (`state.json`, `run.json`, `integrity.json`,
`events.jsonl` and everything the manifest covers, including each counted answer and each
prepared review prompt) are also pinned as one git tree under `refs/code-smith/<run>/_record`,
with only the runner's own bytes, written by git and moved by compare-and-swap. So when the
record is edited or deleted, an integrity failure names its way back, and no command writes into
the record until then: `runner repair-record RUN` puts the pinned bytes back with a
`record-repaired` event, and `resume` continues from the restored state. A pin that failed is
visible (`record-pin-failed`, a STATUS.md line), and a repair never puts an older pinned state
over a later one on disk. A ref moved by someone else stops the run (`record-pin-moved`). This
defends against accidents and makes a forged move visible, not against a hostile writer with the
runner's uid that also kills the runner. Attempt logs (prompts sent, agent output) are not
pinned: an attempt whose directory a `git clean` removed is named as lost, and its outcome, which
is in the state and the intents, is what the run continues from.
