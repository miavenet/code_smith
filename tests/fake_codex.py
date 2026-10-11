"""A model-free stand-in for `codex exec --json`, for adapter tests of the rollout. Prints the
JSONL events of one turn and writes the session rollout the real CLI keeps under
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<stamp>-<thread>.jsonl`, from the constructed fixture
`recorded/codex-rollout-token-count.jsonl` (tests/recorded/README.md): rows stamped `@before`
get a time an hour ago (an earlier turn of the session), rows stamped `@now` the time of writing.

FAKE_CODEX_MODE: `ok` (default), `no-rollout` (nothing written), `hang` (rows written, then no
terminal event: the call times out), `capacity` (the four lines a "model at capacity" refusal
prints, exit 1, nothing written: BUD-15), `capacity-after-work` (the same after one item event),
`capacity-then-work` (the error first, then item events and a file `codex-worked.txt` written in
the working directory, exit 1 with no terminal event and no rollout: BUD-18). Refuses to run without CODEX_HOME, so it never writes into
the owner's real ~/.codex.
"""
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "recorded", "codex-rollout-token-count.jsonl")
THREAD = "0199f1a2-7c3d-7e00-8a11-fakec0dex001"


def stamp(delta_s=0):
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta_s)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main():
    if sys.argv[1:] == ["--version"]:
        print("codex-cli 0.160.0 (fake)")
        return
    home = os.environ.get("CODEX_HOME")
    if not home:
        sys.exit("fake_codex: CODEX_HOME is not set")
    sys.stdin.read()
    mode = os.environ.get("FAKE_CODEX_MODE", "ok")
    print(json.dumps({"type": "thread.started", "thread_id": THREAD}))
    print(json.dumps({"type": "turn.started"}), flush=True)
    if mode == "capacity-then-work":
        # A retryable error the CLI rides out, then work, then the process dies mid-turn.
        print(json.dumps({"type": "error", "message": "Selected model is at capacity. Retrying."}))
        print(json.dumps({"type": "item.started", "item": {"id": "i0", "type": "command_execution",
                                                           "command": "touch codex-worked.txt"}}))
        with open("codex-worked.txt", "w", encoding="utf-8") as fh:
            fh.write("worked\n")
        print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "command_execution",
                                                             "command": "touch codex-worked.txt",
                                                             "exit_code": 0}}), flush=True)
        sys.exit(1)
    if mode.startswith("capacity"):
        if mode == "capacity-after-work":
            print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "reasoning",
                                                                 "text": "Reading the brief."}}))
        message = "Selected model is at capacity. Please try a different model."
        print(json.dumps({"type": "error", "message": message}))
        print(json.dumps({"type": "turn.failed", "error": {"message": message}}), flush=True)
        sys.exit(1)
    if mode != "no-rollout":
        day = datetime.datetime.now(datetime.timezone.utc)
        path = os.path.join(home, "sessions", day.strftime("%Y"), day.strftime("%m"), day.strftime("%d"),
                            f"rollout-{day.strftime('%Y-%m-%dT%H-%M-%S')}-{THREAD}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(FIXTURE, encoding="utf-8") as src, open(path, "a", encoding="utf-8") as out:
            for line in src:
                row = json.loads(line)
                row["timestamp"] = stamp(-3600) if row["timestamp"] == "@before" else stamp()
                out.write(json.dumps(row) + "\n")
    if mode == "hang":
        time.sleep(30)
        return
    answer = {"outcome": "done", "summary": "Made it.", "blocked_reason": "", "responses": []}
    print(json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message",
                                                         "text": json.dumps(answer)}}))
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 39000, "cached_input_tokens": 33000, "output_tokens": 1600,
        "reasoning_output_tokens": 800}}))


if __name__ == "__main__":
    main()
