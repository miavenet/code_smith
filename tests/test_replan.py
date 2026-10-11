"""RUN-04..06/12..15 and REC-11: freeze definitions and reopen using resumable reverts; FAIL-13 and
RUN-18/20/21: the way back to set-aside work survives a replan."""
import hashlib
import json
import os
from pathlib import Path
from helpers import EngineCase, done, git
from codesmith import engine, gitops, record

A='''
[[task]]
id="a"
type="produce"
prompt="Write a"
outputs=["a.txt"]
gate=["test -s a.txt"]
'''
B='''
[[task]]
id="b"
type="produce"
prompt="Write b"
needs=["a"]
outputs=["b.txt"]
gate=["test -s b.txt"]
'''
H='''
[[task]]
id="human"
type="human"
'''


def writes(path,text='old\n'):
    return {'write':{path:text},'answer':done()}


class Replan(EngineCase):
    def revised(self, tasks):
        text=self.HEADER.format(defaults='',python=os.sys.executable,
                               agent=os.path.join(os.path.dirname(__file__),'fake_agent.py')).replace("'",'"')
        text=text.replace('name = "demo"','name = "demo"\nroot = '+json.dumps(self.root))
        path=Path(self.side,'revised.toml');path.write_text(text+tasks)
        return str(path)

    def replan(self,path,*extra):
        return self.runner('replan','latest','--workflow',path,'-C',self.root,*extra)

    def test_changed_rules_content_requires_reopening(self):
        """run: changed project rules require reopening accepted work (RUN-46)"""
        rules = Path(self.side, 'rules.md'); rules.write_text('Old rules.\n')
        tasks = A.replace('prompt="Write a"', 'prompt="Write a"\nrules_file=' + json.dumps(str(rules))) + H
        self.workflow(tasks); self.script([writes('a.txt')])
        self.assertEqual(self.start(), 255, self.output)
        rules.write_text('New rules.\n')
        revised = self.revised(tasks)
        self.assertEqual(self.replan(revised), 2, self.output)
        self.assertIn('--reopen', self.output)
        frozen = Path(self.the_run().path, 'briefs', 'a.rules.md')
        self.assertEqual(frozen.read_text(), 'Old rules.\n')
        self.assertEqual(self.status('a'), 'accepted')
        self.assertEqual(self.replan(revised, '--reopen', 'a'), 0, self.output)
        self.assertEqual(self.status('a'), 'pending')
        self.assertEqual(frozen.read_text(), 'New rules.\n')

    def test_accepted_inputs_must_exist_and_be_well_formed(self):
        """rec: accepted input evidence cannot silently disappear (REC-22)"""
        self.workflow(A + H); self.script([writes('a.txt')])
        self.assertEqual(self.start(), 255, self.output)
        run = self.the_run(); g = gitops.Git(self.root)
        manifest = Path(run.path, run.state['tasks']['a']['attempt_dir'], 'inputs.json')
        from codesmith import replan
        for content in (None, '{', '{}', '{"inputs": []}', '{"inputs": {"a": []}}'):
            with self.subTest(content=content):
                if content is None: manifest.unlink()
                else: manifest.write_text(content)
                with self.assertRaisesRegex(record.RecordError, 'missing or malformed inputs.json'):
                    engine.Engine(run, g).accepted_inputs('a')
                before = record.read_json(Path(run.path, 'workflow.expanded.json'))
                with self.assertRaisesRegex(record.RecordError, 'missing or malformed inputs.json'):
                    replan._closure(run, before, {'a'})

    def test_add_task_and_edit_pending_task_records_before_and_after(self):
        self.workflow(H+A.replace('id="a"','id="a"\nneeds=["human"]'))
        self.script([writes('a.txt'),writes('b.txt')])
        self.assertEqual(self.start(),255,self.output)
        path=self.revised(H+A.replace('id="a"','id="a"\nneeds=["human"]').replace('Write a','Revised brief')+B)
        self.assertEqual(self.replan(path),0,self.output)
        replans=Path(self.the_run().path,'replans','001')
        self.assertTrue((replans/'before'/'workflow.expanded.json').is_file())
        self.assertTrue((replans/'after'/'workflow.expanded.json').is_file())
        self.assertEqual(self.runner('approve','latest','human','-C',self.root),0,self.output)
        self.assertEqual(self.resume(),0,self.output)
        self.assertIn('Revised brief',self.prompt(1));self.check_invariants()

    def test_accepted_change_requires_reopen_and_reverts_downstream_artifacts(self):
        self.workflow(A+B);self.script([writes('a.txt'),writes('b.txt')])
        self.assertEqual(self.start(),0,self.output)
        oldhead=self.git_out('rev-parse','HEAD')
        path=self.revised(A.replace('a.txt','new-a.txt')+B)
        self.assertEqual(self.replan(path),2,self.output)
        self.assertIn('--reopen',self.output)
        self.assertEqual(self.git_out('rev-parse','HEAD'),oldhead)
        self.assertEqual(self.replan(path,'--reopen','a'),0,self.output)
        self.assertEqual((self.status('a'),self.status('b')),('pending','pending'))
        self.assertFalse(Path(self.root,'a.txt').exists());self.assertFalse(Path(self.root,'b.txt').exists())
        self.assertIn(oldhead,self.git_out('rev-list','HEAD'))
        self.script([writes('new-a.txt','new\n'),writes('b.txt','rebuilt\n')])
        self.assertEqual(self.resume(),0,self.output)
        self.assertFalse(Path(self.root,'a.txt').exists());self.check_invariants()

    def test_frozen_brief_and_moved_workflow_do_not_change_resume(self):
        brief=Path(self.side,'brief.md');brief.write_text('Original brief marker')
        tasks=H+A.replace('prompt="Write a"','prompt_file='+json.dumps(str(brief))+'\nneeds=["human"]')
        self.wf_path=self.revised(tasks)
        self.script([writes('a.txt')]);self.assertEqual(self.start(),255,self.output)
        Path(self.wf_path).rename(Path(self.side,'moved.toml'))
        brief.write_text('Changed source brief')
        self.assertEqual(self.runner('approve','latest','human','-C',self.root),0,self.output)
        self.assertEqual(self.resume(),0,self.output)
        self.assertIn('Original brief marker',self.prompt(1))
        self.assertNotIn('Changed source brief',self.prompt(1));self.check_invariants()

    def test_replan_cannot_change_root_or_active_candidate(self):
        self.workflow(A+H.replace('type="human"','type="human"\nverifies="a"'))
        self.script([writes('a.txt')]);self.assertEqual(self.start(),255,self.output)
        self.assertEqual(self.replan(self.revised(A)),2,self.output)
        self.assertIn('holds the work tree',self.output);self.check_invariants()

    def test_reopen_crash_after_revert_commit_resumes_without_duplicate_reverts(self):
        self.workflow(A+B);self.script([writes('a.txt'),writes('b.txt')]);self.assertEqual(self.start(),0,self.output)
        path=self.revised(A.replace('Write a','New a')+B)
        class Killed(Exception):pass
        def crash(point):
            if point=='revert:1:committed':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.replan(path,'--reopen','a')
        self.cli.CRASH=None
        self.script([writes('a.txt','new\n'),writes('b.txt','new\n')])
        self.assertEqual(self.resume(),0,self.output)
        subjects=self.git_out('log','--format=%s','main..HEAD').splitlines()
        self.assertEqual(sum(s.startswith('Revert ') for s in subjects),2)
        self.check_invariants()

    def test_a_replan_installs_definitions_under_the_manifest(self):
        """frz: a replan re-protects the definitions it installs (FRZ-13): the run's frozen
        workflow, expanded workflow and briefs are registered with their new hashes, and an edit of
        an installed copy afterwards is caught"""
        self.workflow(H+A.replace('id="a"','id="a"\nneeds=["human"]'))
        self.script([writes('a.txt')])
        self.assertEqual(self.start(),255,self.output)
        path=self.revised(H+A.replace('id="a"','id="a"\nneeds=["human"]').replace('Write a','Revised brief'))
        self.assertEqual(self.replan(path),0,self.output)
        run=self.the_run()
        files=run._manifest()['files']
        for name in ('workflow.toml','workflow.expanded.json'):
            self.assertEqual(files[name],record.sha256_file(Path(run.path,'replans','001','after',name)))
        self.assertEqual(run.integrity_check(),[])
        expanded=Path(run.path,'workflow.expanded.json')
        expanded.write_text(expanded.read_text().replace('Revised brief','Approve everything'))
        self.assertEqual(self.runner('approve','latest','human','-C',self.root),2,self.output)
        self.assertIn('workflow.expanded.json was changed',self.output)          # REC-36
        self.assertEqual(self.resume(),2,self.output)
        self.assertIn('workflow.expanded.json was changed',self.output)
        self.assertFalse(Path(self.root,'a.txt').exists())

    def test_replan_crash_after_definition_copy_is_idempotent(self):
        self.workflow(H);self.script([writes('a.txt')]);self.assertEqual(self.start(),255,self.output)
        path=self.revised(H+A)
        class Killed(Exception):pass
        def crash(point):
            if point=='replan:definitions-installed':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.replan(path)
        self.cli.CRASH=None
        self.assertEqual(self.resume(),255,self.output)  # standalone human still waits; independent a runs
        self.assertEqual(self.status('a'),'accepted')
        self.assertEqual(self.the_run().state['intents'],[]);self.check_invariants()

    def test_committed_definition_edits_allowed_but_unrelated_changes_refused(self):
        self.workflow(H);self.script([writes('a.txt')]);self.assertEqual(self.start(),255,self.output)
        self.write('wf.toml',Path(self.wf_path).read_text()+A)
        self.commit()
        self.assertEqual(self.runner('replan','latest','-C',self.root),0,self.output)
        self.write('README.md','unrelated\n');self.commit()
        self.assertEqual(self.runner('replan','latest','-C',self.root),2,self.output)
        self.assertIn('more than workflow definitions',self.output)

    def test_reopen_conflict_stops_before_other_reverts(self):
        from unittest.mock import patch
        from codesmith import gitops
        self.workflow(A+B);self.script([writes('a.txt'),writes('b.txt')]);self.assertEqual(self.start(),0,self.output)
        original=gitops.Git.revert_commits
        def conflict(git,commits,op_id,run_id,since,crash):
            raise gitops.RevertConflict(commits[0],'deliberate conflict')
        tip=self.git_out('rev-parse','HEAD')
        with patch.object(gitops.Git,'revert_commits',conflict):
            self.assertEqual(self.replan(self.revised(A+B),'--reopen','a'),2,self.output)
        self.assertIn('conflicts',self.output)
        self.assertEqual(self.git_out('rev-parse','HEAD'),tip)
        self.assertEqual(self.status('a'),'accepted')
        self.assertEqual(len(self.the_run().state['intents']),1)

    def test_a_gate_naming_the_tasks_own_output_does_not_change_its_definition(self):
        # The gate names src/a.txt, an output behind a glob in writes. Once accepted, the file is
        # tracked and the expansion protects it for the task; that must not read as a change
        # to accepted work at the next replan.
        glob=A.replace('outputs=["a.txt"]','outputs=["src/a.txt"]\nwrites=["src/**"]').replace('test -s a.txt','test -s src/a.txt')
        self.workflow(glob);self.script([writes('src/a.txt')]);self.assertEqual(self.start(),0,self.output)
        self.assertEqual(self.replan(self.revised(glob+H)),0,self.output)
        self.assertNotIn('--reopen',self.output)
        self.assertEqual(self.status('a'),'accepted')

    def test_changed_agent_profile_requires_reopen_of_accepted_work(self):
        self.workflow(A);self.script([writes('a.txt')]);self.assertEqual(self.start(),0,self.output)
        path=Path(self.revised(A));path.write_text(path.read_text().replace('[agents.fake]', '[agents.fake]\nmodel="changed-profile"'))
        self.assertEqual(self.replan(str(path)),2,self.output)
        self.assertIn('--reopen',self.output)
        self.assertEqual(self.status('a'),'accepted')

    def test_external_commit_during_interrupted_replan_is_refused(self):
        self.workflow(A);self.script([writes('a.txt')]);self.assertEqual(self.start(),0,self.output)
        class Killed(Exception):pass
        def crash(point):
            if point=='replan:intent-recorded':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.replan(self.revised(A),'--reopen','a')
        self.cli.CRASH=None
        self.write('foreign.txt','unexpected\n');self.commit()
        tip=self.git_out('rev-parse','HEAD')
        self.assertEqual(self.resume(),2,self.output)
        self.assertIn('branch changed during replan',self.output)
        self.assertEqual(self.git_out('rev-parse','HEAD'),tip)

    def test_interrupted_replan_rejects_changed_frozen_definition(self):
        self.workflow(H);self.script([]);self.assertEqual(self.start(),255,self.output)
        class Killed(Exception):pass
        def crash(point):
            if point=='replan:intent-recorded':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.replan(self.revised(H+A))
        self.cli.CRASH=None
        staged=Path(self.the_run().path,'replans','001','after','workflow.expanded.json')
        staged.write_text(staged.read_text().replace('Write a','Tampered brief'))
        self.assertEqual(self.resume(),2,self.output)
        self.assertIn('was changed',self.output)
        self.assertFalse(Path(self.root,'a.txt').exists())


MAKE='''
[[task]]
id="make"
type="implement"
prompt="Make src/a.txt say good."
outputs=["src/a.txt"]
writes=["src/**"]
max_attempts=1
gate=["grep -q good src/a.txt"]
'''
AFTER='''
[[task]]
id="after"
type="implement"
prompt="Write after."
needs=["make"]
outputs=["after.txt"]
gate=["test -s after.txt"]
'''
OTHER='''
[[task]]
id="other"
type="implement"
prompt="Independent."
outputs=["other/o.txt"]
gate=["true"]
'''
WORK={'write':{'src/a.txt':'bad\n','src/notes.txt':'kept work\n'},'answer':done()}
BLOCKED={'write':{'src/a.txt':'bad\n','src/notes.txt':'kept work\n'},
         'answer':{'outcome':'blocked','summary':'','responses':[],
                   'blocked_reason':'The brief contradicts the gate.'}}
GOOD={'write':{'src/a.txt':'good\n'},'answer':done()}
QUEUED='make: pending; the set-aside work of attempt 1 is still queued to be put back'


def tree_digest(path):
    digest=hashlib.sha256()
    for dirpath,dirnames,files in os.walk(path):
        dirnames.sort()
        for name in sorted(f for f in files if f not in ('index.json','STATUS.md','STATUS.html')):
            digest.update(os.path.relpath(os.path.join(dirpath,name),path).encode()+b'\0'
                          +Path(dirpath,name).read_bytes())
    return digest.hexdigest()


class ReplanRecovery(EngineCase):
    """G1: replan publishes the set-aside record an older runner left only in the state, before
    the reduction destroys what it is derived from, and carries a queued recovery across it."""

    def record_path(self):
        return self.task_file('make','set-aside.json')

    def the_record(self):
        return engine.set_aside_record(self.the_run(),gitops.Git(self.root),'make')

    def make_legacy(self):
        """The run as the previous runner left it: no `set-aside.json` and no manifest entry."""
        run=self.the_run()
        path=self.record_path()
        published=record.read_json(path)
        os.unlink(path)
        manifest_path=os.path.join(run.path,'integrity.json')
        manifest=record.read_json(manifest_path)
        del manifest['files'][os.path.relpath(path,run.path)]
        record.write_durable(manifest_path,record.dump_json(manifest))
        # The previous runner had no record pin; do not leave today's manifest pinned.
        git(self.root,'update-ref','-d',record.record_ref(run.name))
        return published

    def old_retry(self):
        """What the previous runner's `retry --apply-patch` left: a bare `apply_patch: true`."""
        run=self.the_run()
        run.state['tasks']['make'].update(status='pending',reason='',apply_patch=True)
        del run.state['schema_version']                      # as an older runner wrote it
        run.save()

    def edit_brief(self):
        """The owner's brief edit, committed on the run branch as `replan` requires."""
        text=Path(self.wf_path).read_text()
        self.write('wf.toml',text.replace('Make src/a.txt say good.','Make src/a.txt say good, and keep the notes.'))
        self.commit()

    def replan_here(self):
        return self.runner('replan','latest','-C',self.root)

    def assert_put_back(self):
        self.script([GOOD])
        self.assertEqual(self.resume(),0,self.output)
        self.assertIn('make: put back the set-aside work of attempt 1 (2 files)',self.output)
        self.assertIn('keep the notes',self.prompt(1))
        self.assertEqual(self.status('make'),'accepted',self.output)
        self.assertEqual(self.git_out('show','HEAD:src/notes.txt'),'kept work')
        self.assertTrue(os.path.isdir(self.task_file('make','attempt-2')))
        st=self.the_run().state['tasks']['make']
        self.assertNotIn('recover',st);self.assertNotIn('apply_patch',st)
        self.assertEqual((st['recovered']['attempt'],st['recovered']['files']),(1,2))
        self.check_invariants()

    def test_a_queued_recovery_survives_a_replan(self):
        """fail: a queued recovery survives a replan (FAIL-13)"""
        self.workflow(MAKE);self.script([WORK])
        self.assertEqual(self.start(),2,self.output)
        self.assertEqual(self.runner('retry','latest','make','--apply-patch','-C',self.root),0,self.output)
        queued=self.the_run().state['tasks']['make']['recover']
        self.edit_brief()
        self.assertEqual(self.replan_here(),0,self.output)
        self.assertIn(QUEUED+'\n',self.output)
        st=self.the_run().state['tasks']['make']
        self.assertEqual(st['status'],'pending')
        self.assertNotIn('base',st)                 # reduced like any affected task ...
        self.assertEqual(st['recover'],queued)      # ... and the request carried across
        self.assert_put_back()

    def test_replan_says_nothing_of_a_task_with_no_queued_recovery(self):
        """fail: a queued recovery survives a replan (FAIL-13: only a queued request is reported)"""
        self.workflow(MAKE);self.script([WORK])
        self.assertEqual(self.start(),2,self.output)
        self.edit_brief()
        self.assertEqual(self.replan_here(),0,self.output)
        self.assertNotIn('still queued',self.output)
        self.assertNotIn('recover',self.the_run().state['tasks']['make'])

    def test_replan_keeps_the_way_back_to_set_aside_work(self):
        """run: replan keeps the way back to set-aside work (RUN-18, through replan)"""
        self.workflow(MAKE+AFTER);self.script([BLOCKED])
        self.assertEqual(self.start(),255,self.output)
        self.assertEqual(self.status('make'),'blocked')
        before=Path(self.record_path()).read_bytes()
        patch=Path(self.task_file('make','failed.patch')).read_bytes()
        digest=tree_digest(self.task_file('make','attempt-1'))
        self.edit_brief()
        self.assertEqual(self.replan_here(),0,self.output)
        self.assertEqual((self.status('make'),self.status('after')),('pending','pending'))
        self.assertEqual(Path(self.record_path()).read_bytes(),before)
        rec=self.the_record()
        self.assertEqual((rec['attempt'],rec['paths']),(1,['src/a.txt','src/notes.txt']))
        self.assertEqual(rec['candidate'],self.git_out('rev-parse',rec['candidate_ref']))
        self.assertFalse(os.path.exists(self.task_file('after','set-aside.json')))  # nothing set aside
        self.assertEqual(Path(self.task_file('make','failed.patch')).read_bytes(),patch)
        self.assertEqual(tree_digest(self.task_file('make','attempt-1')),digest)
        self.assertEqual(self.the_run().integrity_check(),[])
        self.assertEqual(self.runner('retry','latest','make','--apply-patch','-C',self.root),0,self.output)
        self.script([GOOD,writes('after.txt','after\n')])
        self.assertEqual(self.resume(),0,self.output)
        self.assertIn('make: put back the set-aside work of attempt 1 (2 files)',self.output)
        self.assertEqual(self.git_out('show','HEAD:src/notes.txt'),'kept work')
        self.check_invariants()

    def legacy_round_trip(self,order,tasks,script):
        """A run the previous runner left, recovered in either order of `replan` and `retry`.
        Returns the record the set-aside published, for the caller's own checks."""
        self.workflow(tasks);self.script(script)
        self.assertEqual(self.start(),2,self.output)
        published=self.make_legacy()
        if order=='retry, then replan':
            self.old_retry()
        self.edit_brief()
        self.assertEqual(self.replan_here(),0,self.output)
        # Published before the reduction, from the base the reduction then dropped.
        self.assertTrue(os.path.exists(self.record_path()))
        derived=dict(published,at=None)
        if order=='retry, then replan':
            # The old retry already reset the task, and the derivation reads status and reason
            # from the state; everything the recovery uses is the same.
            derived.update(status='pending',reason='')
        self.assertEqual(record.read_json(self.record_path()),derived)
        self.assertEqual(self.the_run().integrity_check(),[])
        st=self.the_run().state['tasks']['make']
        self.assertNotIn('base',st);self.assertNotIn('apply_patch',st)
        if order=='retry, then replan':
            self.assertEqual(st['recover'],{'from':'set-aside','attempt':1,'candidate':published['candidate']})
            self.assertIn(QUEUED+'\n',self.output)
        else:
            self.assertNotIn('recover',st)
            self.assertEqual(self.runner('retry','latest','make','--apply-patch','-C',self.root),0,self.output)
        self.assert_put_back()
        return published

    def test_an_old_run_keeps_its_way_back_across_a_replan(self):
        """run: an old run keeps its way back across a replan (RUN-20)"""
        for order in ('replan, then retry','retry, then replan'):
            with self.subTest(order=order):
                self.setUp()
                published=self.legacy_round_trip(order,OTHER+MAKE,[writes('other/o.txt'),WORK])
                accepted=self.the_run().state['tasks']['other']['commit']
                self.assertEqual(published['head'],accepted)     # set aside after that acceptance
                self.doCleanups()

    def test_an_old_run_set_aside_on_the_starting_commit_recovers(self):
        """run: an old run set aside on the starting commit recovers (RUN-21)"""
        for order in ('replan, then retry','retry, then replan'):
            with self.subTest(order=order):
                self.setUp()
                published=self.legacy_round_trip(order,MAKE,[WORK])
                start_commit=self.the_run().info['base_commit']
                self.assertEqual(published['head'],start_commit)
                self.assertEqual(self.git_out('rev-parse',published['head']+'^{tree}'),published['base'])
                self.doCleanups()

    def test_a_migration_killed_between_file_and_manifest_is_repaired(self):
        """run: an old run keeps its way back across a replan (RUN-20: the replan is killed while
        it publishes the derived record, and `resume` finishes both)"""
        self.workflow(MAKE);self.script([WORK])
        self.assertEqual(self.start(),2,self.output)
        published=self.make_legacy()
        self.old_retry()
        self.edit_brief()
        class Killed(Exception):pass
        def crash(point):
            if point=='decision:file-written':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.replan_here()
        self.cli.CRASH=None
        kinds=[it['kind'] for it in self.the_run().state['intents']]
        self.assertEqual(kinds,['replan','decision'])
        self.assert_put_back()
        self.assertEqual(record.read_json(self.record_path()),dict(published,at=None,status='pending',reason=''))
        self.assertEqual(self.the_run().integrity_check(),[])


class InputIntegrity(EngineCase):
    def test_author_cannot_erase_accepted_input_evidence(self):
        """rec: accepted inputs are protected against a later author (REC-21)"""
        self.workflow(A + B)
        tamper = "find .runs -path '*/tasks/*-a/attempt-1/inputs.json' -exec sh -c 'echo {} > \"$1\"' sh {} \\;"
        self.script([writes('a.txt'), dict(writes('b.txt'), run=[tamper])])
        self.assertEqual(self.start(), 2, self.output)
        self.assertIn('inputs.json was changed', self.output)
        self.assertNotEqual(self.status('b'), 'accepted')
