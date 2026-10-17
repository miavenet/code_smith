"""The one path-pattern matcher (03, Path patterns).

Used for `outputs`, `writes`, `removes` and `protected`. Python's `fnmatch` and
`PurePath.match` disagree on `docs/spec/**`, so neither is used here.
"""

import re
from functools import lru_cache

WILDCARDS = "*?["


def has_wildcard(pattern):
    return any(c in pattern for c in WILDCARDS)


def validate_pattern(pattern):
    """Return a list of problems with a pattern. Empty means well formed."""
    if not isinstance(pattern, str) or not pattern:
        return ["a path pattern must be a non-empty string"]
    problems = []
    if "\\" in pattern:
        problems.append("uses '\\'; patterns use '/'")
    if pattern.startswith("/"):
        problems.append("is absolute; patterns are relative to the root")
    segments = pattern.split("/")
    if not pattern.startswith("/") and "" in segments:
        problems.append("has an empty segment")
    if ".." in segments or "." in segments:
        problems.append("contains '.' or '..'")
    for seg in segments:
        if "**" in seg and seg != "**":
            problems.append("'**' must be a whole segment")
            break
    for seg in segments:
        if seg.count("[") != seg.count("]"):
            problems.append("has an unclosed '['")
            break
    if not problems:
        try:
            _compile(pattern)
        except re.error as exc:
            problems.append(f"is not a valid pattern (a bad character class such as a reversed "
                            f"range?): {exc}")
    return problems


def _segment_regex(seg):
    out = []
    i = 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = seg.find("]", i + 2)  # the first character of a class may be ']'
            if j == -1:
                out.append(re.escape(c))
            else:
                body = seg[i + 1:j]
                negate = body[:1] in ("!", "^")
                if negate:
                    body = body[1:]
                body = body.replace("\\", "\\\\").replace("/", "")
                if body.startswith("^"):
                    body = "\\" + body
                out.append("[^/" + body + "]" if negate else "(?!/)[" + body + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@lru_cache(maxsize=None)
def _compile(pattern):
    segments = pattern.split("/")
    if len(segments) == 1:
        if segments[0] == "**":
            return re.compile(r".+\Z", re.S)
        if has_wildcard(pattern):
            # a pattern with no '/' names a file in any directory
            return re.compile(r"(?:[^/]+/)*" + _segment_regex(pattern) + r"\Z", re.S)
        return re.compile(re.escape(pattern) + r"\Z", re.S)
    parts = []
    last = len(segments) - 1
    for n, seg in enumerate(segments):
        if seg == "**":
            if n == last:
                parts.append(".+")          # everything below, not the directory itself
            else:
                parts.append("(?:[^/]+/)*")  # zero or more whole directories
        else:
            parts.append(_segment_regex(seg))
            if n != last:
                parts.append("/")
    return re.compile("".join(parts) + r"\Z", re.S)


def matches(pattern, path):
    """True if the root-relative, '/'-separated path matches the pattern."""
    return _compile(pattern).match(path) is not None


def matches_any(patterns, path):
    return any(matches(p, path) for p in patterns)


def literal_prefix(pattern):
    """The segments before the first segment that holds a wildcard."""
    prefix = []
    for seg in pattern.split("/"):
        if has_wildcard(seg):
            break
        prefix.append(seg)
    return tuple(prefix)


def covered_by(output, writes):
    """Conservative: is every path the output pattern can match allowed by `writes`?"""
    for w in writes:
        if w == output or w == "**":
            return True
        if w.endswith("/**"):
            d = w[:-3]
            if d and not has_wildcard(d) and output.startswith(d + "/"):
                return True
    if not has_wildcard(output):
        return any(matches(w, output) for w in writes)
    return False


def may_overlap(a, b):
    """Conservative: may report an overlap that cannot happen, never misses one."""
    wa, wb = has_wildcard(a), has_wildcard(b)
    if not wa and not wb:
        return a == b
    if not wa:
        return matches(b, a)
    if not wb:
        return matches(a, b)
    if "/" not in a or "/" not in b:
        return True
    pa, pb = literal_prefix(a), literal_prefix(b)
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]
