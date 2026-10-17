# Brief: a faster update path for the book

The L2 order book in `src/book/` is accepted: its design is
`docs/design/05-order-book.md` and its tests in `tests/book/` pass. Its design requires no
allocation on the update path once the book is warm, and the accepted book already allocates
nothing there; this task keeps it so.

Make the update path (add, reduce, delete on a warm book) faster at the p99 of an update, without
changing what any operation or query returns, reports or orders, and without adding an
allocation to it. The capacity of a warm book is the one the design states; growing past it may
allocate, and is reported as it is today.

Measure with the benchmark and against the baseline the baseline task committed. The runner
measures again after you finish: its gate's lines are what the reviewers and the report use, so
give the p99 of an update before and after from your own run only labelled as such ("my run,
before the gate"); no latency budget is declared, so a slower p99 is judged by the regression
threshold alone.

Out of scope: the query path, the public interface, and any change to the tests in `tests/book/`.
