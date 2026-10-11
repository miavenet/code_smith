"""Deterministic implementation of doctor's probe instructions for model-free tests."""
import json
from pathlib import Path
import subprocess
import sys


def handle_probe(prompt):
    try:
        data = json.loads(prompt)
    except ValueError:
        return False
    if not isinstance(data, dict) or 'code_smith_probe' not in data:
        return False
    probe, value = data['code_smith_probe'], 'done'
    if probe == 'answer':
        value = data['value']
        Path('.remember').write_text(value)
    elif probe == 'read':
        value = Path(data['path']).read_text()
    elif probe == 'execute':
        subprocess.run([sys.executable, 'execute-probe.py'], check=True)
    elif probe == 'write':
        Path(data['path']).write_text(data['contents'])
    elif probe == 'boundary':
        if '--read-only' not in sys.argv:
            Path(data['path']).write_text(data['contents'])
        value = 'denied'
    elif probe == 'resume':
        value = Path('.remember').read_text()
    print(json.dumps({'value': value}))
    return True


if __name__ == '__main__':
    if not handle_probe(sys.stdin.read()):
        sys.exit(97)
