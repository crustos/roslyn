#!/usr/bin/env python3
"""Tests for tools/normalize.py.   python3 tests/test_normalize.py"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import normalize as nz                                    # noqa: E402
from normalize import (normalize, strip_string_nullable, file_scoped_namespace,   # noqa: E402
                       interpolated_strings, null_coalescing, null_conditional_stmt,
                       null_conditional_cmp, interlocked_cas, is_null, ctor_chaining, null_conditional_expr, throw_to_abort, named_arguments, signatures, nullable_attributes)


class StripStringNullable(unittest.TestCase):
    def test_fields_params_returns_and_arrays(self):
        src = "class C { string? f; string? M(string? a, string?[] b) { return a; } }"
        out = strip_string_nullable(src)
        self.assertNotIn("string?", out)
        self.assertEqual(out.count("string"), 4)

    def test_comments_and_literals_untouched(self):
        src = '// string? in a comment\nvar s = "string? literal"; /* string? */ string? x;'
        out = strip_string_nullable(src)
        self.assertIn("// string? in a comment", out)
        self.assertIn('"string? literal"', out)
        self.assertIn("/* string? */", out)
        self.assertTrue(out.endswith("string x;"))

    def test_other_nullables_and_ternaries_untouched(self):
        src = "int? a; Foo? b; var r = c ? 1 : 2; var t = mystring ? 1 : 2;"
        self.assertEqual(strip_string_nullable(src), src)

    def test_line_count_unchanged(self):
        src = "string? a;\nstring? b;\n"
        self.assertEqual(normalize(src).count("\n"), src.count("\n"))


class FileScopedNamespace(unittest.TestCase):
    def test_converted(self):
        src = "using A;\n\nnamespace X.Y;\n\nclass C { }\n"
        out = file_scoped_namespace(src)
        self.assertIn("namespace X.Y {\n", out)
        self.assertTrue(out.endswith("class C { }\n}\n"))
        self.assertEqual(out.count("\n"), src.count("\n") + 1)

    def test_block_namespace_and_comments_untouched(self):
        src = "// namespace X;\nnamespace Y { class C { } }\n"
        self.assertEqual(file_scoped_namespace(src), src)


class InterpolatedStrings(unittest.TestCase):
    def check(self, src, want):
        self.assertEqual(interpolated_strings(src), want)

    def test_basic(self):
        self.check('var s = $"a {x} b {y.Z}";', 'var s = ("a " + (x) + " b " + (y.Z));')

    def test_leading_hole_gets_string_prefix(self):
        self.check('f($"{x}!")', 'f(("" + (x) + "!"))')

    def test_only_literal_and_escaped_braces(self):
        self.check('$"a{{b}}c"', '("a{b}c")')
        self.check('$"plain\\n"', '("plain\\n")')

    def test_hole_with_call_and_nested_string(self):
        self.check('$"x{f(1, "}")}y"', '("x" + (f(1, "}")) + "y")')

    def test_verbatim(self):
        self.check('$@"a\n{x}""q"', '(@"a\n" + (x) + @"""q")')

    def test_verbatim_starting_with_escaped_quote(self):
        self.check('@$"""{x}"""', '(@"""" + (x) + @"""")')

    def test_raw_interpolated_left_alone(self):
        src = '$"""a{x}b"""'
        self.assertEqual(interpolated_strings(src), src)

    def test_format_and_alignment_left_alone(self):
        for src in ('$"{x:X}"', '$"{x,5}"', '$"{(a ? b : c):N}"'):
            self.assertEqual(interpolated_strings(src), src)

    def test_not_inside_comments_or_plain_strings(self):
        src = '// $"{x}"\nvar s = "$\\"{x}";\n'
        self.assertEqual(interpolated_strings(src), src)

    def test_line_count_kept(self):
        src = '$@"a\nb {x}\nc"'
        self.assertEqual(interpolated_strings(src).count("\n"), src.count("\n"))


class NullCoalescing(unittest.TestCase):
    def check(self, src, want):
        self.assertEqual(null_coalescing(src), want)

    def test_plain_paths(self):
        self.check("x = a ?? b;", "x = (a != null ? a : b);")
        self.check("return this.f ?? new C();", "return (this.f != null ? this.f : new C());")
        self.check("f(a.B.C ?? d, e);", "f((a.B.C != null ? a.B.C : d), e);")

    def test_chain_is_right_associative(self):
        self.check("x = a ?? b ?? c;", "x = (a != null ? a : (b != null ? b : c));")

    def test_rhs_stops_at_ternary_and_comma(self):
        self.check("x = a ?? b ? 1 : 2;", "x = (a != null ? a : b) ? 1 : 2;")
        self.check("f(a ?? b, c);", "f((a != null ? a : b), c);")

    def test_lhs_stops_at_lower_precedence(self):
        self.check("x = c ? a ?? b : d;", "x = c ? (a != null ? a : b) : d;")

    def test_assignment_statement(self):
        self.check("{ if (x) { y(); } a ??= b; }", "{ if (x) { y(); } if (a == null) { a = b; } }")
        self.check("a ??= b;", "if (a == null) { a = b; }")
        self.check("{ this.a ??= new C(); }", "{ if (this.a == null) { this.a = new C(); } }")

    def test_left_alone(self):
        for src in ("x = f() ?? b;", "x = a[0] ?? b;", "x = (T)a ?? b;",
                    "x = a ?? throw new E();", "x = a + b ?? c;", "if (c) a ??= b;"):
            self.assertEqual(null_coalescing(src), src, src)

    def test_newline_inside_the_expression_is_kept(self):
        src = "x = a\n    ?? b;\ny;"
        out = null_coalescing(src)
        self.assertEqual(out.count("\n"), src.count("\n"))
        self.assertIn("(a != null ? a : b)", out)

    def test_strings_and_comments(self):
        src = 'x = "a ?? b"; // a ?? b\n'
        self.assertEqual(null_coalescing(src), src)


class NullConditionalStatement(unittest.TestCase):
    def check(self, src, want):
        self.assertEqual(null_conditional_stmt(src), want)

    def test_call_statement(self):
        self.check("{ a?.M(1, 2); }", "{ if (a != null) { a.M(1, 2); } }")
        self.check("{ this.a.b?.M(); }", "{ if (this.a.b != null) { this.a.b.M(); } }")

    def test_assignment_statement(self):
        self.check("{ a?.X = 5; }", "{ if (a != null) { a.X = 5; } }")

    def test_chain_of_conditionals(self):
        self.check("{ a?.B?.M(); }",
                   "{ if (a != null) { if (a.B != null) { a.B.M(); } } }")

    def test_left_alone(self):
        for src in ("x = a?.M();", "return a?.M();", "{ f()?.M(); }", "{ a?.M() ?? b; }",
                    "if (c) a?.M();", "{ a[0]?.M(); }", "{ a?.M() ? 1 : 2; }"):
            self.assertEqual(null_conditional_stmt(src), src, src)

    def test_after_a_block(self):
        self.check("{ if (x) { y(); } c?.D(); }", "{ if (x) { y(); } if (c != null) { c.D(); } }")

    def test_inner_conditional_in_arguments_is_kept(self):
        self.check("{ a?.M(b?.N()); }", "{ if (a != null) { a.M(b?.N()); } }")


class NullConditionalComparison(unittest.TestCase):
    def check(self, src, want):
        self.assertEqual(null_conditional_cmp(src), want)

    def test_null_comparisons(self):
        self.check("if (a?.B != null) { }", "if ((a != null && a.B != null)) { }")
        self.check("x = a?.B == null;", "x = (a == null || a.B == null);")
        self.check("if (a.b?.C.D() is null) { }", "if ((a.b == null || a.b.C.D() == null)) { }")
        self.check("if (a?.B is not null) { }", "if ((a != null && a.B != null)) { }")

    def test_literal_comparisons(self):
        self.check("if (a?.N == 1 && z) { }", "if ((a != null && a.N == 1) && z) { }")
        self.check("if (a?.N != 1) { }", "if ((a == null || a.N != 1)) { }")
        self.check("if (a?.N > 0) { }", "if ((a != null && a.N > 0)) { }")
        self.check('if (a?.S == "x") { }', 'if ((a != null && a.S == "x")) { }')
        self.check("if (a?.M(1, b).C <= 2.5) { }", "if ((a != null && a.M(1, b).C <= 2.5)) { }")

    def test_left_alone(self):
        for src in ("if (a?.B == c) { }",             # c may be null: lifted equality differs
                    "if (a?.B == 1 + 2) { }", "if (a?.B > null) { }", "x = !a?.B == 1;",
                    "x = 1 + a?.B == 1;", "if (f()?.B == 1) { }", "if (a?.B?.C == 1) { }",
                    "if (a?.B ?? 0 == 1) { }", "if (a?.B is Foo) { }"):
            self.assertEqual(null_conditional_cmp(src), src, src)


class InterlockedCas(unittest.TestCase):
    def test_statement_forms(self):
        self.assertEqual(interlocked_cas("{ Interlocked.CompareExchange(ref _x, new D<A, B>(), null); }"),
                         "{ if (_x == null) { _x = new D<A, B>(); } }")
        self.assertEqual(interlocked_cas("{ System.Threading.Interlocked.CompareExchange(ref this.f, M(a, b), S); }"),
                         "{ if (this.f == S) { this.f = M(a, b); } }")

    def test_value_uses_left_alone(self):
        for src in ("x = Interlocked.CompareExchange(ref a, b, c);",
                    "{ return Interlocked.CompareExchange(ref a, b, null) ?? b; }",
                    "{ Interlocked.CompareExchange(ref a[0], b, c); }",
                    "{ Interlocked.CompareExchange(ref f(), b, c); }"):
            self.assertEqual(interlocked_cas(src), src, src)


class IsNull(unittest.TestCase):
    def test_rewrites(self):
        self.assertEqual(is_null("if (a.b is null && c is not null) { }"),
                         "if (a.b == null && c != null) { }")

    def test_left_alone(self):
        for src in ("x = a is Foo;", "x = a is null or b;", "x = a == b is null;", "// a is null\n"):
            self.assertEqual(is_null(src), src, src)


class CtorChaining(unittest.TestCase):
    SRC = ('class T : B {\n  public T(int a, string b = "q") : base(a, b) { x = a; }\n'
           '  public T(int a) : this(a, b: "z") { y(); }\n}\n')

    def test_inlines_target_with_defaults_and_named(self):
        out = ctor_chaining(self.SRC)
        self.assertNotIn("this(", out)
        self.assertIn(': base(a, "z")', out)
        self.assertIn("{ x = a; }", out)
        self.assertIn("y();", out)
        self.assertEqual(out.count("\n"), self.SRC.count("\n") + 1 if out.count("\n") > self.SRC.count("\n") else self.SRC.count("\n"))

    def test_ambiguous_or_impure_left_alone(self):
        src = ('class T { T(int a) { } T(int a, int b = 0) { } T() : this(f(1)) { } }')
        self.assertEqual(ctor_chaining(src), src)
        src2 = 'class T { T(int a) { x = a; z = a; } T() : this(f(1)) { } }'
        self.assertEqual(ctor_chaining(src2), src2)

    def test_returning_target_left_alone(self):
        src = 'class T { T(int a) { if (a > 0) return; } T() : this(1) { } }'
        self.assertEqual(ctor_chaining(src), src)


class NullConditionalExpression(unittest.TestCase):
    def check(self, src, want):
        self.assertEqual(null_conditional_expr(src), want)

    def test_plain_value(self):
        self.check("x = a?.M(1);", "x = (a != null ? a.M(1) : null);")
        self.check("f(a?.B, c);", "f((a != null ? a.B : null), c);")
        self.check("Foo => Data?.V;", "Foo => (Data != null ? Data.V : null);")

    def test_nested_chain(self):
        self.check("x = a?.B?.C(d);", "x = (a != null ? (a.B != null ? a.B.C(d) : null) : null);")

    def test_literal_fallback_is_exact(self):
        self.check("return (_e?.Count ?? 0) == 0;", "return ((_e != null ? _e.Count : 0)) == 0;")
        self.check("x = a?.F ?? false;", "x = (a != null ? a.F : false);")

    def test_property_chain_fallback_reads_twice(self):
        self.check('x = a.b?.N ?? "z";', 'x = (a.b != null && a.b.N != null ? a.b.N : "z");')

    def test_left_alone(self):
        for src in ("x = a?.M() ?? Empty;", "x = !a?.B;", "x = (s as T)?.M();", "x = f()?.B;",
                    "x = a?.B && c;", "x = a?.B + 1;", "x = a?.B ?? throw e;"):
            self.assertEqual(null_conditional_expr(src), src, src)


class ThrowToAbort(unittest.TestCase):
    def check(self, src, want):
        self.assertEqual(throw_to_abort(src), want)

    def test_statements(self):
        self.check("{ switch (k) { case 1: return 2; default: throw ExceptionUtilities.UnexpectedValue(k); } }",
                   "{ switch (k) { case 1: return 2; default: abort(); } }")
        self.check("{ if (c) throw new ArgumentNullException(nameof(x)); }", "{ if (c) abort(); }")
        self.check("{ if (c) { throw ExceptionUtilities.Unreachable(); } else throw new E(\"a\", f(1)); }",
                   "{ if (c) { abort(); } else abort(); }")

    def test_expression_bodied(self):
        self.check("int M(int a) => throw ExceptionUtilities.Unreachable();", "int M(int a) { abort(); }")
        self.check("int P => throw ExceptionUtilities.Unreachable();", "int P { get { abort(); } }")
        self.check("f = () => throw new E();", "f = () => { abort(); };")

    def test_left_alone(self):
        for src in ("x = a ?? throw new E();", "x = c ? 1 : throw new E();", "{ throw; }",
                    "y = k switch { _ => throw ExceptionUtilities.UnexpectedValue(k) };",
                    "{ throw new E().Foo(); }", "{ throw e; }", "{ // throw new E();\n }"):
            self.assertEqual(throw_to_abort(src), src, src)


class NamedArguments(unittest.TestCase):
    DECL = ('class B { public B(int a, string name = null, bool flag = false) { } void V(bool expected) { }\n'
            '  static void Make(int a, int b = 5, int c = 0) { } }\n')

    def run_(self, body):
        src = self.DECL + body
        return named_arguments(src, signatures(src))[len(self.DECL):]

    def test_in_order_just_drops_names(self):
        self.assertEqual(self.run_("void M() { V(expected: true); }"), "void M() { V(true); }")

    def test_skipped_default_is_filled_and_reordered(self):
        self.assertEqual(self.run_("void M() { Make(1, c: 3); Make(b: 2, a: x); }"),
                         "void M() { Make(1, 5, 3); Make(x, 2); }")
        self.assertEqual(self.run_("class D : B { D(int x) : base(x, flag: true) { } }"),
                         "class D : B { D(int x) : base(x, null, true) { } }")

    def test_new_expression(self):
        self.assertEqual(self.run_('void M() { new B(1, name: "n"); }'), 'void M() { new B(1, "n"); }')

    def test_left_alone(self):
        for body in ("void M() { Make(1, b: f(), a: 2); }",      # reordering calls
                     "void M() { Lib.Unknown(q: 1); }",            # no declaration in the tree
                     "void M() { var t = (p: 1, q: 2); }",
                     "void M() { Make(1, z: 2); }"):               # no such parameter
            self.assertEqual(self.run_(body), body, body)

    def test_ambiguous_overloads_disagreeing(self):
        decl = "class A { void F(int a, int b) { } void F(int b, int a, int c) { } }\n"
        src = decl + "void M() { F(a: 1, b: 2); }"
        self.assertEqual(named_arguments(src, signatures(src))[len(decl):], "void M() { F(a: 1, b: 2); }")


class NullableAttributes(unittest.TestCase):
    def test_removed(self):
        self.assertEqual(nullable_attributes("bool F([NotNullWhen(returnValue: false)] this X? x) { }"),
                         "bool F(this X? x) { }")
        self.assertEqual(nullable_attributes("[DoesNotReturn]\nvoid F() { }"), "\nvoid F() { }")
        self.assertEqual(nullable_attributes("[return: NotNullIfNotNull(\"a\")] X F() { }"), "X F() { }")

    def test_other_attributes_kept(self):
        for src in ("[Obsolete] void F() { }", "[NotNull, Foo] int x;", "int[] a = new int[3]; a[0] = 1;"):
            self.assertEqual(nullable_attributes(src), src, src)


class Pipeline(unittest.TestCase):
    def test_idempotent_and_keeps_lines(self):
        src = ('namespace N;\nclass C {\n  void M(string? s) {\n    a ??= b;\n    c?.D();\n'
               '    var t = $"{s}{x}" + (e ?? f);\n  }\n}\n')
        once = normalize(src)
        self.assertEqual(normalize(once), once)
        self.assertEqual(once.count("\n"), src.count("\n") + 1)
        for pat in ("namespace N;", "??", "?.", "$\"", "string?"):
            self.assertNotIn(pat, once)


if __name__ == "__main__":
    unittest.main()
