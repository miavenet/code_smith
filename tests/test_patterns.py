import unittest

import helpers  # noqa: F401  (sets sys.path)
from codesmith import patterns as P


class Matching(unittest.TestCase):
    def test_pattern_table(self):
        """wf: path patterns have one meaning (WF-19)"""
        cases = [
            ("docs/spec/**", "docs/spec/a/b.md", True),
            ("docs/spec/**", "docs/spec/x.md", True),
            ("docs/spec/*", "docs/spec/a/b.md", False),
            ("docs/spec/*", "docs/spec/x.md", True),
            ("src/**/x.h", "src/x.h", True),
            ("src/**/x.h", "src/a/b/x.h", True),
            ("src/**/x.h", "src/a/y.h", False),
            ("**/x.h", "x.h", True),
            ("**/x.h", "a/b/x.h", True),
            ("*.lock", "sub/x.lock", True),
            ("*.lock", "x.lock", True),
            ("*.lock", "x.lock/y", False),
            ("src/book/**", "src/book", False),
            ("src/book/**", "src/bookish/a", False),
            ("src/book/side.hpp", "src/book/side.hpp", True),
            ("src/book/side.hpp", "src/book/side.hpp.bak", False),
            ("CMakeLists.txt", "sub/CMakeLists.txt", False),
            ("a/?.c", "a/b.c", True),
            ("a/?.c", "a//.c", False),
            ("a/[ab].c", "a/b.c", True),
            ("a/[!ab].c", "a/b.c", False),
            ("a/[!ab].c", "a/c.c", True),
            ("**", "anything/at/all", True),
            ("a/*", "a/b{c}.+", True),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(P.validate_pattern(pattern), [])
                self.assertIs(P.matches(pattern, path), expected)

    def test_star_never_crosses_a_slash(self):
        self.assertFalse(P.matches("a/*/c", "a/b/x/c"))
        self.assertTrue(P.matches("a/*/c", "a/b/c"))

    def test_malformed_patterns(self):
        """wf: path patterns have one meaning (WF-19, malformed)"""
        for bad in ["/abs/path", "a/../b", "../x", "a**b", "a/**b/c", "", "a//b", "a\\b", "./a", "a/[b", "[z-a]", "src/[z-a].c"]:
            with self.subTest(pattern=bad):
                self.assertNotEqual(P.validate_pattern(bad), [])

    def test_a_valid_pattern_always_compiles(self):
        """wf: validate_pattern rejects what cannot compile instead of crashing later"""
        self.assertEqual(P.validate_pattern("[a-z]*.c"), [])
        self.assertIn("not a valid pattern", P.validate_pattern("[z-a]")[0])

    def test_literal_prefix(self):
        self.assertEqual(P.literal_prefix("src/book/**"), ("src", "book"))
        self.assertEqual(P.literal_prefix("src/**/x.h"), ("src",))
        self.assertEqual(P.literal_prefix("*.lock"), ())
        self.assertEqual(P.literal_prefix("a/b.c"), ("a", "b.c"))


class CoverAndOverlap(unittest.TestCase):
    def test_covered_by(self):
        """wf: cover and overlap are conservative (WF-20, cover)"""
        self.assertTrue(P.covered_by("src/book/**", ["src/book/**", "CMakeLists.txt"]))
        self.assertTrue(P.covered_by("src/book/impl/*.cpp", ["src/**"]))
        self.assertTrue(P.covered_by("src/book/a.cpp", ["src/book/*.cpp"]))
        self.assertTrue(P.covered_by("anything/*", ["**"]))
        self.assertFalse(P.covered_by("src/book/a.cpp", ["src/other/**"]))
        # really covered, but not provably so under the stated rule: refused
        self.assertFalse(P.covered_by("src/book/*.cpp", ["src/*/*.cpp"]))
        self.assertFalse(P.covered_by("src/bookish/x", ["src/book/**"]))

    def test_may_overlap(self):
        """wf: cover and overlap are conservative (WF-20, overlap)"""
        self.assertTrue(P.may_overlap("a/b.c", "a/b.c"))
        self.assertFalse(P.may_overlap("a/b.c", "a/b.d"))
        self.assertTrue(P.may_overlap("src/book/a.cpp", "src/book/**"))
        self.assertFalse(P.may_overlap("src/other/a.cpp", "src/book/**"))
        self.assertTrue(P.may_overlap("src/**/x.h", "src/book/**"))
        self.assertTrue(P.may_overlap("src/book/**", "src/**/x.h"))
        self.assertFalse(P.may_overlap("src/book/**", "tests/book/**"))
        self.assertTrue(P.may_overlap("*.lock", "src/book/**"))
        # a false alarm the rule accepts: these cannot share a file
        self.assertTrue(P.may_overlap("src/*.h", "src/*.cpp"))


if __name__ == "__main__":
    unittest.main()
