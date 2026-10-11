"""Agents: one interface, and an adapter per kind of command-line agent (05, Agent interface).

The command, Claude Code, Codex and GitHub Copilot CLI adapters plug into REGISTRY by their kind. An adapter builds the command line, runs it under the runner's clock
with streamed and redacted logs, and says whether the call completed properly. It reports facts;
the engine decides what follows.
"""

import json
import math
import os
import datetime
import glob
import re
import subprocess
import time
import uuid

from . import proc, record, validate, activity

OK, ENVIRONMENT, PROTOCOL_ERROR, AGENT_ERROR, TIMED_OUT, INTERRUPTED = (
    "ok", "environment", "protocol-error", "agent-error", "timed-out", "interrupted")
TRANSIENT = "transient"      # the provider failed, not the agent: worth another call


def transient_error(text):
    """Provider-side failures that a later call may not see: capacity, overload, a dropped
    connection. Only error-channel evidence, like `quota_error`."""
    return isinstance(text, str) and bool(re.search(
        r"at capacity|overloaded|server (?:is )?busy|temporarily unavailable|service unavailable|"
        r"try again later|(?:connection|socket) (?:reset|timed out|closed)|"
        r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|EPIPE|\b(?:502|503|504|529)\b.*(?:error|gateway|unavailable|overloaded)|"
        r"\bapi error:?\s*5\d\d\b|\"type\"\s*:\s*\"api_error\"|\binternal server error\b|\bunexpected status 5\d\d\b",
        text, re.IGNORECASE))


QUOTA = "quota"


def quota_error(text):
    """Only provider error-channel evidence, never tool output or successful prose."""
    return isinstance(text, str) and bool(re.search(
        r"usage_limit_reached|insufficient_quota|rate_limit_exceeded|rate_limit_error|"
        r"\bapi error:?\s*429\b|\b429 too many requests\b|\bclaude ai usage limit reached\b|"
        r"you(?:'|’)ve hit your (?:usage )?limit|"
        r"(?:weekly|5.hour|five.hour|session) (?:usage )?limit (?:reached|exceeded)",
        text, re.IGNORECASE))


class AgentResult:
    def __init__(self, status, text="", structured=None, session_id=None, cost_usd=None,
                 usage=None, error="", seconds=0.0, usage_source="terminal", limits=None,
                 estimated_usd=None, estimated_counts=False, reported_cost_usd=None,
                 completed=False, before_work=False, cleanup=None):
        self.status, self.text, self.structured = status, text, structured
        self.completed = completed
        self.cleanup = cleanup
        # A provider failure seen to end before any work (BUD-15): set by the adapter only on
        # positive evidence, and then the call spends nothing (budgets.settle). Kept in
        # `outcome()`, so a result read back from the record after a kill keeps it.
        self.before_work = before_work
        self.session_id, self.cost_usd, self.usage = session_id, cost_usd, usage or {}
        # The provider's own figure when `cost_usd` was reduced to this call's share of a
        # continued session (BUD-13); None otherwise.
        self.reported_cost_usd = reported_cost_usd
        self.error, self.seconds = error, seconds
        self.usage_source = usage_source      # "terminal": the provider's final event; "provider-record": read from its on-disk record after a call that had no final event
        # The provider's rate-limit window as the call saw it (Codex rollouts), or None.
        self.limits = limits
        # Dollars from the profile's price_per_mtok, for an agent that reports no cost, or None.
        # Never known spend; counted against run_budget_usd only with estimated_counts.
        self.estimated_usd, self.estimated_counts = estimated_usd, estimated_counts

    def outcome(self):
        out = {"status": self.status, "error": self.error, "seconds": round(self.seconds, 3),
               "session_id": self.session_id, "cost_usd": self.cost_usd, "usage": self.usage,
               **({"reported_cost_usd": self.reported_cost_usd}
                  if self.reported_cost_usd is not None else {}),
               "usage_source": self.usage_source, "completed": self.completed}
        if self.before_work:
            out["before_work"] = True
        if self.cleanup:
            out["cleanup"] = self.cleanup
        if self.limits:
            out["limits"] = self.limits
        if self.estimated_usd is not None:
            out.update(estimated_usd=self.estimated_usd, estimated_counts=self.estimated_counts)
        return out


def _epoch(stamp):
    """Seconds since the epoch of an ISO-8601 UTC stamp such as 2026-09-23T07:04:19.280Z."""
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def partial_usage(kind, invocation_dir, since, env=None, until=None):
    """Usage of a call that ended without the provider's terminal event (interrupted, timed out,
    killed as an orphan), read from the provider's own record on disk: a Claude session
    transcript, or a Codex rollout. Only rows stamped at or after `since` (epoch seconds) count,
    so a resumed session's earlier turns are left out. Returns {} when nothing can be read; the
    call then stays "unknown usage"."""
    return _provider_read(kind, "read_provider_record", invocation_dir, since, env, until)


def partial_limits(kind, invocation_dir, since, env=None, until=None):
    """The provider's rate-limit window as this call saw it, from the rows of its own record
    stamped between `since` and `until` (Codex: the rollout's `token_count` rows). {} when the
    agent kind keeps no such record or no row was written in the call."""
    return _provider_read(kind, "read_provider_limits", invocation_dir, since, env, until)


def _provider_read(kind, method, invocation_dir, since, env, until):
    reader = getattr(REGISTRY.get(kind), method, None)
    if reader is None or since is None:
        return {}
    env = env if env is not None else os.environ
    try:
        if until is None:
            return reader(invocation_dir, float(since), env)
        return reader(invocation_dir, float(since), env, until=float(until))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def price(profile, usage):
    """Dollars for `usage` at the profile's `price_per_mtok` rates (USD per million tokens), or
    None when the profile has no rates or the call's usage is unknown. Cached input tokens are a
    part of `tokens_in` and are charged at `cached_input` (default: the `input` rate)."""
    rates = profile.get("price_per_mtok")
    if not rates or not usage:
        return None
    tokens_in = int(usage.get("tokens_in", 0))
    cached = min(int(usage.get("cached_tokens_in", 0)), tokens_in)
    dollars = ((tokens_in - cached) * rates["input"] + cached * rates.get("cached_input", rates["input"])
               + int(usage.get("tokens_out", 0)) * rates["output"]) / 1_000_000
    return round(dollars, 6)


def last_json_object(text):
    """The last JSON object in `text` that is followed by nothing but white space, or None."""
    decoder = json.JSONDecoder()
    start = len(text)
    while True:
        start = text.rfind("{", 0, start)
        if start < 0:
            return None
        try:
            obj, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):        # nested too deeply is not an answer either
            continue
        if isinstance(obj, dict) and not text[end:].strip():
            return obj


class Agent:
    """What every adapter offers. `profile` is the [agents.NAME] table of the workflow."""

    kind = ""
    reports_cost = False

    def __init__(self, name, profile):
        self.name, self.profile = name, dict(profile)

    def capabilities(self):
        """What this agent can be relied on for: what the
        adapter supports by construction, not what was qualified on this host."""
        return set()

    def run(self, prompt, *, cwd, invocation_dir, schema, session_id, model, timeout_s,
            budget_usd, read_only, env, on_start=None):
        raise NotImplementedError

    def priced(self, result):
        """Attach the profile's estimate to a call of an agent that reports no cost."""
        if not self.reports_cost and result.cost_usd is None:
            result.estimated_usd = price(self.profile, result.usage)
            result.estimated_counts = bool(self.profile.get("estimated_counts")) and result.estimated_usd is not None
        return result


class CommandAgent(Agent):
    """Any command: the prompt on standard input, the answer the last JSON object on standard
    output. No sessions, no money limit, no tool events."""

    kind = "command"

    def capabilities(self):
        return {"answer", "read", "write", "execute"}

    def argv(self, read_only, session_id):
        return list(self.profile["argv"]) + (list(self.profile.get("read_only_args", []))
                                             if read_only else [])

    def run(self, prompt, *, cwd, invocation_dir, schema, session_id, model, timeout_s,
            budget_usd, read_only, env, on_start=None):
        claim_invocation(invocation_dir)
        argv = self.argv(read_only, session_id)
        record.write_durable(os.path.join(invocation_dir, "argv.json"),
                             proc.redact(record.dump_json({"argv": argv, "cwd": cwd, "read_only": read_only,
                                               "model": model, "session_id": session_id})))
        record.write_durable(os.path.join(invocation_dir, "schema.json"), record.dump_json(schema))
        res = proc.run_process(argv, cwd=cwd, env=env, stdin_data=prompt.encode("utf-8"),
                               stdout_path=os.path.join(invocation_dir, "stdout.log"),
                               stderr_path=os.path.join(invocation_dir, "stderr.log"),
                               timeout_s=timeout_s, on_start=on_start)
        result = self.interpret(res)
        result.cleanup = res.cleanup
        with open(os.path.join(invocation_dir, "last-message.txt"), "w", encoding="utf-8") as fh:
            fh.write(result.text)
        return self.priced(result)

    def interpret(self, res):
        if res.status == "not-started":
            return AgentResult(ENVIRONMENT, error=f"the agent could not be started: {res.error}",
                               before_work=True)
        text = res.stdout_tail.decode("utf-8", errors="replace")
        if res.status == "timed-out":
            return AgentResult(TIMED_OUT, text=text, error="the call ran past its time limit",
                               seconds=res.seconds)
        if res.returncode != 0:
            tail = res.stderr_tail.decode("utf-8", errors="replace")[-2000:]
            return AgentResult(AGENT_ERROR, text=text, seconds=res.seconds,
                               error=f"the agent exited with status {res.returncode}: {tail}".strip())
        answer = last_json_object(text)
        if answer is None:
            return AgentResult(PROTOCOL_ERROR, text=text, seconds=res.seconds,
                               error="the output does not end with a JSON object", completed=True)
        return AgentResult(OK, text=text, structured=answer, seconds=res.seconds, completed=True)


REGISTRY = {"command": CommandAgent}


class UnknownAgent(Exception):
    pass


def make(name, profile):
    kind = profile.get("kind", name)
    if kind not in REGISTRY:
        raise UnknownAgent(f"agent '{name}' has unsupported kind '{kind}'")
    return REGISTRY[kind](name, profile)


def agent_env(base, run_id, task_id, run_dir=None):
    """The environment of an agent call. The record's path goes only to types that ask for it."""
    env = {k: v for k, v in base.items()
           if k not in ("CODE_SMITH_RUN_DIR", "CODE_SMITH_RUNS_DIR")}   # see checks.command_env
    env.update(CODE_SMITH_RUN=run_id, CODE_SMITH_TASK=task_id)
    if run_dir:
        env["CODE_SMITH_RUN_DIR"] = run_dir
    return env


class InvocationError(Exception):
    """An invocation directory must be fresh; stale output is never evidence."""


def claim_invocation(path):
    if any(os.path.exists(os.path.join(path, name)) for name in
           ("last-message.txt", "stdout.log", "stderr.log", "final.raw")):
        raise InvocationError("the invocation directory contains output from an earlier call")
    try:
        with open(os.path.join(path, ".adapter-started"), "x"):
            pass
    except FileExistsError as exc:
        raise InvocationError("this invocation directory has already been used") from exc


# Match failures of the execution/authentication machinery, not ordinary failing tests. Each
# marker matches as whole words, so `ENOTFOUND` is not seen inside `FileNotFoundError`.
STARTUP_MARKERS = (
    "bwrap: no permissions to create a new namespace", "sandbox failed to start",
    "failed to create sandbox", "unprivileged user namespaces are unavailable",
)
NETWORK_MARKERS = (
    "can't reach the api server", "can’t reach the api server", "enotfound", "network is unreachable",
)
PROVIDER_MARKERS = NETWORK_MARKERS + (
    "invalid api key", "invalid_api_key", "authentication failed", "not logged in",
    "unexpected argument", "unknown option", "invalid value for", "please run /login", "please run codex login", "error loading config", "invalid configuration",
)
ENVIRONMENT_MARKERS = STARTUP_MARKERS + PROVIDER_MARKERS
# Copilot CLI 1.0.75 prompt mode, before any session event (its shipped bundle's stderr text).
# Applied to Copilot calls only: a command agent or another CLI that prints one of these phrases
# (say, a test that checks a model-selection message) has not hit an environment failure.
COPILOT_MARKERS = (
    "no authentication information found", "authentication token found but could not be validated",
    "classic personal access tokens", "from --model flag is not available", "to enable this model",
    "no supported model available", "allowed choices are",
    "access denied by policy settings",   # the account's Copilot CLI policy is off
)


def _marker_pattern(markers):
    return re.compile("|".join(r"(?<![\w])" + re.escape(m) + r"(?![\w])" for m in markers), re.IGNORECASE)


_ALL_MARKERS = _marker_pattern(ENVIRONMENT_MARKERS)
_COPILOT_ALL = _marker_pattern(ENVIRONMENT_MARKERS + COPILOT_MARKERS)
_STARTUP_ONLY = _marker_pattern(STARTUP_MARKERS)
_NETWORK = _marker_pattern(NETWORK_MARKERS)


def network_error(text):
    """An environment failure that says nothing about the agent's setup: the network was down.
    What `doctor` learned about the profile still holds, so a run stopped for this is not
    re-qualified on `resume`."""
    return isinstance(text, str) and bool(_NETWORK.search(text))


def environment_error(text, startup_only=False, copilot=False):
    """The first line of `text` naming an environment failure. Text an agent's own tool commands
    printed is checked for startup failures only: source code or documentation that mentions
    "not logged in" is not evidence that the agent is. `copilot` adds the Copilot CLI's own
    markers, for text that the Copilot CLI itself wrote."""
    if not isinstance(text, str):
        return ""
    pattern = _STARTUP_ONLY if startup_only else _COPILOT_ALL if copilot else _ALL_MARKERS
    return next((line[:2000] for line in text.splitlines() if pattern.search(line)), "")


def process_failure(res, copilot=False):
    if res.status == "not-started":
        return AgentResult(ENVIRONMENT, error=res.error, seconds=res.seconds, before_work=True)
    if res.status == "timed-out":
        return AgentResult(TIMED_OUT, error="the call ran past its time limit", seconds=res.seconds)
    err = res.stderr_tail.decode("utf-8", errors="replace")
    env_error = environment_error(err, copilot=copilot)
    if env_error:
        return AgentResult(ENVIRONMENT, error=env_error, seconds=res.seconds)
    if res.returncode:
        return AgentResult(AGENT_ERROR, error=f"agent exited with status {res.returncode}: {err[-2000:]}",
                           seconds=res.seconds)
    return None


class CodexEvents:
    """Incremental reduction of redacted JSONL. Only the final message and counters are retained.
    Unknown events remain in stdout.log. Early environment/turn failures cannot scroll out."""
    def __init__(self):
        self.session_id = None
        self.text = ""
        self.completed = 0
        self.open_turn = False
        self.message_after_completion = False
        self.failed = ""
        self.failed_environment = ""        # what `failed` said, cleared with it
        self.environment = ""
        self.malformed = False
        self.omitted_events = 0
        self.has_usage = False
        self.usage = {"tokens_in": 0, "tokens_out": 0, "cached_tokens_in": 0}
        self.reasoning = None                   # reasoning_output_tokens, when the CLI reports them
        self.items = 0                          # item.* events: the agent did work in this call

    def feed(self, data):
        for line in data.splitlines():
            if not line.strip():
                continue
            if line.strip() in (proc.OVERLONG_PLACEHOLDER, proc.OVERLONG_PLACEHOLDER.decode()):
                # The sink withheld one event longer than its line limit: in practice a
                # command_execution item carrying a large file the agent read. It is not a
                # malformed stream. The answer and the terminal event are still required, so an
                # omitted final message fails later for want of a valid answer, never silently.
                self.omitted_events += 1
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                self.malformed = True
                continue
            if not isinstance(event, dict):
                self.malformed = True
                continue
            kind = event.get("type")
            if isinstance(kind, str) and kind.startswith("item."):
                self.items += 1
            if kind == "thread.started":
                self.session_id = event.get("thread_id")
                if not isinstance(self.session_id, str):
                    self.malformed = True
            elif kind == "turn.started":
                self.open_turn = True
            elif kind == "turn.completed":
                self.open_turn = False
                self.message_after_completion = False
                self.completed += 1
                # An `error` the CLI recovered from (a reconnect) does not decide a completed turn.
                self.failed, self.failed_environment = "", ""
                for source, target in (("input_tokens", "tokens_in"), ("output_tokens", "tokens_out"),
                                       ("cached_input_tokens", "cached_tokens_in")):
                    usage = event.get("usage") or {}
                    if not isinstance(usage, dict):
                        self.malformed = True
                        continue
                    value = usage.get(source, 0)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        self.has_usage = self.has_usage or source in usage
                        self.usage[target] += value
                value = (event.get("usage") or {}).get("reasoning_output_tokens") \
                    if isinstance(event.get("usage"), dict) else None
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self.reasoning = (self.reasoning or 0) + value
            elif kind in ("turn.failed", "error"):
                self.failed = json.dumps(event.get("error", event.get("message", event)))[:2000]
                self.failed_environment = environment_error(self.failed) or self.failed_environment
            elif kind == "item.completed":
                item = event.get("item") or {}
                if not isinstance(item, dict):
                    self.malformed = True
                    continue
                if item.get("type") == "agent_message":
                    self.message_after_completion = bool(self.completed)
                    self.text = item.get("text", "")
                    if not isinstance(self.text, str):
                        self.text = ""
                        self.malformed = True
                elif item.get("type") == "command_execution" and item.get("exit_code") not in (None, 0):
                    self.environment = (environment_error(item.get("aggregated_output", ""), startup_only=True)
                                        or self.environment)

    def counted(self):
        """The usage the terminal events carried, or {} when none did."""
        if not self.has_usage:
            return {}
        return dict(self.usage, **({} if self.reasoning is None else {"reasoning_tokens_out": self.reasoning}))

    def result(self, res):
        failure = process_failure(res)
        if self.failed and quota_error(self.failed) and res.status != "timed-out":
            failure = AgentResult(QUOTA, error=self.failed, seconds=res.seconds)
        elif self.failed and transient_error(self.failed) and res.status != "timed-out":
            failure = AgentResult(TRANSIENT, error=self.failed, seconds=res.seconds)
        if self.environment or self.failed_environment:
            failure = AgentResult(ENVIRONMENT, error=self.environment or self.failed_environment,
                                  seconds=res.seconds)
        if failure:
            failure.session_id, failure.usage = self.session_id, self.counted()
            return failure
        status, error = OK, ""
        if self.failed:
            status, error = (QUOTA if quota_error(self.failed) else
                             TRANSIENT if transient_error(self.failed) else AGENT_ERROR), self.failed
        elif self.malformed or not self.completed or self.open_turn or self.message_after_completion:
            status, error = PROTOCOL_ERROR, "missing successful terminal event or malformed event stream"
        completed = status == OK
        answer = last_json_object(self.text) if isinstance(self.text, str) else None
        if status == OK and answer is None:
            status, error = PROTOCOL_ERROR, "the final agent message is not a JSON object"
        return AgentResult(status, text=self.text, structured=answer, session_id=self.session_id,
                           usage=self.counted(), error=error, seconds=res.seconds, completed=completed)


class HeadlessAgent(Agent):
    # True when the CLI's terminal event carries no token counts, so even a successful call's
    # usage is read from the provider's own record on disk (Copilot).
    usage_from_record = False

    def capabilities(self):
        return {"answer", "read", "write", "execute", "resume"}

    def events(self):
        """A stream parser fed stdout as it arrives (JSONL CLIs), or None to interpret at the end."""
        return None

    def call_env(self, env, read_only):
        return env

    def run(self, prompt, *, cwd, invocation_dir, schema, session_id, model, timeout_s,
            budget_usd, read_only, env, on_start=None):
        claim_invocation(invocation_dir)
        record.write_durable(os.path.join(invocation_dir, "schema.json"), record.dump_json(schema))
        argv = self.build_argv(invocation_dir, schema, session_id, model, budget_usd, read_only)
        record.write_durable(os.path.join(invocation_dir, "argv.json"), proc.redact(record.dump_json(
            {"argv": argv, "cwd": cwd, "read_only": read_only, "model": model, "session_id": session_id})))
        env = activity.prepare(self.kind, cwd, invocation_dir, self.call_env(env, read_only))
        parser = self.events()
        telemetry = activity.CodexTelemetry(cwd, env) if self.kind == "codex" else None
        def on_stdout(data):
            parser.feed(data)
            if telemetry:
                telemetry.feed(data)
        started_at = time.time()
        res = proc.run_process(argv, cwd=cwd, env=env, stdin_data=prompt.encode("utf-8"),
                               stdout_path=os.path.join(invocation_dir, "stdout.log"),
                               stderr_path=os.path.join(invocation_dir, "stderr.log"),
                               timeout_s=timeout_s, on_start=on_start,
                               on_stdout=on_stdout if parser else None, last_line=parser is None,
                               on_stdout_overlong=parser.feed if parser else None)
        answer = parser.result(res) if parser else self.interpret(res)
        answer.cleanup = res.cleanup
        if not answer.usage and (answer.status != OK or self.usage_from_record) and answer.cost_usd is None:
            # No terminal event carried usage (a time-out, a kill, a crash mid-call, or a CLI
            # whose terminal event has no token counts): the provider's own record still says
            # what the call used.
            usage = partial_usage(self.kind, invocation_dir, started_at, env)
            if usage:
                answer.usage, answer.usage_source = usage, "provider-record"
        if answer.status in (TRANSIENT, QUOTA, ENVIRONMENT) and not answer.usage and answer.cost_usd is None:
            answer.before_work = (res.status == "not-started" or
                                  self.refused_before_work(parser, invocation_dir, started_at, env, res))
        # Whatever the outcome, the provider's record may say how much of its rate-limit window
        # the call used (Codex's token_count rows); absent when it says nothing.
        answer.limits = partial_limits(self.kind, invocation_dir, started_at, env, until=time.time()) or None
        self.priced(answer)
        # Codex's output file is never used as a fallback for a missing terminal event.
        raw = os.path.join(invocation_dir, "final.raw")
        if os.path.lexists(raw):
            os.unlink(raw)
        if answer.status == OK and session_id and answer.session_id != session_id:
            answer.status, answer.error = PROTOCOL_ERROR, "resumed call returned a different session id"
            answer.completed = False
        if answer.status == OK:
            errors = validate.check_shape(answer.structured, schema)
            if errors:
                answer.status, answer.error = PROTOCOL_ERROR, "; ".join(errors)
        record.write_durable(os.path.join(invocation_dir, "last-message.txt"),
                             proc.redact(answer.text.encode("utf-8")))
        return answer

    def executable(self):
        return list(self.profile.get("argv") or [self.kind])

    def refused_before_work(self, parser, invocation_dir, since, env, res=None):
        """Whether a provider failure with no usage provably came before any work (BUD-15).
        False unless the adapter can tell: an unknown case stays held, never free. `res` is the
        finished process (its stdout tail), for an adapter whose evidence is its final output."""
        return False


class ClaudeAgent(HeadlessAgent):
    kind = "claude"
    reports_cost = True

    def build_argv(self, invocation_dir, schema, session_id, model, budget_usd, read_only):
        argv = self.executable() + list(self.profile.get("extra_args", []))
        argv += ["-p", "--output-format", "json", "--permission-mode",
                 self.profile.get("permission_mode", "auto"), "--permission-prompts", "none",
                 "--json-schema", json.dumps(schema), "--max-budget-usd", str(budget_usd)]
        if self.profile.get("ignore_user_config"):
            argv += ["--setting-sources", "project,local"]
        if model or self.profile.get("model"):
            argv += ["--model", model or self.profile["model"]]
        if session_id:
            argv += ["--resume", session_id]
        else:
            # Chosen here so the transcript of a call that never returns can still be found.
            argv += ["--session-id", str(uuid.uuid4())]
        if read_only:
            # Disabling Edit/Write alone leaves shell and delegated writes available. Reviewers
            # get only local read/search tools and no MCP servers, then doctor verifies the boundary.
            argv += ["--tools", "Read,Glob,Grep", "--disallowedTools", "Bash,Edit,Write,NotebookEdit,Agent",
                     "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        return argv

    @staticmethod
    def transcript(invocation_dir, env):
        """The session transcript of the call recorded in `invocation_dir`, or None."""
        with open(os.path.join(invocation_dir, "argv.json"), encoding="utf-8") as fh:
            recorded = json.load(fh)
        argv, cwd = recorded["argv"], recorded["cwd"]
        session = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("--session-id", "--resume")), None)
        if not session or not re.fullmatch(r"[\w-]+", session):
            return None
        home = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
        # Claude names the directory after its physical working directory: /tmp is /private/tmp on macOS.
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
        return os.path.join(home, "projects", slug, session + ".jsonl")

    def refused_before_work(self, parser, invocation_dir, since, env, res=None):
        """Claude: positive evidence first, and no field may contradict it (review V-08). Each of
        `num_turns` and `duration_api_ms` the error envelope holds must be 0, and at least one
        must be there; without that the failure is not provably before work, whatever the
        transcript says (BUD-19). With it, a transcript that holds an assistant message written
        since the call began still says work was done. A transcript the runner cannot find
        (another `CLAUDE_CONFIG_DIR`, another slug) proves nothing, so then only the envelope's
        own `duration_api_ms` 0 (no API time at all) makes it free (BUD-22)."""
        _text, data = self.envelope(res) if res is not None else ("", None)
        if not isinstance(data, dict):
            return False
        present = [data[k] for k in ("num_turns", "duration_api_ms") if k in data]
        if not present or any(v != 0 or isinstance(v, bool) for v in present):
            return False
        try:
            path = self.transcript(invocation_dir, env)
            if not path or not os.path.exists(path):
                return "duration_api_ms" in data
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict) or row.get("type") != "assistant":
                        continue
                    stamp = _epoch(row.get("timestamp"))
                    if stamp is None or stamp >= since - 1:
                        return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    @staticmethod
    def read_provider_record(invocation_dir, since, env):
        """Sum the usage of this call's API responses from the session transcript
        `$CLAUDE_CONFIG_DIR/projects/<cwd slug>/<session>.jsonl`. Claude writes one row per
        content block, all carrying the response's usage, so rows are counted once per request."""
        path = ClaudeAgent.transcript(invocation_dir, env)
        if not path or not os.path.isfile(path):
            return {}
        seen, counts = set(), {"tokens_in": 0, "tokens_out": 0}
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("type") != "assistant":
                    continue
                stamp = _epoch(row.get("timestamp"))
                message = row.get("message") if isinstance(row.get("message"), dict) else {}
                usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
                key = row.get("requestId") or message.get("id")
                if stamp is None or stamp < since - 1 or not usage or key in seen:
                    continue
                seen.add(key)
                for source, target in (("input_tokens", "tokens_in"), ("cache_creation_input_tokens", "tokens_in"),
                                       ("cache_read_input_tokens", "tokens_in"), ("output_tokens", "tokens_out")):
                    value = usage.get(source, 0)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        counts[target] += value
        return counts if seen else {}

    @staticmethod
    def envelope(res):
        """(text, data) of Claude's JSON result: the whole of stdout, or else its last line, which
        the log withholds when it is longer than its line limit. `data` is None when neither parses."""
        text = res.stdout_tail.decode("utf-8", errors="replace")
        for candidate in (text, getattr(res, "stdout_last", b"").decode("utf-8", errors="replace")):
            try:
                return candidate, json.loads(candidate)
            except (ValueError, RecursionError):
                continue
        return text, None

    @staticmethod
    def spend(data):
        """(cost, counts) a result envelope reports; counts is None when its usage is malformed."""
        usage = data.get("usage") or {}
        if not isinstance(usage, dict) or any(not isinstance(v, int) or isinstance(v, bool) or v < 0
                for k, v in usage.items() if k in ("input_tokens", "output_tokens",
                                                 "cache_creation_input_tokens", "cache_read_input_tokens")):
            counts = None
        elif not usage:
            counts = {}
        else:
            counts = {"tokens_in": sum(usage.get(k, 0) for k in
                                       ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                      "tokens_out": usage.get("output_tokens", 0)}
        cost = data.get("total_cost_usd")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(cost) or cost < 0:
            cost = None
        return cost, counts

    @staticmethod
    def provider_failure(data, said):
        """(status, error) when an error result says the provider failed, not the task, else None.
        The HTTP status of the failed API request decides first; a limit of the call's own
        (turns, budget) is the task's failure whatever else the envelope carries."""
        if data.get("subtype") in ("error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"):
            return None
        code = data.get("api_error_status")
        if isinstance(code, int) and not isinstance(code, bool):
            if code == 429:
                return QUOTA, said or f"API error status {code}"
            if code >= 500:
                return TRANSIENT, said or f"API error status {code}"
        if quota_error(said) or data.get("subtype") in ("error_rate_limit", "error_usage_limit"):
            return QUOTA, said
        if environment_error(said):
            return ENVIRONMENT, environment_error(said)
        if transient_error(said):
            return TRANSIENT, said
        return None

    def interpret(self, res):
        failure = process_failure(res)
        text, data = self.envelope(res)
        if failure:
            if isinstance(data, dict) and data.get("type") == "result":
                # A non-zero exit with a result envelope: the envelope says what the failure was,
                # and the call still cost what it reports (budgets count it either way).
                if failure.status == AGENT_ERROR and data.get("is_error") is True:
                    said = data.get("result") if isinstance(data.get("result"), str) else ""
                    failure.status, failure.error = self.provider_failure(data, said) or (failure.status, failure.error)
                cost, counts = self.spend(data)
                failure.cost_usd, failure.usage = cost, counts or {}
                failure.session_id = data.get("session_id")
            if failure.status != QUOTA:
                return failure
        if data is None:
            return AgentResult(PROTOCOL_ERROR, text=text, error="Claude did not return a JSON result")
        if not isinstance(data, dict) or data.get("type") != "result":
            return AgentResult(PROTOCOL_ERROR, text=text, error="Claude did not return a terminal result")
        cost, counts = self.spend(data)
        if counts is None:
            return AgentResult(PROTOCOL_ERROR, error="Claude returned malformed usage")
        final = data.get("result", "")
        if not isinstance(final, str):
            final = json.dumps(final)
        answer = data.get("structured_output")
        if answer is None:
            answer = last_json_object(final)
        if not final and answer is not None:
            final = json.dumps(answer)
        status, error = OK, ""
        if data.get("is_error") is not False or data.get("subtype") != "success":
            status, error = self.provider_failure(data, final) or (AGENT_ERROR, f"Claude result subtype: {data.get('subtype')}")
            if status == QUOTA:
                error = error or f"Claude result subtype: {data.get('subtype')}"
        completed = status == OK
        if status == OK and not isinstance(answer, dict):
            status, error = PROTOCOL_ERROR, "Claude's final answer is not a JSON object"
        return AgentResult(status, text=final, structured=answer, session_id=data.get("session_id"),
                           cost_usd=cost, usage=counts, error=error, seconds=res.seconds, completed=completed)


class CodexAgent(HeadlessAgent):
    kind = "codex"

    def events(self):
        return CodexEvents()

    def build_argv(self, invocation_dir, schema, session_id, model, budget_usd, read_only):
        argv = self.executable() + ["exec"] + (["resume"] if session_id else [])
        argv += list(self.profile.get("extra_args", []))
        sandbox = "read-only" if read_only else self.profile.get("sandbox", "workspace-write")
        argv += ["-c", 'approval_policy="never"', "-c", "sandbox_mode=" + json.dumps(sandbox),
                 "--json", "--output-schema", os.path.join(invocation_dir, "schema.json"),
                 "-o", os.path.join(invocation_dir, "final.raw")]
        if self.profile.get("ignore_user_config"):
            argv += ["--ignore-user-config"]
        if model or self.profile.get("model"):
            argv += ["--model", model or self.profile["model"]]
        if session_id:
            argv.append(session_id)
        return argv + ["-"]

    @staticmethod
    def rollout(invocation_dir, env):
        """The rows of this call's rollout
        `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<stamp>-<thread>.jsonl`, or [] when it cannot be
        found. The thread id is the `thread.started` event streamed to stdout.log, or the resumed
        session in argv.json."""
        with open(os.path.join(invocation_dir, "argv.json"), encoding="utf-8") as fh:
            thread = json.load(fh).get("session_id")
        if not thread:
            try:
                with open(os.path.join(invocation_dir, "stdout.log"), "rb") as fh:
                    head = fh.read(65536).decode("utf-8", errors="replace")
            except OSError:
                head = ""
            for line in head.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "thread.started":
                    thread = event.get("thread_id")
                    break
        if not isinstance(thread, str) or not re.fullmatch(r"[\w-]+", thread):
            return []
        home = env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
        paths = glob.glob(os.path.join(glob.escape(home), "sessions", "*", "*", "*", f"rollout-*-{thread}.jsonl"))
        if not paths:
            return []
        rows = []
        with open(sorted(paths)[-1], encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("payload"), dict):
                    rows.append(row)
        return rows

    @staticmethod
    def token_counts(rows, since, until=None):
        """(the last `token_count` payload before the call, those stamped within it). Codex writes
        one after every model response; its `total_token_usage` is the session's running total."""
        before, within = None, []
        for row in rows:
            payload = row["payload"]
            stamp = _epoch(row.get("timestamp"))
            if row.get("type") != "event_msg" or payload.get("type") != "token_count" or stamp is None:
                continue
            if stamp < since - 1:
                before = payload
            elif until is None or stamp <= until + 1:
                within.append(payload)
        return before, within

    @staticmethod
    def read_provider_record(invocation_dir, since, env, until=None):
        """Sum the usage of this call's responses from its rollout: the `token_usage_record` rows
        that carry each response's usage, or else the difference between the running totals of
        the last `token_count` row in the call and the last one before it."""
        rows = CodexAgent.rollout(invocation_dir, env)
        seen, counts = set(), {"tokens_in": 0, "tokens_out": 0, "cached_tokens_in": 0}
        for row in rows:
            payload = row["payload"]
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
            stamp = _epoch(row.get("timestamp"))
            key = payload.get("response_id") or row.get("ordinal")
            if (row.get("type") != "token_usage_record" or stamp is None or stamp < since - 1
                    or (until is not None and stamp > until + 1) or not usage or key in seen):
                continue
            seen.add(key)
            for source, target in (("input_tokens", "tokens_in"), ("output_tokens", "tokens_out"),
                                   ("cached_input_tokens", "cached_tokens_in")):
                value = usage.get(source, 0)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    counts[target] += value
        if seen:
            return counts
        before, within = CodexAgent.token_counts(rows, since, until)

        def total(payload):
            info = payload.get("info") if payload and isinstance(payload.get("info"), dict) else {}
            usage = info.get("total_token_usage")
            return usage if isinstance(usage, dict) else None
        last = next((total(p) for p in reversed(within) if total(p)), None)
        if last is None:
            return {}
        base = total(before) or {}
        counts = {}
        for source, target in (("input_tokens", "tokens_in"), ("output_tokens", "tokens_out"),
                               ("cached_input_tokens", "cached_tokens_in"),
                               ("reasoning_output_tokens", "reasoning_tokens_out")):
            now, was = last.get(source, 0), base.get(source, 0)
            if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (now, was)):
                return {}
            counts[target] = max(0, now - was)
        return counts

    @staticmethod
    def read_provider_limits(invocation_dir, since, env, until=None):
        """The rate-limit window of the first and last `token_count` rows stamped within the call:
        `limits` of its outcome.json. The first row is written after the call's first response,
        so `delta_percent` leaves out that response's share. A window that reset during the call
        (its percentage fell) counts as its new percentage. {} when no row carries one."""
        _before, within = CodexAgent.token_counts(CodexAgent.rollout(invocation_dir, env), since, until)
        seen = [p["rate_limits"] for p in within if isinstance(p.get("rate_limits"), dict)]
        if not seen:
            return {}
        first, last = seen[0], seen[-1]

        def window(name):
            a, b = first.get(name), last.get(name)
            if not (isinstance(a, dict) and isinstance(b, dict)):
                return None
            was, now = a.get("used_percent"), b.get("used_percent")
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                       for v in (was, now)):
                return None
            return {"window_minutes": b.get("window_minutes"), "used_percent_before": was,
                    "used_percent_after": now,
                    "delta_percent": round(now - was if now >= was else now, 3),
                    "resets_at": b.get("resets_at")}
        primary = window("primary")
        if primary is None:
            return {}
        limits = dict(primary, kind="codex", plan_type=last.get("plan_type"),
                      limit_id=last.get("limit_id") or "default", credits=last.get("credits"))
        secondary = window("secondary")
        if secondary:
            limits["secondary"] = secondary
        return limits

    def enabled_features(self):
        """Feature names the profile switches on (`--enable NAME`, `-c features.NAME=true`)."""
        args = list(self.profile.get("extra_args", []))
        names = []
        for i, arg in enumerate(args):
            if arg == "--enable" and i + 1 < len(args):
                names.append(args[i + 1])
            elif arg.startswith("--enable="):
                names.append(arg.split("=", 1)[1])
            elif arg == "-c" and i + 1 < len(args):
                m = re.fullmatch(r"features\.([\w-]+)\s*=\s*true", args[i + 1].strip())
                if m:
                    names.append(m.group(1))
        return names

    def preflight(self, cwd, env, read_only, timeout_s=60):
        """Free checks before any model call. Returns (notes, error): `error` names why no call
        of this profile can work (the Linux sandbox cannot start), `notes` name what the owner
        should know (an enabled feature the CLI has deprecated or removed)."""
        notes, error = [], ""
        sandbox = "read-only" if read_only else self.profile.get("sandbox", "workspace-write")
        if sandbox != "danger-full-access":
            argv = self.executable() + ["sandbox"] + list(self.profile.get("extra_args", [])) + ["--", "true"]
            try:
                res = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout_s)
                if res.returncode != 0:
                    said = (res.stderr or res.stdout).decode("utf-8", errors="replace").strip()
                    line = (environment_error(said, startup_only=True) or (said.splitlines() or [""])[-1]
                            or f"exited with status {res.returncode}")
                    error = "the Codex sandbox cannot start on this host: " + line[:500]
            except (OSError, subprocess.TimeoutExpired) as exc:
                error = f"the Codex sandbox could not be checked: {exc}"
        wanted = self.enabled_features()
        if wanted:
            status = {}
            try:
                out = subprocess.run(self.executable() + ["features", "list"], cwd=cwd, env=env,
                                     capture_output=True, timeout=timeout_s).stdout.decode("utf-8", errors="replace")
                for line in out.splitlines():
                    m = re.match(r"^(\S+)\s+(.*?)\s+(true|false)\s*$", line)
                    if m:
                        status[m.group(1)] = m.group(2).strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
            for name in wanted:
                state = status.get(name)
                if state in ("deprecated", "removed"):
                    notes.append(f"feature '{name}' is {state} in this Codex CLI"
                                 + (": the profile depends on it, so the next CLI may need a different sandbox "
                                    "(unprivileged user namespaces for bwrap) or another agent" if state == "deprecated"
                                    else ": the profile's --enable has no effect"))
        return notes, error

    def interpret(self, res):
        parser = CodexEvents()
        parser.feed(res.stdout_tail)
        return parser.result(res)

    def refused_before_work(self, parser, invocation_dir, since, env, res=None):
        """Codex: the stream failed, and the whole of it, not only what came before the error,
        holds no `item.*` event, no completed turn and no line that could have hidden one (a
        malformed or withheld event). A "model at capacity" refusal prints thread.started,
        turn.started, error, turn.failed; work after a retryable error is still work (BUD-19)."""
        return bool(parser is not None and parser.failed and not parser.items
                    and not parser.completed and not parser.malformed and not parser.omitted_events)


class CopilotEvents:
    """Incremental reduction of `copilot --output-format json`: one session event per line, then
    a closing `result` record (sessionId, exitCode, usage.premiumRequests). The shapes are those of
    the CLI's shipped event schema; no live output has been captured (docs/agent-compatibility.md).
    Unknown events remain in stdout.log."""

    # A session.error of these types ends the call (the CLI exits 1); `model_call` does not.
    QUOTA_TYPES, ENVIRONMENT_TYPES = ("quota", "rate_limit"), ("authentication", "authorization")
    FATAL_WARNINGS = ("compaction_static_context_blocked", "policy_blocked")

    def __init__(self):
        self.session_id = None
        self.text = ""
        self.closing = None                 # the closing `result` record, once seen
        self.after_result = False           # any record after it, a second result included
        self.error = None                   # the last fatal session.error's data
        self.warning = ""                   # a fatal session.warning's message
        self.malformed = False
        self.omitted_events = 0

    def feed(self, data):
        for line in data.splitlines():
            if not line.strip():
                continue
            # The closing `result` is exactly one and the last record (05, Copilot): anything after
            # it, another result included, makes the stream a protocol error.
            self.after_result = self.after_result or self.closing is not None
            if line.strip() in (proc.OVERLONG_PLACEHOLDER, proc.OVERLONG_PLACEHOLDER.decode()):
                # A tool result carrying a large file: not a malformed stream (see CodexEvents).
                self.omitted_events += 1
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                self.malformed = True
                continue
            if not isinstance(event, dict):
                self.malformed = True
                continue
            kind, body = event.get("type"), event.get("data")
            body = body if isinstance(body, dict) else {}
            if kind == "result":
                self.closing = event
                if isinstance(event.get("sessionId"), str):
                    self.session_id = event["sessionId"]
                if not isinstance(event.get("exitCode"), int) or isinstance(event.get("exitCode"), bool):
                    self.malformed = True
            elif kind == "session.start" and isinstance(body.get("sessionId"), str):
                self.session_id = self.session_id or body["sessionId"]
            elif kind == "assistant.message":
                # Only the main agent answers; a sub-agent's messages carry agentId/parentToolCallId,
                # and a message that requests tools is a step, not the final answer.
                if event.get("agentId") or body.get("parentToolCallId") or body.get("toolRequests"):
                    continue
                if not isinstance(body.get("content"), str):
                    self.malformed = True
                    continue
                self.text = body["content"]
            elif kind == "session.error" and body.get("errorType") != "model_call":
                self.error = body
            elif kind == "session.warning" and body.get("warningType") in self.FATAL_WARNINGS:
                self.warning = f"{body.get('warningType')}: {body.get('message', '')}"[:2000]

    def provider_failure(self):
        """(status, error) of the fatal session.error, by its errorType first and its text after."""
        if self.warning:
            return (ENVIRONMENT if self.warning.startswith("policy_blocked") else AGENT_ERROR), self.warning
        if self.error is None:
            return None
        said = json.dumps({k: self.error.get(k) for k in ("errorType", "errorCode", "statusCode", "message")
                           if k in self.error})[:2000]
        kind, code = self.error.get("errorType"), self.error.get("statusCode")
        if kind in self.QUOTA_TYPES or code == 429 or quota_error(said):
            return QUOTA, said
        if kind in self.ENVIRONMENT_TYPES or environment_error(said, copilot=True):
            return ENVIRONMENT, said
        if (isinstance(code, int) and not isinstance(code, bool) and code >= 500) or transient_error(said):
            return TRANSIENT, said
        return AGENT_ERROR, said

    def result(self, res):
        failure = process_failure(res, copilot=True)
        provider = self.provider_failure()
        if provider and res.status != "timed-out" and (failure is None or failure.status == AGENT_ERROR):
            failure = AgentResult(provider[0], error=provider[1], seconds=res.seconds)
        if failure:
            failure.session_id = self.session_id
            return failure
        status, error = OK, ""
        if self.malformed or self.closing is None or self.after_result:
            status, error = PROTOCOL_ERROR, "missing terminal result record or malformed event stream"
        elif self.closing.get("exitCode") != 0:
            status, error = AGENT_ERROR, f"Copilot result exitCode {self.closing.get('exitCode')}"
        completed = status == OK
        answer = last_json_object(self.text) if isinstance(self.text, str) else None
        if status == OK and answer is None:
            status, error = PROTOCOL_ERROR, "the final agent message is not a JSON object"
        # The stream has no token counts: usage comes from the session record (read_provider_record).
        return AgentResult(status, text=self.text, structured=answer, session_id=self.session_id,
                           error=error, seconds=res.seconds, completed=completed)


class CopilotAgent(HeadlessAgent):
    """GitHub Copilot CLI in prompt mode: the prompt on standard input (piped input is a prompt
    when -p is absent), JSONL session events on standard output. The CLI reports premium requests,
    not dollars, and its stream carries no token counts, so usage is read from the session's
    events.jsonl and the token stop line governs it (05, Agent interface)."""

    kind = "copilot"
    usage_from_record = True
    # Read-only tools only; built-in names from the CLI reference. Anything else that asks for
    # permission is denied in prompt mode, and the deny rules win even over a stored approval.
    READ_ONLY_TOOLS = "view,glob,grep"
    DENIED_WHEN_READ_ONLY = "shell,write,url,memory"
    # A writer of a profile with permission_mode = "bypassPermissions" (05, Permissions). A
    # read-only call never gets them: its read tools and deny rules apply as for any reviewer.
    YOLO_ARGS = ("--allow-all-tools", "--allow-all-paths", "--allow-all-urls")

    def events(self):
        return CopilotEvents()

    # Environment variables that change what a call runs without appearing in argv or in the
    # qualification fingerprint (`copilot help environment`, 1.0.75). COPILOT_ALLOW_ALL=true is
    # --allow-all-tools by another name; COPILOT_MODEL picks the model of a profile with no `model`;
    # COPILOT_OFFLINE and COPILOT_PROVIDER_* (BYOK) replace GitHub's model routing with another
    # provider. All are removed, so the profile and argv alone decide permissions, model and provider.
    STRIPPED_ENV = ("COPILOT_ALLOW_ALL", "COPILOT_MODEL", "COPILOT_OFFLINE")
    STRIPPED_ENV_PREFIXES = ("COPILOT_PROVIDER_",)

    def call_env(self, env, read_only):
        return {k: v for k, v in env.items()
                if k not in self.STRIPPED_ENV and not k.startswith(self.STRIPPED_ENV_PREFIXES)}

    def build_argv(self, invocation_dir, schema, session_id, model, budget_usd, read_only):
        argv = self.executable() + list(self.profile.get("extra_args", []))
        # --no-remote: the session cannot be remote-controlled from GitHub web or mobile mid-call.
        argv += ["--output-format", "json", "--stream", "off", "--no-ask-user", "--no-auto-update", "--no-remote"]
        if read_only:
            argv += ["--available-tools=" + self.READ_ONLY_TOOLS, "--deny-tool=" + self.DENIED_WHEN_READ_ONLY,
                     "--disable-builtin-mcps"]
        elif self.profile.get("permission_mode") == "bypassPermissions":
            # The explicit YOLO profile, Claude's bypassPermissions: every permission at once, the
            # three flags `--allow-all`/`--yolo` stand for, spelled out so argv.json says which.
            argv += list(self.YOLO_ARGS)
        else:
            # Required for tools to run unattended; path checks (work tree and temp dir) stay on.
            argv += ["--allow-all-tools"]
        if model or self.profile.get("model"):
            argv += ["--model", model or self.profile["model"]]
        if session_id:
            argv += ["--resume=" + session_id]
        else:
            # Chosen here so the session record of a call that never returns can be found.
            argv += ["--session-id", str(uuid.uuid4())]
        return argv

    @staticmethod
    def read_provider_record(invocation_dir, since, env):
        """Usage of this call from `$COPILOT_HOME/session-state/<session>/events.jsonl`: the
        `session.shutdown` rows written at or after `since` carry per-model token counts
        (modelMetrics.*.usage) and the premium requests used. A call killed before its shutdown
        row has none, and stays unknown usage. A resumed session's shutdown counts may include its
        earlier calls (unverified): that over-counts, so the stop line comes early, never late."""
        with open(os.path.join(invocation_dir, "argv.json"), encoding="utf-8") as fh:
            argv = json.load(fh)["argv"]
        session = next((a.split("=", 1)[1] for a in argv if a.startswith("--resume=")), None) or next(
            (argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--session-id"), None)
        if not session or not re.fullmatch(r"[\w-]+", session):
            return {}
        home = env.get("COPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".copilot")
        path = os.path.join(home, "session-state", session, "events.jsonl")
        if not os.path.isfile(path):
            return {}
        seen, counts, premium = False, {"tokens_in": 0, "tokens_out": 0}, 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("type") != "session.shutdown":
                    continue
                stamp = _epoch(row.get("timestamp"))
                data = row.get("data") if isinstance(row.get("data"), dict) else {}
                metrics = data.get("modelMetrics")
                # No slack before `since`: a shutdown row is written as a call ends, so the previous
                # call of a resumed session wrote its row before this one started.
                if stamp is None or stamp < since or not isinstance(metrics, dict):
                    continue
                for entry in metrics.values():
                    usage = entry.get("usage") if isinstance(entry, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    for source, target in (("inputTokens", "tokens_in"), ("outputTokens", "tokens_out")):
                        value = usage.get(source)
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                            seen = True
                            counts[target] += value
                value = data.get("totalPremiumRequests")
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    premium += value
        if not seen:
            return {}
        return dict(counts, premium_requests=premium)

    def interpret(self, res):
        parser = CopilotEvents()
        parser.feed(res.stdout_tail)
        return parser.result(res)


REGISTRY.update(claude=ClaudeAgent, codex=CodexAgent, copilot=CopilotAgent)


def review_call(agent, prompt, *, invocation_dir, evidence=None, evidence_cap_bytes=None, **kwargs):
    """One read-only review invocation. Panel scheduling and ledger verdicts belong to `panels`.
    Provided-context mode is explicit and includes the complete prebuilt evidence manifest."""
    from . import prompts
    mode = agent.profile.get('review_mode', 'repository')
    if mode == 'provided_context':
        if evidence is None:
            raise ValueError('provided_context review requires complete evidence')
        text, manifest = evidence
        if manifest.get('mode') != 'text-only':
            raise ValueError('provided_context review requires a text-only evidence manifest')
        prompt = prompt + '\n\n' + text
        if evidence_cap_bytes is None or len(prompt.encode()) > evidence_cap_bytes:
            raise prompts.EvidenceTooLarge('complete text-only prompt exceeds its context budget; nothing was sent')
        record.write_durable(os.path.join(invocation_dir, 'evidence.json'), record.dump_json(manifest))
    record.write_durable(os.path.join(invocation_dir, 'review-mode.json'), record.dump_json(
        {'mode': 'text-only' if mode == 'provided_context' else 'repository'}))
    result = agent.run(prompt, invocation_dir=invocation_dir, schema=validate.REVIEW,
                       session_id=None, read_only=True, **kwargs)
    if result.status == OK:
        errors = validate.check_review(result.structured)
        if errors:
            result.status, result.error = PROTOCOL_ERROR, '; '.join(errors)
    return result
