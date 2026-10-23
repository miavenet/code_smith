# Integer addition design

Provide a dependency-free Python add(a, b) returning the exact sum.
Validate inputs with isinstance(value, int). This intentionally accepts bool as
an integer: add(True, 1) returns 2. All non-integer inputs raise TypeError.
No I/O or state is involved; Python integers do not overflow.
Tests cover positive, negative, zero and large operands, plus invalid strings.
