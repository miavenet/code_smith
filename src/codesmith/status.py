"""Read-only rendering of task pages, run pages and directory indexes (04, 05)."""

import datetime
import html
import json
import os
import re
import urllib.parse

from . import budgets, findings, invariants, prompts

HEARTBEAT_S = 15


def read_json(path):
    from .record import read_json as read
    return read(path)


def counted(n, plural):
    """`1 file`, `2 files`: a count with its noun, `plural` given in the plural."""
    return f"{n} {plural[:-1] if n == 1 else plural}"


# -- rendering ----------------------------------------------------------------------------------

RUN_HEADLINE = {"running": "in progress", "done": "done", "failed": "failed",
                "needs_human": "needs a person", "stopped": "stopped"}


def _money(x):
    return f"${x:.2f}"


def _in_flight(state, now, run_path=None):
    """One line per agent call or command that has begun and not finished: its task, attempt and
    step, its age, and, with `run_path`, what the provider's own record shows an agent call has
    used so far: tokens, never dollars, and nothing when no record can be read."""
    lines = []
    for it in state["intents"]:
        if it.get("kind") not in ("agent", "command"):
            continue
        what = "agent call" if it["kind"] == "agent" else f"command `{it.get('command', '')}`" \
            + (" in the acceptance replay" if it.get("replay") else "")
        line = f"- **{it.get('task', '?')}**: {what}"
        st = state["tasks"].get(it.get("task")) or {}
        where = ([f"attempt {st['attempts']}"] if st.get("attempts") else []) + \
            ([f"step {st['step']}"] if st.get("step") else [])
        if where:
            line += " (" + ", ".join(where) + ")"
        if it.get("at"):
            line += f", started {it['at'][11:19]} UTC"
            if now is not None:
                began = _parse_at(it["at"])
                if began is None:
                    raise ValueError(f"intent {it.get('op')} has no readable time: {it['at']!r}")
                secs = max(0, int((now - began).total_seconds()))
                line += f", running for {secs // 60} min {secs % 60:02d} s"
        if run_path and it["kind"] == "agent" and it.get("invocation_dir") and it.get("agent_kind"):
            from . import agents
            usage = agents.partial_usage(it["agent_kind"], os.path.join(run_path, it["invocation_dir"]),
                                         agents._epoch(it.get("at")))
            if usage:
                line += (f", {usage.get('tokens_in', 0)} tokens in and {usage.get('tokens_out', 0)} out "
                         "so far (the provider's record; unpriced)")
        if it.get("invocation_dir"):
            line += f". Log: `{it['invocation_dir']}/`"
        lines.append(line)
    return lines


def _set_aside_record(run_path, t):
    """The task's published `set-aside.json`, or None. `failed.patch` alone does not mean the
    work can be put back: an old run already replanned by an older runner may have `failed.patch`
    with no `set-aside.json` and nothing left to derive it from (C4's failure case), which
    `--apply-patch` cannot act on."""
    path = os.path.join(run_path, "tasks", t["dir"], "set-aside.json")
    return read_json(path) if os.path.exists(path) else None


def _has_set_aside(run_path, t):
    """Is there set-aside work of this task that can still be put back? A record whose `paths`
    is empty is itself "no set-aside work" (the attempt changed nothing), so `--apply-patch`
    has nothing to apply."""
    rec = _set_aside_record(run_path, t)
    return bool(rec and rec["paths"])


PROTOCOL_BLOCK = "the reviewers could not answer in the required form"
MIXED_BLOCK = "one reviewer could not answer in the required form and another did not finish"
REVIEWER_CAUSE = {"protocol-error": "could not answer in the required form",
                  "timed-out": "ran past its time limit",
                  "agent-error": "ended with an error",
                  "interrupted": "was interrupted"}


def _block_kind(t):
    """How a producer's panel failed — "protocol", "mixed", or None. Read defensively: the field
    is absent in every state written before it existed, and it describes a `blocked` task only,
    so a retry or a replan that moves the task on cannot leave a stale label on the page."""
    return t.get("block_kind") if t["status"] == "blocked" else None


def _cause(r):
    """One broken reviewer's cause in its own words, never the panel's classification."""
    return REVIEWER_CAUSE.get(r["status"], r["status"])


def _reviewer_causes(t):
    """The list requirement 3 asks for, rendered whichever way the panel failed, so the accurate
    cause per reviewer is always on the page. The retry budget is named only where it means
    something: a timed-out call is finalised on its first try, without any retry."""
    lines = []
    for r in t.get("block_reviewers") or []:
        tries = r["tries"]
        spent = f" ({tries} tr{'y' if tries == 1 else 'ies'})" if r["status"] == "protocol-error" else ""
        lines.append(f"- {r['reviewer']}: {_cause(r)}{spent}.")
    return lines


def _panel_attention(task_id, t, kind):
    """The "Needs attention" line of a producer its panel could not judge. It says nobody judged
    the work, and sends the owner to the summaries rather than to a parser diagnostic."""
    where = f"tasks/{t['dir']}/STATUS.md"
    if kind == "protocol":
        n = len(t.get("rejected_reviews") or [])
        if n == 0:
            rejected = f"No answer could be read; see {where}."
        elif n == 1:
            rejected = f"1 review answer was rejected and it was not applied; it is summarised in {where}."
        else:
            rejected = (f"{n} review answers were rejected and none was applied; they are "
                        f"summarised in {where}.")
        return f"- **{task_id}** is blocked: {PROTOCOL_BLOCK}. {rejected}"
    per = "; ".join(f"{r['reviewer']}: {_cause(r)}" for r in t.get("block_reviewers") or [])
    return (f"- **{task_id}** is blocked: {MIXED_BLOCK}. Per reviewer: {per}. "
            f"The rejected answers are summarised in {where}.")


RUN_EXIT = {"done": 0, "needs_human": 255, "failed": 2, "stopped": 2}
EVENTS_SHOWN = 10


def _parse_at(stamp):
    """A datetime of an `at` stamp of the record (with or without fractions), or None."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(str(stamp), fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


def recent_events(run_path, n=EVENTS_SHOWN):
    """The last `n` readable events of events.jsonl, oldest first. Only the file's tail is read;
    a torn line (a kill in the middle of an append) is skipped."""
    try:
        with open(os.path.join(run_path, "events.jsonl"), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found = []
    for line in reversed(tail):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and "event" in event:
            found.append(event)
        if len(found) == n:
            break
    return found[::-1]


# What a Recent events line leaves out: the times, shown as "(took …)", and what an outcome keeps
# of its intent for the record's reader (REC-50), too long for one line.
EVENT_LINE_HIDDEN = ("at", "event", "wall_s", "seconds")
OUTCOME_KEPT = ("task", "invocation", "process", "token_reservation", "token_remainder", "ended_at")


def _event_line(event):
    words = [f"{k}={v if isinstance(v, str) else json.dumps(v, sort_keys=True)}"
             for k, v in sorted(event.items()) if k not in EVENT_LINE_HIDDEN
             and not (event.get("event") == "outcome" and k in OUTCOME_KEPT)]
    words = [w if len(w) <= 120 else w[:117] + "..." for w in words]
    line = f"{str(event.get('at', ''))[11:19]} {event['event']}{_took(event)} " + " ".join(words)
    return " ".join(line.split())[:300]


def _took(event):
    """" (took 1 min 12 s)" of an outcome event, or " (1 min 12 s measured, 8 min 19 s by the wall
    clock)" when the two differ by a minute or more (RUN-50); '' for an event with no duration."""
    wall, measured = event.get("wall_s"), event.get("seconds")
    if not isinstance(wall, (int, float)):
        return f" (took {_duration(measured)})" if isinstance(measured, (int, float)) else ""
    if isinstance(measured, (int, float)) and wall - measured >= 60:
        return f" ({_duration(measured)} measured, {_duration(wall)} by the wall clock)"
    return f" (took {_duration(measured if isinstance(measured, (int, float)) else wall)})"


def _duration(secs):
    secs = max(0, int(secs))
    if secs >= 3600:
        return f"{secs // 3600} h {secs % 3600 // 60:02d} min"
    return f"{secs // 60} min {secs % 60:02d} s"


WINDOW_NAMES = {300: "5-hour", 10080: "weekly", 1440: "daily"}


def window_phrases(spend):
    """"Observed Codex weekly account window: 2.0% → 3.0% (shared-account observations, not
    attributable run usage)" per window of `spend.windows` (BUD-21). The account's window is
    shared with everything else that uses the account, and calls that ran side by side each saw
    the others' use, so the run shows what it observed, never a share of its own. Each reset of
    the window starts a new range. A run recorded before the observations were kept shows its
    first and last readings."""
    phrases = []
    for key, w in sorted((spend.get("windows") or {}).items()):
        kind = key.split(":", 1)[0]
        minutes = w.get("window_minutes")
        name = WINDOW_NAMES.get(minutes, f"{minutes}-minute" if minutes else "rate-limit")
        ranges = [(r["low"], r["high"]) for r in budgets.window_ranges(w)]
        seen = "; reset, then ".join(f"{lo:.1f}% → {hi:.1f}%" for lo, hi in ranges)
        phrases.append(f"Observed {kind.capitalize()} {name} account window: {seen} "
                       "(shared-account observations, not attributable run usage)")
    return phrases


def _estimate_phrase(spend):
    if "estimated_usd" not in spend:
        return ""
    counted = spend.get("estimated_counted_usd", 0.0)
    return (f", ≈{_money(spend['estimated_usd'])} estimated from profile rates"
            + (f" ({_money(counted)} of it counted against the budget)" if counted else ""))


def _status_word(state, alive):
    status = state["status"]
    if status == "running":
        return "running" if alive else ("running, but no runner is working on it "
                                        "(interrupted: `runner resume`)")
    word = f"{RUN_HEADLINE.get(status, status)} (exit {RUN_EXIT.get(status, 2)})"
    reason = " ".join(str(state.get("stop_reason") or "").split())
    return word + (f": {reason}" if reason and status in ("failed", "stopped") else "")


def _agent_label(t):
    """`profile / model` of a task as routed (provider_current), else as planned; '' for a task
    that has no agent (a check, a human)."""
    current = t.get("provider_current") or {}
    profile = current.get("profile") or t.get("agent") or ""
    model = current.get("model") or t.get("model") or ""
    if not profile:
        return ""
    return profile + (f" / {model}" if model and model != profile else "")


def spend_by_model(state):
    """Known dollars and call counts per `profile / model`, from the tasks' own records, so the
    spend line says what each model cost beside the total (the total is the authority; this is a
    breakdown of it). A task whose agent reports no price shows its calls as unpriced."""
    by = {}
    for tid in state["order"]:
        t = state["tasks"][tid]
        label = _agent_label(t)
        if not label or not (t.get("attempts") or t.get("cost_usd") or t.get("provider_current")):
            continue
        entry = by.setdefault(label, {"usd": 0.0, "tasks": 0, "unpriced": 0})
        entry["tasks"] += 1
        if t.get("cost_usd"):
            entry["usd"] += t["cost_usd"]
        elif (t.get("provider_current") or {}).get("kind") in ("codex", "copilot", "command"):
            entry["unpriced"] += 1
    return by


def _by_model_phrase(state):
    parts = []
    for label, e in spend_by_model(state).items():
        what = _money(e["usd"]) if e["usd"] else ("unpriced" if e["unpriced"] else _money(0.0))
        parts.append(f"{label} {what} ({e['tasks']} task{'' if e['tasks'] == 1 else 's'})")
    return " By model: " + "; ".join(parts) + "." if parts else ""


def run_status_model(info, state, disposition=None, now=None, run_path=None, alive=False,
                     refresh_s=HEARTBEAT_S):
    """The run's status page as data, rendered once and written twice: STATUS.md
    (`markdown_status`) and STATUS.html (`html_status`). A list of sections, each a heading (or
    None) and its lines: ("text", s), ("item", s), ("code", s) or ("table", header, rows). Inline
    `**bold**` and `` `code` `` are the only markup in the strings.

    `alive`: a runner holds this run's lock and is working. Its open operations are then the
    calls and commands in flight, listed under "In flight", not operations that were interrupted;
    that line is for a run no runner is working on, where an open intent is one left behind.
    `now`: the time of rendering. Without it the page has no clock in it."""
    spend = state["spend"]
    unpriced = spend["unpriced"]
    unsettled = spend.get("unsettled") or {}                # absent in a run saved before it
    events = recent_events(run_path) if run_path else []
    started = _parse_at(info["started"])
    updated = _parse_at(events[-1]["at"]) if events else None
    updated = updated or started
    stamp = updated.strftime("%Y-%m-%d %H:%M:%S UTC") if updated else info["started"]
    age = f" ({max(0, int((now - updated).total_seconds()))}s ago at render)" if now and updated else ""
    sections = [(None, [("text", f"Updated {stamp}{age}. Status: {_status_word(state, alive)}.")])]
    sections.append((f"# {info['workflow']} — run {info['run_id'][:8]} — "
                     f"{RUN_HEADLINE.get(state['status'], state['status'])}", []))
    windows = window_phrases(spend)
    sections.append((None, [
        ("text", f"Started {info['started'].replace('T', ' ').replace('Z', ' UTC')}. "
                 f"{state['seconds'] // 60} min of agent time. Branch {info['branch']}."),
        ("text", f"Spend: {_money(spend['known_usd'])} known of {_money(state['run_budget_usd'])}, "
                 f"{_money(spend['reserved_usd'])} reserved"
         + (f", {_money(unsettled.get('usd', 0.0))} unsettled ({unsettled.get('calls', 0)} "
            f"call{'' if unsettled.get('calls', 0) == 1 else 's'} ended without a price)"
            if unsettled.get("usd") or unsettled.get("calls") else "")
         + (f", plus {unpriced['calls']} unpriced calls ({unpriced['tokens_in']} tokens in, "
            + (f"{budgets.cached_share(unpriced)}% cached, " if budgets.cached_share(unpriced) is not None else "")
            + f"{unpriced['tokens_out']} out; {unpriced['unknown_calls']} with unknown usage"
            + (f"; {unpriced['counted_tokens']} tokens held, not measured, for "
               f"{unpriced['counted_calls']} call{'' if unpriced['counted_calls'] == 1 else 's'} "
               "with unknown usage" + (f": {budgets.held_calls_phrase(state)}"
                                       if unpriced.get("held") else "")
               if unpriced.get("counted_calls") else "")
            + (f"; cap {state['run_budget_tokens']} tokens" if state.get("run_budget_tokens") else "")
            + ")" if unpriced["calls"] or unpriced["unknown_calls"] else "")
         + _estimate_phrase(spend) + "." + "".join(f" {w}." for w in windows) + _by_model_phrase(state))]))
    rows, attention = [], []
    for task_id in state["order"]:
        t = state["tasks"][task_id]
        status = t['status']
        if status == 'accepted' and findings.owner_rulings(t.get('ledger')):
            status = 'accepted with owner rulings'
        if t['reason'] and t['status'] == 'skipped':
            status += f" ({t['reason']})"
        kind = _block_kind(t)
        if kind:
            status = "blocked (protocol)" if kind == "protocol" else "blocked (protocol, in part)"
        if t.get("stale"):
            status += " (stale)"
            attention += [f"**{task_id}** was accepted on an older version of {s['file']} "
                          f"({s['was'][:7]}, now {s['now'][:7]}), changed by '{s['by']}', and only "
                          "review or a person verified it. Its acceptance does not cover the new "
                          "content." for s in t["stale"]]
        rows.append([t['dir'].split('-', 1)[0], task_id, t['type'], _agent_label(t), status,
                     str(t['attempts'] or ''), _money(t['cost_usd']) if t['cost_usd'] else '',
                     (t['commit'] or '')[:7]])
        for finding in t.get("ledger", {}).get("findings", []):
            if finding["severity"] == "blocking" and finding["status"] in ("open", "disputed", "escalated"):
                # The lead says what the reader must do before the record detail (RD-04): a
                # decision for an escalation, nothing for a finding the author is answering.
                where = f" ({finding['location']})" if finding.get("location") else ""
                if finding["status"] == "escalated":
                    attention.append(f"**Your decision is needed on {finding['id']}**: a reviewer and "
                                     f"the author disagree about \"{finding['title']}\"{where}. Read "
                                     f"the finding and the author's reply in tasks/{t['dir']}/findings.json, "
                                     f"then `runner resolve`.")
                else:
                    attention.append(f"**{finding['id']}** [{finding['status']}]: {finding['title']}"
                                     + where + (" (the author's next attempt answers it)"
                                                if t["status"] in ("pending", "running") else ""))
        warning = churn_lines(task_id, review_churn(t.get("ledger")), t["status"])
        if warning:
            attention.append(warning)
        held = _held_for_ruling(t)
        if held is not None and not kind:
            attention.append(_ruling_attention(task_id, t, held, _human_verifiers(run_path, task_id)))
        elif t["status"] == "waiting_human" and not kind:
            attention.append(f"**Waiting for your sign-off on {task_id}**: review found no remaining "
                             f"blocker; the task is not accepted until you approve it"
                             + (f" ({t['reason']})" if t["reason"] else "")
                             + f". See tasks/{t['dir']}/STATUS.md.")
        elif t["status"] in ("waiting_human", "blocked", "failed"):
            attention.append(_panel_attention(task_id, t, kind)[2:] if kind else
                             f"**{task_id}** is {t['status']}"
                             + (f": {t['reason']}" if t["reason"] else "")
                             + f". See tasks/{t['dir']}/STATUS.md.")
    sections.append((None, [("table", ["#", "Task", "Type", "Agent / model", "Status", "Attempts", "Cost", "Commit"], rows)]))
    rulings = [("item", line) for tid in state['order']
               for line in owner_ruling_lines(tid, state['tasks'][tid], task_page=False)]
    if rulings:
        sections.append(("## Owner rulings", rulings))
    standing = state.get("standing_pending") or []
    if standing:
        sections.append(("## Standing rulings from this run", [("item", (
            f"{e['finding']}: {e['decision']}, ruled by {e['by']} ({findings.provenance(e)}): "
            + _standing_phrase(info, state, e))) for e in standing]))
    advised = [("item", line) for line in (accepted_advisory_line(tid, state['tasks'][tid])
                                            for tid in state['order']) if line]
    if advised:
        sections.append(("## Advisories on accepted work", advised))
    sections.append(("## Progress", _progress(info, state, now, updated, started)))
    selections = [(tid, state['tasks'][tid].get('provider_current')) for tid in state['order']]
    if any(selection for _, selection in selections):
        sections.append(('## Providers', [
            ("item", f"**{tid}**: {selection['profile']} / {selection['model'] or 'provider default'} "
                     f"({selection['complexity']}; {selection['reason']}).")
            for tid, selection in selections if selection]))
    flying = _in_flight(state, now, run_path) if state["status"] == "running" else []
    if flying:
        lines = []
        if now is not None:
            how = (f"refreshed about every {refresh_s} s while the runner is alive" if refresh_s
                   else "not refreshed during a call: status_refresh_s is 0")
            lines.append(("text", f"As of {now.strftime('%H:%M:%S')} UTC ({how}; an old time here "
                                  "means no runner is working on this run)."))
        lines += [("item", line[2:]) for line in flying]
        busy = {it.get("task") for it in state["intents"]}
        pending = [tid for tid in state["order"]
                   if state["tasks"][tid]["status"] == "pending" and tid not in busy]
        if pending:
            lines.append(("text", "Then, in workflow order: " + ", ".join(pending[:5])
                          + (f" and {len(pending) - 5} more" if len(pending) > 5 else "") + "."))
        sections.append(("## In flight", lines))
    if state.get("stop_reason"):
        attention.append(f"The run stopped: {state['stop_reason']}")
    stale = state.get("pin_stale")
    if isinstance(stale, dict):
        attention.append(f"The pinned copy of the record is stale since {stale.get('since', '?')}: "
                         f"{stale.get('error', '?')}. `repair-record` keeps the newer state.json; "
                         "see `record-pin-failed` in events.jsonl")
    if state["intents"] and not (alive and state["status"] == "running"):
        # A replay kept for an open cleanup was not interrupted: its checkout waits for the
        # cleanup, which the stop reason names with its remedy (PROC-22, PROC-25).
        waiting = {e.get("task") for e in state.get("cleanup_obligations", [])
                   if (e.get("cleanup") or {}).get("status") == "open"}
        kept = [it for it in state["intents"] if it.get("kind") == "replay" and it.get("task") in waiting]
        for it in kept:
            attention.append(f"The acceptance replay of '{it.get('task', '?')}' keeps its checkout "
                             "while the cleanup of a command run in it is open; `runner resume` "
                             "removes it once that cleanup is closed.")
        if len(state["intents"]) > len(kept):
            attention.append(f"{len(state['intents']) - len(kept)} operation(s) were interrupted; "
                             "`runner resume` reconciles them first.")
    attention += [f"The run state breaks an invariant: {p}. The runner carries on; report it "
                  "with events.jsonl and state.json." for p in invariants.check(state)]
    if attention:
        sections.append(("## Needs attention", [("item", a) for a in attention]))
    if events:
        sections.append(("## Recent events", [("item", _event_line(e)) for e in events]))
    sections.append(("## Next", [("code", line) for line in _next_lines(info, state, disposition, run_path)]))
    return sections


def _standing_phrase(info, state, e):
    """What became of one queued standing ruling. A failed run never writes it itself, so its
    page shows the entry in full and the command that writes it (RUN-77)."""
    where = e.get("file") or "named by the workflow's rulings_file"
    command = f"`runner export-rulings {info['name']}`"
    if e.get("written"):
        return f"written to the rulings file {where}; " + export_next_steps(info["name"], state)
    if state["status"] == "running":
        return f"queued; written to the rulings file when the run is done ({where})"
    live = export_live(state)
    if live:
        # Work is left that can change the tree: the run goes on, never the export (RUN-81).
        return (f"queued; written to the rulings file when the run is done ({where}). Work is "
                f"left ({', '.join(live)}): " + export_ways_on(info["name"], state))
    shown = ", ".join(f"{k} {json.dumps(v, ensure_ascii=False)}"
                      for k, v in findings.exported_entry(e).items()
                      if k in ("task", "finding_title", "location_glob", "decision", "note", "by",
                               "ruled_at"))
    if str(state.get("stop_reason", "")).startswith("the standing rulings could not be written"):
        # The run failed at its own export (RUN-73), and `resume` writes the entry once the
        # cause is fixed. Keyed on the stop, not on "every task accepted" (RUN-81).
        return (f"queued; the run failed writing it: fix the cause, then `runner resume "
                f"{info['name']}` writes it to {where} (or {command}). The entry: {shown}")
    retry = _retryable(state)
    return ("queued, and this run writes it only if it gets to done"
            + (" (by " + ", ".join(f"`runner retry {info['name']} {t}`" for t in retry) + ")"
               if retry else "")
            + f": {command} writes it to {where} now. The entry: {shown}")


def _retryable(state):
    return [tid for tid in state["order"] if state["tasks"][tid]["status"] in ("failed", "blocked")]


def export_live(state):
    """What can still change the work tree of a run (RUN-81): a task that is not terminal (a
    running check restores its base on `resume`, a pending producer starts only from a clean
    tree), a producer's transaction, an operation to reconcile, an open cleanup. `runner
    export-rulings` writes only when this is empty, so nothing in the run undoes the file."""
    from .record import TASK_TERMINAL
    active = state.get("active_producer")
    live = [f"'{tid}' in its transaction" if tid == active
            else f"'{tid}' {state['tasks'][tid]['status']}" for tid in state["order"]
            if tid == active or state["tasks"][tid]["status"] not in TASK_TERMINAL]
    if state.get("intents"):
        live.append("operations to reconcile")
    if any((e.get("cleanup") or {}).get("status") == "open"
           for e in state.get("cleanup_obligations", [])):
        live.append("an open cleanup")
    return live


def _resume_step(name, state):
    """The `resume` a stop needs, in one phrase (`_resume_lines` without its comment)."""
    lines = [line for line in _resume_lines(name, state) if not line.startswith("#")]
    if lines[0].endswith("--stop-orphans"):
        lines = lines[:1]
    return ", then ".join(f"`{line}`" for line in lines)


def export_ways_on(name, state):
    """How a run with work left gets to the end, for the export's refusal and STATUS alike
    (RUN-81): it goes on and writes the rulings at `done`, or a replan removes that work."""
    retry = _retryable(state)
    return (f"finish it: {_resume_step(name, state)} (after the approvals or rulings its STATUS "
            "names) goes on, and the run writes the rulings when it is done"
            + ("; " + ", ".join(f"`runner retry {name} {t}`" for t in retry)
               + " starts a failed or blocked task again" if retry else "")
            + f"; or end it without that work: `runner replan {name} --workflow FILE` with those "
            "tasks removed")


def export_next_steps(name, state):
    """What follows a written export (RUN-81), for the export's message and STATUS alike. Nothing
    in the run can touch the tree, so the file is committed now: `resume` and `replan` take that
    commit, and `retry` of a failed or blocked task adopts it. A stopped run's `resume`, with
    what its stop needs, ends it as it stands."""
    retry = _retryable(state)
    steps = "commit it, so the next run reads it"
    if retry and state["status"] != "done":
        steps += ("; then " + ", ".join(f"`runner retry {name} {t}`" for t in retry)
                  + " starts again from your commit (a blocked task after the rulings its "
                  "STATUS names)")
    if state["status"] == "stopped":
        steps += f"; {_resume_step(name, state)} ends the run as it stands"
    return steps


def _held_for_ruling(t):
    """The one action model of a producer held for a person's ruling (RUN-48), or None when it is
    not held for one. Held: waiting at the escalation step, or (a state saved before the step was
    read here) waiting with blocking findings open. Returns the findings that still need a ruling;
    an upheld one is the author's to answer, not the person's again. [] once the rulings are done."""
    if t["status"] != "waiting_human" or t.get("kind", "produce") != "produce":
        return None
    blockers = [f for f in t.get("ledger", {}).get("findings", [])
                if f["severity"] == "blocking" and f["status"] in ("open", "disputed", "escalated")]
    if t.get("step") != "escalation" and not blockers:
        return None
    return [f["id"] for f in blockers if not f.get("upheld")]


def _human_verifiers(run_path, task_id):
    """The human tasks that verify `task_id`, from the frozen expanded workflow; [] when unknown."""
    try:
        tasks = read_json(os.path.join(run_path, "workflow.expanded.json"))["tasks"]
    except (TypeError, OSError, ValueError, KeyError):
        return []
    return [t["id"] for t in tasks if t.get("kind") == "human" and t.get("verifies") == task_id]


def _ruling_attention(task_id, t, needed, people):
    see = f" See tasks/{t['dir']}/STATUS.md."
    if needed:
        # Held for a ruling, not for an approval: `resolve` each finding, then `resume`
        # carries on by itself (a live run said "sign-off" here and `approve` refused).
        return (f"**{task_id} is held for your ruling** on {', '.join(needed)}: `runner resolve` "
                f"each one, then `runner resume`; no approval follows." + see)
    upheld = [f["id"] for f in t.get("ledger", {}).get("findings", [])
              if f.get("upheld") and f["severity"] == "blocking" and f["status"] == "open"]
    if upheld:
        then = f"sends the work back to the author for {', '.join(upheld)}"
    elif people:
        then = (f"continues acceptance, up to the person at {', '.join(people)}, who approves or "
                "rejects it then")
    else:
        then = "continues acceptance; no approval follows"
    return f"**{task_id}: your rulings are complete**; `runner resume` {then}." + see


def _progress(info, state, now, updated, started):
    tasks = [state["tasks"][t] for t in state["order"]]
    counts = {}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    others = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()) if s != "accepted")
    spend, cap = state["spend"], state.get("run_budget_tokens", 0)
    tokens = budgets.tokens_used(state)
    held = budgets.tokens_held(state)
    if held:
        tokens = f"{budgets.tokens_measured(state)} measured + {held} held for unknown usage = {tokens}"
    money =f"{_money(spend['known_usd'])} known of {_money(state['run_budget_usd'])}"
    if spend.get("unsettled", {}).get("usd"):
        money += f" (+{_money(spend['unsettled']['usd'])} unsettled)"
    lines = [("item", f"Tasks: {counts.get('accepted', 0)} of {len(tasks)} accepted"
                      + (f" ({others})" if others else "") + "."),
             ("item", f"Attempts used: {sum(t['attempts'] for t in tasks)}."),
             ("item", f"Budget used: {money}"
                      + (f"; {tokens} of {cap} tokens" if cap else f"; {tokens} unpriced tokens" if tokens else "")
                      + (f"; ≈{_money(spend['estimated_usd'])} estimated" if "estimated_usd" in spend else "")
                      + ".")]
    end = now if state["status"] == "running" and now else updated
    if started and end:
        lines.append(("item", f"Elapsed: {_duration((end - started).total_seconds())} since start"
                              + ("" if state["status"] == "running" and now else ", to the last event") + "."))
    return lines + _time_lines(state)


def _time_lines(state):
    """Where the time went, each figure measured on its own (RUN-50): agent calls, gates and
    checks, acceptance replays, and the time the run stood stopped waiting for a person. Calls
    that ran side by side each count their own time, so the agent figure is a sum and says how
    much of it overlapped (REC-51; from `agent_spans`, absent in a run recorded before). Absent
    from a run recorded before these were kept. A wall clock that ran ahead of the measured time
    is said with the operation and the two causes it can have, and no choice between them."""
    if not any(k in state for k in ("gate_seconds", "replay_seconds", "stopped_seconds", "clock_gap")):
        return []
    spans = state.get("agent_spans")
    summed = "summed" + (f"; {_duration(overlapped_seconds(spans))} of it overlapped" if spans else "")
    parts = [f"{_duration(state['seconds'])} in agent calls ({summed})",
             f"{_duration(state.get('gate_seconds', 0))} in gates and checks",
             f"{_duration(state.get('replay_seconds', 0))} in acceptance replays"]
    if state.get("stopped_seconds"):
        parts.append(f"{_duration(state['stopped_seconds'])} stopped, waiting for a person")
    lines = [("item", "Time: " + "; ".join(parts) + ".")]
    gap = state.get("clock_gap")
    if gap:
        n = gap["operations"]
        where = [f"{w['op']} ({w.get('task') or w.get('kind', '?')})" for w in gap.get("where", [])]
        if n == 1 and where:
            during = f" during {where[0]}"
        elif where:
            during = (f" over {n} operations, the last {'one' if len(where) == 1 else len(where)}: "
                      + ", ".join(where))
        else:
            during = f" over {n} operation{'' if n == 1 else 's'}"
        lines.append(("item", f"The wall clock ran {_duration(gap['seconds'])} ahead of the "
                              f"monotonic clock{during}: the machine or its VM was suspended, or "
                              "the clock was stepped; the record does not say which."))
    return lines


def overlapped_seconds(spans):
    """How much of the agent calls' summed wall time overlapped another call's (REC-51): the sum
    of their [intent, end] intervals less the length of their union."""
    union = spans.get("folded_s", 0.0) + sum(e - b for b, e in spans.get("open", []))
    return max(0.0, spans.get("summed_s", 0.0) - union)


def _next_lines(info, state, disposition, run_path):
    lines = []
    if state["status"] == "done":
        d = disposition
        if d and d["merged"]:
            where = f"`{d['target']}`"
            if d["pushed"]:
                where += f" and in `{d['upstream']}` (as last fetched)"
            elif d["upstream"]:
                where += f"; `{d['upstream']}` does not have it yet: push `{d['target']}`"
            lines.append(f"Merged: the last accepted commit {d['commit'][:7]} is in {where}.")
            lines.append(f"Nothing is left to do. The branch {info['branch']} can be deleted; "
                         "`runner prune` removes this run's pinned refs.")
        elif d:
            lines.append(f"Not merged: `{d['target']}` does not contain the last accepted "
                         f"commit {d['commit'][:7]}.")
            lines.append("Inspect the branch; merging it is your call.")
        else:
            lines.append("Inspect the branch; merging it is your call.")
        return lines
    for task_id in state["order"]:
        t = state["tasks"][task_id]
        held = _held_for_ruling(t)
        if t["kind"] == "human" and t["status"] == "waiting_human" and not t.get("decision"):
            lines += [f"runner approve {info['name']} {task_id}",
                      f"runner reject {info['name']} {task_id} -m \"why\""]
        elif held is not None and not _block_kind(t):
            lines += [f"runner resolve {info['name']} {fid} --as resolved|advisory|upheld" for fid in held]
        elif t["status"] in ("failed", "blocked") or (
                t["status"] == "pending" and t["kind"] == "produce" and _has_set_aside(run_path, t)):
            if t['status'] == 'blocked':
                for finding in t.get('ledger', {}).get('findings', []):
                    if finding['severity'] == 'blocking' and finding['status'] in ('open', 'disputed', 'escalated'):
                        lines.append(f"runner resolve {info['name']} {finding['id']} --as resolved|advisory|upheld")
            kind = _block_kind(t)
            if kind:
                # Nobody judged this candidate, so continuing from the set-aside work is the
                # right default rather than one of two options: no brackets. The flag is
                # printed only when there is work to put back — an attempt that changed
                # nothing leaves `paths: []`, and `retry --apply-patch` refuses such a task
                # outright, so an unconditional flag would name a command that fails.
                recover = " --apply-patch" if run_path and _has_set_aside(run_path, t) else ""
                lines += [f"# {task_id}: " + (f"{PROTOCOL_BLOCK}." if kind == "protocol"
                          else "one reviewer could not answer in the required form; another "
                               "did not finish."),
                          f"# Read the rejected answers in tasks/{t['dir']}/STATUS.md first.",
                          f"runner retry {info['name']} {task_id}{recover}"]
            else:
                lines.append(f"runner retry {info['name']} {task_id}"
                             + (" [--apply-patch]" if t["kind"] == "produce" else ""))
    for t in state["tasks"].values():
        for finding in t.get("ledger", {}).get("findings", []):
            if finding["status"] == "escalated" and t['status'] != 'blocked' and _held_for_ruling(t) is None:
                lines.append(f"runner resolve {info['name']} {finding['id']} --as resolved|advisory|upheld")
    lines += _resume_lines(info["name"], state)
    return lines


def _resume_lines(name, state):
    """The way on from a stop, by its kind. An open cleanup is closed first, whatever stopped the
    run (PROC-23, PROC-30); a budget stop adds what ran out; a lost record is repaired first."""
    reason = str(state.get("stop_reason", ""))
    if any((e.get("cleanup") or {}).get("status") == "open"
           for e in state.get("cleanup_obligations", [])):
        return [f"runner resume {name} --stop-orphans",
                "# if the stop keeps failing: stop the processes yourself, then",
                f"runner resume {name} --abandon-cleanup"]
    if state["status"] != "stopped":
        return [f"runner resume {name}"]
    if reason.startswith("the token cap"):
        return [f"runner resume {name} --add-tokens N"]
    if reason.startswith("budget cannot cover"):
        return [f"runner resume {name} --add-budget USD"]
    if reason.startswith("stopped: the run record was removed"):
        return [f"runner repair-record {name}", f"runner resume {name}"]
    return [f"runner resume {name}"]


def markdown_status(sections):
    out = []
    for heading, lines in sections:
        block = [heading] if heading else []
        for line in lines:
            if line[0] == "table":
                _kind, header, rows = line
                block += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
                block += ["| " + " | ".join(row) + " |" for row in rows]
            else:
                block.append({"text": "", "item": "- ", "code": "    "}[line[0]] + line[1])
        out.append("\n".join(block))
    return "\n\n".join(out) + "\n"


def render_run_status(info, state, disposition=None, now=None, run_path=None, alive=False,
                      refresh_s=HEARTBEAT_S):
    """STATUS.md of a run (see `run_status_model`)."""
    return markdown_status(run_status_model(info, state, disposition, now, run_path, alive, refresh_s))


def _inline_html(text):
    """Escape everything, then render emphasis, code and local JSON record links."""
    text = html.escape(text, quote=True)
    text = re.sub(r"\[([^]\n]+)\]\((tasks/[A-Za-z0-9_./-]+\.json)\)",
                  r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def _href(rel):
    return html.escape(urllib.parse.quote(rel), quote=True)


STATUS_CSS = """
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
max-width:1100px;margin:16px auto;padding:0 16px;color:#1d1d1f;background:#fff}
h1{font-size:20px;margin:8px 0 12px}h2{font-size:16px;margin:20px 0 6px;border-bottom:1px solid #ddd}
h3{font-size:14px;margin:12px 0 4px}table{border-collapse:collapse;margin:8px 0}
th,td{border:1px solid #ccc;padding:3px 8px;text-align:left;vertical-align:top}th{background:#f3f3f3}
code,pre{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
pre{background:#f6f6f6;padding:8px;overflow-x:auto}ul{margin:4px 0;padding-left:22px}
.updated{color:#555}a{color:#0645ad}.files li{margin:2px 0}
@media (prefers-color-scheme: dark){body{background:#1b1b1d;color:#e6e6e6}th{background:#2a2a2d}
pre{background:#252528}a{color:#8ab4f8}th,td{border-color:#444}.updated{color:#aaa}}
"""

# What the page links to in each attempt, round and invocation directory, and at the task level.
LINKED_IN_TASK = ("STATUS.md", "task.json", "findings.json", "decision.json", "commit.json",
                  "failed.patch", "set-aside.json")
LINKED_IN_STEP = ("prompt.md", "feedback.md", "result.json", "verdict.json", "verification.json",
                  "outputs.json", "changes.diff", "reverted.json", "embedded.json", "gate.log",
                  "replay.log", "responses.json")
LINKED_IN_INVOCATION = ("prompt.md", "stdout.log", "stderr.log", "outcome.json", "last-message.txt")


def _numbered(directory, prefix):
    try:
        names = [n for n in os.listdir(directory) if n.startswith(prefix + "-")
                 and n[len(prefix) + 1:].isdigit() and os.path.isdir(os.path.join(directory, n))]
    except OSError:
        return []
    return sorted(names, key=lambda n: int(n[len(prefix) + 1:]))


def _links(base, rel_dir, names):
    return [f'<a href="{_href(rel_dir + n)}">{html.escape(n)}</a>' for n in names
            if os.path.exists(os.path.join(base, rel_dir, n))]


def record_links(run_path, info, state, alive):
    """The HTML list of the record's files, relative to the run directory so that it works from
    file://. Only what exists is linked; the lock only while a runner holds it."""
    parts = ['<h2>Record</h2>', '<ul class="files">']
    top = _links(run_path, "", ("workflow.toml", "workflow.expanded.json", "state.json", "events.jsonl",
                                "integrity.json", "run.json", "STATUS.md", "index.json"))
    source = info.get("workflow_file")
    if source and os.path.isfile(source):
        top.append(f'<a href="{_href(os.path.relpath(source, run_path).replace(os.sep, "/"))}">'
                   f'the workflow file ({html.escape(os.path.basename(source))})</a>')
    parts.append("<li>Run: " + " · ".join(top) + "</li>")
    # The runner releases the lock as the run stops, after its last regeneration of this page.
    held = alive and state["status"] == "running"
    parts.append("<li>Lock: " + ('<a href="../../lock">lock</a> (held by the runner working on this run)'
                                 if held else "not held by a runner of this run") + "</li>")
    parts.append("</ul>")
    for task_id in state["order"]:
        tdir = f"tasks/{state['tasks'][task_id]['dir']}/"
        if not os.path.isdir(os.path.join(run_path, tdir)):
            continue
        parts.append(f'<h3><a href="{_href(tdir)}">{html.escape(task_id)}</a></h3><ul class="files">')
        files = _links(run_path, tdir, LINKED_IN_TASK)
        if files:
            parts.append("<li>" + " · ".join(files) + "</li>")
        for step in _numbered(os.path.join(run_path, tdir), "attempt") + _numbered(os.path.join(run_path, tdir), "round"):
            sdir = tdir + step + "/"
            parts.append(f'<li><a href="{_href(sdir)}">{step}/</a> ' + " · ".join(_links(run_path, sdir, LINKED_IN_STEP)))
            calls = _numbered(os.path.join(run_path, sdir), "invocation")
            if calls:
                parts.append("<ul>")
                for call in calls:
                    cdir = sdir + call + "/"
                    parts.append(f'<li><a href="{_href(cdir)}">{call}/</a> '
                                 + " · ".join(_links(run_path, cdir, LINKED_IN_INVOCATION)) + "</li>")
                parts.append("</ul>")
            parts.append("</li>")
        parts.append("</ul>")
    return parts


def html_status(sections, info, state, run_path, alive):
    """STATUS.html: the same sections as STATUS.md, plus links into the record. Static, no
    scripts, nothing fetched; it reloads itself every 15 s only while the run is running."""
    title = html.escape(f"{info['workflow']} — run {info['run_id'][:8]}")
    out = ["<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">']
    if state["status"] == "running":
        out.append('<meta http-equiv="refresh" content="15">')
    out += [f"<title>{title}</title>", f"<style>{STATUS_CSS}</style>", "</head><body>"]
    for heading, lines in sections:
        if heading:
            level = 1 if heading.startswith("# ") else 2
            out.append(f"<h{level}>{_inline_html(heading.lstrip('#').strip())}</h{level}>")
        open_list = None
        for line in lines + [("end", "")]:
            kind = line[0]
            if open_list and kind != open_list:
                out.append("</ul>" if open_list == "item" else "</pre>")
                open_list = None
            if kind == "item":
                if not open_list:
                    out.append("<ul>")
                    open_list = "item"
                out.append(f"<li>{_inline_html(line[1])}</li>")
            elif kind == "code":
                if not open_list:
                    out.append("<pre>")
                    open_list = "code"
                out.append(html.escape(line[1]))
            elif kind == "text":
                css = ' class="updated"' if line[1].startswith("Updated ") and heading is None else ""
                out.append(f"<p{css}>{_inline_html(line[1])}</p>")
            elif kind == "table":
                _kind, header, rows = line
                out.append("<table><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in header) + "</tr>")
                for row in rows:
                    cells = [html.escape(c) for c in row]
                    tdir = f"tasks/{state['tasks'][row[1]]['dir']}/" if row[1] in state["tasks"] else ""
                    if tdir and run_path and os.path.isdir(os.path.join(run_path, tdir)):
                        cells[1] = f'<a href="{_href(tdir)}">{cells[1]}</a>'
                    out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
                out.append("</table>")
    if run_path:
        out += record_links(run_path, info, state, alive)
    out.append("</body></html>")
    return "\n".join(out) + "\n"


def _rejected_answer_headline(entry):
    """What one rejected answer claimed, read from its summary alone and never judged here."""
    verdict, found, unreadable = entry["verdict"], entry["findings"], entry["unreadable_findings"]
    if verdict is None:
        return "no readable answer" if not found and not unreadable else "no readable verdict"
    if not found:
        return f"claimed verdict `{verdict}`, no findings"
    return f"claimed verdict `{verdict}`, {len(found)} finding" + ("s" if len(found) > 1 else "")


def _render_rejected_answer(entry):
    lines = [f"- **{entry['reviewer']}**, round {entry['round']}, try {entry['try']} — "
             f"{_rejected_answer_headline(entry)}:"]
    lines += [f"  - {f['severity']}: {f['title']}" for f in entry["findings"]]
    if entry["unreadable_findings"]:
        lines.append(f"  - {entry['unreadable_findings']} further entries could not be read")
    lines.append(f"  Rejected because: {entry['error']}")
    if entry.get("invocation"):
        lines.append(f"  Answer: `{entry['invocation']}/last-message.txt`")
    return lines


def _repaired_answers(ledger):
    """(reviewer, round, dropped entries) per repaired review answer, oldest first, derived from
    the `repair` history event `findings.apply_review` leaves on every finding a repaired answer
    raised, so this needs no new state.

    Answers are told apart by the `answer` discriminator that event carries: the findings of one
    answer share it, and no two answers do, so a repair keeps its own line for the life of the run
    whatever happens to its findings afterwards — a response, a resolution, or a retry, which
    supersedes them and clears the reviewers' rounds, so that the next repair is round 1 again. An
    event written before that field existed groups by its reviewer and round, as it did then."""
    answers = {}
    for f in (ledger or {}).get("findings", []):
        repair = next((h for h in f["history"] if h["event"] == "repair"), None)
        if repair is not None:
            answers.setdefault((f["reviewer"], repair["round"], repair.get("answer")),
                               repair["dropped"])
    return [(reviewer, round_no, dropped)
            for (reviewer, round_no, _answer), dropped in answers.items()]


def review_churn(ledger):
    """What the rework rounds bought, from the ledger alone (deep dive DD-06/DD-08): per round
    the blocking findings raised, resolved and kept open; how many "fixed" answers a reviewer
    refused; disputes against answers. `rounds` is in ledger round order (a retry that keeps the
    ledger continues the numbering). The past is counted by the severity at the time, so a ruling
    that made a blocker advisory does not erase the dispute it ended (RUN-47); `rulings` counts
    the person's decisions by kind and `open_now` the blockers open now. None when the ledger never
    had a blocking finding."""
    findings = [f for f in (ledger or {}).get("findings", []) if _blocked_once(f)]
    if not findings:
        return None
    rounds, refused, answers, disputes, rulings = {}, 0, 0, 0, {}
    for f in findings:
        last_answer = None
        for h in f.get("history", []):
            event = h.get("event")
            if event == "human":
                rulings[h.get("decision")] = rulings.get(h.get("decision"), 0) + 1
            if event == "raised":
                rounds.setdefault(h.get("round"), {"raised": 0, "resolved": 0, "kept": 0})["raised"] += 1
            elif event == "response":
                answers += 1
                last_answer = (h.get("action") or (h.get("response") or {}).get("action"))
                disputes += last_answer == "disputed"
            elif event == "resolution":
                r = rounds.setdefault(h.get("round"), {"raised": 0, "resolved": 0, "kept": 0})
                if h.get("status") == "resolved":
                    r["resolved"] += 1
                else:
                    r["kept"] += 1
                    refused += last_answer == "fixed"
    ordered = [dict(round=k, **v) for k, v in sorted(rounds.items(), key=lambda kv: (kv[0] is None, kv[0]))]
    chain = 0                                    # trailing rework rounds that each raised new blockers
    for r in reversed(ordered[1:]):
        if r["raised"]:
            chain += 1
        else:
            break
    open_now = sum(f.get("severity") == "blocking" and f.get("status") in ("open", "disputed", "escalated")
                   for f in findings)
    return {"rounds": ordered, "refused_fixes": refused, "answers": answers, "disputes": disputes,
            "chain": chain, "rulings": rulings, "open_now": open_now}


def _blocked_once(f):
    """Whether a finding was blocking when it was raised. A ledger written before the raised event
    recorded its severity tells by what only a blocker receives: an answer, a reviewer's
    resolution or a ruling."""
    history = f.get("history", [])
    raised = next((h for h in history if h.get("event") == "raised"), {})
    if "severity" in raised:
        return raised["severity"] == "blocking"
    return f.get("severity") == "blocking" or any(
        h.get("event") in ("response", "resolution", "human") for h in history)


def rulings_phrase(rulings):
    """"2 (2 advisory)" of a churn's rulings; "0" when there were none."""
    total = sum(rulings.values())
    kinds = ", ".join(f"{n} {kind}" for kind, n in sorted(rulings.items()))
    return f"{total} ({kinds})" if total else "0"


def churn_lines(task_id, churn, status):
    """The STATUS wording for a producer whose rework rounds are not converging: a chain of
    rounds each raising new blockers, or a fix the reviewer refused more than once. '' when the
    ledger shows neither."""
    if not churn or not (churn["chain"] >= 2 or churn["refused_fixes"] >= 2):
        return ""
    parts = []
    if churn["chain"] >= 2:
        parts.append(f"the last {churn['chain']} rework rounds each raised new blocking findings")
    if churn["refused_fixes"] >= 2:
        parts.append(f"a reviewer refused an answer of `fixed` {churn['refused_fixes']} times")
    answered = (f"{churn['disputes']} dispute{'s' if churn['disputes'] != 1 else ''} in "
                f"{churn['answers']} answer{'s' if churn['answers'] != 1 else ''}")
    advice = ("Before `retry`, read the open findings: a brief that the reviewers read two ways, "
              "or a case the brief does not make reachable, is yours to settle, not the author's"
              if status in ("blocked", "failed") else
              "If the brief is being read two ways, a dispute from the author or a ruling from you "
              "ends it sooner than another round")
    return (f"**{task_id} is churning**: " + "; ".join(parts) + f"; {answered}. {advice}.")


def owner_ruling_lines(task_id, t, task_page=False):
    if t['status'] != 'accepted':
        return []
    rulings = findings.owner_rulings(t.get('ledger'))
    if not rulings:
        return []
    prefix = '' if task_page else f"tasks/{t['dir']}/"
    candidate_file = (os.path.basename(t['attempt_dir']) + '/outputs.json'
                      if t.get('attempt_dir') else 'commit.json')
    commit = t.get('commit')
    lines = [f"{task_id}: accepted with owner rulings; candidate "
             f"tree [{t.get('candidate') or 'unknown'}]({prefix}{candidate_file})"
             + (f" (commit {commit[:7]})" if commit else "") + ". "
             f"[Findings and full owner notes]({prefix}findings.json)."]
    for h in rulings:
        note = prompts.bounded_text(h.get('note', ''), 2000)
        ruled = h.get('candidate')
        where = prompts.tree_and_commit(ruled, commit if ruled and ruled == t.get('candidate') else None) \
            if ruled else 'unrecorded'
        lines.append(f"{h['finding']}: {h['decision']}, ruled by {h.get('by') or 'an unnamed person'}"
                     f"{ruling_how(h)} on candidate {where}, {h.get('at') or 'time unrecorded'}: {note}")
    return lines


def ruling_how(h):
    """" (non-interactive, name from the environment; agent session markers: CLAUDECODE)" of a
    person's decision (REC-49): how the name was given and where the command ran, so a ruling
    typed by the operating agent under the owner's login does not read as the owner's own. ''
    for a decision recorded before these were kept."""
    if 'by_source' not in h:
        return ''
    parts = ["interactive" if h.get('interactive') else "non-interactive",
             "name given with --by" if h.get('by_source') == 'flag' else "name from the environment"]
    markers = h.get('agent_markers') or []
    return (f" ({', '.join(parts)}"
            + (f"; agent session markers: {', '.join(markers)}" if markers else "") + ")")


def advisories_by_candidate(t):
    """(current, earlier) advisories of a producer's ledger (REC-48): `current` were raised on its
    accepted (or latest) candidate, or ruled advisory by a person on it; `earlier` the rest."""
    candidate = t.get('candidate')
    current, earlier = [], []
    for f in t.get('ledger', {}).get('findings', []):
        if f['severity'] != 'advisory':
            continue
        raised = next((h for h in f.get('history', []) if h.get('event') == 'raised'), {})
        ruled = [h.get('candidate') for h in f.get('history', [])
                 if h.get('event') == 'human' and h.get('decision') == 'advisory']
        on_it = candidate and (raised.get('candidate') == candidate or candidate in ruled)
        (current if on_it else earlier).append(f)
    return current, earlier


ADVISORY_TITLES_SHOWN = 3


def accepted_advisory_line(task_id, t):
    """"make: 2 advisories on d524bde — PE-2: …; PE-3: …" for an accepted producer with
    advisories on its accepted candidate (REC-48); '' otherwise."""
    if t['status'] != 'accepted' or t.get('kind', 'produce') != 'produce':
        return ''
    current, _earlier = advisories_by_candidate(t)
    if not current:
        return ''
    shown = []
    for f in current[:ADVISORY_TITLES_SHOWN]:
        title = " ".join(str(f.get('title', '')).split())
        shown.append(f"{f['id'].rpartition('/')[2]}: " + (title if len(title) <= 80 else title[:77] + "..."))
    more = len(current) - len(shown)
    on = (t.get('commit') or t.get('candidate') or 'unknown')[:7]
    n = len(current)
    return (f"{task_id}: {n} advisor{'y' if n == 1 else 'ies'} on {on} — " + "; ".join(shown)
            + (f"; and {more} more" if more > 0 else "") + f". See tasks/{t['dir']}/STATUS.md.")


def review_learning_lines(t):
    lines = []
    panel = t.get('panel') or {}
    if 'standing_rulings' in panel:
        n, dropped = len(panel['standing_rulings']), panel.get('standing_dropped', 0)
        why = ', '.join(dict.fromkeys(panel.get('standing_dropped_why') or ['the brief changed']))
        lines += ['', f"{n} standing rulings supplied to reviewers"
                  + (f" ({dropped} dropped: {why})" if dropped else '') + '.']
        lines += [f"- {e['finding_title']} at {e['location_glob']}: {e['decision']}, ruled by {e['by']} "
                  f"({findings.provenance(e)})" + (f" in run {e['run']}" if e.get('run') else '')
                  + f", {e['ruled_at']}." for e in panel['standing_rulings']]
    ledger = t.get('ledger') or {}
    pairs = findings.disagreements(ledger.get('findings', []))
    if pairs:
        lines += ['', '## Reviewer disagreements', '']
        lines += [f"- {a['id']} at {a['location']}: {findings.disagreement_note(b)} "
                  f"at {b['location']}. Grouping does not change severity." for a, b in pairs]
    followups = findings.lab_followups(ledger)
    if followups:
        lines += ['', '## Lab follow-ups', '']
        for item in followups:
            if item['kind'] == 'gateable':
                lines.append(f"- {item['finding']}: {item['title']}; command hint: "
                             f"{item['command_hint']}; review rounds: "
                             + ', '.join(map(str, item['rounds'])) + f" ({len(item['rounds'])} rounds).")
            else:
                lines.append(f"- {item['finding']}: {item['ruling']['note']}")
    audits = [(rid, r) for rid, r in ledger.get('reviewers', {}).items() if r.get('reach_audit')]
    if audits:
        lines += ['', '## Reach audit', '', '| Reviewer / round | Test | State claimed | Reach evidence | Oracle |',
                  '|---|---|---|---|---|']
        def cell(value):
            return str(value).replace('|', r'\|').replace('\n', ' ').replace('\r', ' ')
        for rid, r in audits:
            for entry in r['reach_audit']:
                lines.append('| ' + ' | '.join(cell(value) for value in
                    (f"{rid} / {r['round']}", *(entry[k] for k in
                     ('test', 'state_claimed', 'reach_evidence', 'oracle')))) + ' |')
    return lines


def render_task_status(task_id, t, tdir, run_name):
    kind = _block_kind(t)
    headline = t["status"] if not kind else (
        f"blocked: {PROTOCOL_BLOCK}" if kind == "protocol" else
        "blocked: the panel did not finish (one reviewer could not answer in the required form)")
    if t['status'] == 'accepted' and findings.owner_rulings(t.get('ledger')):
        headline = 'accepted with owner rulings'
    lines = [f"# {task_id} — {headline}", "", f"Kind {t['kind']}, type {t['type']}."]
    if t["reason"]:
        lines.append(f"Reason: {t['reason']}")
    if t["commit"]:
        lines.append(f"Accepted as commit {t['commit']}. See commit.json.")
    rulings = owner_ruling_lines(task_id, t, task_page=True)
    if rulings:
        lines += ["", "## Owner rulings", ""] + rulings
    current, earlier = advisories_by_candidate(t)
    candidate = t.get('candidate')
    for heading, group in ((f"## Advisories on the {'accepted' if t['status'] == 'accepted' else 'latest'} "
                            "candidate", current), ("## Earlier advisories", earlier)):
        if group:
            lines += ["", heading, ""]
            for f in group:
                lines += [f"- {f['id']}: {f['title']}"] + prompts.advisory_provenance(f, current=candidate)
    lines += review_learning_lines(t)
    budget = t.get("diff_budget")
    if budget:
        limits = " and ".join(counted(budget[k], k.rpartition('_')[2])
                              for k in ("max_changed_files", "max_changed_lines") if budget.get(k))
        lines.append(f"Diff budget of the latest candidate: {counted(budget['files'], 'files')} and "
                     f"{counted(budget['lines'], 'lines')} changed from its base, against limits of {limits}"
                     + (": over budget." if budget["result"] == "fail" else ": within budget."))
    if kind == "protocol":
        lines += ["", "This is a protocol failure of the panel, not a judgement of the work. No "
                  "finding from these rounds reached the ledger."]
    if kind:
        lines += ["", "Why each reviewer did not finish:"] + _reviewer_causes(t)
    entries = sorted(n for n in os.listdir(tdir)
                     if n.startswith(("attempt-", "round-")) and os.path.isdir(os.path.join(tdir, n)))
    entries.sort(key=lambda n: (n.split("-")[0], int(n.split("-")[1])))
    if entries:
        lines += ["", "## History"] + [f"- {n}/" for n in entries]
    churn = review_churn(t.get("ledger"))
    if churn:
        lines += ["", "## Review rounds", "",
                  "| Round | Blocking raised | Resolved | Kept open |", "|---|---|---|---|"]
        lines += [f"| {r['round']} | {r['raised']} | {r['resolved']} | {r['kept']} |" for r in churn["rounds"]]
        lines.append(f"\nAnswers: {churn['answers']}, disputed: {churn['disputes']}; `fixed` answers the "
                     f"reviewer refused: {churn['refused_fixes']}. Rulings by a person: "
                     f"{rulings_phrase(churn['rulings'])}; blocking findings open now: {churn['open_now']}.")
        warning = churn_lines(task_id, churn, t["status"])
        if warning:
            lines += ["", warning]
    if os.path.exists(os.path.join(tdir, "failed.patch")):
        lines += ["", "Its work was set aside in failed.patch; the candidate tree is pinned under "
                  "refs/code-smith/."]
        sa_path = os.path.join(tdir, "set-aside.json")
        if os.path.exists(sa_path):
            rec = read_json(sa_path)
            if rec["paths"]:                        # empty paths: nothing --apply-patch can put back
                lines.append(f"Set aside from attempt {rec['attempt']} ({rec['status']}: "
                             f"{rec['reason']}), {len(rec['paths'])} files.")
                lines.append(f"To put it back before the next attempt: runner retry {run_name} "
                             f"{task_id} --apply-patch")
    if t.get("recover"):
        lines += ["", f"Queued: the set-aside work of attempt {t['recover']['attempt']} will be "
                  "put back before the next attempt."]
    elif t.get("recovered"):
        r = t["recovered"]
        lines += ["", f"The set-aside work of attempt {r['attempt']} was put back before attempt "
                  f"{r['attempt'] + 1} ({r['files']} files)."]
    if t.get("git_repairs"):
        lines += ["", "## Git state put back", "",
                  "The author or a verifier changed what the transaction owns in git. The runner "
                  "put it back and left the files in the work tree as they were, to be judged as "
                  "any candidate is:", ""]
        lines += [f"- after {r['after']}: " + "; ".join(r["what"])
                  + (f" (their HEAD was {r['their_head'][:7]})" if r.get("their_head") else "")
                  for r in t["git_repairs"]]
    if t.get("rejected_reviews"):
        lines += ["", "## Rejected review answers", "",
                  "Not applied. Nothing below is in the ledger and none of it changed acceptance. "
                  "Read it before you retry: the concerns in it may be real.", ""]
        for entry in t["rejected_reviews"]:
            lines += _render_rejected_answer(entry)
    repaired = _repaired_answers(t.get("ledger"))
    if repaired:
        lines += ["", "## Repaired review answers", ""]
        for reviewer, round_no, dropped in repaired:
            what = ("1 meaningless `resolutions` entry was dropped" if len(dropped) == 1 else
                    f"{len(dropped)} meaningless `resolutions` entries were dropped")
            lines.append(f"- **{reviewer}**, round {round_no}: {what} and the answer was applied. "
                         "The entries named no finding in the ledger, and the round required none. "
                         "See findings.json.")
    return "\n".join(lines) + "\n"


FILE_NOTES = {
    "activity.json": "Hook telemetry routing, correlation IDs and configured source hashes",
    "hooks/": "Native headless-agent hook events; observational, not acceptance evidence",
    "hooks.jsonl": "Native hook events, correlated with run/task/invocation and session/tool IDs",
    "hooks.log": "Readable native hook events for tail -f",

    "run.json": "Identity of the run (id, workflow, root, branch, base commit) and its totals",
    "STATUS.md": "This directory in words. Regenerated from state.json",
    "STATUS.html": "The run's STATUS.md as a static page, with links into this record. Regenerated with it",
    "index.json": "This file: what every entry in this directory is",
    "state.json": "The engine's state. The single source of truth",
    "events.jsonl": "Append-only log, one JSON event per line",
    "workflow.toml": "Frozen copy of the workflow as started",
    "workflow.expanded.json": "Every task after types, personas, panels and defaults are applied",
    "integrity.json": "Hashes of the decision-bearing files, checked around every job",
    "git-index": "The run's scratch git index, used for work-tree snapshots",
    "qualification.json": "What doctor established for each agent profile, per capability",
    "library/": "Frozen copies of the type and persona files this run uses",
    "types/": "Frozen task type files",
    "personas/": "Frozen reviewer persona files",
    "briefs/": "Frozen content of every prompt_file, named by task id, and of every rules_file as <id>.rules.md",
    "replans/": "One directory per replan: before/, after/, plan.json, result.json",
    "tasks/": "One directory per task: <order>-<task id>",
    "task.json": "The resolved task definition",
    "follow-ups.json": "Derived lab follow-ups: gate hints and brief-change rulings, grouped by task",
    "findings.json": "The findings ledger of this producer: all reviewers, all rounds",
    "failed.patch": "The complete, binary-capable patch of work that was set aside",
    "set-aside.json": "What was set aside: the attempt, why, the base and candidate trees, the paths",
    "commit.json": "The accepted commit: sha, files, message",
    "decision.json": "Who approved or rejected, when, and the comment",
    "prompt.md": "Exactly what the agent was sent",
    "feedback.md": "What sent the work back, and the findings that need a response",
    "result.json": "The agent's validated answer, with cost, usage, seconds and session id",
    "responses.json": "The author's answer to each finding",
    "inputs.json": "Hashes of the upstream outputs this attempt was given",
    "outputs.json": "Manifest of the declared outputs after this attempt, and the candidate tree id",
    "changes.diff": "Readable diff of this attempt, capped. For reading, never for recovery",
    "reverted.json": "Paths the runner put back: outside `writes`, protected or frozen",
    "embedded.json": "Embedded git repositories the runner removed from this attempt's work",
    "gate.log": "Output of the gate commands",
    "replay.log": "Output of the gates run again on a clean checkout of the candidate",
    "verification.json": "Each gate, check and verdict with the candidate tree id it judged",
    "verdict.json": "The reviewer's validated answer, the candidate it judged, the diff base",
    "argv.json": "Exactly how the agent was invoked. Never holds credentials",
    "stdout.log": "The agent's standard output, streamed and redacted",
    "stderr.log": "The agent's standard error, streamed and redacted",
    "last-message.txt": "The agent's final message, written by this invocation",
    "schema.json": "The JSON schema the answer was asked to follow",
    "outcome.json": "How the call ended: ok, protocol-error, agent-error, timed-out, interrupted, environment, quota, transient; usage_source: terminal, provider-record or unknown",
}


def _note(name, is_dir):
    key = name + "/" if is_dir else name
    if key in FILE_NOTES:
        return FILE_NOTES[key]
    if not is_dir and name.startswith(".") and name.endswith(".tmp"):
        return "A leftover of an interrupted write (a kill before its rename). Ignored"
    if is_dir:
        stem, _, number = name.partition("-")
        if stem == "attempt" and number.isdigit():
            return f"Attempt {number} of this producer. Numbers are never reused"
        if stem == "round" and number.isdigit():
            return f"Review round {number} of this reviewer"
        if stem == "invocation" and number.isdigit():
            return f"Agent call {number}: raw invocation and output"
        if stem.isdigit():
            return f"Task '{number}'"
        return ""
    if name.endswith(".toml"):
        return "Frozen library file"
    if name.endswith(".md"):
        return "Frozen brief"
    return ""


def _about(run_path, directory):
    rel = os.path.relpath(directory, run_path).replace(os.sep, "/")
    if rel == ".":
        return "One run of a workflow. Start with STATUS.md"
    parts = rel.split("/")
    return _note(parts[-1], True) or f"Part of the run record: {rel}"


def render_index(run_path, directory):
    entries = {}
    names = set(os.listdir(directory)) | {"index.json"}
    for name in sorted(names):
        is_dir = os.path.isdir(os.path.join(directory, name))
        entries[name + ("/" if is_dir else "")] = _note(name, is_dir)
    rel = os.path.relpath(directory, run_path).replace(os.sep, "/")
    return {"path": "" if rel == "." else rel, "about": _about(run_path, directory),
            "files": entries}
