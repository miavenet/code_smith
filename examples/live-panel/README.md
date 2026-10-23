# Real-agent panel acceptance check

Copy `workflow.toml` and `draft.md` into a fresh scratch Git repository and commit
them. Run the checkout's `runner doctor workflow.toml`, then `start`.
The workflow uses Opus 5. Its writer explicitly uses `bypassPermissions`; its
reviewers use `auto` and the adapter's read/search-only tool list. Change the
writer to `auto` when explicit permission controls are needed, then run `doctor`
again. Agent calls consume provider usage.

The initial draft deliberately accepts bool. The author imports it as the first
candidate; the spec reviewer has the exact-integer acceptance contract. Expect
blocking findings, author responses, a second review round, implementation with
unittest gates, a run summary and a human sign-off. This is a test fixture, not a
recommended integer API design.

For native Claude observability, also copy the checkout's `.claude/settings.json`
and `.claude/hooks/log-hook.py` into the scratch repository and commit them. Add
`.claude/hook-logs/` and `__pycache__/` to its `.gitignore` before starting.
`runner activity latest` then shows headless tool activity.

Inspect `design.md`, `adder.py`, `test_adder.py`, `summary.md`, and the run's
findings ledger before approving the final human task. `approve` records a real
operator decision; the runner never invents one.
