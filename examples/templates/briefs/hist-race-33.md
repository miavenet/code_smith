# Bug 33: TSan reports a data race in `LatencyHistogram` under the concurrent test

Reported by the build pipeline: the `tsan` preset fails about one run in four.

`LatencyHistogram` (`src/metrics/LatencyHistogram.hpp`) lets one sampling thread record
latencies into fixed buckets while a flush thread snapshots all buckets without a lock: the
sampler bumps a version counter, updates the buckets, bumps it again; the flusher takes the
version, copies the buckets, and retries if the version changed or was odd. Under
`cmake --preset tsan && cmake --build --preset tsan && ctest --preset tsan -R
'LatencyHistogram.*concurrent'` ThreadSanitizer reports, in a slightly different form each time:

```
==41217==ERROR: ThreadSanitizer: data race (pid=41217)
  Write of size 8 at 0x7b4000001040 by thread T2:
    #0 metrics::LatencyHistogram::record(Nanoseconds) src/metrics/LatencyHistogram.hpp:73
    #1 Sampler::run() tests/metrics/histogram_concurrent_test.cpp:41

  Previous read of size 8 at 0x7b4000001040 by thread T1:
    #0 metrics::LatencyHistogram::try_snapshot(Buckets&) const src/metrics/LatencyHistogram.hpp:112
    #1 Flusher::run() tests/metrics/histogram_concurrent_test.cpp:58

  Location is heap block of size 4096 at 0x7b4000001000 allocated by main thread:
    #0 operator new(unsigned long)
    #1 LatencyHistogramConcurrent_Test::TestBody() tests/metrics/histogram_concurrent_test.cpp:77

SUMMARY: ThreadSanitizer: data race src/metrics/LatencyHistogram.hpp:73 in metrics::LatencyHistogram::record
```

The bucket increment at line 73 and the bucket copy at line 112 are plain (non-atomic) accesses
by design; the version counter is a `std::atomic<std::uint64_t>` read and written with
`std::memory_order_relaxed` at lines 70, 75, 108 and 118. Nobody has yet shown whether the report
is the expected benign race of a version-stamped snapshot that the reader discards, or a real
torn read that the version check does not catch. The test's own assertion (every snapshot's
bucket total equals the number of samples recorded before it) has not failed in a thousand runs
under the plain preset, which proves nothing either way.

Reproduce under the `tsan` preset, through the public `record`/`try_snapshot` pair, with the
report's two sites and kind; the string `ThreadSanitizer: data race` is the symptom. Say for each
atomic in the two stacks which memory order it uses and which pair, if any, synchronises.
