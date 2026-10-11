"""Workflow templates and `runner new`: scenarios TPL-01 to TPL-13, TPL-15 to TPL-18 and TPL-19 of docs/06-scenarios.md, with
a stub template, and the shipped templates. The scripted runs (ACC-29, TPL-14) are in
test_template_runs.py."""

import datetime
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

from helpers import RUNNER, RepoCase, run_cli
from codesmith import cli, templates, workflow

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
        self.assertEqual(names, ["debugging", "implementation", "refactoring"])
        files = sorted(f for f in os.listdir(os.path.join(EXAMPLES, "templates"))
                       if f.endswith(".params.toml"))
        self.assertEqual(files, ["debugging-network.params.toml", "debugging-sanitizer.params.toml",
                                 "debugging.params.toml", "implementation.params.toml",
                                 "refactoring.params.toml"])
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
        per stage, the fix panel widest, packet-trace-pradip only where the parameters pass it (TPL-12)"""
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
        for name in ("debugging", "implementation", "refactoring"):
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


if __name__ == "__main__":
    unittest.main()
