#!/usr/bin/env python3
"""Example project gate: a direct include, latency units, and rejected bad arguments.

Copy into the target project and adapt the literals to its brief. The runner only sees a command.
"""
import argparse
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--program', nargs='+', required=True)
    args = parser.parse_args()
    try:
        source = Path(args.source).read_text()
        if not re.search(r'^\s*#\s*include\s*"testing/doctest.hpp"\s*$', source, re.M):
            raise ValueError('required direct include: testing/doctest.hpp')
        result = subprocess.run(args.program + ['10'], capture_output=True, text=True, timeout=5)
        print(result.stdout, end='')
        lines = result.stdout.splitlines()
        latency = [line for line in lines if line.startswith('latency:')]
        if result.returncode or len(latency) != 1 or not re.fullmatch(
                r'latency: [0-9]+(?:\.[0-9]+)? ns/op', latency[0]):
            raise ValueError('expected one latency line ending in a number and ns/op')
        print('evidence: fast path (argument 10) checked; default path not exercised')
        for value in ('0', 'invalid', '10oops', '-1', '18446744073709551616'):
            result = subprocess.run(args.program + [value], capture_output=True, text=True, timeout=5)
            if result.returncode <= 0:
                raise ValueError(f'{value!r} must be rejected with a nonzero exit')
    except subprocess.TimeoutExpired as exc:
        print(f'contract gate: timed out checking {exc.cmd!r}; completion and output not verified; '
              'default path not exercised')
        return 1
    except (OSError, ValueError) as exc:
        print(f'contract gate: {exc}')
        return 1
    print('contract gate: passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
