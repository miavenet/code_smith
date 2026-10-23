#!/usr/bin/env python3
"""Run the unittest files in parallel, in shards smaller than a file.

    tools/run_tests.py [-j JOBS] [-n SHARD] [PATH ...]

PATH is a directory of test_*.py files or one test file (default: tests/ beside this tool).
Every file's tests are listed, grouped by class and cut into shards of at most SHARD tests
(default 10); each shard is its own `python3 -m unittest` process, started from the file's
directory, the largest first, JOBS at a time (default 8). A whole file as one process makes the
slowest file the wall time: the two largest files hold a third of the tests. Prints one line
per file and the totals; exits 1 if any shard fails or runs a different number of tests than
was listed.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LIST = ("import sys, unittest\n"
        "def walk(s):\n"
        "    for t in s:\n"
        "        if isinstance(t, unittest.TestSuite):\n"
        "            yield from walk(t)\n"
        "        else:\n"
        "            yield t\n"
        "for t in walk(unittest.defaultTestLoader.loadTestsFromName(sys.argv[1])):\n"
        "    print(t.id())\n")


def files_of(paths):
    found = []
    for path in paths:
        path = Path(path).resolve()
        found += sorted(path.glob("test_*.py")) if path.is_dir() else [path]
    return found


def shards_of(path, size, env):
    """[(file, [test id, ...])]: by class, in the file's order, at most `size` tests each. A file
    that cannot be listed (an import error) is one shard naming the module, so its error shows."""
    r = subprocess.run([sys.executable, "-c", LIST, path.stem], cwd=path.parent, env=env,
                       capture_output=True, text=True)
    ids = [line for line in r.stdout.split() if line.startswith(path.stem + ".")]
    if r.returncode or not ids or any("_FailedTest" in i for i in ids):
        return [(path, [path.stem], None)]
    by_class = {}
    for test in ids:
        by_class.setdefault(test.rsplit(".", 1)[0], []).append(test)
    return [(path, tests[i:i + size], len(tests[i:i + size]))
            for tests in by_class.values() for i in range(0, len(tests), size)]


def run_shard(shard, env):
    path, tests, expected = shard
    r = subprocess.run([sys.executable, "-m", "unittest", "-q", *tests], cwd=path.parent, env=env,
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    ran = re.search(r"^Ran (\d+) tests?", out, re.M)
    skipped = re.search(r"skipped=(\d+)", out)
    ran = int(ran.group(1)) if ran else 0
    ok = r.returncode == 0 and (expected is None or ran == expected)
    return path, ok, ran, int(skipped.group(1)) if skipped else 0, out


def main(argv=None):
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-j", dest="jobs", type=int, default=8)
    ap.add_argument("-n", dest="size", type=int, default=10)
    ap.add_argument("paths", nargs="*", default=[str(here.parent / "tests")])
    args = ap.parse_args(argv)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    started = time.time()
    files = files_of(args.paths)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        shards = [s for group in pool.map(lambda f: shards_of(f, args.size, env), files) for s in group]
        shards.sort(key=lambda s: -(s[2] or args.size))
        results = list(pool.map(lambda s: run_shard(s, env), shards))
    failed_files = total = skipped_total = 0
    for path in files:
        mine = [r for r in results if r[0] == path]
        ran, skipped = sum(r[2] for r in mine), sum(r[3] for r in mine)
        bad = [r for r in mine if not r[1]]
        total, skipped_total = total + ran, skipped_total + skipped
        print(f"  {'FAIL' if bad else 'ok  '} {path.stem}: {ran} tests"
              + (f", {skipped} skipped" if skipped else ""))
        if bad:
            failed_files += 1
            for r in bad:
                print("\n".join("      " + line for line in r[4].splitlines()[-40:]))
    print(f"tests: {len(files)} files, {total} tests, {skipped_total} skipped, "
          f"{failed_files} files failed, {len(shards)} shards, {time.time() - started:.0f}s")
    return 1 if failed_files else 0


if __name__ == "__main__":
    sys.exit(main())
