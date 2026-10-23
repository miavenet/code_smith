"""PRE-06: explicit text-only review receives complete evidence or does not run."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from helpers import RepoCase
from codesmith import agents, gitops, prompts

class TextOnly(RepoCase):
    def setUp(self):
        super().setUp()
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.git=gitops.Git(self.root)
        self.base=self.git.tree_of('HEAD')
        self.write('new.txt','the whole candidate\n')
        self.candidate=self.git.snapshot(os.path.join(self.tmp.name,'index'))

    def test_complete_evidence_and_explicit_label(self):
        evidence=prompts.provided_context(self.git,self.base,self.candidate,['README.md','new.txt'],100000)
        self.assertIn('the whole candidate',evidence[0])
        self.assertEqual(len(evidence[1]['files']),2)
        inv=os.path.join(self.tmp.name,'inv');os.mkdir(inv)
        answer={'verdict':'pass','summary':'checked','findings':[],'resolutions':[]}
        a=agents.make('reviewer',{'kind':'command','review_mode':'provided_context',
            'argv':[sys.executable,'-c','import sys;sys.stdin.read();print('+repr(json.dumps(answer))+')']})
        result=agents.review_call(a,'Review every supplied file.',invocation_dir=inv,
            evidence=evidence,evidence_cap_bytes=100000,cwd=self.root,model='',timeout_s=5,
            budget_usd=1,env=dict(os.environ))
        self.assertEqual(result.status,agents.OK)
        self.assertEqual(json.loads(Path(inv,'review-mode.json').read_text()),{'mode':'text-only'})
        self.assertEqual(json.loads(Path(inv,'evidence.json').read_text()),evidence[1])
        self.assertTrue(json.loads(Path(inv,'argv.json').read_text())['read_only'])

    def test_overflow_refuses_instead_of_truncating(self):
        with self.assertRaises(prompts.EvidenceTooLarge):
            prompts.provided_context(self.git,self.base,self.candidate,['new.txt'],10)
        evidence=prompts.provided_context(self.git,self.base,self.candidate,['new.txt'],100000)
        inv=os.path.join(self.tmp.name,'inv');os.mkdir(inv)
        a=agents.make('reviewer',{'kind':'command','review_mode':'provided_context','argv':['must-not-run']})
        with self.assertRaises(prompts.EvidenceTooLarge):
            agents.review_call(a,'large brief'*1000,invocation_dir=inv,evidence=evidence,
                evidence_cap_bytes=1000,cwd=self.root,model='',timeout_s=1,budget_usd=1,env={})
        self.assertFalse(Path(inv,'.adapter-started').exists())

if __name__=='__main__':
    unittest.main()
