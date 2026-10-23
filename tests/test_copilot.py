"""The GitHub Copilot CLI adapter (kind "copilot"). No model calls: fixtures and the fake CLI are
constructed from the CLI's help text and shipped event schema, not captured (tests/recorded)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from helpers import RepoCase
from codesmith import agents, proc, qualification, validate

FIXTURES = Path(__file__).parent / "recorded"
FAKE = str(Path(__file__).with_name("fake_copilot.py"))
SESSION = "0cb916db-26aa-40f2-86b5-1ba81b225fd2"


def result(stdout=b"", stderr=b"", code=0, status="exited"):
    return proc.ProcResult(status, code, stdout, stderr, 1.0, None)


def stream(*events, exit_code=0, session=SESSION):
    """JSONL of `events` (type, data) closed by the result record, as `--output-format json` prints."""
    lines = [json.dumps({"id": str(uuid.uuid4()), "timestamp": "2026-10-01T09:00:00.000Z", "parentId": None,
                         "type": kind, "data": data, **extra}) for kind, data, *rest in events
             for extra in [rest[0] if rest else {}]]
    if exit_code is not None:
        lines.append(json.dumps({"type": "result", "timestamp": "2026-10-01T09:00:01.000Z", "sessionId": session,
                                 "exitCode": exit_code, "usage": {"premiumRequests": 1}}))
    return ("\n".join(lines) + "\n").encode()


ANSWER = ("assistant.message", {"messageId": "m", "content": json.dumps({"outcome": "done", "summary": "s",
                                                                          "blocked_reason": "", "responses": []})})


class CommandLines(unittest.TestCase):
    def setUp(self):
        self.agent = agents.make("helper", {"kind": "copilot"})

    def test_writer_runs_tools_unattended_in_prompt_mode(self):
        argv = self.agent.build_argv("/inv", validate.PRODUCE, None, "gpt-5.4", 5, False)
        self.assertEqual(argv[0], "copilot")
        for word in ("--output-format", "json", "--stream", "off", "--no-ask-user", "--no-auto-update",
                     "--no-remote", "--allow-all-tools", "--model", "gpt-5.4", "--session-id"):
            self.assertIn(word, argv)
        uuid.UUID(argv[argv.index("--session-id") + 1])     # chosen here, so the record can be found
        self.assertNotIn("-p", argv)                         # the prompt goes on standard input
        for wider in ("--allow-all", "--yolo", "--allow-all-paths", "--allow-all-urls"):
            self.assertNotIn(wider, argv)
        self.assertFalse(any(a.startswith("--available-tools") for a in argv))

    def test_reviewer_sees_only_read_tools_and_is_denied_the_rest(self):
        argv = self.agent.build_argv("/inv", validate.REVIEW, None, "", 5, True)
        self.assertNotIn("--allow-all-tools", argv)
        self.assertIn("--no-remote", argv)
        self.assertIn("--available-tools=view,glob,grep", argv)
        self.assertIn("--deny-tool=shell,write,url,memory", argv)
        self.assertIn("--disable-builtin-mcps", argv)
        self.assertNotIn("--model", argv)

    def test_yolo_profile_writer_gets_every_permission(self):
        # permission_mode = "bypassPermissions" is the explicit YOLO profile, as for Claude.
        a = agents.make("helper", {"kind": "copilot", "permission_mode": "bypassPermissions"})
        argv = a.build_argv("/inv", validate.PRODUCE, None, "", 5, False)
        for word in ("--allow-all-tools", "--allow-all-paths", "--allow-all-urls"):
            self.assertEqual(argv.count(word), 1)
        self.assertFalse(any(a.startswith(("--available-tools", "--deny-tool")) for a in argv))

    def test_yolo_profile_reviewer_keeps_read_tools_only(self):
        # A read-only call of the same profile is a reviewer like any other: no allow-all flag,
        # as Claude's --tools and --disallowedTools still bind under bypassPermissions.
        a = agents.make("helper", {"kind": "copilot", "permission_mode": "bypassPermissions"})
        argv = a.build_argv("/inv", validate.REVIEW, None, "", 5, True)
        plain = self.agent.build_argv("/inv", validate.REVIEW, None, "", 5, True)
        self.assertEqual(argv[:-1], plain[:-1])              # all but the fresh session id
        for wider in ("--allow-all", "--yolo", "--allow-all-tools", "--allow-all-paths", "--allow-all-urls"):
            self.assertNotIn(wider, argv)
        self.assertIn("--available-tools=view,glob,grep", argv)
        self.assertIn("--deny-tool=shell,write,url,memory", argv)

    def test_resume_names_the_session_and_profile_settings_apply(self):
        a = agents.make("helper", {"kind": "copilot", "model": "claude-sonnet-5", "extra_args": ["--effort", "high"],
                                   "argv": ["/opt/copilot"]})
        argv = a.build_argv("/inv", validate.PRODUCE, "old-session", "", 5, False)
        self.assertEqual(argv[:3], ["/opt/copilot", "--effort", "high"])
        self.assertIn("--resume=old-session", argv)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5")

    def test_the_environment_cannot_grant_all_tools(self):
        env = self.agent.call_env({"COPILOT_ALLOW_ALL": "true", "GH_TOKEN": "t"}, True)
        self.assertEqual(env, {"GH_TOKEN": "t"})

    def test_the_environment_cannot_change_model_or_provider(self):
        # None of these is in argv or the qualification fingerprint, so each would let a profile
        # without `model` run something doctor never qualified.
        given = {"COPILOT_MODEL": "gpt-9", "COPILOT_OFFLINE": "true",
                 "COPILOT_PROVIDER_BASE_URL": "http://localhost:11434/v1", "COPILOT_PROVIDER_TYPE": "openai",
                 "COPILOT_PROVIDER_API_KEY": "k", "COPILOT_PROVIDER_WIRE_MODEL": "m",
                 "COPILOT_HOME": "/h", "GH_TOKEN": "t", "PATH": "/bin"}
        for read_only in (True, False):
            with self.subTest(read_only=read_only):
                self.assertEqual(self.agent.call_env(given, read_only),
                                 {"COPILOT_HOME": "/h", "GH_TOKEN": "t", "PATH": "/bin"})

    def test_kind_reports_no_cost_and_reads_usage_from_its_record(self):
        self.assertFalse(self.agent.reports_cost)
        self.assertTrue(self.agent.usage_from_record)
        self.assertIn("resume", self.agent.capabilities())


class Interpret(unittest.TestCase):
    def setUp(self):
        self.agent = agents.make("helper", {"kind": "copilot"})

    def test_constructed_review_stream(self):
        r = self.agent.interpret(result((FIXTURES / "copilot-review.jsonl").read_bytes()))
        self.assertEqual(r.status, agents.OK, r.error)
        self.assertEqual(r.structured["verdict"], "block")
        self.assertEqual(validate.check_shape(r.structured, validate.REVIEW), [])
        self.assertEqual(r.session_id, SESSION)
        self.assertIsNone(r.cost_usd)
        self.assertEqual(r.usage, {})                       # the stream has no token counts

    def test_tool_steps_and_sub_agents_are_not_the_answer(self):
        r = self.agent.interpret(result(stream(
            ANSWER,
            ("assistant.message", {"messageId": "s", "content": '{"sub": 1}'}, {"agentId": "a1"}),
            ("assistant.message", {"messageId": "p", "content": '{"sub": 2}', "parentToolCallId": "c"}),
            ("assistant.message", {"messageId": "t", "content": '{"step": 1}', "toolRequests": [{"name": "view"}]}),
            ("tool.execution_complete", {"toolCallId": "c", "success": False}),
            ("future.event", {"x": 1}))))
        self.assertEqual(r.status, agents.OK, r.error)
        self.assertEqual(r.structured["outcome"], "done")

    def test_the_result_record_is_required_and_final(self):
        for data in (stream(ANSWER, exit_code=None), b"", stream(ANSWER) + stream(ANSWER, exit_code=None),
                     stream(ANSWER) + b"not json\n", stream(("assistant.message", {"messageId": "m", "content": 7}))):
            self.assertEqual(self.agent.interpret(result(data)).status, agents.PROTOCOL_ERROR, data)
        bad_exit = stream(ANSWER).replace(b'"exitCode": 0', b'"exitCode": "0"')
        self.assertEqual(self.agent.interpret(result(bad_exit)).status, agents.PROTOCOL_ERROR)
        prose = stream(("assistant.message", {"messageId": "m", "content": "All done."}))
        self.assertEqual(self.agent.interpret(result(prose)).status, agents.PROTOCOL_ERROR)
        self.assertEqual(self.agent.interpret(result(stream(ANSWER, exit_code=1))).status, agents.AGENT_ERROR)

    def test_nothing_may_follow_the_closing_result(self):
        """agent: the closing result is exactly one and the last record (AGENT-15): a tool
        event after it, or a second result after an intervening answer, is a protocol error"""
        tool_after = stream(ANSWER) + stream(("tool.execution_start", {"toolCallId": "c", "toolName": "shell"}),
                                             exit_code=None)
        second_result = stream(ANSWER) + stream(("assistant.message", {"messageId": "x",
                                                 "content": '{"outcome": "blocked"}'}))
        bare_second = stream(ANSWER) + stream()
        for data in (tool_after, second_result, bare_second):
            r = self.agent.interpret(result(data))
            self.assertEqual(r.status, agents.PROTOCOL_ERROR, data)
            self.assertIn("terminal result record", r.error)
        self.assertEqual(self.agent.interpret(result(stream(ANSWER))).status, agents.OK)

    def test_an_overlong_tool_result_is_not_a_malformed_stream(self):
        data = stream(ANSWER).replace(b"\n", b"\n" + proc.OVERLONG_PLACEHOLDER + b"\n", 1)
        self.assertEqual(self.agent.interpret(result(data)).status, agents.OK)

    def test_session_errors_are_classified_by_type(self):
        cases = [({"errorType": "quota", "errorCode": "quota_exceeded", "message": "x"}, agents.QUOTA),
                 ({"errorType": "rate_limit", "errorCode": "user_weekly_rate_limited", "message": "x"}, agents.QUOTA),
                 ({"errorType": "query", "statusCode": 429, "message": "x"}, agents.QUOTA),
                 ({"errorType": "authentication", "message": "x"}, agents.ENVIRONMENT),
                 ({"errorType": "authorization", "message": "x"}, agents.ENVIRONMENT),
                 ({"errorType": "query", "statusCode": 503, "message": "x"}, agents.TRANSIENT),
                 ({"errorType": "query", "message": "socket hang up: connection reset"}, agents.TRANSIENT),
                 ({"errorType": "context_limit", "message": "too long"}, agents.AGENT_ERROR),
                 ({"errorType": "session_limits", "message": "AI credit limit reached"}, agents.AGENT_ERROR)]
        for data, status in cases:
            with self.subTest(data=data):
                r = self.agent.interpret(result(stream(("session.error", data), exit_code=1), code=1))
                self.assertEqual(r.status, status, r.error)
                self.assertEqual(r.session_id, SESSION)
                self.assertIn(data["errorType"], r.error)
        r = self.agent.interpret(result((FIXTURES / "copilot-quota-failure.jsonl").read_bytes(), code=1))
        self.assertEqual(r.status, agents.QUOTA)

    def test_a_model_call_error_the_cli_recovered_from_does_not_decide_the_call(self):
        r = self.agent.interpret(result(stream(("session.error", {"errorType": "model_call", "message": "retrying"}),
                                               ANSWER)))
        self.assertEqual(r.status, agents.OK, r.error)

    def test_fatal_warnings(self):
        for kind, status in (("policy_blocked", agents.ENVIRONMENT),
                             ("compaction_static_context_blocked", agents.AGENT_ERROR)):
            r = self.agent.interpret(result(stream(("session.warning", {"warningType": kind, "message": "m"}),
                                                   exit_code=1), code=1))
            self.assertEqual(r.status, status)
        r = self.agent.interpret(result(stream(("session.warning", {"warningType": "mcp", "message": "m"}), ANSWER)))
        self.assertEqual(r.status, agents.OK)

    def test_startup_failures_on_stderr_are_environment(self):
        for said in (b"Error: No authentication information found.\n\nCopilot can be authenticated ...",
                     b"Error: Authentication token found but could not be validated.",
                     b'Error: Model "gpt-9" from --model flag is not available.',
                     b"Error: Run `copilot --model x` in interactive mode to enable this model",
                     b"error: unknown option '--frobnicate'",
                     b"error: option '--stream <mode>' argument 'x' is invalid. Allowed choices are on, off."):
            with self.subTest(said=said):
                self.assertEqual(self.agent.interpret(result(stderr=said, code=1)).status, agents.ENVIRONMENT)
        r = self.agent.interpret(result(stderr=b"Error executing prompt: boom", code=1))
        self.assertEqual(r.status, agents.AGENT_ERROR)
        self.assertEqual(self.agent.interpret(result(status="not-started")).status, agents.ENVIRONMENT)

    def test_copilot_markers_apply_to_copilot_only(self):
        # A command or Claude agent whose own output quotes Copilot's wording has failed at its
        # task, not in its environment; the same text from the Copilot CLI is an environment failure.
        phrases = (b"Allowed choices are red, green.", b"run it interactively to enable this model",
                   b"classic personal access tokens are not supported",
                   b"No authentication information found", b'Model "x" from --model flag is not available',
                   b"No supported model available",
                   b"Error: Access denied by policy settings (Request ID: E40B:9604E:11348D:156E8E:6AC1972F)")
        others = [agents.make("c", {"kind": "claude"}), agents.make("x", {"kind": "codex"}),
                  agents.make("cmd", {"kind": "command", "argv": ["true"]})]
        for said in phrases:
            with self.subTest(said=said):
                self.assertEqual(self.agent.interpret(result(stderr=said, code=1)).status, agents.ENVIRONMENT)
                failure = agents.process_failure(result(stderr=said, code=1))
                self.assertEqual(failure.status, agents.AGENT_ERROR)
                for other in others:
                    self.assertEqual(other.interpret(result(stderr=said, code=1)).status, agents.AGENT_ERROR,
                                     other.kind)
                self.assertEqual(agents.environment_error(said.decode()), "")
                self.assertTrue(agents.environment_error(said.decode(), copilot=True))

    def test_a_time_out_stays_a_time_out(self):
        r = self.agent.interpret(result(stream(("session.error", {"errorType": "quota", "message": "x"}), exit_code=None),
                                        status="timed-out"))
        self.assertEqual(r.status, agents.TIMED_OUT)


class ProviderRecord(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {"COPILOT_HOME": str(self.root / "home")}
        events = self.root / "home" / "session-state" / SESSION / "events.jsonl"
        events.parent.mkdir(parents=True)
        def shutdown(stamp, metrics, premium=1):
            return json.dumps({"type": "session.shutdown", "timestamp": stamp, "id": "x", "parentId": None,
                               "data": {"shutdownType": "routine", "totalPremiumRequests": premium,
                                        "modelMetrics": metrics}})
        events.write_text("\n".join([
            json.dumps({"type": "session.start", "timestamp": "2026-10-01T08:00:00.000Z", "data": {}}),
            shutdown("2026-10-01T08:30:00.000Z", {"m": {"usage": {"inputTokens": 999, "outputTokens": 999}}}),
            "not json",
            shutdown("2026-10-01T09:00:05.000Z", {"gpt-5.4": {"usage": {"inputTokens": 1200, "outputTokens": 80,
                                                                         "cacheReadTokens": 1000}},
                                                  "claude-haiku-4.5": {"usage": {"inputTokens": 30, "outputTokens": 5}},
                                                  "bad": {"usage": {"inputTokens": "many"}}}, premium=2),
            shutdown("bad stamp", {"m": {"usage": {"inputTokens": 5}}}),
        ]) + "\n")
        self.since = agents._epoch("2026-10-01T09:00:00Z")
        self.inv = self.root / "inv"
        self.inv.mkdir()

    def usage(self, argv, since=None):
        (self.inv / "argv.json").write_text(json.dumps({"argv": argv, "cwd": str(self.root)}))
        return agents.partial_usage("copilot", str(self.inv), self.since if since is None else since, self.env)

    def test_shutdown_rows_of_this_call_are_summed(self):
        expected = {"tokens_in": 1230, "tokens_out": 85, "premium_requests": 2}
        self.assertEqual(self.usage(["copilot", "--session-id", SESSION]), expected)
        self.assertEqual(self.usage(["copilot", "--resume=" + SESSION]), expected)
        self.assertEqual(self.usage(["copilot", "--session-id", SESSION], since=self.since + 3600), {})
        self.assertEqual(self.usage(["copilot", "--session-id", "no-such-session"]), {})
        self.assertEqual(self.usage(["copilot", "--session-id", "../escape"]), {})
        self.assertEqual(self.usage(["copilot"]), {})


class Run(RepoCase):
    """The whole call through proc.run_process: stdin, streamed parsing, the provider record."""

    def call(self, read_only=False, env_extra=None, session_id=None, schema=validate.PRODUCE, timeout_s=10, argv=None):
        inv = Path(self.root, "inv-" + uuid.uuid4().hex[:6])
        inv.mkdir()
        log = Path(self.root, "fake.json")
        home = Path(self._tmp.name, "copilot-home")
        env = dict(os.environ, COPILOT_HOME=str(home), FAKE_COPILOT_LOG=str(log), COPILOT_ALLOW_ALL="true")
        env.update(env_extra or {})
        a = agents.make("helper", {"kind": "copilot", "argv": argv or [sys.executable, FAKE]})
        r = a.run("the prompt", cwd=self.root, invocation_dir=str(inv), schema=schema, session_id=session_id,
                  model="gpt-5.4", timeout_s=timeout_s, budget_usd=1, read_only=read_only, env=env)
        return r, (json.loads(log.read_text()) if log.exists() else None), inv

    def test_a_call_answers_and_its_usage_comes_from_the_session_record(self):
        r, seen, inv = self.call()
        self.assertEqual(r.status, agents.OK, r.error)
        self.assertEqual(seen["prompt"], "the prompt")
        self.assertFalse(seen["allow_all_env"])
        self.assertEqual(r.usage, {"tokens_in": 100, "tokens_out": 9, "premium_requests": 1})
        self.assertEqual(r.usage_source, "provider-record")
        self.assertIsNone(r.cost_usd)
        self.assertEqual(json.loads((inv / "argv.json").read_text())["argv"][2:], seen["argv"])
        resumed, _, _ = self.call(session_id=r.session_id)
        self.assertEqual(resumed.status, agents.OK, resumed.error)
        self.assertEqual(resumed.session_id, r.session_id)

    def test_without_a_record_usage_is_unknown(self):
        r, _, _ = self.call(env_extra={"COPILOT_HOME": ""})
        self.assertEqual(r.status, agents.OK, r.error)
        self.assertEqual(r.usage, {})

    def test_a_hung_cli_is_stopped_by_the_runner_clock(self):
        script = Path(self.root, "hang.py")
        script.write_text("import time\ntime.sleep(60)\n")
        r, _, _ = self.call(argv=[sys.executable, str(script)], timeout_s=0.5)
        self.assertEqual(r.status, agents.TIMED_OUT)
        self.assertEqual(r.usage, {})


class Doctor(RepoCase):
    def workflow(self, tasks):
        return self.load('name="c"\n[defaults]\nagent="helper"\n[agents.helper]\nkind="copilot"\n'
                         + f'argv = [{json.dumps(sys.executable)}, {json.dumps(FAKE)}]\n' + tasks)

    def test_doctor_qualifies_writer_and_reviewer_profiles_by_their_effects(self):
        wf = self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\noutputs=["out.txt"]\ngate=["true"]\n'
                           '[[task]]\nid="look"\ntype="review"\nreviews="make"\nperspective="principled-priya"\n')
        self.assertLoads(wf)
        self.commit()
        with patch.dict(os.environ, COPILOT_HOME=os.path.join(self._tmp.name, "home")):
            report = qualification.check_workflow(wf)
        self.assertEqual(report["problems"], [])
        writer = report["profiles"][report["tasks"]["make"]]
        reviewer = report["profiles"][report["tasks"]["look"]]
        self.assertEqual(set(writer["capabilities"]), {"answer", "read", "execute", "write", "resume"})
        self.assertEqual(set(reviewer["capabilities"]), {"answer", "read", "resume", "boundary"})
        self.assertIn("1.0.75", writer["metadata"]["version"])
        unpriced = report["spend"]["unpriced"]
        self.assertEqual(unpriced["unknown_calls"], 0)
        self.assertEqual(unpriced["tokens_in"], 100 * unpriced["calls"])


if __name__ == "__main__":
    unittest.main()
