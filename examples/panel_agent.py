"""Scripted panel demonstration. No model calls; --dispute exercises human escalation."""
import json
import os
from pathlib import Path
import sys

from command_agent import handle_probe

prompt = sys.stdin.read()
if handle_probe(prompt):
    raise SystemExit(0)

tid = os.environ['CODE_SMITH_TASK']
if '--read-only' in sys.argv:
    answer = {'verdict': 'pass', 'summary': 'Reviewed the demonstration.', 'findings': [], 'resolutions': []}
    if tid.endswith('principled-priya'):
        if 'make/PE-1' in prompt:
            resolved = Path('result.txt').read_text() == 'Verified candidate\n'
            answer['resolutions'] = [{'finding': 'make/PE-1',
                                     'status': 'resolved' if resolved else 'unresolved',
                                     'note': 'Checked result.txt.'}]
            answer['verdict'] = 'pass' if resolved else 'block'
        else:
            answer['verdict'] = 'block'
            answer['findings'] = [{'severity': 'blocking', 'title': 'Use the final wording',
                                  'detail': 'result.txt still contains the draft wording.',
                                  'location': 'result.txt:1', 'caused_by': ''}]
else:
    responses = []
    if 'make/PE-1' in prompt:
        disputed = '--dispute' in sys.argv
        if not disputed:
            Path('result.txt').write_text('Verified candidate\n')
        responses = [{'finding': 'make/PE-1', 'action': 'disputed' if disputed else 'fixed',
                      'note': 'The draft is intentional.' if disputed else 'Updated the wording.'}]
    else:
        Path('result.txt').write_text('Draft candidate\n')
    answer = {'outcome': 'done', 'summary': 'Wrote result.txt.', 'blocked_reason': '', 'responses': responses}
print(json.dumps(answer))
