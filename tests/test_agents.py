"""The agent interface and the `command` adapter: the environment, the logs, the runner's own
clock, and what counts as an answer."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import RUNNER, EngineCase, done

from codesmith import agents, checks, proc, record, validate

PY = sys.executable


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                    # a zombie is not alive
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


class CommandAdapter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = os.path.realpath(self._tmp.name)
        self.n = 0

    def call(self, code, prompt="the prompt", timeout_s=20, on_start=None, **profile):
        self.n += 1
        self.inv = os.path.join(self.dir, f"invocation-{self.n}")
        os.makedirs(self.inv)
        agent = agents.make("scripted", dict({"kind": "command", "argv": [PY, "-c", code]}, **profile))
        env = agents.agent_env(os.environ, "run-uuid", "make")
        return agent.run(prompt, cwd=self.dir, invocation_dir=self.inv, schema=validate.PRODUCE,
                         session_id=None, model="", timeout_s=timeout_s, budget_usd=1.0,
                         read_only=False, env=env, on_start=on_start)

    def log(self, name):
        with open(os.path.join(self.inv, name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_answer_is_the_last_json_object(self):
        code = ("import sys, json; p = sys.stdin.read(); print('thinking about {braces} ...');"
                "print(json.dumps({'first': 1})); print('more prose');"
                "print(json.dumps({'outcome': 'done', 'echo': p, 'nested': {'a': [1, {'b': 2}]}}))")
        result = self.call(code)
        self.assertEqual(result.status, agents.OK)
        self.assertEqual(result.structured["echo"], "the prompt")           # the prompt is on stdin
        self.assertEqual(result.structured["nested"], {"a": [1, {"b": 2}]})
        self.assertIn("more prose", self.log("stdout.log"))
        self.assertIn("more prose", self.log("last-message.txt"))
        argv = json.loads(self.log("argv.json"))
        self.assertEqual(argv["argv"][:2], [PY, "-c"])
        self.assertEqual(json.loads(self.log("schema.json")), validate.PRODUCE)

    def test_what_is_not_an_answer(self):
        self.assertIsNone(agents.last_json_object("no json here"))
        self.assertIsNone(agents.last_json_object('{"outcome": "done"} and then more prose'))
        self.assertIsNone(agents.last_json_object('[1, 2, 3]'))
        self.assertEqual(agents.last_json_object('x {"a": {"b": 1}}\n\n'), {"a": {"b": 1}})
        self.assertEqual(self.call("print('It went well.')").status, agents.PROTOCOL_ERROR)

    def test_a_failing_agent(self):
        result = self.call("import sys; print('{\"outcome\": \"done\"}'); "
                           "print('quota exceeded', file=sys.stderr); sys.exit(3)")
        self.assertEqual(result.status, agents.AGENT_ERROR)        # an answer does not excuse the exit
        self.assertIn("status 3", result.error)
        self.assertIn("quota exceeded", result.error)
        self.assertIn("quota exceeded", self.log("stderr.log"))

    def test_an_agent_that_cannot_start(self):
        self.inv = os.path.join(self.dir, "inv")
        os.makedirs(self.inv)
        agent = agents.make("ghost", {"kind": "command", "argv": ["/nonexistent/agent"]})
        result = agent.run("p", cwd=self.dir, invocation_dir=self.inv, schema={}, session_id=None,
                           model="", timeout_s=5, budget_usd=0, read_only=False, env=dict(os.environ))
        self.assertEqual(result.status, agents.ENVIRONMENT)
        self.assertIn("/nonexistent/agent", result.error)

    def test_the_registry(self):
        self.assertIsInstance(agents.make("x", {"kind": "command", "argv": ["true"]}),
                              agents.CommandAgent)
        with self.assertRaises(agents.UnknownAgent) as caught:
            agents.make("unknown", {"kind": "unknown"})
        self.assertIn("unknown", str(caught.exception))
        self.assertNotIn("resume", agents.CommandAgent("x", {"argv": []}).capabilities())

    def test_read_only_args(self):
        agent = agents.CommandAgent("x", {"argv": ["tool"], "read_only_args": ["--sandbox", "ro"]})
        self.assertEqual(agent.argv(True, None), ["tool", "--sandbox", "ro"])
        self.assertEqual(agent.argv(False, None), ["tool"])

    def test_environment(self):
        """run: environment (RUN-09)"""
        # The suite's own strict-invariants switch (helpers) is inherited like any variable.
        code = ("import os, json; print(json.dumps({k: v for k, v in os.environ.items() "
                "if k.startswith('CODE_SMITH_') and k != 'CODE_SMITH_STRICT_INVARIANTS'}))")
        os.environ["CODE_SMITH_RUN_DIR"] = "/inherited/from/an/outer/run"
        self.addCleanup(os.environ.pop, "CODE_SMITH_RUN_DIR", None)
        # The runner's own record location (`--runs-dir`) is not for its children either: a gate
        # that runs a runner of its own would otherwise write into this run's record.
        previous = os.environ.get("CODE_SMITH_RUNS_DIR")
        os.environ["CODE_SMITH_RUNS_DIR"] = "/the/outer/runs"
        self.addCleanup(lambda: os.environ.pop("CODE_SMITH_RUNS_DIR", None) if previous is None
                        else os.environ.__setitem__("CODE_SMITH_RUNS_DIR", previous))
        self.assertEqual(self.call(code).structured,
                         {"CODE_SMITH_RUN": "run-uuid", "CODE_SMITH_TASK": "make"})
        env = agents.agent_env(os.environ, "run-uuid", "report", run_dir="/the/run")
        self.assertEqual(env["CODE_SMITH_RUN_DIR"], "/the/run")
        gate_env = checks.command_env(dict(os.environ, ANTHROPIC_API_KEY="k", OPENAI_API_KEY="k",
                                           GH_TOKEN="t", PATH="/bin"), "run-uuid", "make")
        self.assertEqual(gate_env["CODE_SMITH_TASK"], "make")
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", "CODE_SMITH_RUN_DIR",
                     "CODE_SMITH_RUNS_DIR"):
            self.assertNotIn(name, gate_env)
        self.assertEqual(gate_env["PATH"], "/bin")

    def test_redaction(self):
        """run: redaction (RUN-10)"""
        secrets = ["ghp_" + "a1B2" * 9, "github_pat_" + "Zz9_" * 8, "sk-ant-" + "x7" * 20,
                   "AKIA" + "ABCDEFGH12345678", "xoxb-" + "1234567890-abcdef"]
        code = ("import sys, json\n"
                f"for s in {secrets!r}:\n"
                "    print('using token', s); print('stderr has', s, file=sys.stderr)\n"
                f"print('Authorization: Bearer ' + {'tok.' * 8!r})\n"
                f"sys.stdout.write('x' * 70000 + {secrets[0]!r} + '\\n')\n"
                f"print(json.dumps({{'outcome': 'done', 'summary': 'my key is ' + {secrets[0]!r}}}))")
        result = self.call(code)
        stored = self.log("stdout.log") + self.log("stderr.log") + self.log("last-message.txt")
        for secret in secrets + ["tok.tok."]:
            self.assertNotIn(secret, stored)
        self.assertGreaterEqual(stored.count("[redacted]"), 12)
        self.assertIn("[overlong line omitted for safe redaction]", stored)
        self.assertIn("Authorization: Bearer [redacted]", stored)
        self.assertNotIn(secrets[0], result.structured["summary"])      # nor in what the engine sees
        self.assertEqual(proc.redact(b"nothing secret: sk-short ghp_x"), b"nothing secret: sk-short ghp_x")

    def test_secrets_split_at_log_buffer_boundaries(self):
        path = os.path.join(self.dir, "boundary.log")
        sink = proc._Sink(path)
        secret = b"ghp_" + b"Q" * 40
        sink.feed(b"x" * (proc.LINE_LIMIT - 2) + secret[:2])
        sink.feed(secret[2:] + b"\n")
        sink.feed(b"prefix " + secret[:10])
        sink.feed(secret[10:] + b"\nnormal\n")
        sink.close()
        with open(path, "rb") as fh:
            stored = fh.read()
        self.assertNotIn(b"Q" * 20, stored)
        self.assertNotIn(secret, stored)
        self.assertIn(b"normal\n", stored)
        self.assertIn(b"prefix [redacted]", stored)

    def test_the_runners_own_clock(self):
        """A hung agent is stopped by the runner, with its whole process group (05, Processes)."""
        code = ("import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
                "print('child', child.pid, flush=True)\n"
                "time.sleep(600)")
        seen = {}
        started = time.monotonic()
        result = self.call(code, timeout_s=1.0, on_start=seen.update)
        self.assertEqual(result.status, agents.TIMED_OUT)
        self.assertLess(time.monotonic() - started, 15)
        child = int(self.log("stdout.log").split()[1])                    # streamed before the end
        self.assertEqual(seen["pgid"], seen["pid"])                       # its own process group
        self.assertIn("start_ticks", seen)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (alive(child) or alive(seen["pid"])):
            time.sleep(0.05)
        self.assertFalse(alive(seen["pid"]))
        self.assertFalse(alive(child))

    def test_a_stubborn_agent_is_killed(self):
        code = ("import signal, time\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN); signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('deaf', flush=True)\nwhile True: time.sleep(1)")
        res = proc.run_process([PY, "-c", code], cwd=self.dir, env=dict(os.environ),
                               stdout_path=os.path.join(self.dir, "out.log"), timeout_s=1.0,
                               grace_s=0.3)
        self.assertEqual(res.status, "timed-out")
        self.assertFalse(alive(res.identity["pid"]))

    def test_a_straggler_cannot_hold_the_call_open(self):
        """A background child that keeps the pipe open does not keep the runner waiting."""
        code = ("import subprocess, sys\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
                "print('{\"outcome\": \"done\"}')")
        started = time.monotonic()
        result = self.call(code, timeout_s=60)
        self.assertEqual(result.status, agents.OK)
        self.assertLess(time.monotonic() - started, 10)

    def test_gate_commands(self):
        log = os.path.join(self.dir, "gate.log")
        results = checks.run_commands(["echo one; echo ghp_" + "q" * 30, "echo two; exit 4", "echo never"],
                                      cwd=self.dir, log_path=log, timeout_s=20, env=dict(os.environ))
        self.assertEqual([(r["result"], r["exit"]) for r in results], [("pass", 0), ("fail", 4)])
        self.assertFalse(checks.passed(results, ["a", "b", "c"]))
        with open(log) as fh:
            text = fh.read()
        self.assertIn("two", text)
        self.assertNotIn("never", text)
        self.assertNotIn("ghp_q", text)
        slow = checks.run_commands(["sleep 30"], cwd=self.dir, log_path=log, timeout_s=0.5,
                                   env=dict(os.environ))
        self.assertEqual(slow[0]["result"], "timeout")


class CodexStream(unittest.TestCase):
    """agent: an event longer than the log's line limit is withheld by the sink, not malformed"""

    @staticmethod
    def events(*lines):
        ev = agents.CodexEvents()
        ev.feed(b"".join(line + b"\n" for line in lines))
        return ev

    RES = proc.ProcResult("ok", 0, b"", b"", 1.0, None)
    START = b'{"type":"thread.started","thread_id":"t1"}'
    TURN = b'{"type":"turn.started"}'
    ANSWER = json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": json.dumps({"verdict": "pass"})}}).encode()
    DONE = b'{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}'

    def test_an_overlong_event_does_not_reject_a_valid_answer(self):
        sink_dir = tempfile.mkdtemp()
        seen = []
        sink = proc._Sink(os.path.join(sink_dir, "stdout.log"), seen.append)
        big = json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "aggregated_output": "x" * (proc.LINE_LIMIT + 10)}}).encode()
        for line in (self.START, self.TURN, big, self.ANSWER, self.DONE):
            sink.feed(line + b"\n")
        sink.close()
        self.assertIn(proc.OVERLONG_PLACEHOLDER + b"\n", seen)
        ev = agents.CodexEvents()
        for chunk in seen:
            ev.feed(chunk)
        result = ev.result(self.RES)
        self.assertEqual((result.status, ev.omitted_events), (agents.OK, 1))
        self.assertEqual(result.structured, {"verdict": "pass"})

    def test_an_overlong_answer_is_still_read(self):
        """An answer longer than the log's line limit is withheld from the log only: the parser
        still gets it whole, redacted (on_stdout_overlong)."""
        sink_dir = tempfile.mkdtemp()
        ev = agents.CodexEvents()
        sink = proc._Sink(os.path.join(sink_dir, "stdout.log"), ev.feed, on_overlong=ev.feed)
        secret = "ghp_" + "Q" * 40
        answer = {"verdict": "pass", "notes": "x" * (proc.LINE_LIMIT + 10) + secret}
        big = json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(answer)}}).encode()
        for line in (self.START, self.TURN, big, self.DONE):
            sink.feed(line[:70000])                     # the long one crosses the limit mid-read
            sink.feed(line[70000:] + b"\n")
        sink.close()
        result = ev.result(self.RES)
        self.assertEqual(result.status, agents.OK, result.error)
        self.assertEqual(result.structured["verdict"], "pass")
        self.assertNotIn(secret, result.structured["notes"])
        with open(os.path.join(sink_dir, "stdout.log"), "rb") as fh:
            self.assertIn(proc.OVERLONG_PLACEHOLDER, fh.read())

    def test_garbage_is_still_malformed_and_an_omitted_answer_still_fails(self):
        self.assertEqual(self.events(self.START, self.TURN, b"not json", self.ANSWER, self.DONE)
                         .result(self.RES).status, agents.PROTOCOL_ERROR)
        lost = self.events(self.START, self.TURN, proc.OVERLONG_PLACEHOLDER, self.DONE).result(self.RES)
        self.assertEqual(lost.status, agents.PROTOCOL_ERROR)          # no answer survived


class CommandRecovery(EngineCase):
    def test_a_live_verifier_is_stopped_before_resume(self):
        marker = os.path.join(self.side, "started")
        command = f"touch {marker}; echo changed > README.md; sleep 600"
        self.workflow('[[task]]\nid = "check"\ntype = "check"\nrestores = true\n'
                      + 'run = [' + json.dumps(f"if test ! -f {marker}; then {command}; fi") + ']\n')
        env = dict(os.environ, CODE_SMITH_CRASH_AT="command:running")
        res = subprocess.run([PY, RUNNER, "start", self.wf_path], env=env,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 70, res.stderr)
        intent = next(i for i in self.the_run().state["intents"] if i["kind"] == "command")
        identity = intent["process"]
        self.addCleanup(record.stop_process_group, identity, 0.2)
        deadline = time.monotonic() + 5
        while not os.path.exists(marker) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(self.resume(), 2)
        self.assertIn("still running", self.output)
        self.assertEqual(self.resume("--stop-orphans"), 0, self.output)
        self.assertFalse(record.is_alive(identity))
        self.check_invariants()


class Validation(unittest.TestCase):
    def test_the_produce_answer(self):
        self.assertEqual(validate.check_produce(done(), []), [])
        self.assertTrue(validate.check_produce({"outcome": "done"}, []))                  # keys missing
        self.assertTrue(validate.check_produce(dict(done(), extra=1), []))                # no other key
        self.assertTrue(validate.check_produce(dict(done(), outcome="finished"), []))
        self.assertTrue(validate.check_produce(done(summary="s" * (validate.SUMMARY_MAX + 1)), []))
        self.assertEqual(validate.check_produce(done(summary="s" * validate.SUMMARY_MAX), []), [])
        self.assertTrue(validate.check_produce("done", []))
        blocked = {"outcome": "blocked", "summary": "", "blocked_reason": "", "responses": []}
        self.assertIn("blocked_reason", " ".join(validate.check_produce(blocked, [])))

    def test_summary_headroom_for_both_result_kinds(self):
        for schema, answer in [(validate.PRODUCE, done()),
                               (validate.REVIEW, dict(verdict='pass', summary='', findings=[], resolutions=[]))]:
            for length in (2062, 2167, validate.SUMMARY_MAX):
                with self.subTest(kind=schema['required'][0], length=length):
                    self.assertEqual(validate.check_shape(dict(answer, summary='s' * length), schema), [])
            self.assertTrue(validate.check_shape(
                dict(answer, summary='s' * (validate.SUMMARY_MAX + 1)), schema))

    def test_responses_answer_each_finding_once(self):
        fixed = {"finding": "make/PE-1", "action": "fixed", "note": "done"}
        self.assertEqual(validate.check_produce(done(responses=[fixed]), ["make/PE-1"]), [])
        self.assertTrue(validate.check_produce(done(responses=[fixed, fixed]), ["make/PE-1"]))
        self.assertTrue(validate.check_produce(done(responses=[dict(fixed, action="ignored")]),
                                               ["make/PE-1"]))
        disputed = dict(fixed, action="disputed", note="")
        self.assertTrue(validate.check_produce(done(responses=[disputed]), ["make/PE-1"]))


class Orphans(EngineCase):
    def test_the_identity_is_durable_before_the_record_is_hashed(self):
        """Once an agent or a command has started, its identity is in the state before the runner
        hashes the record: a kill in that hashing leaves a process `resume` can see, never an
        orphan it would start a second author beside."""
        from unittest import mock
        self.workflow('[[task]]\nid = "make"\ntype = "implement"\nprompt = "p"\n'
                      'outputs = ["src/a.txt"]\ngate = ["true"]\n')
        self.script([{"write": {"src/a.txt": "a\n"}, "answer": done()}])
        real, seen = record.Run.integrity_check, []

        def check(run):
            if sys._getframe(1).f_code.co_name == "started":
                state = record.read_json(os.path.join(run.path, "state.json"))
                seen.extend((i["kind"], bool(i.get("process"))) for i in state["intents"]
                            if i["kind"] in ("agent", "command"))
            return real(run)
        with mock.patch.object(record.Run, "integrity_check", check):
            self.assertEqual(self.start(), 0, self.output)
        # the gate, then the same gate in the acceptance replay
        self.assertEqual(sorted(seen), [("agent", True), ("command", True), ("command", True)])

    def test_an_orphaned_agent_is_never_run_beside(self):
        """The runner dies while its agent runs: `resume` refuses while the agent lives, and
        `--stop-orphans` stops it by its recorded identity (pid, start time and boot)."""
        self.workflow('[[task]]\nid = "make"\ntype = "implement"\nprompt = "p"\n'
                      'outputs = ["src/a.txt"]\ngate = ["true"]\n')
        self.script([{"hang": True}, {"write": {"src/a.txt": "a\n"}, "answer": done()}])
        env = dict(os.environ, CODE_SMITH_CRASH_AT="agent:running")
        res = subprocess.run([PY, RUNNER, "start", self.wf_path], env=env, capture_output=True,
                             text=True)
        self.assertEqual(res.returncode, 70, res.stderr)
        intent = [i for i in self.the_run().state["intents"] if i["kind"] == "agent"][0]
        identity = intent["process"]
        self.addCleanup(record.stop_process_group, identity, 0.2)
        self.assertTrue(record.is_alive(identity))
        self.assertIn("start_ticks", identity)
        # The parent may exit before the child consumes its scripted step.
        receipt = os.path.join(self.script_path + ".prompts", "1.md")
        deadline = time.monotonic() + 5
        while not os.path.exists(receipt) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(os.path.exists(receipt), "the orphan did not consume its first step")

        self.assertEqual(self.resume(), 2)
        self.assertIn("still running", self.output)
        self.assertIn("--stop-orphans", self.output)
        self.assertTrue(record.is_alive(identity))

        self.assertEqual(self.resume("--stop-orphans"), 0, self.output)
        self.assertFalse(record.is_alive(identity))
        outcome = self.read_json("make", "attempt-1", "invocation-1", "outcome.json")
        self.assertEqual(outcome["status"], "interrupted")
        self.assertEqual(self.the_run().state["tasks"]["make"]["attempts_used"], 1)
        self.check_invariants()


if __name__ == "__main__":
    unittest.main()
