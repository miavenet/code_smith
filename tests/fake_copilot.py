"""A model-free stand-in for `copilot --output-format json` in prompt mode, for adapter and doctor
tests. Its event shapes follow the CLI's shipped session-event schema (1.0.75); they are
constructed, not captured from a live call (tests/recorded/README.md).

Reads the prompt on standard input, answers doctor's probes like probe_agent.py, prints session
events and the closing `result` record, and writes a `session.shutdown` row with token counts to
`$COPILOT_HOME/session-state/<session>/events.jsonl` (only when COPILOT_HOME is set). Without --allow-all-tools it acts as the
read-only profile: it neither writes nor executes. FAKE_COPILOT_LOG, when set, receives its argv,
the prompt and whether COPILOT_ALLOW_ALL reached it.
"""
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_agent  # noqa: E402


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def event(kind, data):
    return json.dumps({"id": str(uuid.uuid4()), "timestamp": stamp(), "parentId": None,
                       "type": kind, "data": data})


def main():
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("GitHub Copilot CLI 1.0.75 (fake)")
        return
    prompt = sys.stdin.read()
    writer = "--allow-all-tools" in argv
    session = next((a.split("=", 1)[1] for a in argv if a.startswith("--resume=")), None) or \
        (argv[argv.index("--session-id") + 1] if "--session-id" in argv else str(uuid.uuid4()))
    if os.environ.get("FAKE_COPILOT_LOG"):
        Path(os.environ["FAKE_COPILOT_LOG"]).write_text(json.dumps({
            "argv": argv, "prompt": prompt, "allow_all_env": "COPILOT_ALLOW_ALL" in os.environ}))
    try:
        probe = json.loads(prompt).get("code_smith_probe")
    except (ValueError, AttributeError):
        probe = None
    if probe is not None and not writer and probe in ("execute", "write"):
        answer = json.dumps({"value": "denied"})
    elif probe is not None:
        if not writer:
            sys.argv.append("--read-only")          # probe_agent's boundary switch
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            probe_agent.handle_probe(prompt)
        answer = out.getvalue().strip()
    else:
        answer = os.environ.get("FAKE_COPILOT_ANSWER", json.dumps(
            {"outcome": "done", "summary": "Made it.", "blocked_reason": "", "responses": []}))
    print(event("session.start", {"sessionId": session, "version": 1, "producer": "copilot-agent",
                                  "copilotVersion": "1.0.75", "startTime": stamp()}))
    print(event("assistant.message", {"messageId": "m1", "content": answer, "outputTokens": 9}))
    print(json.dumps({"type": "result", "timestamp": stamp(), "sessionId": session, "exitCode": 0,
                      "usage": {"premiumRequests": 1, "totalApiDurationMs": 10, "sessionDurationMs": 20,
                                "codeChanges": {"linesAdded": 0, "linesRemoved": 0, "filesModified": []}}}))
    if not os.environ.get("COPILOT_HOME"):
        return                                      # never write into the owner's real ~/.copilot
    record = Path(os.environ["COPILOT_HOME"]) / "session-state" / session / "events.jsonl"
    record.parent.mkdir(parents=True, exist_ok=True)
    with record.open("a") as fh:
        fh.write(event("session.shutdown", {
            "shutdownType": "routine", "totalPremiumRequests": 1, "totalApiDurationMs": 10,
            "sessionStartTime": 0, "codeChanges": {"linesAdded": 0, "linesRemoved": 0, "filesModified": []},
            "modelMetrics": {"gpt-5.4": {"requests": {"count": 1, "cost": 1},
                                         "usage": {"inputTokens": 100, "outputTokens": 9}}}}) + "\n")


if __name__ == "__main__":
    main()
