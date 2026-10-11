#!/usr/bin/env python3
"""Codex status and hook JSONL logger.

The TUI status_line itself is configured with supported built-in fields. This
utility provides the richer machine-readable status snapshot and receives hook
events through stdin when installed from hooks.json.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("CODEX_STATUS_ROOT", os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")))
LOG = pathlib.Path(os.environ.get("CODEX_HOOK_LOG", ROOT / "logs" / "hooks.jsonl"))
STATUS = pathlib.Path(os.environ.get("CODEX_STATUS_FILE", ROOT / "status.json"))


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def git_branch(cwd: str) -> str | None:
    try:
        return subprocess.check_output(["git", "-C", cwd, "branch", "--show-current"], text=True, timeout=1).strip() or None
    except Exception:
        return None


def append_jsonl(record: dict) -> None:
    # Reuse the project's tested redaction rules; never write unsanitized hook payloads.
    source = pathlib.Path(__file__).resolve().parents[1] / ".claude/hooks/log-hook.py"
    spec = importlib.util.spec_from_file_location("hook_redaction", source)
    logger = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(logger)
    record["code_smith"] = {k.removeprefix("CODE_SMITH_").lower(): os.environ[k]
                             for k in ("CODE_SMITH_RUN", "CODE_SMITH_TASK", "CODE_SMITH_INVOCATION",
                                       "CODE_SMITH_AGENT_KIND") if k in os.environ}
    os.umask(0o077)
    LOG.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (LOG.parent / ".codex-hook.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        maximum = int(os.environ.get("HOOK_LOG_MAX_BYTES", str(20 * 1024 * 1024)))
        if maximum and LOG.exists() and LOG.stat().st_size > maximum:
            LOG.replace(str(LOG) + ".1")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(logger.scrub(record), sort_keys=True, ensure_ascii=False, default=str) + "\n")


def hook() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        append_jsonl({"timestamp": now(), "hook_event_name": "invalid-json", "error": str(exc), "raw": raw[:4000]})
        return 0
    if not isinstance(payload, dict):
        payload = {"value": payload}
    append_jsonl({
        "timestamp": now(),
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "hook_event_name": payload.get("hook_event_name", "unknown"),
        "model": payload.get("model"),
        "cwd": payload.get("cwd"),
        "permission_mode": payload.get("permission_mode"),
        "tool_name": payload.get("tool_name"),
        "tool_use_id": payload.get("tool_use_id"),
        "payload": payload,
    })
    return 0


def utilization(window: str, env_name: str) -> str | float | None:
    override = os.environ.get(env_name)
    if override is not None:
        return override
    try:
        data = json.loads((ROOT / "sessions" / "latest.json").read_text())
        value = data["codex"][window]["utilization"]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def status() -> int:
    cwd = os.getcwd()
    record = {
        "timestamp": now(),
        "branch": git_branch(cwd),
        "cwd": cwd,
        "model": os.environ.get("CODEX_MODEL"),
        "session_id": os.environ.get("CODEX_SESSION_ID"),
        "tokens": {
            "input": os.environ.get("CODEX_INPUT_TOKENS"),
            "cached_input": os.environ.get("CODEX_CACHED_INPUT_TOKENS"),
            "output": os.environ.get("CODEX_OUTPUT_TOKENS"),
            "reasoning": os.environ.get("CODEX_REASONING_TOKENS"),
            "total": os.environ.get("CODEX_TOTAL_TOKENS"),
        },
        "limits": {
            "five_hour_percent": utilization("five_hour", "CODEX_5H_PERCENT"),
            "weekly_percent": utilization("weekly", "CODEX_WEEKLY_PERCENT"),
            "five_hour_reset": os.environ.get("CODEX_5H_RESET"),
            "weekly_reset": os.environ.get("CODEX_WEEKLY_RESET"),
        },
    }
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    b = record["branch"] or "no-branch"
    t = record["tokens"]
    l = record["limits"]
    print(f"{b} | tok in={t['input'] or '?'} cache={t['cached_input'] or '?'} out={t['output'] or '?'} reason={t['reasoning'] or '?'} total={t['total'] or '?'} | 5h={l['five_hour_percent'] if l['five_hour_percent'] is not None else '?'}% reset={l['five_hour_reset'] or '?'} | wk={l['weekly_percent'] if l['weekly_percent'] is not None else '?'}% reset={l['weekly_reset'] or '?'}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--hook":
        try:
            hook()
        except Exception:
            pass  # Passive telemetry must never block or rewrite the agent operation.
    else:
        raise SystemExit(status())
