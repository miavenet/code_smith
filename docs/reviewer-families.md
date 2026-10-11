# Reviewer model families: the inventory and the assignment

## The suggestion

Add a stage early in every workflow that takes an inventory of the models available and assigns
them to the reviewer personas, perhaps a different one in the next round. Behind it is a guideline
the runner had not encoded: **reviewers come from a different model family than the author when
one is available.** Two live runs of a lock-free queue lab were reviewed by the author's own family (Claude
throughout) with Codex installed on the box, and nothing in the runner said so.

## Evaluation

**The inventory already exists; it is `doctor`.** Qualification probes every profile a workflow
names, per model, and records what each can do (`answer`, `read`, `write`, `execute`,
`boundary`, `resume`) in `qualification.json`. A stage that re-takes that inventory with a model
call would be slower, cost money and be less reliable than the probes, and its output would be an
agent's claim, which the runner never trusts for acceptance decisions. What is missing is not the
inventory but a *policy* over it.

**A workflow stage is the wrong layer for assignment.** Tasks are frozen definitions; a task that
rewrote other tasks' `agent` would be a replan performed by an agent, outside the integrity
manifest and the record's reasons. Provider routing already makes exactly this kind of decision
deterministically in the runner (`model_policy`, `fallback_agents`, quota fallback) and records
each selection with its reason in `provider_current` and the `provider-selection` event. The
assignment belongs there: a rule the loader applies when the workflow is read, and routing
honours or falls back from when qualification says a family is not usable on this host.

**Rotating the reviewer between rounds is adopted only in part.** Round 2 of a panel judges the
fix: the reviewer who raised a blocking finding must resolve it by id, and the author's response is
addressed to that reviewer's reading. Handing the finding to another model breaks that thread: the
new reviewer must resolve findings it never raised, or re-litigate them, and the calibration that
made round 1 blocking or advisory changes under it. So **a persona keeps its family across the
rounds of one panel**. Diversity comes from the panel's composition instead: when more than one
other family is qualified, the reviewers of a panel are spread across them in turn, so one panel
holds two families beside the author's. That is the version of "a different one in the next
round" that survives contact with the findings lifecycle.

**Scope of the rule.** `command` agents are scripted; a family rule between a scripted author and
scripted reviewers means nothing and must not disturb the scripted tests. The rule applies among
the model kinds (`claude`, `codex`, `copilot`), and a `command` profile is never chosen for a
reviewer by the rule.

## What ships

`[defaults] reviewer_family = "different"` (the default) or `"any"`.

Under `"different"`, every review task that names no `agent` of its own (not on the entry, not on
its persona, not on its type) gets:

- `agent`: a profile of a model kind other than the producer's effective profile's kind, taken
  from `[agents]` in file order and dealt round-robin across the producer's reviewers, so a panel
  spreads over the other families when there are several;
- `fallback_agents`: the remaining other-family profiles, then the producer's own profile, after
  any fallbacks the entry already gives.

Qualification probes every name in that list, as it does today. If no other-family profile is
qualified on the host (Codex in a container that cannot start its sandbox, say), routing moves to
the author's family and records why ("previous provider unavailable or unqualified"), so the run
proceeds and STATUS says what happened. `validate` prints the assignment in the DAG (`agent
codex`) and warns when the rule is on, the author is a model kind, and the workflow defines no
profile of another kind: `reviewers share the author's model family; add an [agents.NAME] of
another kind to honour reviewer_family = "different"`.

Under `"any"` nothing changes: a reviewer's agent is the task's, the persona's, the type's or
`[defaults] agent`, as before.

The templates set nothing: the default is `"different"`, and the generated file carries the
`[agents.NAME]` tables the owner adds. The parameter files show the pattern: a `[agents.codex]`
profile beside the Claude one.

Explicit `agent` on a reviewer entry, a persona or a type still wins; the rule only chooses the
default.

## What does not ship, and why

- A persona bound to a family ("Carlos is always Opus"): a persona file may already carry `agent`
  and `model`; that is the explicit override, and the library ships none, because the library
  cannot know what the host has.
- Alternating the family per round: see above.
- Choosing by benchmark or price: the runner has no evidence of either; `model_policy` by
  complexity remains the owner's statement of preference.
