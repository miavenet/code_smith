"""AGENT-13/14: streamed output survives termination; no gate process outlives shutdown."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from helpers import RepoCase, RUNNER
from codesmith import record

class Shutdown(RepoCase):
    def test_signal_during_gate_streams_logs_and_stops_descendants(self):
        code = ('import subprocess,sys,time,signal\n'
                'child=subprocess.Popen([sys.executable,"-c",'
                '"import signal,time;signal.signal(signal.SIGINT,signal.SIG_IGN);'
                'signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(600)"])\n'
                'print("CHILD",child.pid,flush=True)\n'
                'for i in range(2048): print("x"*512,file=sys.stderr,flush=True)\n'
                'print("READY",flush=True)\ntime.sleep(600)\n')
        self.write('gate.py', code)
        wf = self.write('wf.toml', 'name="shutdown"\n[[task]]\nid="gate"\ntype="check"\nrun=["python3 gate.py"]\n')
        self.commit()
        runner = subprocess.Popen([sys.executable,RUNNER,'start',wf], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        deadline = time.monotonic()+10
        log = None
        while time.monotonic()<deadline:
            logs = list(Path(self.root,'.runs').glob('shutdown/*/tasks/*/attempt-*/gate.log'))
            if logs and b'READY' in logs[0].read_bytes():
                log=logs[0]
                break
            time.sleep(.02)
        self.assertIsNotNone(log)
        self.assertGreater(log.stat().st_size, 1024*1024)
        run = record.Run.load(record.resolve_run(str(Path(self.root,'.runs'))))
        intent=next(i for i in run.state['intents'] if i['kind']=='command')
        identity=intent['process']
        self.addCleanup(record.stop_process_group,identity,.1)
        child=int(next(line.split()[1] for line in log.read_text().splitlines() if line.startswith('CHILD ')))
        runner.send_signal(signal.SIGTERM)
        stdout,stderr=runner.communicate(timeout=15)
        self.assertEqual(runner.returncode,2,stderr)
        self.assertIn(b'interrupted',stderr)
        self.assertFalse(record.is_alive(identity))
        try:
            state=Path(f'/proc/{child}/stat').read_text().rsplit(')',1)[1].split()[0]
        except FileNotFoundError:
            state='gone'
        self.assertIn(state,('gone','Z'))
        self.assertGreater(log.stat().st_size,1024*1024)

if __name__=='__main__':
    unittest.main()
