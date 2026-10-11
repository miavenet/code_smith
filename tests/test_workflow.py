"""Workflow loading: scenarios WF-01 to WF-27 of docs/06-scenarios.md."""

import os
import unittest

from helpers import RepoCase
from codesmith import workflow

MINIMAL = """
[[task]]
id = "a"
type = "design"
outputs = ["docs/a.md"]
gate = ["true"]
"""

PERSONA = """
name = "{name}"
code = "{code}"
title = "T"
focus = ["f"]
blocking = ["b"]
out_of_scope = ["o"]
"""

TYPE = """
name = "{name}"
kind = "produce"
requires = ["write"]
{extra}
prompt = "Do {{task.prompt}} {body}"
{params}
"""


class Loading(RepoCase):
    def test_minimal_workflow_gets_defaults(self):
        """wf: minimal workflow gets defaults (WF-01)"""
        wf = self.load(MINIMAL)
        self.assertLoads(wf)
        t = wf.task("a")
        self.assertEqual(wf.name, "workflow")
        self.assertEqual(wf.root, self.root)
        self.assertEqual((t["agent"], t["model"], t["max_attempts"], t["timeout_min"]),
                         ("claude", "", 3, 30))
        self.assertEqual(t["budget_usd"], 5.0)
        self.assertEqual(t["title"], "a")
        self.assertEqual(t["writes"], ["docs/a.md"])
        self.assertEqual(t["outputs"], [{"path": "docs/a.md", "may_be_empty": False}])
        self.assertEqual(t["gates"], [{"run": "true", "new": False, "fail_pattern": ""}])
        self.assertEqual(wf.defaults["run_budget_usd"], 50.0)
        self.assertEqual(wf.defaults["max_parallel"], 4)
        self.assertEqual(wf.defaults["recheck_passed"], "diff")
        self.assertEqual(wf.defaults["branch"], "run")
        self.assertEqual(wf.defaults["diff_cap_bytes"], 200000)

    def test_unknown_keys_are_errors(self):
        """wf: unknown keys are errors (WF-02)"""
        self.write("lib/types/odd.toml", TYPE.format(name="odd", extra='colour = "red"', body="",
                                                     params=""))
        self.write("lib/personas/sre.toml", PERSONA.format(name="sre", code="SR") + 'mood = "x"\n')
        wf = self.load("""
library = ["lib"]
flavour = 1
[defaults]
max_atempts = 2
[agents.codex]
sandbx = "read-only"
[[task]]
id = "design"
type = "design"
ouputs = ["x"]
outputs = [{ path = "docs/a.md", maybe_empty = true }]
gate = [{ run = "true", nw = true }]
reviewers = [{ perspective = "sre", advisry = true }]
""")
        self.assertError(wf, "workflow.toml", "unknown key 'flavour'")
        self.assertError(wf, "[defaults]", "unknown key 'max_atempts'")
        self.assertError(wf, "[agents.codex]", "unknown key 'sandbx'")
        self.assertError(wf, "task 'design'", "unknown key 'ouputs'")
        self.assertError(wf, "task 'design'", "unknown key 'maybe_empty'")
        self.assertError(wf, "task 'design'", "unknown key 'nw'")
        self.assertError(wf, "task 'design'", "unknown key 'advisry'")
        self.assertError(wf, "odd.toml", "unknown key 'colour'")
        self.assertError(wf, "sre.toml", "unknown key 'mood'")

    def test_key_of_another_kind(self):
        wf = self.load(MINIMAL + """
[[task]]
id = "c"
type = "check"
run = ["true"]
outputs = ["x"]
""")
        self.assertError(wf, "task 'c'", "'outputs' does not apply to a check task")

    def test_all_errors_at_once(self):
        """wf: all errors at once (WF-03)"""
        wf = self.load("""
[[task]]
id = "a"
type = "design"
outputs = ["docs/a.md"]
needs = ["ghost"]
[[task]]
id = "b"
type = "nonsense"
[[task]]
id = "bad id"
type = "check"
""")
        self.assertError(wf, "task 'a'", "unknown task 'ghost'")
        self.assertError(wf, "task 'a' has no gate, check, review or human verifier")
        self.assertError(wf, "task 'b'", "type 'nonsense' not found")
        self.assertError(wf, "task 'bad id'", "malformed id")
        self.assertGreaterEqual(len(wf.errors), 4)

    def test_bad_references(self):
        """wf: bad references (WF-04)"""
        wf = self.load(MINIMAL + """
[[task]]
id = "c"
type = "check"
run = ["true"]
[[task]]
id = "r"
type = "design-review"
perspective = "principled-priya"
reviews = "c"
[[task]]
id = "v"
type = "check"
run = ["true"]
verifies = "ghost"
[[task]]
id = "h"
type = "human"
verifies = "c"
needs = ["nobody"]
""")
        self.assertError(wf, "task 'r'", "'reviews' must name a produce task", "'c' is a check")
        self.assertError(wf, "task 'v'", "'verifies' names unknown task 'ghost'")
        self.assertError(wf, "task 'h'", "'verifies' must name a produce task")
        self.assertError(wf, "task 'h'", "'needs' names unknown task 'nobody'")

    def test_ids(self):
        wf = self.load(MINIMAL + MINIMAL + """
[[task]]
id = "a.review.x"
type = "check"
run = ["true"]
""")
        self.assertError(wf, "task 'a'", "duplicate id")
        self.assertError(wf, "task 'a.review.x'", "malformed id", "dot is reserved")

    def test_ids_that_differ_only_by_case_collide(self):
        """wf: ids Impl and impl share a ref and a directory on a case-insensitive file system"""
        wf = self.load(MINIMAL.replace('"a"', '"Impl"') + MINIMAL.replace('"a"', '"impl"')
                       .replace("a.md", "b.md"))
        self.assertError(wf, "task 'impl'", "differs from 'Impl' only by case")

    def test_overlong_ids_and_names_are_errors(self):
        """wf: a 252-character id would die at start with 'File name too long'"""
        wf = self.load(MINIMAL.replace('"a"', '"' + "x" * 252 + '"'))
        self.assertError(wf, "id is longer than 64 characters")
        wf = self.load('name = "' + "n" * 65 + '"\n' + MINIMAL)
        self.assertError(wf, "'name' is longer than 64 characters")
        wf = self.load(MINIMAL.replace('"a"', '"' + "x" * 64 + '"'))
        self.assertLoads(wf)

    def test_derived_name_error_says_so(self):
        """wf: a name taken from `my.wf.v2.toml` is not blamed on the author"""
        wf = self.load(MINIMAL, rel="my.wf.v2.toml")
        self.assertError(wf, "'name' must match", "taken from the file name; set 'name'")
        wf = self.load('name = "bad name"\n' + MINIMAL)
        self.assertNotIn("taken from the file name", self.assertError(wf, "'name' must match"))

    def test_zero_caps_and_empty_check_commands_are_errors(self):
        """wf: caps of 0 or less and an empty check command are rejected"""
        wf = self.load("[defaults]\ndiff_cap_bytes = 0\ninputs_cap_bytes = -5\nfindings_cap_bytes = 0\n"
                       + MINIMAL + '[[task]]\nid = "c"\ntype = "check"\nrun = ["true", ""]\n')
        for key in ("diff_cap_bytes", "inputs_cap_bytes", "findings_cap_bytes"):
            self.assertError(wf, "[defaults]", key, "at least 1")
        self.assertError(wf, "task 'c'", "a check command is empty")

    def test_new_gate_without_fail_pattern_warns(self):
        """wf: a `new` gate with no fail_pattern accepts any failure, and validate says so"""
        wf = self.load(MINIMAL.replace('gate = ["true"]', 'gate = [{ run = "false", new = true }]'))
        self.assertLoads(wf)
        self.assertWarning(wf, "task 'a'", "'new = true' has no 'fail_pattern'")

    def test_literal_output_naming_a_protected_path_warns(self):
        """wf: a literal output or write bypasses `protected`; validate makes that visible"""
        wf = self.load(MINIMAL.replace('"docs/a.md"]', '".gitignore"]\nwrites = ["docs/a.md", "**"]')
                       .replace('outputs = ["docs/a.md", ', 'outputs = ['))
        self.assertWarning(wf, "task 'a'", "path '.gitignore' is named literally",
                           "protected pattern '**/.gitignore'")

    def test_negated_ignore_rule_does_not_reject(self):
        """wf: a `!` rule means not ignored; its output is fine"""
        self.write(".gitignore", "build/*\n!build/keep.h\n")
        self.commit()
        wf = self.load(MINIMAL.replace("docs/a.md", "build/keep.h"))
        self.assertLoads(wf)
        wf = self.load(MINIMAL.replace("docs/a.md", "build/other.h"))
        self.assertError(wf, "is ignored by .gitignore:1")

    def test_cycle(self):
        """wf: cycle (WF-05)"""
        wf = self.load("""
[[task]]
id = "a"
type = "design"
outputs = ["a.md"]
gate = ["true"]
needs = ["b"]
[[task]]
id = "b"
type = "design"
outputs = ["b.md"]
gate = ["true"]
needs = ["a"]
""")
        self.assertError(wf, "dependency cycle: a -> b -> a")

    def test_every_producer_needs_a_verifier(self):
        """wf: every producer needs a verifier (WF-06)"""
        body = '[[task]]\nid = "a"\ntype = "design"\noutputs = ["a.md"]\n'
        self.assertError(self.load(body), "task 'a' has no gate, check, review or human verifier")
        self.assertLoads(self.load(body + 'gate = ["true"]\n'))
        self.assertLoads(self.load(body + 'reviewers = ["principled-priya"]\n'))
        self.assertLoads(self.load(body + '[[task]]\nid = "c"\ntype = "check"\nrun = ["true"]\n'
                                   'verifies = "a"\n'))
        self.assertLoads(self.load(body + '[[task]]\nid = "h"\ntype = "human"\nverifies = "a"\n'))
        # a standalone human task that only needs it is not a verifier
        wf = self.load(body + '[[task]]\nid = "h"\ntype = "human"\nneeds = ["a"]\n')
        self.assertError(wf, "task 'a' has no gate")

    def test_advisory_panel_is_not_a_verifier(self):
        """wf: advisory panel is not a verifier (WF-07)"""
        body = '[[task]]\nid = "a"\ntype = "design"\noutputs = ["a.md"]\n'
        wf = self.load(body + 'reviewers = ["protocol-petra", '
                       '{ perspective = "dependable-diego", advisory = true }]\n')
        self.assertError(wf, "task 'a'", "whole panel is advisory")
        # the persona's default can be overridden by the entry
        self.assertLoads(self.load(body + 'reviewers = [{ perspective = "protocol-petra", '
                                   'advisory = false }]\n'))

    def test_panel_expansion(self):
        """wf: panel expansion (WF-08)"""
        wf = self.load("""
[[task]]
id = "implement"
type = "implement"
outputs = ["src/**"]
reviewers = ["principled-priya", { perspective = "dependable-diego", advisory = true, model = "small" }]
[[task]]
id = "d"
type = "design"
outputs = ["d.md"]
reviewers = ["clause-by-clause-chen"]
""")
        self.assertLoads(wf)
        pe = wf.task("implement.review.principled-priya")
        do = wf.task("implement.review.dependable-diego")
        self.assertEqual((pe["kind"], pe["type"], pe["reviews"], pe["advisory"], pe["generated"]),
                         ("review", "code-review", "implement", False, True))
        self.assertEqual((do["advisory"], do["model"], do["persona_code"]), (True, "small", "DO"))
        self.assertEqual(wf.task("d.review.clause-by-clause-chen")["type"], "design-review")
        self.assertEqual(wf.task("implement")["reviewers"],
                         ["implement.review.principled-priya", "implement.review.dependable-diego"])

    def test_precedence(self):
        """wf: precedence (WF-09)"""
        self.write("lib/personas/pa.toml", PERSONA.format(name="pa", code="PA") + 'model = "persona-m"\n')
        self.write("lib/personas/pb.toml", PERSONA.format(name="pb", code="PB"))
        self.write("lib/personas/pa2.toml", PERSONA.format(name="pa2", code="PZ") + 'model = "persona-m"\n')
        self.write("lib/types/t1.toml", TYPE.format(
            name="t1", extra='model = "type-m"\nmax_attempts = 7\nprotected = ["typep/**"]\n'
            'review_type = "rv"', body="", params=""))
        self.write("lib/types/t2.toml", TYPE.format(name="t2", extra='review_type = "rv"', body="",
                                                    params=""))
        self.write("lib/types/rv.toml", 'name = "rv"\nkind = "review"\nmodel = "rtype-m"\n'
                   'prompt = "{persona} {diff}"\n')
        wf = self.load("""
library = ["lib"]
[defaults]
model = "wf-m"
max_attempts = 5
protected = ["wfp/**"]
[[task]]
id = "task-level"
type = "t1"
model = "task-m"
max_attempts = 2
protected = []
outputs = ["a.md"]
reviewers = ["pa", "pb", { perspective = "pa2", model = "entry-m" }]
[[task]]
id = "type-level"
type = "t1"
outputs = ["b.md"]
gate = ["true"]
[[task]]
id = "wf-level"
type = "t2"
outputs = ["c.md"]
gate = ["true"]
""")
        self.assertLoads(wf)
        self.assertEqual(wf.task("task-level")["model"], "task-m")
        self.assertEqual(wf.task("task-level")["max_attempts"], 2)
        self.assertEqual(wf.task("type-level")["model"], "type-m")
        self.assertEqual(wf.task("type-level")["max_attempts"], 7)
        self.assertEqual(wf.task("wf-level")["model"], "wf-m")
        self.assertEqual(wf.task("wf-level")["max_attempts"], 5)
        self.assertEqual(wf.task("wf-level")["timeout_min"], 30)              # built-in
        self.assertEqual(wf.task("task-level.review.pa2")["model"], "entry-m")  # task over persona
        self.assertEqual(wf.task("task-level.review.pa")["model"], "persona-m")  # persona over type
        self.assertEqual(wf.task("task-level.review.pb")["model"], "rtype-m")   # type over workflow
        # `protected` is the exception: a union that `protected = []` cannot narrow
        prot = wf.task("task-level")["protected"]
        for p in ("wfp/**", "typep/**", "**/.gitignore", "**/.gitattributes", "**/.gitmodules"):
            self.assertIn(p, prot)

    def test_template_checks(self):
        """wf: template checks (WF-10)"""
        self.write("lib/types/bad.toml", TYPE.format(name="bad", extra="", body="{nonsense} {param.x}",
                                                     params=""))
        self.write("lib/types/mixed.toml", TYPE.format(name="mixed", extra="", body="{diff}", params=""))
        self.write("lib/types/caps.toml", TYPE.format(name="caps", extra="", body="", params="")
                   .replace('["write"]', '["write", "telepathy"]'))
        self.write("lib/types/needy.toml", TYPE.format(
            name="needy", extra="", body="{param.component} {param.audience}",
            params='[params.component]\nrequired = true\n[params.audience]\ndefault = "x"\n'))
        wf = self.load("""
library = ["lib"]
[[task]]
id = "n"
type = "needy"
outputs = ["a.md"]
gate = ["true"]
[[task]]
id = "m"
type = "needy"
outputs = ["b.md"]
gate = ["true"]
params = { component = "book", colour = "red" }
""")
        self.assertError(wf, "bad.toml", "unknown placeholder '{nonsense}'")
        self.assertError(wf, "bad.toml", "undeclared parameter '{param.x}'")
        self.assertError(wf, "mixed.toml", "'{diff}' is for review types only")
        self.assertError(wf, "caps.toml", "unknown capability 'telepathy'")
        self.assertError(wf, "task 'n'", "missing required parameter 'component'")
        self.assertError(wf, "task 'm'", "no parameter 'colour'")
        self.assertFalse([e for e in wf.errors if "task 'm'" in e and "component" in e])

    def test_reviewers_default_to_another_model_family(self):
        """wf: reviewers default to another model family than the author (WF-30): with a claude
        author, the panel is dealt across the declared codex profile and the built-in copilot one
        (in the inventory because [model_policy] names its model), each with the other families and then the author as fallbacks; an explicit agent on the
        entry or the persona wins; the author gets the other families as fallbacks; "any" turns
        the rule off"""
        base = """
[agents.codex-review]
kind = "codex"
model = "gpt-6-astra"
[model_policy.standard]
copilot = "gpt-5.4"
claude = "claude-opus-5"
[[task]]
id = "a"
type = "implement"
outputs = ["src/**"]
gate = ["true"]
reviewers = ["principled-priya", "clause-by-clause-chen", "dependable-diego",
             { perspective = "protocol-petra", agent = "claude" }]
"""
        wf = self.load(base)
        self.assertLoads(wf)
        t = {x["id"]: x for x in wf.tasks}
        self.assertEqual([(t[r]["agent"], t[r].get("fallback_agents")) for r in
                          ("a.review.principled-priya", "a.review.clause-by-clause-chen", "a.review.dependable-diego")],
                         [("codex-review", ["copilot", "claude"]), ("copilot", ["codex-review", "claude"]),
                          ("codex-review", ["copilot", "claude"])])
        self.assertEqual((t["a.review.protocol-petra"]["agent"], t["a.review.protocol-petra"].get("fallback_agents")),
                         ("claude", None))
        self.assertIn("reviewer_family", t["a.review.dependable-diego"]["agent_rule"])
        self.assertEqual(t["a"]["fallback_agents"], ["codex-review", "copilot"])
        wf = self.load("[defaults]\nreviewer_family = \"any\"\n" + base)
        self.assertLoads(wf)
        t = {x["id"]: x for x in wf.tasks}
        self.assertEqual(t["a.review.dependable-diego"]["agent"], "claude")
        self.assertNotIn("fallback_agents", t["a"])
        self.assertError(self.load("[defaults]\nreviewer_family = \"other\"\n" + MINIMAL),
                         "[defaults]", "'reviewer_family' must be")

    def test_builtin_library_is_clean(self):
        """Every shipped type uses only documented placeholders and declared parameters."""
        wf = self.load(MINIMAL)
        self.assertLoads(wf)
        self.assertEqual(sorted(wf.types), ["characterise", "code-review", "design", "design-review",
                                            "diagnose", "fix", "implement", "produce",
                                            "refactor-step", "report-review", "reproduce", "review",
                                            "specify", "summarize", "test"])
        self.assertEqual(len({p["code"] for p in wf.personas.values()}), len(wf.personas))

    def test_overlapping_claims_are_reported(self):
        """wf: overlapping claims are reported (WF-11)"""
        base = """
[[task]]
id = "implement"
type = "implement"
outputs = ["src/book/**"]
gate = ["true"]
[[task]]
id = "tests"
type = "test"
needs = ["implement"]
outputs = ["tests/**"]
gate = ["true"]
[[task]]
id = "extend"
type = "implement"
needs = ["implement"]
gate = ["true"]
"""
        wf = self.load(base + 'outputs = ["src/book/extra.cpp"]\n')
        self.assertLoads(wf)
        self.assertWarning(wf, "'extend' will modify outputs of accepted task 'implement'",
                           "src/book", "Accepted consumers of it: tests")
        self.assertEqual(wf.claims[0]["task"], "extend")
        self.assertEqual(wf.claims[0]["consumers"], ["tests"])
        # naming the path in `outputs` alone is not a claim: it must be covered by `writes`
        wf = self.load(base + 'outputs = ["src/book/extra.cpp"]\nwrites = ["src/other/**"]\n')
        self.assertError(wf, "task 'extend'", "output 'src/book/extra.cpp' is not covered")
        self.assertEqual(wf.claims, [])

    def test_deterministic_order(self):
        """wf: deterministic order (WF-12)"""
        text = """
[[task]]
id = "late"
type = "design"
needs = ["a"]
outputs = ["late.md"]
gate = ["true"]
[[task]]
id = "a"
type = "design"
outputs = ["a.md"]
reviewers = ["principled-priya", "clause-by-clause-chen"]
[[task]]
id = "c"
type = "check"
run = ["true"]
verifies = "a"
[[task]]
id = "free"
type = "check"
run = ["true"]
"""
        first = self.load(text)
        second = self.load(text)
        self.assertLoads(first)
        self.assertEqual(first.expanded(), second.expanded())
        # `late` needs `a` accepted, so a's whole panel and its check come before it
        self.assertEqual([t["id"] for t in first.tasks],
                         ["a", "a.review.principled-priya", "a.review.clause-by-clause-chen", "c", "late",
                          "free"])
        self.assertEqual([t["order"] for t in first.tasks], list(range(6)))

    def test_verifier_cannot_need_its_target(self):
        """wf: verifier cannot need its target (WF-14)"""
        for kind, extra in (("check", 'run = ["true"]\n'), ("human", "")):
            wf = self.load(f"""
[[task]]
id = "a"
type = "design"
outputs = ["a.md"]
[[task]]
id = "v"
type = "{kind}"
{extra}verifies = "a"
needs = ["a"]
""")
            self.assertError(wf, "acceptance cycle: a -> v -> a", "'v' verifies 'a' and also needs it",
                             "'a' can never be accepted")

    def test_indirect_acceptance_cycle(self):
        """wf: indirect acceptance cycle (WF-15)"""
        wf = self.load("""
[[task]]
id = "a"
type = "design"
outputs = ["a.md"]
[[task]]
id = "b"
type = "design"
outputs = ["b.md"]
gate = ["true"]
needs = ["a"]
[[task]]
id = "v"
type = "check"
run = ["true"]
verifies = "a"
needs = ["b"]
""")
        self.assertError(wf, "acceptance cycle: a -> b -> v -> a", "'a' can never be accepted")

    def test_standalone_human_is_valid(self):
        """wf: standalone human is valid (WF-16)"""
        wf = self.load(MINIMAL + '[[task]]\nid = "signoff"\ntype = "human"\nneeds = ["a"]\n')
        self.assertLoads(wf)
        self.assertIsNone(wf.task("signoff")["verifies"])

    def test_overlapping_writers_must_be_ordered(self):
        """wf: overlapping writers must be ordered (WF-17)"""
        two = """
[[task]]
id = "implement"
type = "implement"
outputs = ["src/book/**"]
gate = ["true"]
[[task]]
id = "extend"
type = "implement"
outputs = ["src/book/extra.cpp"]
gate = ["true"]
"""
        wf = self.load(two)
        self.assertError(wf, "'extend' and 'implement' both write", "src/book",
                         "neither depends on the other")
        self.assertLoads(self.load(two + 'needs = ["implement"]\n'))
        # ordered through a third task is still ordered
        self.assertLoads(self.load(two + 'needs = ["mid"]\n[[task]]\nid = "mid"\ntype = "check"\n'
                                   'run = ["true"]\nneeds = ["implement"]\n'))
        wf = self.load("""
[[task]]
id = "a"
type = "design"
outputs = [".git/hooks/pre-commit"]
writes = [".git/hooks/pre-commit", ".runs/x/state.json"]
removes = ["sub/.git/config"]
gate = ["true"]
""")
        self.assertError(wf, "task 'a'", "outputs path '.git/hooks/pre-commit'", ".git or .runs")
        self.assertError(wf, "task 'a'", "writes path '.runs/x/state.json'")
        self.assertError(wf, "task 'a'", "removes path 'sub/.git/config'")

    def test_bare_kinds_and_their_limits(self):
        """wf: bare kinds and their limits (WF-18)"""
        wf = self.load("""
[[task]]
id = "one-off"
type = "produce"
prompt = "Do it"
outputs = ["a.md"]
gate = ["true"]
[[task]]
id = "look"
type = "review"
perspective = "principled-priya"
reviews = "one-off"
""")
        self.assertLoads(wf)
        t = wf.task("one-off")
        self.assertEqual((t["kind"], t["type"], t["requires"]), ("produce", "produce", ["read", "write"]))
        self.assertIn("{result_schema}", wf.types["produce"]["prompt"])
        self.assertEqual(wf.task("look")["requires"], ["read"])

        wf = self.load("""
[[task]]
id = "one-off"
type = "produce"
outputs = ["a.md"]
reviewers = ["principled-priya"]
""")
        self.assertError(wf, "task 'one-off'", "names no 'review_type'", "principled-priya")
        self.assertLoads(self.load("""
[[task]]
id = "one-off"
type = "produce"
outputs = ["a.md"]
reviewers = [{ perspective = "principled-priya", type = "code-review" }]
"""))
        self.write("brief.md", "b\n")
        wf = self.load(MINIMAL + 'prompt = "x"\nprompt_file = "brief.md"\n')
        self.assertError(wf, "task 'a'", "'prompt' and 'prompt_file' are both set")

    def test_path_patterns_have_one_meaning(self):
        """wf: path patterns have one meaning (WF-19); the match table is in test_patterns"""
        wf = self.load("""
[defaults]
protected = ["/etc/**"]
[[task]]
id = "a"
type = "design"
outputs = ["docs/a**b.md"]
writes = ["docs/a**b.md", "../outside.md"]
gate = ["true"]
""")
        self.assertError(wf, "[defaults]", "'/etc/**'", "absolute")
        self.assertError(wf, "task 'a'", "'docs/a**b.md'", "'**' must be a whole segment")
        self.assertError(wf, "task 'a'", "'../outside.md'", "'..'")

    def test_cover_and_overlap_are_conservative(self):
        """wf: cover and overlap are conservative (WF-20)"""
        wf = self.load("""
[[task]]
id = "a"
type = "implement"
outputs = ["src/book/*.cpp"]
writes = ["src/*/*.cpp"]
gate = ["true"]
""")
        self.assertError(wf, "task 'a'", "output 'src/book/*.cpp' is not covered", "repeat the entry")
        wf = self.load("""
[[task]]
id = "headers"
type = "implement"
outputs = ["src/**/x.h"]
gate = ["true"]
[[task]]
id = "book"
type = "implement"
outputs = ["src/book/**"]
gate = ["true"]
""")
        self.assertError(wf, "'book' and 'headers' both write", "neither depends on the other")

    def test_unsatisfiable_paths(self):
        """wf: unsatisfiable paths (WF-21)"""
        self.write(".gitignore", "build/\n*.tmp\n")
        self.commit()
        wf = self.load("""
[[task]]
id = "implement"
type = "implement"
outputs = ["build/config.h", "src/**"]
writes = ["build/config.h", "src/**", "build/gen/**", "notes.tmp"]
removes = ["src/old.cpp"]
gate = ["true"]
[[task]]
id = "b"
type = "design"
outputs = ["docs/b.md"]
removes = ["docs/b.md"]
gate = ["true"]
""")
        self.assertError(wf, "'implement' output build/config.h is ignored by .gitignore:1 'build/'")
        self.assertError(wf, "'implement' writes path build/gen/** is ignored by .gitignore:1")
        self.assertError(wf, "'implement' writes path notes.tmp is ignored by .gitignore:2 '*.tmp'")
        self.assertError(wf, "task 'b'", "'docs/b.md' is in 'outputs'", "'removes'")
        self.assertFalse([e for e in wf.errors if "src/" in e])

    def test_root_must_be_a_git_repository(self):
        """git: required (GIT-06, the validate half)"""
        import shutil
        shutil.rmtree(os.path.join(self.root, ".git"))
        wf = self.load(MINIMAL)
        self.assertError(wf, "is not a git repository")

    def test_library_consistency(self):
        """wf: library consistency (WF-22)"""
        self.write("lib/personas/security.toml", PERSONA.format(name="security", code="SC"))
        self.write("lib/types/needy.toml", TYPE.format(name="needy", extra="", body="{param.component}",
                                                       params="[params.component]\nrequired = true\n"))
        wf = self.load("""
library = ["lib"]
[[task]]
id = "a"
type = "needy"
outputs = ["a.md"]
reviewers = ["principled-priya", { perspective = "principled-priya", advisory = true }]
[[task]]
id = "again"
type = "design-review"
perspective = "dependable-diego"
reviews = "a"
[[task]]
id = "and-again"
type = "design-review"
perspective = "dependable-diego"
reviews = "a"
""")
        self.assertError(wf, "personas 'clause-by-clause-chen' and 'security' both use code 'SC'")
        self.assertError(wf, "task 'a'", "perspective 'principled-priya' is listed twice")
        self.assertError(wf, "task 'a'", "perspective 'dependable-diego' reviews it twice")
        self.assertError(wf, "task 'a'", "missing required parameter 'component'")

    def test_a_workflow_library_can_shadow_a_builtin_file(self):
        self.write("lib/personas/dependable-diego.toml", PERSONA.format(name="dependable-diego", code="OPS"))
        wf = self.load('library = ["lib"]\n' + MINIMAL)
        self.assertLoads(wf)
        self.assertEqual(wf.personas["dependable-diego"]["code"], "OPS")

    def test_relative_paths(self):
        """wf: relative paths (WF-23)"""
        self.write("workflows/lib/personas/sre.toml", PERSONA.format(name="sre", code="SR"))
        self.write("workflows/briefs/x.md", "the brief\n")
        self.write("briefs/x.md", "the wrong brief: relative to root\n")
        wf = self.load("""
root = ".."
library = ["lib"]
[[task]]
id = "a"
type = "design"
prompt_file = "briefs/x.md"
outputs = ["docs/a.md"]
reviewers = ["sre"]
""", rel="workflows/wf.toml")
        self.assertLoads(wf)
        paths = wf.expanded()["paths"]
        self.assertEqual(paths["root"], self.root)
        self.assertEqual(paths["workflow_file"], os.path.join(self.root, "workflows", "wf.toml"))
        self.assertEqual(paths["library"][0], os.path.join(self.root, "workflows", "lib"))
        self.assertEqual(paths["library"][-1], workflow.BUILTIN_LIBRARY)
        self.assertEqual(wf.task("a")["prompt_file"], os.path.join(self.root, "workflows", "briefs", "x.md"))
        self.assertTrue(all(os.path.isabs(p) for p in [paths["root"], paths["workflow_file"]]
                            + paths["library"]))
        wf = self.load(MINIMAL + 'prompt_file = "nowhere.md"\n')
        self.assertError(wf, "task 'a'", "prompt_file 'nowhere.md' not found")
        wf = self.load('library = ["nolib"]\n' + MINIMAL)
        self.assertError(wf, "library directory 'nolib' not found")

    def test_validate_needs_no_agent(self):
        """wf: validate needs no agent (WF-24)"""
        self.write("lib/types/exotic.toml", TYPE.format(name="exotic", extra="", body="", params="")
                   .replace('["write"]', '["read", "write", "execute", "boundary"]'))
        old_path = os.environ.get("PATH", "")
        git_dir = os.path.dirname(__import__("shutil").which("git"))
        os.environ["PATH"] = git_dir if not os.path.exists(os.path.join(git_dir, "claude")) else old_path
        self.addCleanup(os.environ.__setitem__, "PATH", old_path)
        wf = self.load("""
library = ["lib"]
[agents.echo]
argv = ["cat"]
[[task]]
id = "a"
type = "exotic"
agent = "echo"
outputs = ["a.md"]
gate = ["true"]
""")
        self.assertLoads(wf)     # a `command` agent with no qualification at all still validates
        self.assertEqual(wf.task("a")["requires"], ["read", "write", "execute", "boundary"])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".runs")))
        wf = self.load(MINIMAL + 'agent = "gemini"\n')
        self.assertError(wf, "task 'a'", "agent 'gemini' is not defined")


class Protection(RepoCase):
    def test_files_a_gate_executes_are_protected(self):
        """frz: files a gate executes are protected (FRZ-11, the load half)"""
        self.write("tools/check.py", "print('ok')\n")
        self.write("tools/mutants.py", "print('ok')\n")
        self.write("CMakeLists.txt", "project(x)\n")
        self.commit()
        self.write("tools/untracked.py", "print('ok')\n")
        text = """
[[task]]
id = "implement"
type = "implement"
outputs = ["src/**"]
{writes}
gate = ["python3 ./tools/check.py --strict", "python3 tools/untracked.py", "cmake -S . -B build"]
[[task]]
id = "mutants"
type = "check"
verifies = "implement"
restores = true
run = ["python3 tools/mutants.py book"]
"""
        wf = self.load(text.format(writes=""))
        self.assertLoads(wf)
        prot = wf.task("implement")["protected"]
        self.assertIn("tools/check.py", prot)
        self.assertIn("tools/mutants.py", prot)         # its verifying check executes it
        self.assertNotIn("tools/untracked.py", prot)    # only existing tracked files
        self.assertNotIn("CMakeLists.txt", prot)        # not named by any command: a floor, not a fence
        self.assertIn("tools/mutants.py", wf.task("mutants")["protected"])

        wf = self.load(text.format(writes='writes = ["src/**", "tools/check.py"]'))
        self.assertLoads(wf)
        self.assertNotIn("tools/check.py", wf.task("implement")["protected"])
        self.assertWarning(wf, "'implement'", "'tools/check.py'", "may edit a file its own verifier executes")

    def test_copilot_kind(self):
        """wf: copilot is a built-in kind that reports no dollar cost"""
        wf = self.load('name="c"\n[defaults]\nagent="copilot"\nrun_budget_tokens=5000\n'
                       '[model_policy.high]\ncopilot="claude-opus-5"\n' + MINIMAL + 'complexity="high"\n')
        self.assertLoads(wf)
        self.assertEqual(wf.agents["copilot"], {"kind": "copilot"})
        self.assertEqual(wf.tasks[0]["provider_models"], {"copilot": "claude-opus-5"})
        self.assertWarning(wf, "'copilot' reports no dollar cost", "stops the run once 5000 tokens")
        wf = self.load('[agents.helper]\nkind="copilot"\nignore_user_config=true\n' + MINIMAL)
        self.assertError(wf, "[agents.helper]", "'ignore_user_config' is not supported for copilot")
        wf = self.load('[agents.helper]\nkind="copilot"\nextra_args=["--allow-all-paths"]\n' + MINIMAL)
        self.assertWarning(wf, "'helper'", "bypass its sandbox or permissions")
        wf = self.load('[agents.helper]\nkind="gh-copilot"\n' + MINIMAL)
        self.assertError(wf, "[agents.helper]", '"copilot"')

    def test_bypass_warning(self):
        wf = self.load('[agents.codex]\nsandbox = "danger-full-access"\n' + MINIMAL)
        self.assertLoads(wf)
        self.assertWarning(wf, "'codex'", "bypass its sandbox or permissions")
        wf = self.load('[agents.claude]\nextra_args = ["--dangerously-skip-permissions"]\n' + MINIMAL)
        self.assertWarning(wf, "'claude'", "bypass")
        wf = self.load('[agents.codex]\nsandbox = "workspace-write"\n' + MINIMAL)
        self.assertEqual(wf.warnings, [])

    def test_copilot_yolo_profile(self):
        """wf: permission_mode = "bypassPermissions" makes a copilot profile YOLO, as for claude"""
        wf = self.load('[agents.claude]\npermission_mode = "bypassPermissions"\n' + MINIMAL)
        self.assertWarning(wf, "'claude'", "bypass its sandbox or permissions")
        wf = self.load('[agents.helper]\nkind = "copilot"\npermission_mode = "bypassPermissions"\n' + MINIMAL)
        self.assertLoads(wf)
        self.assertWarning(wf, "'helper'", "bypass its sandbox or permissions")
        for flag in ("--allow-all", "--yolo", "--allow-all-tools", "--allow-all-urls"):
            wf = self.load(f'[agents.helper]\nkind = "copilot"\nextra_args = ["{flag}"]\n' + MINIMAL)
            self.assertWarning(wf, "'helper'", "bypass")
        # Copilot has no other permission modes: a Claude mode name there would silently do nothing.
        wf = self.load('[agents.helper]\nkind = "copilot"\npermission_mode = "acceptEdits"\n' + MINIMAL)
        self.assertError(wf, "[agents.helper]", "'permission_mode' on a copilot profile")
        wf = self.load('[agents.helper]\nkind = "copilot"\n' + MINIMAL)
        self.assertEqual(wf.warnings, [])


class SmallRules(RepoCase):
    def test_check_rules(self):
        wf = self.load(MINIMAL + """
[[task]]
id = "c"
type = "check"
[[task]]
id = "both"
type = "check"
run = ["true"]
read_only = true
restores = true
""")
        self.assertError(wf, "task 'c'", "a check needs 'run'")
        self.assertError(wf, "task 'both'", "'read_only' and 'restores' cannot both be true")

    def test_gate_tables(self):
        wf = self.load("""
[[task]]
id = "a"
type = "design"
outputs = ["a.md", { path = "pkg/__init__.py", may_be_empty = true }]
gate = ["true", { run = "ctest -R book", new = true, fail_pattern = "BOOK-" }]
""")
        self.assertLoads(wf)
        t = wf.task("a")
        self.assertEqual(t["gates"][1], {"run": "ctest -R book", "new": True, "fail_pattern": "BOOK-"})
        self.assertEqual(t["outputs"][1], {"path": "pkg/__init__.py", "may_be_empty": True})
        self.assertEqual(t["writes"], ["a.md", "pkg/__init__.py"])
        wf = self.load(MINIMAL.replace('["true"]', '[{ run = "x", new = true, fail_pattern = "(" }]'))
        self.assertError(wf, "task 'a'", "not a valid regular expression")

    def test_expected_failure_gate_and_diff_budget_keys(self):
        """wf: expected-failure gate and diff budget keys (WF-25)"""
        wf = self.load(MINIMAL.replace('["true"]', '[{ run = "t", expect = "fail", fail_pattern = "Boom" }]')
                       + 'max_changed_files = 5\nmax_changed_lines = 150\n')
        self.assertLoads(wf)
        t = wf.task("a")
        self.assertEqual(t["gates"], [{"run": "t", "new": False, "fail_pattern": "Boom", "expect": "fail"}])
        self.assertEqual((t["max_changed_files"], t["max_changed_lines"]), (5, 150))
        self.assertNotIn("max_changed_files", self.load(MINIMAL).task("a"))      # no budget: no key
        cases = [
            ('[{ run = "t", expect = "fail" }]', "'expect = \"fail\"' needs 'fail_pattern'"),
            ('[{ run = "t", expect = "fail", new = true, fail_pattern = "x" }]',
             "both 'new = true' and 'expect = \"fail\"'"),
            ('[{ run = "t", expect = "maybe" }]', "'expect' must be \"pass\" or \"fail\""),
            ('[{ run = "t", fail_pattern = "x" }]', "only applies to a gate marked 'new = true' or"),
        ]
        for gate, message in cases:
            with self.subTest(gate=gate):
                self.assertError(self.load(MINIMAL.replace('["true"]', gate)), "task 'a'", message)
        for extra, message in [("max_changed_lines = 0\n", "'max_changed_lines' must be at least 1"),
                               ("max_changed_files = -1\n", "'max_changed_files' must be at least 1"),
                               ('max_changed_files = "5"\n', "'max_changed_files' must be a integer")]:
            with self.subTest(extra=extra):
                self.assertError(self.load(MINIMAL + extra), "task 'a'", message)
        # Not in [defaults]: a budget describes one kind of work.
        self.assertError(self.load("[defaults]\nmax_changed_lines = 100\n" + MINIMAL),
                         "[defaults]", "unknown key 'max_changed_lines'")
        # From the type, unless the task sets its own; a check has none.
        lib = os.path.join(self.root, "lib")
        os.makedirs(os.path.join(lib, "types"))
        with open(os.path.join(lib, "types", "small.toml"), "w") as fh:
            fh.write(TYPE.format(name="small", extra="max_changed_lines = 40", body="", params=""))
        wf = self.load('library = ["lib"]\n' + MINIMAL.replace('"design"', '"small"')
                       + '[[task]]\nid = "b"\ntype = "small"\noutputs = ["b.md"]\nmax_changed_lines = 9\n'
                       + '[[task]]\nid = "c"\ntype = "check"\nrun = ["true"]\nmax_changed_lines = 9\n')
        self.assertEqual((wf.task("a")["max_changed_lines"], wf.task("b")["max_changed_lines"]), (40, 9))
        self.assertError(wf, "task 'c'", "key 'max_changed_lines' does not apply to a check task")
        with open(os.path.join(lib, "types", "small.toml"), "w") as fh:
            fh.write(TYPE.format(name="small", extra="max_changed_lines = 0", body="", params=""))
        self.assertError(self.load('library = ["lib"]\n' + MINIMAL.replace('"design"', '"small"')),
                         "small.toml", "'max_changed_lines' must be at least 1")

    def test_removes_must_be_writable(self):
        wf = self.load(MINIMAL + 'removes = ["old/**"]\n')
        self.assertError(wf, "task 'a'", "removes path 'old/**' is not covered")
        self.assertLoads(self.load(MINIMAL + 'writes = ["docs/a.md", "old/**"]\nremoves = ["old/**"]\n'))

    def test_recheck_passed_per_task(self):
        wf = self.load(MINIMAL + 'recheck_passed = "never"\nreviewers = ["principled-priya", '
                       '{ perspective = "dependable-diego", recheck_passed = "diff" }]\n')
        self.assertLoads(wf)
        self.assertEqual(wf.task("a.review.principled-priya")["recheck_passed"], "never")
        self.assertEqual(wf.task("a.review.dependable-diego")["recheck_passed"], "diff")

    def test_needs_run_dir_comes_from_the_type(self):
        wf = self.load(MINIMAL + '[[task]]\nid = "s"\ntype = "summarize"\nneeds = ["a"]\n'
                       'outputs = ["r.md"]\ngate = ["true"]\n')
        self.assertLoads(wf)
        self.assertTrue(wf.task("s")["needs_run_dir"])
        self.assertFalse(wf.task("a")["needs_run_dir"])

    def test_load_or_raise(self):
        path = self.write("w.toml", '[[task]]\nid = "a"\ntype = "design"\noutputs = ["a.md"]\n')
        with self.assertRaises(workflow.WorkflowError) as ctx:
            workflow.load_or_raise(path)
        self.assertTrue(ctx.exception.errors)

    def test_invalid_toml_and_missing_file(self):
        self.assertError(self.load("[[task]\n"), "not valid TOML")
        self.assertError(workflow.load(os.path.join(self.root, "absent.toml")), "cannot read")


if __name__ == "__main__":
    unittest.main()


class ReviewDiffFrom(RepoCase):
    CHAIN = """
[[task]]
id = "base"
type = "design"
outputs = ["docs/base.md"]
gate = ["true"]
[[task]]
id = "mid"
type = "design"
needs = ["base"]
outputs = ["docs/mid.md"]
gate = ["true"]
[[task]]
id = "side"
type = "design"
outputs = ["docs/side.md"]
gate = ["true"]
[[task]]
id = "look"
type = "check"
verifies = "base"
run = ["true"]
[[task]]
id = "final"
type = "design"
needs = ["mid", "look"]
outputs = ["docs/final.md"]
{key}
{panel}
"""

    def load_with(self, key, panel='reviewers = ["principled-priya"]'):
        return self.load(self.CHAIN.format(key=key, panel=panel))

    def test_review_diff_from_names_an_upstream_producer(self):
        """wf: review_diff_from must name a produce task among the task's needs (WF-27): a
        transitive need loads and is kept on the task; an unknown task, itself, a check, a
        producer not among its needs, a task with no reviewers or the key on a check is an error"""
        wf = self.load_with('review_diff_from = "base"')
        self.assertLoads(wf)
        self.assertEqual(wf.task("final")["review_diff_from"], "base")
        self.assertNotIn("review_diff_from", wf.task("mid"))        # absent unless given
        self.assertError(self.load_with('review_diff_from = "nope"'),
                         "task 'final'", "'review_diff_from' names unknown task 'nope'")
        self.assertError(self.load_with('review_diff_from = "final"'), "'review_diff_from' names itself")
        self.assertError(self.load_with('review_diff_from = "look"'),
                         "'review_diff_from' must name a produce task; 'look' is a check task")
        self.assertError(self.load_with('review_diff_from = "side"'),
                         "'review_diff_from' names 'side', which is not among the tasks it needs",
                         "add it to 'needs'")
        self.assertError(self.load_with('review_diff_from = "base"', panel='gate = ["true"]'),
                         "'review_diff_from' sets what its review panel sees, and the task has no reviewers")
        wf = self.load(MINIMAL + '[[task]]\nid = "c"\ntype = "check"\nverifies = "a"\nrun = ["true"]\n'
                       'review_diff_from = "a"\n')
        self.assertError(wf, "task 'c'", "key 'review_diff_from' does not apply to a check task")
