# Bug 17: `ratio` crashes on an empty period

Reported by the reporting job, 2026-09-28.

`calc.ratio(hits, total)` is documented to return `0.0` when `total` is zero (an empty period has
no hits). The nightly report crashed instead:

```
File "src/calc/__init__.py", line 4, in ratio
    return hits / total
ZeroDivisionError: division by zero
```

Steps: `python3 -c "import sys; sys.path.insert(0, 'src'); import calc; print(calc.ratio(0, 0))"`.
Expected `0.0`. Seen: the traceback above.

Go through `calc.ratio`, the entry point the report job calls. Other helpers in `src/calc/` divide
by a count too; they are not known to be affected.
