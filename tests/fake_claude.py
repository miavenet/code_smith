"""Scripted Claude terminal envelopes around fake_agent; no network or model calls."""
import contextlib
import io
import json
import sys

import fake_agent


def main():
    if sys.argv[1:] == ['--version']:
        print('Claude Code (scripted repair tests)')
        return 0
    # The same boundary probe used by the command fake recognizes this spelling.
    if '--disallowedTools' in sys.argv:
        sys.argv.append('--read-only')
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = fake_agent.main()
    text = output.getvalue().strip()
    if text == 'NO_TERMINAL':
        print(json.dumps({'type': 'assistant', 'content': 'unfinished'}))
        return code
    session = sys.argv[sys.argv.index('--resume') + 1] if '--resume' in sys.argv else 'scripted-session'
    try:
        answer = json.loads(text)
    except ValueError:
        answer = None
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                      'result': text, 'structured_output': answer, 'session_id': session,
                      'total_cost_usd': 0.3 if '--resume' in sys.argv else 0.2,
                      'usage': {'input_tokens': 10, 'output_tokens': 5}}))
    return code


if __name__ == '__main__':
    sys.exit(main())
