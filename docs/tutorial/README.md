# code_smith tutorial

A guided tour of code_smith: what it is for, how a task moves through it, why it is built the
way it is, and how to write a workflow. It is written for someone who has not read the design
documents. Those remain the reference; this tutorial explains them and links to them.

## Chapters

| # | Chapter | You learn | Time |
|--:|---|---|--:|
| 1 | [The idea](01-the-idea.md) | What problem this solves, and the five rules everything follows from | 10 min |
| 2 | [A task, end to end](02-a-task-end-to-end.md) | The life of one producer: attempt, checks, the clean-checkout replay, review, commit | 30 min |
| 3 | [Review panels and findings](03-review-panels-and-findings.md) | How several reviewers converge instead of looping forever, and how a person rules | 30 min |
| 4 | [Safety: git, the record and recovery](04-safety-git-and-recovery.md) | Why a crash, a bad agent or a bad gate cannot corrupt your repository | 30 min |
| 5 | [Architecture](05-architecture.md) | The modules, who decides what, the table of moves, how agents are driven | 20 min |
| 6 | [Writing a workflow](06-writing-a-workflow.md) | The file format by example, and the mistakes `validate` catches | 30 min |
| | [Runbook](../runbook.md) | Operating a run: every command, stops, recovery | reference |

Chapters 1 to 3 are enough to understand what a run does. Chapter 4 is for anyone who needs to
trust it. Chapter 5 is for anyone who will change it. Chapter 6 and the runbook are for daily use:
chapter 6 to write a workflow, the runbook for the commands you type while one runs (`status`,
`pause`, `resume`, `approve`, `resolve`, `retry`, `prune`).

Every chapter is built to be done in one sitting and resumed at any heading. Each ends with
**Check yourself**: a few questions to answer *before* you open the hidden answers. Answering from
memory, even wrongly, is what makes it stick; rereading is not. Chapters 2, 3, 4 and 6 also have a
**Try it** step that runs in a few minutes with no model and no API key.

## How to read the diagrams

Diagrams are [Mermaid](https://mermaid.js.org/); GitHub renders them in place. Shape and colour
mean the same thing in every chapter. A box with no colour is a plain step.

```mermaid
flowchart LR
    subgraph kinds["The four kinds of task"]
        direction LR
        P["produce"]
        R(["review"])
        C{{"check"}}
        H[/"human"/]
    end
    subgraph outcomes["What happened to the work"]
        direction LR
        OK["accepted"]
        RW["sent back"]
        ST["stopped"]
        SA["set aside"]
    end
    kinds ~~~ outcomes

    classDef produce fill:#2f4b7c,stroke:#1d3157,color:#ffffff
    classDef review fill:#6b4c9a,stroke:#4a3370,color:#ffffff
    classDef check fill:#0f6b6b,stroke:#094848,color:#ffffff
    classDef human fill:#a23b72,stroke:#742951,color:#ffffff
    classDef ok fill:#2d6a4f,stroke:#1b4332,color:#ffffff
    classDef rework fill:#8a6100,stroke:#5e4200,color:#ffffff
    classDef stop fill:#a32d2d,stroke:#741f1f,color:#ffffff
    classDef aside fill:#64748b,stroke:#475569,color:#ffffff
    class P produce
    class R review
    class C check
    class H human
    class OK ok
    class RW rework
    class ST stop
    class SA aside
```

| Colour | Used for |
|---|---|
| Blue, violet, teal, rose | A task of kind `produce`, `review`, `check`, `human`. Rose also marks any point where a person decides |
| Green | Work that was accepted, or an action that is allowed |
| Ochre | Work sent back to its author, or a finding that still blocks |
| Red | The run stops, or the task ends as `failed` or `blocked` |
| Grey | Closed with nothing more to do: `skipped`, `noted`, `superseded` |

New diagrams should copy their `classDef` lines from the one above.

## Words used throughout

| Word | Meaning |
|---|---|
| **workflow** | A TOML file: tasks joined by `needs` into a graph with no cycles |
| **run** | One execution of a workflow. It has a UUID, a directory and, by default, its own git branch (`branch = "current"` commits on yours) |
| **producer** | A task that changes files. Everything else checks producers |
| **verifier** | A gate, a `check` task, a `review` task or a `human` task |
| **candidate** | What a producer's attempt left in the work tree, identified by a git tree id |
| **accepted** | Every verifier passed on one unchanged candidate. It is committed and frozen |
| **finding** | One piece of review feedback, tracked by id until it is settled |
| **the record** | The run directory: everything that happened, kept in a fixed layout |
