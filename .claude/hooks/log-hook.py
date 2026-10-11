#!/usr/bin/env python3
"""Log every Claude Code hook invocation, for experimenting with hooks.

Registered for every hook event in .claude/settings.json. Each call
appends one JSON record to .claude/hook-logs/hooks.jsonl and one readable
line to .claude/hook-logs/hooks.log (so `tail -f hooks.log` works live).

Per call: seq, timestamps (ISO + epoch ns, taken at process start), event,
session/agent/tool ids, the stdin JSON payload, payload size, pid/ppid,
derived timings, a one-line result summary, and the logger's own overhead.

Derived timings (tracked per session_id):
  tool_duration_ms      Claude Code's own duration_ms from PostToolUse
  pre_to_post_ms        PreToolUse -> PostToolUse as seen by this logger
                        (includes permission prompts and hook latency)
  turn_duration_s       UserPromptSubmit -> Stop
  since_prev_hook_ms, since_session_start_s (not reset by the SessionStart
                        that follows a compaction or resume)
  compactions           completed compactions so far in the session

Session scorecard (shadow mode): on PreCompact and SessionEnd the record gets a "scorecard" block computed
from this session's earlier records, also appended to
hook-logs/sessions/<session_id>/scorecards.jsonl. It only observes: cheap
no-progress signals and which starter thresholds they cross. Nothing is shown
to the user or the model, and compaction is never blocked.

Status line data: hooks don't receive context/cost/rate-limit info, but the
status line does. When a status-line command saves its latest input to
hook-logs/sessions/<session_id>/statusline.json, each record gets a compact
"statusline" block from it (context %, tokens, cost, cache, rate limits) with
age_ms (how stale the snapshot was: the status line refreshes on its own
schedule, not per hook) and cost/token deltas since the previous record.

Cost controls:
  - CLAUDE_* env vars are logged only when they change within a session.
  - Strings longer than HOOK_LOG_MAX_STR (default 16000, 0 = unlimited) are
    truncated, keeping the original length.
  - Logs rotate to <name>.1 at HOOK_LOG_MAX_BYTES (default 20 MB).
  - HOOK_LOG_SKIP="MessageDisplay,FileChanged" drops noisy events.
  - Run as `python3 -S -E` from an async exec-form hook: no shell, no site
    import, and Claude Code never waits for the logger.

Safety: secrets are redacted (secret-looking keys, env assignments, and
well-known token formats); logs are created 0600 in a 0700 directory. The
logger is passive: nothing on stdout, always exit 0. Its own failures go to
errors.log. Because hooks run async, records can land slightly out of order;
sort by epoch_ns when order matters (--tail does).

Usage outside hooks:
  log-hook.py --summary      calls per event, tool timings, log size
  log-hook.py --tail [N]     last N readable lines (default 20)
  log-hook.py --show SEQ     full pretty-printed JSON record for call #SEQ
  log-hook.py --scorecard [SESSION_ID]   scorecard now (default: latest session)
  log-hook.py --reset        delete logs and state
"""

import os
import sys
import time

NOW_NS = time.time_ns()
T0 = time.perf_counter_ns()

import fcntl  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(HERE))
LOG_DIR = os.environ.get("HOOK_LOG_DIR") or os.path.join(PROJECT_DIR, ".claude", "hook-logs")
JSONL = os.path.join(LOG_DIR, "hooks.jsonl")
TEXT = os.path.join(LOG_DIR, "hooks.log")
STATE = os.path.join(LOG_DIR, "state.json")
ERRORS = os.path.join(LOG_DIR, "errors.log")
LOCK = os.path.join(LOG_DIR, ".lock")

MAX_STR = int(os.environ.get("HOOK_LOG_MAX_STR", "16000"))
MAX_BYTES = int(os.environ.get("HOOK_LOG_MAX_BYTES", str(20 * 1024 * 1024)))
SKIP = {e.strip() for e in os.environ.get("HOOK_LOG_SKIP", "").split(",") if e.strip()}
STALE_NS = 24 * 3600 * 10**9

SECRET_KEY = re.compile(r"(token|secret|passw|api[_-]?key|credential|authorization|private[_-]?key)", re.I)
SECRET_VALUE = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}"                 # OpenAI / Anthropic / OpenRouter style
    r"|gh[pousr]_[A-Za-z0-9]{20,}"           # GitHub
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"         # Slack
    r"|AKIA[0-9A-Z]{16}"                     # AWS access key id
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"  # JWT
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/-]{16,}"
)
# NAME=value where NAME looks secret, e.g. a .env line echoed in tool output.
SECRET_ASSIGN = re.compile(
    r"(?im)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL)[A-Z0-9_]*\s*[=:]\s*)(['\"]?)[^\s'\"]{6,}\2")
REDACTED = "<redacted>"


def scrub(value, key=""):
    """Redact secrets and cap long strings, recursively."""
    if isinstance(value, str):
        if key and SECRET_KEY.search(key) and value:
            return REDACTED
        value = SECRET_ASSIGN.sub(lambda m: m.group(1) + REDACTED, SECRET_VALUE.sub(REDACTED, value))
        if MAX_STR and len(value) > MAX_STR:
            return value[:MAX_STR] + f"... [truncated, {len(value)} chars total]"
        return value
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    return value


def clip(value, n=120):
    return " ".join(str(value).split())[:n]


def summarize(event, p):
    """A short human-readable description of what this call carries."""
    tool = p.get("tool_name")
    ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
    if event in ("PreToolUse", "PermissionRequest"):
        target = (ti.get("command") or ti.get("file_path") or ti.get("pattern") or ti.get("url")
                  or ti.get("description") or ti.get("skill") or "")
        verb = "about to run" if event == "PreToolUse" else "permission requested for"
        return f"{verb} {tool}: {clip(target)}"
    if event == "PostToolUse":
        resp = p.get("tool_response")
        if isinstance(resp, dict):
            flag = " (interrupted)" if resp.get("interrupted") else ""
            return f"{tool} ok{flag}; response keys=[{','.join(sorted(resp)[:8])}]"
        return f"{tool} ok; response type={type(resp).__name__}"
    if event == "PostToolUseFailure":
        return f"{tool} FAILED: {clip(p.get('error') or p.get('tool_response'), 160)}"
    if event == "PermissionDenied":
        return f"permission denied for {tool}: {clip(p.get('reason', ''))}"
    if event == "PostToolBatch":
        calls = p.get("tool_calls") or []
        names = [c.get("tool_name", "?") for c in calls if isinstance(c, dict)]
        return f"batch of {len(calls)}: {','.join(names)}"
    if event in ("UserPromptSubmit", "UserPromptExpansion"):
        return f"prompt: {clip(p.get('prompt', ''))!r}"
    if event == "MessageDisplay":
        return f"index={p.get('index')} final={p.get('final')} delta_chars={len(str(p.get('delta') or ''))}"
    if event in ("Stop", "SubagentStop", "SubagentStart", "StopFailure"):
        agent = f"agent={p.get('agent_type')} " if p.get("agent_type") else ""
        last = clip(p.get("last_assistant_message") or "", 60)
        return f"{agent}stop_hook_active={p.get('stop_hook_active')}" + (f" last={last!r}" if last else "")
    if event == "Notification":
        return f"{p.get('notification_type') or 'notification'}: {clip(p.get('message', ''))}"
    # Generic fallback: whatever scalar fields distinguish this event.
    common = {"cwd", "hook_event_name", "session_id", "transcript_path", "scratchpad_dir",
              "prompt_id", "permission_mode", "effort"}
    parts = [f"{k}={clip(v, 40)}" for k, v in p.items()
             if k not in common and isinstance(v, (str, int, float, bool)) and v != ""]
    return " ".join(parts[:5])


def dig(d, *path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def statusline_info(sid, s):
    """Compact view of the status line's latest snapshot for this session."""
    try:
        with open(os.path.join(LOG_DIR, "sessions", sid, "statusline.json")) as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    sl = snap.get("statusline") or {}
    usage = dig(sl, "context_window", "current_usage") or {}
    ctx_tokens = sum(v for k, v in usage.items() if k != "output_tokens" and isinstance(v, (int, float)))
    info = {
        "age_ms": NOW_NS // 10**6 - snap.get("captured_ms", 0),
        "model": dig(sl, "model", "id"),
        "effort": dig(sl, "effort", "level"),
        "ctx_used_pct": dig(sl, "context_window", "used_percentage"),
        "ctx_tokens": ctx_tokens or None,
        "ctx_window": dig(sl, "context_window", "context_window_size"),
        # Last API request: uncached input / written to cache / read from cache / output.
        "tok_in": usage.get("input_tokens"),
        "tok_cache_write": usage.get("cache_creation_input_tokens"),
        "tok_cache_read": usage.get("cache_read_input_tokens"),
        "tok_out": usage.get("output_tokens"),
        "cost_usd": dig(sl, "cost", "total_cost_usd"),
        "api_duration_ms": dig(sl, "cost", "total_api_duration_ms"),
        "lines_added": dig(sl, "cost", "total_lines_added"),
        "lines_removed": dig(sl, "cost", "total_lines_removed"),
        "cache_warm": dig(sl, "prompt_cache", "warm"),
        "cache_hit_ratio": dig(sl, "prompt_cache", "hit_ratio"),
        "cache_misses": dig(sl, "prompt_cache", "misses"),
        "rate_5h_pct": dig(sl, "rate_limits", "five_hour", "used_percentage"),
        "rate_7d_pct": dig(sl, "rate_limits", "seven_day", "used_percentage"),
        "rate_spend_pct": dig(sl, "rate_limits", "spend_limit", "used_percentage"),
    }
    info = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in info.items() if v is not None}
    # Deltas since the previous record of this session: what the last step cost.
    prev = s.get("sl_prev") or {}
    if "cost_usd" in info and "cost_usd" in prev:
        info["cost_delta_usd"] = round(info["cost_usd"] - prev["cost_usd"], 4)
    if "ctx_tokens" in info and "ctx_tokens" in prev:
        info["ctx_delta_tokens"] = info["ctx_tokens"] - prev["ctx_tokens"]
    s["sl_prev"] = {k: info[k] for k in ("cost_usd", "ctx_tokens") if k in info}
    return info


# Starter thresholds, taken from shipped stuck detectors (OpenHands, Gemini CLI,
# Cline) and the "second compaction means handoff" rule. Uncalibrated: shadow mode
# exists to find out whether they separate good sessions from bad ones.
SCORE_LIMITS = {"identical_call_streak": 4, "same_failure_streak": 3, "compactions": 2}
SCORE_EVENTS = ("PreCompact", "SessionEnd")


def scorecard(sid):
    """Cheap no-progress signals for one session, from its main-agent records."""
    calls, fails, edits, prompts, compactions = [], 0, {}, 0, 0
    streak = best = fail_streak = best_fail = 0
    last_call = last_fail = None
    pending = {}
    cost = cost_at_compact = None
    needle = '"session_id":"%s"' % sid
    try:
        f = open(JSONL)
    except OSError:
        return None
    with f:
        for line in f:
            if needle not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ev, pay = r.get("event"), r.get("payload") or {}
            if "cost_usd" in (r.get("statusline") or {}):
                cost = r["statusline"]["cost_usd"]
            if ev == "PostCompact":
                compactions, cost_at_compact = compactions + 1, cost
            if r.get("agent_id"):
                continue  # subagents repeat work by design
            if ev == "UserPromptSubmit":
                prompts += 1
            elif ev == "PreToolUse":
                key = json.dumps([pay.get("tool_name"), pay.get("tool_input")], sort_keys=True, default=str)
                pending[r.get("tool_use_id")] = key
                calls.append(key)
                streak = streak + 1 if key == last_call else 1
                best, last_call = max(best, streak), key
                path = (pay.get("tool_input") or {}).get("file_path")
                if pay.get("tool_name") in ("Edit", "Write", "NotebookEdit") and path:
                    edits[path] = edits.get(path, 0) + 1
            elif ev == "PostToolUseFailure":
                fails += 1
                key = pending.get(r.get("tool_use_id"))
                fail_streak = fail_streak + 1 if key is not None and key == last_fail else 1
                best_fail, last_fail = max(best_fail, fail_streak), key
            elif ev == "PostToolUse":
                last_fail, fail_streak = None, 0
    n = len(calls)
    # A-B-A-B with nothing else in between, anywhere in the session.
    alternation = any(calls[i] == calls[i + 2] == calls[i + 4] and calls[i + 1] == calls[i + 3] == calls[i + 5]
                      and calls[i] != calls[i + 1] for i in range(n - 5))
    card = {
        "tool_calls": n, "tool_failures": fails, "failure_rate": round(fails / n, 3) if n else 0.0,
        "identical_call_streak": best, "same_failure_streak": best_fail, "alternation_6": alternation,
        "user_prompts": prompts, "compactions": compactions,
        "max_edits_one_file": max(edits.values(), default=0), "files_edited": len(edits),
    }
    if cost is not None:
        card["cost_usd"] = cost
        if cost_at_compact is not None:
            card["cost_since_compaction_usd"] = round(cost - cost_at_compact, 4)
    flags = [k for k, limit in SCORE_LIMITS.items() if card[k] >= limit] + (["alternation_6"] if alternation else [])
    card["flags"] = flags
    card["level"] = "green" if not flags else "amber" if len(flags) == 1 else "red"
    return card


def load_state():
    try:
        with open(STATE) as f:
            state = json.load(f)
        if isinstance(state.get("sessions"), dict):
            return state
    except (OSError, ValueError):
        pass
    return {"seq": 0, "counts": {}, "sessions": {}}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)  # atomic: a crash never leaves half a state file


def append(path, text):
    try:
        if MAX_BYTES and os.path.getsize(path) + len(text) > MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def log_call():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
        parse_error = None
        if not isinstance(payload, dict):
            payload = {"_value": payload}
    except ValueError as e:
        payload, parse_error = {"_raw": raw}, str(e)

    event = payload.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "unknown")
    if event in SKIP:
        return
    sid = payload.get("session_id") or "no-session"
    env = {k: (REDACTED if SECRET_KEY.search(k) else v)
           for k, v in sorted(os.environ.items()) if k.startswith("CLAUDE")}
    # Everything that doesn't need the lock happens before taking it.
    clean = scrub(payload)
    result = scrub(summarize(event, payload))
    card = scorecard(sid) if event in SCORE_EVENTS and not payload.get("agent_id") else None

    os.umask(0o077)  # state and lock files are private too
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    # One lock serializes seq, state and both appends: async hooks run concurrently.
    with open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state()
        state["seq"] += 1
        state["counts"][event] = state["counts"].get(event, 0) + 1
        sessions = state["sessions"]
        s = sessions.setdefault(sid, {"pending": {}})

        derived = {}
        if s.get("last_ns"):
            derived["since_prev_hook_ms"] = round((NOW_NS - s["last_ns"]) / 1e6, 3)
        # SessionStart also fires mid-session (source "compact" or "resume") with the
        # same session_id; those must not restart the clock.
        continuing = payload.get("source") in ("compact", "resume")
        if "start_ns" not in s or (event == "SessionStart" and not continuing):
            s["start_ns"] = NOW_NS
            s["start_is_real"] = event == "SessionStart" and not continuing
        key = "since_session_start_s" if s.get("start_is_real") else "since_first_seen_s"
        derived[key] = round((NOW_NS - s["start_ns"]) / 1e9, 3)
        if event == "PostCompact":
            s["compactions"] = s.get("compactions", 0) + 1
        if s.get("compactions"):
            derived["compactions"] = s["compactions"]

        tool_use_id = payload.get("tool_use_id")
        if event == "PreToolUse" and tool_use_id:
            s["pending"][tool_use_id] = NOW_NS
        elif event in ("PostToolUse", "PostToolUseFailure"):
            if isinstance(payload.get("duration_ms"), (int, float)):
                derived["tool_duration_ms"] = payload["duration_ms"]
            if tool_use_id in s["pending"]:
                derived["pre_to_post_ms"] = round((NOW_NS - s["pending"].pop(tool_use_id)) / 1e6, 3)
        elif event == "PermissionDenied":
            s["pending"].pop(tool_use_id, None)
        # Main-agent turns only: subagents share the session_id but carry agent_id.
        if not payload.get("agent_id"):
            if event == "UserPromptSubmit":
                s["turn_ns"] = NOW_NS
            elif event == "Stop" and s.get("turn_ns"):
                derived["turn_duration_s"] = round((NOW_NS - s.pop("turn_ns")) / 1e9, 3)

        sl = statusline_info(sid, s)
        env_changed = env != s.get("env")
        if env_changed:
            s["env"] = env
        s["last_ns"] = max(NOW_NS, s.get("last_ns", 0))
        for k in [k for k, v in sessions.items() if NOW_NS - v.get("last_ns", NOW_NS) > STALE_NS]:
            del sessions[k]

        record = {
            "seq": state["seq"],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW_NS // 10**9)) + f".{NOW_NS % 10**9 // 1000:06d}Z",
            "epoch_ns": NOW_NS,
            "event": event,
            "event_count": state["counts"][event],
            "session_id": sid,
            "agent_id": payload.get("agent_id"),
            "tool_name": payload.get("tool_name"),
            "tool_use_id": tool_use_id,
            "result": result,
            "derived": derived,
            "payload_bytes": len(raw.encode()),
            "payload": clean,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "code_smith": scrub({k.removeprefix("CODE_SMITH_").lower(): os.environ[k]
                                  for k in ("CODE_SMITH_RUN", "CODE_SMITH_TASK", "CODE_SMITH_INVOCATION",
                                            "CODE_SMITH_AGENT_KIND") if k in os.environ}),
        }
        if sl:
            record["statusline"] = sl
        if card:
            record["scorecard"] = card
        if parse_error:
            record["parse_error"] = parse_error
        if env_changed:
            record["env"] = env
        if len(sys.argv) > 1:
            record["argv"] = sys.argv[1:]
        record = {k: v for k, v in record.items() if v is not None}
        record["logger_overhead_ms"] = round((time.perf_counter_ns() - T0) / 1e6, 3)

        append(JSONL, json.dumps(record, separators=(",", ":"), default=str) + "\n")
        extras = " ".join(f"{k}={v}" for k, v in derived.items())
        if sl:
            extras += f" | ctx={sl.get('ctx_used_pct')}% ${sl.get('cost_usd', 0):.2f}"
            if sl.get("cost_delta_usd"):
                extras += f" (+${sl['cost_delta_usd']:.4f})"
            extras += f" sl_age={sl['age_ms']}ms"
        if card:
            extras += f" | scorecard={card['level']}" + (f" flags={','.join(card['flags'])}" if card["flags"] else "")
            os.makedirs(os.path.join(LOG_DIR, "sessions", sid), mode=0o700, exist_ok=True)
            append(os.path.join(LOG_DIR, "sessions", sid, "scorecards.jsonl"), json.dumps(
                {"ts": record["ts"], "seq": record["seq"], "event": event, "trigger": payload.get("trigger"), **card},
                separators=(",", ":")) + "\n")
        append(TEXT, f"{record['ts']} #{record['seq']:<5} {event:<20} "
                     f"{payload.get('tool_name') or '':<12} {result} | {extras}\n")
        save_state(state)


def records():
    """Stream records without loading the whole log."""
    try:
        with open(JSONL) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def summary():
    events, tools, n = {}, {}, 0
    for r in records():
        n += 1
        e = events.setdefault(r["event"], {"n": 0, "first": r["ts"], "first_ns": r["epoch_ns"], "bytes": 0, "over": 0.0})
        e["n"] += 1
        e["bytes"] += r.get("payload_bytes", 0)
        e["over"] += r.get("logger_overhead_ms", 0)
        d = r.get("derived", {})
        if "tool_duration_ms" in d or "pre_to_post_ms" in d:
            t = tools.setdefault(r.get("tool_name", "?"), {"native": [], "seen": []})
            if "tool_duration_ms" in d:
                t["native"].append(d["tool_duration_ms"])
            if "pre_to_post_ms" in d:
                t["seen"].append(d["pre_to_post_ms"])
    if not n:
        print(f"No log yet at {JSONL}")
        return
    size = os.path.getsize(JSONL)
    print(f"{n} hook calls, {size / 1024:.1f} KB ({size / n:.0f} B/call) in {JSONL}\n")
    print(f"{'event':<22}{'calls':>7}  {'first seen':<30}{'avg payload B':>14}{'avg logger ms':>15}")
    for ev, e in sorted(events.items(), key=lambda kv: kv[1]["first_ns"]):
        print(f"{ev:<22}{e['n']:>7}  {e['first']:<30}{e['bytes'] / e['n']:>14.0f}{e['over'] / e['n']:>15.2f}")
    if tools:
        def avg(xs):
            return f"{sum(xs) / len(xs):.1f}" if xs else "-"
        print(f"\n{'tool':<22}{'calls':>7}{'avg tool ms':>14}{'max tool ms':>14}{'avg pre->post ms':>18}")
        for name, t in sorted(tools.items()):
            calls = max(len(t["native"]), len(t["seen"]))
            mx = f"{max(t['native']):.1f}" if t["native"] else "-"
            print(f"{name:<22}{calls:>7}{avg(t['native']):>14}{mx:>14}{avg(t['seen']):>18}")


def tail(n):
    try:
        with open(TEXT) as f:
            lines = f.readlines()[-max(n * 3, 60):]
    except OSError:
        lines = []
    lines.sort(key=lambda line: line[:27])  # async hooks can land out of order
    sys.stdout.write("".join(lines[-n:]))


def main():
    if len(sys.argv) > 1 and sys.argv[1].startswith("--"):
        cmd = sys.argv[1]
        if cmd == "--summary":
            summary()
        elif cmd == "--tail":
            tail(int(sys.argv[2]) if len(sys.argv) > 2 else 20)
        elif cmd == "--show" and len(sys.argv) > 2:
            want = int(sys.argv[2])
            found = next((r for r in records() if r.get("seq") == want), None)
            print(json.dumps(found, indent=2) if found else f"No call #{want}")
        elif cmd == "--scorecard":
            sid = sys.argv[2] if len(sys.argv) > 2 else next(
                (r.get("session_id") for r in reversed(list(records())) if r.get("session_id")), None)
            print(json.dumps({"session_id": sid, **(scorecard(sid) or {})}, indent=2) if sid else "No log yet")
        elif cmd == "--reset":
            for p in (JSONL, TEXT, STATE, ERRORS, JSONL + ".1", TEXT + ".1"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            print(f"Cleared logs in {LOG_DIR}")
        else:
            print(__doc__)
        return
    try:
        log_call()
    except Exception:
        try:
            import traceback
            os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
            append(ERRORS, f"--- {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n{traceback.format_exc()}\n")
        except Exception:
            pass
    # Never influence Claude Code: no stdout, always success.
    sys.exit(0)


if __name__ == "__main__":
    main()
