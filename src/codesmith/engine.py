"""The scheduler and the task lifecycle (05). The only module that decides anything, and only from
exit codes, validated answer fields and counters. `agents`, `checks`, `gitops` and `record` report
facts and carry out effects.

One producer owns the tree through checks, parallel review panels, rework and acceptance.
Ledger verdicts, candidate snapshots and durable budget reservations determine every transition.
"""

import threading
import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import tomllib
import uuid

from .providers import ProviderRouting
from . import agents, checks, gitops, patterns, prompts, record, validate, qualification, findings, budgets
from . import proc, transaction
from .transaction import move, set_status
from .panels import Panels
from .workflow import DIFF_BUDGET_KEYS

EXIT_OK, EXIT_FAILED, EXIT_HUMAN = 0, 2, 255
PROTOCOL_RETRIES = 2
PAUSE, DONE = "pause", "done"
REPLAY_NAMED = 20        # ignored paths a replay failure names
IGNORED_SHOWN = 50       # ignored paths new since the base, kept for outputs.json and reviewers
IGNORED_AT_BASE = 1000   # the most ignored entries a transaction's base listing keeps


class CleanupStop(Exception):
    """An invocation's cleanup is open (PROC-21): the run stops before the next call. Its own
    stop, not the owner's pause: its event is `cleanup-stop`, a waiting `runner pause` request
    stays, and the message ends with the cleanup's remedy, never a plain `resume` (PROC-30)."""


class StandingExportError(Exception):
    """The queued standing rulings could not be written to `name` (RUN-73, RUN-76)."""

    def __init__(self, name, cause):
        super().__init__(f"the standing rulings could not be written to {name}: {cause}")
        self.name = name


class EngineStop(Exception):
    """The run cannot continue as it is. `environment` stops use no attempt."""

    def __init__(self, message, exit_code=EXIT_FAILED, remember=True):
        super().__init__(message)
        self.exit_code = exit_code
        # False when the owner is asked to change the tree or the git state before `resume`: the
        # stop then records no tip and tree for `resume` to hold them to.
        self.remember = remember


def _env_crash(point):
    if os.environ.get("CODE_SMITH_CRASH_AT") == point:
        os._exit(70)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Engine(ProviderRouting, Panels):
    def __init__(self, run, git, out=None, crash=None, environ=None):
        self.run, self.git = run, git
        self.out = out or sys.stderr
        self.crash = crash or _env_crash
        self.environ = dict(os.environ if environ is None else environ)
        wf = record.read_json(os.path.join(run.path, "workflow.expanded.json"))   # frozen
        self.wf = wf
        self.tasks = {t["id"]: t for t in wf["tasks"]}
        self.order = list(run.state["order"])
        self.defaults = wf["defaults"]
        self.root = wf["paths"]["root"]
        rel = os.path.relpath(os.path.realpath(self.root), git.top).replace(os.sep, "/")
        self.prefix = "" if rel == "." else rel
        self.run_id = run.state["run_id"]
        git.runs_dir = os.path.dirname(os.path.dirname(run.path))   # never restored (W-02)
        self.lost = False                          # the record was removed under the runner (W-02)

    # -- small helpers -----------------------------------------------------------------------

    def say(self, text):
        print(text, file=self.out)
        self.out.flush()

    def st(self, tid):
        return self.run.state["tasks"][tid]

    def to_root(self, top_path):
        """A repository path as the workflow sees it, or None when it lies outside `root`."""
        if not self.prefix:
            return top_path
        return top_path[len(self.prefix) + 1:] if top_path.startswith(self.prefix + "/") else None

    def to_top(self, root_path):
        return f"{self.prefix}/{root_path}" if self.prefix else root_path

    def snapshot(self):
        for rel in self.git.embedded_repositories():
            raise EngineStop(f"an embedded git repository is in the work tree at '{rel}'; a "
                             "snapshot cannot be taken. Remove it, then `runner resume`")
        return self.git.snapshot(self.run.index_file)

    def pin(self, name, obj):
        _pin(self.run, self.git, name, obj)

    def restore(self, target, paths, expected):
        """Put `paths` back to `target` under an intent, so a crash in the middle is repaired."""
        paths = self.hide_record(paths)
        op = self.run.begin("restore", target=target, paths=list(paths), expected=expected)
        try:
            self.git.restore(target, paths, expected_tree=expected, index_file=self.run.index_file,
                             crash=self.crash)
        except gitops.RestoreError as exc:
            raise EngineStop(f"environment failure: {exc}") from exc
        self.run.finish(op, restored=len(paths))

    def save(self):
        self.run.save()
        self.run.write_findings()
        self.run.regenerate_safely()

    def hide_record(self, paths):
        """A path inside the run record in a snapshot means the record's .gitignore was removed:
        it is put back, with a `record-ignore-restored` event, and those paths are never the
        author's change to restore (W-02). Returns the other paths."""
        inside = [p for p in paths if self.git.in_runs_dir(p)]
        if inside:
            ignore = os.path.join(os.path.dirname(os.path.dirname(self.run.path)), ".gitignore")
            try:
                with open(ignore, encoding="utf-8") as fh:
                    was = "changed" if fh.read() != "*\n" else ""
            except FileNotFoundError:
                was = "missing"
            if not was:              # its ignore file is as written, yet git sees it: not ours to fix
                raise EngineStop("the run record is visible to git, so its files look like changes "
                                 f"in the work tree ({', '.join(inside[:3])}); the runner never "
                                 "restores them. Make git ignore it again, then `runner resume`",
                                 remember=False)
            record.write_durable(ignore, b"*\n")
            self.run.event("record-ignore-restored", was=was, seen=inside[:3])
        return [p for p in paths if p not in inside]

    def producers(self, status=None):
        return [t for t in (self.tasks[i] for i in self.order) if t["kind"] == "produce"
                and (status is None or self.st(t["id"])["status"] == status)]

    def verifiers_of(self, tid, kind):
        return [self.tasks[i] for i in self.order
                if self.tasks[i]["kind"] == kind and self.tasks[i].get("verifies") == tid]

    def pause_point(self, what):
        """Before a call starts: stop here if the owner asked for a pause. Nothing is running at
        a pause point, so the stop needs no reconciliation and loses no work."""
        self.cleanup_point()
        if self.run.pause_requested():
            raise budgets.Paused(f"paused at the owner's request before {what}")

    def cleanup_point(self):
        if self.run.cleanup_problems():
            raise CleanupStop(record.cleanup_refusal(self.run))

    # -- the loop (05, The engine loop) ------------------------------------------------------

    def execute(self):
        """Run until nothing can start, with a heartbeat that keeps STATUS.md and STATUS.html
        alive meanwhile: every `status_refresh_s` seconds (0: none) a thread rewrites them from
        the state in memory, so a long agent call shows its age and tokens. It only reads the
        state and writes the derived pages; it never waits on the call or holds it up."""
        stop = threading.Event()
        interval = self.defaults.get("status_refresh_s", record.HEARTBEAT_S)
        self.run.refresh_s = interval

        def beat():
            while not stop.wait(interval):
                self.run.refresh_status()

        thread = threading.Thread(target=beat, name="status-heartbeat", daemon=True)
        if interval:
            thread.start()
        try:
            code = self._execute()
            if self.run.state["status"] in ("needs_human", "stopped", "failed") and not self.lost:
                self.stopped_now()
            return code
        finally:
            stop.set()
            if interval:
                thread.join(timeout=5)
            # Whatever ended the loop, an interrupt included, the page shows the state as it is:
            # the operations left without an outcome, and that `resume` reconciles them.
            self.run.refresh_status()

    def _execute(self):
        """Run until nothing can start. Returns the exit code."""
        state = self.run.state
        try:
            self.cleanup_point()
            problems = self.run.record_problems()
            if problems:
                raise EngineStop("the run record was changed: " + "; ".join(problems) + ". "
                                 + record.repair_hint(self.run.name))
            for task in (t for t in self.tasks.values() if t["kind"] in ("produce", "review")):
                try:
                    agents.make(task["agent"], self.wf["agents"][task["agent"]])
                except agents.UnknownAgent as exc:
                    raise EngineStop(str(exc)) from exc
            state["status"] = "running"
            state["expect"] = None
            state.pop("stop_reason", None)
            stopped = state.pop("stopped_at", None)
            if stopped:                  # the time the run waited, stopped, for a person (RUN-50)
                state["stopped_seconds"] = round(state.get("stopped_seconds", 0)
                                                 + max(0.0, time.time() - stopped))
            self.settle()
            while True:
                self.mark_skips()
                active = state.get("active_producer")
                if active:
                    if self.advance(active) == PAUSE:
                        break
                    continue
                task = self.next_ready()
                if task is None:
                    break
                if task["kind"] == "produce":
                    self.begin_transaction(task)
                elif task["kind"] == "check":
                    self.standalone_check(task)
                elif task["kind"] == "human":
                    set_status(self.st(task["id"]), task["id"], "waiting_human",
                               reason="waiting for a person's approval")
                    self.save()
        except budgets.Paused as stop:
            state.update(status="stopped", stop_reason=str(stop))
            self.remember_tree()
            self.save()
            self.run.clear_pause()
            self.run.event("pause-stop", reason=str(stop))
            self.say(f"runner: {stop}. Continue with runner resume")
            return EXIT_FAILED
        except CleanupStop as stop:
            # Not the owner's pause: the pause request a person left waiting stays, and the
            # reason already ends with the cleanup's own remedy (PROC-30).
            state.update(status="stopped", stop_reason=str(stop))
            self.remember_tree()
            self.save()
            self.run.event("cleanup-stop", reason=str(stop))
            self.say(f"runner: {stop}")
            return EXIT_FAILED
        except budgets.Exhausted as stop:
            return self.budget_stop(stop)
        except EngineStop as stop:
            state["status"] = "failed"
            state["stop_reason"] = str(stop)
            if stop.remember:
                self.remember_tree()
            else:
                state["expect"] = None
            self.save()
            self.say(f"runner: {stop}")
            return stop.exit_code
        except (record.RecordError, transaction.RunnerError) as exc:
            state["status"] = "failed"
            state["stop_reason"] = str(exc)
            self.run.save()
            self.say(f"runner: {exc}")
            return EXIT_FAILED
        except OSError as exc:
            if os.path.exists(os.path.join(self.run.path, "integrity.json")):
                raise
            return self.record_lost(exc)
        return self.finish_run()

    def record_lost(self, exc):
        """The run record was removed under the runner (`git clean -fdx` or `rm -rf .runs` by a
        writer), found when the runner next wrote into it. The state in memory is saved again,
        the run directory recreated for it, and the run stops there: nothing else is written into
        a record whose decision files are gone. `repair-record` puts them back from git (W-02).
        The run is `stopped`, not `failed`: repair and resume continue it (K8)."""
        state = self.run.state
        reason = (f"stopped: the run record was removed while the runner worked ({exc.filename or exc}); "
                  + record.repair_hint(self.run.name))
        state.update(status="stopped", stop_reason=reason, expect=None, stopped_at=round(time.time(), 1))
        self.lost = True
        self.run.save()
        self.run.event("record-lost", missing=exc.filename or str(exc))
        self.say(f"runner: {reason}")
        return EXIT_FAILED

    def budget_stop(self, stop):
        """Stop the run for budget: a pause that `resume --add-budget/--add-tokens` continues."""
        self.run.state.update(status="stopped", stop_reason=str(stop))
        self.remember_tree()
        self.stopped_now()
        self.run.event("budget-stop", reason=str(stop))
        self.run.event("run-stopped", status="stopped")
        self.say(f"runner: {stop}. Continue with runner resume {stop.hint}")
        return EXIT_FAILED

    def stopped_now(self):
        """Saves the state with the moment the run stopped; the next `_execute` counts the wait."""
        self.run.state["stopped_at"] = round(time.time(), 1)
        self.save()

    def finish_run(self):
        state = self.run.state
        statuses = [self.st(i)["status"] for i in self.order]
        if all(s == "accepted" for s in statuses):
            state["status"], code = "done", EXIT_OK
        elif any(s in ("waiting_human", "blocked") for s in statuses):
            state["status"], code = "needs_human", EXIT_HUMAN
        else:
            state["status"], code = "failed", EXIT_FAILED
        exported = True
        if state["status"] == "done":
            exported = self.export_standing()
            if not exported:
                state["status"], code = "failed", EXIT_FAILED
        if not exported:
            # Every task is accepted and nothing else uses the tree: the owner's fix of the cause
            # (a rulings file edited in the tree, say) is no change `resume` refuses (RUN-73).
            state["expect"] = None
        elif state["status"] != "done":
            self.remember_tree()
        self.save()
        self.run.event("run-stopped", status=state["status"])
        return code

    def export_standing(self):
        """Write the standing rulings `resolve --standing` queued into the rulings file, once the
        run is done and no task can see the tree again (V-01 of the K–N review). False, with the
        run's stop reason, when the file cannot be written; `resume` tries again."""
        try:
            done = export_queued(self.run, self.git.top, self.defaults.get("rulings_file"), self.crash)
        except StandingExportError as exc:
            self.run.state["stop_reason"] = f"{exc}. Fix it, then `runner resume`"
            self.say(f"runner: {self.run.state['stop_reason']}")
            return False
        for name, count in done:
            self.say(f"runner: {count} standing ruling(s) written to {name}; commit it, so the "
                     "next run reads them")
        return True

    def remember_tree(self):
        """While a run is stopped, `resume` checks that nobody moved the branch or the tree."""
        try:
            self.run.state["expect"] = {"tip": self.git.head(), "tree": self.snapshot()}
        except (EngineStop, gitops.GitError):
            self.run.state["expect"] = None

    def settle(self):
        """After a reconciliation, an acceptance may be recorded whose bookkeeping is not done."""
        for tid in self.order:
            task, st = self.tasks[tid], self.st(tid)
            if task["kind"] == "check" and not task.get("verifies") and st["status"] == "running":
                base = st["check_base"]
                now = self.snapshot()
                self.restore(base, [p for _s, p, _o, _n in self.git.changed_paths(base, now)], base)
                set_status(st, tid, "pending", reason="interrupted check; restored before retry")
                self.run.save()
        for t in self.producers("accepted"):
            if self.st(t["id"]).get("step"):
                self.finalize_acceptance(t)

    def prerequisites(self, task):
        needs = set(task["needs"])
        if task["kind"] == "produce":
            for verifier in self.verifiers_of(task["id"], "check") + self.verifiers_of(task["id"], "human") + self.reviewers_of(task["id"]):
                needs.update(verifier["needs"])
        return sorted(needs)

    def mark_skips(self):
        """The skip closure follows `needs`, `reviews` and `verifies` together. A verifier
        that objected to a producer which then ended unsuccessfully will never be accepted
        either, so it counts as dead too."""
        dead = ("failed", "blocked", "skipped")

        def why_dead(n):
            status = self.st(n)["status"]
            if status in dead:
                return f"'{n}' is {status}"
            target = self.tasks[n].get("verifies") or self.tasks[n].get("reviews")
            if status == "objected" and target and self.st(target)["status"] in dead:
                return f"'{n}' is objected and '{target}' is {self.st(target)['status']}"
            return None

        changed = True
        while changed:
            changed = False
            for tid in self.order:
                t, st = self.tasks[tid], self.st(tid)
                if st["status"] not in ("pending", "waiting_human") or \
                        (st["status"] == "waiting_human" and not t.get("verifies")):
                    continue
                reason = next((r for r in map(why_dead, self.prerequisites(t)) if r), None)
                target = t.get("verifies") or t.get("reviews")
                if not reason and target and self.st(target)["status"] in dead:
                    reason = f"'{target}' is {self.st(target)['status']}"
                if reason:
                    set_status(st, tid, "skipped", reason=reason)
                    changed = True

    def next_ready(self):
        """The first task in workflow order whose `needs` are all accepted. A verifying check or
        human task is driven by its producer's transaction, never scheduled on its own."""
        for tid in self.order:
            t, st = self.tasks[tid], self.st(tid)
            if st["status"] != "pending" or t.get("verifies") or t.get("reviews"):
                continue
            if all(self.st(n)["status"] == "accepted" for n in self.prerequisites(t)):
                return t
        return None

    # -- standalone checks --------------------------------------------------------------

    def standalone_check(self, task):
        tid, st = task["id"], self.st(task["id"])
        base = self.snapshot()
        self.pin(f"{tid}/check-base", base)
        set_status(st, tid, "running", reason="", check_base=base)
        self.run.save()
        n, adir = self.run.new_attempt(tid)
        results, problems = self.run_commands(task["run"], tid, adir, task["gate_timeout_min"])
        after = self.snapshot()
        verdict, reason = "accepted", ""
        if problems:
            verdict, reason = "failed", "the run record was changed: " + "; ".join(problems)
        elif not checks.passed(results, task["run"]):
            last = results[-1]
            verdict, reason = "failed", f"`{last['command']}` did not pass ({last['result']})"
        if after != base:
            changed = [p for _s, p, _o, _n in self.git.changed_paths(base, after)]
            self.restore(base, changed, base)
            if not task["restores"]:
                what = "declared read_only but wrote: " if task["read_only"] else "left changes in the tree: "
                verdict, reason = "failed", what + ", ".join(changed)
        self.run.write_decision(os.path.join(adir, "verification.json"),
                                {"task": tid, "tree": base, "results": results,
                                 "result": "pass" if verdict == "accepted" else "fail"})
        set_status(st, tid, verdict, reason=reason)
        self.save()
        self.say(f"{tid}: {verdict}" + (f" ({reason})" if reason else ""))

    def run_commands(self, commands, tid, adir, timeout_min, cwd=None, log="gate.log", replay=False):
        results, problems = [], []
        for command in commands:
            self.pause_point(f"the next command of '{tid}'")
            op = self.run.begin("command", task=tid, command=command,
                                **({"replay": True} if replay else {}))
            guard = self.run.integrity_begin()
            startup_problems = []

            def started(identity):
                # The identity is made durable first, so that a kill from here on leaves a process
                # `resume` can see; hashing every protected file comes after.
                changed = self.run.guard_changes(guard)
                self.run.amend(op, process=identity)
                guard["state.json"] = hashlib.sha256(record.dump_json(self.run.state)).hexdigest()
                startup_problems.extend(changed)
                if "leader" not in identity.get("group", {}):
                    startup_problems.extend(self.run.integrity_check())
                return lambda: self.crash("replay:running" if replay else "command:running")

            ran = checks.run_commands([command], cwd=cwd or self.root,
                                      log_path=os.path.join(adir, log),
                                      timeout_s=timeout_min * 60,
                                      env=checks.command_env(self.environ, self.run_id, tid),
                                      on_start=started)
            problems = startup_problems + self.run.integrity_end(guard)
            seconds = sum(r["seconds"] for r in ran)
            key = "replay_seconds" if replay else "gate_seconds"         # RUN-50
            self.run.state[key] = round(self.run.state.get(key, 0) + seconds, 3)
            self.run.finish(op, result=ran[-1]["result"], seconds=round(seconds, 3),
                            **({"cleanup": ran[-1]['cleanup']} if ran[-1].get('cleanup') else {}))
            self.cleanup_point()
            results.extend(ran)
            if problems or not checks.passed(ran, [command]):
                break
        return results, problems

    # -- the producer transaction -------------------------------------------------------

    def begin_transaction(self, task):
        """Open the producer's transaction. A queued recovery is checked again here, against the
        tree as it is now, and the intent is recorded before any state changes, so a refusal
        leaves the task pending with its request queued and a crash is settled from the intent."""
        tid = task["id"]
        base = self.snapshot()
        if base != self.git.tree_of("HEAD") or not self.git.is_clean():
            raise EngineStop(f"the work tree is not at a clean accepted state, so '{tid}' cannot "
                             "start: " + ", ".join(self.git.dirty_paths()[:10]))
        plan = op = None
        if queued_recovery(self.run, self.git, tid):
            try:
                plan = recovery_plan(self.run, self, self.git, tid)
            except Refused as exc:
                raise EngineStop(str(exc)) from exc
        if plan:
            rec, paths = plan["record"], plan["paths"]
            op = self.run.begin("recover", task=tid, attempt=rec["attempt"], head=self.git.head(),
                                target=rec["candidate"], base=base, expected=plan["expected"],
                                paths=paths, ruled_candidate=(rec['candidate'] if
                                    queued_recovery(self.run, self.git, tid).get('ruled') and
                                    plan['expected'] == rec['candidate'] else None))
            self.crash("recover:before-restore")
            try:
                self.git.restore(rec["candidate"], paths, expected_tree=plan["expected"],
                                 index_file=self.run.index_file, crash=self.crash)
            except gitops.RestoreError as exc:
                raise EngineStop(f"environment failure: {exc}") from exc
            self.crash("recover:after-restore")
            self.say(f"{tid}: put back the set-aside work of attempt {rec['attempt']} "
                     f"({len(paths)} files)")
        open_transaction(self.run, self.git, tid, base, plan, op)
        self.say(f"{tid}: started")

    def advance(self, tid):
        """One step of the active producer's life. Returns PAUSE when a person is needed."""
        self.cleanup_point()
        task, st = self.tasks[tid], self.st(tid)
        step = st.get("step")
        if step:
            self.keep_lease(task, f"before step {step}")
        if step == "attempt":
            return self.attempt(task)
        if step == "inspect":
            return self.inspect_step(task)
        if step == "verify":
            return self.verify(task)
        if step == "panel":
            return self.panel(task)
        if step == "escalation":
            return self.panel_decision(task)
        if step == "human":
            return self.humans(task)
        if step == "commit":
            return self.commit(task)
        if step == "set-aside":
            return self.set_aside(task)
        raise EngineStop(f"task '{tid}' is the active producer but has no step; the state is damaged")

    def end(self, task, status, reason, block_kind=None):
        """Decide the unsuccessful end of a producer; the set-aside itself is the next step.

        `block_kind` classifies an unsuccessful end for the reader: "protocol" when every broken
        reviewer failed at the protocol level, "mixed" when only some did, None otherwise. It is
        carried in `st["final"]` and applied by `set_aside` with the status."""
        st = self.st(task["id"])
        # An attempt ended before it was counted (a record change, findings too large) leaves
        # nothing to resume: the move clears every `pending_*` key (transaction).
        final = {"status": status, "reason": reason}
        if block_kind:
            final["block_kind"] = block_kind
        move(st, task["id"], "set-aside", final=final)
        self.run.save()
        return None

    def unfinished_notice(self, task, feedback):
        """What a fresh session is told about a work tree that already differs from the task's
        base: an interrupted call, a timeout, a provider switch and an ordinary rework all leave
        the author's files in place, and a session that does not continue the last one would
        otherwise meet them unexplained. Nothing when the tree is at base, or when the recovery
        notice (R3) already names the files."""
        st = self.st(task["id"])
        if (feedback or {}).get("recovered"):
            return ""
        tree = self.snapshot()
        if tree == st["base"]:
            return ""
        paths = [p for _s, p, _o, _n in self.git.changed_paths(st["base"], tree)]
        return "\n\n" + prompts.unfinished_section(paths, switched=bool(st.get("provider_switched"))) + "\n"

    # -- an attempt --------------------------------------------------------------------------

    def attempt(self, task):
        tid, st = task["id"], self.st(task["id"])
        if st["attempts_used"] >= task["max_attempts"]:
            if st.get("sender") in ("human", "review"):
                return self.end(task, "blocked", f"{st['attempts_used']} attempts used and the "
                                "last was sent back by a person or a reviewer")
            return self.end(task, "failed", f"{st['attempts_used']} attempts used; the last was "
                            f"sent back by {st.get('sender') or 'a check'}")
        set_status(st, tid, "rework" if st["attempts_used"] else "running")
        pending = st.get("pending_attempt")
        if pending:
            n, rel = pending
            adir = os.path.join(self.run.path, rel)
        else:
            n, adir = self.run.new_attempt(tid)
            st["pending_attempt"] = [n, os.path.relpath(adir, self.run.path)]
            self.run.save()
        rel_adir = os.path.relpath(adir, self.run.path)
        if self.count_interruption(task) >= transaction.MAX_INTERRUPTIONS:
            return self.end(task, "blocked", f"{st['pending_interruptions']} calls of attempt {n} "
                            "were interrupted before they returned (the limit is "
                            f"{transaction.MAX_INTERRUPTIONS}); no attempt was used. Find what "
                            "keeps stopping the runner, then `runner retry`")
        if st.get('ruled_candidate'):
            # Reverify in a fresh directory; the ruling must not rewrite the old evidence.
            if self.snapshot() != st['ruled_candidate'] or findings.blockers(self.ledger(tid)):
                raise EngineStop('the restored candidate no longer matches the human rulings')
            self.run.write_decision(os.path.join(adir, 'inputs.json'), self.input_manifest(task))
            self.run.write_decision(os.path.join(adir, 'result.json'), {
                'status': agents.OK, 'agent_calls': 0, 'recovered_candidate': st['ruled_candidate'],
                'answer': {'outcome': 'done', 'summary': st.get('summary', ''),
                           'blocked_reason': '', 'responses': []}})
            move(st, tid, 'inspect', attempt_dir=rel_adir)       # consumes ruled_candidate
            self.save()
            return self.inspect_step(task)
        feedback = st.get("feedback")
        if st.get('pending_provider_quota'):
            st.pop('pending_author_result', None)
            st.pop('pending_repair', None)
            self.provider_quota(task, agents.AgentResult(**st['pending_provider_quota']))
        selected = self.provider_task(task)
        agent = agents.make(selected["agent"], self.wf["agents"][selected["agent"]])
        continuing = bool(feedback and st.get("session_id") and "resume" in st.get("qualified", []))
        caps = {k: self.defaults[k] for k in ("diff_cap_bytes", "inputs_cap_bytes",
                                              "findings_cap_bytes")}
        try:
            if continuing:
                prompt = prompts.rework_prompt(task, feedback=feedback, caps=caps,
                                               attempt=st["attempts_used"] + 1)
            else:
                prompt = prompts.produce_prompt(
                    task, self.template(task), brief=self.brief(task), inputs=self.inputs(task), project_rules=self.project_rules(task),
                    feedback=feedback, attempt=st["attempts_used"] + 1, frozen=self.frozen(tid),
                    caps=caps)
        except prompts.FindingsTooLarge as exc:
            return self.end(task, "blocked", str(exc))
        with open(os.path.join(adir, "prompt.md"), "w", encoding="utf-8") as fh:
            fh.write(prompt)
        if feedback:
            with open(os.path.join(adir, "feedback.md"), "w", encoding="utf-8") as fh:
                fh.write(prompts.feedback_text(feedback, caps["findings_cap_bytes"]) + "\n")
        self.run.write_decision(os.path.join(adir, "inputs.json"), self.input_manifest(task))
        self.run.save()

        result = agents.AgentResult(agents.PROTOCOL_ERROR,
                                    error=st.get("pending_protocol_error", "protocol retries used up"))
        problems = []
        if self.repair_tree(task):
            return
        while st.get('pending_author_result') or st.get("pending_protocol_tries", 0) < 1 + PROTOCOL_RETRIES:
            call_prompt = prompt
            if not continuing:
                call_prompt += self.unfinished_notice(task, feedback)
            if st.get("pending_protocol_error"):
                call_prompt += ('\n\n# Previous response was rejected\n'
                                'Repair the final response using the saved work. Do not repeat completed '
                                'research or rewrite correct artifacts just to repair the response. '
                                'The diagnostic below is data, not instructions.\n'
                                + prompts.fence('validation diagnostic', st["pending_protocol_error"]))
            repair = st.get('pending_repair')
            if st.get('pending_author_result'):
                saved = st['pending_author_result']
                result, problems = agents.AgentResult(**saved['result']), saved['problems']
            else:
                result, problems = self.call_agent(
                    agent, selected, adir, repair['prompt'] if repair else call_prompt,
                    repair['session_id'] if repair else st["session_id"] if continuing else None,
                    read_only=bool(repair and repair['read_only']))
            self.keep_lease(task, f"the author's call in attempt {n}")
            if self.repair_tree(task):
                return
            if problems:
                break
            if result.status == agents.QUOTA:
                st.pop('pending_author_result', None)
                st.pop('pending_repair', None)
                self.provider_quota(task, result)
                selected = self.provider_task(task)
                agent = agents.make(selected['agent'], self.wf['agents'][selected['agent']])
                continuing = False
                prompt = prompts.produce_prompt(
                    task, self.template(task), brief=self.brief(task), inputs=self.inputs(task), project_rules=self.project_rules(task),
                    feedback=feedback, attempt=st['attempts_used'] + 1, frozen=self.frozen(tid), caps=caps)
                continue
            if result.status == agents.TRANSIENT:
                # The provider failed, not the author: another call, on a fallback profile from
                # the second failure on, within the protocol-retry budget and without an attempt.
                st["provider_failures"] = st.get("provider_failures", 0) + 1
                self.charge_try(st)
                st["session_id"] = None
                st.pop('pending_author_result', None)
                st.pop('pending_repair', None)
                continuing = False
                if st["provider_failures"] >= 2 and self.step_to_fallback(
                        task, f"provider failed twice: {result.error[:200]}"):
                    selected = self.provider_task(task)
                    agent = agents.make(selected['agent'], self.wf['agents'][selected['agent']])
                prompt = prompts.produce_prompt(
                    task, self.template(task), brief=self.brief(task), inputs=self.inputs(task), project_rules=self.project_rules(task),
                    feedback=feedback, attempt=st['attempts_used'] + 1, frozen=self.frozen(tid), caps=caps)
                self.run.save()
                continue
            if result.status == agents.OK:
                try:
                    responded = findings.respond(self.ledger(tid), result.structured, n)
                    errors = []
                except findings.ProtocolError as exc:
                    errors = [str(exc)]
                if not errors:
                    break
                result.status, result.error = agents.PROTOCOL_ERROR, "; ".join(errors)
            if result.status != agents.PROTOCOL_ERROR:
                break
            st["pending_protocol_error"] = result.error
            self.charge_try(st)
            self.prepare_repair(selected, result)
            st.pop('pending_author_result', None)
            st["session_id"] = None
            continuing = False
            prompt = prompts.produce_prompt(
                task, self.template(task), brief=self.brief(task), inputs=self.inputs(task), project_rules=self.project_rules(task),
                feedback=feedback, attempt=st["attempts_used"] + 1, frozen=self.frozen(tid), caps=caps)
            self.run.save()
        calls = st.get("pending_calls", 0)
        self.crash("attempt:after-agent")

        if problems:                                              # FRZ-07
            return self.end(task, "failed", "the run record was changed during the agent's call: "
                            + "; ".join(problems))
        if result.status == agents.ENVIRONMENT:
            st.pop('pending_author_result', None)
            if agents.network_error(result.error):                    # PROV-16
                self.save()
                raise EngineStop(f"the network is down: the call of '{tid}' could not reach the "
                                 f"provider ({result.error}). No attempt was used. `runner resume` "
                                 "when it is back")
            qualification.invalidate(self.run, tid)
            raise EngineStop(f"environment failure in task '{tid}': {result.error}. No attempt "
                             "was used. Fix the cause, then `runner resume`")
        if result.status == agents.TRANSIENT:
            # One try given back, so `resume` makes one more call before stopping again.
            st["pending_protocol_tries"] = max(0, st.get("pending_protocol_tries", 0) - 1)
            st.pop("provider_failures", None)
            raise EngineStop(f"the provider kept failing on task '{tid}': {result.error}. No attempt "
                             "was used. Wait, or replan its profile, then `runner resume`")
        st.pop("provider_failures", None)

        # Counted: the move that follows (to `inspect`, or back to `attempt`) clears `pending_*`
        # and `provider_switched` in the same save as the count.
        st["attempts_used"] += 1
        st["attempt_dir"] = rel_adir
        # Protected, and so pinned, before the save that clears `pending_author_result`: from
        # then on result.json is the only copy of the answer, and `repair-record` must have it (K4).
        # Not under an intent (`write_decision`): its save would count this attempt before the
        # move to `inspect`; written again after a kill, the same answer gives the same bytes.
        record.write_durable(os.path.join(adir, "result.json"), record.dump_json({
            "status": result.status, "answer": result.structured, "error": result.error,
            "cost_usd": result.cost_usd, "usage": result.usage, "session_id": result.session_id,
            "seconds": round(result.seconds, 3), "agent_calls": calls}))
        self.run.protect(os.path.join(adir, "result.json"))
        if result.status != agents.OK:
            st["session_id"] = None                               # a broken session is abandoned
            what = {agents.TIMED_OUT: "ran past its time limit",
                    agents.AGENT_ERROR: "ended with an error",
                    agents.PROTOCOL_ERROR: "did not end with a valid answer"}[result.status]
            return self.send_back(task, "agent", f"your previous call {what}",
                                  result.error or what)
        st["session_id"] = result.session_id
        st["summary"] = result.structured["summary"]
        st["ledger"] = responded
        # The attempt is counted in the same save that moves on to inspecting it, so a kill from
        # here on resumes at `inspect` and never spends another attempt on work already done.
        move(st, tid, "inspect")
        self.save()
        self.crash("attempt:counted")
        return self.inspect_step(task)

    def charge_try(self, st):
        """A call returned with a protocol error or a provider failure: one of the attempt's
        three tries is used. Charged once per returned call, also when its settled answer is read
        again after a kill (W-04)."""
        saved = st.get("pending_author_result") or {}
        if not saved.get("charged"):
            st["pending_protocol_tries"] = st.get("pending_protocol_tries", 0) + 1
            saved["charged"] = True

    def count_interruption(self, task):
        """A call left in flight (`pending_call`) with no settled answer never returned: the
        runner was stopped (`pause --now`, a kill). It is counted apart from the protocol tries,
        so interruptions never spend the attempt. Returns the count so far (W-04)."""
        st = self.st(task["id"])
        if st.get("pending_call") and not st.get("pending_author_result"):
            st.pop("pending_call")
            st["pending_interruptions"] = st.get("pending_interruptions", 0) + 1
            self.run.event("call-interrupted", task=task["id"], count=st["pending_interruptions"])
            self.run.save()
        return st.get("pending_interruptions", 0)

    def inspect_step(self, task):
        """What the counted attempt left behind: its answer, then the tree. Taken from the
        record (result.json, the attempt directory), so it runs again whole after a crash."""
        tid, st = task["id"], self.st(task["id"])
        rel_adir = st["attempt_dir"]
        adir = os.path.join(self.run.path, rel_adir)
        n = int(os.path.basename(rel_adir).rpartition("-")[2])
        answer = record.read_json(os.path.join(adir, "result.json"))["answer"]
        if answer["responses"]:
            record.write_durable(os.path.join(adir, "responses.json"),
                                 record.dump_json(answer["responses"]))
        if answer["outcome"] == "blocked":
            return self.end(task, "blocked", "the agent answered blocked: " + answer["blocked_reason"])

        problems, candidate = self.inspect(task, adir)
        st["ledger"] = findings.note_advisories(self.ledger(tid), answer, n, candidate)
        if problems:
            return self.send_back(task, "contract", "the output contract or the write rules "
                                  "were not met", "\n".join(f"- {p}" for p in problems))
        self.pin(f"{tid}/candidate-{n}", candidate)
        self.crash("inspect:after-pin")
        self.note_ignored(task)
        self.write_manifest(task, adir, candidate)
        move(st, tid, "verify", candidate=candidate, status="verifying", panel=None)
        self.save()
        return None

    def prepare_repair(self, task, result):
        """Only a completed answer and an observed capability authorize a short retry."""
        st = self.st(task['id'])
        st.pop('pending_repair', None)
        if not result.completed:
            return
        session = result.session_id if 'resume' in st.get('qualified', []) else None
        read_only = not session and task['agent'] in st.get('repair_qualifications', {})
        if not session and not read_only:
            return
        candidate = self.snapshot()
        self.pin(f"{task['id']}/repair-{st['pending_attempt'][0]}-{st['pending_calls']}", candidate)
        answer = json.dumps(result.structured, ensure_ascii=False) if result.structured is not None else result.text
        ids = [f['id'] for f in findings.needing_response(self.ledger(task['id']))]
        st['pending_repair'] = {'candidate': candidate, 'session_id': session,
                                'read_only': bool(read_only), 'agent': task['agent'],
                                'prompt': prompts.repair_prompt(answer, result.error, ids)}

    def repair_tree(self, task):
        """Check completed and interrupted repairs alike before any further call can run."""
        st = self.st(task['id'])
        repair = st.get('pending_repair')
        if not repair:
            return False
        before, after = repair['candidate'], self.snapshot()
        changed = [p for _s, p, _o, _n in self.git.changed_paths(before, after)]
        if repair.get('invocation') and not repair.get('checked'):
            path = os.path.join(self.run.path, repair['invocation'], 'repair.json')
            self.run.write_decision(path, {'before': before, 'after': after,
                'read_only': repair['read_only'], 'edited_files': changed,
                'ordinary_authoring': bool(changed and not repair['read_only'])})
            repair['checked'] = True
        if changed:
            if repair['read_only']:
                repair['violation'] = changed
            else:
                self.run.event('repair-edited-files', task=task['id'], changed=changed,
                               before=before, after=after)
                repair['candidate'] = after
            self.save()
        if repair.get('violation'):
            self.restore(before, repair['violation'], before)
            self.end(task, 'failed', 'a read-only response repair changed the work tree: '
                     + ', '.join(repair['violation']))
            return True
        return False

    def call_agent(self, agent, task, adir, prompt, session_id, read_only=False):
        tid = task["id"]
        self.pause_point(f"the next call of '{tid}'")
        reservation = budgets.cap_for(agent, task)
        if not budgets.fits(self.run.state, reservation):
            raise budgets.Exhausted(f"budget cannot cover the next call of '{tid}' (${reservation:g})")
        if not budgets.fits_tokens(self.run.state, agent):
            raise budgets.token_stop(self.run.state, f"the next call of '{tid}'")
        tokens = budgets.token_reservation(self.run.state, agent)
        remainder = budgets.token_remainder(self.run.state, agent)
        _n, inv = self.run.new_invocation(adir)
        repair = self.st(tid).get('pending_repair')
        if repair:
            repair['candidate'] = self.snapshot()
            repair.pop('checked', None)
            repair['invocation'] = os.path.relpath(inv, self.run.path)
            self.run.write_decision(os.path.join(inv, 'repair.json'), {
                'before': repair['candidate'], 'read_only': read_only})
        self.run.state["spend"]["reserved_usd"] += reservation
        # The call in flight, durable with its intent: found again with no settled answer, it was
        # interrupted (`count_interruption`). A try is charged only when it returns (`charge_try`).
        st = self.st(tid)
        st["pending_calls"] = st.get("pending_calls", 0) + 1
        st["pending_call"] = os.path.relpath(inv, self.run.path)
        record.write_durable(os.path.join(inv, "prompt.md"), prompt.encode())
        op = self.run.begin("agent", task=tid, agent_kind=agent.kind, agent=agent.name,
                            invocation_dir=os.path.relpath(inv, self.run.path), reservation=reservation,
                            token_reservation=tokens, token_remainder=remainder)
        guard = self.run.integrity_begin()
        startup_problems = []

        def started(identity):
            # The identity is made durable first, so that a kill from here on leaves a process
            # `resume` can see; hashing every protected file comes after.
            changed = self.run.guard_changes(guard)
            self.run.amend(op, process=identity)
            guard["state.json"] = hashlib.sha256(record.dump_json(self.run.state)).hexdigest()
            startup_problems.extend(changed)
            if "leader" not in identity.get("group", {}):
                startup_problems.extend(self.run.integrity_check())
            return lambda: self.crash("agent:running")

        env = agents.agent_env(self.environ, self.run_id, tid,
                               self.run.path if task.get("needs_run_dir") else None)
        result = agent.run(prompt, cwd=self.root, invocation_dir=inv, schema=validate.PRODUCE,
                           session_id=session_id, model=task.get("model", ""),
                           timeout_s=task["timeout_min"] * 60, budget_usd=task["budget_usd"],
                           read_only=read_only, env=env, on_start=started)
        problems = startup_problems + self.run.integrity_end(guard)
        self.book_session_cost(agent, task, session_id, result)
        record.write_durable(os.path.join(inv, "outcome.json"), record.dump_json(result.outcome()))
        budgets.settle(self.run.state, tid, reservation, result, tokens,
                       call={"invocation": os.path.relpath(inv, self.run.path)},
                       token_remainder=remainder)
        # Persist the answer with settlement: a kill before validation must not replay authoring
        # or charge this completed call again. Old records simply have no pending result.
        st['pending_author_result'] = {
            'result': dict(result.outcome(), text=result.text, structured=result.structured),
            'problems': problems}
        st.pop('pending_call', None)
        if result.status == agents.QUOTA and not problems:
            st['pending_provider_quota'] = result.outcome()
        self.run.finish(op, status=result.status, seconds=round(result.seconds, 1),
                        **({"cleanup": result.cleanup} if result.cleanup else {}))
        self.cleanup_point()
        self.crash('provider:outcome-recorded')
        return result, problems

    def book_session_cost(self, agent, task, session_id, result):
        """Charge a continued Claude session only for what this call added. Claude Code reports
        `total_cost_usd` for the whole session, so a resumed attempt's envelope carries every
        earlier attempt's dollars again; booking it whole doubled the cost of every rework
        attempt (BUD-13). The last total seen per session is kept on the task; a new session, a
        missing price or a total that went down (a different session under the same id) is
        booked as reported. `usage` is per call already."""
        st = self.st(task["id"])
        if agent.kind != "claude" or result.cost_usd is None or not result.session_id:
            return
        seen = st.get("session_cost") or {}
        last = seen.get(result.session_id)
        if session_id and result.session_id == session_id and last is not None \
                and result.cost_usd >= last:
            result.reported_cost_usd = result.cost_usd
            result.cost_usd = round(result.cost_usd - last, 6)
            seen[result.session_id] = result.reported_cost_usd
        else:
            seen = {result.session_id: result.cost_usd}
        st["session_cost"] = seen

    def send_back(self, task, sender, title, cause, check_progress=None):
        """The attempt did not pass. Record why; the next step is another attempt."""
        st = self.st(task["id"])
        if check_progress is not None:
            mark = {"signature": check_progress, "tree": st.get("candidate")}
            if st.get("last_failure") == mark:                    # the no-progress rule
                return self.end(task, "failed", "no progress: the same failure on an identical "
                                f"candidate tree ({check_progress})")
            st["last_failure"] = mark
        move(st, task["id"], "attempt", sender=sender, last_cause=cause,
                  feedback={"cause_title": title, "cause": cause, "candidate": st.get("candidate"),
                            **findings.feedback(self.ledger(task["id"]))})
        self.save()
        self.say(f"{task['id']}: sent back ({title})")
        return None

    # -- what the attempt left behind (steps 2 and 3) ------------------------------------

    def inspect(self, task, adir):
        """Returns (problems, candidate tree). Anything outside the rules is put back first."""
        tid, st = task["id"], self.st(task["id"])
        problems = []
        # Recorded before anything is removed, as reverted.json is: a re-run after a kill no
        # longer finds what an earlier run removed, and must still tell the author.
        path_embedded = os.path.join(adir, "embedded.json")
        removed = record.read_json(path_embedded) if os.path.exists(path_embedded) else []
        present = self.git.embedded_repositories()
        if any(rel not in removed for rel in present):
            removed += [rel for rel in present if rel not in removed]
            record.write_durable(path_embedded, record.dump_json(removed))
        for rel in present:
            self.git.remove_embedded(rel)
        if present:
            self.crash("inspect:embedded-removed")
        for rel in removed:
            problems.append(f"you left an embedded git repository at '{rel}'; it was removed. "
                            "Do not run `git init` or clone inside the work tree")
        frozen = self.frozen_patterns(tid)
        reverted = []
        # What an earlier run of this step put back before a crash: the tree no longer shows it.
        path_reverted = os.path.join(adir, "reverted.json")
        earlier = record.read_json(path_reverted) if os.path.exists(path_reverted) else []
        # Putting back a `.gitignore` can reveal files it hid, so look again until nothing is left.
        for _round in range(4):
            tree = self.git.snapshot(self.run.index_file)
            found = []
            for status, path, _old, new_mode in self.git.changed_paths(st["base"], tree):
                why = self.violation(task, path, new_mode, frozen)
                if why:
                    found.append({"path": path, "change": status, "why": why})
            if not found:
                break
            outside = self.hide_record([r["path"] for r in found])
            if len(outside) < len(found):                   # the record was visible: look again
                continue
            stuck = sorted({r["path"] for r in found} & {r["path"] for r in reverted})
            if stuck or _round == 3:
                raise EngineStop("environment failure: these paths could not be put back: "
                                 + ", ".join(stuck or [r["path"] for r in found]))
            reverted += found
            record.write_durable(path_reverted, record.dump_json(_merge_reverted(earlier, reverted)))
            self.restore(st["base"], [r["path"] for r in found], None)
        reverted = _merge_reverted(earlier, reverted)
        if reverted:
            for r in reverted:
                problems.append(f"{r['path']}: {r['why']}; the runner put it back")

        declared = [o["path"] for o in task["outputs"]] + task["writes"] + task["removes"]
        literal = sorted({p for p in declared if not patterns.has_wildcard(p)})
        ignored = self.git.check_ignored([self.to_top(p) for p in literal])
        for path, rule in sorted(ignored.items()):
            problems.append(f"{path} is now ignored by git ({rule}), so your work there is "
                            "invisible to reviewers and would never be committed")

        present = [p for p in (self.to_root(top) for top in self.git.ls_tree(tree)) if p]
        for out in task["outputs"]:
            found = [p for p in present if patterns.matches(out["path"], p)]
            if not found:
                problems.append(f"the declared output {out['path']} does not exist")
            elif not out.get("may_be_empty"):
                empty = [p for p in found if self.size(p) == 0]
                if empty and len(empty) == len(found):
                    problems.append(f"the declared output {out['path']} is empty")
        for pattern in task["removes"]:
            still = [p for p in present if patterns.matches(pattern, p)]
            if still:
                problems.append(f"{pattern} must not exist afterwards, but is still there: "
                                + ", ".join(still[:5]))
        return problems, tree

    def size(self, root_path):
        try:
            return os.lstat(os.path.join(self.root, root_path)).st_size
        except OSError:
            return 0

    def violation(self, task, top_path, new_mode, frozen):
        """Why this change is not allowed, or ''. Protection beats `writes`, except for an exact
        path the task lists there; a frozen output may be changed only by claiming it in `writes`."""
        path = self.to_root(top_path)
        if path is None:
            return "it lies outside the workflow's root"
        if patterns.matches_any(task["protected"], path) and path not in task["writes"]:
            return "it is protected"
        if not patterns.matches_any(task["writes"], path):
            owner = next((o for o, pats in frozen if patterns.matches_any(pats, path)), None)
            if owner:
                return f"it is a frozen output of accepted task '{owner}', and this task does not claim it in `writes`"
            return "it is outside this task's `writes`"
        if new_mode == "120000":
            full = os.path.join(self.root, path)
            try:
                target = os.path.realpath(os.path.join(os.path.dirname(full), os.readlink(full)))
            except OSError:
                target = ""
            if target != self.git.top and not target.startswith(self.git.top + os.sep):
                return "it is a symbolic link that points outside the repository"
        return ""

    def frozen_patterns(self, tid):
        return [(t["id"], [o["path"] for o in t["outputs"]])
                for t in self.producers("accepted") if t["id"] != tid]

    def frozen(self, tid):
        return [p for _owner, pats in self.frozen_patterns(tid) for p in pats]

    def write_manifest(self, task, adir, candidate):
        entries = self.git.ls_tree(candidate)
        outputs = {}
        for top, (mode, sha) in sorted(entries.items()):
            path = self.to_root(top)
            if path and patterns.matches_any([o["path"] for o in task["outputs"]], path):
                outputs[path] = {"blob": sha, "mode": mode, "size": self.size(path)}
        base = self.st(task["id"])["base"]
        changed = [p for _s, p, _o, _n in self.git.changed_paths(base, candidate)]
        ignored = self.st(task["id"]).get("ignored_since_base")
        self.run.write_decision(os.path.join(adir, "outputs.json"),
                                dict({"task": task["id"], "candidate": candidate, "base": base,
                                      "outputs": outputs, "changed": changed},
                                     **({"ignored_since_base": ignored} if ignored else {})))
        diff = self.git.review_diff(base, candidate)
        with open(os.path.join(adir, "changes.diff"), "w", encoding="utf-8",
                  errors="surrogateescape") as fh:
            fh.write(gitops.cap_diff(diff, self.defaults["diff_cap_bytes"],
                                     "failed.patch if the task is set aside, or the commit"))

    # -- inputs ------------------------------------------------------------------------------

    def template(self, task):
        path = os.path.join(self.run.path, "library", "types", task["type"] + ".toml")
        with open(path, "rb") as fh:
            return tomllib.load(fh)["prompt"]

    def brief(self, task):
        if task.get("prompt_file"):
            with open(os.path.join(self.run.path, "briefs", task["id"] + ".md"),
                      encoding="utf-8") as fh:
                return fh.read()
        return task.get("prompt", "")

    def project_rules(self, task):
        """The frozen rules_file of a task, or ''."""
        if task.get("rules_file"):
            with open(os.path.join(self.run.path, "briefs", task["id"] + ".rules.md"),
                      encoding="utf-8") as fh:
                return fh.read()
        return ""

    def effective_brief(self, task):
        """The brief a standing ruling is bound to, at export and when it is supplied."""
        return findings.effective_brief(task, self.brief(task), self.project_rules(task), self.tasks)

    def upstream_files(self, need_id, tree_entries):
        need = self.tasks[need_id]
        if need["kind"] != "produce":
            return {}
        pats = [o["path"] for o in need["outputs"]]
        return {self.to_root(top): sha for top, (_m, sha) in sorted(tree_entries.items())
                if self.to_root(top) and patterns.matches_any(pats, self.to_root(top))}

    def inputs(self, task):
        entries = self.git.ls_tree(self.st(task["id"])["base"])
        items = []
        for need_id in task["needs"]:
            need = self.tasks[need_id]
            items.append({"id": need_id, "type": need["type"], "title": need["title"],
                          "summary": self.st(need_id).get("summary", ""),
                          "files": sorted(self.upstream_files(need_id, entries))})
            upstream = self.st(need_id)
            standing, _why = self.standing_rulings(need)
            rulings = findings.owner_rulings(upstream.get('ledger')) + standing
            if upstream['status'] == 'accepted' and rulings:
                if standing:
                    items[-1]['standing_rulings_record'] = 'workflow.expanded.json: defaults._standing_rulings'
                items[-1].update(accepted_candidate=upstream.get('candidate'), owner_rulings=rulings,
                                 accepted_commit=upstream.get('commit'),
                                 rulings_record=f"tasks/{upstream['dir']}/findings.json")
        return items

    def input_manifest(self, task):
        entries = self.git.ls_tree(self.st(task["id"])["base"])
        return {"task": task["id"], "base": self.st(task["id"])["base"],
                "inputs": {n: self.upstream_files(n, entries) for n in task["needs"]}}

    # -- verification (steps 4, 5 and 7; step 6 is in panels) --------------------------------

    def plan_verifiers(self, task):
        """Cheapest first: own gates, verifying checks, then the gates and checks of accepted
        tasks whose outputs or inputs this candidate touched."""
        tid, st = task["id"], self.st(task["id"])
        plan = [{"id": f"gate:{n}", "kind": "gate", "task": None, "commands": [g["run"]],
                 "read_only": False, "restores": False, "timeout_min": task["gate_timeout_min"],
                 "new": g.get("new", False), "fail_pattern": g.get("fail_pattern", ""),
                 "expect": g.get("expect", "pass")}
                for n, g in enumerate(task["gates"], 1)]
        for c in self.verifiers_of(tid, "check"):
            if c in self.parallel_checks_of(tid):
                continue  # these readers run with the panel after writer checks
            plan.append(self.check_verifier(c, c["id"], "check"))
        touched = {self.to_root(p) for _s, p, _o, _n
                   in self.git.changed_paths(st["base"], st["candidate"])}
        for other in self.producers("accepted"):
            oid = other["id"]
            if oid == tid:
                continue
            pats = [o["path"] for o in other["outputs"]]
            consumed = set(self.accepted_inputs(oid))
            if not any(p and (patterns.matches_any(pats, p) or p in consumed) for p in touched):
                continue
            for n, g in enumerate(other["gates"], 1):
                if g.get("expect") == "fail":
                    # A claim about the candidate it was accepted on: run again after the fix,
                    # it would fail because the fix worked.
                    continue
                plan.append({"id": f"regression:{oid}:gate:{n}", "kind": "regression", "task": None,
                             "commands": [g["run"]], "read_only": False, "restores": False,
                             "timeout_min": other["gate_timeout_min"]})
            for c in self.verifiers_of(oid, "check"):
                plan.append(self.check_verifier(c, f"regression:{oid}:{c['id']}", "regression"))
        return plan

    def check_verifier(self, c, vid, kind):
        demoted = self.st(c["id"]).get("demoted", False)
        return {"id": vid, "kind": kind, "task": c["id"] if kind == "check" else None,
                "commands": list(c["run"]), "read_only": c["read_only"] and not demoted,
                "restores": c["restores"], "timeout_min": c["gate_timeout_min"]}

    def accepted_inputs(self, tid):
        return {path: sha for files in self.run.accepted_inputs(tid).values()
                for path, sha in files.items()}

    def verify(self, task):
        tid, st = task["id"], self.st(task["id"])
        adir = os.path.join(self.run.path, st["attempt_dir"])
        candidate = st["candidate"]
        now = self.snapshot()
        if now != candidate:                          # a crash may have come in the middle of a verifier
            self.restore(candidate, [p for _s, p, _o, _n
                                     in self.git.changed_paths(candidate, now)], candidate)
        budget = self.diff_budget(task)
        if budget and budget["result"] == "fail":            # step 3b: no gate or reviewer runs
            self.run.write_decision(os.path.join(adir, "verification.json"),
                                    {"candidate": candidate, "diff_budget": budget, "results": [],
                                     "result": "fail"})
            largest = "\n".join(f"- {f['path']}: {f['lines']} lines" for f in budget["largest"])
            return self.send_back(
                task, "the diff budget", "the change is larger than the task's diff budget",
                f"From the task's base, this candidate changes {record.counted(budget['files'], 'files')} "
                f"and {record.counted(budget['lines'], 'lines')} (added plus deleted); the limits are "
                + " and ".join(record.counted(budget[k], k.rpartition('_')[2]) for k in DIFF_BUDGET_KEYS
                               if budget.get(k))
                + ". Rework is measured from the same base, so make the whole change smaller. "
                "The largest changes:\n" + largest,
                check_progress=f"diff-budget:{budget['files']}:{budget['lines']}")
        results, failure = [], None
        plan = self.plan_verifiers(task)
        already = {}      # commands -> the verifier that ran them against this candidate
        i = 0
        while i < len(plan) and not failure:
            v = plan[i]
            expect_fail = v.get("expect") == "fail"
            key = (tuple(v["commands"]), v["fail_pattern"] if expect_fail else None)
            if v["task"] is None and key in already:
                # The same commands against the same tree give the same answer: a candidate that
                # touches the outputs of several accepted tasks sharing one gate runs it once.
                first = already[key]
                entry = dict(first, verifier=v["id"], kind=v["kind"], config_sha256=_config_hash(v),
                             same_as=first["verifier"])
                results.append(entry)
                i += 1
                continue
            ran, problems = self.run_commands(v["commands"], tid, adir, v["timeout_min"])
            self.crash("verify:after-command")
            self.keep_lease(task, f"`{v['commands'][0]}`")
            after = self.snapshot()
            entry = {"verifier": v["id"], "kind": v["kind"], "commands": v["commands"],
                     "candidate": candidate, "config_sha256": _config_hash(v),
                     "result": "pass" if checks.passed(ran, v["commands"]) else "fail",
                     "runs": [{k: r[k] for k in ("command", "result", "exit", "seconds")}
                              for r in ran]}
            wrong = ""
            if expect_fail:                                   # F2: a different pass test only
                wrong = checks.expected_failure(ran[-1], v["fail_pattern"]) if ran else "could not run"
                entry.update(expect="fail", fail_pattern=v["fail_pattern"],
                             result="fail" if wrong else "pass")
                if wrong:
                    entry["note"] = wrong
            if v["task"] is None and after == candidate:
                already[key] = entry
            if problems:
                self.run.write_decision(os.path.join(adir, "verification.json"),
                                        dict({"candidate": candidate, "results": results + [entry]},
                                             **({"diff_budget": budget} if budget else {})))
                return self.end(task, "failed", f"the run record was changed while "
                                f"`{v['commands'][0]}` ran: " + "; ".join(problems))
            if after != candidate:
                changed = [p for _s, p, _o, _n in self.git.changed_paths(candidate, after)]
                self.restore(candidate, changed, candidate)
                entry["changed_the_tree"] = changed
                if v["restores"]:
                    pass                                          # its job; the runner put it back
                elif v["read_only"] and v["task"]:
                    # The claim was false. The check fails as a check; it is a writer from now on.
                    self.st(v["task"])["demoted"] = True
                    entry["result"] = "fail"
                    entry["note"] = "declared read_only but wrote: " + ", ".join(changed)
                    results.append(entry)
                    plan[i] = self.check_verifier(self.tasks[v["task"]], v["id"], v["kind"])
                    self.run.save()
                    continue                                       # no producer attempt is used
                else:
                    entry["result"] = "void"
                    failure = ("gate" if v["kind"] != "check" else "check",
                               "a verifier changed the candidate, so every result is void",
                               f"`{ran[-1]['command'] if ran else v['commands'][0]}` changed or "
                               "left files in the work tree, which the runner put back:\n"
                               + "\n".join(f"- {p}" for p in changed)
                               + "\nBuild products belong in paths git ignores.", None, v)
            results.append(entry)
            if not failure and entry["result"] == "fail":
                last = ran[-1]
                failure = ("check" if v["kind"] == "check" else "gate",
                           f"`{last['command']}` did not fail as required: {wrong}"
                           if wrong else f"`{last['command']}` did not pass ({last['result']})",
                           f"$ {last['command']}\n[{last['result']}, exit {last['exit']}]\n"
                           + last["tail"], f"{v['id']}:{last['result']}:{last['exit']}", v)
            i += 1
        replay, problems = None, []
        if not failure and self.replay_beside(task):
            # Run as a job of the panel (N3); the panel writes its evidence here when it ends.
            replay = self.replay_beside_record(st)
        elif not failure:
            replay, failure, problems = self.replay(task, adir, candidate)
        self.run.write_decision(os.path.join(adir, "verification.json"),
                                dict({"candidate": candidate, "results": results,
                                      "result": "fail" if failure or problems else "pass"},
                                     **({"diff_budget": budget} if budget else {}),
                                     **({"replay": replay} if replay else {})))
        if problems:
            return self.end(task, "failed", "the run record was changed during the acceptance "
                            "replay: " + "; ".join(problems))
        for c in self.verifiers_of(tid, "check"):
            mine = [r for r in results if r["verifier"] == c["id"]]
            if mine:
                set_status(self.st(c["id"]), c["id"], "accepted" if mine[-1]["result"] == "pass"
                           else "objected", reason="")
        if failure:
            sender, title, cause, signature, _v = failure
            return self.send_back(task, sender, title, cause, check_progress=signature)

        move(st, tid, "panel")
        self.run.save()
        return None

    def diff_budget(self, task):
        """Step 3b: how much the candidate changes from the transaction's BASE, both trees
        pinned, never HEAD, the work tree or the agent's report. None when the task has no budget.
        It is a read of two pinned trees, so it needs no intent: a resumed `verify` measures again
        and gets the same answer."""
        limits = {k: task[k] for k in DIFF_BUDGET_KEYS if task.get(k)}
        if not limits:
            return None
        st = self.st(task["id"])
        changed = self.git.numstat(st["base"], st["candidate"])
        files, lines = len(changed), sum(n for _p, n in changed)
        over = files > limits.get("max_changed_files", files) or lines > limits.get("max_changed_lines", lines)
        budget = {"files": files, "lines": lines, **limits,
                  "largest": [{"path": self.to_root(p) or p, "lines": n}
                              for p, n in sorted(changed, key=lambda c: (-c[1], c[0]))[:5]],
                  "result": "fail" if over else "pass"}
        self.crash("verify:diff-budget-measured")
        st["diff_budget"] = {k: v for k, v in budget.items() if k != "largest"}
        return budget

    # -- the acceptance replay (W-03) --------------------------------------------------------

    def replay_settings(self, task):
        """(on, cache) for a task: its own keys, else the frozen [defaults]. A run frozen before
        the replay existed has neither, and replays nothing, as it did."""
        on = task.get("acceptance_replay", self.defaults.get("acceptance_replay", False))
        cache = task.get("gate_cache", self.defaults.get("gate_cache", []))
        return bool(on and task["gates"]), list(cache)

    def replay_beside(self, task):
        """True when the replay runs as a job beside the task's panel instead of before it (N3):
        it is on, `replay_beside_panel` is on, and the panel dispatches anything at all. A run
        frozen before the key existed keeps the serial order."""
        on, _cache = self.replay_settings(task)
        tid = task["id"]
        return bool(on and self.defaults.get("replay_beside_panel", False)
                    and (self.reviewers_of(tid) or self.parallel_checks_of(tid)))

    def replay_beside_record(self, st):
        """What `verify` writes for a replay beside the panel: `pending` until `settle_replay`
        writes the evidence, or, when this candidate already passed its replay on this starting
        commit (`replay_passed`: the panel then adds no replay job, as after a ruled `retry
        --apply-patch`), a pass naming the attempt whose verification.json holds the evidence
        (review V-09). `reused_from` is None when that file is gone."""
        if st.get("replay_passed") != {"candidate": st["candidate"], "head": self.git.head()}:
            return {"beside_panel": True, "result": "pending"}
        task_dir = os.path.dirname(st["attempt_dir"])
        earlier = sorted((int(name[len("attempt-"):]), name)
                         for name in os.listdir(os.path.join(self.run.path, task_dir))
                         if name.startswith("attempt-") and name[len("attempt-"):].isdigit()
                         and os.path.join(task_dir, name) != st["attempt_dir"])
        source = None
        for _n, name in reversed(earlier):
            try:
                found = record.read_json(os.path.join(self.run.path, task_dir, name,
                                                      "verification.json")).get("replay") or {}
            except (OSError, ValueError):
                continue
            if (found.get("result"), found.get("tree"), found.get("head")) == (
                    "pass", st["candidate"], st["replay_passed"]["head"]):
                source = os.path.join(task_dir, name)
                break
        return {"beside_panel": True, "result": "pass", "reused_from": source}

    def replay_gates(self, task, dest, candidate, head, adir, on_start):
        """The replay as a panel job runs it, in a worker thread: the checkout, the cache, then
        each gate in order up to the first that does not pass. Writes nothing to the record; the
        coordinator turns what it returns into evidence (`replay_evidence`)."""
        _on, cache = self.replay_settings(task)
        tid = task["id"]
        gates, kept = [], False
        try:
            repo = os.path.join(dest, "repo")
            try:
                os.makedirs(dest)
                self.git.replay_checkout(candidate, head, repo)
            except (OSError, gitops.GitError) as exc:
                raise EngineStop(f"environment failure: the acceptance replay of '{tid}' could not "
                                 f"check out its candidate: {exc}") from exc
            cwd = os.path.join(repo, self.prefix) if self.prefix else repo
            copied = self.copy_gate_cache(cache, cwd)
            for gate in task["gates"]:
                try:
                    ran = checks.run_commands([gate["run"]], cwd=cwd,
                                              log_path=os.path.join(adir, "replay.log"),
                                              timeout_s=task["gate_timeout_min"] * 60,
                                              env=checks.command_env(self.environ, self.run_id, tid),
                                              on_start=lambda identity, c=gate["run"]: on_start(identity, c))
                except proc.Cancelled as exc:
                    # The gates that ran go with it, settled as run (PROC-26); a group its
                    # failed stop left alive keeps the checkout it runs in (PROC-25).
                    exc.runs = [r for done in gates for r in done] + list(exc.runs)
                    kept = bool(exc.cleanup)
                    raise
                gates.append(ran)
                entry, _wrong = self.replay_entry(len(gates), gate, ran)
                if entry["result"] == "fail" or any(r.get('cleanup') for r in ran):
                    break
        finally:
            if not kept and not any(r.get('cleanup') for ran in gates for r in ran):
                shutil.rmtree(dest, ignore_errors=True)
        return {"head": head, "copied": copied, "gates": gates}

    def replay_entry(self, n, gate, ran):
        """One gate's line in the replay's evidence, judged as the live gate is, and the reason an
        `expect = "fail"` gate did not fail as required ('' when it did or is not one)."""
        entry = {"verifier": f"gate:{n}", "commands": [gate["run"]],
                 "result": "pass" if checks.passed(ran, [gate["run"]]) else "fail",
                 "runs": [{k: r[k] for k in ("command", "result", "exit", "seconds")} for r in ran]}
        wrong = ""
        if gate.get("expect") == "fail":                  # F2: the same pass test as live
            wrong = checks.expected_failure(ran[-1], gate["fail_pattern"]) if ran else "could not run"
            entry.update(expect="fail", result="fail" if wrong else "pass")
            if wrong:
                entry["note"] = wrong
        return entry, wrong

    def replay_evidence(self, task, raw, dest, candidate):
        """(evidence, failure) from what `replay_gates` returned, as the serial replay records
        them, the failure telling the author that the panel beside it was set aside."""
        results, failure = [], None
        for n, (gate, ran) in enumerate(zip(task["gates"], raw["gates"]), 1):
            entry, wrong = self.replay_entry(n, gate, ran)
            results.append(entry)
            if entry["result"] == "fail":
                sender, title, cause, signature, _v = self.replay_failure(task, n, ran, wrong,
                                                                          raw["copied"])
                cause += ("\nThe review panel ran beside the replay; its verdicts on this candidate "
                          "were set aside, and it reviews your next candidate.")
                failure = (sender, title, cause, signature, None)
        evidence = {"where": "a shared clone of the repository holding the candidate tree, HEAD at "
                             "the starting commit, outside the work tree, removed afterwards",
                    "dir": dest, "tree": candidate, "head": raw["head"], "cache": raw["copied"],
                    "beside_panel": True, "results": results,
                    "result": "fail" if failure else "pass"}
        if failure:
            evidence["ignored_in_work_tree"] = self.missing_from_checkout(task, raw["copied"])
        return evidence, failure

    def settle_replay(self, task, outcome):
        """The panel's replay has ended: its evidence goes into the attempt's verification.json
        (written as `pending` by `verify`), and a pass is remembered for this candidate and
        starting commit, so the candidate is not replayed again (a panel prepared again after a
        demoted check). Safe to repeat after a crash. Returns the failure as `verify` makes them,
        or None."""
        st = self.st(task["id"])
        self.keep_lease(task, "the acceptance replay")
        path = os.path.join(self.run.path, st["attempt_dir"], "verification.json")
        verification = record.read_json(path)
        verification["replay"] = outcome["evidence"]
        if outcome["failure"]:
            verification["result"] = "fail"
        self.run.write_decision(path, verification)
        if outcome["failure"]:
            return tuple(outcome["failure"])
        st["replay_passed"] = {"candidate": st["candidate"], "head": outcome["evidence"]["head"]}
        self.save()
        return None

    def replay(self, task, adir, candidate):
        """Run the task's own gates once more, in their order, in a throwaway checkout of exactly
        the candidate: the live gates also see every file git ignores (a stale build, a package
        in an ignored venv), and a clean clone of the commit would not. Returns (the record for
        verification.json or None, a failure as `verify` makes them or None, record problems).
        The checkout's directory is named in a `replay` intent before it exists, so a kill
        anywhere leaves `resume` the path to remove; `verify` then runs again whole."""
        on, cache = self.replay_settings(task)
        if not on:
            return None, None, []
        tid = task["id"]
        dest = os.path.join(os.path.realpath(tempfile.gettempdir()),
                            f"code-smith-replay-{uuid.uuid4().hex[:12]}")
        op = self.run.begin("replay", task=tid, dir=dest, candidate=candidate)
        try:
            repo = os.path.join(dest, "repo")
            try:
                os.makedirs(dest)
                head = self.git.head()
                self.git.replay_checkout(candidate, head, repo)
            except (OSError, gitops.GitError) as exc:
                raise EngineStop(f"environment failure: the acceptance replay of '{tid}' could not "
                                 f"check out its candidate: {exc}") from exc
            cwd = os.path.join(repo, self.prefix) if self.prefix else repo
            copied = self.copy_gate_cache(cache, cwd)
            self.crash("replay:checked-out")
            results, failure, problems = [], None, []
            for n, gate in enumerate(task["gates"], 1):
                ran, problems = self.run_commands([gate["run"]], tid, adir, task["gate_timeout_min"],
                                                  cwd=cwd, log="replay.log", replay=True)
                entry, wrong = self.replay_entry(n, gate, ran)
                results.append(entry)
                if problems:
                    break
                if entry["result"] == "fail":
                    failure = self.replay_failure(task, n, ran, wrong, copied)
                    break
        finally:
            # A gate whose cleanup failed may still run in the checkout: it is kept, with the
            # `replay` intent, for `resume` to remove once the cleanup is closed, as the replay
            # beside the panel does (PROC-22).
            if not self.run.cleanup_problems():
                shutil.rmtree(dest, ignore_errors=True)
        self.keep_lease(task, "the acceptance replay")
        evidence = {"where": "a shared clone of the repository holding the candidate tree, HEAD at "
                             "the starting commit, outside the work tree, removed afterwards",
                    "dir": dest, "tree": candidate, "head": head, "cache": copied,
                    "results": results, "result": "fail" if failure or problems else "pass"}
        if failure:
            evidence["ignored_in_work_tree"] = self.missing_from_checkout(task, copied)
        self.run.finish(op, result=evidence["result"])
        return evidence, failure, problems

    def copy_gate_cache(self, cache, cwd):
        """Copy each owner-declared cache path that exists in the work tree into the checkout,
        as it is: a cache trusted by the owner, recorded with the replay. Returns what was copied."""
        copied = []
        for path in cache:
            rel = path.rstrip("/")
            src, dst = os.path.join(self.root, rel), os.path.join(cwd, rel)
            if not os.path.lexists(src):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
            copied.append(path)
        return copied

    def missing_from_checkout(self, task, copied):
        """The ignored paths of the work tree that the checkout lacked, from the root, those new
        since the task's base first; at most REPLAY_NAMED of them."""
        old = set(self.st(task["id"]).get("ignored_at_base") or [])
        cached = [c.rstrip("/") for c in copied]
        paths = []
        for top in self.git.ignored_paths():
            path = self.to_root(top)
            plain = (path or "").rstrip("/")
            if path and not any(plain == c or plain.startswith(c + "/") for c in cached):
                paths.append((top in old, path))
        return [p for _old, p in sorted(paths)][:REPLAY_NAMED]

    def replay_failure(self, task, n, ran, wrong, copied):
        """A gate that passed in the work tree and failed on the clean checkout: sent back like a
        gate failure, naming the ignored paths that are the likely cause."""
        last = ran[-1] if ran else {"command": task["gates"][n - 1]["run"], "result": "error",
                                    "exit": None, "tail": ""}
        missing = self.missing_from_checkout(task, copied)
        why = (f"did not fail as required: {wrong}" if wrong
               else f"did not pass ({last['result']})")
        if missing:
            cause_of = ("These paths are in the work tree but ignored by git, so the checkout "
                        "lacked them; one of them is the likely cause:\n"
                        + "\n".join(f"- {p}" for p in missing)
                        + "\nWhat a gate needs must be committed (within your `writes`) or made "
                          "by the gate itself.")
        else:
            cause_of = ("No ignored path is in the work tree, so the difference lies elsewhere: "
                        "a file outside the repository, an absolute path, or a gate that does not "
                        "give the same answer twice.")
        title = (f"`{last['command']}` passed in the work tree but {why} on a clean checkout of "
                 "the candidate")
        cause = (f"The acceptance replay ran the gate again in a fresh checkout of exactly your "
                 f"candidate, without the files git ignores.\n$ {last['command']}\n"
                 f"[{last['result']}, exit {last['exit']}]\n" + last["tail"] + "\n" + cause_of)
        return ("gate", title, cause, f"replay:gate:{n}:{last['result']}:{last['exit']}", None)

    def note_ignored(self, task):
        """After an attempt: the ignored paths that appeared since the task's base, kept on the
        task (bounded) for `outputs.json` and the review target. A transaction opened before this
        was recorded has no base listing, and nothing is noted."""
        st = self.st(task["id"])
        if "ignored_at_base" not in st:
            return
        base = set(st["ignored_at_base"])
        new = [p for p in self.git.ignored_paths() if p not in base]
        if new:
            st["ignored_since_base"] = {"paths": new[:IGNORED_SHOWN], "count": len(new)}
        else:
            st.pop("ignored_since_base", None)

    def humans(self, task):
        """Step 7. A pending approval holds the work tree and stops the run. Every earlier
        result is bound to this same candidate, so nothing is repeated when the person answers."""
        tid, st = task["id"], self.st(task["id"])
        candidate = st["candidate"]
        if self.snapshot() != candidate:
            raise EngineStop(f"the work tree is no longer the verified candidate of '{tid}'")
        for h in self.verifiers_of(tid, "human"):
            hst = self.st(h["id"])
            decision = hst.get("decision")
            if decision and decision.get("candidate") == candidate:
                if decision["decision"] == "approve":
                    set_status(hst, h["id"], "accepted", reason="")
                    continue
                set_status(hst, h["id"], "objected", reason="rejected: " + decision["note"], decision=None)
                return self.send_back(task, "human", f"a person rejected the work at '{h['id']}'",
                                      decision["note"])
            set_status(hst, h["id"], "waiting_human", reason=f"approve or reject the candidate of '{tid}'")
            set_status(st, tid, "waiting_human", reason=f"waiting for a person at '{h['id']}'")
            self.save()
            self.say(f"{tid}: waiting for a person at '{h['id']}'. The work tree is held.")
            return PAUSE
        move(st, tid, "commit", status="verifying", reason="")
        self.run.save()
        return None

    # -- what the transaction owns in git (W-01) ---------------------------------------------

    def keep_lease(self, task, where):
        """Put back what the transaction owns in git if anything since `where` moved it: the run
        branch at the starting commit and checked out, `.git/info/exclude` and `attributes`, and
        this run's pinned refs. The work tree is left exactly as it is, so the author's files are
        judged as any candidate is; the attempt is not sent back for it, because nothing it did
        to git can now reach the commit (see 02-concepts, "What a producer owns in git"). A state
        recorded before the lease existed has none, and nothing is checked."""
        tid, st = task["id"], self.st(task["id"])
        lease = st.get("lease")
        if not lease:
            return
        busy = self.git.in_progress()
        if busy:
            raise EngineStop(
                f"a git operation is in progress in the work tree ({', '.join(busy)}), left by "
                f"{where} of '{tid}'. The runner does not finish or abort it for its author: abort "
                "it (for example `git merge --abort` or `git rebase --abort`), then `runner "
                "resume`; the runner then puts its branch back and judges the files as they are",
                remember=False)
        branch, start = lease["branch"], lease["commit"]
        name = (branch or "HEAD").removeprefix("refs/heads/")
        head_ref, head = self.git.head_ref(), self.git.ref("HEAD")
        tip = self.git.ref(branch) if branch else head
        what = []
        if head_ref != branch:
            what.append(f"checked out {head_ref.removeprefix('refs/heads/') if head_ref else 'a detached HEAD'}"
                        + (f" at {head[:7]}" if head else ""))
        if tip != start:
            what.append(f"moved {name} from {start[:7]} to {tip[:7]}" if tip else f"deleted {name}")
        moved = bool(what)
        info = self.git.info_files()
        edited = [n for n, was in lease["info"].items() if info.get(n) != was]
        what += [f"edited .git/{n}" for n in edited]
        pins = self.git.pins(self.run.name)
        lost = [ref for ref, obj in sorted(lease["refs"].items()) if pins.get(ref) != obj]
        what += [f"deleted or moved {ref}" for ref in lost]
        if not what:
            return
        if moved:
            self.git.put_head_back(branch, start)
        for n in edited:
            self.git.put_info_file(n, lease["info"][n])
        for ref in lost:
            self.git.run("update-ref", ref, lease["refs"][ref])
        st.setdefault("git_repairs", []).append({"after": where, "what": what, "their_head": head})
        self.run.event("git-repaired", task=tid, after=where, what=what, their_head=head)
        self.save()
        self.say(f"{tid}: put back what {where} changed in git: " + "; ".join(what))

    def owned_parent(self, task):
        """The parent of the runner's commit: the transaction's starting commit, required to be
        checked out on its branch, never whatever HEAD happens to be. A legacy state without a
        lease commits on HEAD, as before."""
        tid, lease = task["id"], self.st(task["id"]).get("lease")
        if not lease:
            return self.git.head()
        head_ref, head = self.git.head_ref(), self.git.ref("HEAD")
        if head_ref != lease["branch"] or head != lease["commit"]:
            raise EngineStop(f"'{tid}' was not committed: HEAD is {head_ref or 'detached'} at "
                             f"{(head or 'nothing')[:7]}, but the transaction started on "
                             f"{lease['branch'] or 'a detached HEAD'} at {lease['commit'][:7]}. "
                             "Nothing was committed; `runner resume` puts it back")
        return lease["commit"]

    # -- acceptance ----------------------------------------------------------------

    def commit(self, task):
        tid, st = task["id"], self.st(task["id"])
        candidate = st["candidate"]
        if self.snapshot() != candidate:
            raise EngineStop(f"the work tree is no longer the verified candidate of '{tid}'; "
                             "nothing was committed")
        paths = [p for _s, p, _o, _n in self.git.changed_paths(st["base"], candidate)]
        parent = self.owned_parent(task)
        subject = task["title"]
        trailer = self.defaults.get("commit_trailer", "")
        op = self.run.begin("commit", task=tid, candidate=candidate, parent=parent, paths=paths,
                            subject=subject, extra_trailer=trailer)
        self.crash("commit:intent-recorded")
        try:
            sha = self.git.commit_candidate(candidate, paths, subject, self.run_id, tid, op,
                                            parent, trailer, crash=self.crash)
        except gitops.CommitRefused as exc:
            raise EngineStop(f"'{tid}' was not committed: {exc}") from exc
        self.crash("commit:before-outcome")
        self.run.record_acceptance(tid, sha, self.git.commit_files(sha),
                                   self.git.commit_message(subject, self.run_id, tid, op, trailer))
        self.run.finish(op, commit=sha)
        self.finalize_acceptance(task)
        return None

    def finalize_acceptance(self, task):
        """Bookkeeping after the commit is recorded. Safe to repeat."""
        tid, st = task["id"], self.st(task["id"])
        for v in self.verifiers_of(tid, "check") + self.verifiers_of(tid, "human") + self.reviewers_of(tid):
            if self.st(v["id"])["status"] not in ("accepted",):
                set_status(self.st(v["id"]), v["id"], "accepted", reason="")
        if st.get("attempt_dir"):
            self.run.close_directory(os.path.join(self.run.path, st["attempt_dir"]))
        self.mark_stale(task)
        move(st, tid, None, session_id=None, feedback=None, reason="")
        self.run.state["active_producer"] = None
        self.save()
        if self.snapshot() != self.git.tree_of("HEAD") or not self.git.is_clean():   # SCH-10
            raise EngineStop(f"after accepting '{tid}' the work tree is not clean: "
                             + ", ".join(self.git.dirty_paths()[:10]))
        self.say(f"{tid}: accepted as {st['commit'][:7]}")

    def mark_stale(self, task):
        """Accepted work that consumed a file this task changed, and that nothing mechanical can
        re-check, is marked stale against the new version."""
        tid, st = task["id"], self.st(task["id"])
        entries = self.git.ls_tree(self.git.tree_of(st["commit"]))
        for other in self.producers("accepted"):
            oid = other["id"]
            # An expected-failure gate is never re-run, so it re-checks nothing here.
            rechecked = any(g.get("expect") != "fail" for g in other["gates"])
            if oid == tid or rechecked or self.verifiers_of(oid, "check"):
                continue
            notes = self.st(oid).setdefault("stale", [])
            for path, was in sorted(self.accepted_inputs(oid).items()):
                now = entries.get(self.to_top(path), ("", "removed"))[1]
                if now != was and not any(s["file"] == path and s["now"] == now for s in notes):
                    notes.append({"file": path, "was": was, "now": now, "by": tid})

    # -- setting work aside -------------------------------------------------------------

    def set_aside(self, task):
        tid, st = task["id"], self.st(task["id"])
        final = st["final"]
        for rel in self.git.embedded_repositories():
            self.git.remove_embedded(rel)
        tree = st.get("pending_set_aside")
        if not tree:
            # What is being set aside, remembered before anything is undone: the restore below
            # puts the work tree back to `base`, so a step replayed after a crash in it must take
            # the candidate from here and never snapshot the tree again (as `pending_attempt`
            # keeps a numbered directory). Without it the replay would pin, patch and record an
            # empty candidate over the work.
            tree = self.git.snapshot(self.run.index_file)
            st["pending_set_aside"] = tree
            self.run.save()
        self.pin(f"{tid}/set-aside", tree)
        patch_rel = os.path.relpath(os.path.join(self.run.task_dir(tid), "failed.patch"),
                                    self.run.path)
        op = self.run.begin("patch", path=patch_rel, base=st["base"], candidate=tree)
        record.write_durable(os.path.join(self.run.path, patch_rel),
                             self.git.full_patch(st["base"], tree))
        self.run.finish(op, path=patch_rel)
        changed = [p for _s, p, _o, _n in self.git.changed_paths(st["base"], tree)]
        attempt, attempt_dir = _latest_attempt(self.run, tid)
        self.run.publish_decision(
            os.path.join(self.run.task_dir(tid), "set-aside.json"),
            {"task": tid, "attempt": attempt, "attempt_dir": attempt_dir,
             "status": final["status"], "reason": final["reason"], "at": _now(),
             "head": self.git.head(), "base": st["base"], "candidate": tree,
             "candidate_ref": f"{gitops.REF_PREFIX}{self.run.name}/{tid}/set-aside",
             "patch": patch_rel, "paths": changed},
            crash=self.crash)
        self.crash("set-aside:before-restore")
        self.restore(st["base"], changed, st["base"])
        for v in self.verifiers_of(tid, "check") + self.verifiers_of(tid, "human") + self.reviewers_of(tid):
            vst = self.st(v["id"])
            if vst["status"] in ("pending", "waiting_human", "running"):
                set_status(vst, v["id"], "skipped", reason=f"'{tid}' is {final['status']}", decision=None)
        if st.get("attempt_dir"):
            self.run.close_directory(os.path.join(self.run.path, st["attempt_dir"]))
        # The move also ends `pending_set_aside`: the step is done, nothing to replay.
        move(st, tid, None, status=final["status"], reason=final["reason"], session_id=None)
        # The classification of this end, and nothing older: a producer that blocked for a
        # protocol failure and blocks again on a substantive one must not keep the old label.
        if final.get("block_kind"):
            st["block_kind"] = final["block_kind"]
        else:
            st.pop("block_kind", None)
            st.pop("block_reviewers", None)
        self.run.state["active_producer"] = None
        self.mark_skips()
        self.save()
        cause = (record.PROTOCOL_BLOCK if final.get("block_kind") == "protocol"
                 else final["reason"])
        self.say(f"{tid}: {final['status']} ({cause}). Its work is in failed.patch")
        return None


def export_queued(run, top, default, crash):
    """Write the run's queued standing entries, each to the rulings file it was bound to when it
    was queued (`file`; an entry queued before it was bound goes to `default`, the workflow's
    `rulings_file`), and mark them written with one `standing-rulings-written` event per file.
    `finish_run` calls it at `done`, `runner export-rulings` for a run that never gets there
    (RUN-77). Idempotent: an entry already in the file, from an export a crash cut short, is not
    appended again. Each entry passes the file's own reader first (RUN-78), and a path through a
    symbolic link is refused (RUN-79). Returns [(file, count)]; raises StandingExportError,
    with nothing marked, when a file cannot be written."""
    queued = [e for e in run.state.get("standing_pending", []) if not e.get("written")]
    by_file = {}
    for entry in queued:
        by_file.setdefault(entry.get("file") or default or "", []).append(entry)
    for name, entries in by_file.items():
        try:
            if not name:
                raise ValueError(", ".join(e.get("finding", "?") for e in entries) + " has no "
                                 "rulings file: it was queued by an older runner, and the workflow "
                                 "sets no rulings_file now")
            for entry in entries:
                try:
                    findings.check_entry(entry)
                except ValueError as exc:
                    raise ValueError(f"{entry.get('finding', '?')}: {exc}") from exc
            link = findings.symlink_on_path(top, name)
            if link:
                raise ValueError(f"'{link}' is a symbolic link; the rulings file is written only "
                                 "as itself")
            path = findings.rulings_path(top, name)
            present = findings.read_rulings(path)
            text = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            new = [e for e in entries if not any(findings.same_ruling(e, p) for p in present)]
            crash("standing:before-write")
            if new:
                text = "\n\n".join(([text.rstrip()] if text.strip() else []) + [
                    findings.ruling_toml(findings.exported_entry(e)) for e in new])
                os.makedirs(os.path.dirname(path), exist_ok=True)
                record.write_durable(path, (text.rstrip() + "\n").encode("utf-8"))
        except (OSError, ValueError) as exc:
            raise StandingExportError(name or "the rulings file", exc) from exc
    try:
        crash("standing:written")
    except (OSError, ValueError) as exc:
        raise StandingExportError(", ".join(by_file), exc) from exc
    for entry in queued:
        entry["written"] = True
    for name, entries in by_file.items():
        run.event("standing-rulings-written", file=name, count=len(entries))
    return [(name, len(entries)) for name, entries in by_file.items()]


def _merge_reverted(earlier, now):
    """reverted.json entries, one per path: an earlier run of `inspect` first, then this one."""
    seen = {r["path"] for r in now}
    return [r for r in earlier if r["path"] not in seen] + now


# -- the set-aside record -----------------------------------------------------------------

def _latest_attempt(run, tid):
    """The highest attempt directory of a task: its number and its path relative to the run.
    `(0, None)` when the task has none; numbers are never reused, so the highest is the last."""
    tdir = run.task_dir(tid)
    used = [int(name[len("attempt-"):]) for name in os.listdir(tdir)
            if name.startswith("attempt-") and name[len("attempt-"):].isdigit()]
    if not used:
        return 0, None
    n = max(used)
    return n, os.path.relpath(os.path.join(tdir, f"attempt-{n}"), run.path)


def _set_aside_head(run, git, base):
    """The commit the branch was on when work was set aside: `set_aside` returns the tree to
    `base` and `begin_transaction` starts from `base == tree_of(HEAD)`, so it is a commit of this
    run branch whose tree is `base`. The oldest such commit is taken, so that `head..HEAD` is the
    widest range a later check can examine. The run's starting commit is searched too, and only
    while it is still an ancestor of HEAD. None when the history holds no such commit."""
    start = run.info["base_commit"]
    if git.run("merge-base", "--is-ancestor", start, "HEAD", check=False).returncode:
        return None
    commits = git.out("rev-list", f"{start}..HEAD").splitlines() + [start]
    for sha in reversed(commits):                          # rev-list is newest first
        if git.tree_of(sha) == base:
            return sha
    return None


def set_aside_record(run, git, tid):
    """The task's set-aside record: `set-aside.json` when it is there, otherwise the same record
    derived from what an older runner left behind, and None when the task has no set-aside work
    or nothing is left to derive from. Deriving writes nothing; publishing is the caller's step,
    so a refusal can never change the record."""
    tdir = run.task_dir(tid)
    path = os.path.join(tdir, "set-aside.json")
    if os.path.exists(path):
        return record.read_json(path)
    patch = os.path.join(tdir, "failed.patch")
    if not os.path.exists(patch):
        return None
    st = run.state["tasks"][tid]
    base = st.get("base")
    ref = f"{gitops.REF_PREFIX}{run.name}/{tid}/set-aside"
    candidate = git.pins(run.name).get(ref)
    if not base or not candidate:                # an old run already reduced, or an unpinned tree
        return None
    attempt, attempt_dir = _latest_attempt(run, tid)
    return {"task": tid, "attempt": attempt, "attempt_dir": attempt_dir,
            "status": st.get("status"), "reason": st.get("reason", ""), "at": None,
            "head": _set_aside_head(run, git, base), "base": base, "candidate": candidate,
            "candidate_ref": ref, "patch": os.path.relpath(patch, run.path),
            "paths": [p for _s, p, _o, _n in git.changed_paths(base, candidate)]}


def queued_recovery(run, git, tid):
    """The recovery `retry --apply-patch` queued for the task, or None. (An older runner's bare
    `apply_patch: true` is migrated to this on load: `schema`.)"""
    return run.state["tasks"][tid].get("recover") or None


def _pin(run, git, name, obj):
    op = run.begin("pin", name=name, object=obj)
    ref = git.pin(run.name, name, obj)
    active = run.state.get("active_producer")
    lease = run.state["tasks"][active].get("lease") if active else None
    if lease is not None:                         # the transaction's own refs are kept too (W-01)
        lease["refs"][ref] = obj
    run.finish(op, ref=name)


def take_lease(run, git):
    """What a producer transaction owns in git, as it is when the transaction opens: the commit
    and the branch it starts on, the `.git/info` files, and this run's pinned refs (W-01)."""
    record_ref = f"{gitops.REF_PREFIX}{run.name}/{record.RECORD_REF}"     # moves at every save
    return {"commit": git.head(), "branch": git.head_ref(), "info": git.info_files(),
            "refs": {ref: obj for ref, obj in git.pins(run.name).items() if ref != record_ref}}


def open_transaction(run, git, task_id, base, plan=None, op=None):
    """The producer's transaction is open, and with `plan` the set-aside work is back: installed
    whole in memory and saved once, then `<task>/base` pinned and the `recover` intent `op`
    finished. Every field comes from the arguments, which the engine and `_reconcile_recover`
    both take from the intent, so running it again over a half-installed transaction completes
    it. `plan` needs only the record's `attempt` and the `paths` put back."""
    st = run.state["tasks"][task_id]
    transaction.open_(st, task_id, reason="", base=base, candidate=None, attempts_used=0,
                      session_id=None, feedback=None, last_failure=None, final=None, sender=None,
                      lease=take_lease(run, git))
    if plan and run.intent(op).get('ruled_candidate'):
        st['ruled_candidate'] = run.intent(op)['ruled_candidate']
    run.state["active_producer"] = task_id
    # What git ignores at the start, so each attempt can name what appeared since (W-03). A tree
    # with more than IGNORED_AT_BASE such entries is not tracked: the state would carry them all.
    ignored = git.ignored_paths()
    if len(ignored) <= IGNORED_AT_BASE:
        st["ignored_at_base"] = ignored
    else:
        st.pop("ignored_at_base", None)
    if plan:
        attempt, files = plan["record"]["attempt"], len(plan["paths"])
        st["recovered"] = {"attempt": attempt, "at": run.intent(op)["at"], "files": files,
                           "op": op}
        st["feedback"] = {"recovered": {"attempt": attempt, "paths": plan["paths"]}}
        if findings.blockers(st.get("ledger") or {"findings": []}):
            # The retry kept the panel's open findings: the author answers them, as after any
            # send-back, instead of meeting them again as a new round's discoveries.
            st["feedback"].update(cause_title="the review panel's blocking findings are still open",
                                  cause="A person retried this task with the set-aside work put "
                                        "back. The findings below are what the panel held open "
                                        "against that work; answer each, fixed or disputed.",
                                  candidate=plan["record"].get("candidate"),
                                  **findings.feedback(st["ledger"]))
    run.save()
    _pin(run, git, f"{task_id}/base", base)
    if plan:
        run.finish(op, restored=files, attempt=attempt)
        run.event("recovered", task=task_id, attempt=attempt, files=files)
    run.save()
    run.write_findings()
    run.regenerate_safely()


def _config_hash(verifier):
    data = {k: verifier[k] for k in ("id", "kind", "commands", "read_only", "restores",
                                     "timeout_min")}
    if verifier.get("expect") == "fail":                  # only then, so other hashes stay as they were
        data.update(expect="fail", fail_pattern=verifier["fail_pattern"])
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


# -- what a person does between runs ---------------------------------------------------------------

class Refused(Exception):
    pass


def decide(run, engine, task_id, decision, note, who="", how=None):
    """`approve` or `reject` a human task. A verifying decision is bound to the candidate it saw.
    `how`: how the name was given and where the command ran (`cli.ruling_context`)."""
    if task_id not in engine.tasks:
        raise Refused(f"no task '{task_id}' in this run")
    task, st = engine.tasks[task_id], engine.st(task_id)
    if task["kind"] != "human":
        raise Refused(f"'{task_id}' is a {task['kind']} task; only a human task is approved or rejected")
    if st["status"] != "waiting_human":
        raise Refused(f"'{task_id}' is {st['status']}, not waiting for a person")
    target = task.get("verifies")
    candidate = engine.st(target)["candidate"] if target else None
    body = {"task": task_id, "decision": decision, "note": note, "by": who, **(how or {}),
            "at": _now(), "verifies": target, "candidate": candidate}
    run.write_decision(os.path.join(run.task_dir(task_id), "decision.json"), body)
    if target:
        st["decision"] = body                                    # applied by the next `resume`
        st["reason"] = f"{decision}d; `runner resume` continues"
    elif decision == "approve":
        set_status(st, task_id, "accepted", reason="")
    else:
        set_status(st, task_id, "blocked", reason="rejected: " + note)
    run.event("human-decision", task=task_id, decision=decision)
    run.save()
    run.write_findings()
    run.regenerate_safely()


# -- can the set-aside work still be put back? ------------------------------------------------

def _short(sha):
    return sha[:7]


def _no_candidate(run, task_id, rec=None):
    """C4, and the old record nothing is left to derive: the pinned tree cannot be reached, so
    `failed.patch` and `git apply` are all the owner has left."""
    rec = rec or {}
    ref = rec.get("candidate_ref", f"{gitops.REF_PREFIX}{run.name}/{task_id}/set-aside")
    patch = rec.get("patch", os.path.relpath(os.path.join(run.task_dir(task_id), "failed.patch"),
                                             run.path))
    return Refused(f"the candidate tree of '{task_id}' is no longer in this repository ({ref}). "
                   f"Its complete patch is still at {patch} and can be applied by hand with "
                   "`git apply`. Nothing was changed.")


def _runner_commit_since(git, head, tip):
    """C3: the oldest commit of `head..tip` that a runner made — an acceptance or a `--reopen`
    revert — with its trailers. None when only the owner's own commits landed."""
    for sha in reversed(git.out("rev-list", f"{head}..{tip}").splitlines()):
        trailers = git.trailers(sha)
        if "Run" in trailers:
            return sha, trailers
    return None


def _parents(path):
    """Every directory a path lies in, deepest first: 'a/b/c.txt' -> ['a/b', 'a']."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[:n]) for n in range(len(parts), 0, -1)]


def _obstructed(path, entries, directories):
    """Is `path` blocked by what a tree holds? `ls_tree` lists no directories at all, so a file
    and a directory of the same name never differ by name: what lies around the path is the only
    evidence — a directory standing where it goes, or a file on the way to it."""
    return path in directories or any(parent in entries for parent in _parents(path))


def recovery_plan(run, engine, git, task_id):
    """C1-C5 against the task's set-aside record, whether or not a recovery is queued: the caller
    decides when to ask. None only when the task has no set-aside work at all — no patch, or a
    record whose `paths` is empty, which is the attempt that changed nothing. Reads only — the
    record, the state, the index and the work tree are untouched, whatever it returns. Raises
    `Refused`, with the owner's message, when the work cannot be put back.
    Returns {"record": <the record>, "paths": [...], "expected": "<tree>", "base": "<tree>"}."""
    task = engine.tasks.get(task_id)
    patch = os.path.join(run.task_dir(task_id), "failed.patch")
    if not task or task["kind"] != "produce" or not os.path.exists(patch):
        return None
    rec = set_aside_record(run, git, task_id)
    if rec is None:                      # an old run whose state was reduced before this design
        raise _no_candidate(run, task_id)
    paths = rec["paths"]
    if not paths:                        # the attempt changed nothing: there is nothing to put back
        return None
    work = f"the set-aside work of '{task_id}' (attempt {rec['attempt']})"
    if git.pins(run.name).get(rec["candidate_ref"]) != rec["candidate"]:                  # C4
        raise _no_candidate(run, task_id, rec)
    head, tree = git.head(), git.tree_of("HEAD")
    was_on = rec.get("head")            # None in a legacy record whose set-aside commit was lost
    if was_on:                                                                            # C1
        if git.run("merge-base", "--is-ancestor", was_on, head, check=False).returncode:
            raise Refused(f"{work} cannot be put back: the run branch is no longer a descendant "
                          f"of {_short(was_on)}, where the work was set aside. Nothing was "
                          "changed.")
    elif rec["base"] != tree:
        # A legacy record whose set-aside commit could not be established: C1 and C3 cannot be
        # evaluated, so today's whole-tree rule stands in their place, word for word.
        raise Refused(f"the patch of '{task_id}' was made against tree {rec['base']}, but the "
                      f"accepted tree is now {tree}: other work was accepted since. Retry without "
                      "--apply-patch")
    at_base, at_head = git.ls_tree(rec["base"]), git.ls_tree(tree)
    at_candidate = git.ls_tree(rec["candidate"])
    # What HEAD still holds once the restore has taken away the paths the work deleted: a
    # directory the work itself empties does not stand in the way of the file that replaces it.
    taken_away = {p for p in paths if p not in at_candidate}
    stays = {p for p in at_head if p not in taken_away}
    directories = {d for p in stays for d in _parents(p)}
    moved = []                                                                            # C2
    for path in paths:
        if at_head.get(path) != at_base.get(path):
            moved.append(path)
        elif path in at_candidate and _obstructed(path, stays, directories):
            # Nothing is recorded under that name in either tree, and yet the work cannot be put
            # back: the owner has since committed a directory where the work's own file goes, or
            # a file where its directory goes. Restoring would quietly take the owner's work with
            # it, so it is a path that conflicts like any other.
            moved.append(path)
    if moved:
        raise Refused(f"{work} no longer applies: these paths changed since it was set aside: "
                      + ", ".join(moved) + ". Nothing was changed. Settle them, or retry without "
                      "--apply-patch to start clean.")
    runner_made = _runner_commit_since(git, was_on, head) if was_on else None             # C3
    if runner_made:
        sha, trailers = runner_made
        why = (f"accepted work was reverted since (commit {_short(sha)} reverts "
               f"{_short(trailers['Reverts'])})" if "Reverts" in trailers else
               f"other work was accepted since (commit {_short(sha)} of task "
               f"'{trailers.get('Task')}')")
        raise Refused(f"{work} cannot be put back: {why}. Nothing was changed. Retry without "
                      "--apply-patch to start clean.")
    for path in paths:                                                                    # C5
        as_root = engine.to_root(path)
        if as_root is None or not patterns.matches_any(task["writes"], as_root):
            raise Refused(f"{work} cannot be put back: it changed {path}, which '{task_id}' may "
                          "no longer write. Nothing was changed. Retry without --apply-patch to "
                          "start clean.")
    return {"record": rec, "paths": paths, "base": tree,
            "expected": git.tree_with(tree, rec["candidate"], paths)}


def _adopt_tip(run, git, task_id, rulings_file=None):
    """The pause expectation `retry` re-records, so that `resume` does not refuse the commit the
    runbook told the owner to make: the branch tip and the clean work tree as they are now. None
    when no pause expectation was recorded, and so nothing is adopted. A refusal writes nothing:
    `Refused` is raised when the branch or the tree moved in a way the owner's own commits cannot explain."""
    expect = run.state.get("expect")
    if not expect:
        return None
    tip, head = expect["tip"], git.head()
    why = None
    if git.run("merge-base", "--is-ancestor", tip, head, check=False).returncode:
        why = f"the run branch is no longer a descendant of {_short(tip)}, where the run paused"
    elif not git.is_clean():
        why = "the work tree has uncommitted changes"
    else:
        runner_made = _runner_commit_since(git, tip, head)
        if runner_made:
            why = (f"commit {_short(runner_made[0])}, made since the run paused, carries a Run: "
                   "trailer, so a runner made it")
    if why and why.startswith("the work tree"):
        # The owner's own change is adopted once committed. A rulings file `export-rulings` wrote
        # is committed as it was written: its entries are already marked written, so removing
        # it would drop them (RUN-81). Any other change may be put back as it was.
        dirty = git.dirty_paths()
        exported = findings.exported_files(run.state, rulings_file)
        ruled = [p[3:] for p in dirty if p[3:] in exported]
        remedy = (f"commit {', '.join(ruled)} as `runner export-rulings` wrote it"
                  + (", and commit the rest or put it back as it was" if len(ruled) < len(dirty)
                     else "")
                  if ruled else "commit them, or put them back as they were")
        raise Refused(f"'{task_id}' cannot be retried as the run stands: {why} "
                      f"({', '.join(dirty[:10])}). Nothing was changed; {remedy}, then run this "
                      "retry again.")
    if why:
        raise Refused(f"'{task_id}' cannot be retried as the run stands: {why}. Nothing was "
                      "changed; put the branch and the work tree back as they were, then "
                      "`runner resume`.")
    return {"tip": head, "tree": git.snapshot(run.index_file)}


def retry(run, engine, git, task_id, apply_patch=False):
    """Fresh attempts for a failed or blocked task, or for a pending producer that still has
    set-aside work (a replan resets a blocked producer to pending). With `apply_patch` the work is
    queued to be put back before the next attempt; without it any queued recovery is cancelled.
    Attempt numbers continue; nothing is reused. A refusal writes nothing at all.
    Returns {"recovering": <attempt queued, or None>, "set_aside": <attempt left in failed.patch,
    or None>}."""
    state = run.state
    if task_id not in engine.tasks:
        raise Refused(f"no task '{task_id}' in this run")
    active = state.get("active_producer")
    if active and active != task_id:
        raise Refused(f"the run is in the middle of '{active}' ({engine.st(active)['status']}"
                      + (f": {engine.st(active)['reason']}" if engine.st(active)["reason"] else "")
                      + "), which holds the work tree. Settle that first; nothing was changed")
    st = engine.st(task_id)
    task = engine.tasks[task_id]
    interrupted = [it["op"] for it in state["intents"]
                   if it["kind"] == "recover" and it["task"] == task_id]
    if interrupted:
        # The work may already be partly back in the tree, and only the intent can finish or
        # verify that: a retry here would promise a start the next `resume` does not make.
        raise Refused(f"putting back the set-aside work of '{task_id}' was interrupted "
                      f"({interrupted[0]}); `runner resume` finishes it first. Nothing was changed")
    rec = set_aside_record(run, git, task_id) if task["kind"] == "produce" else None
    if st["status"] not in ("failed", "blocked") and not (st["status"] == "pending" and rec):
        raise Refused(f"'{task_id}' is {st['status']}; only a failed or blocked task is retried")
    plan = None
    if apply_patch:
        patch = os.path.join(run.task_dir(task_id), "failed.patch")
        if task["kind"] != "produce" or not os.path.exists(patch):
            raise Refused(f"'{task_id}' has no set-aside patch to apply")
        plan = recovery_plan(run, engine, git, task_id)            # raises Refused: C1-C5
        if plan is None:                                    # the attempt changed nothing
            raise Refused(f"'{task_id}' has no set-aside patch to apply")
    adopted = _adopt_tip(run, git, task_id, engine.defaults.get("rulings_file"))
    # Every check has passed: from here on the retry writes.
    if adopted:
        state["expect"] = adopted
    if plan:
        rec = plan["record"]
        path = os.path.join(run.task_dir(task_id), "set-aside.json")
        if not os.path.exists(path):                        # a derived record, published now
            run.publish_decision(path, rec, crash=engine.crash)
    continued = False
    if task["kind"] == "produce":
        ledger = engine.ledger(task_id)
        # With the set-aside work put back, the line of work continues: the panel's open
        # findings stay open and go to the author, and each reviewer's next round judges the
        # rework from the candidate it last saw. Without the patch, or when a reviewer last saw
        # some other candidate than the one coming back, it is a new line of work (FND-19).
        ruled = bool(plan) and any(h.get('event') == 'human' and h.get('candidate') == rec['candidate']
                                  for f in ledger['findings'] for h in f['history'])
        continued = bool(plan) and (bool(findings.blockers(ledger)) or ruled) and all(
            r.get("last_seen_candidate") == rec["candidate"] for r in ledger["reviewers"].values())
        st["ledger"] = ledger if continued else findings.restart(ledger)
    # Clears the last attempt's `pending_*`, the panel, the label of the end being undone and a
    # queued recovery (without the flag: cancelled; with it, queued again below).
    transaction.reset(st, task_id, reason="", final=None, feedback=None, last_failure=None,
                      session_id=None, decision=None)
    if plan:
        st["recover"] = {"from": "set-aside", "attempt": rec["attempt"],
                         "candidate": rec["candidate"], **({"continued": True} if continued else {}),
                         **({"ruled": True} if continued and ruled and
                            not findings.blockers(st['ledger']) else {})}
    for other in engine.order:
        ost = engine.st(other)
        verifies = (engine.tasks[other].get("verifies") == task_id or
                    engine.tasks[other].get("reviews") == task_id)
        if ost["status"] == "skipped" or (verifies and ost["status"] in ("objected", "accepted")):
            set_status(ost, other, "pending", reason="", decision=None)
    state["status"] = "running"
    run.event("retry", task=task_id, apply_patch=bool(apply_patch), continued=continued)
    if plan:
        run.event("recover-requested", task=task_id, attempt=rec["attempt"],
                  files=len(plan["paths"]))
    run.save()
    run.write_findings()
    run.regenerate_safely()
    left = rec["attempt"] if rec and rec["paths"] and not plan else None
    return {"recovering": rec["attempt"] if plan else None, "set_aside": left,
            "continued": continued, "ruled": bool(st.get('recover', {}).get('ruled'))}


def resolve(run, engine, fid, decision, note="", who="", how=None):
    tid = fid.split("/", 1)[0]
    if tid not in engine.tasks or engine.tasks[tid]["kind"] != "produce":
        raise Refused(f"no producer for finding '{fid}'")
    st = engine.st(tid)
    held = (run.state.get("active_producer") == tid and st.get("step") == "escalation"
            and st["status"] == "waiting_human")
    if not held and st["status"] != "blocked":
        raise Refused(f"'{tid}' is {st['status']}; resolve needs a blocked task or a held escalation")
    ledger = engine.ledger(tid)
    finding = next((f for f in findings.blockers(ledger) if f['id'] == fid), None)
    if finding is None:
        raise Refused(f"{fid} is not an open blocking finding")
    candidate = st.get("candidate")
    if held:
        if engine.snapshot() != candidate:
            raise Refused("the work tree is no longer the candidate that was reviewed")
    else:
        rec = set_aside_record(run, engine.git, tid)
        if not rec or rec['candidate'] != candidate:
            raise Refused("the set-aside work is no longer the candidate that was reviewed")
    if ledger['reviewers'].get(finding['reviewer'], {}).get('last_seen_candidate') != candidate:
        raise Refused("the finding belongs to a different candidate lineage")
    try:
        st["ledger"] = findings.resolve(ledger, fid, decision, note, who, candidate, _now(), how)
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    run.event("finding-resolved", finding=fid, decision=decision, note=note, by=who, **(how or {}))
    if held and not [f for f in findings.blockers(st["ledger"]) if not f.get("upheld")]:
        st["reason"] = "rulings complete: `runner resume` continues"      # RUN-48
    engine.save()
