# Brief: design the L2 order book

Design a price-level (L2) order book for one symbol.

It must support three operations: add quantity at a price on one side, reduce quantity at a price,
and delete a price level. It must answer two queries: the best bid and best offer, and the top N
levels of each side in price priority.

Constraints:

- No allocation on the update path once the book is warm.
- A level whose quantity reaches zero is removed; it is never reported with quantity zero.
- A reduce or delete for a level that does not exist is reported to the caller and changes nothing.
- A crossed book (best bid at or above best offer) is reported, not silently repaired.

State the behaviour as WHEN/THEN scenarios with ids that start with `BOOK-`, one row per behaviour,
each naming the test that will prove it. The tests task writes one test per row, and the implement
task is judged by those tests, so a behaviour with no row will not be built.

Say what you considered and turned down, and list what you had to assume.
