"""Workflow templates and `runner new`: scenarios TPL-01 to TPL-13, TPL-15 to TPL-19, TPL-22
to TPL-28, PRM-19 and PRM-20 of docs/06-scenarios.md, with a stub template, and the shipped
templates. The scripted runs (ACC-29, TPL-14, WF-32) are in test_template_runs.py."""

import datetime
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

from helpers import RUNNER, RepoCase, run_cli
from codesmith import cli, gitops, prompts, templates, workflow

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.normpath(os.path.join(HERE, "..", "examples"))

# A small debugging shape over the shipped types: every template feature, and no runner feature
# the loader may not have.
STUB = """
name = "debug-{param.bug}"

[template]
description = "Stub: reproduce, chained steps, final checks, sign-off"

[template.params.bug]
required = true
description = "Short id used in paths"
[template.params.area]
required = true
[template.params.steps]
type = "list"
default = ["only step"]
[template.params.invariants]
default = []
[template.params.final_checks]
default = []
[template.params.fix_max_lines]
default = 150

[[task]]
id = "reproduce"
type = "test"
title = "Reproduce {param.bug}"
outputs = ["tests/regression/{param.bug}.py"]
gate = ["{param.invariants}", "true"]

[[task]]
for_each = "steps"
chain = true
id = "step-{each.n}"
type = "implement"
title = "Step {each.n}: {each.value}"
needs = ["reproduce"]
note = "Step {each.n} of {param.bug}."
outputs = ["{param.area}"]
gate = ["true"]
max_changed_lines = "{param.fix_max_lines}"

[[task]]
for_each = "final_checks"
id = "final-{each.n}"
type = "check"
verifies = "{last.steps}"
run = ["{each.value}"]

[[task]]
id = "signoff"
type = "human"
needs = ["{last.steps}"]
"""


class Templates(RepoCase):
    def setUp(self):
        super().setUp()
        self.stub = self.write("lib/workflows/debugging.toml", STUB)
        os.makedirs(os.path.join(self.root, "w"))
        self.commit()

    def new(self, *args):
        return run_cli("new", *args, cwd=self.root)

    def params(self, text):
        return self.write("p.toml", text)

    def written(self):
        return sorted(os.listdir(os.path.join(self.root, "w")))

    def stub_template(self, text=STUB):
        return templates.load(self.write("lib/workflows/stub.toml", text))

    def test_renders_to_a_valid_workflow(self):
        """tpl: debugging renders to a valid workflow (TPL-01)"""
        res = self.new("debugging", "-o", "w/debug-17.toml", "--library", "lib",
                       "--set", "bug=div-zero-17", "--set", "area=src/calc/**")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = os.path.join(self.root, "w", "debug-17.toml")
        self.assertEqual(run_cli("validate", out).returncode, 0)
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        with open(self.stub, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        self.assertIn(f"from ../lib/workflows/debugging.toml (sha256 {digest})", text)
        self.assertIn(f"# on {datetime.date.today().isoformat()} with:", text)
        self.assertIn('#   bug = "div-zero-17"', text)
        self.assertIn('#   area = "src/calc/**"', text)
        self.assertIn("#   fix_max_lines = 150", text)
        data = tomllib.loads(text)
        self.assertEqual((data["name"], data["root"], data["library"]),
                         ("debug-div-zero-17", "..", ["../lib"]))
        self.assertNotIn("template", data)
        self.assertFalse([t for t in data["task"] if {"for_each", "chain", "note"} & set(t)])
        self.assertEqual(self.written(), ["debug-17.toml"])

    def test_parameters_are_checked(self):
        """tpl: parameters are checked (TPL-02)"""
        res = self.new("debugging", "-o", "w/x.toml", "--library", "lib", "--set", "bug=b")
        self.assertEqual(res.returncode, 2)
        self.assertIn("missing required parameter 'area'", res.stderr)
        res = self.new("debugging", "-o", "w/x.toml", "--library", "lib", "--set", "bug=b",
                       "--set", "area=src/**", "--set", "colour=red", "--set", "steps=a",
                       "--set", "fix_max_lines=many")
        self.assertEqual(res.returncode, 2)
        self.assertIn("has no parameter 'colour'", res.stderr)
        self.assertIn("parameter 'steps' is a list; give it in --params", res.stderr)
        self.assertIn("parameter 'fix_max_lines' must be an integer", res.stderr)
        res = self.new("debugging", "-o", "w/x.toml", "--library", "lib", "--params",
                       self.params('bug = 17\narea = "src/**"\nsteps = "one"\nshade = 1\n'))
        self.assertEqual(res.returncode, 2)
        self.assertIn("parameter 'bug' must be a string, got a integer", res.stderr)
        self.assertIn("parameter 'steps' must be a list", res.stderr)
        self.assertIn("has no parameter 'shade'", res.stderr)
        self.assertEqual(self.written(), [])

    def test_nothing_is_written_unless_it_loads(self):
        """tpl: nothing is written unless it loads (TPL-03)"""
        self.write(".gitignore", "src/calc/\n")
        self.commit()
        res = self.new("debugging", "-o", "w/debug-17.toml", "--library", "lib", "--params",
                       self.params('bug = "b"\narea = "src/calc/**"\nsteps = ["a", "b"]\n'))
        self.assertEqual(res.returncode, 2)
        self.assertIn("error: 'step-2' output src/calc/** is ignored by .gitignore:1", res.stderr)
        self.assertIn("(from template task 'step-{each.n}', copy 2)", res.stderr)
        self.assertIn("nothing was written", res.stderr)
        self.assertEqual(self.written(), [])

    def test_values_are_never_rescanned(self):
        """tpl: one pass, values never rescanned (TPL-04)"""
        t = self.stub_template()
        value = 'x{param.area}"q\'\n${HOME}\\{each.n}'
        values = templates.resolve_params(t, {"bug": value, "area": "src/**"})
        text, _ = templates.render(t, values, self.root)
        data = tomllib.loads(text)
        self.assertEqual(data["task"][0]["title"], "Reproduce " + value)
        self.assertEqual(data["name"], "debug-" + value)
        header = [line for line in text.splitlines() if line.startswith("#   bug = ")]
        self.assertEqual(len(header), 1)                    # the newline did not end the comment

    def test_for_each_chains_copies(self):
        """tpl: for_each chains copies (TPL-05)"""
        t = self.stub_template()
        values = templates.resolve_params(t, {"bug": "b", "area": "src/**",
                                              "steps": ["split", "move", "rename"],
                                              "final_checks": ["true"]})
        doc, notes, origins = templates.expand(t, values)
        by_id = {task["id"]: task for task in doc["task"]}
        self.assertEqual(by_id["step-1"]["needs"], ["reproduce"])
        self.assertEqual(by_id["step-2"]["needs"], ["reproduce", "step-1"])
        self.assertEqual(by_id["step-3"]["needs"], ["reproduce", "step-2"])
        self.assertEqual(by_id["step-3"]["title"], "Step 3: rename")
        self.assertEqual(by_id["final-1"]["verifies"], "step-3")
        self.assertEqual(by_id["signoff"]["needs"], ["step-3"])
        self.assertEqual(notes[2], "Step 2 of b.")
        self.assertEqual(origins["step-2"], ("step-{each.n}", 2))
        wf = templates.write(t, values, os.path.join(self.root, "w", "chain.toml"))
        self.assertEqual(wf.errors, [])
        with open(wf.workflow_file, encoding="utf-8") as fh:
            self.assertIn("# Step 2 of b.\n[[task]]\nid = \"step-2\"", fh.read())

    def test_empty_for_each(self):
        """tpl: empty for_each (TPL-06)"""
        t = self.stub_template()
        doc, _, _ = templates.expand(t, templates.resolve_params(t, {"bug": "b", "area": "src/**"}))
        self.assertEqual([task["id"] for task in doc["task"]], ["reproduce", "step-1", "signoff"])
        values = templates.resolve_params(t, {"bug": "b", "area": "src/**", "steps": []})
        with self.assertRaises(templates.TemplateError) as caught:
            templates.write(t, values, os.path.join(self.root, "w", "x.toml"))
        self.assertTrue(any("'{last.steps}' has no value (its for_each list is empty)" in e
                            for e in caught.exception.errors), caught.exception.errors)
        self.assertEqual(self.written(), [])

    def test_whole_value_placeholders_keep_their_type(self):
        """tpl: whole-value placeholders keep their type (TPL-07)"""
        t = self.stub_template()
        values = templates.resolve_params(t, {"bug": "b", "area": "src/**"}, ["fix_max_lines=40"])
        doc, _, _ = templates.expand(t, values)
        self.assertEqual(doc["task"][1]["max_changed_lines"], 40)
        self.assertEqual(doc["task"][0]["gate"], ["true"])
        values["invariants"] = ["make", "lint"]
        doc, _, _ = templates.expand(t, values)
        self.assertEqual(doc["task"][0]["gate"], ["make", "lint", "true"])
        bad = self.stub_template(STUB.replace('title = "Reproduce {param.bug}"',
                                              'title = "Reproduce {param.bug} in {param.steps}"'))
        with self.assertRaises(templates.TemplateError) as caught:
            templates.expand(bad, templates.resolve_params(bad, {"bug": "b", "area": "src/**"}))
        self.assertIn("template task 'reproduce': list '{param.steps}' is used inside a longer "
                      "string", caught.exception.errors[0])

    def test_template_errors(self):
        """A template is checked like a type: undeclared and unused parameters, misplaced keys."""
        broken = STUB.replace('[template.params.area]', '[template.params.unused]\ndefault = 1\n'
                              '[template.params.area]').replace(
            'title = "Reproduce {param.bug}"', 'title = "Reproduce {param.bg} {each.n}"\nchain = true'
        ).replace('name = "debug-{param.bug}"', 'name = "debug-{param.bug}"\nroot = ".."')
        with self.assertRaises(templates.TemplateError) as caught:
            self.stub_template(broken)
        errors = "\n".join(caught.exception.errors)
        self.assertIn("uses undeclared parameter '{param.bg}'", errors)
        self.assertIn("parameter 'unused' is declared and never used", errors)
        self.assertIn("task 'reproduce': '{each.n}' is used outside a for_each task", errors)
        self.assertIn("task 'reproduce': 'chain' needs 'for_each'", errors)
        self.assertIn("a template may not set 'root'", errors)

    def test_library_order(self):
        """tpl: library order (TPL-08)"""
        self.write("proj/lib/workflows/implementation.toml", STUB.replace("Stub:", "Project stub:"))
        self.commit()
        res = self.new("--list", "--library", "proj/lib")
        self.assertIn("implementation   Project stub:", res.stdout)
        self.assertIn(os.path.join("proj", "lib", "workflows", "implementation.toml"), res.stdout)
        self.assertIn("Project stub:", self.new("--describe", "implementation", "--library",
                                                "proj/lib").stdout)
        res = self.new("implementation", "-o", "w/x.toml", "--library", "proj/lib",
                       "--set", "bug=b", "--set", "area=src/**")
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(os.path.join(self.root, "w", "x.toml"), encoding="utf-8") as fh:
            self.assertIn("from ../proj/lib/workflows/implementation.toml (sha256 ", fh.read())
        res = self.new("--list")
        self.assertIn("implementation   Build something new", res.stdout)

    def test_existing_output_is_kept(self):
        """tpl: existing output is kept (TPL-09)"""
        out = self.write("w/debug.toml", "keep\n")
        args = ("debugging", "-o", "w/debug.toml", "--library", "lib", "--set", "bug=b",
                "--set", "area=src/**")
        res = self.new(*args)
        self.assertEqual(res.returncode, 2)
        self.assertIn("exists; nothing was written (pass --force to replace it)", res.stderr)
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep\n")
        self.assertEqual(self.new(*args, "--force").returncode, 0)
        self.assertEqual(workflow.load(out).errors, [])
        self.assertEqual(self.written(), ["debug.toml"])

    def test_a_template_without_a_name_takes_the_output_stem(self):
        """tpl: a template without a name takes the output's stem (TPL-16): never the name of the
        temporary file it is loaded from"""
        self.write("lib/workflows/plain.toml", STUB.replace('name = "debug-{param.bug}"\n', ""))
        res = self.new("plain", "-o", "w/my-flow.toml", "--library", "lib", "--set", "bug=b",
                       "--set", "area=src/**")
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(os.path.join(self.root, "w", "my-flow.toml"), "rb") as fh:
            self.assertEqual(tomllib.load(fh)["name"], "my-flow")
        res = self.new("plain", "-o", "w/my flow.toml", "--library", "lib", "--set", "bug=b",
                       "--set", "area=src/**")
        self.assertEqual(res.returncode, 2)
        self.assertIn("got 'my flow'", res.stderr)
        self.assertNotIn(".my flow.toml.", res.stderr)

    def test_symlinked_directories_give_plain_relative_paths(self):
        """tpl: symlinked directories give plain relative paths (TPL-17): the output directory,
        the root and the libraries are resolved before `root` and `library` are written, and the
        header names the template relative to its library, never by an absolute path"""
        link = os.path.join(self._tmp.name + "-link")
        os.symlink(self.root, link)
        self.addCleanup(os.unlink, link)
        outside = os.path.realpath(self.enterContext(tempfile.TemporaryDirectory()))
        shutil.copytree(os.path.join(self.root, "lib"), os.path.join(outside, "lib"))
        for library in (os.path.join(link, "lib"), os.path.join(outside, "lib")):
            with self.subTest(library=library):
                res = self.new("debugging", "-o", os.path.join(link, "w", "d.toml"), "--force",
                               "--library", library, "--set", "bug=b", "--set", "area=src/**")
                self.assertEqual(res.returncode, 0, res.stderr)
                with open(os.path.join(self.root, "w", "d.toml"), encoding="utf-8") as fh:
                    text = fh.read()
                data = tomllib.loads(text)
                self.assertEqual(data["root"], "..")
                self.assertNotIn("private", data["library"][0])
                header = text.splitlines()[0]
                self.assertNotIn(" from /", header)
                self.assertIn("/workflows/debugging.toml (sha256 ", header)
                self.assertEqual(os.path.normpath(os.path.join(self.root, "w", data["library"][0])),
                                 os.path.realpath(library))

    def test_odd_input_is_a_clean_error(self):
        """tpl: odd input is a clean error, and the file has the usual mode (TPL-18): a list
        element that is a boolean, a params file that is not UTF-8 and a --set that is not UTF-8
        are errors naming what is wrong; a note's control character is escaped in its comment;
        the written file is readable by others as any new file is (mkstemp makes 0600)"""
        base = ("debugging", "-o", "w/x.toml", "--library", "lib")
        res = self.new(*base, "--params", self.params('bug = "b"\narea = "src/**"\nsteps = ["a", true]\n'))
        self.assertEqual(res.returncode, 2)
        self.assertIn("parameter 'steps': element 2 is a bool", res.stderr)
        with open(os.path.join(self.root, "p.toml"), "wb") as fh:
            fh.write(b'bug = "\xff"\n')
        res = self.new(*base, "--params", "p.toml", "--set", "area=src/**")
        self.assertEqual(res.returncode, 2)
        self.assertIn("p.toml: not valid TOML", res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        res = subprocess.run([sys.executable, RUNNER, "new", *base, "--set", b"bug=\xff", "--set", "area=src/**"],
                             cwd=self.root, capture_output=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn(b"not valid UTF-8", res.stderr)
        self.assertNotIn(b"Traceback", res.stderr)
        self.assertEqual(self.written(), [])
        t = self.stub_template(STUB.replace('note = "Step {each.n} of {param.bug}."',
                                            'note = "Step {each.value}."'))
        path = os.path.join(self.root, "w", "x.toml")
        templates.write(t, templates.resolve_params(t, {"bug": "b", "area": "src/**",
                                                        "steps": ["a\x01b"]}), path)
        with open(path, encoding="utf-8") as fh:
            self.assertIn("# Step a\\u0001b.\n", fh.read())
        mask = os.umask(0)
        os.umask(mask)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o666 & ~mask)

    def test_the_toml_writer_round_trips(self):
        doc = {"name": "n", "x": [1, 2.5, True, "a\"b\\c\nd\te\x01"], "e": [], "t": {},
               "defaults": {"protected": ["a/**"], "budget_usd": 1.0},
               "agents": {"fake": {"argv": ["python3", "x y.py"]}},
               "task": [{"id": "a", "params": {"k": "v"}, "gate": ["g", {"run": "r", "new": True}],
                         "prompt": "line 1\n\"quoted\" \"\"\" \\ end\""}]}
        self.assertEqual(tomllib.loads(templates.dumps(doc, ["a\nnote"])), doc)


class Library(RepoCase):
    def test_debugging_and_refactoring_library_content(self):
        """The types and personas the debugging and refactoring templates use load, with the diff
        budgets of the design, and persona codes stay unique (WF-22, re-run)"""
        wf = workflow.load(self.write("wf.toml", '[[task]]\nid = "a"\ntype = "human"\n'))
        self.assertEqual(wf.errors, [])
        for name in ("reproduce", "diagnose", "fix", "characterise", "refactor-step"):
            self.assertEqual(wf.types[name]["kind"], "produce", name)
        self.assertEqual((wf.types["fix"]["max_changed_files"], wf.types["fix"]["max_changed_lines"]),
                         (5, 150))
        self.assertEqual((wf.types["refactor-step"]["max_changed_files"],
                          wf.types["refactor-step"]["max_changed_lines"]), (15, 300))
        self.assertEqual(sorted(wf.types["reproduce"]["params"]), ["symptom", "test_command"])
        self.assertEqual(sorted(wf.types["refactor-step"]["params"]), ["invariant", "step"])
        codes = {name: p["code"] for name, p in wf.personas.items()}
        self.assertEqual((codes["root-cause-rosa"], codes["gatekeeper-gao"], codes["steadfast-stefan"]),
                         ("RC", "SG", "AS"))
        self.assertEqual(len(set(codes.values())), len(codes))
        self.assertTrue(wf.personas["steadfast-stefan"]["advisory"])

    def test_bench_rules_carry_ids_and_reasons(self):
        """tpl: every rule of the C++ rules file and the performance appendix carries a unique id,
        a reason, and a source or an exception where it blocks (TPL-23)"""
        import re
        benches = os.path.join(EXAMPLES, "templates", "benches")
        cite = {}
        seen = set()
        for name in ("cpp-standards.md", "performance.md"):
            with open(os.path.join(benches, name), encoding="utf-8") as fh:
                text = fh.read()
            cite[name] = " ".join(next(p for p in text.split("\n\n")      # words, not line breaks
                                       if p.startswith("How to cite")).split())
            rules = re.findall(r"^- \*\*.*?(?=^- |^\n|\Z)", text, re.M | re.S)
            self.assertGreaterEqual(len(rules), 8, name)
            for rule in rules:
                rule = " ".join(rule.split())             # a clause may wrap at any word
                head = rule[len("- **"):rule.index("**", len("- **"))]
                rid = head.split()[0]
                with self.subTest(name=name, rule=rid):
                    self.assertRegex(rid, r"^(CPP|PF-C|PF-M)-?\d{2}$")
                    self.assertNotIn(rid, seen)
                    seen.add(rid)
                    self.assertIn("Reason: ", rule)
                    if name == "cpp-standards.md":     # a guideline id, or house: never neither
                        source = rule[rule.index("Source: "):]
                        self.assertRegex(source, r"^Source: (CG [A-Z]+\.\d+|house)")
                    elif "(blocking" in head:          # a hot-path blocker names its exception
                        self.assertIn("Exception: ", rule)
        self.assertEqual(cite["cpp-standards.md"], cite["performance.md"])
        self.assertIn("A rule's marking sets its severity: a violation of a rule marked "
                      "blocking may block, a violation of one marked advisory may not; citing a "
                      "Core Guidelines id or an advisory rule never makes a finding blocking; a "
                      "reviewer may still block on a consequence no rule names.",
                      cite["performance.md"])


class ShippedTemplates(RepoCase):
    def render(self, name, params=None):
        """Render a shipped template with an example parameter file (by default NAME.params.toml)
        into this scratch repository, beside a copy of examples/briefs/, examples/templates/briefs/
        and examples/templates/benches/, and load it."""
        briefs = os.path.join(self.root, "briefs")
        shutil.rmtree(briefs, ignore_errors=True)
        shutil.copytree(os.path.join(EXAMPLES, "briefs"), briefs)
        shutil.copytree(os.path.join(EXAMPLES, "templates", "briefs"), briefs, dirs_exist_ok=True)
        benches = os.path.join(self.root, "benches")
        shutil.rmtree(benches, ignore_errors=True)
        shutil.copytree(os.path.join(EXAMPLES, "templates", "benches"), benches)
        self.commit()
        t = templates.load(templates.find(name))
        params = params or name + ".params.toml"
        values = templates.resolve_params(
            t, templates.read_params(os.path.join(EXAMPLES, "templates", params)))
        return templates.write(t, values, os.path.join(self.root, params.replace(".params", "")),
                               force=True)

    def test_shipped_templates_render(self):
        """tpl: shipped templates render (TPL-10): every example parameter file, each with the
        template its name starts with, and every shipped template has one"""
        names = [n for n, _, _ in templates.available()]
        self.assertEqual(names, ["debugging", "implementation", "optimisation", "refactoring"])
        files = sorted(f for f in os.listdir(os.path.join(EXAMPLES, "templates"))
                       if f.endswith(".params.toml"))
        self.assertEqual(files, ["debugging-network.params.toml", "debugging-sanitizer.params.toml",
                                 "debugging.params.toml", "implementation.params.toml",
                                 "optimisation.params.toml", "refactoring.params.toml"])
        for params in files:
            name = params.split(".")[0].split("-")[0]
            with self.subTest(params):
                wf = self.render(name, params)
                self.assertEqual(wf.errors, [])
                self.assertEqual(run_cli("validate", wf.workflow_file).returncode, 0)

    def panels(self, wf):
        out = {}
        for t in wf.tasks:
            if t["kind"] == "review":
                out.setdefault(t["reviews"], []).append(
                    t["perspective"] + (" (advisory)" if t["advisory"] else ""))
        return out

    def test_debugging_panels_and_budget(self):
        """tpl: the debugging template follows the reviewer plan: one blocking root-cause skeptic
        per stage, the fix panel widest, packet-trace-pradip only where the parameters pass it
        (TPL-12); the C++ rules reach the author and each reviewer with their ids (TPL-24)"""
        plain = self.render("debugging")
        self.assertEqual(self.panels(plain), {
            "reproduce": ["root-cause-rosa"],
            "diagnose": ["root-cause-rosa", "principled-priya (advisory)"],
            "fix": ["root-cause-rosa", "principled-priya"],
            "siblings": ["root-cause-rosa", "principled-priya"],
        })
        by_id = {t["id"]: t for t in plain.tasks}
        self.assertEqual((by_id["fix"]["max_changed_files"], by_id["fix"]["max_changed_lines"]), (5, 150))
        self.assertEqual([g.get("expect") for g in by_id["reproduce"]["gates"]], [None, "fail"])
        self.assertTrue(by_id["fix"]["gates"][-1]["new"])
        self.assertEqual(by_id["diagnose"]["max_attempts"], 4)
        self.assertIsNone(by_id["fix"]["rules_file"])          # a Python project passes no rules file
        dag = cli.format_dag(plain)                       # `runner validate` marks the expected failure
        self.assertIn("gate: python3 -m unittest tests/regression/test_div_zero_17.py   "
                      "(must FAIL, matching /ZeroDivisionError/)", dag)
        self.assertIn("gate (new): python3 -m unittest tests/regression/test_div_zero_17.py\n", dag)
        net = self.render("debugging", "debugging-network.params.toml")
        self.assertEqual(self.panels(net), {
            "reproduce": ["root-cause-rosa", "packet-trace-pradip"],
            "diagnose": ["root-cause-rosa", "principled-priya (advisory)", "packet-trace-pradip"],
            "fix": ["root-cause-rosa", "principled-priya", "packet-trace-pradip"],
            "siblings": ["root-cause-rosa", "principled-priya", "packet-trace-pradip"],
        })
        self.assertEqual({t["id"]: t.get("max_changed_files") for t in net.tasks}["fix"], 3)
        san = self.render("debugging", "debugging-sanitizer.params.toml")
        self.assertEqual(self.panels(san), {
            "reproduce": ["root-cause-rosa", "memory-model-mei"],
            "diagnose": ["root-cause-rosa", "principled-priya (advisory)", "memory-model-mei"],
            "fix": ["root-cause-rosa", "principled-priya", "memory-model-mei", "concerned-carlos"],
            "siblings": ["root-cause-rosa", "principled-priya", "memory-model-mei", "concerned-carlos"],
        })
        # the project's rules file reaches every author and every reviewer of the C++ example
        rules = os.path.join("benches", "cpp-standards.md")
        for t in san.tasks:
            if t["kind"] in ("produce", "review"):
                self.assertEqual(os.path.relpath(t["rules_file"], self.root), rules, t["id"])
        # ... and its text, ids included, is in the author's prompt and a reviewer's, verbatim
        by_id = {t["id"]: t for t in san.tasks}
        with open(by_id["fix"]["rules_file"], encoding="utf-8") as fh:
            text = fh.read()
        ids = [f"CPP-{n:02d}" for n in range(1, 9)]
        caps = {"inputs_cap_bytes": 1000, "findings_cap_bytes": 60000, "diff_cap_bytes": 200000}
        author = prompts.produce_prompt(by_id["fix"], "{rules}", brief="b", inputs=[],
                                        feedback=None, attempt=1, frozen=[], caps=caps,
                                        project_rules=text)
        target = {"id": "fix", "title": "t", "summary": "", "outputs": [], "brief": "b",
                  "gates": [], "inputs": [], "candidate": "c", "base": "b"}
        reviewer = next(t for t in san.tasks if t["kind"] == "review" and t["reviews"] == "fix")
        with open(reviewer["rules_file"], encoding="utf-8") as fh:
            review_rules = fh.read()
        review = prompts.review_prompt(reviewer, "{rules}", persona="{}", target=target, brief="",
                                       diff="", full_path="", open_findings=[], round_number=1,
                                       caps=caps, project_rules=review_rules)
        section = prompts.project_rules_text(text)
        for prompt in (author, review):
            self.assertIn(section, prompt)
            for rule in ids:
                self.assertIn(f"- **{rule} ", prompt)

    def test_persona_names_resolve_and_are_documented(self):
        """tpl: every persona named in a template, an example or the library's README is a
        shipped file, and every shipped file is in the README with what it looks at (TPL-22)"""
        import re
        library = os.path.join(os.path.dirname(EXAMPLES), "library")
        shipped = {f[:-5] for f in os.listdir(os.path.join(library, "personas")) if f.endswith(".toml")}
        given = {n.rsplit("-", 1)[1] for n in shipped}                 # nate, mira, ...
        name = re.compile(r"\b[a-z]+(?:-[a-z]+)+\b")
        texts = {}
        for top, _dirs, files in os.walk(os.path.join(EXAMPLES, "templates")):
            for f in files:
                texts[os.path.join(top, f)] = None
        for f in os.listdir(os.path.join(library, "workflows")):
            texts[os.path.join(library, "workflows", f)] = None
        texts[os.path.join(library, "README.md")] = None
        for path in texts:
            with open(path, encoding="utf-8") as fh:
                texts[path] = fh.read()
        for path, text in texts.items():
            for word in set(name.findall(text)):
                if word.rsplit("-", 1)[1] in given:                 # named like a persona: must be one
                    with self.subTest(path=os.path.relpath(path, library), word=word):
                        self.assertIn(word, shipped)
        readme = texts[os.path.join(library, "README.md")]
        for persona in sorted(shipped):
            with self.subTest(persona=persona):
                self.assertRegex(readme, r"(?m)^\| `" + re.escape(persona) + r"` \|")

    def test_refactoring_panels_and_chain(self):
        """tpl: the refactoring template follows the reviewer plan: Mira on the safety net,
        gatekeeper-gao on every step, the widest panel on the final review; steps chained, each with
        the diff budget (TPL-13)"""
        wf = self.render("refactoring")
        panels = self.panels(wf)
        self.assertEqual(panels.pop("characterise"), ["meticulous-mira", "principled-priya (advisory)",
                                                      "steadfast-stefan"])
        self.assertEqual(panels.pop("final"), ["principled-priya", "steadfast-stefan"])
        self.assertEqual(panels, {f"step-{n}": ["gatekeeper-gao", "principled-priya (advisory)"]
                                  for n in (1, 2, 3)})
        by_id = {t["id"]: t for t in wf.tasks}
        self.assertEqual([by_id[f"step-{n}"]["needs"] for n in (1, 2, 3)],
                         [["characterise"], ["characterise", "step-1"], ["characterise", "step-2"]])
        self.assertEqual({(by_id[f"step-{n}"]["max_changed_files"], by_id[f"step-{n}"]["max_changed_lines"])
                          for n in (1, 2, 3)}, {(15, 300)})
        self.assertEqual((by_id["net-1"]["verifies"], by_id["net-1"]["restores"]), ("characterise", True))
        self.assertEqual((by_id["final"]["needs"], by_id["final-1"]["verifies"], by_id["signoff"]["needs"]),
                         (["step-3"], "final", ["final"]))

    def render_with(self, name, **changes):
        self.render(name)                                 # the briefs, committed
        t = templates.load(templates.find(name))
        given = templates.read_params(os.path.join(EXAMPLES, "templates", name + ".params.toml"))
        given.update(changes)
        return templates.write(t, templates.resolve_params(t, given),
                               os.path.join(self.root, "changed.toml"), force=True)

    def test_the_agent_is_a_parameter(self):
        """tpl: every shipped template takes agent (default claude) into [defaults] (TPL-20), and
        every family is in the inventory: the authors use the agent parameter with the other two
        families as fallbacks, the reviewers are dealt across the other two families"""
        for name in ("debugging", "implementation", "optimisation", "refactoring"):
            with self.subTest(name):
                plain = self.render(name)
                self.assertEqual((plain.errors, plain.defaults["agent"]), ([], "claude"))
                with open(plain.workflow_file, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertIn('[defaults]\nagent = "claude"\n', text)
                self.assertIn('[model_policy.high]\nclaude = "claude-opus-5-5"\ncodex = "gpt-6-astra"\n'
                              'copilot = "claude-sonnet-5"\n', text)
                authors = [t for t in plain.tasks if t["kind"] == "produce"]
                self.assertEqual({(t["agent"], tuple(t["fallback_agents"])) for t in authors},
                                 {("claude", ("codex", "copilot"))})
                reviewers = [t for t in plain.tasks if t["kind"] == "review"]
                self.assertEqual({t["agent"] for t in reviewers}, {"codex", "copilot"})
                self.assertTrue(all("claude" in t["fallback_agents"] for t in reviewers))
                other = self.render_with(name, agent="codex")
                self.assertEqual(other.errors, [])
                self.assertEqual({t["agent"] for t in other.tasks if t["kind"] == "produce"}, {"codex"})
                self.assertEqual({t["agent"] for t in other.tasks if t["kind"] == "review"},
                                 {"claude", "copilot"})

    def test_later_tasks_cannot_change_the_safety_net(self):
        """tpl: the safety net is protected by name in every later task (TPL-19): the debugging
        regression test inside area = "src/**" in fix and siblings; the characterisation tests and
        baselines inside the area in every step and in final. A parameter combination that would
        name one of them literally in `outputs` or `writes` is refused, with nothing written"""
        from types import SimpleNamespace
        from codesmith import engine

        def violation(task, path):
            probe = SimpleNamespace(to_root=lambda p: p)
            return engine.Engine.violation(probe, task, path, "100644", [("reproduce", [path])])
        wf = self.render_with("debugging", area="src/**", regression_test="src/calc/test_regression.py",
                              test_one="python3 -B src/calc/test_regression.py")
        by_id = {t["id"]: t for t in wf.tasks}
        for tid in ("fix", "siblings"):
            self.assertEqual(violation(by_id[tid], "src/calc/test_regression.py"), "it is protected", tid)
            self.assertEqual(violation(by_id[tid], "src/calc/__init__.py"), "", tid)
        wf = self.render_with("refactoring", area="src/**", char_tests="src/pricing/char/**",
                              baselines=["src/pricing/api-surface.txt"])
        by_id = {t["id"]: t for t in wf.tasks}
        for tid in ("step-1", "step-2", "step-3", "final"):
            for path in ("src/pricing/char/test_quote.py", "src/pricing/api-surface.txt", "tests/test_x.py"):
                self.assertEqual(violation(by_id[tid], path), "it is protected", (tid, path))
        self.assertEqual(violation(by_id["step-1"], "src/pricing/fees.py"), "")
        os.unlink(os.path.join(self.root, "changed.toml"))
        for name, changes in (("debugging", {"area": "src/calc/test_regression.py",
                                             "regression_test": "src/calc/test_regression.py"}),
                              ("refactoring", {"also_writes": ["docs/refactor/split-pricing/api-surface.txt"]})):
            with self.subTest(name), self.assertRaises(templates.TemplateError) as ctx:
                self.render_with(name, **changes)
            self.assertIn("would lift the template's protection", "\n".join(ctx.exception.errors))
            self.assertFalse(os.path.exists(os.path.join(self.root, "changed.toml")))

    def test_the_final_review_carries_the_whole_refactoring(self):
        """tpl: the final review carries the whole refactoring (TPL-15): `final` sets
        review_diff_from = "characterise", so its panel sees the cumulative diff and the author is
        not asked to paste git evidence; the report keeps the narrative, and a read-only check
        re-runs the safety net on the final tree"""
        by_id = {t["id"]: t for t in self.render("refactoring").tasks}
        self.assertEqual(by_id["final"]["review_diff_from"], "characterise")
        prompt = by_id["final"]["prompt"]
        self.assertNotIn("git diff --stat", prompt)
        self.assertIn("cumulative diff from the commit that accepted characterise", prompt)
        self.assertIn("public surface at the characterise commit and at HEAD", prompt)
        check = by_id["final-equivalence"]
        self.assertEqual((check["kind"], check["verifies"], check["read_only"]), ("check", "final", True))
        self.assertEqual(check["run"], [
            "python3 -m pytest -q tests/characterisation/split-pricing",
            "python3 tools/api_surface.py pricing | diff -u docs/refactor/split-pricing/api-surface.txt -"])

    def test_implementation_gives_book_module(self):
        """tpl: the implementation template renders the equivalent of examples/book-module.toml:
        the same tasks, paths, gates and checks; the panels of the reviewer plan (TPL-11)"""
        ours = self.render("implementation")
        book = workflow.load(os.path.join(EXAMPLES, "book-module.toml"))
        self.assertEqual(book.errors, [])
        rename = {"mutants": "mutants-1", "report-links": "report-check-1"}

        def shape(t):
            keys = ("kind", "type", "read_only", "restores", "writes", "removes", "gates")
            out = {k: t.get(k) for k in keys}
            out["needs"] = [rename.get(n, n) for n in t["needs"]]
            out["verifies"] = rename.get(t.get("verifies"), t.get("verifies"))
            out["outputs"] = [o["path"] for o in t.get("outputs", [])]
            out["protected"] = sorted(t["protected"])
            return out

        self.assertEqual(ours.name, book.name)
        self.assertEqual({rename.get(t["id"], t["id"]): shape(t) for t in book.tasks
                          if t["kind"] != "review"},
                         {t["id"]: shape(t) for t in ours.tasks if t["kind"] != "review"})
        panels = {}
        for t in ours.tasks:
            if t["kind"] == "review":
                panels.setdefault(t["reviews"], []).append(
                    t["perspective"] + (" (advisory)" if t["advisory"] else ""))
        self.assertEqual(panels, {
            # the C++ bench by stage: Aurélie blocks where the interface is decided, Carlos blocks
            # and Nate advises on the code (the brief names a hot path), Mira is not seated twice
            "design": ["principled-priya", "clause-by-clause-chen (advisory)",
                       "timeline-tanaka (advisory)", "api-aurelie"],
            "tests": ["meticulous-mira"],
            "implement": ["principled-priya", "dependable-diego (advisory)", "concerned-carlos",
                          "neckbeard-nate (advisory)"],
            "report": ["protocol-petra"],
        })

    def test_example_gates_configure_before_they_build(self):
        """tpl: the implementation example's gates and examples/book-module.toml's each configure
        before they build, so the acceptance replay passes on a clean checkout (TPL-30)"""
        ours = self.render("implementation")
        book = workflow.load(os.path.join(EXAMPLES, "book-module.toml"))
        self.assertEqual(book.errors, [])
        for where, wf in (("the rendered implementation example", ours),
                          ("examples/book-module.toml", book)):
            gates = [g["run"] for t in wf.tasks for g in t.get("gates", [])]
            builds = [run for run in gates if "cmake" in run or "ctest" in run]
            self.assertEqual(len(builds), 2, gates)        # the invariant and the test command
            for run in builds:
                with self.subTest(where, gate=run):
                    # A clean checkout has no build/: the replay (on by default) configures it.
                    self.assertTrue(run.startswith("cmake -S . -B build && "),
                                    f"a gate builds without configuring: {run}")
            replay = [t.get("acceptance_replay", wf.defaults["acceptance_replay"])
                      for t in wf.tasks if t["kind"] == "produce" and t.get("gates")]
            self.assertTrue(all(replay), "the replay is off, so the test proves nothing")
        # The brief names a hot path: the example appends the hot-path rules to its one rules
        # file, as library/README.md says a brief declaring one does.
        with open(os.path.join(EXAMPLES, "templates", "implementation.params.toml"),
                  encoding="utf-8") as fh:
            self.assertIn("cat benches/performance.md >> benches/cpp-standards.md", fh.read())


class Optimisation(RepoCase):
    """The opt-in optimisation template, its persona, type and contract script (pilot)."""

    render, panels, render_with = (ShippedTemplates.render, ShippedTemplates.panels,
                                   ShippedTemplates.render_with)

    def test_optimisation_panels_and_gate_order(self):
        """tpl: the optimisation template seats nadia, gao and mira blocking with the correctness
        reviewers on the optimised candidate, runs suite, benchmark and contract in that order,
        and leaves concerned-carlos out only when the parameters say so; the baseline may write
        also_writes, and the acceptance replay runs before the panel, serially (TPL-25)"""
        wf = self.render("optimisation")
        self.assertEqual(self.panels(wf), {
            "baseline": ["nanosecond-nadia", "meticulous-mira"],
            "optimise": ["nanosecond-nadia", "gatekeeper-gao", "meticulous-mira",
                         "concerned-carlos"],
            "report": ["protocol-petra"],
        })
        by_id = {t["id"]: t for t in wf.tasks}
        example = os.path.join(EXAMPLES, "templates", "optimisation.params.toml")
        params = templates.read_params(example)
        contract = ("python3 tools/bench_contract.py bench/book/baseline.json "
                    "build/bench/book.json --threshold-pct 5 --repetitions 10 --budget-p99-ns 0 "
                    "--session-ref 'build/bench/book-session.json' "
                    "|| { code=$?; [ $code -eq 3 ] || exit $code; }")
        session = params["baseline_bench_cmd"][0]
        self.assertIn("git archive HEAD | tar -x -C build/base", session)
        self.assertTrue(session.endswith("bench_book -o build/bench/book-session.json"), session)
        self.assertEqual([g["run"] for g in by_id["optimise"]["gates"]],
                         [params["test_cmd"], params["bench_cmd"], session, contract])
        self.assertFalse(any(g["new"] or g.get("expect") for g in by_id["optimise"]["gates"]))
        self.assertEqual([g["run"] for g in by_id["baseline"]["gates"]][-1],
                         "python3 tools/bench_contract.py --validate bench/book/baseline.json "
                         "--repetitions 10 --threshold-pct 5")
        # V-04: the baseline may register its targets in the build; optimise may not
        self.assertEqual(by_id["baseline"]["writes"],
                         ["bench/book/**", "tests/equivalence/book/**", "bench/book/baseline.json",
                          "CMakeLists.txt"])
        self.assertEqual(by_id["optimise"]["writes"], ["src/book/**"])
        # V-03: the acceptance replay re-runs the benchmark before the panel, never beside it
        self.assertIs(wf.defaults["replay_beside_panel"], False)
        self.assertEqual((by_id["optimise"]["type"], by_id["optimise"]["needs"]),
                         ("optimise", ["baseline"]))
        self.assertEqual(by_id["optimise"]["params"], {
            "hot_path": "the update path once the book is warm", "latency_budget_p99_ns": 0,
            "alloc_budget": "no allocation on the update path once the book is warm"})
        self.assertEqual((by_id["equivalence"]["verifies"], by_id["equivalence"]["read_only"],
                          by_id["equivalence"]["run"]),
                         ("optimise", True, [params["equivalence_cmd"]]))
        self.assertEqual((by_id["report"]["review_diff_from"], by_id["signoff"]["needs"]),
                         ("baseline", ["report"]))
        self.assertEqual(sorted(by_id["baseline"]["outputs"], key=lambda o: o["path"])[0]["path"],
                         "bench/book/**")
        self.assertIn("tests/book/**", by_id["optimise"]["protected"])
        default = self.render_with("optimisation", latency_budget_p99_ns=800)
        self.assertEqual(self.panels(default)["optimise"][-1], "concerned-carlos")
        self.assertEqual({t["id"]: t for t in default.tasks}["optimise"]["params"]
                         ["latency_budget_p99_ns"], 800)
        t = templates.load(templates.find("optimisation"))
        given = templates.read_params(example)
        del given["correctness_reviewers"]                    # the default seats carlos
        omitted = templates.write(t, templates.resolve_params(t, given),
                                  os.path.join(self.root, "omitted.toml"), force=True)
        self.assertEqual(self.panels(omitted)["optimise"][-1], "concerned-carlos")
        bare = self.render_with("optimisation", baseline_bench_cmd=[], session_ref="")
        bare_gates = [g["run"] for g in {t["id"]: t for t in bare.tasks}["optimise"]["gates"]]
        self.assertEqual(len(bare_gates), 3)
        self.assertIn("--session-ref '' ||", bare_gates[-1])
        empty = self.render_with("optimisation", correctness_reviewers=[])
        self.assertEqual(self.panels(empty)["optimise"],
                         ["nanosecond-nadia", "gatekeeper-gao", "meticulous-mira"])
        mei = self.render_with("optimisation",
                               correctness_reviewers=["concerned-carlos", "memory-model-mei"])
        self.assertEqual(self.panels(mei)["optimise"][-2:],
                         ["concerned-carlos", "memory-model-mei"])

    def test_equivalence_check_builds_what_it_runs(self):
        """tpl: the equivalence check of the example parameters configures and builds before it
        runs the test, with the suite's own build prefix, and its note says it runs beside the
        optimise panel (TPL-28)"""
        wf = self.render("optimisation")
        by_id = {t["id"]: t for t in wf.tasks}
        params = templates.read_params(
            os.path.join(EXAMPLES, "templates", "optimisation.params.toml"))
        with open(wf.workflow_file, encoding="utf-8") as fh:  # a note is a comment above its task
            text = fh.read()
        above = text[:text.index('id = "equivalence"')].rsplit("[[task]]", 2)[1]
        note = " ".join(line.lstrip("#") for line in above.splitlines() if line.startswith("#"))
        suite = by_id["optimise"]["gates"][0]["run"]
        self.assertEqual(suite, params["test_cmd"])
        build = suite[:suite.index("ctest ")]      # what the suite runs before its tests
        self.assertTrue(build.startswith("cmake -S . -B build && cmake --build build"), suite)
        [check] = by_id["equivalence"]["run"]
        self.assertTrue(check.startswith(build), check)
        self.assertTrue(check.endswith("ctest --test-dir build -R book_equivalence "
                                       "--output-on-failure"), check)
        self.assertIn("Runs beside the optimise panel as a read-only check, not before it.",
                      " ".join(note.split()))

    def test_the_baseline_result_is_summarised_for_its_reviewers(self):
        """tpl: the baseline task of the optimisation template with its example parameters
        summarises the committed baseline in its reviewers' diff and nothing else; optimise and
        report do not set the key (TPL-29)"""
        wf = self.render("optimisation")
        by_id = {t["id"]: t for t in wf.tasks}
        self.assertEqual(by_id["baseline"]["review_diff_summarised"], ["bench/book/baseline.json"])
        for tid in ("optimise", "report"):
            self.assertNotIn("review_diff_summarised", by_id[tid])
        rows = "".join(f"+{n}\n" for n in range(2000))
        diff = "".join(f"diff --git a/{p} b/{p}\nnew file mode 100644\n--- /dev/null\n+++ b/{p}\n"
                       f"@@ -0,0 +1,{n} @@\n" + body
                       for p, n, body in (("bench/book/bench.cpp", 1, "+int main();\n"),
                                          ("bench/book/baseline.json", 2000, rows)))
        shown = gitops.summarise_diff(diff, by_id["baseline"]["review_diff_summarised"])
        self.assertIn("+int main();\n", shown)
        self.assertIn("[summarised: bench/book/baseline.json is a result file", shown)
        self.assertNotIn("+1000\n", shown)

    def optimise_prompts(self):
        """Nadia's review prompt and the author's prompt on the optimise task, rendered with the
        example parameters and performance.md appended to the rules file (PRM-19, PRM-20)."""
        wf = self.render("optimisation")
        by_id = {t["id"]: t for t in wf.tasks}
        benches = os.path.join(EXAMPLES, "templates", "benches")
        with open(os.path.join(benches, "cpp-standards.md"), encoding="utf-8") as fh:
            rules = fh.read()
        with open(os.path.join(benches, "performance.md"), encoding="utf-8") as fh:
            rules += "\n" + fh.read()                      # appended, as the parameter says
        with open(os.path.join(os.path.dirname(EXAMPLES), "library", "types",
                               "code-review.toml"), "rb") as fh:
            template = tomllib.load(fh)["prompt"]
        with open(os.path.join(os.path.dirname(EXAMPLES), "library", "personas",
                               "nanosecond-nadia.toml"), "rb") as fh:
            nadia = tomllib.load(fh)
        task = by_id["optimise"]
        reviewers = [t for t in wf.tasks if t["kind"] == "review" and t["reviews"] == "optimise"]
        me = next(r for r in reviewers if r["perspective"] == "nanosecond-nadia")
        panel = [{"perspective": r["perspective"],
                  "mode": "advisory" if r["advisory"] else "blocking"}
                 for r in reviewers if r is not me]
        target = {"id": "optimise", "title": task["title"], "type": task["type"],
                  "params": task["params"], "summary": "", "outputs": task["outputs"],
                  "brief": "Take allocation off the update path.", "gates": task["gates"],
                  "inputs": [], "candidate": "c", "base": "b", "settled_by_person": [],
                  "gate_evidence": {"output": "result: pass (exit 0)"}, "panel": panel}
        caps = {"inputs_cap_bytes": 20000, "findings_cap_bytes": 60000, "diff_cap_bytes": 200000}
        prompt = prompts.review_prompt(me, template, persona=nadia, target=target, brief="",
                                       diff="", full_path="", open_findings=[], round_number=1,
                                       caps=caps, project_rules=rules)
        author = prompts.produce_prompt(task, wf.types["optimise"]["prompt"], brief="b", inputs=[],
                                        feedback=None, attempt=1, frozen=[], caps=caps,
                                        project_rules=rules)
        return nadia, panel, prompt, author

    def test_nadia_carries_the_budget_and_the_rule_ids(self):
        """prm: nadia's review prompt carries the declared budget and the appendix ids, and her
        out-of-scope routing names the owners seated beside her on the optimise panel (PRM-19)"""
        nadia, panel, prompt, author = self.optimise_prompts()
        for text in ('"latency_budget_p99_ns": 0', '"alloc_budget": "no allocation on the update',
                     '"hot_path": "the update path once the book is warm"',
                     "The declared budget first", "On this panel you can BLOCK",
                     "an inconclusive comparison whose p50 or p99 is worse than the baseline",
                     '("inconclusive, worse") blocks on an optimise task'):
            self.assertIn(text, prompt)
        for rid in [f"PF-M0{n}" for n in range(1, 6)] + ["PF-C01", "PF-C08", "CPP-01"]:
            self.assertIn(f"- **{rid} ", prompt)
        seated = [p["perspective"] for p in panel]
        self.assertEqual(seated, ["gatekeeper-gao", "meticulous-mira", "concerned-carlos"])
        routing = " ".join(nadia["out_of_scope"])
        for owner in seated:                     # every seated owner is where her routing sends it
            self.assertIn(owner, routing)
            self.assertIn(f'"perspective": "{owner}"', prompt)
        self.assertIn("A directly checkable violation of a requirement the brief or the project "
                      "rules state", prompt)
        self.assertIn("<<<DATA parameter alloc_budget\nno allocation on the update path", author)
        self.assertIn("<<<DATA parameter latency_budget_p99_ns\n0\nDATA>>>", author)
        self.assertIn("A behaviour change the brief did\n  not name is a defect", author)

    def test_summary_numbers_are_the_gates_or_estimates(self):
        """prm: the optimise author gives no number as the runner's, labels its own run's "my
        run, before the gate", and nadia blocks only on a summary number credited to the
        runner's gate that the gate evidence does not hold (PRM-20)"""
        _, _, prompt, author = self.optimise_prompts()
        flat = {"nadia's review prompt": " ".join(prompt.split()),
                "the optimise author's prompt": " ".join(author.split())}
        expected = [
            ("nadia's review prompt", "Every number the author's summary (target.summary) "
             "credits to the runner's gate looked up in the gate evidence"),
            ("nadia's review prompt", "a number the author's summary credits to the runner's "
             'gate (presented as the gate\'s, "as printed", or measured by the runner) that the '
             "executed gate evidence does not hold: quote the author's number and the nearest "
             "gate line. A number labelled as the author's own run or as an estimate is advisory "
             "at most"),
            ("the optimise author's prompt", "The runner runs the gates again after you finish "
             "and re-measures, so your run's numbers will not be its numbers"),
            ("the optimise author's prompt", "Your summary gives no number as the runner's or "
             "the gate's: the runner's gate output is the measurement of record"),
            ("the optimise author's prompt", 'A number from your own run of the benchmark or the '
             'contract is given only labelled "my run, before the gate: " with the line it '
             "printed, or not at all"),
        ]
        for where, text in expected:
            self.assertIn(text, flat[where], f"{where} lacks: {text}")
        for gone in ("did not print is an estimate, and you say so",
                     "quotes the contract's result lines verbatim"):
            self.assertNotIn(gone, flat["the optimise author's prompt"])


class BenchContract(unittest.TestCase):
    """examples/tools/bench_contract.py on fixture result files (TPL-26)."""

    SCRIPT = os.path.join(EXAMPLES, "tools", "bench_contract.py")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def result(self, name, runs, **changes):
        doc = {"command": "python3 bench.py", "pinning": "none", "warm": True,
               "benchmarks": [{"bench": "update", "unit": "ns", "runs": runs}]}
        doc.update(changes)
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    @staticmethod
    def steady(cost, seed=1, scales=(1.0,) * 5, n=200):
        """One list of samples per repetition; each repetition's level scaled (session drift)."""
        rng = random.Random(seed)
        return [[round(cost * s) + rng.randint(0, 10) for _ in range(n)] for s in scales]

    @staticmethod
    def cliff(cost, seed=1, reps=5, n=200):
        """A tail of about 1% at random heights: the p99 sits on the cliff, so it is all noise."""
        rng = random.Random(seed)
        k = n // 100 + 1
        return [[cost + rng.randint(0, 10) for _ in range(n - k)]
                + [rng.randint(cost * 2, cost * 20) for _ in range(k)] for _ in range(reps)]

    @staticmethod
    def two_tail(body, seed=1, reps=5, n=200):
        """2% of the samples at 1000 ns, the rest at the body: p99 is the tail, p50 the body."""
        rng = random.Random(seed)
        return [[body + rng.randint(0, 10) for _ in range(n - n // 50)] + [1000] * (n // 50)
                for _ in range(reps)]

    def run_contract(self, *args):
        res = subprocess.run([sys.executable, self.SCRIPT, *args], capture_output=True, text=True)
        return res.returncode, res.stdout + res.stderr

    def compare(self, base, cand, *extra, threshold="10"):
        return self.run_contract(base, cand, "--threshold-pct", threshold, "--repetitions", "5",
                                 *extra)

    def test_the_contract_decides_on_fixture_results(self):
        """tpl: bench_contract.py passes an unchanged or faster candidate, fails a regression of
        three times the threshold on p50 or p99, calls a statistic inside the noise inconclusive
        (", worse" when its point estimate is worse than the threshold), resamples repetitions so
        session drift is not a regression, refuses bad input, prints its table and result last,
        and the template's gate fails only on 1 and 2 (TPL-26)"""
        base = self.result("base.json", self.steady(100))
        cases = {
            "same": (self.result("same.json", self.steady(100, seed=2)), 0, "pass: no change"),
            "faster": (self.result("fast.json", self.steady(50, seed=3)), 0, "pass: faster"),
            "regression": (self.result("slow.json", self.steady(134, seed=4)), 1, ": regression"),
        }
        for name, (cand, code, verdict) in cases.items():
            with self.subTest(name):
                got, out = self.compare(base, cand, "--budget-p99-ns", "120")
                self.assertEqual(got, code, out)
                lines = out.rstrip("\n").split("\n")
                self.assertTrue(lines[-3].startswith("update  p50 "), lines[-3])
                self.assertTrue(lines[-2].startswith("update  p99 "), lines[-2])
                self.assertIn(verdict, lines[-2])
                word = "regression" if code else "pass"
                self.assertEqual(lines[-1], f"result: {word} (exit {code})")
                self.assertIn("bench   side       unit  count  p50  p99  p99.9  max", out)
                for prefix in ("PF-M01 baseline: present,", "PF-M02 command: python3 bench.py",
                               "PF-M03 warm", "PF-M04 threshold: 10% on p50 and p99"):
                    self.assertIn(prefix, out)
        _code, slow = self.compare(base, cases["regression"][0])
        delta = float(slow.split("update  p99 ")[1].split("%")[0])
        self.assertGreaterEqual(delta, 30.0)                       # 3 x the 10% threshold
        self.assertIn("budget p99 <= 120 ns: EXCEEDED (update candidate p99 144 ns)",
                      self.compare(base, cases["regression"][0], "--budget-p99-ns", "120")[1])
        # V-01: three times slower in the body, the tail unchanged: p50 decides
        code, out = self.compare(self.result("tb.json", self.two_tail(100)),
                                 self.result("tc.json", self.two_tail(300, seed=2)))
        self.assertEqual(code, 1, out)
        self.assertIn("update  p50 +190.5%", out)
        self.assertIn("update  p99 +0.0%", out)
        self.assertTrue(out.rstrip().endswith("result: regression (exit 1)"), out)
        # a p99 on a tail cliff: inconclusive; worse than the threshold says so in the result
        noisy_base = self.result("nb.json", self.cliff(100, seed=5))
        noisy = self.result("nc.json", self.cliff(100, seed=7))          # p99 -11.4%
        code, out = self.compare(noisy_base, noisy)
        self.assertEqual(code, 3, out)
        self.assertIn(": inconclusive: the noise", out.rstrip().split("\n")[-2])
        self.assertTrue(out.rstrip().endswith("result: inconclusive (exit 3)"), out)
        self.assertEqual(out, self.compare(noisy_base, noisy)[1])          # a fixed seed
        worse = self.result("nw.json", self.cliff(100, seed=6))          # p99 +21.6%, in the noise
        code, out = self.compare(noisy_base, worse)
        self.assertEqual(code, 3, out)
        self.assertIn("update  p99 +21.6%", out)
        self.assertIn("; worse by more than the threshold", out.rstrip().split("\n")[-2])
        self.assertTrue(out.rstrip().endswith("result: inconclusive, worse (exit 3)"), out)
        # V-02: a whole session 6% slower, tight repetitions: the committed baseline re-measured
        # in this session (--session-ref) shows the drift, and the comparison is refused before
        # the candidate is judged; a 2% drift lets the candidate be judged as usual
        tight = self.result("tt.json", self.steady(1000))
        same_code_later = self.result("sl.json", self.steady(1000, seed=8, scales=(1.06,) * 5))
        drifted = self.result("sr6.json", self.steady(1000, seed=9, scales=(1.06,) * 5))
        code, out = self.compare(tight, same_code_later, "--session-ref", drifted, threshold="5")
        self.assertEqual(code, 2, out)
        self.assertIn("bad input: session drift: the baseline re-measured in this session differs "
                      "from the committed baseline by +", out)
        self.assertIn("re-baseline or run on a quiet machine, then `runner retry RUN optimise`",
                      out)
        steady_ref = self.result("sr2.json", self.steady(1000, seed=10, scales=(1.02,) * 5))
        code, out = self.compare(tight, self.result("t3.json", self.steady(1340, seed=4)),
                                 "--session-ref", steady_ref, threshold="5")
        self.assertEqual(code, 1, out)                              # judged: the 3x case
        self.assertIn("session drift: the baseline re-measured in this session is within", out)
        code, out = self.compare(tight, self.result("t1.json", self.steady(1000, seed=11)),
                                 "--session-ref", steady_ref, threshold="5")
        self.assertEqual(code, 0, out)
        self.assertIn("session drift: not checked", self.compare(tight, steady_ref,
                                                                 threshold="5")[1])
        # the candidate is judged against the session reference, not the committed baseline:
        # session +4%, candidate truly 2% slower (+6% on the committed one) -> pass
        up = self.result("su.json", self.steady(1000, seed=12, scales=(1.04,) * 5))
        code, out = self.compare(tight, self.result("c2.json", self.steady(
            1000, seed=13, scales=(1.04 * 1.02,) * 5)), "--session-ref", up, threshold="5")
        self.assertEqual(code, 0, out)
        self.assertIn("update  session", out)
        # session -3.5%, candidate truly 8% slower (+4.2% on the committed one) -> regression
        down = self.result("sd.json", self.steady(1000, seed=14, scales=(0.965,) * 5))
        code, out = self.compare(tight, self.result("c8.json", self.steady(
            1000, seed=15, scales=(0.965 * 1.08,) * 5)), "--session-ref", down, threshold="5")
        self.assertEqual(code, 1, out)
        self.assertIn("is within 3.", out)                       # the drift, for information
        # the spread between repetitions: the baseline's own gate refuses it under the threshold
        drift = self.result("db.json", self.steady(1000, scales=(0.92, 1.0, 1.08, 0.95, 1.05)))
        # ... and the baseline's own gate measures that spread and refuses it under the threshold
        code, out = self.run_contract("--validate", drift, "--repetitions", "5",
                                      "--threshold-pct", "5")
        self.assertEqual(code, 2, out)
        self.assertIn("bad input: PF-M04: the spread between repetitions of 'update'", out)
        code, out = self.run_contract("--validate", tight, "--repetitions", "5",
                                      "--threshold-pct", "5")
        self.assertEqual(code, 0, out)
        self.assertIn("PF-M04 spread between 5 repetitions of update: p50 ±", out)
        bad = {
            "missing": os.path.join(self.dir, "absent.json"),
            "not json": self.result("broken.json", []),
            "no pinning": self.result("np.json", self.steady(100), pinning=""),
            "too few repetitions": self.result("rep.json", self.steady(100)[:2]),
            "pooled samples": self.result("pool.json", self.steady(100)[0]),
            "warm not stated": self.result("warm.json", self.steady(100), warm="yes"),
            "too few samples": self.result("few.json", self.steady(100, n=10)),
            "another command": self.result("cmd.json", self.steady(100), command="other"),
            "cold against warm": self.result("cold.json", self.steady(100), warm=False),
        }
        with open(bad["not json"], "w", encoding="utf-8") as fh:
            fh.write("{not json")
        for name, cand in bad.items():
            with self.subTest(name):
                code, out = self.compare(base, cand)
                self.assertEqual(code, 2, out)
                self.assertTrue(out.rstrip().endswith("result: bad input (exit 2)"), out)
                self.assertIn("bad input: ", out)
        self.assertEqual(self.run_contract("--validate", base, "--repetitions", "5")[0], 0)
        self.assertEqual(
            self.run_contract("--validate", bad["no pinning"], "--repetitions", "5")[0], 2)
        self.assertEqual(self.run_contract(base, cases["same"][0], "--repetitions", "5")[0], 2)
        # The template's gate, as rendered, through the shell the runner uses: exit 3 passes.
        with open(os.path.join(os.path.dirname(EXAMPLES), "library", "workflows",
                               "optimisation.toml"), "rb") as fh:
            gate = next(t for t in tomllib.load(fh)["task"] if t["id"] == "optimise")["gate"][-1]
        script = gate.replace("tools/bench_contract.py", self.SCRIPT)
        script = script.replace("python3", sys.executable)
        for name, (b, c), code in (("inconclusive", (noisy_base, noisy), 0),
                                   ("inconclusive, worse", (noisy_base, worse), 0),
                                   ("regression", (base, cases["regression"][0]), 1),
                                   ("bad input", (base, bad["missing"]), 2),
                                   ("pass", (base, cases["faster"][0]), 0)):
            with self.subTest(gate=name):
                values = {"param.baseline": b, "param.bench_out": c, "param.session_ref": "",
                          "param.threshold_pct": "10", "param.repetitions": "5",
                          "param.latency_budget_p99_ns": "0"}
                command = templates.PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], script)
                self.assertEqual(subprocess.run(["/bin/sh", "-c", command],
                                                capture_output=True).returncode, code)


class MeasurementRules(unittest.TestCase):
    def test_measurement_rows_carry_reason_and_gate_output(self):
        """tpl: the measurement requirements PF-M01 to PF-M05 each carry a requirement, a reason
        and what the gate prints, which the contract script does print; the appendix no longer
        says they do not ship; PF-M04 and nadia's blocking list say that an "inconclusive,
        worse" result blocks on an optimise task (TPL-27)"""
        import re
        with open(os.path.join(EXAMPLES, "templates", "benches", "performance.md"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("does not ship", text)
        rules = [" ".join(r.split()) for r in re.findall(r"^- \*\*PF-M.*?(?=^- |^\n|\Z)", text,
                                                         re.M | re.S)]
        self.assertEqual([r.split()[1].lstrip("*") for r in rules],
                         [f"PF-M0{n}" for n in range(1, 6)])
        self.assertIn('An inconclusive result whose p50 or p99 is worse than the baseline by more '
                      'than the threshold ("inconclusive, worse") blocks on an optimise task',
                      rules[3])
        with open(os.path.join(os.path.dirname(EXAMPLES), "library", "personas",
                               "nanosecond-nadia.toml"), "rb") as fh:
            third = tomllib.load(fh)["blocking"][2]
        self.assertIn("an inconclusive comparison whose p50 or p99 is worse than the baseline by "
                      'more than the threshold (the gate\'s result line reads "inconclusive, '
                      'worse")', third)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"command": "c", "pinning": "none", "warm": True,
                           "benchmarks": [{"bench": "u", "unit": "ns",
                                           "runs": BenchContract.steady(100)}]}, fh)
            out = subprocess.run([sys.executable, BenchContract.SCRIPT, path, path,
                                  "--threshold-pct", "5", "--repetitions", "5"],
                                 capture_output=True, text=True).stdout
        for rule in rules:
            with self.subTest(rule[:12]):
                self.assertIn("(blocking on an optimise task).** ", rule)
                self.assertIn(" Reason: ", rule)
                self.assertIn(" Exception: ", rule)
                printed = rule[rule.index(" The gate prints: "):]
                quoted = re.findall(r"`([^`]*)`", printed)
                self.assertTrue(quoted, rule)
                shown = quoted[0].split("…")[0].split(" N ")[0].strip()
                self.assertIn(shown, out)               # the row quotes what the script prints


if __name__ == "__main__":
    unittest.main()
