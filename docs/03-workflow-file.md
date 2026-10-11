# 03 — Workflow file, type files and persona files

All three are TOML, read with Python's standard library. Unknown keys are errors everywhere, so a
typo is caught before anything runs.

**Where relative paths resolve.** `root`, `library` and `prompt_file` resolve against the
directory of the workflow file. Every path inside tasks (`outputs`, `writes`, `removes`,
`protected`) is relative to `root`. `run.json` records the resolved absolute paths.

## Workflow file

```toml
name = "book-module"            # names the run directories. Default: the file name without its extension
root = "../.."                  # the repository, relative to this file. Default: the file's directory. Must be a git repository
library = ["library"]           # extra directories of types and personas, relative to this file, searched before the built-in ones

[defaults]                      # every key optional
agent = "claude"
model = ""
complexity = "standard"         # "mechanical" | "standard" | "high"; see Provider routing below
max_attempts = 3
timeout_min = 30
gate_timeout_min = 20
budget_usd = 5.0
run_budget_usd = 50.0
run_budget_tokens = 0            # stop line for agents that report no cost (Codex, Copilot, command): 0 = no cap
unpriced_call_reserve = 0        # tokens one such call is held to under that line: 0 = twice the run's largest call, at least 500k
probe_reserve_tokens = 20000     # tokens one qualification probe of such an agent is held to under that line (at least 1)
max_parallel = 4
recheck_passed = "diff"         # after rework, reviewers who passed see the rework diff: "diff" | "never"
branch = "run"                  # "run": a branch per run. "current": commit on the checked-out branch
commit_trailer = ""
protected = ["docs/spec/**"]    # never changeable by any task in this workflow. Tasks can add to it, never remove
diff_cap_bytes = 200000         # prompt size caps, see Placeholders
inputs_cap_bytes = 40000
findings_cap_bytes = 60000
status_refresh_s = 15           # how often a working runner rewrites STATUS.md/STATUS.html during a call; 0 = never
runs_dir = ".runs"              # where the record goes, relative to the repository top (04, Where it lives)
rulings_file = "rulings.toml"   # optional standing rulings: committed, relative to the repository top
rules_file = "docs/RULES.md"    # the project's rules, shown to every author and reviewer (see Task keys)
reviewer_family = "different"   # reviewers default to another model family than the author's; "any" turns it off (provider-routing.md)
acceptance_replay = true        # run each producer's gates once more on a clean checkout before accepting (02, Acceptance, step 5b)
gate_cache = []                 # ignored paths copied into that checkout first, e.g. ["build/"]; the owner vouches for them
rulings_require_interactive = false   # true: resolve/approve/reject (and resume --abandon-cleanup) refuse a non-terminal or agent session, even with --by; standing rulings not ruled at a terminal are dropped
replay_beside_panel = true      # run that replay beside the review panel's calls; false: before them (02, step 5b; heavy local gates)

[agents.codex]                  # a named profile per agent. Its non-secret settings and their hash are recorded in the run
sandbox = "workspace-write"
# review_mode = "provided_context"   # only for a reviewer qualified for text-only review; see 05, Capabilities
# price_per_mtok = { input = 2.0, cached_input = 0.5, output = 8.0 }   # USD per million tokens: an estimate, see below
# estimated_counts = false      # true: the estimate counts against run_budget_usd like known spend

[[task]]
id = "design"
type = "design"
title = "Design the L2 order book"
prompt = "Design the order book described in ..."      # or prompt_file = "items/design.md"
outputs = ["docs/design/05-order-book.md"]
reviewers = ["principled-priya", "clause-by-clause-chen"]

[[task]]
id = "implement"
type = "implement"
needs = ["design"]
outputs = ["src/book/**", "tests/book/**"]
gate = ["cmake --build build", "ctest --test-dir build -R book"]
reviewers = ["principled-priya", { perspective = "dependable-diego", advisory = true }]

[[task]]
id = "signoff"
type = "human"
needs = ["implement"]
```

Qualification probes under `run_budget_tokens`: before `start` creates a run (and on a `resume`
that qualifies again), each probe of an agent that reports no cost is held to
`probe_reserve_tokens` (20,000 by default, at least 1), or to what is left under the line when
that is less; a probe whose usage stays unknown counts that amount. Probing a writer and a
read-only reviewer is about eight probes, so the line needs room for about 160,000 tokens at the
default: `start` refuses ("Raise run_budget_usd or run_budget_tokens … or run `runner doctor`
first") only when the line is reached before its last probe starts, which with probes of unknown
usage is a cap under about 8 × `probe_reserve_tokens`; the documented caps (3,000,000 in the
tutorial) start. A run recorded before this holds its probes to the same bound (BUD-24).

### Task keys

| Key | Kinds | Meaning |
|---|---|---|
| `id` | all | Required. Unique. `[A-Za-z0-9][A-Za-z0-9_-]*`. A dot is reserved for the ids the runner generates for panel members |
| `type` | all | Required. A type from the library, or a bare kind (`produce`, `review`, `check`, `human`) for a one-off. `produce` and `review` are minimal type files in the built-in library |
| `title` | all | Shown in status, and the commit subject for a producer. Default: the id |
| `prompt`, `prompt_file` | produce, review | The brief. A type's template wraps it. Giving both is an error |
| `rules_file` | produce, review | The project's rules (a Markdown file, relative to this file), shown to the author under "Project rules" and to every reviewer of the work in the same words; a reviewer may block on a violation. Default: `[defaults] rules_file`; `""` opts a task out. A panel member inherits its producer's unless its entry sets one. Frozen in the record as `briefs/<id>.rules.md` |
| `needs` | all | Task ids that must be **accepted** first |
| `outputs` | produce | Required. Paths or globs of the deliverables. An entry may be a table: `{ path = "pkg/__init__.py", may_be_empty = true }` |
| `writes` | produce | Every path the task may change, helpers included. Default: the same as `outputs`. A change outside it is reverted and the attempt does not pass |
| `removes` | produce | Paths that must not exist afterwards |
| `gate` | produce | Commands that must exit 0 after each attempt. An entry may be a table: `{ run = "ctest -R book", new = true, fail_pattern = "BOOK-" }`, or `{ run = "pytest tests/regression/test_17.py", expect = "fail", fail_pattern = "ZeroDivisionError" }` for an expected failure, see below. `fail_pattern` is a Python regular expression searched in the command's combined standard output and error: in its last 6000 characters, the part kept for feedback, so a test runner whose summary comes last is matched and an early line of a long output is not |
| `max_changed_files`, `max_changed_lines` | produce | The **diff budget**: at most this many files, and this many lines added plus deleted, changed from the transaction's base. Integers, at least 1. Also allowed in a produce type; not in `[defaults]`. Each key falls back to the type's on its own: a task that sets only `max_changed_lines` keeps its type's `max_changed_files`. Checked before any gate, see [02, Acceptance](02-concepts.md#acceptance). A binary file counts zero lines; binary means by content (a NUL in the first 8000 bytes), whatever any `.gitattributes` or attributes file says |
| `acceptance_replay`, `gate_cache` | produce | The task's own acceptance replay switch and cache paths, over `[defaults]` (02, Acceptance, step 5b). `gate_cache` entries are plain paths under the root: no wildcards, `..` or `.git` |
| `reviewers` | produce | Panel shorthand, see below |
| `review_diff_from` | produce | A task id. Each reviewer's first round sees the diff from the tree of that task's **accepted commit** to the candidate, instead of from this task's own base: the cumulative change, capped like any review diff, and the prompt says it spans from that task. It must name a produce task among this task's `needs`, directly or through their needs, and the task must have reviewers. Rework rounds still see the rework diff. See [02, Review](02-concepts.md) |
| `reviews` | review | The producer under review. Set automatically for panel members |
| `perspective` | review | A persona name |
| `advisory` | review | `true`: this reviewer's findings never block |
| `verifies` | check, human | The producer this task verifies. A failure or rejection sends that producer to rework |
| `run` | check | Required for a check. Commands that must exit 0 |
| `read_only` | check | `true`: the commands write nothing, so the check may run in parallel with readers. The runner verifies the claim by snapshot |
| `restores` | check | `true`: the check changes source while it runs and is meant to put it back (a mutation tester). The runner restores the candidate itself afterwards. Cannot be combined with `read_only` |
| `params` | produce, review | Values for the type's own parameters. A `check` or `human` task has no type parameters, so a non-empty `params` there is an error |
| `agent`, `model`, `timeout_min`, `budget_usd`, `recheck_passed`, `complexity`, `fallback_agents` | produce, review | Override the defaults for this task. `complexity` and `fallback_agents` are described under Provider routing |
| `max_attempts` | produce | Overrides the default; at least 1 |
| `protected` | produce, review, check | **Added to** the protected set for this task. Protection is a union of every level and cannot be narrowed. Always on, at every level: `**/.gitignore`, `**/.gitattributes`, `**/.gitmodules`. A path written **literally** in a task's `outputs` or `writes` may be changed even if a protected pattern matches it; `validate` warns |

A `check` or `human` task accepts only `id`, `type`, `title`, `needs`, `params` and its own keys
(`verifies`, and for a check `run`, `read_only`, `restores`, `protected`).

### Path patterns

One matcher, written for the runner, is used for `outputs`, `writes`, `removes` and `protected`.
Python's `fnmatch` and `PurePath.match` disagree with each other on `docs/spec/**`, so neither is
used.

| Pattern part | Matches |
|---|---|
| a literal path, `src/book/side.hpp` | exactly that path |
| `*`, `?`, `[abc]` | within one path segment; never crosses `/` |
| `**/` at the start or in the middle | zero or more whole directories |
| a trailing `/**` | everything below that directory, at any depth, but not the directory name itself |
| a **wildcard** pattern with no `/`, such as `*.lock` | that name in any directory. A wildcard-free entry with no `/`, such as `CMakeLists.txt`, is a literal path at the root |

Patterns are relative to `root`, use `/`, and may not start with `/` or contain `..`.

Clarifications:

- A `removes` path must be covered by `writes`, like an output: a deletion is a change, and a change
  outside `writes` is reverted.
- `outputs` and `removes` conflict only when an entry is identical, or a literal output is matched
  by a `removes` entry. `outputs = ["src/**"]` beside `removes = ["src/old.cpp"]` is satisfiable.
- The ignored-path check tests a literal path directly, and a glob by probing a name under its
  literal prefix. A glob with no literal prefix is checked only after each attempt.
- `root` may be a subdirectory of a repository. `validate` warns, because snapshots and the
  clean-tree rule cover the whole repository.
- A file that a verifying check executes is protected for the check **and for the producer it
  verifies**, since the producer is the one that could edit it.
- `[agents.NAME]` accepts a closed set of keys: `kind`, `model`, `sandbox`, `review_mode`,
  `permission_mode`, `ignore_user_config`, `extra_args`, `argv`, `read_only_args`,
  `price_per_mtok`, `estimated_counts`. `kind` is
  `claude`, `codex`, `copilot` (GitHub Copilot CLI) or `command`; a profile named after one of the
  first three is that kind by default. `ignore_user_config` is an error on a `copilot` profile:
  that CLI has no flag to leave out user configuration. Copilot's `--allow-all`, `--yolo`,
  `--allow-all-tools`, `--allow-all-paths` and `--allow-all-urls` in `extra_args` or `argv` count as
  bypassing permissions (the warning below, and a fallback may not add them).
- A YOLO profile is explicit: `permission_mode = "bypassPermissions"`. On a `claude` profile it is
  passed as `--permission-mode bypassPermissions`; on a `copilot` profile its writers get
  `--allow-all-tools --allow-all-paths --allow-all-urls` (the CLI's `--allow-all`) instead of
  `--allow-all-tools` alone, and it is the only `permission_mode` a `copilot` profile accepts.
  Either way `validate` warns, a fallback may not be YOLO unless its primary is, and the profile's
  read-only (review) calls keep their read-only tools ([architecture](05-architecture.md)).

  ```toml
  [agents.copilot-yolo]
  kind = "copilot"
  model = "claude-sonnet-5"
  permission_mode = "bypassPermissions"
  ```
- `price_per_mtok = { input = 2.0, cached_input = 0.5, output = 8.0 }` gives the owner's rates, in
  US dollars per million tokens, for a profile whose calls report no dollar cost (`codex`,
  `copilot`, `command`; on a `claude` profile it is an error, since Claude prices its own calls).
  `input` and `output` are required, `cached_input` defaults to `input`; each must be a number of at
  least 0. Each call with observed tokens then records `estimated_usd` (cached input tokens at the
  cached rate, the rest of the input at `input`, the output at `output`), and the run sums them in
  `spend.estimated_usd`, shown on the Spend line as `≈$X estimated from profile rates`. An estimate
  is **never** known spend and does not count against `run_budget_usd`, unless the profile also sets
  `estimated_counts = true`: that is the owner's choice to hold the estimate against the dollar
  limit exactly like known spend (05, Honest accounting). `estimated_counts` without rates is an
  error. Neither key is part of the qualification fingerprint, so changing rates never asks for a
  new `doctor`.
- `status_refresh_s` (an integer, at least 0; default 15) is how often a working runner rewrites
  `STATUS.md` and `STATUS.html` while nothing else changes, such as during one long agent call; 0
  turns the refresh off (the pages are still written on every state change).
- `runs_dir` names the directory of the record, relative to the top of the repository (not to the
  workflow file), as `--runs-dir` and `CODE_SMITH_RUNS_DIR` do; both of those win over it. `start`
  remembers it for the commands on a RUN (04, Where it lives).
- Ids and names name directories and refs: an id, the workflow `name` or a persona name longer than
  64 characters is an error, and two ids that differ only by case are an error (they would share a
  directory on a case-insensitive file system). A `name` taken from the file name must satisfy the
  same pattern; set `name` when the file name does not.
- A review task must name a `perspective`. Persona codes match `[A-Z][A-Z0-9]*`. Every type in the
  merged library is validated, used or not.

Two questions compare a pattern with a pattern, which cannot be decided exactly, so they are
answered **conservatively**:

- **Is an output covered by `writes`?** Yes if the same entry appears literally in `writes`; or a
  `writes` entry `D/**` exists and the output starts with `D/`; or the output has no wildcard and a
  `writes` entry matches it. Anything else is a load error that asks for the entry to be repeated
  in `writes`.
- **Can two tasks' `writes` overlap?** Each pattern has a literal prefix: the segments before its
  first wildcard. Two wildcard-free patterns overlap when they are equal. A wildcard-free path and
  a pattern overlap when the pattern matches the path. Two patterns overlap when one's literal
  prefix is a path-prefix of the other's, and a pattern with no `/` overlaps everything. This may
  report an overlap that cannot happen; it never misses one.

### Gates: invariants and new checks

A gate given as a plain string is an **invariant**: a build, a lint, the existing test suite. It may
well pass before the task starts, and that is fine; its job is to stay green.

A gate marked `new = true` claims to prove this task's new behaviour, so it is expected to **fail
before the task is done, for the right reason**. `check-gates` runs every gate on the untouched tree
in a clean clone of the committed files (ignored and untracked files are not there) and reports
`pass`, `fail` or `error` for each (exit 126 and 127, a timeout, or a failure whose output does not
match the gate's `fail_pattern`, are `error`). It complains about a `new` gate that already
passes (`objection`), since it cannot show the task was done, and about one that errors, since a
missing import that exits 1 is not the intended failure. A `new` gate that fails with no
`fail_pattern` is reported `fails (no fail_pattern to confirm the reason)` (`unconfirmed` in
`results.json`): it fails, but nothing says why, so it counts as ok and `validate` warns about it. A gate or check that fails only because an output it
names does not exist yet is reported `fails as intended`: an output of its own task, or of a
task its task needs, directly or transitively, which it then names: `fails as intended (waits for
reproduce)` (`waits_for` in `results.json`). This holds also when it cannot run at all (exit 126 or
127) because what it runs is that missing output (`sh tests/regression/t.sh`); a timeout is always
`error`. A command names a literal output by its path, and a glob output either by the glob's
literal prefix (`tests/characterisation/x` for `tests/characterisation/x/**`) or by a path the glob
matches; a glob output counts as missing while the clone holds no file that matches it. A command that names a missing output of a task that is
**not** upstream is `error`: that dependency is missing from the workflow. It never complains about
an invariant that passes. Any file a command leaves behind in the clone makes its result `error`.

A gate marked `expect = "fail"` is an **expected failure**: the task's deliverable is a test
that fails, such as the reproduction of a reported bug. It needs a `fail_pattern`, and cannot also
be `new` ("fails now" and "failed before" are different claims). It passes only when the command
exits non-zero, other than 126 or 127, not by a timeout and not by a signal (a negative exit, or
129 to 192), with output that matches the pattern; it is recorded like every gate and never re-run as a regression gate of a later task. In
`check-gates`, one that names its task's (or an upstream task's) missing output `fails as
intended`; one that already fails with the pattern on the untouched tree is `pass` with the note
`the defect already reproduces through existing files`; one that passes is `pass` with the note `it
passes on the untouched tree; the task must make it fail`; any other failure is `error`.

**Files a gate executes are protected.** Each gate and `run` command is split into words, and
every word that names an existing tracked file (`tools/run_mutants.py`) joins that task's protected
set, unless the task lists that exact path in `writes`, in which case `validate` warns that the
task may edit a file its own verifier executes. Because the words are matched against the tree,
a gate that names a producer's own output (behind a glob in `writes`) protects it only once the
output exists; `replan` therefore leaves these derived entries out when it compares an accepted
task's definition with the revised one. This is a floor, not a fence: an author who
legitimately owns a build file (`CMakeLists.txt`) can still weaken the build. The defences for that
are a `new` gate with a `fail_pattern`, tests frozen by an earlier task, and the reviewers, who see
the build file in the diff.

### Literal requirements belong in command gates

Put cheap, directly checkable requirements in project-owned gates: a required direct include,
an output line's shape and units, and rejection of zero or invalid arguments. Gates run before
any paid review and again in the acceptance replay. A failing gate sends its diagnostic back to
the author without calling the panel. The runner learns no C++ or benchmark rules.

For example, copy [contract_gate.py](../examples/contract_gate.py) into the target project as
`tools/contract_gate.py` and adapt its literals to the brief. It checks a direct include, exactly
one `latency: NUMBER ns/op` line and rejects zero, malformed (`invalid`, `10oops`), negative and
unsigned-64-bit-overflow (`18446744073709551616`) arguments, with a process timeout. Its evidence
line explicitly says the fast path (argument `10`) was checked and the default path was not.
Adapt bounds to the program's numeric types and check multiplication bounds separately. A timeout
names the command and says completion and output were not verified:

```toml
gate = ["python3 tools/contract_gate.py --source tests/bench_ut.cpp --program build/bench"]
```

Place the build gate before this one. Keep hardware-dependent duration expectations separate
from deterministic interface checks. Passing the shape check says nothing about measurement
quality; that still needs tests and judgement.

### Panel shorthand

`reviewers` on a producer expands into one review task per entry. An entry is a persona name, or a
table with `perspective` and any of `advisory`, `agent`, `model`, `type`, `prompt`, `prompt_file`, `rules_file`,
`recheck_passed`, `timeout_min`, `budget_usd`, `fallback_agents`, `complexity`, `params`.

```
reviewers = ["principled-priya", { perspective = "dependable-diego", advisory = true }]
```

becomes

```
implement.review.principled-priya   type = code-review   reviews = implement
implement.review.dependable-diego               type = code-review   reviews = implement   advisory = true
```

The review type comes from the producer's type: each `produce` type names its `review_type`
(`design` names `design-review`, `implement` names `code-review`). `reviewers` on a producer whose
type names no `review_type` is an error, unless every entry gives its own `type`. A review task can
also be written out in full as its own `[[task]]`, with any id of the ordinary form, when it needs
more than the shorthand gives. 

### Validation

`validate` reports every problem at once, then prints the expanded DAG in execution order.

| Check | Example message |
|---|---|
| Unknown key, anywhere | `task 'design': unknown key 'ouputs'` |
| Missing or malformed `id`, duplicate `id` | |
| Unknown `type`, `perspective`, `agent`, or a missing parameter the type marks `required` | `task 'r1': persona 'sre' not found in library` |
| `prompt` and `prompt_file` together; `reviewers` with no `review_type`; `read_only` with `restores` | |
| Two personas with the same `code`, or the same perspective twice on one producer | `personas 'clause-by-clause-chen' and 'security' both use code 'SC'` |
| `root` is not a git repository | |
| A path in `outputs`, `writes` or `removes` that git ignores, or that is in both `outputs` and `removes` | `'implement' output build/config.h is ignored by .gitignore:1 'build/'` |
| A malformed pattern: absolute, containing `..`, or `**` not a whole segment | |
| `needs`, `reviews` or `verifies` names no task, or the wrong kind | `'reviews' must name a produce task` |
| A cycle | `dependency cycle: a -> b -> a` |
| **A producer with no verifier** | `task 'design' has no gate, check, review or human verifier` |
| A producer with no `outputs`, or an output not covered by its `writes` (see Path patterns) | |
| A path under `.git` or `.runs` in `outputs`, `writes` or `removes` | |
| **A cycle in the acceptance graph**: a verifier that `needs` its own target, or anything downstream of it | `'mutants' verifies 'implement' but needs 'report', which needs 'implement': implement can never be accepted` |
| **Overlapping `writes` between tasks with no order between them** | `'extend' and 'implement' both write src/book/** and neither depends on the other` |
| A type's `requires` names an unknown capability | |
| A check with no `run`, or an empty check or gate command | `task 'c': a check command is empty` |
| A pattern that does not compile | `... is not a valid pattern (a bad character class such as a reversed range?): <re error>` |
| An id, the `name` or a persona name longer than 64 characters; two ids that differ only by case | `task 'x': id differs from 'X' only by case; they would share a directory on a case-insensitive file system` |
| A `name` derived from the file name that is not a valid name | `'name' must match [A-Za-z0-9][A-Za-z0-9_-]*, got '<x>' (no 'name' is set, so it was taken from the file name; set 'name')` |
| `diff_cap_bytes`, `inputs_cap_bytes` or `findings_cap_bytes` below 1; `max_attempts` below 1 | `[defaults]: '<cap>' must be at least 1` |
| A bad `complexity`; a fallback agent that is undefined, repeats the primary, has no model (unless it is a `command` agent), widens permission controls or changes `review_mode` | `complexity must be mechanical, standard, or high` |
| A `fail_pattern` on a gate that is neither `new` nor `expect = "fail"`, or one that is not a valid regular expression | `'fail_pattern' only applies to a gate marked 'new = true' or 'expect = "fail"'` |
| An `expect` other than `"pass"` or `"fail"`; `expect = "fail"` without `fail_pattern`, or with `new = true` | `a gate with 'expect = "fail"' needs 'fail_pattern': a failure with no stated reason proves nothing` |
| `max_changed_files` or `max_changed_lines` below 1 or not an integer; either in `[defaults]` or on a review type | `task 'fix': 'max_changed_lines' must be at least 1` |
| `review_diff_from` naming an unknown task, itself, a task that is not a produce task, or one not among the task's needs (direct or transitive); on a task with no reviewers | `task 'final': 'review_diff_from' names 'x', which is not among the tasks it needs (directly or through other needs); the diff starts at that task's accepted commit, so add it to 'needs'` |
| **A producer whose whole panel is advisory and that has no other verifier** | `its whole panel is advisory and it has no other verifier; an advisory review cannot accept work` |
| A non-empty `params` on a `check` or `human` task | |
| Empty model names in `model_policy` | `model_policy.high: model names must not be empty` |
| `price_per_mtok` on a `claude` profile, with an unknown key, a rate below 0 or without `input` or `output`; `estimated_counts` without it | `[agents.codex]: price_per_mtok needs 'output'` |
| A `gate_cache` entry that is absolute, has a wildcard, `..` or `.git` | `[defaults]: 'gate_cache' entry '../x' must be a plain relative path under the root, without wildcards, '..' or .git` |
| `status_refresh_s` below 0; an empty `runs_dir` | `[defaults]: 'status_refresh_s' must be 0 (no refresh during a call) or more` |
| Warning: a later, dependent task's `writes` claim frozen outputs | `'extend' will modify outputs of accepted task 'implement': src/book/**. Accepted consumers of it: tests, report` |
| Warning: a task's `writes` include a file its own gate executes | |
| Warning: an agent is configured to bypass its sandbox or permissions | |
| Warning: a gate marked `new = true` has no `fail_pattern` | `a gate marked 'new = true' has no 'fail_pattern'; check-gates cannot confirm it fails for the intended reason` |
| Warning: a literal `outputs` or `writes` path is matched by a protected pattern, so it may change | `outputs path 'P' is named literally, which lets it change although protected pattern 'Q' matches it` |
| Warning: an agent of kind `command`, `codex` or `copilot` reports no dollar cost | `agent 'X' reports no dollar cost: dollar limits do not bind on it; ... set run_budget_tokens to cap its usage`; with a cap, a `command` agent adds `a command agent reports no usage, so each of its calls counts at its reservation, the rest of the cap` (BUD-12) |

A token stop line also holds unknown usage after quota or environment failures when work may
have begun. An unpriced fallback cannot launch past that line (BUD-23).

`validate` needs no agent and spends nothing, so it does **not** check capabilities. Whether each
profile is qualified for what its types `require` is checked by `doctor` and again by `start`,
which refuses to begin otherwise: `'code-review' needs 'read'; profile 'codex' is qualified for
'answer' only`.

## Type file

`library/types/<name>.toml`

```toml
name = "design"
kind = "produce"
description = "Write or revise a design document"
requires = ["read", "write"]         # capabilities the agent profile must be qualified for; see 05.
                                     # `resume` is never required: it is used when the profile has it
review_type = "design-review"        # which review type a `reviewers` panel uses
needs_run_dir = false                # true only for types that read the run record, such as summarize
agent = ""                           # optional defaults, below the task and above the workflow
model = ""
# complexity = "standard"            # optional, like the next three keys: "mechanical" | "standard" | "high"
# max_attempts = 3
# timeout_min = 30
# budget_usd = 5.0
# max_changed_files = 5              # the diff budget, produce types only; a task may override it
# max_changed_lines = 150
gate = []                            # default gates, e.g. a link checker
protected = []

prompt = """
...template text with placeholders...
"""

[params.audience]                    # parameters a task may set under `params`. Tables go last in TOML
default = "an engineer joining the project"
[params.component]
required = true                      # no default: a task that omits it does not load
```

### Placeholders

The same state always gives the same prompt. An unknown placeholder in a template is a validation
error. The rules of assembly:

- **One pass, over the template only.** A scanner finds `{name}` placeholders in the type's
  template and replaces each once. Substituted text is never scanned again, so a brief, a diff or a
  summary that contains the characters `{diff}` stays as it is. `str.format` is not used, so braces
  in C++, JSON or shell text are ordinary characters.
- **Data is fenced.** Every substituted value that comes from a task, an agent or the repository
  (`{task.prompt}`, `{inputs}`, `{diff}`, `{findings}`, `{target}`) is wrapped in a labelled block,
  and `{rules}` delegates task specification to the workflow brief, parameters and persona while
  treating repository content and agent replies as evidence. No block can override runner rules or
  allowed write paths. This does not make injection impossible; the ledger-derived verdict, the gates and the
  human sign-off are the defences that do not depend on a model's obedience.
- **Sizes are capped, and the overflow rule depends on what is lost.** `{diff}` over
  `diff_cap_bytes` is cut at a file boundary with a visible marker that names the omitted files and
  the full diff's path in the record. `{inputs}` over `inputs_cap_bytes` drops summaries, longest
  first, and keeps the ids and file lists. `{findings}` is never cut: over `findings_cap_bytes` the
  task is `blocked` for a person, because dropping a blocking finding would change the outcome. The
  result validator limits a `summary` to 4,000 characters.

| Placeholder | Filled with |
|---|---|
| `{task.id}`, `{task.title}`, `{task.prompt}` | From the task. In a review type, `{task.prompt}` is the review task's own `prompt` or `prompt_file`, shown under "# Instructions for this review", and empty when the task has none |
| `{param.NAME}` | The task's value, or the type's default |
| `{inputs}` | For each upstream task: id, type, title, summary, output files. In a review type whose template has no `{inputs}`, the same list appears inside `{target}`; either way it is shown once and capped by `inputs_cap_bytes` |
| `{outputs}` | The task's declared outputs |
| `{gates}` | The commands that will be run, an expected failure marked `(must FAIL, matching /…/)`; then, when the task has a diff budget, the line `Diff budget: at most 5 files and 150 lines changed from your base …` (in a review: from the task's base) |
| `{rules}` | The standing rules: do not commit; your report does not count; never weaken a check; answer `blocked` if it cannot be done properly; the protected and frozen paths |
| `{persona}` | The persona's rendered text (review types) |
| `{target}` | The producer under review: id, title, brief, outputs (review types) |
| `{diff}` | The diff of the work under review, capped (review types) |
| `{findings}` | On rework: the immediate cause, then the open blocking findings that still need a response, then advisory ones for information. In a later review round: the reviewer's own open findings (id, severity, title, detail, location, status and the author's latest response, without their history) |
| `{attempt}`, `{max_attempts}` | |
| `{result_schema}` | The JSON schema of the answer the task must end with |

## Persona file

`library/personas/<name>.toml`

```toml
name = "principled-priya"
code = "PE"                          # prefix of this persona's finding ids. Unique across the merged library
title = "Principled Priya: correctness, design and the trust boundary"
agent = ""                           # optional: give this perspective to another agent or model
model = ""
advisory = false                     # default for this persona

voice = ""                           # optional: how to phrase the summary and advisory findings; never scope, severity or verdict

focus = [ "...", "..." ]             # what this reviewer looks at
blocking = [ "...", "..." ]          # what justifies a blocking finding from this perspective
out_of_scope = [ "...", "..." ]      # what to leave to the other reviewers, each naming who; a reviewer who sees one with no owner on the panel raises it once, advisory
```

## Provider routing and complexity

Producer/reviewer tasks and inline reviewers accept `complexity = "mechanical"`,
`"standard"`, or `"high"`, and an ordered `fallback_agents = ["profile-name"]` list.
Complexity may also be set in `[defaults]` or a task type. Top-level
`[model_policy.<complexity>]` tables map `claude`, `codex`, `copilot` and `command` to model identifiers; an
empty name is an error.
See [provider routing](provider-routing.md) for precedence, controls, quota handling,
examples and recovery semantics. Existing workflows without routing settings retain
their configured primary model and profile.

## Templates and `runner new`

A **template** is a workflow file with a `[template]` table and three task keys of its own. `runner
new` renders it into an ordinary workflow file, which the owner reads, edits, validates and
commits. The engine never sees a template: what runs is the generated file.

`library/workflows/<name>.toml`, found like types and personas: the directories given with
`--library DIR` (repeatable, earlier wins), then the built-in library. A project shadows a shipped
template with a file of the same name.

```toml
name = "debug-{param.bug}"           # top-level keys come before the first table; without `name`, the output file's stem

[template]
description = "One line shown by `runner new --list`"

[template.params.bug]                # one table per parameter
required = true                      # or `default = …`; never both
type = "string"                      # "string" (default) | "integer" | "list"; inferred from `default`
description = "Short id used in paths"

[template.params.steps]
default = []                         # a list's elements may be strings, numbers or inline tables, nothing else

# ... then an ordinary workflow: [defaults], [agents.*], [model_policy.*], [[task]] ...
[[task]]
for_each = "steps"                   # one copy per element; none for an empty list
chain = true                         # copy k > 1 also needs copy k-1
id = "step-{each.n}"
title = "Step {each.n}: {each.value}"
note = "Written as a comment above each copy"
```

**Placeholders.** `{param.NAME}`; inside a `for_each` task `{each.n}` (1-based) and `{each.value}`;
anywhere, `{last.NAME}`, the id of the last copy made by the `for_each` over list `NAME`. Any other
brace text (`${HOME}`, `{a,b}`, C++) is literal. Substitution runs once over the template's string
values after it is parsed as TOML: a value is never scanned again, so a value holding `{param.x}`,
quotes or newlines comes out literally and the output is valid TOML.

- A string that is exactly one placeholder takes the value with its type:
  `max_changed_lines = "{param.fix_max_lines}"` gives an integer, `reviewers = "{param.panel}"` a list.
- In an array, an element that is exactly a list placeholder is spliced:
  `gate = ["{param.invariants}", "make test"]` with `invariants = []` gives `["make test"]`.
- A list anywhere else (inside a longer string) is an error; an integer there is written in decimal.

**Template errors.** Unknown keys in `[template]` or a parameter table; a parameter both required
and defaulted, or neither; a default of the wrong type; `root` or `library` set (`runner new` writes
them); an undeclared `{param.X}`; a parameter declared and never used; `{each.*}` outside a
`for_each` task; `{last.X}` where no `for_each` iterates `X`; `chain` without `for_each`.

```
runner new --list [--library DIR]...         the templates and their one-line descriptions
runner new --describe TEMPLATE [--library DIR]...
                                             its parameters: name, type, required or default, description
runner new TEMPLATE -o FILE [--params P.toml] [--set NAME=VALUE]... [--library DIR]... [--root DIR] [--force]
```

- `--params` is a TOML file of `NAME = value`; `--set` gives a string or an integer and wins over
  it. Lists come only from `--params`. A missing required parameter, an undeclared one, or a value
  of the wrong type is an error naming the parameter.
- The output is rendered to a temporary file in the output's directory (so relative paths such as
  `prompt_file` resolve as they will), loaded by the ordinary loader, and moved into place only if
  it loads with no error. Otherwise nothing is written, and an error about a task that was
  rendered from a `for_each` task, or whose id the template wrote with a placeholder, names the
  template task and copy it came from: `'step-2' output src/calc/** is ignored by .gitignore:1
  'src/calc/' (from template task 'step-{each.n}', copy 2)`; other errors are the loader's own.
  An existing output is refused without `--force`. The file gets the mode any new file gets.
  What `validate` only warns about is an error here when it would break a template's protection:
  a path named literally in `outputs` or `writes` that a `protected` pattern matches (`task 'fix':
  outputs path 'src/calc/test_regression.py' would lift the template's protection
  'src/calc/test_regression.py'; choose parameters that keep it apart`). A template's `protected`
  is a promise its parameters must not undo.
- `root` is written relative to the output: the git top level holding it, or `--root DIR`. Each
  `--library DIR` is written into the workflow's `library`, so the project's types and personas
  resolve as they did for the template. Every directory is resolved through symbolic links first,
  so `/tmp` and `/private/tmp` give `..`, not a path that climbs to the file-system root.
- The file begins with its provenance: the template's path relative to its library (never an
  absolute path), its SHA-256, the date and every parameter value. Changing the template later
  changes no generated file.
- A `--params` file that is not UTF-8 TOML, a `--set` value that is not UTF-8, and a list element
  that is not a string, number or table are errors naming the file or the parameter.

```toml
# Generated by `runner new implementation` from the built-in library/workflows/implementation.toml (sha256 82163a…)
# on 2026-10-03 with:
#   name = "book-module"
#   …
# Edit freely: this file, not the template, defines the work.
```

### The shipped templates

Each has a filled-in parameter file in [`examples/templates/`](../examples/templates/) and is
listed in [`library/README.md`](../library/README.md); `runner new NAME --describe` prints every
parameter. The panels follow one reviewer plan:
gates first, one blocking reviewer per question, the widest panel where a wrong acceptance costs
most. Each `*_reviewers` parameter (default `[]`) is **appended** to the stage's fixed panel; it is
how a project adds its language bench (`concerned-carlos` for C++) or a domain bench
(`packet-trace-pradip`, `memory-model-mei`), never a default. The rules every author and reviewer
must follow travel separately, once: the `rules_file` parameter (for example
`benches/cpp-standards.md` from `examples/templates/benches/`), written as `[defaults] rules_file`.

Every shipped template also takes `agent` (default `"claude"`), written as `[defaults] agent`: the
profile every agent task uses unless the generated file says otherwise. `--set agent=codex` (or
`copilot`, or a profile you then add under `[agents.NAME]`) switches the whole workflow.

| Template | Stages | Required parameters | Reviewer parameters and the fixed panels (blocking; *advisory*) |
|---|---|---|---|
| `implementation` | `design` → `tests` → `implement` → `check-*`, `mutants-*` → `report` → `report-check-*` → `signoff` | `name`, `brief`, `design_doc`, `src`, `tests`, `test_cmd`, `fail_pattern` | `design_reviewers`: PE, SC; *TPM*. `test_reviewers`: Mira; *SC*. `code_reviewers`: PE; *dependable-diego*. `final_reviewers`: protocol-petra |
| `debugging` | `reproduce` → `diagnose` → `fix` → `siblings` → `signoff` | `bug`, `report`, `symptom`, `area`, `regression_test`, `test_one`, `suite` | `reproduce_reviewers`: RC. `diagnose_reviewers`: RC; *PE*. `fix_reviewers`: RC, PE; *gatekeeper-gao*. `siblings_reviewers`: RC |
| `refactoring` | `characterise` → `net-*` → `step-1` → … → `step-N` → `final` → `final-equivalence`, `final-*` → `signoff` | `name`, `brief`, `invariant`, `area`, `char_tests`, `test_char`, `suite`, `steps` | `characterise_reviewers`: Mira; *PE*, *steadfast-stefan*. `step_reviewers` (every step): gatekeeper-gao; *PE*. `final_reviewers`: PE, steadfast-stefan; *picky-paola* |

(RC `root-cause-rosa`, PE `principled-priya`, SC `clause-by-clause-chen`, TPM
`timeline-tanaka`, Mira `meticulous-mira`.)

**Debugging.** `reproduce` writes `regression_test` and its notes; its gates are the `invariants`
and `{ run = test_one, expect = "fail", fail_pattern = symptom }`, so it is accepted only while the
test fails for the reported reason (the suite is not a gate there: it fails by design). `diagnose`
has `max_attempts = 4`: each attempt is one hypothesis. `fix` writes `area`, may not touch `tests`
(default `tests/**`) or `regression_test`, which is protected by name in `fix` and `siblings` so that
it stays as reproduced even when it lies inside `area` (`area = "src/**"` with the test under
`src/`): a frozen output that a glob in `writes` claims could otherwise be changed. It has the diff budget `fix_max_files` / `fix_max_lines` (default 5 / 150), and
gates `invariants`, `suite` and `{ run = test_one, new = true, fail_pattern = symptom }`: the same
frozen file must now pass. `test_one` must name `regression_test` literally, so `check-gates`
reports that gate as "fails as intended (waits for reproduce)". `siblings` claims the fix's area on
purpose (the expected warning), so the fix's gates re-run on its candidate when it changes the area;
its own gates run the suite either way.

**Refactoring.** `characterise` writes `char_tests` and the `baselines`; its gates (`invariants`,
`test_char`, `equivalence`) must pass on the unchanged code. Each `mutation` command becomes a
restoring check `net-N` that verifies it. Each element of `steps` becomes `step-N` (chained, type
`refactor-step`), writing `area` and `also_writes`, with `tests`, `char_tests` and `baselines`
protected (also in `final`), wherever they lie, the diff budget
`step_max_files` / `step_max_lines` (default 15 / 300) and the gates `invariants`, `suite`,
`test_char`, `equivalence`, all compared with the frozen baselines, never with the previous step.
`final` (type `summarize`, after the last step) writes `docs/refactor/<name>/final.md`, the
whole-refactoring report the final panel judges. A panel reviews the diff from its producer's base,
which for `final` is the tree after the last step, so `final` sets `review_diff_from = "characterise"`:
**its panel sees the cumulative diff from characterise's accepted commit to the candidate**, every
step's change and the report together, and the author does not paste git evidence. The report
stays: each step's commit and what it moved, the public surface at the characterise commit and at
HEAD, compared, and whether the goal is met. The read-only check `final-equivalence` re-runs `test_char` and
every `equivalence` command on the final tree, and each `final_checks` command is a further
read-only check `final-N` that verifies `final`.


## Standing rulings and optional review evidence

`[defaults] rulings_file` names a TOML file relative to the repository top (including when `root`
is a subdirectory). Absolute paths, glob characters and paths escaping through `..` or symlinks
are rejected, and so is a path with a symbolic link on it (the file or a directory, in the work
tree or on the commit read), since only the link's target would be protected. The file is owner
input, so an author must not be able to write it unseen: a path
git ignores, or one under `.git` or `.runs`, is refused, and the file joins every task's
`protected` set (naming it in a task's `writes` or `outputs` is an error). A run reads the copy
committed on its starting commit (`git cat-file`), never the work tree, and freezes the entries
in the expanded workflow with the blob id (`_standing_rulings_blob`) and the commit; a replan
reads the same commit. `start` refuses a file that is not committed or differs from its
committed copy: commit it, or remove `rulings_file`. A missing file is empty. A normal in-run
ruling still reaches later reviewers through the ledger.
Each `[[ruling]]` has string fields `task`, `finding_title`, `location_glob`, `decision`
(`"advisory"` or `"resolved"`), `note`, `ruled_at`, `by`, and optional `brief_sha256` (64 hex
digits), `brief_parts` (a table of the same digests per part: `prompt`, `rules`, `gates`,
`outputs`, `inputs`), `run`, `finding`, and the ruling's provenance `by_source` (`"flag"` or
`"env"`), `interactive` (a boolean) and `agent_markers` (a list of names). For example:

```toml
[[ruling]]
task = "parse"
finding_title = "The parser accepts an empty record"
location_glob = "src/parser.cpp:*"
decision = "advisory"
note = "Empty records are outside this task; the brief will say so"
ruled_at = "2026-10-08T12:00:00Z"
by = "owner"
```

The task must match. If the ledger already knows a location for that title, it must match the
glob; otherwise the reviewer receives the scoped entry and must apply its title and location.
An entry with a brief hash is dropped when the task's effective brief differs: its prompt, its
project rules, its gates, its declared outputs, or its upstream tasks and their declared outputs.
With `brief_parts` STATUS names the part, as in "the brief changed (project rules)". An entry
exported by an earlier runner hashed the prompt alone, and is dropped; rule again. Unhashed
entries remain until edited. Entries without provenance are shown as "provenance unknown".
`resolve --standing` queues the entry in the run's state, saved with the ruling, and never
touches the tree of the paused run; when the run is done the runner appends the queued entries
(with hashes, provenance and an exact location glob) to the file and says so, and the owner
commits it. Each entry goes to the file named when it was queued; a replan may not drop or move
`rulings_file` while entries wait, and an entry the file's reader would refuse is never queued.
For a run that never reaches `done`, `runner export-rulings RUN` writes the queued entries once
nothing in the run can touch the work tree again (every task terminal); commit the file then.
Broaden a glob deliberately in the file if the ruling covers a whole function or
file. Rulings are prompt context, never automatic verdict changes.

Review answers may add `"gateable": {"command_hint": "..."}` to a **blocking** finding and may
add `"reach_audit": [{"test": "...", "state_claimed": "...", "reach_evidence": "...", "oracle": "..."}]`
to the answer. The audit's shape
is checked and its UTF-8 JSON size is bounded by `findings_cap_bytes` (a larger one is cut to the
entries that fit); a hint on a finding not reported blocking is dropped. Both are repairs recorded
in `review-repair`, never a rejection (FND-48); no audit is required and its absence triggers no
repair. Hints are recorded as suggestions, never executed by the runner.
Use literal project requirements as gates when a deterministic command can check them cheaply;
keep measurement calibration and semantic test reach separate from syntax checks.
