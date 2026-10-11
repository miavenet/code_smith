"""The command line: validate and graph (WF-13), start, status, runs and prune (RUN-01, RUN-02,
RUN-17, GIT-05, GIT-14), and the stubs of later stages."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import unittest

from helpers import RUNNER, EngineCase, RepoCase, done, git, run_cli

WORKFLOW = """
name = "demo"
[[task]]
id = "design"
type = "design"
outputs = ["docs/d.md"]
reviewers = ["principled-priya", { perspective = "dependable-diego", advisory = true }]
[[task]]
id = "implement"
type = "implement"
needs = ["design"]
outputs = ["src/**"]
gate = ["true"]
[[task]]
id = "lint"
type = "check"
verifies = "implement"
read_only = true
run = ["true"]
[[task]]
id = "signoff"
type = "human"
needs = ["implement"]
"""


class Cli(RepoCase):
    def test_validate_prints_the_expanded_dag(self):
        path = self.write("wf.toml", WORKFLOW)
        res = run_cli("validate", path)
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = [l for l in res.stdout.splitlines() if re.match(r"\s+\d+  ", l)]
        ids = [l.split()[1] for l in lines]
        self.assertEqual(ids, ["design", "design.review.principled-priya", "design.review.dependable-diego",
                               "implement", "lint", "signoff"])
        self.assertIn("reviews design (advisory)", res.stdout)
        self.assertIn("verifies implement; read-only", res.stdout)
        self.assertIn("needs design", res.stdout)
        self.assertIn("claims on frozen outputs: none", res.stdout)

    def test_validate_reports_every_error_and_exits_2(self):
        path = self.write("wf.toml", WORKFLOW.replace('gate = ["true"]', 'gaet = ["true"]')
                          .replace('needs = ["implement"]', 'needs = ["nobody"]'))
        res = run_cli("validate", path)
        self.assertEqual(res.returncode, 2)
        self.assertIn("unknown key 'gaet'", res.stderr)
        self.assertIn("unknown task 'nobody'", res.stderr)
        self.assertIn("2 errors", res.stderr)
        self.assertEqual(res.stdout, "")

    def test_validate_shows_claims(self):
        path = self.write("wf.toml", WORKFLOW + '[[task]]\nid = "extend"\ntype = "implement"\n'
                          'needs = ["implement"]\noutputs = ["src/extra.cpp"]\ngate = ["true"]\n')
        res = run_cli("validate", path)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("warning: 'extend' will modify outputs of accepted task 'implement'", res.stderr)
        self.assertIn("extend will modify", res.stdout)

    def test_dot_export(self):
        """wf: dot export (WF-13)"""
        path = self.write("wf.toml", WORKFLOW)
        res = run_cli("graph", path)
        self.assertEqual(res.returncode, 0, res.stderr)
        dot = res.stdout
        self.assertTrue(dot.startswith('digraph "demo" {'))
        self.assertEqual(dot.rstrip()[-1], "}")
        self.assertEqual(dot.count("{"), dot.count("}"))
        nodes = re.findall(r'^  "([^"]+)" \[label=', dot, re.M)
        self.assertEqual(sorted(nodes), sorted(["design", "design.review.principled-priya",
                                                "design.review.dependable-diego", "implement", "lint", "signoff"]))
        edges = set(re.findall(r'^  "([^"]+)" -> "([^"]+)" \[label="(\w+)"', dot, re.M))
        self.assertEqual(edges, {
            ("design", "implement", "needs"), ("implement", "signoff", "needs"),
            ("design", "design.review.principled-priya", "reviews"),
            ("design", "design.review.dependable-diego", "reviews"), ("implement", "lint", "verifies")})
        self.assertIn("style=dashed", dot)
        self.assertIn("style=dotted", dot)

    def test_graph_to_a_file(self):
        path = self.write("wf.toml", WORKFLOW)
        out = os.path.join(self.root, "g.dot")
        res = run_cli("graph", path, "-o", out)
        self.assertEqual((res.returncode, res.stdout), (0, ""))
        with open(out) as fh:
            self.assertIn("digraph", fh.read())

    def test_replan_and_resolve_are_available(self):
        for cmd in ("replan", "resolve"):
            res = run_cli(cmd, "--help")
            self.assertEqual(res.returncode, 0)
            self.assertNotIn("not implemented", res.stdout)

    def test_no_command(self):
        self.assertEqual(run_cli().returncode, 2)

    def test_the_shipped_example_validates(self):
        example = os.path.join(os.path.dirname(__file__), "..", "examples", "book-module.toml")
        res = run_cli("validate", example)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("13 tasks", res.stdout)


def gitout(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class Runs(RepoCase):
    RUN_WORKFLOW = 'name = "demo"\n[[task]]\nid = "design"\ntype = "human"\n'
    def setUp(self):
        super().setUp()
        self.wf = self.write("wf.toml", self.RUN_WORKFLOW)
        self.commit()

    def start(self):
        res = run_cli("start", self.wf)
        self.assertEqual(res.returncode, 255, res.stderr)
        self.assertIn("needs_human", res.stderr)
        return re.search(r"^run (\S+)", res.stdout, re.M).group(1)

    def run_info(self, name):
        with open(os.path.join(self.root, ".runs", "demo", name, "run.json")) as fh:
            return json.load(fh)

    def run_dirs(self):
        base = os.path.join(self.root, ".runs", "demo")
        return sorted(n for n in os.listdir(base) if n != "latest")

    def test_start_always_creates(self):
        """run: start always creates (RUN-01)"""
        first = self.start()
        second = self.start()
        self.assertNotEqual(first, second)
        self.assertEqual(sorted([first, second]), self.run_dirs())
        with open(os.path.join(self.root, ".runs", "demo", "latest")) as fh:
            self.assertEqual(fh.read().strip(), second)
        ids = [self.run_info(d)["run_id"] for d in (first, second)]
        self.assertNotEqual(ids[0], ids[1])

    def test_runs_dir_option_relocates_the_record(self):
        """run: --runs-dir keeps the record in a named directory; later commands need it too"""
        res = run_cli("--runs-dir", os.path.join(self.root, "artifacts"), "start", self.wf)
        self.assertEqual(res.returncode, 255, res.stderr)
        name = re.search(r"^run (\S+)", res.stdout, re.M).group(1)
        self.assertRegex(name, r"^demo-\d{8}T\d{6}Z-[0-9a-f]{8}$")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "artifacts", "demo", name, "run.json")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".runs")))
        self.assertEqual(gitout(self.root, "status", "--porcelain"), "")     # it ignores itself
        found = run_cli("--runs-dir", "artifacts", "status", "-C", self.root)  # relative to the top
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn("demo", found.stdout)
        lost = run_cli("status", "-C", self.root)
        self.assertEqual(lost.returncode, 2)
        self.assertIn("--runs-dir", lost.stderr)
        env = dict(os.environ, CODE_SMITH_RUNS_DIR="artifacts")              # relative to the top
        via_env = subprocess.run([sys.executable, RUNNER, "status", "-C", self.root],
                                 capture_output=True, text=True, env=env)
        self.assertEqual(via_env.returncode, 0, via_env.stderr)

    def test_a_workflow_names_its_runs_dir(self):
        """run: a workflow's runs_dir is used and remembered; a misplaced --runs-dir gets a hint
        (RUN-37). The directory is relative to the repository top; start remembers it for the
        commands on a RUN"""
        os.makedirs(os.path.join(self.root, "w"))
        wf = self.write("w/wf.toml", self.RUN_WORKFLOW.replace(
            'name = "demo"', 'name = "demo"\n[defaults]\nruns_dir = "records"'))
        self.commit()
        res = run_cli("start", wf)
        self.assertEqual(res.returncode, 255, res.stderr)
        name = re.search(r"^run (\S+)", res.stdout, re.M).group(1)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "records", "demo", name, "run.json")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "w", "records")))   # the top, not the file
        self.assertFalse(os.path.exists(os.path.join(self.root, ".runs")))
        self.assertEqual(gitout(self.root, "config", "--get", "codesmith.runsdir"),
                         os.path.join(self.root, "records"))
        found = run_cli("status", "-C", os.path.join(self.root, "w"))
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn(name[-8:], found.stdout)
        self.assertIn(name, run_cli("runs", "-C", self.root).stdout)
        misplaced = run_cli("status", "--runs-dir", "records", cwd=self.root)
        self.assertEqual(misplaced.returncode, 2)
        self.assertIn("put it before the command, as in `runner --runs-dir DIR status ...`", misplaced.stderr)
        # The option (or the variable) wins over the workflow, and is not remembered.
        res = run_cli("--runs-dir", "elsewhere", "start", wf)
        self.assertEqual(res.returncode, 255, res.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "elsewhere", "demo")))
        self.assertEqual(subprocess.run(["git", "config", "--get", "codesmith.runsdir"], cwd=self.root)
                         .returncode, 1)
        self.assertEqual(run_cli("--runs-dir", "elsewhere", "status", cwd=self.root).returncode, 0)

    def test_runs_lists_every_run_without_a_workflow(self):
        """run: runner runs with no workflow lists every run in the runs directory (RUN-38)"""
        other = self.write("other.toml", self.RUN_WORKFLOW.replace('"demo"', '"other"'))
        self.commit()
        first = self.start()
        git(self.root, "checkout", "-q", "main")
        second = re.search(r"^run (\S+)", run_cli("start", other).stdout, re.M).group(1)
        listed = run_cli("runs", "-C", self.root)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(first, listed.stdout)
        self.assertIn(second, listed.stdout)
        self.assertNotIn(second, run_cli("runs", "demo", "-C", self.root).stdout)

    def test_status_watch_and_open(self):
        """run: status --watch and --open (RUN-36): `--watch N` prints the page again when it
        changes and ends cleanly on Ctrl-C or SIGTERM; `--open` opens STATUS.html or prints its path"""
        import time
        from unittest import mock
        import contextlib
        import io
        from codesmith import cli
        name = self.start()
        run_dir = os.path.join(self.root, ".runs", "demo", name)
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig=sig):
                # A test started in the background by a shell inherits SIGINT ignored, and Python
                # then installs no Ctrl-C handler; a terminal's runner has the default one.
                watch = subprocess.Popen([sys.executable, RUNNER, "status", "--watch", "0.2", "-C", self.root],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
                time.sleep(1.5)
                with open(os.path.join(run_dir, "STATUS.md"), "a", encoding="utf-8") as fh:
                    fh.write("a line written while watching\n")
                time.sleep(1.0)
                watch.send_signal(sig)
                out, err = watch.communicate(timeout=20)
                self.assertEqual(watch.returncode, 0, err)
                self.assertEqual(out.count("# demo — run"), 2, out)          # printed, then again
                self.assertIn("=" * 72 + "\n", out)
                self.assertIn("a line written while watching", out)
        self.assertEqual(run_cli("status", "--watch", "0", "-C", self.root).returncode, 2)
        html_page = os.path.join(run_dir, "STATUS.html")
        for opener, said in ((["true"], f"opened {html_page}\n"), (None, html_page + "\n"),
                             (["false"], html_page + "\n")):
            with self.subTest(opener=opener), mock.patch.object(cli, "_opener", return_value=opener), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["status", "--open", "-C", self.root]), 0)
            self.assertEqual(out.getvalue(), said)
        self.assertTrue(os.path.isfile(html_page))

    def test_an_old_python_gets_one_sentence(self):
        """cli: an older Python is refused before any import that needs 3.11 (RUN-39)"""
        code = ("import sys; sys.version_info = (3, 9, 6); sys.argv = ['runner', '--version']; "
                f"exec(compile(open({RUNNER!r}).read(), {RUNNER!r}, 'exec'), {{'__name__': '__main__'}})")
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual((res.returncode, res.stderr), (2, "code_smith needs Python 3.11 or newer (found 3.9.6)\n"))
        for old in ("/usr/bin/python3", shutil.which("python3.9") or "", shutil.which("python3.10") or ""):
            if old and os.path.exists(old):
                probe = subprocess.run([old, "-c", "import sys; print(sys.version_info < (3, 11))"],
                                       capture_output=True, text=True)
                if probe.stdout.strip() == "True":                      # a real old Python here
                    res = subprocess.run([old, RUNNER, "--version"], capture_output=True, text=True)
                    self.assertEqual(res.returncode, 2)
                    self.assertTrue(res.stderr.startswith("code_smith needs Python 3.11 or newer (found 3."))

    def test_the_run_branch_is_checked_out(self):
        """git: the run branch is checked out (GIT-14)"""
        before = gitout(self.root, "rev-parse", "HEAD^{tree}")
        name = self.start()
        info = self.run_info(name)
        self.assertEqual(info["branch"], f"run/demo-{info['run_id'][:8]}")
        self.assertEqual(info["original_branch"], "main")
        self.assertEqual(gitout(self.root, "symbolic-ref", "--short", "HEAD"), info["branch"])
        self.assertEqual(gitout(self.root, "rev-parse", "HEAD^{tree}"), before)
        self.assertEqual(gitout(self.root, "status", "--porcelain"), "")
        self.assertEqual(info["base_commit"], gitout(self.root, "rev-parse", "main"))

    def test_no_dirty_starts(self):
        """run: no dirty starts (RUN-02)"""
        self.write("src/a.py", "a = 1\n")
        self.commit()
        self.write("src/a.py", "a = 2\n")
        git(self.root, "add", "src/a.py")
        self.write("src/a.py", "a = 3\n")
        status = gitout(self.root, "status", "--porcelain")
        index = gitout(self.root, "ls-files", "--stage")
        res = run_cli("start", self.wf)
        self.assertEqual(res.returncode, 2)
        self.assertIn("not clean", res.stderr)
        self.assertIn("src/a.py", res.stderr)
        self.assertEqual(gitout(self.root, "status", "--porcelain"), status)
        self.assertEqual(gitout(self.root, "ls-files", "--stage"), index)
        with open(os.path.join(self.root, "src/a.py")) as fh:
            self.assertEqual(fh.read(), "a = 3\n")
        self.assertEqual(gitout(self.root, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".runs")))
        self.assertNotIn("dirty", run_cli("start", "--help").stdout)      # no option to override

    def test_current_branch_option(self):
        """git: current branch option (GIT-05)"""
        self.wf = self.write("wf.toml", self.RUN_WORKFLOW.replace('name = "demo"',
                                                          'name = "demo"\n[defaults]\nbranch = "current"'))
        self.commit()
        name = self.start()
        info = self.run_info(name)
        self.assertEqual((info["branch"], info["original_branch"]), ("main", "main"))
        self.assertEqual(gitout(self.root, "branch", "--format=%(refname:short)"), "main")
        git(self.root, "checkout", "-q", "--detach")
        res = run_cli("start", self.wf)
        self.assertEqual(res.returncode, 2)
        self.assertIn("detached", res.stderr)

    def test_a_held_lock_refuses_a_second_runner(self):
        """run: lock (RUN-07, at the command line)"""
        from codesmith import record
        runs = record.ensure_runs_dir(self.root)
        lock = record.Lock(runs).acquire("someone-else")
        try:
            res = run_cli("start", self.wf)
            self.assertEqual(res.returncode, 2)
            self.assertIn("another runner holds this repository", res.stderr)
            self.assertEqual(gitout(self.root, "symbolic-ref", "--short", "HEAD"), "main")
        finally:
            lock.release()

    def test_status_and_runs(self):
        name = self.start()
        res = run_cli("status", "-C", self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("| 010 | design | human |  | waiting_human |", res.stdout)
        info = self.run_info(name)
        def page(out):                       # the first line holds the clock's age figure
            return out.split("\n", 1)[1]
        for ref in (name, info["run_id"][:6], "latest"):
            self.assertEqual(page(run_cli("status", ref, "-C", self.root).stdout), page(res.stdout))
        self.assertEqual(run_cli("status", "nope", "-C", self.root).returncode, 2)

        status_md = os.path.join(self.root, ".runs", "demo", name, "STATUS.md")
        os.unlink(status_md)
        rebuilt = run_cli("status", name, "--rebuild", "-C", self.root)
        self.assertEqual(page(rebuilt.stdout), page(res.stdout))
        self.assertTrue(os.path.exists(status_md))

        listed = run_cli("runs", self.wf)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(name, listed.stdout)
        self.assertIn("needs_human", listed.stdout)
        self.assertIn(name, run_cli("runs", "demo", "-C", self.root).stdout)

    def test_status_never_rewrites_a_run_a_runner_is_working_on(self):
        """run: `status --rebuild` rewrites findings.json and integrity.json, so it is refused
        while a runner holds this run, and a plain `status` then only reads"""
        from codesmith import record
        name = self.start()
        run_dir = os.path.join(self.root, ".runs", "demo", name)
        lock = record.Lock(os.path.join(self.root, ".runs")).acquire(self.run_info(name)["run_id"])
        try:
            with open(os.path.join(run_dir, "integrity.json"), "rb") as fh:
                before = fh.read()
            res = run_cli("status", name, "--rebuild", "-C", self.root)
            self.assertEqual(res.returncode, 2)
            self.assertIn("--rebuild is refused while it works", res.stderr)
            os.unlink(os.path.join(run_dir, "STATUS.md"))
            res = run_cli("status", name, "-C", self.root)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("| 010 | design | human |  | waiting_human |", res.stdout)
            self.assertFalse(os.path.exists(os.path.join(run_dir, "STATUS.md")))   # printed only
            with open(os.path.join(run_dir, "integrity.json"), "rb") as fh:
                self.assertEqual(fh.read(), before)
        finally:
            lock.release()
        self.assertEqual(run_cli("status", name, "--rebuild", "-C", self.root).returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(run_dir, "STATUS.md")))

    def test_pause_leaves_the_record_to_a_runner_that_took_the_lock(self):
        """run: once the paused runner is gone, `pause` rewrites STATUS.md only under the lock, so
        never beside a runner that a resume started at once"""
        import contextlib
        import io
        from unittest import mock
        from codesmith import cli, record
        name = self.start()
        lock = record.Lock(os.path.join(self.root, ".runs")).acquire(self.run_info(name)["run_id"])
        alive = iter([True])                     # working when asked; gone at the first wait
        try:
            with mock.patch.object(record, "is_alive", lambda identity: next(alive, False)), \
                    mock.patch.object(record.Run, "regenerate") as regenerate, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(["pause", name, "-C", self.root]), 0)
        finally:
            lock.release()
        regenerate.assert_not_called()
        self.assertIn(f"Continue with: runner resume {name}", err.getvalue())

    def test_prune(self):
        """run: prune (RUN-17)"""
        from codesmith import gitops, record
        done, deleted, unfinished = self.start(), self.start(), self.start()
        g = gitops.Git(self.root)
        tree = g.tree_of("HEAD")
        for name in (done, deleted, unfinished):
            g.pin(name, "design/base", tree)
        run = record.Run.load(os.path.join(self.root, ".runs", "demo", done))
        run.state["status"] = "done"
        run.save()
        shutil.rmtree(os.path.join(self.root, ".runs", "demo", deleted))
        res = run_cli("prune", "-C", self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(g.pinned_runs(), sorted([deleted, unfinished]))
        self.assertIn(f"pruned  {done}", res.stdout)
        self.assertIn(f"kept    {deleted}: no run directory", res.stdout)
        self.assertIn(f"kept    {unfinished}", res.stdout)
        res = run_cli("prune", "--orphans", "-C", self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(g.pinned_runs(), [unfinished])
        self.assertIn(f"pruned  {deleted}", res.stdout)

    def test_prune_keeps_runs_under_another_runs_dir(self):
        """run: plain prune keeps the refs of a run whose directory is in a different runs dir"""
        from codesmith import gitops
        name = self.start()
        g = gitops.Git(self.root)
        g.pin(name, "design/base", g.tree_of("HEAD"))
        os.rename(os.path.join(self.root, ".runs"), os.path.join(self.root, "elsewhere"))
        os.makedirs(os.path.join(self.root, ".runs"))
        res = run_cli("prune", "-C", self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(g.pinned_runs(), [name])
        self.assertIn("--orphans", res.stdout)

    def test_unwritable_output_is_an_error_not_a_traceback(self):
        """cli: an OSError ends as `runner: ...` with exit 2, not a traceback"""
        res = run_cli("graph", os.path.join(self.root, "wf.toml"), "-o", "/nonexistent-dir/x.dot")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertTrue(res.stderr.startswith("runner: "), res.stderr)
        self.assertNotIn("Traceback", res.stderr)



class RetryLines(EngineCase):
    """`runner retry` says which of three things it did: the set-aside work queued to be put
    back, a clean start that leaves the set-aside work in failed.patch, or a plain clean start."""

    TASK = """
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt say good."
outputs = ["src/a.txt"]
writes = ["src/**"]
max_attempts = 1
gate = ["grep -q good src/a.txt"]
"""

    def retry(self, *flags):
        res = run_cli("retry", "latest", "make", *flags, "-C", self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_retry_names_what_it_queued(self):
        self.workflow(self.TASK)
        self.script([{"write": {"src/a.txt": "bad\n"}, "answer": done()}])
        self.assertEqual(self.start(), 2)
        name = self.the_run().name
        self.assertEqual(self.retry("--apply-patch"),
                         "make: fresh attempts, continuing from the set-aside work of attempt 1. "
                         f"Continue with: runner resume {name}\n")
        # Retried again without the flag: the queue is cancelled, and the line says so.
        self.assertEqual(self.retry(),
                         "make: fresh attempts, starting clean. The set-aside work of attempt 1 "
                         "stays in failed.patch and will not be put back. Continue with: runner "
                         f"resume {name}\n")
        self.assertNotIn("recover", self.the_run().state["tasks"]["make"])

    def test_retry_without_set_aside_work_starts_clean(self):
        self.workflow(self.TASK)
        self.script([{"answer": {"outcome": "blocked", "summary": "", "responses": [],
                                 "blocked_reason": "Nothing to do."}}])
        self.assertEqual(self.start(), 255)
        self.assertEqual(self.retry(), "make: fresh attempts, starting clean. Continue with: "
                                       f"runner resume {self.the_run().name}\n")

    def test_a_refused_retry_prints_the_refusal_only(self):
        self.workflow(self.TASK)
        self.script([{"write": {"src/a.txt": "bad\n"}, "answer": done()}])
        self.assertEqual(self.start(), 2)
        self.write("src/a.txt", "the owner's own\n")
        self.commit()
        res = run_cli("retry", "latest", "make", "--apply-patch", "-C", self.root)
        self.assertEqual(res.returncode, 2)
        self.assertEqual(res.stdout, "")
        self.assertIn("these paths changed since it was set aside: src/a.txt", res.stderr)


class OpenRun(EngineCase):
    def test_a_changing_command_reads_the_state_under_the_lock(self):
        """run: `resume` (and every command that changes a run) takes the lock before it reads
        state.json, so it never acts on a state a runner was still changing"""
        from unittest import mock
        from codesmith import record
        self.workflow('[[task]]\nid = "sign"\ntype = "human"\n')
        self.assertEqual(self.start(), 255, self.output)
        run = self.the_run()
        real, holders = record.Run.load.__func__, []

        def load(cls, path):
            holders.append((record.Lock(os.path.dirname(os.path.dirname(path))).holder() or {})
                           .get("run_id"))
            return real(cls, path)
        with mock.patch.object(record.Run, "load", classmethod(load)):
            self.assertEqual(self.runner("resume", run.name, "-C", self.root), 255, self.output)
        self.assertEqual(holders[0], run.state["run_id"])

class Doctor(EngineCase):
    def test_a_command_agent_gets_no_hook_hint(self):
        """pre: doctor on a command agent expects no hook activity (PRE-12): it never sends the
        owner to check hook configuration, which only agents with hooks have"""
        self.workflow('[[task]]\nid = "make"\ntype = "implement"\noutputs = ["src/a.txt"]\ngate = ["true"]\n')
        self.assertEqual(self.runner("doctor", self.wf_path), 0, self.output)
        self.assertIn("observed activity: none expected (a command agent has no hooks)", self.output)
        self.assertNotIn("hook configuration", self.output)


if __name__ == "__main__":
    unittest.main()
