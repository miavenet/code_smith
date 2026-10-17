#!/usr/bin/env python3
"""The mechanical half of the measurement requirements PF-M01 to PF-M04 of
benches/performance.md, as a gate. Standard library only.

Copy it into the target project as tools/bench_contract.py (tools/** is protected in every
shipped template, and a gate's own script is protected by name). Two modes:

  bench_contract.py --validate FILE --repetitions N [--threshold-pct P]
      FILE is a well-formed result (PF-M01 to PF-M03), and prints the spread between its
      repetitions; with --threshold-pct, a spread not below the threshold is refused (PF-M04).
      Exit 0, or 2.

  bench_contract.py BASELINE CANDIDATE --threshold-pct P --repetitions N [--budget-p99-ns NS]
                    [--session-ref FILE]
      Compare the candidate's p50 and p99 with the baseline's (PF-M04). Exit 0 pass,
      1 regression, 2 bad input, 3 inconclusive.

Session drift. The baseline was measured in another session (another time, temperature, load),
and a machine that runs a few per cent slower today shifts every repetition alike, which no
interval computed from today's repetitions can see. `--session-ref FILE` names the committed
baseline code measured again in this session, just before the candidate; the candidate is then
judged against it, not against the committed baseline, so the two numbers compared come from one
session, and the drift between the committed baseline and the reference is printed for
information. When that drift reaches the threshold at p50 or p99, in either direction, the
comparison is refused (exit 2: re-baseline or run on a quiet machine, then `runner retry RUN
optimise`). Without it (an empty FILE), one line says session drift is not checked and the
candidate is judged against the committed baseline: a limitation of the pilot.

A result file is one JSON object:

  {"command": "build/bench_book --benchmark_repetitions=10",  # PF-M02: what produced it
   "pinning": "taskset -c 3",                                 # PF-M02: stated, or "none"
   "warm": true,                                              # PF-M03: warm (true) or cold
   "benchmarks": [{"bench": "update", "unit": "ns",
                   "runs": [[412, 398, ...], [405, 411, ...], ...]}]}

`runs` holds one list per repetition (PF-M02: at least --repetitions of them), each one latency
per measured operation. The repetitions are kept apart because the noise between them
(frequency, temperature, load) is the noise that matters, and pooled samples hide it. Google
Benchmark's JSON is not read directly: it reports one mean per repetition, from which no p99 of
the operation's latency can be computed. A project converts its histogram or its per-operation
timings into this shape.

The comparison. For each benchmark and each of p50 and p99, the difference (candidate minus
baseline) and its 95% interval from a bootstrap over the repetitions: each resample draws whole
repetitions with replacement, so the interval carries the spread between runs as well as within
them (seeded, so the same files always give the same answer). A bootstrap, not a rank test: a
Mann-Whitney test asks whether the whole distribution moved, which can miss a tail that grew
under an unchanged median. A statistic is a regression when it is worse by more than the
threshold AND its interval lies above zero; the candidate is a regression when either is. When
the noise (half the interval's width) is as large as the threshold, or a statistic is worse by
more than the threshold while its interval still reaches zero, the answer is inconclusive, never
a failure: a noisy machine must not fail an honest task. An inconclusive statistic whose point
estimate is worse by more than the threshold is reported "inconclusive, worse", and the result
line says so: the gate passes it, and on an optimise task the performance reviewer blocks on it.
Work is bounded: `--bootstrap` resamples of each side, each a sort of its samples.

The output ends with the summary table, one verdict line per statistic and one result line,
because a reviewer is shown only the last 8 KiB of a gate's output. A gate passes on exit 0 and
fails on any other, so a workflow that lets an inconclusive comparison pass writes the gate as
`python3 tools/bench_contract.py ... || { code=$?; [ $code -eq 3 ] || exit $code; }`: exit 3
passes, every other exit is kept, and the reviewer reads "inconclusive" in the evidence. PF-M05
(the equivalence test) is not checked here: it is a command of its own, run by a check.
"""

import argparse
import json
import math
import random
import statistics
import sys

PASS, REGRESSION, BAD_INPUT, INCONCLUSIVE = 0, 1, 2, 3
WORDS = {PASS: "pass", REGRESSION: "regression", BAD_INPUT: "bad input",
         INCONCLUSIVE: "inconclusive"}
MIN_SAMPLES = 100              # fewer, and the p99 is the maximum
STATS = (("p50", 0.50), ("p99", 0.99))


class BadInput(Exception):
    pass


def percentile(ordered, q):
    """Nearest-rank percentile of a sorted list."""
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def pooled(runs):
    return [s for run in runs for s in run]


def summary(samples):
    ordered = sorted(samples)
    return {"count": len(ordered), "p50": percentile(ordered, 0.50),
            "p99": percentile(ordered, 0.99), "p99.9": percentile(ordered, 0.999),
            "max": ordered[-1]}


def spread(runs, q):
    """Half the range of one statistic between the repetitions, as a percentage of its median."""
    values = [percentile(sorted(run), q) for run in runs]
    middle = statistics.median(values) or 1
    return 100.0 * (max(values) - min(values)) / 2 / middle


def load(path, repetitions):
    """The result file at `path`, checked against PF-M01 to PF-M03. Raises BadInput."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise BadInput(f"PF-M01: {path} does not exist")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadInput(f"PF-M01: {path} is not a JSON result file: {exc}")
    if not isinstance(data, dict):
        raise BadInput(f"{path}: a result file is one JSON object")
    for key in ("command", "pinning"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise BadInput(f"PF-M02: {path} does not state its {key}")
    if not isinstance(data.get("warm"), bool):
        raise BadInput(f"PF-M03: {path} does not state warm (true) or cold (false)")
    benches = data.get("benchmarks")
    if not isinstance(benches, list) or not benches:
        raise BadInput(f"{path}: 'benchmarks' must be a non-empty list")
    seen = {}
    for b in benches:
        if not isinstance(b, dict) or not isinstance(b.get("bench"), str) or not b["bench"]:
            raise BadInput(f"{path}: every benchmark needs a 'bench' name")
        name = b["bench"]
        if name in seen:
            raise BadInput(f"{path}: benchmark '{name}' appears twice")
        if not isinstance(b.get("unit"), str) or not b["unit"]:
            raise BadInput(f"{path}: benchmark '{name}' does not state its unit")
        runs = b.get("runs")
        if not isinstance(runs, list) or not all(isinstance(r, list) and r for r in runs):
            raise BadInput(f"PF-M02: {path}: benchmark '{name}': 'runs' must hold one non-empty "
                           "list of samples per repetition")
        if len(runs) < repetitions:
            raise BadInput(f"PF-M02: {path}: benchmark '{name}' has {len(runs)} repetitions; at "
                           f"least {repetitions} are required")
        if not all(isinstance(s, (int, float)) and not isinstance(s, bool)
                   and math.isfinite(s) and s >= 0 for r in runs for s in r):
            raise BadInput(f"{path}: benchmark '{name}': samples must be non-negative numbers")
        count = sum(len(r) for r in runs)
        if count < MIN_SAMPLES:
            raise BadInput(f"PF-M03: {path}: benchmark '{name}' has {count} samples; a p99 "
                           f"needs at least {MIN_SAMPLES}")
        seen[name] = b
    data["by_name"] = seen
    data["repetitions"] = min(len(b["runs"]) for b in seen.values())
    return data


def resample(runs, rng):
    """One bootstrap draw: whole repetitions, with replacement, pooled and sorted."""
    return sorted(s for run in rng.choices(runs, k=len(runs)) for s in run)


def compare(base, cand, threshold, resamples, rng):
    """[(decision, worse, line)] for one benchmark, one per statistic: decision is PASS,
    REGRESSION or INCONCLUSIVE; worse says the point estimate is worse than the threshold."""
    b_sum, c_sum = summary(pooled(base["runs"])), summary(pooled(cand["runs"]))
    diffs = {name: [] for name, _q in STATS}
    for _ in range(resamples):
        rb, rc = resample(base["runs"], rng), resample(cand["runs"], rng)
        for name, q in STATS:
            diffs[name].append(percentile(rc, q) - percentile(rb, q))
    out = []
    for name, _q in STATS:
        ref = b_sum[name] or 1
        ds = sorted(diffs[name])
        low, high = percentile(ds, 0.025), percentile(ds, 0.975)
        delta = 100.0 * (c_sum[name] - b_sum[name]) / ref
        noise = 100.0 * (high - low) / 2 / ref
        worse = delta > threshold
        if worse and low > 0:
            decision, why = REGRESSION, "regression"
        elif noise >= threshold:
            decision, why = INCONCLUSIVE, "inconclusive: the noise is as large as the threshold"
        elif worse:
            decision, why = INCONCLUSIVE, "inconclusive: worse, but within the noise"
        elif delta < -threshold and high < 0:
            decision, why = PASS, "pass: faster"
        else:
            decision, why = PASS, "pass: no change beyond the threshold"
        if decision == INCONCLUSIVE and worse and "worse" not in why:
            why += "; worse by more than the threshold"
        line = (f"{base['bench']}  {name} {delta:+.1f}% (95% interval {100.0 * low / ref:+.1f}% "
                f".. {100.0 * high / ref:+.1f}%; noise ±{noise:.1f}%; threshold {threshold:g}%): "
                f"{why}")
        out.append((decision, worse and decision == INCONCLUSIVE, line))
    return out


def table(rows):
    head = ("bench", "side", "unit", "count", "p50", "p99", "p99.9", "max")
    out = [head] + [tuple(str(c) for c in r) for r in rows]
    widths = [max(len(r[i]) for r in out) for i in range(len(head))]
    return ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in out]


def rows_of(label, data, name):
    b = data["by_name"][name]
    s = summary(pooled(b["runs"]))
    return (name, label, b["unit"], s["count"], s["p50"], s["p99"], s["p99.9"], s["max"])


def finish(code, lines, worse=False):
    word = "inconclusive, worse" if code == INCONCLUSIVE and worse else WORDS[code]
    lines.append(f"result: {word} (exit {code})")
    print("\n".join(lines))
    return code


def spread_lines(data, threshold=None):
    """The spread between repetitions of each benchmark; BadInput when a threshold is given and
    the p99 spread is not below it (PF-M04: the threshold must sit above the measured noise)."""
    lines = []
    for name, b in data["by_name"].items():
        p50, p99 = spread(b["runs"], 0.50), spread(b["runs"], 0.99)
        lines.append(f"PF-M04 spread between {len(b['runs'])} repetitions of {name}: "
                     f"p50 ±{p50:.1f}%, p99 ±{p99:.1f}%")
        if threshold is not None and max(p50, p99) >= threshold:
            raise BadInput(f"PF-M04: the spread between repetitions of '{name}' (p50 ±{p50:.1f}%, "
                           f"p99 ±{p99:.1f}%) is not below the threshold ({threshold:g}%)")
    return lines


def validate(path, repetitions, threshold):
    lines = [f"bench_contract: validate {path}"]
    try:
        data = load(path, repetitions)
        spreads = spread_lines(data, threshold)
    except BadInput as exc:
        return finish(BAD_INPUT, lines + [f"bad input: {exc}"])
    lines += [f"PF-M01 result file: present, {len(data['by_name'])} benchmark(s)",
              f"PF-M02 command: {data['command']}; pinning: {data['pinning']}; "
              f"repetitions: {data['repetitions']} (at least {repetitions})",
              f"PF-M03 {'warm' if data['warm'] else 'cold'}; percentiles below"]
    lines += spreads + [""]
    return finish(PASS, lines + table([rows_of("file", data, n) for n in data["by_name"]]))


def session_drift(base, path, repetitions, threshold):
    """(line, reference): the line that reports session drift, and the session reference the
    candidate is judged against (None without one); BadInput when the baseline code
    re-measured in this session (`path`) differs from the committed baseline by the threshold
    or more."""
    if not path:
        return ("session drift: not checked (no --session-ref: the committed baseline was not "
                "re-measured in this session; the candidate is judged against it)"), None
    ref = load(path, repetitions)
    for key in ("command", "pinning", "warm"):
        if base[key] != ref[key]:
            raise BadInput(f"session drift: the {key} of the session reference differs from the "
                           "committed baseline's")
    if set(base["by_name"]) != set(ref["by_name"]):
        raise BadInput("session drift: the session reference measures other benchmarks")
    worst = []
    for name in sorted(base["by_name"]):
        b, r = summary(pooled(base["by_name"][name]["runs"])), \
            summary(pooled(ref["by_name"][name]["runs"]))
        for stat in ("p50", "p99"):
            shift = 100.0 * (r[stat] - b[stat]) / (b[stat] or 1)
            worst.append((abs(shift), shift, name, stat))
    _size, shift, name, stat = max(worst)
    if abs(shift) >= threshold:
        raise BadInput(f"session drift: the baseline re-measured in this session differs from "
                       f"the committed baseline by {shift:+.1f}% at {name} {stat} (threshold "
                       f"{threshold:g}%); re-baseline or run on a quiet machine, then "
                       "`runner retry RUN optimise`")
    return (f"session drift: the baseline re-measured in this session is within "
            f"{abs(shift):.1f}% of the committed one (largest: {name} {stat}, {shift:+.1f}%); "
            "the candidate is judged against the session reference"), ref


def contract(args):
    lines = [f"bench_contract: baseline {args.baseline}, candidate {args.candidate}"]
    try:
        base = load(args.baseline, args.repetitions)
        cand = load(args.candidate, args.repetitions)
        for key in ("command", "pinning", "warm"):
            if base[key] != cand[key]:
                raise BadInput(f"PF-M02/PF-M03: the {key} differs: baseline {base[key]!r}, "
                               f"candidate {cand[key]!r}; the same benchmark must measure both")
        if set(base["by_name"]) != set(cand["by_name"]):
            raise BadInput("the baseline and the candidate measure different benchmarks: "
                           f"{sorted(base['by_name'])} and {sorted(cand['by_name'])}")
        for name, b in base["by_name"].items():
            if b["unit"] != cand["by_name"][name]["unit"]:
                raise BadInput(f"benchmark '{name}': the unit differs")
        drift, ref = session_drift(base, args.session_ref, args.repetitions,
                                   args.threshold_pct)
    except BadInput as exc:
        return finish(BAD_INPUT, lines + [f"bad input: {exc}"])
    lines += [f"PF-M01 baseline: present, {len(base['by_name'])} benchmark(s)",
              f"PF-M02 command: {base['command']}; pinning: {base['pinning']}; repetitions: "
              f"{base['repetitions']} and {cand['repetitions']} (at least {args.repetitions})",
              f"PF-M03 {'warm' if base['warm'] else 'cold'}; percentiles below",
              f"PF-M04 threshold: {args.threshold_pct:g}% on p50 and p99; noise: bootstrap 95% "
              f"interval over repetitions, {args.bootstrap} resamples, seed {args.seed}",
              "PF-M05 equivalence: not checked here; the equivalence check runs it", drift]
    judge = ref or base                  # one session: the reference when there is one
    rng = random.Random(args.seed)
    decisions, verdicts = [], []
    for name in sorted(base["by_name"]):
        for decision, worse, line in compare(judge["by_name"][name], cand["by_name"][name],
                                             args.threshold_pct, args.bootstrap, rng):
            decisions.append((decision, worse))
            verdicts.append(line)
    if args.budget_p99_ns:
        for name in sorted(cand["by_name"]):
            b = cand["by_name"][name]
            if b["unit"] != "ns":
                lines.append(f"budget p99 <= {args.budget_p99_ns} ns: not compared for '{name}' "
                             f"(unit {b['unit']})")
                continue
            p99 = summary(pooled(b["runs"]))["p99"]
            state = "met" if p99 <= args.budget_p99_ns else "EXCEEDED"
            lines.append(f"budget p99 <= {args.budget_p99_ns} ns: {state} ({name} candidate "
                         f"p99 {p99} ns)")
    else:
        lines.append("budget: none declared")
    rows = []
    for name in sorted(base["by_name"]):
        rows += [rows_of("baseline", base, name)]
        rows += [rows_of("session", ref, name)] if ref else []
        rows += [rows_of("candidate", cand, name)]
    lines += [""] + table(rows) + verdicts
    codes = [d for d, _w in decisions]
    code = (REGRESSION if REGRESSION in codes else
            INCONCLUSIVE if INCONCLUSIVE in codes else PASS)
    return finish(code, lines, worse=any(w for _d, w in decisions))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("baseline", nargs="?")
    p.add_argument("candidate", nargs="?")
    p.add_argument("--validate", metavar="FILE")
    p.add_argument("--threshold-pct", type=float)
    p.add_argument("--repetitions", type=int, required=True)
    p.add_argument("--budget-p99-ns", type=int, default=0)
    p.add_argument("--bootstrap", type=int, default=400)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--session-ref", metavar="FILE", default="")
    args = p.parse_args(argv)
    if args.threshold_pct is not None and not args.threshold_pct > 0:
        p.error("--threshold-pct must be above zero (PF-M04)")
    if args.validate:
        if args.baseline or args.candidate:
            p.error("--validate takes one file and no BASELINE CANDIDATE")
        return validate(args.validate, args.repetitions, args.threshold_pct)
    if not (args.baseline and args.candidate):
        p.error("give BASELINE and CANDIDATE, or --validate FILE")
    if args.threshold_pct is None:
        p.error("--threshold-pct must be stated and above zero (PF-M04)")
    if args.bootstrap < 100:
        p.error("--bootstrap must be at least 100")
    return contract(args)


if __name__ == "__main__":
    sys.exit(main())
