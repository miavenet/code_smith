# Provider routing and task complexity

The runner selects a qualified agent profile for each producer or reviewer. An explicit
`fallback_agents` list authorizes automatic switching when the current provider reports
quota exhaustion, or fails twice in a row on the same call for a transient reason (capacity,
overload, a dropped connection, or, for a review, a time-out). A provider failure never counts
as a producer attempt or as a reviewer's answer: when a call's three tries all fail at the
provider, the run stops (status `failed`, exit 2) with the candidate and the other reviewers'
answers kept, and `resume` makes only that call again. This layer sits above the native adapters; task contracts, reviews,
acceptance, budgets and checkpoints remain owned by the runner.

## Complexity levels

| Level | Use for | Typical model choice |
|---|---|---|
| `mechanical` | Clear transformations, repetitive edits, straightforward checks with precise acceptance criteria | Claude Sonnet; a configured economical Codex model |
| `standard` | Bounded implementation or review with established design | A configured balanced model, such as Codex Sol |
| `high` | Architecture, ambiguous requirements, difficult debugging, concurrency or consequential review | Claude Fable/Opus; Codex Astra/Sol |

These levels describe the task's reasoning demands, not estimated token counts or
permission levels. A short task can be `high`. The runner does not guess complexity from
prompt length or escalate models in response to an objection. The workflow author assigns
it on a task, inline reviewer, task type, or `[defaults]`; omission means `standard`.
Models are configurable strings, not a hardcoded ranking or availability claim.

```toml
[defaults]
agent = "codex-writer"
complexity = "standard"

[model_policy.mechanical]
claude = "sonnet"
codex = "gpt-5.6-luna"

[model_policy.standard]
claude = "sonnet"
codex = "gpt-5.6-sol"

[model_policy.high]
claude = "opus" # replace with the exact Fable model available in your environment if desired
codex = "gpt-6-astra" # or gpt-5.6-sol
# command = "..."     # a `command` agent profile may be mapped too; an empty model name is an error

[agents.codex-writer]
kind = "codex"
sandbox = "workspace-write"

[agents.claude-writer]
kind = "claude"
permission_mode = "auto"

[[task]]
id = "implement-parser"
type = "implement"
prompt = "Implement the reviewed parser contract and run its tests."
complexity = "high"
fallback_agents = ["claude-writer"]
outputs = ["src/parser.cpp"]
gate = ["./test-parser"]
```

This is a configuration fragment: supply the real outputs, gates and contracts for the
project. `doctor` verifies actual access to each selected model before dispatch.
Primary model precedence is task/persona/type `model`, complexity mapping, workflow
default `model`, then profile `model`. A fallback uses its own provider's complexity
mapping, then its profile model. The primary provider's model identifier is never passed
to another provider. Every native fallback must resolve an explicit model.

A reviewer can set `complexity` and `fallback_agents` in its inline reviewer table or its
own task. Reviewers keep their identity and open findings when changing providers; the
fallback adapter receives the same read-only controls and evidence mode. Profiles are
qualified separately for model and review/write mode. An explicit sandbox or permission
bypass cannot be introduced by fallback from a profile without one. `review_mode` must
match. Use separate writer/reviewer profiles when their controls differ.

## Quota handoff

1. Native provider error events or terminal error results identify quota exhaustion.
   Successful prose, tool output, failed tests, malformed responses, timeouts and review
   objections do not trigger a quota switch. For Claude, an `api_error_status` of 429, or
   text such as `rate_limit_error` or "Claude AI usage limit reached", is quota; a 5xx or 529
   status, `api_error` or "internal server error" is a transient provider failure instead (see
   the first paragraph), and a turn or budget limit of the call itself is a task failure.
2. The coordinator records the quota outcome, settles its cost/usage, retains saved work,
   and selects the next available qualified profile in the task's configured order.
3. The replacement starts a fresh session with the full task, current findings and the
   runner's notice that work of this task is already in the work tree, with the changed paths
   and the fact that the provider changed (the notice every fresh call over a changed tree
   gets; see [concepts](02-concepts.md)). The notice lasts for that attempt only and names no
   place in the run record. It never receives the
   previous provider's session identifier. The runner does not replay external commands;
   agents are told not to repeat external effects whose outcome is uncertain.
4. A quota call consumes neither a task attempt nor a protocol repair allowance. It still
   contributes to recorded time and any cost it reports; a priced call refused for quota holds nothing
   against the dollar limit only on positive evidence that it ended before any work, with no usage
   (`before_work`, BUD-15); any other keeps its reservation as unsettled (see 05, Honest accounting). The next provider must fit the remaining
   run budget; changing providers does not reset any counters or acceptance history.
5. If no qualified available fallback exists, execution stops visibly with the current
   work retained. `resume` can continue after the cooldown; a replan can change profiles.

Selections are sticky: a task moves forward through its fallback list, preventing cycles.
Observed quota exhaustion applies a five-minute cooldown across profiles of that native
provider within this run. This is a bounded retry delay, **not a claim about the account's
reset time**. No background timer resumes a stopped run. Other tasks can select the
provider again after the cooldown; the current task retains its selected fallback.
A provider unavailable during qualification can be skipped if an authorized fallback
qualifies. Failed qualifications remain visible in the qualification report.

This implementation reacts to confirmed quota errors. It does not infer near-limit status
from token totals or consume the illustrative `~/.codex/sessions/latest.json` path. No
stable native near-limit signal has been established in this environment, so headroom
remains unknown until such telemetry is integrated. Automatic context sizing and dynamic
complexity classification are also outside this policy.

## GitHub Copilot CLI profiles

A `copilot` profile routes like any other: `[model_policy.*]` takes a `copilot` key, its quota
scope is the kind (a quota or rate-limit `session.error` from one Copilot profile cools down every
Copilot profile for five minutes), and it reports no dollar cost, so `run_budget_tokens` is its
stop line. Model names are the CLI's own (`copilot help config` lists them, such as
`claude-sonnet-5` or `gpt-5.4`).

```toml
[model_policy.standard]
copilot = "gpt-5.4"

[agents.copilot-writer]
kind = "copilot"
model = "claude-sonnet-5"
```

## The Codex sandbox on a container host

Codex runs the model's commands inside a Linux sandbox. Since CLI 0.155 that sandbox is
`bwrap`, which needs unprivileged user namespaces; a Docker or linuxkit container usually
does not allow them, and every call fails at once with `bwrap: No permissions to create a
new namespace`. The older Landlock sandbox still works there but is behind a feature the CLI
marks **deprecated**:

```toml
[agents.astra]
kind = "codex"
model = "gpt-6-astra"
sandbox = "read-only"
ignore_user_config = true
extra_args = ["--enable", "use_legacy_landlock", "-c", 'model_reasoning_effort="high"']
```

`doctor` checks the sandbox for free before it spends a probe (`codex sandbox -- true`) and
reports a profile whose enabled feature the installed CLI marks deprecated or removed as a
`note:` under that profile (PRE-09). The container fix that was verified: `docker run
--security-opt seccomp=unconfined --security-opt systempaths=unconfined` (the first lets
`bwrap` create its user namespace, the second unmasks `/proc` so it can mount a new one); no
`--privileged` is needed, and `codex exec -s read-only` then runs inside the container. A
bind-mounted repository may also need `git config --global --add safe.directory '*'` in the
container's HOME, or a fresh dependency clone fails with "dubious ownership". Without the fix,
the choices are the deprecated Landlock feature above or giving the reviewer role to another
family. `sandbox = "danger-full-access"` needs no sandbox but is never used for a reviewer: the
runner passes `read-only` for every review call.

## Reviewer families

`[defaults] reviewer_family = "different"` (the default; `"any"` turns it off) is the rule that
reviewers come from a model family other than the author's, and that the whole inventory,
`claude`, `codex` and `copilot`, is in play ([design](reviewer-families.md)). The loader
applies it to every review task that names no `agent` of its own: the reviewers of a panel are
dealt round-robin across the *runnable* profiles of the other kinds (the owner's declared
`[agents.NAME]` tables first, then a built-in kind whose model `[model_policy]` names), each with
the remaining families and then the author's profile as `fallback_agents`; an author with no
fallbacks of its own gets the other families as fallbacks. `doctor` probes every name in those
lists, so it is the inventory of what this box can run; routing moves past an unqualified or
exhausted family and records why (`provider-selection`, STATUS "Providers"). `validate` prints
the assignment (`agent codex, then copilot, claude [by reviewer_family]`) and warns when the
author is a model kind and no runnable profile of another kind exists. The templates name a model
for all three kinds (`claude_model`, `codex_model`, `copilot_model`), so a box without Copilot
simply fails its probe and the reviewers it was dealt fall back to the next family. A persona or
a reviewer entry with an explicit `agent` is left alone; a family is kept for all rounds of one
panel, because round 2 resolves round 1's findings by id.

## Visibility and recovery

`runner status` shows each selected profile/model, complexity and selection reason.
`state.json` retains provider selection history, qualifications, quota observations and
pending producer quota transitions. `events.jsonl` records `provider-quota` and
`provider-selection`; each invocation retains its actual argv, model, output and usage.
Native hooks and agent milestone checkpoints continue through the existing adapters.

If the coordinator crashes after recording a quota result or a selection, `resume` uses
that durable transition instead of calling the exhausted provider again. Existing orphan
reconciliation still runs first. Do not edit frozen workflow/run records directly; use
`replan` to change model mappings or profiles. Those changes participate in task definition
comparison, including fallback profile contents.

## Verification scenarios

The deterministic tests in `tests/test_providers.py` exercise partial-work retention,
exhausted fallback lists, no provider cycling on protocol errors, read-only review handoff,
open-finding preservation, complexity model qualification/dispatch, explicit overrides,
invalid/widened controls, unqualified fallbacks, budget stops, error-channel classification,
and coordinator crashes on both sides of selection. They use scratch repositories and
scripted subprocesses, with no paid model calls.
