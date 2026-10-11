"""Run gate and check commands: a shell, its own process group, a time limit, the output streamed
to a log with a bounded tail kept for feedback. Returns facts: pass or fail and the output."""

import os
import re

from . import proc

FEEDBACK_TAIL = 6000
# Agents authenticate through these; gates and checks must not inherit them (05, Profiles).
AUTH_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_", "COPILOT_", "AZURE_OPENAI_", "GEMINI_")
AUTH_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "CODE_SMITH_RUN_DIR")


def command_env(base, run_id="", task_id=""):
    # CODE_SMITH_RUNS_DIR names THIS runner's record. A command that itself runs a runner (this
    # project's own test suite does) must not inherit it and write into that record.
    env = {k: v for k, v in base.items()
           if not k.startswith(AUTH_PREFIXES) and k not in AUTH_NAMES
           and k != "CODE_SMITH_RUNS_DIR"}
    if run_id:
        env.update(CODE_SMITH_RUN=run_id, CODE_SMITH_TASK=task_id)
    return env


def run_commands(commands, *, cwd, log_path, timeout_s, env, on_start=None):
    """Run each command in turn and stop at the first that does not pass. Returns one dict per
    command that ran: command, result (pass | fail | timeout | error), exit, seconds, tail.
    A command cancelled before release (`proc.Cancelled`) leaves with the ones that ran on the
    exception, and its line in the log says it did not run (PROC-26)."""
    results = []
    for command in commands:
        with open(log_path, "ab") as fh:
            fh.write(proc.redact(f"$ {command}\n".encode("utf-8")))
        try:
            res = proc.run_process(["/bin/sh", "-c", command], cwd=cwd, env=env,
                                   stdout_path=log_path, timeout_s=timeout_s, on_start=on_start)
        except proc.Cancelled as exc:
            exc.runs = list(results)
            with open(log_path, "ab") as fh:
                fh.write(b"[cancelled before release: not run]\n")
            raise
        if res.status == "not-started":
            result, tail = "error", res.error
        else:
            tail = res.stdout_tail.decode("utf-8", errors="replace")[-FEEDBACK_TAIL:]
            if res.status == "timed-out":
                result = "timeout"
            elif res.returncode in (126, 127):
                result = "error"
            else:
                result = "pass" if res.returncode == 0 else "fail"
        with open(log_path, "ab") as fh:
            fh.write(f"[{result}, exit {res.returncode}, {res.seconds:.1f}s]\n".encode("utf-8"))
        results.append({"command": command, "result": result, "exit": res.returncode,
                        "seconds": round(res.seconds, 3), "tail": tail,
                        **({"cleanup": res.cleanup} if res.cleanup else {})})
        if result != "pass" or res.cleanup:
            break
    return results


def passed(results, commands):
    return len(results) == len(commands) and all(
        r["result"] == "pass" and (r.get("cleanup") or {}).get("status") != "open" for r in results)


def expected_failure(ran, fail_pattern):
    """Why one run of an `expect = "fail"` gate does not pass, or '' when it does: it must exit
    non-zero, not 126 or 127 and not by timeout (`run_commands` reports those as error and
    timeout), with output that matches `fail_pattern`. A missing binary or a hang is not the bug.
    The pattern is searched in the tail kept for feedback (FEEDBACK_TAIL characters), as everywhere
    a `fail_pattern` is checked (docs/03, `gate`); the full output is in the log.
    A process that died of a signal did not fail; it was stopped, whatever it printed first: a
    negative exit (the shell itself was killed) or 129..192 (the shell reports a killed child as
    128 + the signal) is not the defect reproducing."""
    if ran["result"] == "pass":
        return "passed (expected a failure)"
    if ran["result"] == "timeout":
        return "could not run (it timed out)"
    if ran["result"] != "fail":
        return f"could not run (exit {ran['exit']})"
    signal_number = killed_by(ran["exit"])
    if signal_number:
        return f"could not run (killed by signal {signal_number})"
    if not re.search(fail_pattern, ran["tail"]):
        return f"failed without matching /{fail_pattern}/"
    return ""


def killed_by(exit_code):
    """The signal that ended a gate's process, or 0 when it exited by itself."""
    if exit_code is None:
        return 0
    if exit_code < 0:
        return -exit_code
    if 128 < exit_code <= 128 + 64:
        return exit_code - 128
    return 0
