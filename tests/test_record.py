"""The run record: creation, durable state, intents and reconciliation, the lock, derived files.

Scenarios REC-01 to REC-13, REC-16 (REC-11 at primitive level is in test_gitops), RUN-07, RUN-08, RUN-11,
RUN-16, and the record half of FRZ-07. Each crash test stops the runner at an injected point,
then reconciles, as `resume` will.
"""

import json
import os
import signal
import subprocess
import sys
import time
import unittest

from helpers import EngineCase, RepoCase, done, git

from codesmith import gitops, record, workflow

WORKFLOW = """
name = "demo"
[[task]]
id = "design"
type = "design"
prompt_file = "briefs/d.md"
outputs = ["docs/d.md"]
reviewers = ["principled-priya", "clause-by-clause-chen"]
[[task]]
id = "implement"
type = "implement"
needs = ["design"]
outputs = ["src/**"]
gate = ["true"]
[[task]]
id = "signoff"
type = "human"
needs = ["implement"]
"""


class Crash(Exception):
    pass


def crash_at(name):
    def hook(point):
        if point == name:
            raise Crash(point)
    return hook


class RunCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.write("briefs/d.md", "Design it.\n")
        self.wf_path = self.write("wf.toml", WORKFLOW)
        self.commit()
        self.wf = workflow.load(self.wf_path)
        self.assertEqual(self.wf.errors, [])
        self.g = gitops.Git(self.root)
        self.g.create_and_checkout_run_branch("run/demo-test")
        self.run_ = record.Run.create(self.wf, self.g, "run/demo-test", "main")

    def reload(self):
        return record.Run.load(self.run_.path)

    def candidate(self, rel="docs/d.md", text="the design\n"):
        base = self.g.snapshot(self.run_.index_file)
        self.write(rel, text)
        cand = self.g.snapshot(self.run_.index_file)
        # As the engine leaves a producer about to be committed: an accepted one has both.
        self.run_.state["tasks"]["design"].update(base=base, candidate=cand)
        return base, cand, [p for _s, p, _o, _n in self.g.changed_paths(base, cand)]

    def commit_intent(self, cand, paths):
        return self.run_.begin("commit", task="design", attempt=1, parent=self.g.head(),
                               candidate=cand, paths=paths, subject="Design the thing")

    def tree_files(self, top):
        out = {}
        for dirpath, _dirs, files in os.walk(top):
            for f in files:
                with open(os.path.join(dirpath, f), "rb") as fh:
                    out[os.path.relpath(os.path.join(dirpath, f), top)] = fh.read()
        return out


class ManifestBoundary(EngineCase):
    def test_paired_manifest_edit_refuses_mutating_commands(self):
        """rec: paired manifest edits refuse mutating commands before writes (REC-55)"""
        import hashlib
        from pathlib import Path
        self.workflow('[[task]]\nid="look"\ntype="human"\n')
        self.assertEqual(self.start(), 255, self.output)
        run = self.the_run()
        path = Path(run.path)
        protected = path / 'workflow.expanded.json'
        protected.write_bytes(protected.read_bytes() + b' ')
        manifest = record.read_json(path / 'integrity.json')
        manifest['files']['workflow.expanded.json'] = hashlib.sha256(protected.read_bytes()).hexdigest()
        record.write_durable(path / 'integrity.json', record.dump_json(manifest))
        before = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()}
        commands = [('resume',), ('approve', 'latest', 'look'), ('reject', 'latest', 'look', '-m', 'no'),
                    ('retry', 'latest', 'look'), ('resolve', 'latest', 'look/X-1', '--as', 'advisory',
                                               '--note', 'note'), ('replan',)]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.runner(*command, '-C', self.root), 2, self.output)
                self.assertIn('workflow.expanded.json was changed', self.output)
                after = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()}
                self.assertEqual(after, before)
                self.assertEqual(self.calls(), 0)
        self.assertEqual(self.runner('repair-record', '-C', self.root), 0, self.output)
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        # A killed replan is an authorized publication, not a paired edit of its manifest.
        from unittest.mock import patch
        with open(self.wf_path, 'a') as fh:
            fh.write('\n[[task]]\nid="later"\ntype="human"\nneeds=["look"]\n')
        self.commit()
        original = record.Run.protect_definitions
        def kill_before_manifest(run, *args, **kwargs):
            if any(it['kind'] == 'replan' for it in run.state['intents']):
                raise Crash('replan definitions written before their manifest')
            return original(run, *args, **kwargs)
        with patch.object(record.Run, 'protect_definitions', kill_before_manifest):
            with self.assertRaises(Crash):
                self.runner('replan', '-C', self.root)
        self.assertEqual(self.resume(), 255, self.output)
        self.assertEqual(self.the_run().integrity_check(), [])
        self.assertEqual(self.runner('approve', 'latest', 'later', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)


class EventsAndManifestPins(EngineCase):
    def test_an_edited_event_keeps_every_other_pin(self):
        """rec: an edited event keeps the earlier events pinned and every other file pinned too
        (REC-58)"""
        from pathlib import Path
        self.workflow('[[task]]\nid="look"\ntype="human"\n')
        self.assertEqual(self.start(), 255, self.output)
        run = self.the_run()
        path = Path(run.path)
        events = path / 'events.jsonl'
        original = events.read_bytes()
        first, rest = original.split(b'\n', 1)
        events.write_bytes(first.replace(b'"event": "', b'"event": "x-') + b'\n' + rest)
        run.event('appended-after-edit')
        run.save()
        run.save()
        files, data = record.pinned_record(self.git_for(run), run.name)
        # The pin is current: the state and the manifest are this save's, the events the original.
        self.assertEqual(data[files['state.json']], (path / 'state.json').read_bytes())
        self.assertEqual(data[files['integrity.json']], (path / 'integrity.json').read_bytes())
        self.assertEqual(data[files['events.jsonl']], original)
        self.assertNotIn('pin_stale', run.state)
        self.assertEqual(events.read_bytes().count(b'record-events-changed'), 1)
        self.assertEqual(self.the_run().events_changed(), ['events.jsonl was changed'])
        # The pinned manifest stays the trusted one: a paired edit is still found.
        protected = path / 'workflow.expanded.json'
        protected.write_bytes(protected.read_bytes() + b' ')
        manifest = record.read_json(path / 'integrity.json')
        manifest['files']['workflow.expanded.json'] = record.sha256_file(protected)
        record.write_durable(path / 'integrity.json', record.dump_json(manifest))
        self.assertIn('workflow.expanded.json was changed', self.the_run().integrity_check())
        before = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()}
        for command in (('resume', '-C', self.root), ('approve', 'latest', 'look', '-C', self.root)):
            with self.subTest(command=command):
                self.assertEqual(self.runner(*command), 2, self.output)
                self.assertIn('events.jsonl was changed', self.output)
                after = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()}
                self.assertEqual(after, before)
        # And the state is still checked against its pinned copy of the same save.
        state = record.read_json(path / 'state.json')
        state['stop_reason'] = 'edited'
        record.write_durable(path / 'state.json', record.dump_json(state))
        self.assertEqual(self.the_run().state_changed(), ['state.json was changed'])
        self.assertEqual(self.runner('repair-record', '-C', self.root), 0, self.output)
        self.assertTrue(events.read_bytes().startswith(original))
        self.assertEqual(self.the_run().record_problems(), [])
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)

    def test_events_are_hashed_in_bounded_pieces(self):
        """rec: events.jsonl is pinned in bounded pieces: a save with nothing appended reads none
        of it, a grown one reads it once and git hashes the spooled copy, and the open check reads
        the pinned prefix once (REC-64)"""
        from pathlib import Path
        from unittest.mock import patch
        self.workflow('[[task]]\nid="look"\ntype="human"\n')
        self.assertEqual(self.start(), 255, self.output)
        run = self.the_run()
        events = Path(run.path) / 'events.jsonl'
        for n in range(1500):
            run.event('padding', n=n, text='x' * 200)
        run.save()
        chunk, reads = 4096, []
        span = record._read_span
        def counted(fh, size):
            for piece in span(fh, size):
                reads.append(len(piece))
                yield piece
        with patch.object(record, 'EVENTS_CHUNK', chunk), patch.object(record, '_read_span', counted):
            run.save()
            self.assertEqual(reads, [])                          # unchanged: only a stat
            run.event('one-more')
            size = events.stat().st_size
            self.assertGreater(size, 100 * chunk)
            run.save()
            self.assertEqual(sum(reads), size)                   # one pass over the grown file
            self.assertLessEqual(max(reads), chunk)
            files, data = record.pinned_record(self.git_for(run), run.name)
            self.assertEqual(data[files['events.jsonl']], events.read_bytes())
            self.assertEqual(self.the_run()._pinner().events, (size, files['events.jsonl']))
            reads.clear()
            self.assertEqual(self.the_run().events_changed(), [])
            self.assertEqual(sum(reads), size)                   # the pinned prefix, once
            self.assertLessEqual(max(reads), chunk)
        self.assertFalse([p for p in Path(run.path).iterdir() if p.name.endswith('.tmp')])

    def test_a_kill_between_the_manifest_pin_and_its_write_resumes(self):
        """rec: a kill between pinning the manifest and writing it is resumed without repair
        (REC-59)"""
        from pathlib import Path
        from unittest.mock import patch
        self.workflow('[[task]]\nid="look"\ntype="human"\n')
        self.assertEqual(self.start(), 255, self.output)
        path = Path(self.the_run().path)
        real = record.write_durable
        def kill_at_manifest(where, data, *rest):
            if os.path.basename(str(where)) == 'integrity.json':
                raise Crash('killed between the pin and the manifest write')
            return real(where, data, *rest)

        def killed(write):
            run = self.the_run()
            disk = (path / 'integrity.json').read_bytes()
            with patch.object(record, 'write_durable', kill_at_manifest):
                with self.assertRaises(Crash):
                    write(run)
            self.assertEqual((path / 'integrity.json').read_bytes(), disk)
            files, data = record.pinned_record(self.git_for(run), run.name)
            self.assertNotEqual(data[files['integrity.json']], disk)
            self.assertEqual(self.the_run().integrity_check(), [])
            replaced = (path / 'events.jsonl').read_bytes().count(b'integrity-manifest-replaced')
            self.assertEqual(self.resume(), 255, self.output)
            self.assertNotIn('was changed', self.output)
            files, data = record.pinned_record(self.git_for(run), run.name)
            self.assertEqual((path / 'integrity.json').read_bytes(), data[files['integrity.json']])
            self.assertEqual((path / 'events.jsonl').read_bytes().count(
                b'integrity-manifest-replaced'), replaced + 1)
            self.assertEqual(self.the_run().record_problems(), [])
            return run

        # A first write (no intent): reconcile writes the trusted manifest back.
        run = killed(lambda run: run.write_decision(os.path.join(run.task_dir('look'), 'a.json'),
                                                    {'n': 1}))
        target = os.path.join(run.task_dir('look'), 'a.json')
        # A rewrite under an intent (`publish_decision`): the decision is published again.
        killed(lambda run: run.publish_decision(target, {'n': 2}))
        self.assertEqual(record.read_json(target), {'n': 2})
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)

    def git_for(self, run):
        return gitops.Git(run.info['git_toplevel'])


class Heartbeat(RunCase):
    """rec: STATUS.md shows what is in flight and how long, refreshed without a state change"""

    def status(self):
        with open(os.path.join(self.run_.path, "STATUS.md")) as fh:
            return fh.read()

    def test_in_flight_calls_show_their_age(self):
        import datetime
        self.run_.state["status"] = "running"
        op = self.run_.begin("agent", task="design", invocation_dir="tasks/010-design/attempt-1/invocation-1")
        began = self.run_.intent(op)["at"]
        self.run_.regenerate()
        text = self.status()
        self.assertIn("## In flight", text)
        self.assertIn(f"**design**: agent call, started {began[11:19]} UTC, running for 0 min", text)
        state_before = record.read_json(os.path.join(self.run_.path, "state.json"))
        later = datetime.datetime.strptime(began, "%Y-%m-%dT%H:%M:%S.%fZ").replace(   # REC-50
            tzinfo=datetime.timezone.utc) + datetime.timedelta(minutes=14, seconds=5)
        self.assertTrue(self.run_.refresh_status(now=later))
        text = self.status()
        self.assertIn("running for 14 min 05 s", text)
        self.assertIn(f"As of {later.strftime('%H:%M:%S')} UTC", text)
        self.assertEqual(record.read_json(os.path.join(self.run_.path, "state.json")), state_before)
        self.assertFalse(os.path.exists(os.path.join(self.run_.path, "STATUS.md.beat")))
        self.run_.finish(op, status="ok")
        self.run_.regenerate()
        self.assertNotIn("## In flight", self.status())

    def test_a_call_in_flight_under_a_live_runner_is_not_reported_interrupted(self):
        """rec: an open operation is "interrupted" only when no runner is working on the run"""
        import datetime
        self.run_.state["status"] = "running"
        op = self.run_.begin("agent", task="design", invocation_dir="tasks/010-design/attempt-1/invocation-1")
        now = datetime.datetime.now(datetime.timezone.utc)
        lock = record.Lock(os.path.dirname(os.path.dirname(self.run_.path)))
        lock.acquire(self.run_.info["run_id"])                 # this process is the working runner
        try:
            self.run_.regenerate()
            self.assertIn("## In flight", self.status())
            self.assertNotIn("were interrupted", self.status())
            self.assertTrue(self.run_.refresh_status(now=now))
            self.assertNotIn("were interrupted", self.status())
        finally:
            lock.release()
        self.run_.regenerate()                                  # no runner: the open call was left behind
        self.assertIn("1 operation(s) were interrupted", self.status())
        self.run_.finish(op, status="ok")

    def test_in_flight_calls_show_the_tokens_used_so_far(self):
        """rec: the heartbeat reads the provider's record for what a running call has used"""
        import datetime
        from unittest.mock import patch
        from codesmith import agents
        self.run_.state["status"] = "running"
        now = datetime.datetime.now(datetime.timezone.utc)
        op = self.run_.begin("agent", task="design", invocation_dir="tasks/010-design/attempt-1/invocation-1")
        self.assertTrue(self.run_.refresh_status(now=now))
        self.assertNotIn("so far", self.status())                     # no agent kind: no record to read
        self.run_.finish(op, status="ok")
        self.run_.begin("agent", task="design", agent_kind="claude",
                        invocation_dir="tasks/010-design/attempt-1/invocation-2")
        with patch.object(agents, "partial_usage", return_value={"tokens_in": 48000, "tokens_out": 2000}) as reader:
            self.assertTrue(self.run_.refresh_status(now=now))
        self.assertIn("48000 tokens in and 2000 out so far (the provider's record; unpriced)", self.status())
        self.assertEqual(reader.call_args.args[:2],
                         ("claude", os.path.join(self.run_.path, "tasks/010-design/attempt-1/invocation-2")))
        with patch.object(agents, "partial_usage", return_value={}):
            self.assertTrue(self.run_.refresh_status(now=now))
        self.assertNotIn("so far", self.status())

    def test_a_beat_never_raises(self):
        self.run_.state["status"] = "running"
        self.run_.state["intents"].append({"op": "x", "kind": "agent", "at": "not a time"})
        self.assertFalse(self.run_.refresh_status())


class LivePage(RunCase):
    """rec: STATUS.md says when it was updated, how far the run is and what just happened (RUN-33)"""

    def page(self):
        with open(os.path.join(self.run_.path, "STATUS.md"), encoding="utf-8") as fh:
            return fh.read()

    def test_the_first_line_progress_and_recent_events(self):
        import datetime
        self.run_.state["tasks"]["design"].update(status="accepted", attempts=2, commit=self.g.head(),
                                                  base="b" * 40, candidate="c" * 40)
        self.run_.state["tasks"]["implement"].update(status="failed", attempts=1, reason="gate")
        self.run_.state.update(status="failed", stop_reason="the provider kept failing\non 'x'")
        for n in range(12):
            self.run_.event("tick", n=n)
        self.run_.save()
        self.run_.regenerate()
        text = self.page()
        first = text.splitlines()[0]
        self.assertRegex(first, r"^Updated \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC \(\d+s ago at render\)\. "
                                r"Status: failed \(exit 2\): the provider kept failing on 'x'\.$")
        self.assertIn("## Progress\n- Tasks: 1 of 5 accepted (1 failed, 3 pending).\n- Attempts used: 3.\n"
                      "- Budget used: $0.00 known of $50.00.\n- Elapsed: 0 min ", text)
        self.assertIn("since start, to the last event.", text)
        events = text[text.index("## Recent events"):text.index("## Next")].strip().splitlines()[1:]
        self.assertEqual(len(events), 10)
        self.assertRegex(events[-1], r"^- \d\d:\d\d:\d\d tick n=11$")
        self.assertLess(text.index("## Recent events"), text.index("## Next"))
        # The age is the clock's: rendered later, the same page says so.
        model = record.run_status_model(self.run_.info, self.run_.state, now=datetime.datetime.now(
            datetime.timezone.utc) + datetime.timedelta(seconds=90), run_path=self.run_.path)
        self.assertRegex(model[0][1][0][1], r"\((89|9\d)s ago at render\)")
        self.assertNotIn("ago at render", record.render_run_status(self.run_.info, self.run_.state))

    def test_in_flight_names_the_step_and_what_runs_next(self):
        self.run_.state["status"] = "running"
        self.run_.state["tasks"]["design"].update(status="running", attempts=2, step="attempt")
        self.run_.state["active_producer"] = "design"
        self.run_.begin("agent", task="design", invocation_dir="tasks/010-design/attempt-2/invocation-1")
        self.run_.regenerate()
        text = self.page()
        self.assertIn("- **design**: agent call (attempt 2, step attempt), started", text)
        self.assertIn("Then, in workflow order: design.review.principled-priya, "
                      "design.review.clause-by-clause-chen, implement, signoff.", text)
        self.assertTrue(text.startswith("Updated "))
        self.assertIn("Status: running, but no runner is working on it", text.splitlines()[0])


class StatusHtml(RunCase):
    """rec: STATUS.html is the same page, with links into the record (RUN-35)"""

    def html(self):
        with open(os.path.join(self.run_.path, "STATUS.html"), encoding="utf-8") as fh:
            return fh.read()

    def test_links_escaping_and_refresh(self):
        import re
        import urllib.parse
        _n, adir = self.run_.new_attempt("design")
        _i, inv = self.run_.new_invocation(adir)
        for d, name in ((adir, "prompt.md"), (adir, "result.json"), (adir, "changes.diff"),
                        (adir, "verification.json"), (inv, "stdout.log"), (inv, "stderr.log"),
                        (inv, "outcome.json")):
            with open(os.path.join(d, name), "w") as fh:
                fh.write("x")
        _r, rdir = self.run_.new_round("design.review.principled-priya")
        with open(os.path.join(rdir, "verdict.json"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(self.run_.task_dir("design"), "failed.patch"), "w") as fh:
            fh.write("diff\n")
        self.run_.set_status("design", "blocked", "<script>alert('x')</script> & more")
        self.run_.state["status"] = "needs_human"
        self.run_.save()
        self.run_.regenerate()
        page = self.html()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertNotIn("<script", page)
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; more", page)
        self.assertNotIn('http-equiv="refresh"', page)                # not running: no reload
        self.assertNotIn("http://", page.replace("http-equiv", ""))
        hrefs = re.findall(r'href="([^"]+)"', page)
        for want in ("workflow.toml", "state.json", "events.jsonl", "integrity.json",
                     "tasks/010-design/", "tasks/010-design/attempt-1/prompt.md",
                     "tasks/010-design/attempt-1/changes.diff",
                     "tasks/010-design/attempt-1/invocation-1/stdout.log",
                     "tasks/010-design/attempt-1/invocation-1/outcome.json",
                     "tasks/010-design/failed.patch",
                     "tasks/011-design.review.principled-priya/round-1/verdict.json"):
            self.assertIn(want, hrefs)
        for href in hrefs:                                            # every link resolves from file://
            self.assertFalse(href.startswith("/"), href)
            target = os.path.normpath(os.path.join(self.run_.path, urllib.parse.unquote(href)))
            self.assertTrue(os.path.exists(target), href)
        self.assertIn("not held by a runner of this run", page)
        # The same sections as STATUS.md: every heading of the one is in the other.
        with open(os.path.join(self.run_.path, "STATUS.md"), encoding="utf-8") as fh:
            headings = [l[3:] for l in fh if l.startswith("## ")]
        for h in headings:
            self.assertIn(f"<h2>{h.strip()}</h2>", page)
        self.run_.state["status"] = "running"
        self.assertTrue(self.run_.refresh_status())
        self.assertIn('<meta http-equiv="refresh" content="15">', self.html())


class HeartbeatInterval(EngineCase):
    """run: during a long agent call STATUS.md is refreshed every status_refresh_s (RUN-34)"""

    TASK = """
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt say good."
outputs = ["src/a.txt"]
writes = ["src/**"]
gate = ["grep -q good src/a.txt"]
"""

    def during(self, refresh):
        copy = os.path.join(self.side, f"during-{refresh}.md")
        self.workflow(self.TASK, defaults=f"status_refresh_s = {refresh}")
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done(),
                      "run": [f'sleep 2.4; cp ".runs/demo/$(cat .runs/demo/latest)/STATUS.md" "{copy}"']}])
        self.assertEqual(self.start(), 0, self.output)
        with open(copy, encoding="utf-8") as fh:
            return fh.read()

    def test_the_page_moves_during_a_call(self):
        text = self.during(1)
        self.assertIn("## In flight", text)
        self.assertRegex(text, r"\*\*make\*\*: agent call \(attempt 1, step attempt\), started "
                               r"\d\d:\d\d:\d\d UTC, running for 0 min 0[1-6] s")     # the launch handshake and load add a second or two
        self.assertIn("refreshed about every 1 s", text)
        self.assertTrue(text.startswith("Updated "))
        self.assertNotIn("## In flight", self.during(0))              # 0: no refresh during a call


class InvariantReport(RunCase):
    def test_a_broken_invariant_is_reported_and_raised_only_under_the_suite(self):
        """run: every save checks the state's invariants; a real run reports a broken one as an
        event and a STATUS line and carries on, the suite raises (RUN-45)"""
        from codesmith import invariants
        os.environ.pop(invariants.STRICT_ENV)
        self.addCleanup(os.environ.__setitem__, invariants.STRICT_ENV, "1")
        design = self.run_.state["tasks"]["design"]
        design.update(status="accepted", step="attempt", pending_attempt=[1, "x"],
                      pending_protocol_tries=4)
        self.run_.save()                                     # no exception
        self.run_.save()
        with open(os.path.join(self.run_.path, "events.jsonl"), encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh]
        reported = [e for e in events if e["event"] == "invariant-violation"]
        self.assertEqual(len(reported), 1)                   # once per change of what is broken
        self.assertEqual(reported[0]["problems"], [
            "'design' has step 'attempt' but is not the active producer",
            "'design' was charged 4 protocol tries",
            "'design' is accepted without a commit",
            "'design' is accepted without a candidate"])
        self.run_.regenerate()
        with open(os.path.join(self.run_.path, "STATUS.md"), encoding="utf-8") as fh:
            self.assertIn("The run state breaks an invariant: 'design' is accepted without a commit",
                          fh.read())
        self.assertEqual(self.reload().state["tasks"]["design"]["pending_protocol_tries"], 4)
        design.update(status="running", step=None, pending_protocol_tries=0, pending_attempt=None)
        self.run_.state["active_producer"] = "design"
        os.environ[invariants.STRICT_ENV] = "1"
        with self.assertRaises(invariants.Violation) as ctx:
            self.run_.save()
        self.assertIn("the active producer 'design' has no step", str(ctx.exception))

    def test_each_invariant_names_its_rule(self):
        """run: the invariants of a run state, one by one (RUN-45)"""
        from codesmith import invariants

        def state(**design):
            st = {"kind": "produce", "status": "running", "step": "attempt", "base": "b"}
            st.update(design)
            return {"active_producer": "design", "intents": [],
                    "tasks": {"design": st, "look": {"kind": "human", "status": "pending"}}}
        self.assertEqual(invariants.check(state()), [])
        cases = [
            (state(step=None), "the active producer 'design' has no step"),
            (state(status="failed"), "the active producer 'design' is failed"),
            (state(status="odd"), "'design' has the unknown status 'odd'"),
            (state(step="verify", pending_attempt=[1, "a"]), "holds a pending attempt at step 'verify'"),
            (state(candidate="c", base=None), "'design' has a candidate but no base"),
            (state(attempts_used=4), "'design' used 4 attempts of 3"),
            (dict(state(), intents=[{"op": "op-1"}, {"op": "op-1"}]), "share an operation id"),
            (dict(state(), active_producer="look"), "'look' is not a producer"),
        ]
        for st, rule in cases:
            with self.subTest(rule=rule):
                self.assertTrue(any(rule in p for p in invariants.check(
                    st, {"design": {"max_attempts": 3}})), invariants.check(st))
        ledger = {"findings": [{"id": "design/X-1", "severity": "blocking", "status": "open"}]}
        accepted = state(status="accepted", step="commit", commit="c", candidate="c", ledger=ledger)
        accepted["active_producer"] = None
        self.assertEqual(invariants.check(accepted),
                         ["'design' is accepted with blocking findings open: design/X-1"])
        accepted["tasks"]["look"]["step"] = "human"
        self.assertIn("'look' is a human task with step 'human'", invariants.check(accepted))


class BranchDisposition(RunCase):
    """rec: STATUS.md of a finished run says where its branch went, observed from git"""

    def finish(self):
        self.write("docs/d.md", "the design\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "design")
        for tid, t in self.run_.state["tasks"].items():
            t["status"] = "accepted"
            if t["kind"] == "produce":
                t.update(commit=self.g.head(), base="b" * 40, candidate="c" * 40)
        self.run_.state["status"] = "done"
        self.run_.save()
        self.run_.regenerate()

    def status(self):
        with open(os.path.join(self.run_.path, "STATUS.md")) as fh:
            return fh.read()

    def test_unmerged_then_merged(self):
        self.finish()
        self.assertIn("Not merged: `main` does not contain", self.status())
        self.assertIn("merging it is your call", self.status())
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--ff-only", "run/demo-test")
        self.assertIn("Not merged", self.status())               # nothing regenerated it yet
        self.reload().regenerate()
        self.assertIn("Merged: the last accepted commit", self.status())
        self.assertIn("is in `main`.", self.status())             # no upstream: no push remark
        self.assertNotIn("your call", self.status())

    def test_an_unfinished_run_says_nothing_about_merging(self):
        self.run_.regenerate()
        self.assertNotIn("erged", self.status())


class Creation(RunCase):
    def test_layout(self):
        p = self.run_.path
        runs = os.path.join(self.root, ".runs")
        with open(os.path.join(runs, ".gitignore")) as fh:
            self.assertEqual(fh.read(), "*\n")
        with open(os.path.join(runs, "README.md")) as fh:
            self.assertIn("state.json", fh.read())
        self.assertRegex(os.path.basename(p), r"^demo-\d{8}T\d{6}Z-[0-9a-f]{8}$")
        with open(os.path.join(runs, "demo", "latest")) as fh:
            self.assertEqual(fh.read().strip(), os.path.basename(p))
        info = record.read_json(os.path.join(p, "run.json"))
        self.assertTrue(os.path.basename(p).endswith(info["run_id"][:8]))
        self.assertEqual((info["workflow"], info["branch"], info["original_branch"]),
                         ("demo", "run/demo-test", "main"))
        self.assertEqual(info["workflow_file"], self.wf_path)
        self.assertEqual(info["root"], self.root)
        self.assertEqual(set(info["spend"]), {"known_usd", "reserved_usd", "unpriced"})
        self.assertEqual(sorted(n for n in os.listdir(os.path.join(p, "tasks")) if n != "index.json"),
                         ["010-design", "011-design.review.principled-priya",
                          "012-design.review.clause-by-clause-chen", "020-implement", "030-signoff"])
        with open(os.path.join(p, "workflow.toml")) as fh:
            self.assertEqual(fh.read(), WORKFLOW)
        with open(os.path.join(p, "briefs", "design.md")) as fh:
            self.assertEqual(fh.read(), "Design it.\n")
        self.assertEqual(sorted(os.listdir(os.path.join(p, "library", "types"))),
                         ["design-review.toml", "design.toml", "implement.toml", "index.json"])
        self.assertEqual(sorted(os.listdir(os.path.join(p, "library", "personas"))),
                         ["clause-by-clause-chen.toml", "index.json", "principled-priya.toml"])
        expanded = record.read_json(os.path.join(p, "workflow.expanded.json"))
        self.assertEqual(len(expanded["tasks"]), 5)
        self.assertTrue(self.g.is_clean())                      # the record ignores itself

    def test_the_branch_intent_is_in_the_first_state(self):
        """rec: a run interrupted before its branch exists is resumed by creating it: the intent
        is in the state Run.create writes first"""
        run = record.Run.create(self.wf, self.g, "run/demo-second", "main", branch_intent=True)
        intents = record.Run.load(run.path).state["intents"]
        self.assertEqual([(i["kind"], i["name"]) for i in intents], [("branch", "run/demo-second")])
        record.reconcile(record.Run.load(run.path), self.g)
        self.assertEqual(self.g.current_branch(), "run/demo-second")
        self.assertEqual(record.Run.load(run.path).state["intents"], [])

    def test_frozen_copies_do_not_follow_later_edits(self):
        self.write("briefs/d.md", "edited later\n")
        with open(os.path.join(self.run_.path, "briefs", "design.md")) as fh:
            self.assertEqual(fh.read(), "Design it.\n")

    def test_the_frozen_definitions_are_protected(self):
        """frz: the frozen definitions are protected (FRZ-13): an edit of the COPIED brief,
        persona, type, workflow or expanded workflow inside the run directory fails the integrity
        check; a run recorded before this protection gets them registered at resume"""
        run = self.run_
        frozen = ["briefs/design.md", "library/personas/principled-priya.toml",
                  "library/types/implement.toml", "workflow.toml", "workflow.expanded.json"]
        self.assertTrue(set(frozen) <= set(run._manifest()["files"]))
        self.assertEqual(run.integrity_check(), [])
        for rel in frozen:
            with self.subTest(rel=rel):
                path = os.path.join(run.path, rel)
                with open(path, "rb") as fh:
                    was = fh.read()
                with open(path, "ab") as fh:
                    fh.write(b"\n# approve whatever the author wrote\n")
                self.assertEqual(run.integrity_check(), [f"{rel} was changed"])
                with self.assertRaises(record.RecordError):
                    run.integrity_begin()
                with open(path, "wb") as fh:
                    fh.write(was)
        self.assertEqual(run.integrity_check(), [])
        manifest = run._manifest()                       # a run recorded before definitions were hashed
        legacy = {k: v for k, v in manifest["files"].items() if k not in frozen}
        record.write_durable(run._manifest_path(), record.dump_json(dict(manifest, files=legacy)))
        run._manifest_bytes = record.dump_json(dict(manifest, files=legacy))  # as its runner wrote it
        self.assertEqual(sorted(run.protect_definitions(missing_only=True)), sorted(frozen))
        self.assertEqual(run.protect_definitions(missing_only=True), [])
        self.assertEqual(run._manifest()["files"], manifest["files"])


class DurableState(RunCase):
    def test_durable_state(self):
        """rec: durable state (REC-06)"""
        self.run_.set_status("design", "running")
        self.run_.save()
        self.run_.set_status("design", "accepted")
        self.run_.crash = crash_at("state:before-rename")
        with self.assertRaises(Crash):
            self.run_.save()
        # A failure before the rename removes its temporary file (REC-63).
        self.assertEqual([n for n in os.listdir(self.run_.path) if n.endswith(".tmp")], [])
        again = self.reload()
        self.assertEqual(again.state["tasks"]["design"]["status"], "running")
        again.set_status("design", "verifying")
        again.save()                                            # the leftover does not get in the way
        self.assertEqual(self.reload().state["tasks"]["design"]["status"], "verifying")

    def test_durable_writes_never_use_a_project_path_as_scratch(self):
        """rec: write_durable creates its temporary file exclusively under a unique name: a
        declared `<file>.tmp` and a link there survive, and a failed write leaves nothing (REC-63)"""
        from unittest.mock import patch
        directory = os.path.join(self.run_.path, "scratch")
        os.makedirs(directory)
        target = os.path.join(directory, "rulings.toml")
        sentinel = os.path.join(directory, "rulings.toml.tmp")
        with open(sentinel, "wb") as fh:
            fh.write(b"a deliverable\n")
        victim = os.path.join(directory, "victim.txt")
        with open(victim, "wb") as fh:
            fh.write(b"untouched\n")
        os.symlink(victim, os.path.join(directory, ".rulings.toml.tmp"))
        made = []
        real_open = os.open
        def recording(path, flags, *args):
            made.append((path, flags))
            return real_open(path, flags, *args)
        with patch.object(record.os, "open", side_effect=recording):
            record.write_durable(target, b"one\n")
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"one\n")
        with open(sentinel, "rb") as fh:
            self.assertEqual(fh.read(), b"a deliverable\n")
        with open(victim, "rb") as fh:
            self.assertEqual(fh.read(), b"untouched\n")
        [(tmp, flags)] = [(p, f) for p, f in made if p.endswith(".tmp")]
        self.assertEqual(os.path.dirname(tmp), directory)
        self.assertRegex(os.path.basename(tmp), r"^\.rulings\.toml\.[0-9a-f]{12}\.tmp$")
        self.assertTrue(flags & os.O_CREAT and flags & os.O_EXCL)
        with self.assertRaises(Crash):
            record.write_durable(target, b"two\n", crash=crash_at("state:before-rename"))
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"one\n")
        self.assertEqual(sorted(os.listdir(directory)),
                         [".rulings.toml.tmp", "rulings.toml", "rulings.toml.tmp", "victim.txt"])

    def test_intents_have_unique_ids_and_are_logged(self):
        a = self.run_.begin("pin", name="x", object="1" * 40)
        b = self.run_.begin("pin", name="y", object="1" * 40)
        self.assertNotEqual(a, b)
        self.assertEqual([i["op"] for i in self.reload().state["intents"]], [a, b])
        self.run_.finish(a, ref="x")
        self.assertEqual([i["op"] for i in self.reload().state["intents"]], [b])
        with open(os.path.join(self.run_.path, "events.jsonl")) as fh:
            events = [json.loads(line) for line in fh]
        self.assertEqual([e["event"] for e in events][-3:], ["intent", "intent", "outcome"])

    def test_a_torn_last_event_does_not_swallow_the_next(self):
        """rec: a kill in the middle of an append leaves a torn last line; the next event starts
        a line of its own"""
        events = os.path.join(self.run_.path, "events.jsonl")
        with open(events, "ab") as fh:
            fh.write(b'{"event": "half')
        self.run_.event("after-a-kill")
        with open(events, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[-2], '{"event": "half')
        self.assertEqual(json.loads(lines[-1])["event"], "after-a-kill")

    def test_attempt_numbers_are_never_reused(self):
        """rec: attempt numbers are never reused (REC-07, record level)"""
        n1, d1 = self.run_.new_attempt("design")
        n2, d2 = self.run_.new_attempt("design")
        self.assertEqual((n1, n2), (1, 2))
        with open(os.path.join(d2, "result.json"), "w") as fh:
            fh.write("{}")
        self.run_.state["tasks"]["design"]["attempts"] = 0       # what `retry` does to the counter
        n3, d3 = self.run_.new_attempt("design")
        self.assertEqual(n3, 3)
        self.assertEqual(os.listdir(d3), [])
        with open(os.path.join(d2, "result.json")) as fh:
            self.assertEqual(fh.read(), "{}")
        i1, p1 = self.run_.new_invocation(d3)
        i2, p2 = self.run_.new_invocation(d3)
        self.assertEqual((i1, i2), (1, 2))
        self.assertEqual(os.listdir(p2), [])                     # created exclusively: nothing stale
        r1, _ = self.run_.new_round("design.review.principled-priya")
        self.assertEqual(r1, 1)


class Integrity(RunCase):
    def test_event_growth_preserves_pinned_prefix(self):
        """rec: event growth must preserve the pinned prefix (REC-57)"""
        run = self.run_
        run.event('original-history', detail='trusted')
        run.save()
        files, data = record.pinned_record(self.g, run.name)
        original = data[files['events.jsonl']]
        path = os.path.join(run.path, 'events.jsonl')
        with open(path, 'wb') as fh:
            fh.write(original.replace(b'original-history', b'edited-history'))
        run.event('later-event')
        run.save()
        files, data = record.pinned_record(self.g, run.name)
        self.assertEqual(data[files['events.jsonl']], original)
        with open(path, 'rb') as fh: damaged = fh.read()
        self.assertEqual(damaged.count(b'record-events-changed'), 1)
        self.assertNotIn(b'record-pin-failed', damaged)
        self.assertEqual(self.reload().events_changed(), ['events.jsonl was changed'])
        report = record.repair_record(run.path, self.g)
        self.assertEqual(report['problems'], [])
        with open(path, 'rb') as fh: repaired = fh.read()
        self.assertTrue(repaired.startswith(original))
        self.assertNotIn(b'edited-history', repaired)
        self.assertIn(b'later-event', repaired)
        self.assertIn(b'record-events-changed', repaired)
        run = self.reload()
        self.assertEqual(run.events_changed(), [])
        run.save()
        self.assertNotIn('pin_stale', self.reload().state)

    def test_the_record_protects_itself(self):
        """frz: the record protects itself (FRZ-07, record level: detection; failing the job is
        the engine's)."""
        _n, adir = self.run_.new_attempt("design")
        result = os.path.join(adir, "result.json")
        with open(result, "w") as fh:
            fh.write('{"outcome": "done"}')
        findings = os.path.join(self.run_.task_dir("design"), "findings.json")
        self.run_.write_decision(findings, {"producer": "design", "findings": []})
        self.assertEqual(self.run_.close_directory(adir),
                         ["tasks/010-design/attempt-1/result.json"])

        guard = self.run_.integrity_begin()
        self.assertEqual(self.run_.integrity_end(guard), [])

        guard = self.run_.integrity_begin()
        with open(findings, "w") as fh:
            fh.write('{"producer": "design", "findings": [], "tidied": true}')
        self.assertEqual(self.run_.integrity_end(guard), ["tasks/010-design/findings.json was changed"])
        self.run_.write_decision(findings, {"producer": "design", "findings": []})

        guard = self.run_.integrity_begin()
        state = os.path.join(self.run_.path, "state.json")
        with open(state, "a") as fh:
            fh.write(" ")
        os.unlink(result)
        self.assertEqual(self.run_.integrity_end(guard),
                         ["tasks/010-design/attempt-1/result.json was removed",
                          "state.json was changed"])

    def test_a_run_can_be_deleted(self):
        """run: a run can be deleted (RUN-16)"""
        _n, adir = self.run_.new_attempt("design")
        _i, inv = self.run_.new_invocation(adir)
        with open(os.path.join(inv, "outcome.json"), "w") as fh:
            fh.write("{}")
        self.run_.close_directory(adir)
        self.run_.regenerate()
        for dirpath, _dirs, _files in os.walk(self.run_.path):
            self.assertTrue(os.access(dirpath, os.W_OK), dirpath)
        subprocess.run(["rm", "-rf", self.run_.path], check=True)
        self.assertFalse(os.path.exists(self.run_.path))
        with open(record.__file__) as fh:
            self.assertNotIn("chmod", fh.read())


class DerivedFiles(RunCase):
    def populate(self):
        _n, adir = self.run_.new_attempt("design")
        _i, inv = self.run_.new_invocation(adir)
        for d, name in ((adir, "prompt.md"), (adir, "result.json"), (inv, "stdout.log"),
                        (inv, "outcome.json")):
            with open(os.path.join(d, name), "w") as fh:
                fh.write("x")
        self.run_.set_status("design", "blocked", "the agent said the brief contradicts the spec")
        self.run_.set_status("implement", "skipped", "upstream blocked")
        self.run_.state["status"] = "needs_human"
        self.run_.save()
        self.run_.regenerate()

    def derived(self):
        """The derived files, less the run page's first line: its age figure is the clock's."""
        return {k: (v.split(b"\n", 1)[1] if k == "STATUS.md" else v)
                for k, v in self.tree_files(self.run_.path).items()
                if os.path.basename(k) in ("STATUS.md", "index.json")}

    def test_derived_files_are_derived(self):
        """run: derived files are derived (RUN-08)"""
        self.populate()
        before = self.derived()
        self.assertGreater(len(before), 8)
        for rel in list(before) + ["STATUS.html"]:
            os.unlink(os.path.join(self.run_.path, rel))
        self.reload().regenerate()
        self.assertEqual(self.derived(), before)
        self.assertTrue(os.path.isfile(os.path.join(self.run_.path, "STATUS.html")))

    def test_self_describing(self):
        """run: self-describing (RUN-11)"""
        self.populate()
        for dirpath, dirnames, filenames in os.walk(self.run_.path):
            index = record.read_json(os.path.join(dirpath, "index.json"))
            listed = set(index["files"])
            actual = set(filenames) | {d + "/" for d in dirnames}
            self.assertEqual(listed, actual, dirpath)
            self.assertTrue(index["about"], dirpath)
        top = record.read_json(os.path.join(self.run_.path, "index.json"))
        self.assertIn("single source of truth", top["files"]["state.json"])
        attempt = record.read_json(os.path.join(self.run_.task_dir("design"), "attempt-1",
                                                "index.json"))
        self.assertEqual(attempt["path"], "tasks/010-design/attempt-1")
        self.assertTrue(all(attempt["files"].values()))

    def test_a_derived_file_is_replaced_whole(self):
        """rec: run.json (read back by resume) and STATUS.md are replaced whole: a kill while
        they are written leaves the old file, never an empty one"""
        from unittest import mock
        real = os.replace
        for name in ("run.json", "STATUS.md"):
            with self.subTest(name=name):
                self.run_.regenerate()
                with open(os.path.join(self.run_.path, name), "rb") as fh:
                    before = fh.read()
                self.run_.state["status"] = "stopped" if name == "run.json" else "failed"

                def replace(src, dst):
                    if dst.endswith(name):
                        raise Crash(dst)
                    return real(src, dst)
                with mock.patch.object(record.os, "replace", replace), self.assertRaises(Crash):
                    self.run_.regenerate()
                with open(os.path.join(self.run_.path, name), "rb") as fh:
                    self.assertEqual(fh.read(), before)

    def test_status_says_what_happened(self):
        self.populate()
        with open(os.path.join(self.run_.path, "STATUS.md")) as fh:
            text = fh.read()
        self.assertIn("needs a person", text)
        self.assertIn("| 010 | design | design | claude | blocked | 1 |", text)
        self.assertIn("skipped (upstream blocked)", text)
        self.assertIn("**design** is blocked: the agent said the brief contradicts the spec", text)
        info = record.read_json(os.path.join(self.run_.path, "run.json"))
        self.assertEqual(info["status"], "needs_human")


class Locking(RepoCase):
    def setUp(self):
        super().setUp()
        self.runs = record.ensure_runs_dir(self.root)

    def test_lock(self):
        """run: lock (RUN-07)"""
        first = record.Lock(self.runs).acquire("run-1")
        with self.assertRaises(record.LockHeld) as ctx:
            record.Lock(self.runs).acquire("run-2")
        self.assertIn("run-1", str(ctx.exception))
        first.release()
        record.Lock(self.runs).acquire("run-2").release()

    def test_process_identity_is_not_a_pid(self):
        """rec: process identity is not a pid (REC-08)"""
        mine = record.process_identity(os.getpid())
        stale = dict(mine, start_ticks=mine["start_ticks"] - 1)     # same pid, another process
        with open(os.path.join(self.runs, "lock"), "wb") as fh:
            fh.write(record.dump_json({"run_id": "dead-run", "process": stale}))
        self.assertTrue(record.is_alive(mine))
        self.assertFalse(record.is_alive(stale))
        lock = record.Lock(self.runs).acquire("run-3")
        self.assertEqual(lock.holder()["run_id"], "run-3")
        lock.release()

    def test_boot_id_is_part_of_process_identity(self):
        """rec: boot id is part of process identity (REC-13)"""
        mine = record.process_identity(os.getpid())
        self.assertTrue(mine["boot_id"])
        rebooted = dict(mine, boot_id="00000000-0000-0000-0000-000000000000")
        self.assertFalse(record.is_alive(rebooted))
        with open(os.path.join(self.runs, "lock"), "wb") as fh:
            fh.write(record.dump_json({"run_id": "before-reboot", "process": rebooted}))
        record.Lock(self.runs).acquire("after-reboot").release()

    def test_the_command_name_may_hold_parentheses(self):
        script = self.write("odd) name (x", "#!/bin/sh\nsleep 30\n")
        os.chmod(script, 0o755)
        child = subprocess.Popen([script], start_new_session=True)
        try:
            time.sleep(0.1)
            ident = record.process_identity(child.pid)
            self.assertIsInstance(ident["start_ticks"], int)
            self.assertEqual(ident["pgid"], child.pid)
        finally:
            child.kill()
            child.wait()
        self.assertIsNone(record.process_identity(child.pid))

    @unittest.skipUnless(sys.platform == "darwin", "libproc is macOS only")
    def test_a_process_of_another_user_is_alive_on_macos(self):
        """rec: libproc refuses to describe another user's process (EPERM); it is described by
        its `ps` start time and is alive, not taken for gone"""
        from unittest import mock
        child = subprocess.Popen(["sleep", "30"])
        real = record._darwin_start
        try:
            with mock.patch.object(record, "_darwin_start",
                                   lambda pid: None if pid == child.pid else real(pid)):
                ident = record.process_identity(child.pid)
                self.assertEqual(ident["lstart"], record._ps_lstart(child.pid))
                self.assertTrue(record.is_alive(ident))
                child.kill()
                child.wait()
                self.assertFalse(record.is_alive(ident))
                self.assertIsNone(record.process_identity(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_an_unreadable_lock_whose_flock_is_free_is_taken_over(self):
        """rec: a takeover killed half-way never blocks the repository (REC-19): an empty
        or torn lock file whose flock nobody holds is taken over; a live holder still excludes"""
        path = os.path.join(self.runs, "lock")
        for torn in (b"", b'{"run_id": "dead-r'):
            with self.subTest(torn=torn):
                with open(path, "wb") as fh:
                    fh.write(torn)
                lock = record.Lock(self.runs).acquire("run-4")
                self.assertEqual(lock.holder()["run_id"], "run-4")
                with self.assertRaises(record.LockHeld):
                    record.Lock(self.runs).acquire("run-5")
                lock.release()
        self.assertFalse(os.path.exists(path))

    def test_a_takeover_killed_before_its_rename_leaves_a_lock_the_next_runner_takes(self):
        """rec: a stale-lock takeover is written beside the lock and renamed over it (REC-19):
        killed before the rename, the stale holder is still whole and the next runner takes it;
        the locked file is never truncated or written in place"""
        from unittest import mock
        path = os.path.join(self.runs, "lock")
        dead = {"pid": 2 ** 22 + 7, "start_ticks": 1, "boot_id": "gone", "pgid": 2 ** 22 + 7}
        with open(path, "wb") as fh:
            fh.write(record.dump_json({"run_id": "dead-run", "process": dead}))
        stale_inode = os.stat(path).st_ino

        class Killed(BaseException):
            pass

        def killed(*_a):
            raise Killed()
        lock = record.Lock(self.runs)
        with mock.patch.object(os, "replace", killed), self.assertRaises(Killed):
            lock.acquire("killed-run")
        self.assertFalse(lock.held)
        self.assertEqual(record.read_json(path)["run_id"], "dead-run")
        with mock.patch.object(os, "ftruncate", side_effect=AssertionError("written in place")):
            taken = record.Lock(self.runs).acquire("next-run")
        self.assertEqual(taken.holder()["run_id"], "next-run")
        self.assertNotEqual(os.stat(path).st_ino, stale_inode)
        with self.assertRaises(record.LockHeld):
            record.Lock(self.runs).acquire("third-run")
        taken.release()
        self.assertEqual([n for n in os.listdir(self.runs) if n.startswith("lock")], [])

    def test_two_runners_that_find_one_stale_lock_cannot_both_take_it(self):
        """rec: the lock is an flock held for the runner's life, so of two runners that read the
        same stale lock only one takes it; and a release leaves alone a lock file not its own"""
        from unittest import mock
        path = os.path.join(self.runs, "lock")
        dead = {"pid": 2 ** 22 + 7, "start_ticks": 1, "boot_id": "gone", "pgid": 2 ** 22 + 7}
        with open(path, "wb") as fh:
            fh.write(record.dump_json({"run_id": "dead-run", "process": dead}))
        a, b = record.Lock(self.runs), record.Lock(self.runs)
        real, raced = record.Lock.holder, []

        def holder(lock):
            if lock is b and not raced:                  # B has read the stale holder: A runs now
                raced.append(True)
                with self.assertRaises(record.LockHeld):
                    a.acquire("run-A")
            return real(lock)
        with mock.patch.object(record.Lock, "holder", holder):
            b.acquire("run-B")
        self.assertEqual((a.held, b.held), (False, True))
        self.assertEqual(b.holder()["run_id"], "run-B")
        os.unlink(path)                                  # someone else's lock takes its place
        with open(path, "wb") as fh:
            fh.write(record.dump_json({"run_id": "run-C", "process": dead}))
        b.release()
        self.assertEqual(record.read_json(path)["run_id"], "run-C")


    def test_a_new_lock_file_is_never_seen_before_it_is_locked_and_written(self):
        """rec: a runner creating the lock links it into place already flocked and holding its
        run id, so a second runner finds it held, never empty, and the creator still takes it"""
        from unittest import mock
        a, b, seen = record.Lock(self.runs), record.Lock(self.runs), []
        real = os.link

        def link(src, dst):
            real(src, dst)
            if not seen:                                 # B looks the moment the file appears
                with self.assertRaises(record.LockHeld) as ctx:
                    b.acquire("run-B")
                seen.append(ctx.exception)
        with mock.patch.object(os, "link", link):
            a.acquire("run-A")
        self.assertIn("run-A", str(seen[0]))
        self.assertEqual((a.held, b.held), (True, False))
        self.assertEqual(os.listdir(self.runs).count("lock"), 1)
        self.assertEqual([n for n in os.listdir(self.runs) if n.startswith("lock.")], [])
        a.release()


class CommitRecovery(RunCase):
    def test_intent_without_effect(self):
        """rec: intent without effect (REC-01)"""
        _base, cand, paths = self.candidate()
        tip = self.g.head()
        op = self.commit_intent(cand, paths)
        # killed here: the intent is on disk, the commit is not made
        run = self.reload()
        done = record.reconcile(run, self.g)
        self.assertEqual(len(done), 1)
        self.assertEqual(self.g.head() != tip, True)
        self.assertEqual(int(gitops._text(self.g.run("rev-list", "--count", f"{tip}..HEAD").stdout)), 1)
        self.assertTrue(self.g.find_operation(self.g.head(), op))
        st = self.reload().state
        self.assertEqual(st["tasks"]["design"]["status"], "accepted")
        self.assertEqual(st["tasks"]["design"]["commit"], self.g.head())
        self.assertEqual(st["intents"], [])
        commit_json = record.read_json(os.path.join(run.task_dir("design"), "commit.json"))
        self.assertEqual(commit_json["files"], ["docs/d.md"])

    def test_intent_without_effect_but_the_tree_changed(self):
        _base, cand, paths = self.candidate()
        self.commit_intent(cand, paths)
        self.write("docs/d.md", "someone edited the candidate\n")
        with self.assertRaises(record.ReconcileError):
            record.reconcile(self.reload(), self.g)

    def test_effect_without_outcome(self):
        """rec: effect without outcome (REC-02)"""
        _base, cand, paths = self.candidate()
        parent = self.g.head()
        op = self.commit_intent(cand, paths)
        with self.assertRaises(Crash):
            self.g.commit_candidate(cand, paths, "Design the thing", self.run_.state["run_id"],
                                    "design", op, parent, crash=crash_at("commit:after-index-sync"))
        tip = self.g.head()
        self.assertNotEqual(tip, parent)
        record.reconcile(self.reload(), self.g)
        self.assertEqual(self.g.head(), tip)                     # no second commit
        st = self.reload().state
        self.assertEqual((st["tasks"]["design"]["status"], st["tasks"]["design"]["commit"]),
                         ("accepted", tip))

    def test_index_sync_is_part_of_the_commit(self):
        """rec: index sync is part of the commit (REC-10)"""
        _base, cand, paths = self.candidate()
        parent = self.g.head()
        op = self.commit_intent(cand, paths)
        with self.assertRaises(Crash):
            self.g.commit_candidate(cand, paths, "Design the thing", self.run_.state["run_id"],
                                    "design", op, parent, crash=crash_at("commit:after-update-ref"))
        self.assertFalse(self.g.is_clean())                      # the stale index shows phantom changes
        record.reconcile(self.reload(), self.g)
        self.assertTrue(self.g.is_clean())
        self.assertEqual(self.reload().state["tasks"]["design"]["status"], "accepted")

    def test_unexpected_branch_tip(self):
        """rec: unexpected branch tip (REC-03)"""
        _base, cand, paths = self.candidate()
        self.commit_intent(cand, paths)
        git(self.root, "commit", "-q", "--allow-empty", "-m", "someone else")
        with self.assertRaises(record.ReconcileError) as ctx:
            record.reconcile(self.reload(), self.g)
        self.assertIn("Someone changed the branch", str(ctx.exception))
        self.assertEqual(len(self.reload().state["intents"]), 1)   # nothing was settled by guessing


class EffectRecovery(RunCase):
    def test_restores_are_re_run(self):
        """rec: restores are re-run (REC-09)"""
        base, cand, paths = self.candidate("src/deep/new.py", "x = 1\n")
        self.write("docs/other.md", "also new\n")
        cand = self.g.snapshot(self.run_.index_file)
        paths = [p for _s, p, _o, _n in self.g.changed_paths(base, cand)]
        self.g.pin(self.run_.name, "design/base", base)
        self.run_.begin("restore", target=base, paths=paths, expected=base)
        with self.assertRaises(Crash):
            self.g.restore(base, paths, expected_tree=base, index_file=self.run_.index_file,
                           crash=crash_at("restore:after-removals"))
        record.reconcile(self.reload(), self.g)
        self.assertEqual(self.g.snapshot(self.run_.index_file), base)
        self.assertFalse(os.path.exists(os.path.join(self.root, "src")))
        self.assertEqual(self.reload().state["intents"], [])

    def test_idempotent_effects(self):
        """rec: idempotent effects (REC-12)"""
        base, cand, _paths = self.candidate()
        _n, adir = self.run_.new_attempt("design")
        with open(os.path.join(adir, "result.json"), "w") as fh:
            fh.write("{}")
        rel = os.path.relpath(adir, self.run_.path)
        self.run_.begin("pin", name="design/candidate-1", object=cand)
        self.run_.begin("close", dir=rel)
        self.run_.begin("patch", base=base, candidate=cand, path="tasks/010-design/failed.patch")
        self.g.pin(self.run_.name, "design/candidate-1", cand)   # the pin happened; the rest did not
        done = record.reconcile(self.reload(), self.g)
        self.assertEqual(len(done), 3)
        self.assertEqual([obj for ref, obj in self.g.pins(self.run_.name).items()
                          if not ref.endswith("/" + record.RECORD_REF)], [cand])
        run = self.reload()
        self.assertEqual(run.integrity_check(), [])
        self.assertIn("tasks/010-design/attempt-1/result.json", run._manifest()["files"])
        with open(os.path.join(run.task_dir("design"), "failed.patch"), "rb") as fh:
            self.assertEqual(fh.read(), self.g.full_patch(base, cand))
        self.assertEqual(record.reconcile(self.reload(), self.g), [])    # and once more: nothing to do

    def test_external_changes_while_paused(self):
        """run: external changes while paused (RUN-12, record level)"""
        tree = self.g.snapshot(self.run_.index_file)
        self.run_.state["expect"] = {"tip": self.g.head(), "tree": tree}
        self.run_.save()
        self.assertEqual(record.reconcile(self.reload(), self.g), [])
        self.write("docs/d.md", "edited while the run waited\n")
        with self.assertRaises(record.ReconcileError) as ctx:
            record.reconcile(self.reload(), self.g)
        self.assertIn("docs/d.md", str(ctx.exception))


class AgentRecovery(RunCase):
    def spawn(self, code):
        child = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait()))
        time.sleep(0.2)
        return child

    def agent_intent(self, child):
        _n, adir = self.run_.new_attempt("design")
        _i, inv = self.run_.new_invocation(adir)
        op = self.run_.begin("agent", task="design",
                             invocation_dir=os.path.relpath(inv, self.run_.path))
        self.run_.amend(op, process=record.process_identity(child.pid))
        self.run_.state["tasks"]["design"]["session_id"] = "sess-1"
        self.run_.save()
        return inv

    def test_no_concurrent_authors(self):
        """rec: no concurrent authors (REC-04)"""
        # The child ignores SIGINT and has a child of its own in the same group.
        child = self.spawn("import signal, subprocess, sys, time\n"
                           "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                           "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                           "time.sleep(60)\n")
        inv = self.agent_intent(child)
        with self.assertRaises(record.OrphanAlive):
            record.reconcile(self.reload(), self.g)
        self.assertIsNone(child.poll())                          # refused, and left alone
        self.assertEqual(len(self.reload().state["intents"]), 1)

        record.reconcile(self.reload(), self.g, stop_orphans=True, grace_s=0.5)
        child.wait(timeout=5)
        with self.assertRaises(ProcessLookupError):
            os.killpg(child.pid, 0)                              # descendants are gone too
        self.assertEqual(record.read_json(os.path.join(inv, "outcome.json"))["status"], "interrupted")

    def test_unknown_completion_is_not_success(self):
        """rec: unknown completion is not success (REC-05)"""
        child = self.spawn("import time; time.sleep(60)")
        inv = self.agent_intent(child)
        child.send_signal(signal.SIGKILL)
        child.wait()
        run = self.reload()
        record.reconcile(run, self.g)
        self.assertEqual(record.read_json(os.path.join(inv, "outcome.json"))["status"], "interrupted")
        st = self.reload().state
        self.assertIsNone(st["tasks"]["design"]["session_id"])
        self.assertEqual(st["intents"], [])
        n, retry_dir = run.new_invocation(os.path.dirname(inv))
        self.assertEqual(n, 2)
        self.assertNotEqual(retry_dir, inv)
        self.assertTrue(os.path.exists(os.path.join(inv, "outcome.json")))


class DecisionPublication(RunCase):
    def set_aside_path(self):
        return os.path.join(self.run_.task_dir("design"), "set-aside.json")

    def set_aside_record(self, attempt):
        return {"task": "design", "attempt": attempt, "status": "blocked",
                "reason": f"reason of attempt {attempt}", "at": "2026-09-20T11:02:14Z",
                "paths": ["docs/d.md"]}

    def test_a_published_decision_is_protected_and_leaves_no_intent(self):
        path = self.set_aside_path()
        self.run_.publish_decision(path, self.set_aside_record(1))
        run = self.reload()
        self.assertEqual(record.read_json(path), self.set_aside_record(1))
        self.assertEqual(run.state["intents"], [])
        self.assertIn("tasks/010-design/set-aside.json", run._manifest()["files"])
        self.assertEqual(run.integrity_check(), [])
        with open(os.path.join(run.path, "events.jsonl"), encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh]
        self.assertEqual([e["event"] for e in events[-2:]], ["intent", "outcome"])
        self.assertEqual(events[-1]["kind"], "decision")

    def test_a_replaced_decision_file_is_repaired(self):
        """rec: a replaced decision file is repaired (REC-16)"""
        path = self.set_aside_path()
        self.run_.publish_decision(path, self.set_aside_record(1))
        with self.assertRaises(Crash):
            self.run_.publish_decision(path, self.set_aside_record(2),
                                       crash=crash_at("decision:file-written"))
        run = self.reload()
        self.assertEqual(record.read_json(path), self.set_aside_record(2))    # new bytes ...
        self.assertEqual(run.integrity_check(),
                         ["tasks/010-design/set-aside.json was changed"])     # ... under the old hash
        self.assertEqual([it["kind"] for it in run.state["intents"]], ["decision"])

        done = record.reconcile(run, self.g)
        self.assertEqual(len(done), 1)
        run = self.reload()
        self.assertEqual(run.state["intents"], [])
        self.assertEqual(run.integrity_check(), [])
        self.assertEqual(record.read_json(path), self.set_aside_record(2))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), record.dump_json(self.set_aside_record(2)))
        self.assertEqual(record.reconcile(self.reload(), self.g), [])         # and once more

    def test_a_first_decision_file_lost_before_its_manifest_entry_is_repaired(self):
        path = self.set_aside_path()
        with self.assertRaises(Crash):
            self.run_.publish_decision(path, self.set_aside_record(1),
                                       crash=crash_at("decision:file-written"))
        record.reconcile(self.reload(), self.g)
        run = self.reload()
        self.assertEqual(run.integrity_check(), [])
        self.assertIn("tasks/010-design/set-aside.json", run._manifest()["files"])
        self.assertEqual(record.read_json(path), self.set_aside_record(1))

    def test_a_rewritten_decision_file_killed_before_its_entry_is_repaired(self):
        """rec: write_decision of a file already under the manifest (verification.json after a
        re-run, decision.json, findings.json) goes through an intent, so a kill between the file
        and its entry is repaired by `resume` instead of failing every later integrity check"""
        from unittest import mock
        path = os.path.join(self.run_.task_dir("design"), "findings.json")
        rel = "tasks/010-design/findings.json"
        self.run_.write_decision(path, {"producer": "design", "findings": []})
        self.assertEqual(self.reload().state["intents"], [])        # a first write needs none
        real = record.Run.protect

        def protect(run, *paths):
            if path in paths:
                raise Crash("between the file and its manifest entry")
            return real(run, *paths)
        with mock.patch.object(record.Run, "protect", protect), self.assertRaises(Crash):
            self.run_.write_decision(path, {"producer": "design", "findings": ["new"]})
        run = self.reload()
        self.assertEqual(run.integrity_check(), [f"{rel} was changed"])
        record.reconcile(run, self.g)
        run = self.reload()
        self.assertEqual(run.integrity_check(), [])
        self.assertEqual(record.read_json(path), {"producer": "design", "findings": ["new"]})
        with open(os.path.join(run.path, "events.jsonl"), encoding="utf-8") as fh:
            before = fh.read()
        run.write_decision(path, {"producer": "design", "findings": ["new"]})   # the same bytes
        with open(os.path.join(run.path, "events.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)                      # left alone: no intent

    def test_an_intent_without_its_file_is_rebuilt_from_the_payload(self):
        """The crash came after the intent was durable and before any write: the file is missing,
        or still holds the previous record. Only the intent's payload can repair it."""
        path = self.set_aside_path()
        rel = "tasks/010-design/set-aside.json"
        self.run_.begin("decision", path=rel, payload=self.set_aside_record(1))
        self.assertFalse(os.path.exists(path))
        record.reconcile(self.reload(), self.g)
        run = self.reload()
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), record.dump_json(self.set_aside_record(1)))
        self.assertEqual(run._manifest()["files"][rel], record.sha256_file(path))
        self.assertEqual(run.integrity_check(), [])
        self.assertEqual(run.state["intents"], [])

        self.run_ = run
        run.begin("decision", path=rel, payload=self.set_aside_record(2))
        self.assertEqual(record.read_json(path), self.set_aside_record(1))    # the old record
        record.reconcile(self.reload(), self.g)
        run = self.reload()
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), record.dump_json(self.set_aside_record(2)))
        self.assertEqual(run._manifest()["files"][rel], record.sha256_file(path))
        self.assertEqual(run.integrity_check(), [])
        self.assertEqual(run.state["intents"], [])


class BlockedProducerStatus(RunCase):
    """A blocked producer reads as it always did unless its panel failed at the protocol level.
    `block_kind` is absent in every state written before it existed, and it
    describes a `blocked` task only, so nothing else on the page moves."""

    def rendered(self, **extra):
        t = self.run_.state["tasks"]["implement"]
        t.update(reason="3 attempts used and the last was sent back by a person or a reviewer",
                 **extra)
        self.run_.save()
        self.run_.regenerate()
        with open(os.path.join(self.run_.path, "STATUS.md")) as fh:
            run_text = fh.read()
        with open(os.path.join(self.run_.task_dir("implement"), "STATUS.md")) as fh:
            return run_text, fh.read()

    def test_a_blocked_producer_without_a_block_kind_renders_as_today(self):
        """run: a protocol block is not a substantive block (RUN-19, the unchanged half)

        A producer blocked with a finding open, in a state that has no `block_kind` at all."""
        self.run_.state["tasks"]["implement"]["ledger"] = {
            "task": "implement", "reviewers": {}, "next_ids": {"PE": 2},
            "findings": [{"id": "implement/PE-1", "reviewer": "implement.review.principled-priya",
                          "severity": "blocking", "status": "open", "round": 1,
                          "title": "the queue drops the last element", "history": []}]}
        run_text, task_text = self.rendered(status="blocked")
        self.assertIn("| implement | implement | claude | blocked |", run_text)
        self.assertIn("- **implement/PE-1** [open]: the queue drops the last element", run_text)
        self.assertIn("- **implement** is blocked: 3 attempts used and the last was sent back by "
                      "a person or a reviewer. See tasks/020-implement/STATUS.md.", run_text)
        self.assertIn(f"    runner retry {self.run_.name} implement [--apply-patch]", run_text)
        self.assertIn("# implement — blocked\n", task_text)
        for text in (run_text, task_text):
            self.assertNotIn("required form", text)
            self.assertNotIn("Why each reviewer did not finish", text)

    def test_a_block_kind_left_by_an_earlier_end_does_not_label_a_later_status(self):
        """run: a protocol block is not a substantive block (RUN-19, a label outlives nothing)

        `replan` resets an affected producer to `pending` without touching the fields of the end
        it undoes, so the label is read only while the task is still blocked."""
        run_text, task_text = self.rendered(
            status="pending", block_kind="protocol",
            block_reviewers=[{"reviewer": "implement.review.principled-priya",
                              "status": "protocol-error", "tries": 3}])
        self.assertIn("| implement | implement | claude | pending |", run_text)
        self.assertIn("# implement — pending\n", task_text)
        for text in (run_text, task_text):
            self.assertNotIn("required form", text)
            self.assertNotIn("Why each reviewer did not finish", text)


class ReviewChurn(RunCase):
    """What the rework rounds bought, from the ledger (RUN-42)."""

    @staticmethod
    def finding(n, history):
        return {"id": f"implement/PE-{n}", "reviewer": "implement.review.principled-priya",
                "severity": "blocking", "status": "open", "title": f"defect {n}",
                "location": "src/q.py:4", "history": history}

    def test_a_chain_of_new_blockers_and_refused_fixes_is_named(self):
        """run: STATUS counts per round the blockers raised, resolved and kept open, the `fixed`
        answers a reviewer refused and the disputes, and warns when the last two rework rounds
        each raised new blockers or a fix was refused twice (RUN-42); an accepted task warns too,
        and a blocked one is told to settle the reading before `retry`"""
        fixed = {"event": "response", "action": "fixed", "note": "done"}
        findings = [self.finding(1, [{"event": "raised", "round": 1}, fixed,
                                     {"event": "resolution", "round": 2, "status": "unresolved"}, fixed,
                                     {"event": "resolution", "round": 3, "status": "unresolved"}]),
                    self.finding(2, [{"event": "raised", "round": 2}, fixed,
                                     {"event": "resolution", "round": 3, "status": "resolved"}]),
                    self.finding(3, [{"event": "raised", "round": 3}])]
        churn = record.review_churn({"findings": findings})
        self.assertEqual(churn, {"rounds": [{"round": 1, "raised": 1, "resolved": 0, "kept": 0},
                                            {"round": 2, "raised": 1, "resolved": 0, "kept": 1},
                                            {"round": 3, "raised": 1, "resolved": 1, "kept": 1}],
                                 "refused_fixes": 2, "answers": 3, "disputes": 0, "chain": 2,
                                 "rulings": {}, "open_now": 3})
        self.assertIsNone(record.review_churn({"findings": [dict(self.finding(9, []), severity="advisory")]}))
        st = self.run_.state["tasks"]["implement"]
        st["status"] = "blocked"
        st["ledger"] = {"task": "implement", "reviewers": {}, "next_ids": {"PE": 4}, "findings": findings}
        self.run_.save()
        self.run_.regenerate()
        with open(os.path.join(self.run_.path, "STATUS.md")) as fh:
            text = fh.read()
        self.assertIn("- **implement is churning**: the last 2 rework rounds each raised new blocking "
                      "findings; a reviewer refused an answer of `fixed` 2 times; 0 disputes in 3 "
                      "answers. Before `retry`, read the open findings: a brief that the reviewers "
                      "read two ways, or a case the brief does not make reachable, is yours to "
                      "settle, not the author's.", text)
        with open(os.path.join(self.run_.path, "tasks", "020-implement", "STATUS.md")) as fh:
            task_text = fh.read()
        self.assertIn("## Review rounds", task_text)
        self.assertIn("| 2 | 1 | 0 | 1 |", task_text)
        self.assertIn("Answers: 3, disputed: 0; `fixed` answers the reviewer refused: 2.", task_text)
        self.assertEqual(record.churn_lines("t", record.review_churn({"findings": findings[1:2]}), "accepted"), "")
        self.assertIn("a dispute from the author or a ruling from you ends it sooner",
                      record.churn_lines("t", churn, "accepted"))

    def test_rulings_do_not_erase_the_rounds_they_ended(self):
        """run: churn counts survive a ruling (RUN-47): the shape seen in a live run, two blockers in round 1,
        one disputed and escalated in round 2 beside a new blocker, counts the same before and
        after two advisory rulings; the rulings and the blockers open now are counted apart; a
        ledger written before raised events kept their severity counts the same"""
        import copy
        from codesmith import findings as fl
        from test_findings import finding, review, resolution
        carlos = {"id": "latch.review.concerned-carlos", "persona_code": "CC", "advisory": False}
        ledger, _v, _r = fl.apply_review(fl.empty("latch"), carlos,
                                         review([finding(), finding("src/a:3")]), "C1", {})
        ledger = fl.respond(ledger, done(responses=[
            dict(finding="latch/CC-1", action="disputed", note="the brief's recipe cannot"),
            dict(finding="latch/CC-2", action="fixed", note="ran the experiment")]), 2)
        ledger, verdict, _r = fl.apply_review(ledger, carlos, review(
            [finding("src/a:1")], [resolution("latch/CC-1", "unresolved"), resolution("latch/CC-2")]),
            "C2", {"src/a": [(1, 5)]})
        self.assertEqual(verdict, "block")
        before = record.review_churn(ledger)
        expected_rounds = [{"round": 1, "raised": 2, "resolved": 0, "kept": 0},
                           {"round": 2, "raised": 1, "resolved": 1, "kept": 1}]
        self.assertEqual((before["rounds"], before["answers"], before["disputes"], before["rulings"],
                          before["open_now"]), (expected_rounds, 2, 1, {}, 2))
        for fid in ("latch/CC-1", "latch/CC-3"):
            ledger = fl.resolve(ledger, fid, "advisory", "the brief's recipe", "owner", "C2", "now")
        after = record.review_churn(ledger)
        self.assertEqual((after["rounds"], after["answers"], after["disputes"], after["rulings"],
                          after["open_now"]), (expected_rounds, 2, 1, {"advisory": 2}, 0))
        self.assertEqual(ledger["findings"][0]["history"][-1]["was"], {"severity": "blocking",
                                                                       "status": "escalated"})
        legacy = copy.deepcopy(ledger)
        for f in legacy["findings"]:
            for h in f["history"]:
                h.pop("severity", None) if h["event"] == "raised" else h.pop("was", None)
        self.assertEqual(record.review_churn(legacy), after)
        st = self.run_.state["tasks"]["implement"]
        st.update(status="pending", ledger=ledger)
        self.run_.save()
        self.run_.regenerate()
        with open(os.path.join(self.run_.path, "tasks", "020-implement", "STATUS.md")) as fh:
            task_text = fh.read()
        self.assertIn("| 1 | 2 | 0 | 0 |\n| 2 | 1 | 1 | 1 |", task_text)
        self.assertIn("Answers: 2, disputed: 1; `fixed` answers the reviewer refused: 0. Rulings by a "
                      "person: 2 (2 advisory); blocking findings open now: 0.", task_text)


class AttentionLeads(RunCase):
    """The "Needs attention" list leads with what the reader must do (RD-04)."""

    def rendered(self, status, findings):
        st = self.run_.state["tasks"]["implement"]
        st["status"] = status
        st["ledger"] = {"task": "implement", "reviewers": {}, "next_ids": {"PE": 2},
                        "findings": findings}
        self.run_.save()
        self.run_.regenerate()
        with open(os.path.join(self.run_.path, "STATUS.md")) as fh:
            return fh.read()

    def test_an_escalation_asks_for_a_decision_and_a_sign_off_says_what_it_waits_for(self):
        """run: STATUS leads an escalation with the decision it needs, an open finding with who
        answers it, and a human task with what it waits for (RUN-40)"""
        finding = {"id": "implement/PE-1", "reviewer": "implement.review.principled-priya",
                   "severity": "blocking", "round": 1, "history": [],
                   "title": "the queue drops the last element", "location": "src/q.py:40"}
        text = self.rendered("waiting_human", [dict(finding, status="escalated")])
        self.assertIn('- **Your decision is needed on implement/PE-1**: a reviewer and the author '
                      'disagree about "the queue drops the last element" (src/q.py:40). Read the '
                      "finding and the author's reply in tasks/020-implement/findings.json, then "
                      "`runner resolve`.", text)
        # held for a ruling, not for an approval (a live run read "sign-off" here)
        self.assertIn("- **implement is held for your ruling** on implement/PE-1: `runner resolve` "
                      "each one, then `runner resume`; no approval follows. See "
                      "tasks/020-implement/STATUS.md.", text)
        self.assertNotIn("Waiting for your sign-off", text)
        text = self.rendered("pending", [dict(finding, status="open")])
        self.assertIn("- **implement/PE-1** [open]: the queue drops the last element (src/q.py:40) "
                      "(the author's next attempt answers it)", text)
        text = self.rendered("waiting_human", [])
        self.assertIn("- **Waiting for your sign-off on implement**: review found no remaining "
                      "blocker; the task is not accepted until you approve it. See "
                      "tasks/020-implement/STATUS.md.", text)


class ModelInStatus(RunCase):
    """STATUS shows each task's agent and model beside its cost, and the spend by model."""

    def test_the_table_and_the_spend_line_name_the_model(self):
        """run: STATUS names the model beside the cost (RUN-41): the task table has an
        "Agent / model" column (the planned agent until routing records what ran, then
        `profile / model`), and the spend line ends with the known dollars per model"""
        tasks = self.run_.state["tasks"]
        tasks["design"].update(status="accepted", attempts=1, cost_usd=3.95, commit="a" * 40,
                               base="b" * 40, candidate="c" * 40,
                               provider_current={"profile": "claude", "kind": "claude",
                                                 "model": "claude-opus-5", "complexity": "high",
                                                 "reason": "configured preference"})
        tasks["design.review.principled-priya"].update(
            status="accepted", cost_usd=0.0, attempts=0,
            provider_current={"profile": "codex", "kind": "codex", "model": "gpt-6-astra",
                              "complexity": "standard", "reason": "configured preference"})
        tasks["design.review.clause-by-clause-chen"].update(
            status="accepted", cost_usd=0.28,
            provider_current={"profile": "claude", "kind": "claude", "model": "claude-sonnet-5",
                              "complexity": "standard", "reason": "configured preference"})
        self.run_.state["spend"]["known_usd"] = 4.23
        self.run_.save()
        self.run_.regenerate()
        with open(os.path.join(self.run_.path, "STATUS.md")) as fh:
            text = fh.read()
        self.assertIn("| # | Task | Type | Agent / model | Status | Attempts | Cost | Commit |", text)
        self.assertIn("| 010 | design | design | claude / claude-opus-5 | accepted | 1 | $3.95 |", text)
        self.assertIn("| 011 | design.review.principled-priya | design-review | codex / gpt-6-astra | accepted |  |  |", text)
        self.assertIn("| 020 | implement | implement | claude | pending |", text)   # planned, not yet routed
        self.assertIn("| 030 | signoff | human |  | pending |", text)
        self.assertIn("By model: claude / claude-opus-5 $3.95 (1 task); codex / gpt-6-astra unpriced (1 task); "
                      "claude / claude-sonnet-5 $0.28 (1 task).", text)
        with open(os.path.join(self.run_.path, "STATUS.html")) as fh:
            self.assertIn("claude / claude-opus-5", fh.read())


class FrozenRules(RepoCase):
    """A task's rules_file is frozen with the briefs and covered by the integrity manifest."""

    def test_the_rules_file_is_frozen_per_task_and_inherited_by_generated_reviewers(self):
        """rec: rules_file is frozen as briefs/<id>.rules.md for the author and each reviewer, and
        a later edit of the original does not reach the run (REC-20)"""
        self.write("briefs/d.md", "Design it.\n")
        self.write("rules.md", "- No bool.\n")
        wf_path = self.write("wf.toml", WORKFLOW.replace('name = "demo"\n', 'name = "demo"\n[defaults]\nrules_file = "rules.md"\n'))
        self.commit()
        wf = workflow.load(wf_path)
        self.assertEqual(wf.errors, [])
        run = record.Run.create(wf, gitops.Git(self.root), "run/demo-rules", "main")
        briefs = sorted(n for n in os.listdir(os.path.join(run.path, "briefs")) if n != "index.json")
        self.assertEqual(briefs, ["design.md", "design.review.clause-by-clause-chen.rules.md",
                                  "design.review.principled-priya.rules.md", "design.rules.md",
                                  "implement.rules.md"])
        self.write("rules.md", "- Anything goes.\n")
        with open(os.path.join(run.path, "briefs", "implement.rules.md")) as fh:
            self.assertEqual(fh.read(), "- No bool.\n")
        self.assertEqual(run.integrity_check(), [])


class SetAsideStatus(RunCase):
    """STATUS.md tells the way back to set-aside work."""

    def record(self, attempt=1, paths=("src/a.py", "src/b.py")):
        return {"task": "implement", "attempt": attempt, "attempt_dir": f"tasks/020-implement/attempt-{attempt}",
                "status": "blocked", "reason": "the review panel could not answer in the required form",
                "at": "2026-09-20T11:02:14Z", "head": "c" * 40, "base": "b" * 40, "candidate": "d" * 40,
                "candidate_ref": f"refs/code-smith/{self.run_.name}/implement/set-aside",
                "patch": "tasks/020-implement/failed.patch", "paths": list(paths)}

    def set_aside(self, attempt=1, paths=("src/a.py", "src/b.py")):
        """Leaves `failed.patch` and a published `set-aside.json` behind, as `engine.set_aside`
        does, without running the full engine."""
        tdir = self.run_.task_dir("implement")
        with open(os.path.join(tdir, "failed.patch"), "w", encoding="utf-8") as fh:
            fh.write("diff --git a/src/a.py b/src/a.py\n")
        self.run_.publish_decision(os.path.join(tdir, "set-aside.json"), self.record(attempt, paths))

    def task_status(self):
        with open(os.path.join(self.run_.task_dir("implement"), "STATUS.md")) as fh:
            return fh.read()

    def run_status(self):
        with open(os.path.join(self.run_.path, "STATUS.md")) as fh:
            return fh.read()

    def test_replan_keeps_the_way_back_to_set_aside_work(self):
        """run: replan keeps the way back to set-aside work (RUN-18, STATUS.md)"""
        self.set_aside()
        # A replan resets an affected task's state to {dir, attempts, cost_usd[, recover]}; here
        # there is no queued recovery, only the record that survived on disk.
        self.run_.state["tasks"]["implement"] = {
            "status": "pending", "dir": "020-implement", "kind": "produce", "type": "implement",
            "attempts": 1, "cost_usd": 0.0, "commit": None, "reason": ""}
        self.run_.save()
        self.run_.regenerate()
        task_text = self.task_status()
        self.assertIn("Set aside from attempt 1 (blocked: the review panel could not answer in "
                     "the required form), 2 files.", task_text)
        self.assertIn(f"To put it back before the next attempt: runner retry {self.run_.name} "
                     "implement --apply-patch", task_text)
        self.assertIn("## Next", self.run_status())
        self.assertIn(f"    runner retry {self.run_.name} implement [--apply-patch]",
                     self.run_status())

    def test_a_queued_recovery_is_shown(self):
        self.set_aside()
        self.run_.state["tasks"]["implement"].update(
            status="pending", recover={"from": "set-aside", "attempt": 1, "candidate": "d" * 40})
        self.run_.save()
        self.run_.regenerate()
        self.assertIn("Queued: the set-aside work of attempt 1 will be put back before the next "
                     "attempt.", self.task_status())

    def test_recovered_work_is_shown(self):
        self.set_aside()
        self.run_.state["tasks"]["implement"].update(
            status="running", recovered={"attempt": 1, "at": "2026-09-20T11:05:00Z", "files": 2,
                                         "op": "op-0001-aaaaaaaa"})
        self.run_.save()
        self.run_.regenerate()
        self.assertIn("The set-aside work of attempt 1 was put back before attempt 2 (2 files).",
                     self.task_status())
        self.assertNotIn("Queued:", self.task_status())

    def next_section(self):
        text = self.run_status()
        return text[text.index("## Next"):]

    def test_a_legacy_record_that_cannot_be_derived_advertises_nothing(self):
        """pe: an old run already replanned by an older runner has `failed.patch` but no
        `set-aside.json` and nothing left to derive it from (C4's failure case); `--apply-patch`
        cannot act on it, so neither STATUS page offers the command (PE-1)"""
        tdir = self.run_.task_dir("implement")
        with open(os.path.join(tdir, "failed.patch"), "w", encoding="utf-8") as fh:
            fh.write("diff --git a/src/a.py b/src/a.py\n")
        self.run_.state["tasks"]["implement"] = {
            "status": "pending", "dir": "020-implement", "kind": "produce", "type": "implement",
            "attempts": 1, "cost_usd": 0.0, "commit": None, "reason": ""}
        self.run_.save()
        self.run_.regenerate()
        task_text = self.task_status()
        self.assertIn("Its work was set aside in failed.patch", task_text)
        self.assertNotIn("Set aside from attempt", task_text)
        self.assertNotIn("To put it back", task_text)
        self.assertNotIn("implement", self.next_section())

    def test_a_record_with_no_paths_advertises_nothing(self):
        """pe: a replanned attempt whose set-aside record has `paths: []` (the attempt changed
        nothing) is itself no set-aside work; `--apply-patch` has nothing to apply, so neither
        STATUS page offers it, and the task page never promises recovery of zero files (PE-1)"""
        self.set_aside(paths=[])
        self.run_.state["tasks"]["implement"] = {
            "status": "pending", "dir": "020-implement", "kind": "produce", "type": "implement",
            "attempts": 1, "cost_usd": 0.0, "commit": None, "reason": ""}
        self.run_.save()
        self.run_.regenerate()
        task_text = self.task_status()
        self.assertIn("Its work was set aside in failed.patch", task_text)
        self.assertNotIn("Set aside from attempt", task_text)
        self.assertNotIn("To put it back", task_text)
        self.assertNotIn("0 files", task_text)
        self.assertNotIn("implement", self.next_section())


class TimeAndSource(RunCase):
    """Which runner source made a record, and where its time went (RUN-49, RUN-50)."""

    def test_the_runner_source_is_recorded_with_or_without_git(self):
        """run: run.json names the runner source (RUN-49): the version, a SHA-256 over the
        package's files and, in a Git checkout, its commit and whether the package differs from
        it; with no git the fingerprint alone"""
        source = self.run_.info["runner_source"]
        self.assertRegex(source["src_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
        self.assertIsInstance(source["dirty"], bool)
        self.assertEqual(source["version"], self.run_.info["runner_version"])
        from unittest.mock import patch
        with patch.object(record.subprocess, "run", side_effect=OSError("no git")):
            bare = record.runner_source()
        self.assertEqual(bare, {"version": source["version"], "src_sha256": source["src_sha256"]})

    def test_durations_and_a_wall_clock_that_ran_ahead_are_shown(self):
        """run: STATUS shows how long each operation took and where the time went (RUN-50): an
        outcome event carries `wall_s` and its measured `seconds`; Recent events show the duration,
        and both figures when the wall clock ran a minute or more ahead; the Progress section
        splits agent calls, gates, replays and the time stopped for a person, and says the wall
        clock ran ahead without naming a cause; a run recorded before has no such lines"""
        import datetime
        self.run_.regenerate()
        self.assertNotIn("- Time:", self.page())
        op = self.run_.begin("command", task="implement", command="make test")
        self.run_.intent(op)["at"] = (datetime.datetime.now(datetime.timezone.utc)
                                      - datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.run_.finish(op, result="pass", seconds=5.0)
        quick = self.run_.begin("command", task="implement", command="true")
        self.run_.finish(quick, result="pass", seconds=0.2)
        with open(os.path.join(self.run_.path, "events.jsonl")) as fh:
            outcome = [json.loads(l) for l in fh if '"outcome"' in l][0]
        self.assertGreaterEqual(outcome["wall_s"], 600)
        self.assertEqual(outcome["seconds"], 5.0)
        gap = self.run_.state["clock_gap"]
        self.assertEqual(gap["operations"], 1)
        self.assertGreaterEqual(gap["seconds"], 595)
        self.run_.state.update(seconds=1925, gate_seconds=65.2, replay_seconds=113.3, stopped_seconds=7800)
        self.run_.save()
        self.run_.regenerate()
        text = self.page()
        self.assertRegex(text, rf"outcome \(0 min 05 s measured, 10 min 0\d s by the wall clock\) "
                               rf"kind=command op={op} result=pass")
        self.assertIn(f"outcome (took 0 min 00 s) kind=command op={quick} result=pass", text)
        self.assertIn("- Time: 32 min 05 s in agent calls (summed); 1 min 05 s in gates and checks; 1 min 53 s "
                      "in acceptance replays; 2 h 10 min stopped, waiting for a person.", text)
        # The two causes a wall clock ahead of the monotonic one can have, and the operation (V-08).
        self.assertRegex(text, rf"- The wall clock ran 9 min 5\d s ahead of the monotonic clock during "
                               rf"{op} \(implement\): the machine or its VM was suspended, or the clock "
                               r"was stepped; the record does not say which\.")

    def page(self):
        with open(os.path.join(self.run_.path, "STATUS.md")) as fh:
            return fh.read()


class TimeAndSourceOfARun(EngineCase):
    """A run records its source at start and resume and counts the time it stood stopped (RUN-49, RUN-50)."""

    TASKS = """
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt say good."
outputs = ["src/a.txt"]
gate = ["grep -q good src/a.txt"]
[[task]]
id = "look"
type = "human"
verifies = "make"
"""

    def test_start_and_resume_record_the_source_and_the_wait(self):
        """run: start and resume each write a runner-source event (RUN-49); the time a run
        stood stopped for a person is counted on resume, and gate time and command durations are
        recorded as the run goes (RUN-50)"""
        self.workflow(self.TASKS)
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.start(), 255, self.output)
        run = self.the_run()
        self.assertGreater(run.state["gate_seconds"], 0)
        run.state["stopped_at"] -= 3600                   # an hour before the person answers
        run.save()
        self.assertEqual(self.runner("approve", "latest", "look", "-C", self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        run = self.the_run()
        self.assertGreaterEqual(run.state["stopped_seconds"], 3600)
        self.assertNotIn("stopped_at", run.state)
        with open(os.path.join(run.path, "events.jsonl")) as fh:
            events = [json.loads(l) for l in fh if l.strip()]
        sources = [e for e in events if e["event"] == "runner-source"]
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["src_sha256"], run.info["runner_source"]["src_sha256"])
        agent = [e for e in events if e["event"] == "outcome" and e["kind"] == "agent"]
        self.assertTrue(agent and all("seconds" in e and "wall_s" in e for e in agent))
        with open(os.path.join(run.path, "STATUS.md")) as fh:
            self.assertIn("stopped, waiting for a person.", fh.read())


if __name__ == "__main__":
    unittest.main()


class RenderingFailures(EngineCase):
    def test_render_failure_does_not_stop_decisions(self):
        """rec: rendering failure leaves decisions durable and CLI errors visible (REC-40)"""
        from unittest import mock
        self.workflow('''
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt."
outputs = ["src/a.txt"]
gate = ["test -s src/a.txt"]
''')
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        regenerate, saved, attempts, snapshots = record.Run.regenerate, [], [], []

        def observe(run, *args, **kwargs):
            snapshots.append((record.read_json(os.path.join(run.path, "state.json")),
                              json.loads(record.dump_json(run.state))))
            saved.append(run.state["tasks"]["make"]["status"])
            return regenerate(run, *args, **kwargs)

        def broken(*args):
            attempts.append(args[0])
            raise KeyError("broken task page")

        with mock.patch.object(record.Run, "regenerate", observe), \
                mock.patch.object(record, "render_task_status", broken):
            self.assertEqual(self.start(), 0, self.output)
            run = self.the_run()
            self.assertEqual(run.state["status"], "done")
            self.assertIn("running", saved)
            for disk, memory in snapshots:
                self.assertEqual(disk, memory)
            self.assertIn("accepted", saved)
            self.assertGreater(len(attempts), 2)
            record.reconcile(run, gitops.Git(self.root))
            for flags in ((), ("--rebuild",)):
                self.assertEqual(self.runner("status", "-C", self.root, *flags), 2)
                self.assertIn("broken task page", self.output)
        with open(os.path.join(run.path, "events.jsonl")) as fh:
            failures = [json.loads(line) for line in fh if json.loads(line)["event"] == "render-failed"]
        self.assertEqual(len(failures), 2)  # Once in the engine, once in the reloaded reconciliation.
        self.assertTrue(all(e["task"] == "make" and e["exception"] == "KeyError"
                            and e["page"].endswith("/STATUS.md")
                            and "broken task page" in e["message"] for e in failures))
        run.regenerate()
        with mock.patch.object(record, "render_task_status", broken):
            run.regenerate_safely(full=True)
        with open(os.path.join(run.path, "events.jsonl")) as fh:
            self.assertEqual(sum(json.loads(line)["event"] == "render-failed" for line in fh), 3)
        with mock.patch.object(record, "run_status_model", side_effect=ValueError("broken run page")):
            self.assertFalse(run.refresh_status())
        with open(os.path.join(run.path, "events.jsonl")) as fh:
            failure = json.loads(fh.readlines()[-1])
        self.assertEqual(failure["exception"], "ValueError")
        self.assertEqual(failure["page"], "STATUS.md")
        self.check_invariants()

    TWO = '''
[[task]]
id = "alpha"
type = "implement"
prompt = "Make src/a.txt."
outputs = ["src/a.txt"]
gate = ["test -s src/a.txt"]

[[task]]
id = "beta"
type = "implement"
prompt = "Make src/b.txt."
outputs = ["src/b.txt"]
gate = ["test -s src/b.txt"]
'''

    def test_one_broken_task_page_does_not_stop_the_others(self):
        """rec: one task page that raises leaves the other pages and indexes written, reported
        once (REC-44)"""
        from unittest import mock
        self.workflow(self.TWO)
        self.script([{"write": {"src/a.txt": "a\n"}, "answer": done()},
                     {"write": {"src/b.txt": "b\n"}, "answer": done()}])
        render = record.render_task_status

        def broken(task_id, *args):
            if task_id == "alpha":
                raise KeyError("broken alpha page")
            return render(task_id, *args)

        with mock.patch.object(record, "render_task_status", broken):
            self.assertEqual(self.start(), 0, self.output)
        run = self.the_run()
        alpha, beta = run.task_dir("alpha"), run.task_dir("beta")
        self.assertFalse(os.path.exists(os.path.join(alpha, "STATUS.md")))
        for path in (os.path.join(alpha, "index.json"), os.path.join(beta, "STATUS.md"),
                     os.path.join(beta, "index.json"), os.path.join(beta, "attempt-1", "index.json"),
                     os.path.join(run.path, "index.json")):
            self.assertTrue(os.path.exists(path), path)
        with open(os.path.join(run.path, "events.jsonl")) as fh:
            failures = [e for e in map(json.loads, fh) if e["event"] == "render-failed"]
        self.assertEqual([(e["page"], e["task"]) for e in failures],
                         [(run._rel(os.path.join(alpha, "STATUS.md")), "alpha")])
        self.assertEqual(self.runner("status", "-C", self.root, "--rebuild"), 0, self.output)
        self.assertTrue(os.path.exists(os.path.join(alpha, "STATUS.md")))

    def test_status_reports_a_render_error_as_a_runner_error(self):
        """rec: status --rebuild reports a render error as a runner error with exit 2, never a
        traceback (REC-45)"""
        from unittest import mock
        self.workflow(self.TWO)
        self.script([{"write": {"src/a.txt": "a\n"}, "answer": done()},
                     {"write": {"src/b.txt": "b\n"}, "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        page = self.the_run()._rel(os.path.join(self.the_run().task_dir("alpha"), "STATUS.md"))
        with mock.patch.object(record, "render_task_status", side_effect=KeyError("broken")):
            self.assertEqual(self.runner("status", "-C", self.root, "--rebuild"), 2)
        self.assertEqual(self.last[2], f"runner: status: {page}: KeyError: 'broken'\n")
        self.assertEqual(self.last[1], "")


class IncrementalRendering(RunCase):
    def test_ledger_is_published_without_rendering(self):
        """rec: ledger changes are durable without regenerating pages (REC-41)"""
        from unittest import mock
        from codesmith import engine, findings
        run = self.run_
        runner = engine.Engine(run, self.g)
        path = os.path.join(run.task_dir("design"), "findings.json")
        run.state["tasks"]["design"]["ledger"] = findings.empty("design")
        with mock.patch.object(run, "regenerate"):
            for number in (1, 2):
                run.state["tasks"]["design"]["ledger"]["next_ids"]["reviewer"] = number
                runner.save()
                self.assertEqual(record.read_json(path), run.state["tasks"]["design"]["ledger"])
                self.assertEqual(run.integrity_check(), [])
        with mock.patch.object(run, "write_decision", side_effect=AssertionError("renderer wrote ledger")):
            run.regenerate(full=True)
        run.state["tasks"]["design"]["ledger"]["next_ids"]["reviewer"] = 3
        run.save()  # A kill between the state write and publishing the ledger is recoverable.
        record.reconcile(self.reload(), self.g)
        self.assertEqual(record.read_json(path), run.state["tasks"]["design"]["ledger"])

    def test_only_changed_task_directories_are_rendered(self):
        """rec: saves render changed tasks and run pages only (REC-42)"""
        from unittest import mock
        from codesmith import engine, findings
        run = self.run_
        _, attempt = run.new_attempt("design")
        run.new_invocation(attempt)
        run.regenerate(full=True)
        runner = engine.Engine(run, self.g)
        run.state["tasks"]["design"]["reason"] = "changed"
        expected = {"STATUS.md", "STATUS.html", "follow-ups.json", "index.json", "tasks/010-design/STATUS.md",
                    "tasks/010-design/index.json", "tasks/010-design/attempt-1/index.json",
                    "tasks/010-design/attempt-1/invocation-1/index.json"}
        with mock.patch.object(run, "_write", wraps=run._write) as writer:
            runner.save()
            self.assertEqual({run._rel(c.args[0]) for c in writer.call_args_list}, expected)
            writer.reset_mock()
            runner.save()
            self.assertEqual({run._rel(c.args[0]) for c in writer.call_args_list},
                             {"STATUS.md", "STATUS.html", "follow-ups.json", "index.json"})
            writer.reset_mock()
            run.state["tasks"]["design"]["ledger"] = findings.empty("design")
            runner.save()
            self.assertEqual({run._rel(c.args[0]) for c in writer.call_args_list}, expected)
        self.assertFalse(any("fingerprint" in key for key in self.reload().state))

    def test_cli_rebuild_restores_every_page(self):
        """rec: status rebuild restores every tampered page (REC-43)"""
        from helpers import run_cli
        run = self.run_
        _, attempt = run.new_attempt("design")
        run.new_invocation(attempt)
        run.regenerate(full=True)
        before = {rel: data for rel, data in self.tree_files(run.path).items()
                  if os.path.basename(rel) in ("STATUS.md", "STATUS.html", "index.json")}
        for rel in before:
            with open(os.path.join(run.path, rel), "w") as fh:
                fh.write("tampered page\n")
        nested = "tasks/010-design/attempt-1/invocation-1/index.json"
        os.unlink(os.path.join(run.path, nested))
        result = run_cli("status", "--rebuild", "-C", self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.tree_files(run.path)
        for rel, data in before.items():
            if rel in ("STATUS.md", "STATUS.html"):
                self.assertNotIn(b"tampered page", after[rel])
                self.assertIn(b"design", after[rel])
            else:
                self.assertEqual(after[rel], data, rel)


class OutcomesKeepTheirIntent(EngineCase):
    """An outcome keeps what its intent knew, and parallel calls are timed as such (REC-50, REC-51)."""
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'
    PANEL = """
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt say good."
outputs = ["src/a.txt"]
gate = ["grep -q good src/a.txt"]
reviewers = ["principled-priya", "clause-by-clause-chen"]
"""

    def test_outcomes_keep_the_intent_and_parallel_calls_are_timed_as_such(self):
        """rec: an outcome event keeps its intent's task, invocation, process and token reservation,
        and when the call ended; two reviewers side by side count their overlap (REC-50, REC-51)"""
        from test_findings import review
        # Room for the qualification probes, which hold their reservations too (PROC-31).
        self.workflow(self.PANEL, defaults="run_budget_tokens = 30000000")
        reviewers = [t["id"] for t in workflow.load(self.wf_path).tasks if t["kind"] == "review"]
        self.script({"make": [{"write": {"src/a.txt": "good\n"}, "answer": done()}],
                     reviewers[0]: [{"answer": review(), "sleep_s": 1}],
                     reviewers[1]: [{"answer": review(), "sleep_s": 2}]})
        self.assertEqual(self.start(), 0, self.output)
        run = self.the_run()
        with open(os.path.join(run.path, "events.jsonl")) as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        agents = [e for e in events if e["event"] == "outcome" and e["kind"] == "agent"]
        self.assertEqual(sorted(e["task"] for e in agents), sorted(["make"] + reviewers))
        for e in agents:
            self.assertTrue(e["invocation"].startswith("tasks/"), e)
            self.assertIsInstance(e["token_reservation"], int)
            self.assertGreater(e["token_reservation"], 0)
            self.assertIn("token_remainder", e)
            self.assertIsInstance(e["process"]["pgid"], int)
            self.assertIsInstance(e["process"]["supervisor"]["pid"], int)
            self.assertNotIn("members", json.dumps(e["process"]))
            self.assertRegex(e["ended_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
        commands = [e for e in events if e["event"] == "outcome" and e["kind"] == "command"]
        self.assertTrue(commands and all(e["task"] == "make" and "process" in e for e in commands))
        ended = {e["task"]: e["ended_at"] for e in agents}
        self.assertNotEqual(ended[reviewers[0]], ended[reviewers[1]])   # each when it ended, not when collected
        first = next(e for e in agents if e["task"] == reviewers[0])
        overlap = __import__("codesmith.status", fromlist=["x"]).overlapped_seconds(run.state["agent_spans"])
        self.assertGreaterEqual(overlap, 0.9)
        self.assertLessEqual(overlap, first["wall_s"] + 0.2)
        with open(os.path.join(run.path, "STATUS.md")) as fh:
            text = fh.read()
        self.assertRegex(text, r"in agent calls \(summed; 0 min 0\d s of it overlapped\)")
        recent = text.split("## Recent events", 1)[1]
        for hidden in ("pgid", "token_reservation", "ended_at", "invocation="):
            self.assertNotIn(hidden, recent)
        self.assertEqual(run.state["intents"], [])
        self.check_invariants()

    def test_the_overlap_of_agent_calls_is_the_sum_less_the_union(self):
        """rec: agent call intervals fold into a sum and a union; an interval a call in flight
        can still overlap stays open (REC-51)"""
        from codesmith import status
        state = {"intents": [{"op": "op-9", "kind": "agent", "at": "2026-10-01T10:00:05.000000Z"}]}
        base = status._parse_at("2026-10-01T10:00:00Z").timestamp()
        record._count_span(state, base, base + 2)            # before the call in flight began: folded
        record._count_span(state, base + 4, base + 10)       # overlaps it: kept open
        record._count_span(state, base + 6, base + 8)        # inside the open one
        spans = state["agent_spans"]
        self.assertEqual((spans["summed_s"], spans["folded_s"], spans["open"]),
                         (10.0, 2.0, [[base + 4, base + 10]]))
        self.assertEqual(status.overlapped_seconds(spans), 2.0)
