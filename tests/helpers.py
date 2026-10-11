"""Shared test helpers: a scratch git repository holding a workflow file."""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from codesmith import workflow  # noqa: E402

RUNNER = os.path.normpath(os.path.join(HERE, "..", "runner"))
# Every save checks the run state's invariants (`invariants.py`); under the suite a broken one
# raises instead of being reported, so every scenario proves they hold. Inherited by the runner
# processes the tests start.
os.environ["CODE_SMITH_STRICT_INVARIANTS"] = "1"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class RepoCase(unittest.TestCase):
    """Each test gets an empty committed git repository in `self.root`."""

    def setUp(self):
        # The suite makes scratch runs in its own repositories: an owner's `--runs-dir` (or the
        # variable exported for it) must never redirect them into a real record.
        previous = os.environ.pop("CODE_SMITH_RUNS_DIR", None)
        if previous is not None:
            self.addCleanup(os.environ.__setitem__, "CODE_SMITH_RUNS_DIR", previous)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        self.write("README.md", "scratch\n")
        self.commit()

    def write(self, rel, text):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        return full

    def commit(self):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "c", "--allow-empty")

    def load(self, text, rel="workflow.toml"):
        return workflow.load(self.write(rel, text))

    def assertError(self, wf, *fragments):
        for e in wf.errors:
            if all(f in e for f in fragments):
                return e
        self.fail(f"no error containing {fragments!r} in:\n  " + "\n  ".join(wf.errors or ["(none)"]))

    def assertWarning(self, wf, *fragments):
        for w in wf.warnings:
            if all(f in w for f in fragments):
                return w
        self.fail(f"no warning containing {fragments!r} in:\n  " + "\n  ".join(wf.warnings or ["(none)"]))

    def assertLoads(self, wf):
        self.assertEqual(wf.errors, [])


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, RUNNER, *args], cwd=cwd, capture_output=True, text=True)


# -- driving the engine with the scripted agent ----------------------------------------------------

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402

FAKE_AGENT = os.path.join(HERE, "fake_agent.py")


def done(summary="Made it.", responses=()):
    return {"outcome": "done", "summary": summary, "blocked_reason": "",
            "responses": list(responses)}


class EngineCase(RepoCase):
    """A scratch repository, a workflow whose agent is the scripted one, and the runner driven in
    process through its command line."""

    HEADER = ('name = "demo"\n[defaults]\nagent = "fake"\n{defaults}\n'
              '[agents.fake]\nargv = [{python!r}, {agent!r}]\n')

    def setUp(self):
        super().setUp()
        from codesmith import cli
        self.cli = cli
        self._side = tempfile.TemporaryDirectory()
        self.addCleanup(self._side.cleanup)
        self.side = os.path.realpath(self._side.name)
        self.script_path = os.path.join(self.side, "script.json")
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)
        os.environ["FAKE_AGENT_SCRIPT"] = self.script_path
        self.addCleanup(setattr, cli, "CRASH", None)
        self.last = None

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def workflow(self, tasks, defaults=""):
        header = self.HEADER.format(defaults=defaults, python=sys.executable, agent=FAKE_AGENT)
        self.wf_path = self.write("wf.toml", header.replace("'", '"') + tasks)
        self.commit()
        return self.wf_path

    def script(self, steps):
        with open(self.script_path, "w", encoding="utf-8") as fh:
            json.dump(steps, fh)
        if os.path.exists(self.script_path + ".counter"):
            os.unlink(self.script_path + ".counter")

    def prompt(self, n):
        with open(os.path.join(self.script_path + ".prompts", f"{n}.md"), encoding="utf-8") as fh:
            return fh.read()

    def calls(self):
        try:
            with open(self.script_path + ".counter", encoding="utf-8") as fh:
                return int(fh.read())
        except FileNotFoundError:
            return 0

    def runner(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli.main(list(args))
        self.last = (code, out.getvalue(), err.getvalue())
        return code

    def start(self):
        return self.runner("start", self.wf_path)

    def resume(self, *extra):
        return self.runner("resume", "-C", self.root, *extra)

    @property
    def output(self):
        return self.last[1] + self.last[2]

    def the_run(self):
        from codesmith import record
        return record.Run.load(record.resolve_run(os.path.join(self.root, ".runs")))

    def status(self, task):
        return self.the_run().state["tasks"][task]["status"]

    def task_file(self, task, *parts):
        run = self.the_run()
        return os.path.join(run.task_dir(task), *parts)

    def read_json(self, task, *parts):
        with open(self.task_file(task, *parts), encoding="utf-8") as fh:
            return json.load(fh)

    def git_out(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True,
                              text=True).stdout.strip()

    def check_invariants(self):
        """Every accepted commit is exactly the verified candidate, and nothing else is ever in
        the tree when a transaction ends."""
        from codesmith import gitops
        run = self.the_run()
        g = gitops.Git(self.root)
        active = run.state.get("active_producer")
        for tid, st in run.state["tasks"].items():
            if st["kind"] != "produce":
                continue
            if st["status"] == "accepted":
                self.assertEqual(g.tree_of(st["commit"]), st["candidate"], tid)
                verification = self.read_json(tid, os.path.basename(st["attempt_dir"]),
                                              "verification.json")
                self.assertEqual({r["candidate"] for r in verification["results"]}
                                 | {st["candidate"]}, {st["candidate"]}, tid)
            if st["status"] in ("failed", "blocked"):
                self.assertTrue(os.path.exists(self.task_file(tid, "failed.patch")), tid)
            lease = st.get("lease")
            if lease and st["status"] == "accepted":
                # Parent ownership (W-01): the commit sits on the transaction's starting commit.
                self.assertEqual(self.git_out("rev-parse", st["commit"] + "^"), lease["commit"], tid)
            if lease and tid == active:
                self.assertEqual(g.head(), lease["commit"], "HEAD during the transaction")
        # Branch ownership (W-01): the run's branch is checked out and holds every acceptance.
        self.assertEqual(g.head_ref(), "refs/heads/" + run.info["branch"])
        for tid, st in run.state["tasks"].items():
            if st["kind"] == "produce" and st["status"] == "accepted":
                self.assertEqual(subprocess.run(["git", "merge-base", "--is-ancestor", st["commit"],
                                                 "HEAD"], cwd=self.root).returncode, 0, tid)
        tree = g.snapshot(os.path.join(self.side, "check-index"))
        if active:
            self.assertEqual(tree, run.state["tasks"][active]["candidate"], "the held candidate")
        else:
            self.assertEqual(self.git_out("status", "--porcelain"), "")
            self.assertEqual(tree, g.tree_of("HEAD"))
            self.assertEqual(run.state["intents"], [])
