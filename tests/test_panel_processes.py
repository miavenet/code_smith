"""Parallel reader shutdown and orphan recovery use real operating-system process groups."""
import os
import signal
import subprocess
import sys
import time
from helpers import EngineCase, RUNNER
from test_panels import ONE, GOOD, PASS
from codesmith import record, workflow


class PanelProcesses(EngineCase):
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'

    def interrupt_panel(self, sig):
        self.workflow(ONE)
        ids=[t['id'] for t in workflow.load(self.wf_path).tasks if t['kind']=='review']
        self.script({'make':[GOOD],**{rid:[{'hang':True},PASS] for rid in ids}})
        runner=subprocess.Popen([sys.executable,RUNNER,'start',self.wf_path],
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        identities=[]
        deadline=time.monotonic()+45       # a poll that ends early; the bound only matters on a loaded machine
        while time.monotonic()<deadline:
            try:
                run=self.the_run()
                intents=[i for i in run.state['intents'] if i['kind']=='agent' and i['task'] in ids]
                identities=[i['process'] for i in intents if i.get('process')]
                # Wait for both agents to consume their scripted hang steps, not just to spawn.
                consumed=all(os.path.exists(self.script_path+'.'+rid+'.counter') for rid in ids)
                if len(identities)==2 and consumed:break
            except (record.RecordError,FileNotFoundError):pass
            time.sleep(.02)
        self.assertEqual(len(identities),2)
        for identity in identities:self.addCleanup(record.stop_process_group,identity,.1)
        runner.send_signal(sig)
        _out,err=runner.communicate(timeout=15)
        if sig==signal.SIGTERM:
            self.assertEqual(runner.returncode,2,err)
            self.assertIn(b'interrupted',err)
            self.assertTrue(all(not record.is_alive(identity) for identity in identities))
        else:
            self.assertEqual(self.resume(),2,self.output)
            self.assertIn('still running',self.output)
        self.assertEqual(self.resume('--stop-orphans'),0,self.output)
        self.assertTrue(all(not record.is_alive(identity) for identity in identities))
        self.assertEqual(self.the_run().state['tasks']['make']['attempts_used'],1)
        self.check_invariants()

    def test_sigterm_stops_all_readers_and_resume_keeps_candidate(self):
        self.interrupt_panel(signal.SIGTERM)

    def test_sigkill_refuses_live_orphans_then_stops_them_before_resume(self):
        self.interrupt_panel(signal.SIGKILL)
