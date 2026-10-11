# Starter library

The runner's built-in library, used by the examples. These files are read by the runner; changing how a
type of work is briefed, or what a reviewer looks for, means editing a file here, not code. A
workflow can add its own directory of types and personas with `library = [...]`, and a file there
with the same name replaces the one here. `runner new --library DIR` looks up workflow templates
the same way.

| Types | Kind | |
|---|---|---|
| `design` | produce | A design document. Panels use `design-review` |
| `implement` | produce | Code and tests, from a brief and an accepted design. Panels use `code-review` |
| `test` | produce | Tests written from a specification, apart from the code |
| `summarize` | produce | A narrative summary written from the run record |
| `reproduce` | produce | One minimal test that fails with a reported symptom; no code change. Parameters `symptom`, `test_command` |
| `diagnose` | produce | Hypotheses tested by experiment, the root cause at `path:line`, the same pattern elsewhere. Panels use `design-review` |
| `fix` | produce | Change only what the accepted diagnosis names. Diff budget 5 files / 150 lines |
| `characterise` | produce | Tests and baselines pinning today's behaviour before a refactoring. Parameter `invariant` |
| `refactor-step` | produce | One step of a refactoring, no behaviour change. Parameters `step`, `invariant`; diff budget 15 files / 300 lines |
| `specify` | produce | A short behaviour specification of one type or function: three to six Given/When/Then scenarios, contract text quoted from the inputs, no implementation. Parameter `subject`. Panels use `design-review` |
| `design-review` | review | One perspective on a design |
| `report-review` | review | One perspective on a report about accepted work (`summarize` uses it): the report is judged against the record and the diff it accounts for, which may span from an earlier task's acceptance (`review_diff_from`) |
| `code-review` | review | One perspective on a code change. Always checks for weakened tests and out-of-brief changes; asks for entry, persistence until observation, expiry and cleanup in tests claiming a state, as Mira does |
| `produce`, `review` | produce, review | The bare kinds: a one-off task with no template of its own |

The review types show the review task's own `prompt` or `prompt_file` to each reviewer under
"# Instructions for this review", and the project's `rules_file` (for example
[`../examples/templates/benches/cpp-standards.md`](../examples/templates/benches/cpp-standards.md))
to every author and reviewer alike under "Project rules". All three hold every reviewer to one
calibration rule: a blocking finding needs checkable evidence and states how the author can
confirm it in minutes, a suspicion is advisory with the experiment named, and no reviewer calls
its own finding confirmed; and to one shape: a title that says what goes wrong and for whom, a
detail in the order what happens, evidence, fix, how to confirm, and a summary that opens with the
verdict's reason. A persona is rendered to the reviewer as prose with the panel's effective mode
(its file's `advisory` is only a default), and carries a `voice` for the summary and advisory
findings only: a manner of speaking fitted to the persona's responsibility, with humour where
that responsibility can bear it (one line, in the summary or an advisory finding) and none
where it cannot (a record, a plan, a race report, a security finding).

Each type lists the **capabilities** its agent must be qualified for (`requires`): authors need
`write`, reviewers need `read`. `doctor` establishes them per agent profile, and a workflow that
asks for more than a profile has is refused before any work starts.

`check` and `human` need no type file: a task names the kind directly.

| Personas | Code | Blocks by default |
|---|---|---|
| `principled-priya` | PE | yes |
| `clause-by-clause-chen` | SC | yes |
| `dependable-diego` | DO | yes |
| `protocol-petra` | PM | no, advisory; blocks on a requirement with no evidence of being met where the workflow seats it blocking (the report stage) |
| `timeline-tanaka` | TPM | no, advisory |
| `root-cause-rosa` | RC | yes: a cause without a discriminating experiment, a symptom patch, a reproduction of the wrong failure |
| `gatekeeper-gao` | SG | yes: a behaviour change or a change outside the stated step, a weakened test, an exported name or error the step changes without naming it |
| `steadfast-stefan` | AS | no, advisory; blocks on an unannounced incompatible change to the public surface, a format change with no migration path, and at a characterisation stage a public name or error nothing pins |

The eight above are the general bench; the last three serve debugging and refactoring. Sixteen
persona files ship in all: these, the C++ bench, the two domain benches and the security bench
below.

C++ review bench, for projects whose rules file states C++ rules (strong types, no `bool`,
`explicit`, `noexcept` policy, test conventions; `examples/templates/benches/cpp-standards.md` is
one). Any reviewer may block on a violation of a rule the file marks blocking; the persona
nearest the rule raises it, the others cite its finding. The bench is seated by stage, not as
a block: `api-aurelie` blocking on the design (the interface is frozen once the design is
accepted), `concerned-carlos` blocking on the implementation, `meticulous-mira` on the tests
(the implementation template seats her already), `picky-paola` advisory on a refactoring's
final panel. A persona with no question to answer on a piece of work is left out: every seat
is a call per round.

| Personas | Code | Looks at |
|---|---|---|
| `concerned-carlos` | CC | memory and exception safety, edge cases, undefined behaviour, races |
| `neckbeard-nate` | NN | standards, modern idioms, performance, minimalism. Seat him, advisory, when the brief names a hot path; a warnings-as-errors gate covers the rest more cheaply |
| `api-aurelie` | AA | public interfaces: swap test, strong types, leaked lifetimes, exposed state machines |
| `picky-paola` | PP | structure: responsibilities, decomposition, duplication, testability |
| `meticulous-mira` | MM | tests: coverage, property tests, assertion quality, isolation |

Domain benches bring knowledge a piece of work may need. Like the C++ bench they are never a
template default: a project passes them through a template's per-stage reviewer parameters.

| Personas | Code | Looks at |
|---|---|---|
| `packet-trace-pradip` | NE | the Linux network stack, drivers and kernel bypass, read from the packets up: the capture first, then the socket layer and `sk_buff` path, `tcp_input`/`tcp_output`, IGMP/MLD and `ip_mr`/PIM, NAPI, rings, RSS and offloads, XDP, DPDK/AF_XDP/Onload/VMA/ExaSock, tracepoints and counters. Blocks when a diagnosis or fix contradicts the capture, the kernel counters or the driver's receive path, rests on a capture taken at the wrong point, or breaks the data path's rules. It reads the decoded excerpts and counter dumps the author committed, never a raw pcap. A network project passes it as a blocking reviewer of every stage, the reproduction included when it replays a capture |
| `memory-model-mei` | ST | ASan, TSan, UBSan and MSan reports read literally (sanitizer, kind, each stack, verbatim lines) and the memory model behind them: memory orders and which pairs synchronise, torn reads, version-stamped snapshots and lock-free protocols, benign versus undefined races. Blocks when a diagnosis names a cause the report's stacks do not support, when a fix silences the tool (suppression, attribute, sleep, a test no longer run under it) instead of ordering the accesses, or when a reproduction does not run under the reporting sanitizer. Passed like the network bench: blocking on every stage, the reproduction first, since a reproduction of the wrong race is frozen once accepted |

Security bench, for work that reads input it does not control, keeps a secret or has callers
with different rights. Without it, `principled-priya` owns the trust boundary (who controls
each outside input, as the design or the brief declare it, and what checks it before a parser,
a shell, a path, a query or an allocation size; at design, an outside input with no stated
controller or bound blocks), `concerned-carlos` the bounds and lengths an outside value can reach,
`dependable-diego` what logs and command lines must not carry. A project that needs more seats the bench
through `design_reviewers` and `code_reviewers` (blocking) and appends
`examples/templates/benches/security.md` to its rules file.

| Personas | Code | Looks at |
|---|---|---|
| `sentinel-sato` | SEC | what an outsider controls and how far it reaches: the declared trust boundary, injection (shell, eval, deserialisers, format strings, queries, templates), paths and check-then-use, exhaustion from an attacker-chosen size or count, secrets at runtime, authorisation at the action, cryptographic primitives and randomness, dependencies and build steps that run code. Blocks on a source-to-sink path it can quote with the input that does harm, a credential at a line it can quote, a restricted operation with no authorisation check, and at design an outside input with no stated controller or bound. Says so in one sentence and passes where the work reads nothing from outside the process; hands memory safety to Carlos and the committed repository to dependable-diego |

Every persona lists what is **out of scope** for it and who covers that instead; the review
target's `panel` lists who else sits on the panel, and a reviewer who sees such a matter with
its owner absent raises it once, advisory. A project's
rules (strong types, test conventions) are in one `rules_file`, cited by the personas, never
restated in them, so a panel of five does not file the same violation five times; where two
reviewers still land on one line, the rework feedback says so and the author fixes once. A
workflow can make an advisory persona blocking, or the reverse, per task.

| Workflow templates (`workflows/`) | Stages | Default panels (blocking; *advisory*), each extended by its `*_reviewers` parameter | Example parameters |
|---|---|---|---|
| `implementation` | design → tests → implement → `check-*`, `mutants-*` → report → `report-check-*` → signoff | design: PE; *SC* (blocking when the brief cites a specification), *TPM*. tests: MM. implement: PE; *DO*. report: PM, over the diff since design | [`implementation.params.toml`](../examples/templates/implementation.params.toml) |
| `debugging` | reproduce (accepted while its test fails with the symptom) → diagnose (4 attempts, one hypothesis each) → fix (5 files / 150 lines; the same test passes) → siblings → signoff | reproduce: RC. diagnose: RC; *PE*. fix: RC, PE. siblings: RC, PE | [`debugging.params.toml`](../examples/templates/debugging.params.toml), [`debugging-network.params.toml`](../examples/templates/debugging-network.params.toml) (adds NE), [`debugging-sanitizer.params.toml`](../examples/templates/debugging-sanitizer.params.toml) (adds ST on every stage, CC on fix and siblings, and the C++ `rules_file`) |
| `refactoring` | characterise → `net-*` mutation checks → step-1 → … → step-N (chained; 15 files / 300 lines each) → final → `final-equivalence`, `final-*` → signoff | characterise: MM, AS; *PE*. each step: SG; *PE*. final: PE, AS | [`refactoring.params.toml`](../examples/templates/refactoring.params.toml) |

The bug reports and the refactoring brief the examples point at are in
[`../examples/templates/briefs/`](../examples/templates/briefs/); the implementation example's
design brief is [`../examples/briefs/book-design.md`](../examples/briefs/book-design.md). Each is
named relative to the generated workflow, so copy them into a `briefs/` directory beside it.

`runner new --list` lists them and `runner new --describe NAME` their parameters. Templates name the
general personas, plus `meticulous-mira` where the question (test coverage) is not tied to a
language; a project adds its bench (the C++ one, a domain one) through the template's
`*_reviewers` parameters, and its rules through `rules_file`. Each template also takes `agent`
(default `claude`), written as `[defaults] agent`, and a model per family (`claude_model`,
`codex_model`, `copilot_model`) written as `[model_policy]`, so every family is in the run's
inventory: the authors use `agent` with the other families as fallbacks, and the reviewers are
dealt across the other families (`reviewer_family`; `doctor` says which this box can run).

The format of the three file types is in [`../docs/03-workflow-file.md`](../docs/03-workflow-file.md).
