# Security rules a project switches on

Append this section to the project's one rules file (a task has one `rules_file`, and a reviewer
entry that names its own replaces the author's, so a second file would give the reviewers rules
the author never saw). It is for work that reads input it does not control, keeps a secret, or
has callers with different rights; a library with trusted callers and no such input leaves it out.
Reviewers judge "untrusted" only by the design's trust section, the brief or these rules, never
by assumption; the nearest persona raises a violation (`sentinel-sato` where the project seats
it, otherwise `principled-priya`; memory and bounds: `concerned-carlos`; the repository and CI:
`dependable-diego`) and the others cite its finding. Every blocker cites a rule, the source-to-sink path
with `path:line` at each hop, the smallest input that shows the effect, and a way to confirm it by
reading, in under five minutes. Never copy a secret value into a finding.

- **SEC-1 Declare the boundary (blocking at design).** A design that reads input from outside
  the process has a "Trust boundaries" section: each input, who controls it, its maximum size,
  count and rate, and what happens on hostile input. A design with none says "no input from
  outside the process".
- **SEC-2 Bound before use (blocking).** A length, count, offset, depth or size read from
  untrusted input is checked against a stated maximum before it sizes an allocation, indexes
  memory, bounds a loop or recursion, grows a retained container, or triggers a request to
  another system.
- **SEC-3 No shell, no eval, no unsafe loaders (blocking).** Python: `subprocess` with an
  argument list, never `shell=True` with an interpolated value; no `eval`, `exec`, `pickle`,
  `marshal` or `yaml.load` without `SafeLoader` on data this process did not write. C++: no
  `system`/`popen` with built strings; no format string that is not a literal.
- **SEC-4 Paths confined after resolution (blocking).** A path built from untrusted input is
  resolved (`os.path.realpath`, `std::filesystem::weakly_canonical`) and checked to lie under its
  root before use. In a directory other users can write, open without following links and act
  on the opened handle, not the name.
- **SEC-5 Temporary files (blocking).** `tempfile.mkstemp`, `NamedTemporaryFile`, `mkdtemp`, or
  `mkstemp(3)`; never a predictable name in a shared directory.
- **SEC-6 Secrets at runtime (blocking).** No credential, token or key in source, tests,
  fixtures, logs, error messages, command-line arguments, or files created with default
  permissions. Credentials are read from the environment or a file at runtime, and a child
  process gets only the environment variables built for it.
- **SEC-7 Authority at the action (blocking where the brief has callers with different
  rights).** Credentials are verified before an identity is relied on, and that identity is
  authorised for the object and the operation requested; a valid identifier or a successful
  login alone is not authority. Failed checks never reach the protected effect.
- **SEC-8 Randomness and comparison (blocking).** Anything an attacker must not guess comes from
  `secrets`, `os.urandom` or the OS generator, never `random`, `std::rand` or `std::mt19937`.
  Secrets are compared with `hmac.compare_digest` or a constant-time function. No home-made
  cryptography: a maintained library's high-level API.
- **SEC-9 Errors to outside callers (advisory).** What an outside caller sees carries no stack
  trace, absolute path or internal state; the full detail goes to the log, minus secrets.
- **SEC-10 Dependencies (advisory; blocking for a build step that downloads and runs code).**
  A new dependency is pinned by version and lock file or checksum, and the summary says why it
  is needed. A CVE blocks only with evidence that the selected version and the reached call are
  affected; a remembered advisory is not evidence.

Not a violation: hardening with no reachable attacker path (compiler flags, sandboxing, defence
in depth), a weak hash on a cache key, a check-then-open on a file in the user's own
configuration directory. Those are advisory at most.
