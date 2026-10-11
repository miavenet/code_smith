"""Headless adapter contracts (AGENT-01 to AGENT-12). No model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import helpers
from codesmith import agents, proc, validate

FIXTURES = Path(__file__).parent / "recorded"

def result(stdout=b"", stderr=b"", code=0, status="exited"):
    return proc.ProcResult(status, code, stdout, stderr, 1.0, None)

class Headless(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_claude_command_lines(self):
        a = agents.make("author", {"kind": "claude"})
        argv = a.build_argv(self.root, validate.PRODUCE, "session-id", "chosen-model", 2.5, True)
        for word in ["-p", "--json-schema", "--max-budget-usd", "2.5", "--resume", "session-id",
                     "--disallowedTools", "--permission-prompts", "none", "auto", "chosen-model"]:
            self.assertIn(word, argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--setting-sources", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertIn("--strict-mcp-config", argv)
        writer = a.build_argv(self.root, validate.PRODUCE, None, "chosen-model", 2.5, False)
        self.assertNotIn("--tools", writer)
        self.assertNotIn("--disallowedTools", writer)

    def test_codex_command_lines(self):
        a = agents.make("writer", {"kind": "codex"})
        for sid in (None, "explicit-id"):
            argv = a.build_argv(self.root, validate.PRODUCE, sid, "chosen-model", 1, bool(sid))
            self.assertEqual(argv[:2], ["codex", "exec"])
            if sid:
                self.assertEqual(argv[2], "resume")
                self.assertIn(sid, argv)
            self.assertIn('approval_policy="never"', argv)
            self.assertIn('sandbox_mode="read-only"' if sid else 'sandbox_mode="workspace-write"', argv)
            self.assertIn("--output-schema", argv)
            self.assertEqual(argv[-1], "-")
            for forbidden in ("--last", "--skip-git-repo-check", "--sandbox", "--dangerously-bypass-approvals-and-sandbox"):
                self.assertNotIn(forbidden, argv)

    def test_codex_preflight_is_free_and_names_the_sandbox_and_deprecated_features(self):
        """agent: the Codex profile is checked before any model call (PRE-09)"""
        from unittest.mock import patch
        import subprocess
        calls = []
        FEATURES = b"apps          stable      true\nuse_legacy_landlock     deprecated   false\nold_thing  removed  false\n"
        BWRAP = b"bwrap: No permissions to create a new namespace, likely because the kernel does not allow non-privileged user namespaces.\n"
        def fake_run(argv, **kw):
            calls.append(argv)
            if argv[1:3] == ["features", "list"]:
                return subprocess.CompletedProcess(argv, 0, FEATURES, b"")
            if argv[1] == "sandbox":
                ok = "--enable" in argv
                return subprocess.CompletedProcess(argv, 0 if ok else 1, b"", b"" if ok else BWRAP)
            raise AssertionError(argv)
        with patch.object(agents.subprocess, "run", fake_run):
            plain = agents.make("plain", {"kind": "codex"})
            notes, error = plain.preflight(self.root, {}, True)
            self.assertEqual(notes, [])
            self.assertIn("the Codex sandbox cannot start on this host: bwrap: No permissions", error)
            self.assertEqual(calls[-1][:2], ["codex", "sandbox"])
            self.assertEqual(calls[-1][-2:], ["--", "true"])
            legacy = agents.make("astra", {"kind": "codex", "extra_args": ["--enable", "use_legacy_landlock", "-c", 'x="y"']})
            self.assertEqual(legacy.enabled_features(), ["use_legacy_landlock"])
            notes, error = legacy.preflight(self.root, {}, True)
            self.assertEqual(error, "")
            self.assertEqual(len(notes), 1)
            self.assertIn("'use_legacy_landlock' is deprecated in this Codex CLI", notes[0])
            gone = agents.make("gone", {"kind": "codex", "extra_args": ["--enable", "old_thing", "--enable", "use_legacy_landlock"]})
            self.assertIn("'old_thing' is removed", gone.preflight(self.root, {}, True)[0][0])
            n = len(calls)
            full = agents.make("full", {"kind": "codex", "sandbox": "danger-full-access"})
            self.assertEqual(full.preflight(self.root, {}, False), ([], ""))
            self.assertEqual(len(calls), n)                     # no sandbox to check: nothing run
            self.assertEqual(agents.make("c", {"kind": "codex", "extra_args": ["-c", "features.apps=true", "--enable=apps"]}).enabled_features(), ["apps", "apps"])

    def test_a_new_claude_call_names_its_own_session(self):
        """agent: the transcript of a call that never returns can still be found"""
        import uuid
        a = agents.make("author", {"kind": "claude"})
        argv = a.build_argv(self.root, validate.PRODUCE, None, "m", 1, False)
        uuid.UUID(argv[argv.index("--session-id") + 1])
        resumed = a.build_argv(self.root, validate.PRODUCE, "old-session", "m", 1, False)
        self.assertNotIn("--session-id", resumed)
        self.assertIn("--resume", resumed)

    def provider_records(self):
        """A Claude transcript and a Codex rollout under a fake home, as the CLIs write them."""
        home = Path(self.root, "home"); cwd = Path(self.root, "work"); cwd.mkdir()
        slug = "".join(c if c.isalnum() else "-" for c in str(cwd.resolve()))
        transcript = home / ".claude" / "projects" / slug / "11111111-2222-4333-8444-555555555555.jsonl"
        transcript.parent.mkdir(parents=True)
        def claude_row(stamp, request, usage):
            return json.dumps({"type": "assistant", "timestamp": stamp, "requestId": request,
                               "message": {"id": "msg_" + request, "usage": usage}})
        transcript.write_text("\n".join([
            json.dumps({"type": "user", "timestamp": "2026-09-23T07:00:00.000Z"}),
            claude_row("2026-09-23T06:00:00.000Z", "old", {"input_tokens": 999, "output_tokens": 999}),   # an earlier turn
            claude_row("2026-09-23T07:00:01.000Z", "r1", {"input_tokens": 2, "cache_creation_input_tokens": 10,
                                                        "cache_read_input_tokens": 100, "output_tokens": 7}),
            claude_row("2026-09-23T07:00:01.000Z", "r1", {"input_tokens": 2, "cache_creation_input_tokens": 10,
                                                        "cache_read_input_tokens": 100, "output_tokens": 7}),  # second block, same response
            claude_row("2026-09-23T07:00:05.000Z", "r2", {"input_tokens": 3, "output_tokens": 4}),
            "not json", json.dumps({"type": "assistant", "timestamp": "bad", "message": {"usage": {"input_tokens": 5}}}),
        ]) + "\n")
        rollout = home / ".codex" / "sessions" / "2026" / "09" / "23" / "rollout-2026-09-23T07-00-00-thread-abc.jsonl"
        rollout.parent.mkdir(parents=True)
        def codex_row(stamp, response, usage):
            return json.dumps({"timestamp": stamp, "type": "token_usage_record",
                               "payload": {"response_id": response, "usage": usage}})
        rollout.write_text("\n".join([
            json.dumps({"timestamp": "2026-09-23T07:00:00.000Z", "type": "session_meta", "payload": {}}),
            codex_row("2026-09-23T06:00:00.000Z", "old", {"input_tokens": 999, "output_tokens": 999}),
            codex_row("2026-09-23T07:00:02.000Z", "resp1", {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 50}),
            codex_row("2026-09-23T07:00:02.000Z", "resp1", {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 50}),
            codex_row("2026-09-23T07:00:09.000Z", "resp2", {"input_tokens": 1500, "cached_input_tokens": 0, "output_tokens": 60}),
        ]) + "\n")
        env = {"CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex")}
        since = agents._epoch("2026-09-23T07:00:00Z")
        return cwd, env, since

    def test_usage_of_a_call_without_a_terminal_event_is_read_from_the_provider_record(self):
        """agent: interrupted and timed-out calls are not "unknown usage" when the provider's record says"""
        cwd, env, since = self.provider_records()
        inv = Path(self.root, "inv-claude"); inv.mkdir()
        (inv / "argv.json").write_text(json.dumps({"argv": ["claude", "-p", "--session-id", "11111111-2222-4333-8444-555555555555"],
                                                   "cwd": str(cwd)}))
        self.assertEqual(agents.partial_usage("claude", str(inv), since, env), {"tokens_in": 115, "tokens_out": 11})
        self.assertEqual(agents.partial_usage("claude", str(inv), since + 3, env), {"tokens_in": 3, "tokens_out": 4})
        # Claude names the transcript's directory after the physical path, not a link to it.
        link = Path(self.root, "link-to-work"); link.symlink_to(cwd)
        (inv / "argv.json").write_text(json.dumps({"argv": ["claude", "--session-id", "11111111-2222-4333-8444-555555555555"],
                                                   "cwd": str(link)}))
        self.assertEqual(agents.partial_usage("claude", str(inv), since, env), {"tokens_in": 115, "tokens_out": 11})
        (inv / "argv.json").write_text(json.dumps({"argv": ["claude", "--resume", "no-such-session"], "cwd": str(cwd)}))
        self.assertEqual(agents.partial_usage("claude", str(inv), since, env), {})
        inv = Path(self.root, "inv-codex"); inv.mkdir()
        (inv / "argv.json").write_text(json.dumps({"argv": ["codex", "exec"], "cwd": str(cwd), "session_id": None}))
        (inv / "stdout.log").write_bytes(b'{"type":"thread.started","thread_id":"thread-abc"}\n{"type":"turn.started"}\n')
        self.assertEqual(agents.partial_usage("codex", str(inv), since, env),
                         {"tokens_in": 2500, "tokens_out": 110, "cached_tokens_in": 600})
        (inv / "stdout.log").write_bytes(b"")
        self.assertEqual(agents.partial_usage("codex", str(inv), since, env), {})
        (inv / "argv.json").write_text(json.dumps({"argv": ["codex", "exec", "resume"], "cwd": str(cwd), "session_id": "thread-abc"}))
        self.assertEqual(agents.partial_usage("codex", str(inv), since + 5, env)["tokens_in"], 1500)
        self.assertEqual(agents.partial_usage("command", str(inv), since, env), {})
        self.assertEqual(agents.partial_usage("codex", str(Path(self.root, "missing")), since, env), {})
        self.assertEqual(agents.partial_usage("codex", str(inv), None, env), {})

    def test_a_timed_out_call_reports_what_the_provider_record_says(self):
        from unittest.mock import patch
        cwd, env, since = self.provider_records()
        inv = Path(self.root, "inv"); inv.mkdir()
        a = agents.make("author", {"kind": "claude"})
        def timed_out(argv, **kw):
            return result(status="timed-out")
        with patch.object(proc, "run_process", timed_out), \
             patch.object(agents, "partial_usage", return_value={"tokens_in": 9, "tokens_out": 1}) as reader:
            answer = a.run("p", cwd=str(cwd), invocation_dir=str(inv), schema=validate.PRODUCE, session_id=None,
                           model="m", timeout_s=1, budget_usd=1, read_only=False, env=env)
        self.assertEqual(answer.status, agents.TIMED_OUT)
        self.assertEqual((answer.usage, answer.usage_source), ({"tokens_in": 9, "tokens_out": 1}, "provider-record"))
        self.assertEqual(reader.call_args.args[:2], ("claude", str(inv)))
        self.assertEqual(json.loads(json.dumps(answer.outcome()))["usage_source"], "provider-record")

    def codex_call(self, mode, timeout_s=20, profile=None):
        """One call of the fake Codex CLI, which writes the constructed rollout fixture."""
        home = Path(self.root, f"codex-home-{mode}")
        home.mkdir()
        inv = Path(self.root, f"inv-{mode}")
        inv.mkdir()
        fake = Path(__file__).parent / "fake_codex.py"
        a = agents.make("codex-fake", dict({"kind": "codex", "argv": [sys.executable, str(fake)]}, **(profile or {})))
        env = dict(os.environ, CODEX_HOME=str(home), FAKE_CODEX_MODE=mode)
        r = a.run("p", cwd=self.root, invocation_dir=str(inv), schema=validate.PRODUCE, session_id=None,
                  model="", timeout_s=timeout_s, budget_usd=1, read_only=False, env=env)
        return r

    def test_codex_rollout_token_count_rows_give_usage_and_the_rate_limit_window(self):
        """agent: a Codex call records its usage and rate-limit window from the rollout (AGENT-16)"""
        r = self.codex_call("ok")
        self.assertEqual(r.status, agents.OK, r.error)
        self.assertEqual(r.usage, {"tokens_in": 39000, "tokens_out": 1600, "cached_tokens_in": 33000,
                                   "reasoning_tokens_out": 800})
        self.assertEqual(r.usage_source, "terminal")
        limits = r.outcome()["limits"]
        self.assertEqual({k: limits[k] for k in ("kind", "plan_type", "limit_id", "window_minutes",
                                                 "used_percent_before", "used_percent_after", "delta_percent",
                                                 "resets_at")},
                         {"kind": "codex", "plan_type": "plus", "limit_id": "codex", "window_minutes": 10080,
                          "used_percent_before": 0.0, "used_percent_after": 3.0, "delta_percent": 3.0,
                          "resets_at": 1791936000})
        self.assertEqual(limits["credits"], {"has_credits": False, "unlimited": False, "balance": None})
        self.assertEqual((limits["secondary"]["window_minutes"], limits["secondary"]["delta_percent"]), (300, 2.5))
        # Interrupted (here: timed out) before its terminal event: the running totals of the rows in
        # the call, less the last row before it, are its usage; the window is recorded all the same.
        hung = self.codex_call("hang", timeout_s=3)
        self.assertEqual(hung.status, agents.TIMED_OUT)
        self.assertEqual((hung.usage, hung.usage_source),
                         ({"tokens_in": 39000, "tokens_out": 1600, "cached_tokens_in": 33000,
                           "reasoning_tokens_out": 800}, "provider-record"))
        self.assertEqual(hung.limits["used_percent_after"], 3.0)
        # No rollout rows: no limits at all, and the outcome says nothing about them.
        bare = self.codex_call("no-rollout")
        self.assertEqual(bare.status, agents.OK, bare.error)
        self.assertIsNone(bare.limits)
        self.assertNotIn("limits", bare.outcome())

    def test_profile_rates_price_a_codex_call(self):
        """bud: profile rates turn observed tokens into an estimate on the call (BUD-11, adapter)"""
        r = self.codex_call("ok", profile={"price_per_mtok": {"input": 2.0, "cached_input": 0.5, "output": 8.0}})
        # 6000 fresh input tokens at $2, 33000 cached at $0.50, 1600 output at $8, per million.
        self.assertEqual(r.estimated_usd, round((6000 * 2.0 + 33000 * 0.5 + 1600 * 8.0) / 1e6, 6))
        self.assertIsNone(r.cost_usd)
        self.assertEqual((r.outcome()["estimated_usd"], r.outcome()["estimated_counts"]), (r.estimated_usd, False))
        plain = self.codex_call("no-rollout")                    # no rates: no estimate, no key
        self.assertIsNone(plain.estimated_usd)
        self.assertNotIn("estimated_usd", plain.outcome())

    def test_ignore_config_is_explicit(self):
        for kind, flag in (("codex", "--ignore-user-config"), ("claude", "--setting-sources")):
            a = agents.make(kind, {"kind": kind, "ignore_user_config": True})
            self.assertIn(flag, a.build_argv(self.root, validate.PRODUCE, None, "", 1, False))

    def test_claude_recorded_result(self):
        a = agents.make("claude", {"kind": "claude"})
        r = a.interpret(result((FIXTURES / "claude-success.json").read_bytes()))
        self.assertEqual(r.status, agents.OK)
        self.assertEqual(r.structured["outcome"], "done")
        self.assertTrue(r.session_id)
        self.assertGreater(r.cost_usd, 0)
        self.assertGreater(r.usage["tokens_in"], 0)
        for data in ({"type": "result", "subtype": "error_max_budget_usd", "is_error": True},
                     {"type": "result", "subtype": "success", "is_error": True}):
            self.assertEqual(a.interpret(result(json.dumps(data).encode())).status, agents.AGENT_ERROR)
        self.assertEqual(a.interpret(result(b"not JSON")).status, agents.PROTOCOL_ERROR)

    def test_missing_usage_is_unknown_and_nonfinite_cost_is_not_money(self):
        claude = {"type":"result", "is_error":False, "subtype":"success",
                  "structured_output":{"outcome":"done"}, "total_cost_usd":float('nan')}
        r = agents.make("claude", {"kind":"claude"}).interpret(result(json.dumps(claude).encode()))
        self.assertEqual(r.usage, {})
        self.assertIsNone(r.cost_usd)
        codex = b'{"type":"item.completed","item":{"type":"agent_message","text":"{}"}}\n{"type":"turn.completed"}'
        self.assertEqual(agents.make("codex", {"kind":"codex"}).interpret(result(codex)).usage, {})

    def test_a_claude_result_longer_than_the_log_line_limit_is_read(self):
        """A paid, successful call whose one-line envelope the log withholds (over 64 KiB) is still
        read from the stream, with its cost; the log keeps the placeholder."""
        envelope = json.loads((FIXTURES / "claude-success.json").read_text())
        answer = {"notes": "x" * (proc.LINE_LIMIT + 100)}
        schema = {"type": "object", "properties": {"notes": {"type": "string"}}, "required": ["notes"]}
        envelope.update(structured_output=answer, result=json.dumps(answer))
        script = Path(self.root) / "emit.py"
        script.write_text("print(" + repr(json.dumps(envelope)) + ")")
        inv = Path(self.root) / "inv"; inv.mkdir()
        a = agents.make("claude", {"kind": "claude", "argv": [sys.executable, str(script)]})
        r = a.run("p", cwd=self.root, invocation_dir=str(inv), schema=schema, session_id=None, model="",
                  timeout_s=10, budget_usd=1, read_only=False, env=dict(os.environ))
        self.assertEqual(r.status, agents.OK, r.error)
        self.assertEqual(r.structured["notes"], answer["notes"])
        self.assertEqual(r.cost_usd, envelope["total_cost_usd"])
        self.assertIn(proc.OVERLONG_PLACEHOLDER, (inv / "stdout.log").read_bytes())

    def test_a_failed_claude_call_still_reports_what_it_cost(self):
        """A non-zero exit with a result envelope (budget or turns exhausted, an error during the
        call) keeps the envelope's cost, usage and session: the call is still charged."""
        a = agents.make("claude", {"kind": "claude"})
        envelope = json.loads((FIXTURES / "claude-success.json").read_text())
        for subtype in ("error_max_budget_usd", "error_max_turns", "error_during_execution"):
            envelope.update(is_error=True, subtype=subtype, result="", total_cost_usd=1.75)
            r = a.interpret(result(json.dumps(envelope).encode(), code=1))
            self.assertEqual(r.status, agents.AGENT_ERROR)
            self.assertEqual((r.cost_usd, r.usage["tokens_out"], r.session_id),
                             (1.75, 385, envelope["session_id"]))

    def test_a_recovered_codex_error_does_not_decide_a_completed_turn(self):
        events = [{"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"},
                  {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion)"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(helpers.done())}},
                  {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}]
        a = agents.make("codex", {"kind": "codex"})
        r = a.interpret(result("\n".join(json.dumps(e) for e in events).encode()))
        self.assertEqual(r.status, agents.OK, r.error)
        events[-1] = {"type": "turn.failed", "error": {"message": "transport failed"}}   # not recovered
        self.assertEqual(a.interpret(result("\n".join(json.dumps(e) for e in events).encode())).status,
                         agents.AGENT_ERROR)

    def test_deeply_nested_output_is_not_a_crash(self):
        deep = "[" * 100000 + "]" * 100000
        self.assertIsNone(agents.last_json_object('{"a": ' + deep + '}'))
        r = agents.make("claude", {"kind": "claude"}).interpret(result(deep.encode()))
        self.assertEqual(r.status, agents.PROTOCOL_ERROR)
        r = agents.make("codex", {"kind": "codex"}).interpret(result(deep.encode()))
        self.assertEqual(r.status, agents.PROTOCOL_ERROR)

    def test_claude_json_fallback(self):
        data = {"type": "result", "subtype": "success", "is_error": False,
                "result": 'Some prose. {"outcome":"done"}'}
        a = agents.make("claude", {"kind": "claude"})
        self.assertEqual(a.interpret(result(json.dumps(data).encode())).structured, {"outcome": "done"})

    def test_codex_recorded_result(self):
        a = agents.make("codex", {"kind": "codex"})
        r = a.interpret(result((FIXTURES / "codex-review.jsonl").read_bytes()))
        self.assertEqual(r.status, agents.OK)
        self.assertFalse(r.structured["approved"])
        self.assertEqual(r.usage["tokens_in"], 14592)
        self.assertIsNone(r.cost_usd)
        self.assertTrue(r.session_id)

    def test_sandbox_startup_is_an_environment_failure(self):
        a = agents.make("codex", {"kind": "codex"})
        r = a.interpret(result((FIXTURES / "codex-sandbox-failure.jsonl").read_bytes()))
        self.assertEqual(r.status, agents.ENVIRONMENT)
        self.assertIn("namespace", r.error)

    def test_terminal_event_required(self):
        a = agents.make("codex", {"kind": "codex"})
        stream = (FIXTURES / "codex-review.jsonl").read_bytes()
        lines = stream.splitlines()
        self.assertEqual(a.interpret(result(b"\n".join(lines[:-1]))).status, agents.PROTOCOL_ERROR)
        self.assertEqual(a.interpret(result(b"")).status, agents.PROTOCOL_ERROR)
        failed = b'{"type":"turn.failed","error":{"message":"transport failed"}}'
        self.assertEqual(a.interpret(result(stream + failed)).status, agents.AGENT_ERROR)
        self.assertEqual(a.interpret(result(stream, code=1)).status, agents.AGENT_ERROR)

    def test_incomplete_later_turn_and_malformed_fields_are_protocol_errors(self):
        a = agents.make("codex", {"kind": "codex"})
        stream = (FIXTURES / "codex-review.jsonl").read_bytes()
        for suffix in (b'{"type":"turn.started"}', b'{"type":"item.completed","item":42}',
                       b'{"type":"turn.completed","usage":42}'):
            self.assertEqual(a.interpret(result(stream + suffix)).status, agents.PROTOCOL_ERROR)
        malformed = {"type":"result", "is_error":False, "subtype":"success", "usage":{"input_tokens":"bad"}}
        self.assertEqual(agents.make("claude", {"kind":"claude"}).interpret(
            result(json.dumps(malformed).encode())).status, agents.PROTOCOL_ERROR)

    def test_tool_failure_is_ordinary_work(self):
        tool = {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 1,
                "status": "failed", "aggregated_output": "FAILED test_expected_behavior"}}
        stream = json.dumps(tool).encode() + b"\n" + (FIXTURES / "codex-review.jsonl").read_bytes()
        self.assertEqual(agents.make("codex", {"kind": "codex"}).interpret(result(stream)).status, agents.OK)

    def test_usage_is_summed_and_unknown_events_survive(self):
        stream = (FIXTURES / "codex-review.jsonl").read_bytes()
        stream += b'{"type":"future.event","value":1}\n'
        stream += b'{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":4}}\n'
        r = agents.make("codex", {"kind": "codex"}).interpret(result(stream))
        self.assertEqual(r.usage["tokens_in"], 14595)
        self.assertEqual(r.usage["tokens_out"], 104)

    def call_script(self, output, schema=validate.PRODUCE):
        inv = Path(self.root) / "inv"
        inv.mkdir()
        script = Path(self.root) / "replay.py"
        script.write_text("print(" + repr(output) + ")")
        a = agents.make("test", {"kind": "codex", "argv": [sys.executable, str(script)]})
        return a.run("prompt", cwd=self.root, invocation_dir=str(inv), schema=schema, session_id=None,
                     model="", timeout_s=5, budget_usd=1, read_only=False, env=dict(os.environ))

    def test_local_schema_validation(self):
        for answer in (42, {"outcome": "done"}, dict(helpers.done(), extra=True), dict(helpers.done(), summary=True)):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as root:
                self.root = root
                stream = json.dumps({"type":"item.completed","item":{"type":"agent_message","text":json.dumps(answer)}})
                stream += '\n{"type":"turn.completed"}'
                self.assertEqual(self.call_script(stream).status, agents.PROTOCOL_ERROR)

    def test_fresh_invocation_refuses_stale_final_message(self):
        inv = Path(self.root) / "inv"
        inv.mkdir()
        (inv / "last-message.txt").write_text(json.dumps(helpers.done()))
        a = agents.make("codex", {"kind": "codex"})
        with self.assertRaises(agents.InvocationError):
            a.run("p", cwd=self.root, invocation_dir=str(inv), schema=validate.PRODUCE,
                  session_id=None, model="", timeout_s=5, budget_usd=1, read_only=False, env=dict(os.environ))

    def test_stream_parser_keeps_early_environment_failure(self):
        stream = (FIXTURES / "codex-sandbox-failure.jsonl").read_text()
        stream += '\n'.join(json.dumps({"type":"future.event","text":"x"*1000}) for _ in range(500))
        r = self.call_script(stream)
        self.assertEqual(r.status, agents.ENVIRONMENT)

    def test_tool_output_is_not_the_provider_error_channel(self):
        """A reviewer's failed `rg` printing source code is not an environment failure (PROV-15)."""
        review = json.dumps({"verdict": "pass", "findings": [], "resolutions": [], "summary": "fine"})
        events = [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "exit_code": 1,
             "command": "sed -n 1,5p x.py; rg missing", "aggregated_output":
             "        except FileNotFoundError:\n            pass\n# docs: say 'not logged in' here\n"}},
            {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": review}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ]
        a = agents.make("codex", {"kind": "codex"})
        r = a.interpret(result("\n".join(json.dumps(e) for e in events).encode()))
        self.assertEqual(r.status, agents.OK, r.error)
        # Startup failures in a failed command's output still count; provider ones do on the error channel.
        events[2]["item"]["aggregated_output"] = "bwrap: no permissions to create a new namespace\n"
        r = a.interpret(result("\n".join(json.dumps(e) for e in events).encode()))
        self.assertEqual(r.status, agents.ENVIRONMENT)
        self.assertEqual(agents.environment_error("FileNotFoundError: x"), "")
        self.assertTrue(agents.environment_error("getaddrinfo ENOTFOUND api.anthropic.com"))

if __name__ == "__main__":
    unittest.main()
