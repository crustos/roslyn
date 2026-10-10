#!/usr/bin/env python3
"""Tests for tools/partial_merge.py.   python3 tests/test_partial_merge.py"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import partial_merge as pm            # noqa: E402


DEFINES = ("CRUST",)


def merged(**files):
    srcs = {k.replace("__", "/") + ".cs": v for k, v in files.items()}
    return pm.merge_partials(srcs, defines=()), srcs


class Masking(unittest.TestCase):
    def test_same_length_and_newlines(self):
        s = 'a "x{" b // c {\n/* { */ \'}\' $"{y}" @"q""{" #if X\n'
        m = pm.mask_source(s)
        self.assertEqual(len(m), len(s))
        self.assertEqual(m.count("\n"), s.count("\n"))
        self.assertNotIn("{", m)
        self.assertNotIn("}", m)

    def test_interpolated_nested_string(self):
        s = 'var a = $"{(b ? "}" : "{")}"; class X { }'
        m = pm.mask_source(s)
        self.assertEqual(m.count("{"), 1)


class Conditionals(unittest.TestCase):
    def test_if_else_elif(self):
        src = ("a\n#if X\nb\n#elif Y && !Z\nc\n#else\nd\n#endif\ne\n")
        self.assertEqual(pm.preprocess(src, {"X"}).split(), ["a", "b", "e"])
        self.assertEqual(pm.preprocess(src, {"Y"}).split(), ["a", "c", "e"])
        self.assertEqual(pm.preprocess(src, {"Y", "Z"}).split(), ["a", "d", "e"])
        self.assertEqual(pm.preprocess(src, set()).split(), ["a", "d", "e"])

    def test_line_numbers_kept_and_nesting(self):
        src = "#if A\n#if B\nx\n#endif\ny\n#endif\nz\n"
        out = pm.preprocess(src, {"A"})
        self.assertEqual(out.count("\n"), src.count("\n"))
        self.assertEqual(out.split(), ["y", "z"])

    def test_other_directives_kept(self):
        self.assertIn("#nullable enable", pm.preprocess("#nullable enable\n", set()))

    def test_debug_only_part_is_dropped_not_merged(self):
        r, _ = merged(
            A__X="partial class X { int a; }\n",
            A__Y="#if DEBUG\npartial class X { int dbg; }\n#endif\n")
        self.assertNotIn("dbg", r.files["A/X.cs"])
        self.assertNotIn("A/Y.cs", r.files)     # untouched: Crust drops the region itself

    def test_name_chosen_by_condition(self):
        r, _ = merged(A__X="partial class\n#if DEBUG\n  Adapter\n#else\n  Sym\n#endif\n{ int a; }\n",
                      A__Y="partial class Sym { int b; }\n")
        self.assertIn("int b;", r.files["A/X.cs"])
        self.assertEqual(r.types_merged, 1)


class PartialMethods(unittest.TestCase):
    def test_declaration_and_implementation_fuse(self):
        r, _ = merged(
            A__X="partial class X {\n  [Conditional(\"DEBUG\")]\n  private partial void V();\n  void C() { V(); }\n}\n",
            A__Y="partial class X {\n  private partial void V()\n  { int q = 1; }\n}\n")
        out = r.files["A/X.cs"]
        self.assertEqual(len(re.findall(r"\bV\(\)", out)), 2)        # call + one method
        self.assertNotRegex(out, r"\bpartial\b")
        self.assertIn("[Conditional(\"DEBUG\")]", out)
        self.assertIn("int q = 1;", out)
        self.assertNotIn("V();\n  private", out)

    def test_implementation_without_declaration_becomes_plain_method(self):
        r, _ = merged(A__X="class X { private partial void V() { } }\n")
        self.assertNotRegex(r.files["A/X.cs"], r"\bpartial\b")
        self.assertIn("private void V()", r.files["A/X.cs"])

    def test_unpaired_declaration_is_left(self):
        r, _ = merged(A__X="partial class X { partial void V(); }\n")
        self.assertIn("partial void V();", r.files["A/X.cs"])

    def test_nested_partial_in_plain_type(self):
        r, _ = merged(A__X="class X { partial class In { int a; } }\n")
        self.assertNotRegex(r.files["A/X.cs"], r"\bpartial\b")


class RoundTrip(unittest.TestCase):
    def test_render_reproduces_source_without_partials(self):
        src = ("using System;\n// c\nnamespace N\n{\n    public class A\n    {\n"
               "        int X { get; set; } = 5;\n        void M(int a = 3) { }\n"
               "        [Foo(Bar = 1)] void K() { var s = \"}\"; }\n    }\n}\n")
        pieces = []
        pm._render(pm.parse(src), pieces)
        self.assertEqual("".join(p[0] for p in pieces), src)


class Merge(unittest.TestCase):
    def test_two_parts(self):
        r, _ = merged(A__X="namespace N { public partial class X { int a; } }\n",
                      A__X_Part="namespace N { partial class X { int b; void M() { } } }\n")
        out = r.files["A/X.cs"]
        self.assertIn("int a;", out)
        self.assertIn("int b;", out)
        self.assertIsNone(r.files["A/X_Part.cs"])
        self.assertNotRegex(out, r"\bpartial\b")
        self.assertEqual((r.types_merged, r.parts_merged), (1, 1))

    def test_modifiers_attributes_bases_where(self):
        r, _ = merged(
            A__X="public partial class X<T> : Base { }\n",
            A__Y="[Dbg(1)]\nsealed partial class X<T> : Base, IFoo where T : class { }\n")
        out = r.files["A/X.cs"]
        self.assertRegex(out, r"public\s+sealed\s+class X<T>")
        self.assertIn("[Dbg(1)]", out)
        self.assertRegex(out, r"Base, IFoo")
        self.assertIn("where T : class", out)

    def test_generic_arity_distinguishes_types(self):
        r, _ = merged(A__X="partial class X { }\n", A__Y="partial class X<T> { }\n")
        self.assertEqual(r.types_merged, 0)
        self.assertNotIn("A/Y.cs", [k for k, v in r.files.items() if v is None])

    def test_same_name_in_different_namespaces_not_merged(self):
        r, _ = merged(A__X="namespace P { partial class X { int a; } }\n",
                      A__Y="namespace Q { partial class X { int b; } }\n")
        self.assertEqual(r.types_merged, 0)
        self.assertNotIn("int b;", r.files["A/X.cs"])

    def test_nested_partial(self):
        r, _ = merged(
            A__X="partial class X { partial class In { int a; } }\n",
            A__Y="partial class X { partial class In { int b; } int c; }\n")
        out = r.files["A/X.cs"]
        self.assertEqual(out.count("class In"), 1)
        for v in ("int a;", "int b;", "int c;"):
            self.assertIn(v, out)

    def test_braces_in_strings_and_initializers(self):
        r, _ = merged(
            A__X='partial class X { string s = "}{"; char c = \'}\'; int[] a = { 1, 2 }; '
                 'int P { get; } = 4; string t => $"{s}"; }\n',
            A__Y="partial class X { int z; }\n")
        out = r.files["A/X.cs"]
        self.assertIn('"}{"', out)
        self.assertIn("int z;", out)
        self.assertEqual(r.errors, {})

    def test_attribute_with_equals_and_lambda_bodied_member(self):
        r, _ = merged(
            A__X="partial class X { [Foo(Bar = 1)] void M() { } int F() => G(() => { return 1; }); }\n",
            A__Y="partial class X { void Q() { } }\n")
        out = r.files["A/X.cs"]
        self.assertIn("void Q()", out)
        self.assertIn("[Foo(Bar = 1)] void M()", out)

    def test_usings_carried_to_carrier(self):
        r, _ = merged(
            A__X="using System;\npartial class X { }\n",
            A__Y="using System;\nusing System.Text;\nusing R = System.IO.Path;\npartial class X { }\n")
        out = r.files["A/X.cs"]
        self.assertIn("using System.Text;", out)
        self.assertIn("using R = System.IO.Path;", out)
        self.assertEqual(out.count("using System;"), 1)

    def test_file_with_other_content_is_kept(self):
        r, _ = merged(A__X="partial class X { }\n",
                      A__Y="partial class X { }\nclass Other { }\n")
        self.assertIsNotNone(r.files["A/Y.cs"])
        self.assertNotIn("class X", r.files["A/Y.cs"])

    def test_single_part_partial_loses_modifier(self):
        r, _ = merged(A__X="public partial struct S { int a; }\n")
        self.assertNotRegex(r.files["A/X.cs"], r"\bpartial\b")
        self.assertEqual(r.types_merged, 0)

    def test_file_scoped_namespace_keys(self):
        r, _ = merged(A__X="namespace P;\npartial class X { int a; }\n",
                      A__Y="namespace Q;\npartial class X { int b; }\n")
        self.assertEqual(r.types_merged, 0)

    def test_unparseable_file_is_left_alone(self):
        r, _ = merged(A__X="partial class X { \n")
        self.assertIn("A/X.cs", r.errors)
        self.assertNotIn("A/X.cs", r.files)

    def test_source_map_points_at_the_part(self):
        r, _ = merged(A__X="partial class X {\n int a;\n}\n",
                      A__Y="partial class X {\n int b;\n}\n")
        files = {f for _, f, _ in r.maps["A/X.cs"]}
        self.assertEqual(files, {"A/X.cs", "A/Y.cs"})


@unittest.skipUnless(os.path.isdir(os.path.join(ROOT, "src/Compilers/Core/Portable")), "needs the roslyn tree")
class WholeTree(unittest.TestCase):
    """Nothing may be lost or invented across the real Roslyn sources."""

    @classmethod
    def setUpClass(cls):
        base = ROOT
        srcs = {}
        for d in ("src/Compilers/Core/Portable", "src/Compilers/CSharp/Portable"):
            for dp, _, fns in os.walk(os.path.join(base, d)):
                for fn in fns:
                    if fn.endswith(".cs"):
                        p = os.path.join(dp, fn)
                        with open(p, encoding="utf-8-sig", newline="") as f:
                            srcs[os.path.relpath(p, base)] = f.read()
        cls.srcs = srcs
        cls.res = pm.merge_partials(srcs, defines=DEFINES, pair_methods=False)
        cls.res_pm = pm.merge_partials(srcs, defines=DEFINES)
        cls.pre = {p: pm.preprocess(s, DEFINES) for p, s in srcs.items()}

    def test_member_multiset_preserved(self):
        before = pm.Counter()
        for p, s in self.srcs.items():
            if p not in self.res.errors:
                before += pm.member_chunks(self.pre[p], p)
        after = pm.Counter()
        for p, s in self.srcs.items():
            if p in self.res.errors:
                continue
            if p in self.res.files:
                if self.res.files[p] is not None:
                    after += pm.member_chunks(self.res.files[p], p)
            else:
                after += pm.member_chunks(self.pre[p], p)
        self.assertEqual(before - after, pm.Counter(), "members lost")
        self.assertEqual(after - before, pm.Counter(), "members invented")

    def test_partial_methods_fused(self):
        left = []
        for p, text in self.res_pm.files.items():
            if text is None:
                continue
            for m in re.finditer(r"\bpartial\b[^;{(]*\(", pm.mask_source(text)):
                left.append(p)
        self.assertLessEqual(len(left), 3, left)          # unpaired declarations only
        self.assertEqual(self.res_pm.errors, {})

    def test_no_partial_left_and_few_errors(self):
        self.assertEqual(self.res.leftover, [])
        self.assertLess(len(self.res.errors), 5, self.res.errors)


if __name__ == "__main__":
    unittest.main()
