# Documentation checks

## Mermaid

Run `python3 tools/lint_mermaid.py` from the repository root with the official `mmdc`
CLI on `PATH` to render-check every diagram in `docs/tutorial/` and `docs/runbook.md`, the default paths. Pass other Markdown files or directories as arguments to check them too. Add `--fix` for the narrowly
defined safe repairs, or `--structure-only` when `mmdc` is unavailable (that mode does not validate
Mermaid syntax).

For the version used to verify these diagrams, install
`npm install --prefix /tmp/code-smith-mermaid @mermaid-js/mermaid-cli@11.12.0` and pass
`--mmdc /tmp/code-smith-mermaid/node_modules/.bin/mmdc`. The fixer only normalizes Mermaid fences,
replaces ambiguous colons in Gantt labels, and adds contrasting text to hexadecimal class fills;
it reports anything it cannot repair without guessing.

## Tests

`python3 tools/run_tests.py [-j JOBS] [-n SHARD] [PATH ...]` runs the unittest files
in parallel shards of at most SHARD tests (default 10), each its own process started from the
test file's directory, JOBS at a time (default 8). A file per process makes the largest file
the wall time; shards make it the machine. It fails if a shard fails or runs a different number
of tests than were listed.

## Fault drill

`python3 tools/fault_drill.py [N ...] [--keep]` runs the failure paths through the
command line on scratch Git repositories, with the scripted agents only (no model or network
call): a runner killed during an author call, an orphaned child that ignores SIGTERM, a gate that
runs `git clean -fdx`, a gate that deletes `.runs/.gitignore`, a frozen file edited while the run
waits, a provider refusal before any work under a token cap, a renderer that raises, and
`pause --now` three times in one attempt. It prints one line per drill, `ok` or `FAIL` with the
evidence it read from the record, and exits 1 on any failure. `--keep` keeps the scratch
repositories. About two minutes.
