# C++ rules every author and reviewer on the bench checks

The project's rules file: name it once as `[defaults] rules_file = "benches/cpp-standards.md"`
(relative to the generated workflow; `--set rules_file=...` on `runner new`) and every producer
sees it under "Project rules" in its prompt and every reviewer of that work sees the same text.
A reviewer may block on a violation of a rule marked blocking; the persona files do not repeat
these rules, they cite them. Where two reviewers on a panel both see a violation, the one whose
focus is nearest raises it (interface types: api-aurelie; a test convention: meticulous-mira) and
the others cite its finding instead of filing their own.

- **Strong semantic types (blocking).** Every value with a meaning gets its own type. No naked
  `int`, `std::string`, `pid_t` and friends in a public interface. The same underlying type with
  two meanings means two strong types. The one exception is a truly universal operation such as
  `size()` returning `std::size_t`. No two public members or parameters of a function may share a
  type.
- **No `bool` (blocking).** Never as a parameter, return type or member. Use
  `enum class X : bool { no, yes }` or a safe-bool type.
- **`explicit` on every constructor (blocking)** except copy and move: `explicit`,
  `explicit(false)` when implicit conversion is intended, or `explicit(expr)` on wrapping and
  forwarding constructors.
- **`noexcept` policy (blocking).** Copy and move constructors and assignments are `noexcept`,
  and their bodies must not be able to throw: a member whose copy can throw makes the type's
  copy a throwing operation, and adding the keyword to it is a defect (it terminates), not a
  fix. Generic code uses `noexcept(noexcept(...))`. Nothing else is `noexcept`.
- **`[[nodiscard]]` (advisory)** only where ignoring the return is almost certainly a bug, which
  includes most side-effect-free functions. If a reasonable caller would have to cast to `void`,
  it does not belong.
- **Tests (blocking).** Every component has a property-based test of its invariants beside its
  example tests; test code uses the project's test-framework wrapper headers, never the framework
  directly; `REQUIRE` for preconditions and `CHECK` for assertions; test code in an anonymous
  namespace.
- **Headers (advisory).** Every header includes what it uses and compiles on its own; a
  forward declaration where a full definition is not needed.
- **Terminology.** "Member function", never "method".

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
