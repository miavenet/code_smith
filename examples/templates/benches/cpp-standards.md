# C++ rules every author and reviewer on the bench checks

The project's rules file: name it once as `[defaults] rules_file = "benches/cpp-standards.md"`
(relative to the generated workflow; `--set rules_file=...` on `runner new`) and every producer
sees it under "Project rules" in its prompt and every reviewer of that work sees the same text.
A reviewer may block on a violation of a rule marked blocking; the persona files do not repeat
these rules, they cite them. Where two reviewers on a panel both see a violation, the one whose
focus is nearest raises it (interface types: api-aurelie; a test convention: meticulous-mira) and
the others cite its finding instead of filing their own.

How to cite these rules. A reviewer's finding that rests on a rule starts its detail with the rule's
id (`CPP-02`, `PF-C08`); a finding that rests on no rule shows its consequence: what breaks, for
whom. No rule and no consequence: the finding is advisory. A rule's marking sets its severity: a
violation of a rule marked blocking may block, a violation of one marked advisory may not; citing a
Core Guidelines id or an advisory rule never makes a finding blocking; a reviewer may still block on
a consequence no rule names. An author who departs from a rule says which id and why. Ids are never
reused; a retired rule keeps its id, marked retired.

Each rule gives its id, a reason, and its source: a C++ Core Guidelines id (CG, the edition of
Jun 14, 2026, isocpp.github.io/CppCoreGuidelines) only where that guideline says the same thing,
with how it is narrower or wider; **house** where no guideline states the rule, so the rule
carries the authority of this file and no more. "Use" names the primitive to reach for.

- **CPP-01 Strong semantic types (blocking).** Every value with a meaning gets its own type. No
  naked `int`, `std::string`, `pid_t` and friends in a public interface. The same underlying
  type with two meanings means two strong types. The one exception is a truly universal
  operation such as `size()` returning `std::size_t`. No two public members or parameters of a
  function may share a type.
  Reason: a swapped argument or a wrong unit fails to compile instead of failing at run time.
  Source: CG I.4 for precisely typed interfaces (narrower: it does not ask for one type per
  meaning); one type per meaning, its one exception and "no two share a type" are house.
  Use: `std::chrono` durations (I.4's example), `gsl::not_null` for a pointer that must not be
  null (CG I.12), `std::span` or `gsl::span` for a range (CG F.24).
- **CPP-02 No `bool` (blocking).** Never as a parameter, return type or member. Use
  `enum class X : bool { no, yes }` or a safe-bool type.
  Reason: a call site such as `f(true, false)` does not say what it sets.
  Source: house. CG I.4's enforcement flags only more than one `bool` parameter; this ban is
  wider and is not a guideline.
  Use: `enum class X : bool { no, yes }`; a flags `enum` for a set of options.
- **CPP-03 `explicit` on every constructor (blocking)** except copy and move: `explicit`,
  `explicit(false)` when implicit conversion is intended, or `explicit(expr)` on wrapping and
  forwarding constructors.
  Reason: an unintended conversion builds an object nobody asked for.
  Source: CG C.46, which covers single-argument constructors; this rule covers all, and asks
  for `explicit(false)` where C.46's exception leaves the keyword out.
- **CPP-04 `noexcept` policy (blocking).** Copy and move constructors and assignments are
  `noexcept`, and their bodies must not be able to throw: a member whose copy can throw makes
  the type's copy a throwing operation, and adding the keyword to it is a defect (it
  terminates), not a fix. Generic code uses `noexcept(noexcept(...))`. Nothing else is
  `noexcept`.
  Reason: containers move only what cannot throw, and a throw through `noexcept` terminates.
  Source: CG C.66 for move operations (narrower); `noexcept` copy is house, and "nothing else"
  is house and departs from CG F.6, which asks for `noexcept` on any function that must not
  throw.
- **CPP-05 `[[nodiscard]]` (advisory)** only where ignoring the return is almost certainly a
  bug, which includes most side-effect-free functions. If a reasonable caller would have to
  cast to `void`, it does not belong.
  Reason: a warning on every call teaches callers to silence it.
  Source: house; no guideline says where to put the attribute.
- **CPP-06 Tests (blocking).** Every component has a property-based test of its invariants
  beside its example tests; test code uses the project's test-framework wrapper headers, never
  the framework directly; `REQUIRE` for preconditions and `CHECK` for assertions; test code in an
  anonymous namespace.
  Reason: examples miss the inputs nobody thought of, and one wrapper is one place to change.
  Source: CG SF.22 for the anonymous namespace (wider: it covers every internal entity of an
  implementation file); the rest is house.
- **CPP-07 Headers (advisory).** Every header includes what it uses and compiles on its own; a
  forward declaration where a full definition is not needed.
  Reason: a header that leans on another's includes breaks when that one changes.
  Source: CG SF.10 and SF.11 (the same); the forward declaration is house.
- **CPP-08 Terminology.** "Member function", never "method".
  Reason: one word for one thing in findings and summaries.
  Source: house.

Approved patterns that must not be flagged:

```cpp
struct Foo {                                   // nested enum constants
    enum class Kind : std::uint8_t { queue, tcp, udp };
    static constexpr Kind queue = Kind::queue;
    static constexpr Kind tcp = Kind::tcp;
};

class INode {                                  // NVI: virtuals are never public
public:
    virtual ~INode() = default;
    void execute() { do_execute(); }
private:
    virtual void do_execute() = 0;
};
```

Non-thread-safe is the C++ default; do not ask for documentation of it. Validation belongs in the
type that owns the invariant, so similar validation in two types is not duplication.
