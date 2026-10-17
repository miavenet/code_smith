#!/usr/bin/env python3
"""A scripted agent, driven by a JSON file (07, The scripted agent's contract).

Started with the prompt on standard input and FAKE_AGENT_SCRIPT naming a JSON list of steps. A
counter file beside the script says which step this call performs. The prompt of every call is
kept beside the script too (`<script>.prompts/<n>.md`), so a test can assert what the runner sent.
It never decides anything: all behaviour comes from the script.

Exit 97: called more often than the script has steps. Exit 98: the prompt does not match `match`.
"""

import json
import os
import re
import subprocess
import sys
import time


def main():
    prompt = sys.stdin.read()
    from probe_agent import handle_probe
    if handle_probe(prompt):
        return 0
    script = os.environ["FAKE_AGENT_SCRIPT"]
    with open(script, encoding="utf-8") as fh:
        steps = json.load(fh)
    if isinstance(steps, dict):
        task = os.environ["CODE_SMITH_TASK"]
        steps = steps.get(task, [])
        script = script + "." + task
    counter = script + ".counter"
    try:
        with open(counter, encoding="utf-8") as fh:
            n = int(fh.read().strip() or 0)
    except FileNotFoundError:
        n = 0
    with open(counter, "w", encoding="utf-8") as fh:
        fh.write(str(n + 1))
    os.makedirs(script + ".prompts", exist_ok=True)
    with open(os.path.join(script + ".prompts", f"{n + 1}.md"), "w", encoding="utf-8") as fh:
        fh.write(prompt)
    if n >= len(steps):
        print(f"fake agent: call {n + 1}, but the script has {len(steps)} steps", file=sys.stderr)
        return 97
    step = steps[n]
    if "match" in step and not re.search(step["match"], prompt):
        print(f"fake agent: step {n + 1} expects a prompt matching {step['match']!r}",
              file=sys.stderr)
        return 98

    for path, content in step.get("write", {}).items():
        mode = None
        if isinstance(content, dict):
            content, mode = content.get("text", ""), content.get("mode")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.islink(path):
            os.unlink(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        if mode:
            os.chmod(path, int(mode, 8))
    for link, target in step.get("symlink", {}).items():
        os.makedirs(os.path.dirname(link) or ".", exist_ok=True)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(target, link)
    for path in step.get("remove", []):
        if os.path.lexists(path):
            os.unlink(path)
    for command in step.get("run", []):
        subprocess.run(command, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if step.get("sleep_s"):
        time.sleep(step["sleep_s"])
    if step.get("hang"):
        while True:
            time.sleep(3600)
    if step.get("stdout_noise"):
        print(step["stdout_noise"])
    if "raw_answer" in step:
        print(step["raw_answer"])
    elif "answer" in step:
        print(json.dumps(step["answer"]))
    sys.stdout.flush()
    return int(step.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
