# Performance rules a project switches on (C++)

Append this section to the project's one rules file (a task has one `rules_file`, and a reviewer
entry that names its own replaces the author's, so a second file would give the reviewers rules
the author never saw). It is for a project whose brief declares a hot path; a project without
one leaves it out, and every prompt stays shorter.

Where these rules apply. The code rules (`PF-C`) apply only to code on a path the brief of the work
under review names as hot. A rule marked "blocking on a hot path" is blocking there and advisory
everywhere else; the others are advisory everywhere. A number that this run did not measure is an
estimate, and a finding that rests on one is advisory. Whether an optimised candidate still does the
same thing is judged by the correctness reviewers (gatekeeper-gao, meticulous-mira, and the C++
correctness reviewers the workflow seats, such as concerned-carlos), never by this appendix. The
nearest persona raises a violation (allocation, copies, layout: neckbeard-nate, or nanosecond-nadia
where she sits; atomics and their ordering: memory-model-mei; the network card:
packet-trace-pradip) and the others cite its finding. The measurement requirements (`PF-M`, at the
end) apply to the optimise task of the optimisation workflow and are checked by its gates
(`tools/bench_contract.py` and the equivalence check); a reviewer cites them as the gate printed
them and does not re-derive them.

How to cite these rules. A reviewer's finding that rests on a rule starts its detail with the rule's
id (`CPP-02`, `PF-C08`); a finding that rests on no rule shows its consequence: what breaks, for
whom. No rule and no consequence: the finding is advisory. A rule's marking sets its severity: a
violation of a rule marked blocking may block, a violation of one marked advisory may not; citing a
Core Guidelines id or an advisory rule never makes a finding blocking; a reviewer may still block on
a consequence no rule names. An author who departs from a rule says which id and why. Ids are never
reused; a retired rule keeps its id, marked retired.

- **PF-C01 No heap allocation after warm-up (blocking on a hot path).** Reason: the allocator
  can take a lock or make a system call, and its time varies. Look for: `new`, `make_shared`,
  `push_back` without `reserve`, a `std::function` whose capture allocates, `std::string`
  concatenation. Exception: allocation at start-up, or behind a budget the brief declares.
- **PF-C02 No exception thrown in normal flow; error paths cold (blocking on a hot path).**
  Reason: a throw unwinds the stack and costs far more than a return. Look for: `throw` in a
  loop, `try` around the per-message path. Exception: a `throw` on a path taken only on a
  failure that ends the session.
- **PF-C03 A virtual call inside a hot loop is justified or removed.** Reason: an indirect
  branch the compiler cannot inline. Look for: `virtual` in a loop; suggest a template,
  `std::variant` over a closed set, or `final`.
- **PF-C04 Data written by different threads on separate cache lines (blocking on a hot
  path).** Reason: two writers on one line pass it back and forth (false sharing). Look for:
  `alignas(std::hardware_destructive_interference_size)`, or 64/128 bytes. Exception: data
  written only before the threads start.
- **PF-C05 Hot data dense; cold fields split out.** Reason: fewer cache lines and pages per
  message. Look for: struct layout, array of structs or struct of arrays, `sizeof`.
- **PF-C06 The common path is predictable; the error path is cold; any hint is measured.**
  Reason: a mispredicted branch costs a pipeline flush (varies by CPU, unmeasured), and a wrong
  hint makes it worse. Look for: `[[likely]]`, `[[unlikely]]`, `__builtin_expect`, `noinline`.
- **PF-C07 Each atomic uses the weakest correct order, with the pair it orders named.** Reason:
  a sequentially consistent store is a full fence on x86. Look for: `std::memory_order_*`,
  `std::atomic_thread_fence`. Whether the order is correct is memory-model-mei's to judge.
- **PF-C08 No mutex, condition variable or sleep on the critical path (blocking on a hot
  path).** Reason: blocking, priority inversion and wake-up latency. Look for: `std::mutex`,
  `wait` on a `std::condition_variable`, `sleep_for`. Exception: a blocking design the brief
  chose with its latency budget stated.
- **PF-C09 The waiting strategy is explicit.** Reason: spinning costs a core, blocking costs
  wake-up latency, and the brief should say which it pays. Look for: a polling loop with
  `_mm_pause` on an isolated core, or a blocking wait with its budget stated.
- **PF-C10 No system call on the hot path (blocking on a hot path).** Reason: a kernel entry
  takes time and pollutes the caches. Look for: `write`, `send` without kernel bypass,
  `gettimeofday`, logging that writes. Exception: a call the brief names with its cost
  budgeted, or one that does not enter the kernel (a vDSO clock read).
- **PF-C11 Memory prefaulted and locked; huge pages for large tables.** Reason: page faults and
  TLB misses land in the tail. Look for: `mlockall`, `MAP_POPULATE`, `madvise(MADV_HUGEPAGE)`.
- **PF-C12 Thread pinned; memory local to its core and network card.** Reason: remote memory
  is slower than local (varies by machine, unmeasured). Look for: `pthread_setaffinity_np`,
  `numactl`, `isolcpus`.
- **PF-C13 Latency timestamps from the TSC or the hardware, never the wall clock.** Reason: the
  wall clock jumps and its resolution and cost vary. Look for: `rdtscp`; `steady_clock`
  rather than `system_clock`.
- **PF-C14 Work that can be done at compile time or start-up is not done per message.** Reason:
  per-message parsing, formatting and hashing. Look for: `constexpr` tables; formatting on the
  hot path.
- **PF-C15 Bounded work per message; no rehash or resize on the hot path.** Reason: a rehash
  or a long loop is a spike in the tail. Look for: `std::unordered_map` growth, a loop over
  input with no bound.
- **PF-C16 Logging deferred to another thread, with its arguments copied in binary form.**
  Reason: formatting and I/O add jitter. Look for: a log call that formats or writes on the hot
  path; an asynchronous log ring.

Measurement requirements. Each is checked by the optimisation workflow's gates; a reviewer cites
it as the gate printed it and does not re-derive it. A rule marked "blocking on an optimise task"
is blocking on the optimise task of that workflow and applies nowhere else. Each row says what the
gate prints, so a finding quotes that line, or says the line is missing.

- **PF-M01 A committed baseline exists (blocking on an optimise task).** The baseline result of
  the accepted code, committed before the change and measured by the same benchmark command.
  Reason: without a baseline there is a number, not a claim. Exception: none. The gate prints:
  `PF-M01 baseline: present, N benchmark(s)`, or `bad input: PF-M01: … does not exist`.
- **PF-M02 The measurement is reproducible (blocking on an optimise task).** The benchmark runs
  from the gate command with fixed flags, its pinning stated (or "none"), with at least the
  required repetitions kept apart (one list of samples each); baseline and candidate name the same
  command and pinning. Reason: only a number the runner executed counts, and only a repeated one
  means anything. Exception: none. The gate prints: `PF-M02 command: …; pinning: …; repetitions:
  …`, or `bad input: PF-M02: …`.
- **PF-M03 The tail is reported, warm or cold stated (blocking on an optimise task).** p50, p99,
  p99.9 and max with the sample count, for baseline and candidate, and whether the code ran warm
  or cold, the same on both sides and as the brief names it. Reason: the mean hides the tail.
  Exception: none. The gate prints: `PF-M03 warm` (or `cold`) and the table `bench side unit
  count p50 p99 p99.9 max`, or `bad input: PF-M03: …`.
- **PF-M04 The regression threshold is stated and above the measured noise (blocking on an
  optimise task).** The baseline's spread between repetitions is below the threshold (the baseline
  gate refuses it otherwise). A candidate p50 or p99 worse by more than the threshold, with the
  bootstrap interval of the difference (resampled over repetitions) above zero, is a regression;
  when the noise is as large as the threshold the result is inconclusive, which passes the gate.
  An inconclusive result whose p50 or p99 is worse than the baseline by more than the threshold
  ("inconclusive, worse") blocks on an optimise task, as a claimed win in an inconclusive result
  does. Reason: a threshold inside the noise turns noise into failed tasks, into claimed wins and
  into slower code accepted. Session drift: when the gate re-measures the committed baseline in
  its own session, a drift of the threshold or more is refused at the statistic the workflow's
  `drift_stat` names (default p50, since a uniformly slower machine moves the p50 and the p99 of
  identical code is tail noise; `both` also refuses on p99), and the other is printed for
  information; the candidate is still judged on p50 and p99. Exception: none. The gate prints:
  `session drift: …` and `PF-M04 threshold: …`, in the
  baseline's gate `PF-M04 spread between …`, one line per benchmark and statistic ending
  `regression`, `inconclusive: …` or `pass: …`, and last `result: pass|regression|inconclusive|
  inconclusive, worse|bad input (exit N)`.
- **PF-M05 An equivalence test is present and passes (blocking on an optimise task).** The
  previous implementation kept as an oracle, or recorded inputs and outputs, written before the
  change and run against the candidate. Reason: a fast wrong system is worthless. Exception: none.
  The gate prints: the equivalence check's own output, in its own record rather than the optimise
  task's gate log; the contract script prints `PF-M05 equivalence: not checked here`.
