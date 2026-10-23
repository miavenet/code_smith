#!/usr/bin/env python3
"""A fault-injection drill: the runner's failure paths, run through the command line on a real
Git repository with the scripted agents. No model or network call is made.

Each drill builds its own scratch repository with a small workflow (two producers, `alpha` and
`beta`, each with one reviewer, and shell gates), injects one fault, drives `runner` as a
subprocess, and reads the record (events.jsonl, state.json, STATUS.md, exit codes) for the
evidence. One line per drill; the exit status is 1 when any drill fails.

    python3 tools/fault_drill.py            # every drill
    python3 tools/fault_drill.py 2 6        # some of them
    python3 tools/fault_drill.py --keep     # keep the scratch repositories and print where
"""

import glob
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TR = os.path.dirname(HERE)
RUNNER = os.path.join(TR, "runner")
TESTS = os.path.join(TR, "tests")
SRC = os.path.join(TR, "src")
PY = sys.executable

DONE = {"outcome": "done", "summary": "Made it.", "blocked_reason": "", "responses": []}
PASS = {"verdict": "pass", "summary": "Reviewed.", "findings": [], "resolutions": []}
REVIEWERS = {"alpha": "alpha.review.principled-priya", "beta": "beta.review.principled-priya"}


class DrillFailed(Exception):
    pass


def expect(condition, what):
    if not condition:
        raise DrillFailed(what)


def wait_for(condition, what, timeout_s=30):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise DrillFailed(f"timed out waiting for {what}")


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie is dead for our purposes; `ps` says so where kill(0) cannot.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(state.strip()) and not state.strip().startswith("Z")


class Scratch:
    """A scratch repository with a workflow of two reviewed producers, the scripted agent as
    every author and reviewer, and the runner as a subprocess."""

    def __init__(self, name, keep=False):
        self.keep = keep
        self.base = os.path.realpath(tempfile.mkdtemp(prefix=f"fault-drill-{name}-"))
        self.root = os.path.join(self.base, "repo")
        self.side = os.path.join(self.base, "side")
        os.makedirs(self.root)
        os.makedirs(self.side)
        self.script_path = os.path.join(self.side, "script.json")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CODE_SMITH_", "GIT_", "FAKE_"))}
        self.env.update(FAKE_AGENT_SCRIPT=self.script_path, CODE_SMITH_STRICT_INVARIANTS="1",
                        PYTHONPATH=TESTS)       # fake_agent imports probe_agent beside it
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "drill@example.invalid")
        self.git("config", "user.name", "Drill")
        self.write("README.md", "scratch\n")

    def close(self):
        if not self.keep:
            shutil.rmtree(self.base, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True,
                              text=True).stdout

    def write(self, rel, text, base=None):
        path = os.path.join(base or self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def marker(self, name):
        return os.path.join(self.side, name)

    def workflow(self, gates=None, defaults="", agents="", extra_tasks="", reviewer=None):
        """alpha, then beta, each with one reviewer; `gates` overrides a producer's gate list."""
        gates = gates or {}
        reviewer = reviewer or '"principled-priya"'
        text = (f'name = "drill"\n[defaults]\nagent = "fake"\n{defaults}\n'
                f'[agents.fake]\nargv = [{json.dumps(PY)}, {json.dumps(os.path.join(TESTS, "fake_agent.py"))}]\n'
                'read_only_args = ["--read-only"]\n' + agents)
        for tid, needs in (("alpha", ""), ("beta", 'needs = ["alpha"]\n')):
            gate = gates.get(tid, [f"test -s src/{tid}.txt"])
            text += (f'\n[[task]]\nid = "{tid}"\ntype = "implement"\n{needs}'
                     f'prompt = "Write src/{tid}.txt."\noutputs = ["src/{tid}.txt"]\n'
                     f'gate = {json.dumps(gate)}\nreviewers = [{reviewer}]\n')
        self.write("wf.toml", text + extra_tasks)
        self.git("add", "-A")
        self.git("commit", "-qm", "drill workflow")

    def script(self, alpha=None, beta=None, **more):
        steps = {"alpha": alpha or [self.produce("alpha")], "beta": beta or [self.produce("beta")],
                 REVIEWERS["alpha"]: [{"answer": PASS}] * 3, REVIEWERS["beta"]: [{"answer": PASS}] * 3}
        steps.update(more)
        with open(self.script_path, "w", encoding="utf-8") as fh:
            json.dump(steps, fh)

    @staticmethod
    def produce(tid):
        return {"write": {f"src/{tid}.txt": f"{tid}\n"}, "answer": DONE}

    def calls(self, tid):
        try:
            with open(f"{self.script_path}.{tid}.counter", encoding="utf-8") as fh:
                return int(fh.read())
        except FileNotFoundError:
            return 0

    # -- the runner -----------------------------------------------------------------------

    def runner(self, *args, env=None):
        res = subprocess.run([PY, RUNNER, *args], cwd=self.root, capture_output=True, text=True,
                             env=env or self.env, timeout=300)
        return res.returncode, res.stdout + res.stderr

    def spawn(self, *args, env=None):
        log = open(os.path.join(self.side, f"runner-{time.monotonic_ns()}.log"), "w+")
        child = subprocess.Popen([PY, RUNNER, *args], cwd=self.root, stdout=log,
                                 stderr=subprocess.STDOUT, env=env or self.env)
        child.log = log
        return child

    @staticmethod
    def finish(child, timeout_s=120):
        code = child.wait(timeout=timeout_s)
        child.log.seek(0)
        return code, child.log.read()

    # -- the record -----------------------------------------------------------------------

    def run_dir(self):
        found = glob.glob(os.path.join(self.root, ".runs", "drill", "drill-*"))
        expect(len(found) == 1, f"one run directory, found {found}")
        return found[0]

    def state(self):
        with open(os.path.join(self.run_dir(), "state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def try_state(self):
        try:
            return self.state()
        except (OSError, ValueError, DrillFailed):
            return None

    def events(self, name=None):
        with open(os.path.join(self.run_dir(), "events.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [r for r in rows if name is None or r["event"] == name]

    def status_md(self):
        with open(os.path.join(self.run_dir(), "STATUS.md"), encoding="utf-8") as fh:
            return fh.read()

    def invocations(self, tid, attempt=1):
        st = self.state()["tasks"][tid]
        return sorted(os.path.basename(p) for p in glob.glob(os.path.join(
            self.run_dir(), "tasks", st["dir"], f"attempt-{attempt}", "invocation-*")))

    def attempts(self, tid):
        st = self.state()["tasks"][tid]
        return sorted(os.path.basename(p) for p in glob.glob(os.path.join(
            self.run_dir(), "tasks", st["dir"], "attempt-*")))

    def agent_intent(self, task):
        state = self.try_state() or {"intents": []}
        for it in state["intents"]:
            if it["kind"] == "agent" and it.get("task") == task and (it.get("process") or {}).get("pgid"):
                return it
        return None

    def assert_done(self, code, out, what):
        expect(code == 0, f"{what}: exit {code}, expected 0: {out.strip()[-600:]}")
        state = self.state()
        expect(state["status"] == "done", f"{what}: run status {state['status']}")
        for tid in ("alpha", "beta"):
            expect(state["tasks"][tid]["status"] == "accepted",
                   f"{what}: {tid} is {state['tasks'][tid]['status']}")
        expect(self.git("status", "--porcelain").strip() == "", f"{what}: the work tree is not clean")
        expect(".runs/" not in self.git("log", "--all", "--name-only", "--format="),
               f"{what}: a commit carries a .runs/ path")


# -- the drills ---------------------------------------------------------------------------------

def drill_kill(s):
    """1. SIGKILL of the runner while an author call is in flight."""
    started = s.marker("alpha-call-1")
    s.workflow()
    s.script(alpha=[{"run": ["touch " + shlex.quote(started)], "sleep_s": 3, "answer": DONE},
                    s.produce("alpha")])
    child = s.spawn("start", "wf.toml")
    wait_for(lambda: os.path.exists(started) and s.agent_intent("alpha"), "the author call")
    pgid = s.agent_intent("alpha")["process"]["pgid"]
    child.send_signal(signal.SIGKILL)
    code, _ = s.finish(child)
    expect(code == -signal.SIGKILL, f"the runner died of SIGKILL (exit {code})")
    wait_for(lambda: not group_alive(pgid), "the orphaned call to end on its own")
    code, out = s.runner("resume", "-C", s.root)
    s.assert_done(code, out, "resume")
    interrupted = s.events("call-interrupted")
    expect([(e["task"], e["count"]) for e in interrupted] == [("alpha", 1)],
           f"one call-interrupted event for alpha, found {interrupted}")
    st = s.state()["tasks"]["alpha"]
    expect(st["attempts_used"] == 1, f"attempts_used {st['attempts_used']}")
    expect(s.attempts("alpha") == ["attempt-1"], f"attempt directories {s.attempts('alpha')}")
    invs = s.invocations("alpha")
    expect(invs == ["invocation-1", "invocation-2"], f"alpha attempt-1 invocations {invs}")
    with open(os.path.join(s.run_dir(), "tasks", st["dir"], "attempt-1", "invocation-1",
                           "outcome.json"), encoding="utf-8") as fh:
        first = json.load(fh)
    expect(first["status"] == "interrupted", f"invocation-1 outcome {first['status']}")
    with open(os.path.join(s.run_dir(), "tasks", st["dir"], "attempt-1", "result.json"),
              encoding="utf-8") as fh:
        result = json.load(fh)
    expect(s.calls("alpha") == 2, f"alpha author calls {s.calls('alpha')}")
    return (f"exit -9 then resume 0; call-interrupted(alpha, count 1); attempts_used 1, one "
            f"attempt dir with {', '.join(invs)} (invocation-1 interrupted); result.json "
            f"agent_calls={result.get('agent_calls')}; run done")


STUBBORN = '''import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)
with open(sys.argv[1], "w") as fh:
    fh.write(str(os.getpid()))
time.sleep(600)
'''


def drill_orphan(s):
    """2. The same, with the agent's child alive after the runner dies and ignoring SIGTERM."""
    stubborn = s.write("stubborn.py", STUBBORN, base=s.side)
    pidfile = s.marker("stubborn.pid")
    s.workflow()
    s.script(alpha=[{"run": [f"{shlex.quote(PY)} {shlex.quote(stubborn)} {shlex.quote(pidfile)} &"],
                     "hang": True},
                    s.produce("alpha")])
    child = s.spawn("start", "wf.toml")
    wait_for(lambda: os.path.exists(pidfile) and os.path.getsize(pidfile) and s.agent_intent("alpha"),
             "the author call and its child")
    intent = s.agent_intent("alpha")
    pgid, invocation = intent["process"]["pgid"], intent["invocation_dir"]
    with open(pidfile, encoding="utf-8") as fh:
        orphan = int(fh.read())
    child.send_signal(signal.SIGKILL)
    s.finish(child)
    try:
        expect(pid_alive(orphan), "the child outlived the runner")
        code, out = s.runner("resume", "-C", s.root)
        expect(code == 2, f"resume beside the orphan exits 2, got {code}: {out.strip()[-400:]}")
        expect("still running" in out and invocation in out and str(orphan) in out,
               f"the refusal names the invocation {invocation} and pid {orphan}: {out.strip()[-500:]}")
        expect(pid_alive(orphan), "the refusal left the child alone")
        refused = out.strip().splitlines()[-1]
        named = refused[refused.find("(invocation"):refused.find(")", refused.find("(invocation")) + 1]
        before = time.monotonic()
        code, out = s.runner("resume", "--stop-orphans", "-C", s.root)
        took = time.monotonic() - before
        expect(not pid_alive(orphan), "--stop-orphans stopped the SIGTERM-proof child")
        expect(not group_alive(pgid), "--stop-orphans stopped the whole group")
        s.assert_done(code, out, "resume --stop-orphans")
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    interrupted = [(e["task"], e["count"]) for e in s.events("call-interrupted")]
    expect(interrupted == [("alpha", 1)], f"call-interrupted events {interrupted}")
    expect(s.state()["tasks"]["alpha"]["attempts_used"] == 1, "alpha used one attempt")
    return (f"resume 2, \"still running {named}\"; resume --stop-orphans 0 in {took:.1f} s, pid "
            f"{orphan} (ignores SIGTERM) and group {pgid} gone; call-interrupted(alpha, 1); run done")


def drill_git_clean(s):
    """3. A gate that runs `git clean -fdx` removes the record; a second runner is refused."""
    once = s.marker("cleaned")
    gate = (f"test -s src/alpha.txt && if test ! -e {shlex.quote(once)}; then touch {shlex.quote(once)}"
            " && git clean -fdxq && sleep 4; fi")
    s.workflow(gates={"alpha": [gate]})
    s.script()
    child = s.spawn("start", "wf.toml")
    wait_for(lambda: os.path.exists(once), "the cleaning gate")
    wait_for(lambda: not os.path.exists(os.path.join(s.root, ".runs")), "git clean to remove .runs")
    second_code, second = s.runner("resume", "-C", s.root)
    third_code, third = s.runner("start", "wf.toml")
    code, out = s.finish(child)
    findings = [f"first runner exit {code}"]
    expect(second_code == 2, f"resume during the gate fails: {second_code} {second.strip()[-300:]}")
    expect(third_code == 2 and "another runner holds" in third,
           f"a second runner (start) during the gate is refused: {third_code} {third.strip()[-300:]}")
    expect(code == 2 and "repair-record" in out, f"the run stops naming repair-record: {out.strip()[-600:]}")
    stop = [line for line in out.splitlines() if "repair-record" in line][0]
    code, dry = s.runner("repair-record", "--dry-run", "-C", s.root)
    expect(code == 0 and "Nothing was changed" in dry, f"repair-record --dry-run: {code} {dry.strip()[-400:]}")
    would = sum(line.startswith("would restore") for line in dry.splitlines())
    lost = sum(line.startswith("lost") for line in dry.splitlines())
    code, rep = s.runner("repair-record", "-C", s.root)
    expect(code == 0 and "Continue with: runner resume" in rep, f"repair-record: {code} {rep.strip()[-400:]}")
    restored = sum(line.startswith("restored") for line in rep.splitlines())
    expect(restored == would, f"repair restored {restored}, the dry run said {would}")
    code, out = s.runner("resume", "-C", s.root)
    s.assert_done(code, out, "resume after repair")
    names = {e["event"] for e in s.events()}
    expect("record-repaired" in names, "a record-repaired event")
    stop, second, third = (x.replace(s.root, "<repo>").replace(s.base, "<scratch>")
                           for x in (stop, second, third))
    findings.append(f"stop: \"{stop.strip()[:260]}\"")
    return ("; ".join(findings) + f"; meanwhile resume exit 2 \"{second.strip()[:110]}...\", start exit 2 "
            f"\"{third.strip().splitlines()[-1][:110]}\"; dry run: {would} would restore, {lost} lost; repair restored "
            f"{restored}; resume done; record-repaired event")


def drill_ignore(s):
    """4. A gate that deletes .runs/.gitignore."""
    s.workflow(gates={"alpha": ["test -s src/alpha.txt && rm -f .runs/.gitignore"]})
    s.script()
    code, out = s.runner("start", "wf.toml")
    s.assert_done(code, out, "start")
    restored = s.events("record-ignore-restored")
    expect(restored, "a record-ignore-restored event")
    expect(os.path.exists(os.path.join(s.root, ".runs", ".gitignore")), ".runs/.gitignore is back")
    commits = s.git("log", "--format=%h", "main..HEAD").split()
    return (f"start 0; {len(restored)} record-ignore-restored event(s) "
            f"({', '.join(sorted({json.dumps({k: v for k, v in e.items() if k in ('was', 'path', 'seen')}) for e in restored}))}); "
            f"{len(commits)} run commits, none touches .runs/; .runs/.gitignore present")


def drill_frozen(s):
    """5. A frozen library file edited while the run waits for a person."""
    s.workflow(extra_tasks='\n[[task]]\nid = "signoff"\ntype = "human"\nverifies = "beta"\n')
    s.script()
    code, out = s.runner("start", "wf.toml")
    expect(code == 255, f"start waits for the person (255), got {code}: {out.strip()[-400:]}")
    frozen = os.path.join(s.run_dir(), "library", "personas", "principled-priya.toml")
    expect(os.path.exists(frozen), "the frozen persona file exists")
    with open(frozen, "a", encoding="utf-8") as fh:
        fh.write("\n# edited while the run waited\n")
    name = os.path.basename(s.run_dir())
    code, approve = s.runner("approve", name, "signoff", "-C", s.root)
    expect(code == 2 and not s.events('human-decision'), 'approval refuses the changed record')
    code_r, resume = s.runner("resume", "-C", s.root)
    expect(code_r == 2 and "repair-record" in resume,
           f"resume stops on the integrity failure naming repair-record: {code_r} {resume.strip()[-500:]}")
    refusal = [line for line in resume.splitlines() if "repair-record" in line][0]
    code_d, dry = s.runner("repair-record", "--dry-run", "-C", s.root)
    expect(code_d == 0 and "library/personas/principled-priya.toml" in dry,
           f"the dry run names the file: {dry.strip()[-400:]}")
    code_p, rep = s.runner("repair-record", "-C", s.root)
    expect(code_p == 0 and "restored" in rep, f"repair-record: {code_p} {rep.strip()[-400:]}")
    with open(frozen, encoding="utf-8") as fh:
        expect("edited while" not in fh.read(), "the frozen file is back as it was")
    code_a, approved = s.runner('approve', name, 'signoff', '-C', s.root)
    expect(code_a == 0, f'approval after repair: {code_a} {approved[-300:]}')
    code_r2, out = s.runner("resume", "-C", s.root)
    s.assert_done(code_r2, out, "resume after repair")
    expect(s.state()["tasks"]["signoff"]["status"] == "accepted", "signoff accepted")
    decisions = len(s.events("human-decision"))
    return (f"approve on the edited record exit {code}, no decision written; "
            f"resume 2: \"{refusal.strip()[:170]}\"; dry run names the persona file; repaired; "
            f"approved after repair, resume done ({decisions} human-decision event)")


CODEX_SHIM = '''"""A scripted Codex CLI for the drill: the first review call is the fake Codex's "model at
capacity" refusal (FAKE_CODEX_MODE=capacity), later ones answer the review; doctor's probes
are answered by probe_agent in Codex's event format. No network."""
import contextlib, io, json, os, subprocess, sys
sys.path.insert(0, %(tests)r)
from probe_agent import handle_probe

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.160.0 (drill shim)"); sys.exit(0)
if args[:1] in (["sandbox"], ["features"]):
    sys.exit(0)
prompt = sys.stdin.read()

def emit(text, usage):
    print(json.dumps({"type": "thread.started", "thread_id": "0199f1a2-7c3d-7e00-8a11-drillcodex01"}))
    print(json.dumps({"type": "turn.started"}))
    print(json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}}))
    print(json.dumps({"type": "turn.completed", "usage": usage}))

out = io.StringIO()
with contextlib.redirect_stdout(out):
    probed = handle_probe(prompt)
if probed:
    emit(out.getvalue().strip(), {"input_tokens": 10, "output_tokens": 2})
    sys.exit(0)
counter = %(counter)r
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
if n == 0:
    res = subprocess.run([sys.executable, %(fake)r, *args], input=prompt, text=True,
                         env=dict(os.environ, FAKE_CODEX_MODE="capacity"))
    sys.exit(res.returncode)
emit(json.dumps({"verdict": "pass", "summary": "Reviewed.", "findings": [], "resolutions": []}),
     {"input_tokens": 1000, "output_tokens": 100})
'''


def drill_capacity(s):
    """6. A provider refusal before any work, under a token cap."""
    counter = s.marker("codex.counter")
    shim = s.write("codex_shim.py", CODEX_SHIM % {"tests": TESTS, "counter": counter,
                                                  "fake": os.path.join(TESTS, "fake_codex.py")},
                   base=s.side)
    home = s.marker("codex-home")
    os.makedirs(home)
    s.env["CODEX_HOME"] = home
    s.workflow(defaults="run_budget_tokens = 100000\nunpriced_call_reserve = 1000\n",
               agents=f'[agents.cx]\nkind = "codex"\nargv = [{json.dumps(PY)}, {json.dumps(shim)}]\n'
                      'review_mode = "provided_context"\n',
               reviewer='{ perspective = "principled-priya", agent = "cx" }')
    s.script()
    code, out = s.runner("doctor", "wf.toml")
    expect(code == 0, f"doctor qualifies the shim: {code} {out.strip()[-600:]}")
    code, out = s.runner("start", "wf.toml")
    s.assert_done(code, out, "start")
    state = s.state()
    rid = REVIEWERS["alpha"]
    rdir = os.path.join(s.run_dir(), "tasks", state["tasks"][rid]["dir"], "round-1")
    outcomes = []
    for inv in sorted(glob.glob(os.path.join(rdir, "invocation-*"))):
        with open(os.path.join(inv, "outcome.json"), encoding="utf-8") as fh:
            o = json.load(fh)
        outcomes.append((os.path.basename(inv), o["status"], o.get("before_work"), o.get("usage")))
    expect(len(outcomes) == 2, f"two calls of the alpha reviewer, found {outcomes}")
    expect(outcomes[0][1] == "transient" and outcomes[0][2] is True and not outcomes[0][3],
           f"the first is a refusal before work with no usage: {outcomes[0]}")
    expect(outcomes[1][1] == "ok", f"the retry answered: {outcomes[1]}")
    unpriced = state["spend"]["unpriced"]
    with open(counter, encoding="utf-8") as fh:
        shim_calls = int(fh.read())
    answered = shim_calls - 1                       # each answered review call: 1000 in, 100 out
    # The producers are the command fake, which reports no usage: each is held at the reserve.
    # The refusal must be in none of the counts: not held, not a call, no tokens.
    held = [h["invocation"] for h in unpriced.get("held", [])]
    expect(not any("review" in h for h in held), f"no review call is held: {held}")
    expect((unpriced["calls"], unpriced["tokens_in"], unpriced["tokens_out"])
           == (2 + answered, 1000 * answered, 100 * answered),
           f"the refusal is not counted: {unpriced}")
    return (f"doctor 0, start 0; alpha reviewer: {outcomes[0][0]} transient before_work=true "
            f"usage={outcomes[0][3]}, {outcomes[1][0]} ok; {shim_calls} codex review calls, "
            f"{answered} answered; unpriced calls={unpriced['calls']} (2 producers + {answered}), "
            f"tokens {unpriced['tokens_in']}/{unpriced['tokens_out']}, held only the producers' "
            f"unknown-usage calls {held}; run done")


SITECUSTOMIZE = '''import os, sys
# Installed by the fault drill: in the runner process only, the task page renderer raises.
if os.environ.get("DRILL_BREAK_RENDER") and sys.argv and os.path.basename(sys.argv[0]) == "runner":
    sys.path.insert(0, %(src)r)
    from codesmith import record
    def broken(*args, **kwargs):
        raise RuntimeError("drill: the task page renderer is broken")
    record.render_task_status = broken
'''


def drill_render(s):
    """7. A renderer that raises."""
    hook = os.path.join(s.side, "hook")
    s.write("sitecustomize.py", SITECUSTOMIZE % {"src": SRC}, base=hook)
    broken = dict(s.env, DRILL_BREAK_RENDER="1", PYTHONPATH=hook + os.pathsep + TESTS)
    s.workflow()
    s.script()
    code, out = s.runner("start", "wf.toml", env=broken)
    s.assert_done(code, out, "start with a broken renderer")
    failed = s.events("render-failed")
    expect(failed, "render-failed events")
    expect(all(e.get("message") == "drill: the task page renderer is broken" for e in failed),
           f"the events carry the error: {failed[:2]}")
    pages = sorted({e["page"] for e in failed})
    written = sorted(os.path.relpath(p, s.run_dir()) for p in
                     glob.glob(os.path.join(s.run_dir(), "tasks", "*", "STATUS.md")))
    code_b, out_b = s.runner("status", "--rebuild", "-C", s.root, env=broken)
    expect(code_b != 0 and "renderer is broken" in out_b,
           f"status --rebuild reports the error: {code_b} {out_b.strip()[-400:]}")
    last = out_b.strip().splitlines()[-1]
    code_f, out_f = s.runner("status", "--rebuild", "-C", s.root)
    expect(code_f == 0, f"status --rebuild without the fault: {code_f} {out_f.strip()[-300:]}")
    task_page = os.path.join(s.run_dir(), "tasks", s.state()["tasks"]["alpha"]["dir"], "STATUS.md")
    expect(os.path.exists(task_page), "the task page exists after the rebuild")
    return (f"start 0 (done); {len(failed)} render-failed events, pages {pages}; task pages on "
            f"disk after the run: {written or 'none'}; status --rebuild "
            f"with the fault exit {code_b}: \"{last[:140]}\"; without it exit 0 and the task pages "
            "are written")


def drill_pause_now(s):
    """8. `pause --now` three times in one attempt."""
    marks = [s.marker(f"alpha-call-{n}") for n in (1, 2, 3)]
    s.workflow()
    s.script(alpha=[{"run": ["touch " + shlex.quote(m)], "hang": True} for m in marks]
             + [s.produce("alpha")])
    child = s.spawn("start", "wf.toml")
    said = []
    for n, mark in enumerate(marks, 1):
        wait_for(lambda: os.path.exists(mark) and s.agent_intent("alpha"), f"call {n}")
        code, out = s.runner("pause", "--now", "-C", s.root)
        expect(code == 0 and "interrupted" in out, f"pause --now {n}: {code} {out.strip()[-300:]}")
        rc, _ = s.finish(child)
        said.append(rc)
        if n < 3:
            child = s.spawn("resume", "-C", s.root)
    code, out = s.runner("resume", "-C", s.root)
    s.assert_done(code, out, "the last resume")
    interrupted = [(e["task"], e["count"]) for e in s.events("call-interrupted")]
    expect(interrupted == [("alpha", 1), ("alpha", 2), ("alpha", 3)], f"call-interrupted {interrupted}")
    st = s.state()["tasks"]["alpha"]
    expect(st["attempts_used"] == 1 and s.attempts("alpha") == ["attempt-1"],
           f"attempts_used {st['attempts_used']}, {s.attempts('alpha')}")
    invs = s.invocations("alpha")
    expect(len(invs) == 4, f"four calls in attempt-1: {invs}")
    return (f"three pause --now (runner exits {said}); call-interrupted counts 1,2,3; attempts_used 1, "
            f"attempt-1 holds {len(invs)} invocations; run done")


def drill_cleanup_answer(s):
    """9. Kill after saving an answer with a live cleanup obligation."""
    s.workflow()
    step = s.produce('alpha')
    step['run'] = ['sleep 300 </dev/null >/dev/null 2>&1 &']
    s.script(alpha=[step])
    hook = os.path.join(s.side, 'hook')
    s.write('sitecustomize.py', f'''
import os, sys
if os.environ.get('DRILL_CLEANUP') and os.path.basename(sys.argv[0]) == 'runner':
    sys.path.insert(0, {SRC!r})
    from codesmith import proc, record
    original_stop, original_finish = proc.stop_group, record.Run.finish
    named = set()
    original_amend = record.Run.amend
    def amend(run, op, **more):
        if run.intent(op).get('task') == 'alpha' and more.get('process'):
            named.add(more['process']['pgid'])
        return original_amend(run, op, **more)
    def stop(process, identity, *args, **kwargs):
        if identity['pgid'] in named:
            raise record.RecordError('drill: alpha cleanup failed')
        return original_stop(process, identity, *args, **kwargs)
    def finish(run, op, **outcome):
        original_finish(run, op, **outcome)
        if outcome.get('cleanup'):
            os._exit(91)
    record.Run.amend, record.Run.finish, proc.stop_group = amend, finish, stop
''', base=hook)
    env = dict(s.env, DRILL_CLEANUP='1', PYTHONPATH=hook + os.pathsep + TESTS)
    code, out = s.runner('start', 'wf.toml', env=env)
    expect(code == 91, f'kill after cleanup save: {code} {out[-300:]}')
    entry = s.state()['cleanup_obligations'][0]
    expect(entry['cleanup']['status'] == 'open', 'cleanup is durable')
    expect(group_alive(entry['cleanup']['group']['pgid']), 'the group is still alive')
    expect(s.calls(REVIEWERS['alpha']) == 0 and s.calls('beta') == 0, 'no following call')
    code, out = s.runner('resume', '--stop-orphans', '-C', s.root)
    s.assert_done(code, out, 'resume after cleanup')
    expect(s.calls('alpha') == 1, 'the saved answer was used once')
    expect(s.state()['cleanup_obligations'][0]['cleanup']['status'] == 'closed', 'cleanup closed')
    return 'kill 91 with a live group; resume 0, cleanup closed, saved author called once; run done'


DRILLS = [drill_kill, drill_orphan, drill_git_clean, drill_ignore, drill_frozen, drill_capacity,
          drill_render, drill_pause_now, drill_cleanup_answer]


def main(argv):
    keep = "--keep" in argv
    wanted = {int(a) for a in argv if a.isdigit()} or set(range(1, len(DRILLS) + 1))
    failed = 0
    for n, drill in enumerate(DRILLS, 1):
        if n not in wanted:
            continue
        title = drill.__doc__.split(". ", 1)[1].strip()
        s = Scratch(drill.__name__.split("_", 1)[1], keep=keep)
        started = time.monotonic()
        try:
            evidence, ok = drill(s), True
        except DrillFailed as exc:
            evidence, ok = str(exc), False
        except Exception as exc:                 # noqa: BLE001 - one broken drill, not the rest
            evidence, ok = f"{type(exc).__name__}: {exc}", False
        finally:
            s.close()
        failed += not ok
        evidence = evidence.replace(s.root, "<repo>").replace(s.base, "<scratch>")
        print(f"{'ok  ' if ok else 'FAIL'} {n}. {title} ({time.monotonic() - started:.1f} s): {evidence}"
              + (f" [kept: {s.base}]" if keep else ""), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
