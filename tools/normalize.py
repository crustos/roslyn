#!/usr/bin/env python3
"""Source-to-source normalizations applied after the partial merge.

Each takes C# text and returns C# text, and only touches code (comments and
string literals are skipped). All of them keep the line numbers of existing
lines; `file_scoped_namespace` adds one closing line at the end of the file.

What is rewritten is limited to forms whose meaning is exactly preserved
without knowing any types. Everything else is left alone for Crust to refuse.

  strip_string_nullable   string?  ->  string
  file_scoped_namespace   namespace N;  ->  namespace N { ... }
  interpolated_strings    $"a{x}b" ->  ("a" + (x) + "b")        (no :format / ,align)
  null_coalescing         P ?? R   ->  (P != null ? P : R)       (P a plain a.b.c path)
                          P ??= R; ->  if (P == null) { P = R; }
  null_conditional_stmt   P?.M(a); ->  if (P != null) { P.M(a); }
  null_conditional_cmp    P?.C != null  ->  (P != null && P.C != null)
                          P?.C == 1     ->  (P != null && P.C == 1)      (also is [not] null,
                                            == != < > <= >= with a literal)

A "plain path" is identifiers joined by dots (`x`, `this.x`, `a.B.C`): it is
repeated in the output, so it must be cheap and free of side effects. Property
getters on such a path are assumed pure.

Not handled, and why: `P?.X` as an expression and `f() ?? R` need the type of
the operand (value type or reference type) or a temporary; `params` needs every
call site rewritten and Crust has no array initializers to rewrite them to.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from partial_merge import iter_tokens, mask_source


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class Tok(NamedTuple):
    kind: str        # id | num | str | chr | op
    a: int
    b: int
    s: str


_CODE = re.compile(r"""
    (?P<id>@?[A-Za-z_]\w*)
  | (?P<num>\d[\w.]*|\.\d\w*)
  | (?P<op>\?\?=|\?\?|\?\.|\?\[|=>|==|!=|<=|>=|&&|\|\||<<=|>>=|\+\+|--
           |[-+*/%&|^]=|::|\S)
""", re.X)


def tokenize(text: str) -> list[Tok]:
    toks: list[Tok] = []

    def code(a: int, b: int) -> None:
        for m in _CODE.finditer(text, a, b):
            toks.append(Tok(m.lastgroup, m.start(), m.end(), m.group()))

    last = 0
    for a, j, tok in iter_tokens(text):
        code(last, a)
        if tok == "'":
            toks.append(Tok("chr", a, j, text[a:j]))
        elif tok not in ("//", "/*") and not tok.endswith("#"):
            toks.append(Tok("str", a, j, text[a:j]))
        last = j
    code(last, len(text))
    return toks


# ---------------------------------------------------------------------------
# strip_string_nullable
# ---------------------------------------------------------------------------

_STRING_NULLABLE = re.compile(r"\bstring\?")


def strip_string_nullable(text: str) -> str:
    """`string?` -> `string`.

    A nullable reference annotation changes nothing at run time. Crust's
    string pass (csrust --coost) does not understand it: `string? f;` crashes
    the pass and `string? p` is misread as a string collection.
    """
    msk = mask_source(text)
    hits = list(_STRING_NULLABLE.finditer(msk))
    if not hits:
        return text
    out, last = [], 0
    for m in hits:
        out.append(text[last:m.end() - 1])
        last = m.end()
    out.append(text[last:])
    return "".join(out)


# ---------------------------------------------------------------------------
# file_scoped_namespace
# ---------------------------------------------------------------------------

_FILE_NS = re.compile(r"(?m)^([ \t]*namespace[ \t]+[\w.]+[ \t]*);")


def file_scoped_namespace(text: str) -> str:
    """`namespace N;` -> `namespace N {`, with the closing brace at the end."""
    msk = mask_source(text)
    m = _FILE_NS.search(msk)
    if not m:
        return text
    out = text[:m.end(1)] + " {" + text[m.end():]
    return out + ("}\n" if out.endswith("\n") else "\n}\n")


# ---------------------------------------------------------------------------
# interpolated_strings
# ---------------------------------------------------------------------------

def _split_interpolated(lit: str):
    """lit = the whole literal, e.g. $"a{x}b". -> list of ("s"|"e", text), or
    None if it uses something that cannot be rewritten as plain `+`."""
    m = re.match(r'(\$@?|@\$)"', lit)
    if not m or lit.startswith('$"""'):                     # raw interpolated: leave
        return None
    verbatim = "@" in m.group(1)
    body = lit[m.end():-1] if lit.endswith('"') else None
    if body is None:
        return None
    parts: list = []
    seg: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == "{":
            if body[i + 1:i + 2] == "{":
                seg.append("{")
                i += 2
                continue
            # find the end of the hole, skipping strings inside it
            depth, k = 1, i + 1
            while k < n and depth:
                ch = body[k]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif ch in "\"'":
                    sub = body[k:]
                    toks = list(iter_tokens(sub))
                    if toks and toks[0][0] == 0:
                        k += toks[0][1]
                        continue
                k += 1
            if depth:
                return None
            hole = body[i + 1:k]
            h0 = _depth0(hole)
            if ":" in h0 or "," in h0:                 # format or alignment
                return None
            if not hole.strip():
                return None
            if seg:
                parts.append(("s", "".join(seg)))
                seg = []
            parts.append(("e", hole.strip()))
            i = k + 1
        elif c == "}":
            if body[i + 1:i + 2] == "}":
                seg.append("}")
                i += 2
                continue
            return None
        else:
            if c == "\\" and not verbatim:
                seg.append(body[i:i + 2])
                i += 2
                continue
            if c == '"' and verbatim and body[i + 1:i + 2] == '"':
                seg.append('""')
                i += 2
                continue
            seg.append(c)
            i += 1
    if seg:
        parts.append(("s", "".join(seg)))
    return parts, verbatim


def _depth0(h: str) -> str:
    """Text of `h` outside (), [], {}, <> and string/char literals."""
    out, d = [], 0
    msk = mask_source(h)
    for ch in msk:
        if ch in "([{":
            d += 1
        elif ch in ")]}":
            d -= 1
        elif d == 0:
            out.append(ch)
    return "".join(out)


def interpolated_strings(text: str) -> str:
    """$"a{x}b" -> ("a" + (x) + "b"), for holes without a format or alignment."""
    edits = []
    for a, j, tok in iter_tokens(text):
        if "$" not in tok:
            continue
        r = _split_interpolated(text[a:j])
        if r is None:
            continue
        parts, verbatim = r
        pieces = []
        for kind, t in parts:
            if kind == "s":
                pieces.append(('@"%s"' if verbatim else '"%s"') % t)
            else:
                pieces.append("(%s)" % t)
        if not pieces:
            pieces = ['""']
        elif parts[0][0] == "e":
            pieces.insert(0, '""')                     # make the sum a string
        edits.append((a, j, "(" + " + ".join(pieces) + ")"))
    return _apply(text, edits)


# ---------------------------------------------------------------------------
# null_coalescing / null_conditional_stmt
# ---------------------------------------------------------------------------

_PLAIN_PATH = re.compile(r"(?:this\.)?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_NOT_PATH = {"null", "true", "false", "default", "new", "typeof", "sizeof",
             "nameof", "base", "default", "await", "throw"}
_LEFT_STOP = {",", ";", "=", "=>", "?", ":", "??", "??=", "+=", "-=", "*=", "/=",
              "%=", "&=", "|=", "^=", "<<=", ">>="}
_RIGHT_STOP = {",", ";", "=", "=>", "?", ":", "+=", "-=", "*=", "/=", "%=", "&=",
               "|=", "^=", "<<=", ">>=", "??="}
_LEFT_KW = {"return", "throw", "case", "yield", "await", "in", "when", "else", "do"}
_OPEN = {"(", "[", "{"}
_CLOSE = {")", "]", "}"}


def _left_start(toks: list[Tok], k: int) -> int:
    """Index of the first token of the operand that ends just before toks[k]."""
    depth, i = 0, k - 1
    while i >= 0:
        s = toks[i].s
        if toks[i].kind == "op":
            if s in _CLOSE:
                depth += 1
            elif s in _OPEN:
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and s in _LEFT_STOP:
                break
        elif depth == 0 and toks[i].kind == "id" and s in _LEFT_KW:
            break
        i -= 1
    return i + 1


def _right_end(toks: list[Tok], k: int) -> int:
    """Index just past the last token of the operand that starts after toks[k]."""
    depth, i, n = 0, k + 1, len(toks)
    while i < n:
        s = toks[i].s
        if toks[i].kind == "op":
            if s in _OPEN:
                depth += 1
            elif s in _CLOSE:
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and s in _RIGHT_STOP:
                break
        i += 1
    return i


def _path_start(toks: list[Tok], k: int) -> int:
    """Index of the first token of the `ident(.ident)*` path ending before toks[k]."""
    i = k - 1
    while i >= 0 and toks[i].kind == "id":
        i -= 1
        if i >= 0 and toks[i].s == "." and toks[i].kind == "op":
            i -= 1
        else:
            break
    return i + 1


def _plain(toks: list[Tok], lo: int, hi: int) -> str | None:
    s = "".join(t.s for t in toks[lo:hi])
    if _PLAIN_PATH.fullmatch(s) and s.split(".")[0] not in _NOT_PATH \
            and s.split(".")[-1] not in _NOT_PATH:
        return s
    return None


def _at_statement_start(toks: list[Tok], i: int) -> bool:
    return i == 0 or toks[i - 1].s in (";", "{", "}")


def _apply(text: str, edits: list) -> str:
    """Apply non-overlapping (a, b, replacement) edits."""
    out, last = [], 0
    for a, b, r in sorted(edits):
        if a < last:
            continue
        out.append(text[last:a])
        out.append(r)
        missing = text.count("\n", a, b) - r.count("\n")
        if missing > 0:                  # keep every later line where it was
            out.append("\n" * missing)
        last = b
    out.append(text[last:])
    return "".join(out)


def _fixpoint(text: str, one_pass) -> str:
    for _ in range(12):
        toks = tokenize(text)
        edits = one_pass(text, toks)
        if not edits:
            return text
        # keep the rightmost edit of any overlapping group: inner before outer
        edits.sort(key=lambda e: -e[0])
        kept: list = []
        for e in edits:
            if all(e[1] <= k[0] or e[0] >= k[1] for k in kept):
                kept.append(e)
        new = _apply(text, kept)
        if new == text:
            return text
        text = new
    return text


def _coalesce_pass(text: str, toks: list[Tok]) -> list:
    edits = []
    for k, t in enumerate(toks):
        if t.kind != "op" or t.s not in ("??", "??="):
            continue
        ls = _left_start(toks, k) if t.s == "??" else _path_start(toks, k)
        re_ = _right_end(toks, k)
        if ls >= k or re_ <= k + 1:
            continue
        path = _plain(toks, ls, k)
        if path is None or toks[k + 1].s == "throw":
            continue
        rhs = text[toks[k + 1].a:toks[re_ - 1].b]
        if t.s == "??":
            if any(x.s == "??" and d == 0 for d, x in _depth_iter(toks[k + 1:re_])):
                continue                                  # a ?? b ?? c: do b ?? c first
            edits.append((toks[ls].a, toks[re_ - 1].b,
                          "(%s != null ? %s : %s)" % (path, path, rhs)))
        else:                                              # ??=, as a statement
            if not _at_statement_start(toks, ls) or re_ >= len(toks) or toks[re_].s != ";":
                continue
            edits.append((toks[ls].a, toks[re_].b,
                          "if (%s == null) { %s = %s; }" % (path, path, rhs)))
    return edits


def _depth_iter(toks):
    d = 0
    for t in toks:
        if t.kind == "op" and t.s in _CLOSE:
            d -= 1
        yield d, t
        if t.kind == "op" and t.s in _OPEN:
            d += 1


def null_coalescing(text: str) -> str:
    if "??" not in text:
        return text
    return _fixpoint(text, _coalesce_pass)


def _conditional_pass(text: str, toks: list[Tok]) -> list:
    edits = []
    for k, t in enumerate(toks):
        if t.s != "?." or t.kind != "op":
            continue
        ls = _path_start(toks, k)
        path = _plain(toks, ls, k)
        if path is None or not _at_statement_start(toks, ls):
            continue
        # the statement must run, unbroken, to its `;`
        depth, i = 0, k + 1
        while i < len(toks):
            s = toks[i].s
            if toks[i].kind == "op":
                if s in _OPEN:
                    depth += 1
                elif s in _CLOSE:
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and s in (";", "??", "??=", "?", "?["):
                    break
            i += 1
        if i >= len(toks) or toks[i].s != ";":
            continue
        rest = text[toks[k + 1].a:toks[i].b]            # up to and including ';'
        edits.append((toks[ls].a, toks[i].b, "if (%s != null) { %s.%s }" % (path, path, rest)))
    return edits


def null_conditional_stmt(text: str) -> str:
    if "?." not in text:
        return text
    return _fixpoint(text, _conditional_pass)



_CMP = {"==", "!=", "<", ">", "<=", ">="}
_CMP_BEFORE = {"(", ",", ";", "{", "}", "=", "&&", "||", "return", "?", ":", "=>"}
_CMP_AFTER = {")", "]", ",", ";", "&&", "||", "?", ":", "}"}


def _comparison_pass(text: str, toks: list[Tok]) -> list:
    edits = []
    n = len(toks)
    for k, t in enumerate(toks):
        if t.kind != "op" or t.s != "?.":
            continue
        ls = _path_start(toks, k)
        path = _plain(toks, ls, k)
        if path is None or (ls > 0 and toks[ls - 1].s not in _CMP_BEFORE):
            continue
        depth, i = 0, k + 1                      # the chain: ids, dots, (...), [...]
        while i < n:
            s, kind = toks[i].s, toks[i].kind
            if kind == "op":
                if s in _OPEN:
                    depth += 1
                elif s in _CLOSE:
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and s in _CMP:
                    break
                elif depth == 0 and s != ".":
                    i = n
                    break
            elif depth == 0 and kind == "id" and s == "is":
                break
            i += 1
        if i <= k + 1 or i >= n:
            continue
        op = toks[i].s
        if op == "is":
            if i + 2 < n and toks[i + 1].s == "null":
                op, lit, end = "==", "null", i + 2
            elif i + 3 < n and toks[i + 1].s == "not" and toks[i + 2].s == "null":
                op, lit, end = "!=", "null", i + 3
            else:
                continue
        else:
            if i + 1 >= n:
                continue
            r = toks[i + 1]
            if not (r.kind in ("num", "str", "chr") or r.s in ("true", "false", "null")):
                continue
            lit, end = r.s, i + 2
            if r.kind == "str":
                lit = text[r.a:r.b]
            if r.s == "null" and op not in ("==", "!="):
                continue
        if end < n and toks[end].s not in _CMP_AFTER:
            continue
        chain = text[toks[k + 1].a:toks[i - 1].b]
        if lit == "null":
            new = ("(%s == null || %s.%s == null)" if op == "==" else
                   "(%s != null && %s.%s != null)") % (path, path, chain)
        elif op == "!=":
            new = "(%s == null || %s.%s != %s)" % (path, path, chain, lit)
        else:
            new = "(%s != null && %s.%s %s %s)" % (path, path, chain, op, lit)
        edits.append((toks[ls].a, toks[end - 1].b, new))
    return edits


def null_conditional_cmp(text: str) -> str:
    if "?." not in text:
        return text
    return _fixpoint(text, _comparison_pass)


# ---------------------------------------------------------------------------
# interlocked_cas / is_null
# ---------------------------------------------------------------------------

_GENERIC_TOK = re.compile(r"[A-Za-z_@]\w*|[.,<>?\[\]]")


def _close(toks: list[Tok], i: int) -> int:
    """Index of the bracket closing toks[i] (one of ( [ {), or -1."""
    depth = 0
    for j in range(i, len(toks)):
        t = toks[j]
        if t.kind != "op":
            continue
        if t.s in ("(", "[", "{"):
            depth += 1
        elif t.s in (")", "]", "}"):
            depth -= 1
            if depth == 0:
                return j
    return -1


def _split_args(toks: list[Tok], lo: int, hi: int) -> list[tuple[int, int]]:
    """Split toks[lo:hi] at top-level commas; a comma inside `Name<A, B>` does not count."""
    parts, start, depth, j = [], lo, 0, lo
    while j < hi:
        t = toks[j]
        if t.kind == "op":
            if t.s in ("(", "[", "{"):
                depth += 1
            elif t.s in (")", "]", "}"):
                depth -= 1
            elif t.s == "<" and j > lo and toks[j - 1].kind == "id":
                d, k = 0, j
                while k < hi and _GENERIC_TOK.fullmatch(toks[k].s):
                    d += toks[k].s == "<"
                    d -= toks[k].s == ">"
                    if d == 0:
                        break
                    k += 1
                if k < hi and d == 0 and toks[k].s == ">":
                    j = k                       # skip the generic argument list
            elif t.s == "," and depth == 0:
                parts.append((start, j))
                start = j + 1
        j += 1
    parts.append((start, hi))
    return parts


def _cas_pass(text: str, toks: list[Tok]) -> list:
    edits = []
    for i, t in enumerate(toks):
        if t.s != "Interlocked" or t.kind != "id":
            continue
        k = i
        while k >= 2 and toks[k - 1].s == "." and toks[k - 2].kind == "id":
            k -= 2
        if not _at_statement_start(toks, k):
            continue
        if not (i + 3 < len(toks) and toks[i + 1].s == "." and toks[i + 2].s == "CompareExchange"
                and toks[i + 3].s == "("):
            continue
        c = _close(toks, i + 3)
        if c < 0 or c + 1 >= len(toks) or toks[c + 1].s != ";":
            continue
        args = _split_args(toks, i + 4, c)
        if len(args) != 3 or toks[args[0][0]].s != "ref":
            continue
        place = _plain(toks, args[0][0] + 1, args[0][1])
        if place is None or any(b <= a for a, b in args[1:]):
            continue
        val = text[toks[args[1][0]].a:toks[args[1][1] - 1].b]
        cmp_ = text[toks[args[2][0]].a:toks[args[2][1] - 1].b]
        edits.append((toks[k].a, toks[c + 1].b, f"if ({place} == {cmp_}) {{ {place} = {val}; }}"))
    return edits


def interlocked_cas(text: str) -> str:
    """Statement `Interlocked.CompareExchange(ref P, V, C);` -> `if (P == C) { P = V; }`.

    Single-threaded lowering (decided by the project owner): no atomicity, and V is
    only evaluated when the swap happens.  Value uses of the result are left alone.
    """
    if "CompareExchange" not in text:
        return text
    return _fixpoint(text, _cas_pass)


def _is_null_pass(text: str, toks: list[Tok]) -> list:
    edits = []
    for i, t in enumerate(toks):
        if t.s != "is" or t.kind != "id":
            continue
        j = i + 1
        neg = False
        if j < len(toks) and toks[j].s == "not":
            neg, j = True, j + 1
        if not (j < len(toks) and toks[j].s == "null" and toks[j].kind == "id"):
            continue
        b = _path_start(toks, i)
        if b > 0 and toks[b - 1].s in ("==", "!=", "is"):
            continue
        if j + 1 < len(toks) and toks[j + 1].s in ("and", "or", "==", "!="):
            continue
        edits.append((t.a, toks[j].b, "!= null" if neg else "== null"))
    return edits


def is_null(text: str) -> str:
    """`x is null` -> `x == null`, `x is not null` -> `x != null` (Crust has no `is`)."""
    if not re.search(r"\bis\s+(not\s+)?null\b", text):
        return text
    return _fixpoint(text, _is_null_pass)


# ---------------------------------------------------------------------------
# ctor_chaining:  `C(P1) : this(A) { B1 }`  ->  the target's initializer and body inlined
# ---------------------------------------------------------------------------

class _Ctor(NamedTuple):
    params: list          # [(name, default_text | None)]
    colon: int            # token index of ':' (or -1)
    kind: str             # "this" | "base" | ""
    iargs: list           # [(lo, hi)] token ranges of the initializer arguments
    ilo: int              # token index of the first initializer token (the keyword)
    ihi: int              # token index of the ')' closing the initializer
    blo: int              # token index of '{'
    bhi: int              # token index of the matching '}'


_TYPE_KW = ("class", "struct")
_SKIP_PARAM = ("ref", "out", "in", "params", "this")


def _find_ctors(toks: list[Tok]) -> dict:
    """{ (type-open-brace token index, ctor index) : _Ctor } grouped per enclosing type."""
    res: dict = {}
    stack: list = []                        # (name, open brace index, depth after open)
    depth = 0
    pending = None
    for i, t in enumerate(toks):
        if t.kind == "id" and t.s in _TYPE_KW and i + 1 < len(toks) and toks[i + 1].kind == "id":
            pending = toks[i + 1].s
        elif t.kind == "op" and t.s == "{":
            depth += 1
            if pending is not None:
                stack.append((pending, i, depth))
                pending = None
        elif t.kind == "op" and t.s == "}":
            if stack and stack[-1][2] == depth:
                stack.pop()
            depth -= 1
        elif t.kind == "op" and t.s == ";":
            pending = None
        elif (t.kind == "id" and stack and stack[-1][0] == t.s and depth == stack[-1][2]
              and i + 1 < len(toks) and toks[i + 1].s == "(" and i > 0
              and toks[i - 1].s not in (".", "new", "=>", "=", "return", ":")):
            c = _close(toks, i + 1)
            if c < 0 or c + 1 >= len(toks):
                continue
            j, kind, iargs, ilo, ihi, colon = c + 1, "", [], -1, -1, -1
            if toks[j].s == ":":
                colon = j
                if not (j + 2 < len(toks) and toks[j + 1].s in ("this", "base") and toks[j + 2].s == "("):
                    continue
                kind, ilo = toks[j + 1].s, j + 1
                ihi = _close(toks, j + 2)
                if ihi < 0:
                    continue
                iargs = _split_args(toks, j + 3, ihi) if ihi > j + 3 else []
                j = ihi + 1
            if j >= len(toks) or toks[j].s != "{":
                continue
            e = _close(toks, j)
            if e < 0:
                continue
            params = []
            ok = True
            for lo, hi in (_split_args(toks, i + 2, c) if c > i + 2 else []):
                ts = toks[lo:hi]
                if not ts or ts[0].s in _SKIP_PARAM or ts[0].s == "[":
                    ok = False
                    break
                eq = next((k for k, x in enumerate(ts) if x.s == "="), None)
                nm = ts[(eq if eq is not None else len(ts)) - 1]
                dflt = None
                if eq is not None:
                    dflt = "".join(x.s + " " for x in ts[eq + 1:]).strip()
                if nm.kind != "id":
                    ok = False
                    break
                params.append((nm.s, dflt, toks[lo].a, toks[hi - 1].b))
            if ok:
                res[(stack[-1][1], i)] = _Ctor(params, colon, kind, iargs, ilo, ihi, j, e)
    return res


def _simple_arg(src: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*|-?\d[\w.]*|\"[^\"\\]*\"|null|true|false|default|this)\s*", src))


def _ctor_pass(text: str, toks: list[Tok]) -> list:
    ctors = _find_ctors(toks)
    by_type: dict = {}
    for (ty, i), c in ctors.items():
        by_type.setdefault(ty, []).append((i, c))
    edits = []
    for (ty, i), c in ctors.items():
        if c.kind != "this":
            continue
        # argument texts, split into positional and named
        pos, named = [], {}
        bad = False
        for lo, hi in c.iargs:
            if hi <= lo:
                bad = True
                break
            src = text[toks[lo].a:toks[hi - 1].b]
            if hi - lo > 2 and toks[lo].kind == "id" and toks[lo + 1].s == ":":
                named[toks[lo].s] = text[toks[lo + 2].a:toks[hi - 1].b]
            else:
                if named:
                    bad = True
                pos.append(src)
        if bad:
            continue
        cands = []
        for j, d in by_type[ty]:
            if j == i or d is c:
                continue
            names = [p[0] for p in d.params]
            if len(pos) > len(names) or any(n not in names[len(pos):] for n in named):
                continue
            need = [p for k, p in enumerate(d.params)
                    if p[1] is None and k >= len(pos) and p[0] not in named]
            if need:
                continue
            cands.append(d)
        if len(cands) != 1 or cands[0].kind == "this":
            continue
        d = cands[0]
        bind = {}
        for k, (nm, dflt, _, _) in enumerate(d.params):
            bind[nm] = pos[k] if k < len(pos) else named.get(nm, dflt)
        # body of the target: tokens, its uses of each parameter
        btoks = toks[d.blo + 1:d.bhi]
        itoks = toks[d.ilo + 2:d.ihi] if d.kind == "base" else []
        uses = {nm: sum(1 for x in btoks + itoks if x.kind == "id" and x.s == nm) for nm in bind}
        if any(x.s in ("return", "yield", "goto") for x in btoks):
            continue
        arg_ids = {x.s for a in bind.values() for x in tokenize(a) if x.kind == "id"}
        clash = False
        for k in range(1, len(btoks)):
            if btoks[k].kind == "id" and btoks[k].s in arg_ids and btoks[k - 1].kind == "id" \
                    and k + 1 < len(btoks) and btoks[k + 1].s in ("=", ";", ","):
                clash = True
        if clash:
            continue
        ok = True
        for nm, a in bind.items():
            n = uses[nm]
            if n > 1 and not _simple_arg(a):
                ok = False
            if n and any(x.s in ("=", "++", "--") and k > 0 and btoks[k - 1].s == nm
                         for k, x in enumerate(btoks)):
                ok = False                       # the target assigns to its own parameter
        if not ok:
            continue

        def subst(seg: list) -> str:
            out, last = [], None
            if not seg:
                return ""
            a0 = seg[0].a
            buf = []
            pos_ = a0
            for k, x in enumerate(seg):
                buf.append(text[pos_:x.a])
                pos_ = x.b
                if x.kind == "id" and x.s in bind and not (k and seg[k - 1].s == ".") \
                        and not (k + 1 < len(seg) and seg[k + 1].s == ":" and k and seg[k - 1].s in ("(", ",")):
                    a = bind[x.s]
                    buf.append(a if _simple_arg(a) else f"({a})")
                else:
                    buf.append(text[x.a:x.b])
            return "".join(buf)

        init = ""
        if d.kind == "base":
            args_src = subst(itoks) if itoks else ""
            init = f" : base({args_src})"
        elif d.kind == "":
            init = ""
        inner_t = subst(btoks)
        inner_c = text[toks[c.blo].b:toks[c.bhi].a]
        new = f"{init}\n        {{ {{ {inner_t} }} {inner_c} }}"
        edits.append((toks[c.colon].a, toks[c.bhi].b, new))
    return edits


def ctor_chaining(text: str) -> str:
    """Inline the target of `: this(..)` into the calling constructor.

    The target is chosen by argument count and names (several candidates: skipped).  Named
    arguments are bound by parameter name, missing ones take the target's defaults, and an
    argument expression that is not a plain path or literal is only substituted for a parameter
    used once (so it is evaluated once).  Targets that return, assign their own parameters or
    chain further are resolved innermost-first or left alone.
    """
    if not re.search(r":\s*this\s*\(", text):
        return text
    return _fixpoint(text, _ctor_pass)


# ---------------------------------------------------------------------------
# null_conditional_expr:  P?.chain  as a value
# ---------------------------------------------------------------------------

_VALUE_BEFORE = {"(", ",", "=", "return", "=>", ":", "?", "{", "[", "==", "!="}
_VALUE_AFTER = {")", "]", ",", ";", "}", ":"}


def _chain_end(toks: list[Tok], i: int) -> int:
    """Index just past the member chain `id ((.|?.) id | (...) | [...] | <...>(...))*` starting at i."""
    n = len(toks)
    if i >= n or toks[i].kind != "id":
        return i
    i += 1
    while i < n:
        s = toks[i].s
        if toks[i].kind == "op" and s in (".", "?.") and i + 1 < n and toks[i + 1].kind == "id":
            i += 2
        elif toks[i].kind == "op" and s in ("(", "["):
            c = _close(toks, i)
            if c < 0:
                return i
            i = c + 1
        elif s == "<" and toks[i - 1].kind == "id":
            k, d = i, 0
            while k < n and _GENERIC_TOK.fullmatch(toks[k].s):
                d += toks[k].s == "<"
                d -= toks[k].s == ">"
                if d == 0:
                    break
                k += 1
            if k + 1 < n and d == 0 and toks[k].s == ">" and toks[k + 1].s == "(":
                i = k + 1
            else:
                return i
        else:
            break
    return i


def _conditional_expr_pass(text: str, toks: list[Tok]) -> list:
    edits = []
    n = len(toks)
    for k, t in enumerate(toks):
        if t.kind != "op" or t.s != "?.":
            continue
        ls = _path_start(toks, k)
        path = _plain(toks, ls, k)
        if path is None or ls == 0 or toks[ls - 1].s not in _VALUE_BEFORE:
            continue
        e = _chain_end(toks, k + 1)
        if e <= k + 1 or e >= n:
            continue
        chain = text[toks[k + 1].a:toks[e - 1].b]
        nxt = toks[e].s
        if nxt == "??":
            re_ = _right_end(toks, e)
            if re_ <= e + 1 or any(x.s == "??" and d == 0 for d, x in _depth_iter(toks[e + 1:re_])):
                continue
            first = toks[e + 1]
            rhs = text[first.a:toks[re_ - 1].b]
            if re_ - e == 2 and (first.kind == "num" or first.s in ("true", "false")):
                new = "(%s != null ? %s.%s : %s)" % (path, path, chain, rhs)
            elif "(" not in chain and "[" not in chain and first.s != "throw":
                new = "(%s != null && %s.%s != null ? %s.%s : %s)" % (path, path, chain, path, chain, rhs)
            else:
                continue
            edits.append((toks[ls].a, toks[re_ - 1].b, new))
        elif nxt in _VALUE_AFTER:
            edits.append((toks[ls].a, toks[e - 1].b, "(%s != null ? %s.%s : null)" % (path, path, chain)))
    return edits


def null_conditional_expr(text: str) -> str:
    """Value uses of `P?.chain` (P a plain path), without needing types.

    `P?.c ?? 0`   -> `(P != null ? P.c : 0)`            (numeric/bool fallback: c is a value type)
    `P?.c ?? R`   -> `(P != null && P.c != null ? P.c : R)`  (c without calls: read twice)
    `P?.c`        -> `(P != null ? P.c : null)`          (c must be a reference type)
    Anything in a boolean/operator context, or `P?.m() ?? R` with a non-literal R, is left alone.
    """
    if "?." not in text:
        return text
    return _fixpoint(text, _conditional_expr_pass)


# ---------------------------------------------------------------------------
# throw_to_abort:  `throw ExceptionUtilities.Unreachable();` / `throw new X(..);`  ->  `abort();`
# ---------------------------------------------------------------------------

_STMT_BEFORE = {";", "{", "}", ")", "else"}


def _throw_is_statement(toks: list[Tok], i: int) -> bool:
    prev = toks[i - 1].s if i else ";"
    if prev in _STMT_BEFORE:
        return True
    if prev == ":":                       # a `case X:` / `default:` label, not a ternary
        j = i - 2
        while j >= 0 and toks[j].s not in (";", "{", "}"):
            j -= 1
        return j + 1 < i and toks[j + 1].s in ("case", "default")
    return False


def _throw_thrown(toks: list[Tok], i: int, end: int) -> bool:
    """Is toks[i+1:end] `ExceptionUtilities.X(..)` or `new Name(..)` -- an error value we may drop?"""
    seg = toks[i + 1:end]
    if len(seg) < 3 or seg[-1].s != ")":
        return False
    if seg[0].s == "new":
        return seg[1].kind == "id" and _close(toks, i + 1 + next(k for k, x in enumerate(seg) if x.s == "(")) == end - 1 \
            if any(x.s == "(" for x in seg) else False
    return (seg[0].s == "ExceptionUtilities" and seg[1].s == "." and seg[2].kind == "id"
            and len(seg) > 3 and seg[3].s == "(" and _close(toks, i + 4) == end - 1)


def _abort_pass(text: str, toks: list[Tok]) -> list:
    edits = []
    for i, t in enumerate(toks):
        if t.kind != "id" or t.s != "throw" or i + 1 >= len(toks) or toks[i + 1].s == ";":
            continue
        end = _right_end(toks, i)
        if end >= len(toks) or toks[end].s != ";" or not _throw_thrown(toks, i, end):
            continue
        if i and toks[i - 1].s == "=>":                       # expression body
            if i < 2:
                continue
            q = toks[i - 2]
            method = prop = False
            if q.s == ")":
                o = i - 2
                depth = 0
                while o >= 0:
                    if toks[o].s == ")":
                        depth += 1
                    elif toks[o].s == "(":
                        depth -= 1
                        if depth == 0:
                            break
                    o -= 1
                method = o >= 2 and toks[o - 1].kind == "id" and toks[o - 1].s not in ("return", "await") \
                    and (toks[o - 2].kind == "id" or toks[o - 2].s in (">", "]", "?"))
            elif q.kind == "id" and i >= 3 and (toks[i - 3].kind == "id" or toks[i - 3].s in (">", "]", "?")) \
                    and toks[i - 3].s not in ("return", "await"):
                prop = True
            if method:
                edits.append((toks[i - 1].a, toks[end].b, "{ abort(); }"))
            elif prop:
                edits.append((toks[i - 1].a, toks[end].b, "{ get { abort(); } }"))
            else:                                              # a lambda
                edits.append((t.a, toks[end - 1].b, "{ abort(); }"))
            continue
        if _throw_is_statement(toks, i):
            edits.append((t.a, toks[end - 1].b, "abort()"))
    return edits


def throw_to_abort(text: str) -> str:
    """Drop the exception: an unhandled `throw` ends the process, and so does `abort()`.

    Only `throw ExceptionUtilities.Unreachable()/UnexpectedValue(..)` and `throw new T(..)`
    used as a statement (or as an expression-bodied method/property).  Code that catches
    these exceptions would behave differently; Roslyn's compiler paths do not.  Throw
    expressions (`x ?? throw ..`, switch-expression arms) are left alone.
    """
    if "throw" not in text:
        return text
    return _fixpoint(text, _abort_pass)


# ---------------------------------------------------------------------------
# named_arguments:  f(a, c: 1)  ->  f(a, <b's default>, 1)   (needs the signatures in the tree)
# ---------------------------------------------------------------------------

class Param(NamedTuple):
    name: str
    default: str | None
    special: bool            # ref / out / in / params: such a signature is not rewritten against


_CALL_KW = frozenset((
    "if", "while", "for", "foreach", "switch", "using", "lock", "catch", "return", "nameof", "typeof",
    "sizeof", "default", "fixed", "when", "checked", "unchecked", "await", "throw", "new", "is", "as",
    "in", "out", "ref", "case", "else", "do", "yield", "stackalloc", "base", "this", "where", "get", "set"))
_PARAM_MOD = frozenset(("this", "ref", "out", "in", "params", "scoped", "readonly"))
_TYPE_END = frozenset((">", "]", "?", "*"))


def _parse_param(toks: list[Tok], text: str, lo: int, hi: int) -> Param | None:
    j = lo
    while j < hi and toks[j].s == "[":                 # attributes
        c = _close(toks, j)
        if c < 0 or c >= hi:
            return None
        j = c + 1
    special = False
    while j < hi and toks[j].kind == "id" and toks[j].s in _PARAM_MOD:
        special = special or toks[j].s == "params"
        j += 1
    eq = next((k for k in range(j, hi) if toks[k].s == "="), None)
    end = eq if eq is not None else hi
    if end - j < 2:
        return None
    name, last = toks[end - 1], toks[end - 2]
    if name.kind != "id" or name.s in _CALL_KW:
        return None
    if not (last.kind == "id" or last.s in _TYPE_END) or last.s in _PARAM_MOD or last.s == "var":
        return None
    if toks[j].kind in ("num", "str", "chr"):
        return None
    dflt = None
    if eq is not None:
        if eq + 1 >= hi:
            return None
        dflt = text[toks[eq + 1].a:toks[hi - 1].b]
    return Param(name.s, dflt, special)


def signatures(text: str) -> dict:
    """{ name: [ [Param, ...], ... ] } for every method / constructor declared in text."""
    toks = tokenize(text)
    out: dict = {}
    n = len(toks)
    for i, t in enumerate(toks):
        if t.kind != "id" or t.s in _CALL_KW or i + 1 >= n or toks[i + 1].s != "(":
            continue
        prev = toks[i - 1] if i else None
        if prev is not None and (prev.s in (".", "new", "=>", "=", "(", ",", "?", ":", "!", "&&", "||", "return")):
            continue
        c = _close(toks, i + 1)
        if c < 0 or c + 1 >= n:
            continue
        after = toks[c + 1].s
        typed = prev is not None and (prev.kind == "id" and prev.s not in _CALL_KW or prev.s in _TYPE_END)
        if after == ";" and not typed:
            continue
        if after not in ("{", "=>", ";", ":", "where"):
            continue
        params = []
        ok = True
        if c > i + 2:
            for lo, hi in _split_args(toks, i + 2, c):
                pr = _parse_param(toks, text, lo, hi)
                if pr is None:
                    ok = False
                    break
                params.append(pr)
        if ok:
            out.setdefault(t.s, []).append(params)
    return out


def merge_signatures(into: dict, other: dict) -> dict:
    for k, v in other.items():
        into.setdefault(k, []).extend(v)
    return into


def _type_bases(toks: list[Tok]) -> dict:
    """{ token index of `base`/`this` : (class name, first base type name | None) } for ctor initializers."""
    res: dict = {}
    stack: list = []
    depth = 0
    pending = None
    for i, t in enumerate(toks):
        if t.kind == "id" and t.s in ("class", "struct") and i + 1 < len(toks) and toks[i + 1].kind == "id":
            base = None
            j = i + 2
            if j < len(toks) and toks[j].s == "<":
                d = 0
                while j < len(toks):
                    d += toks[j].s == "<"
                    d -= toks[j].s == ">"
                    j += 1
                    if d == 0:
                        break
            if j < len(toks) and toks[j].s == ":" and j + 1 < len(toks) and toks[j + 1].kind == "id":
                k = j + 1
                while k + 2 < len(toks) and toks[k + 1].s == "." and toks[k + 2].kind == "id":
                    k += 2
                base = toks[k].s
            pending = (toks[i + 1].s, base)
        elif t.s == "{" and t.kind == "op":
            depth += 1
            if pending is not None:
                stack.append((pending, depth))
                pending = None
        elif t.s == "}" and t.kind == "op":
            if stack and stack[-1][1] == depth:
                stack.pop()
            depth -= 1
        elif t.kind == "id" and t.s in ("base", "this") and stack and i + 1 < len(toks) and toks[i + 1].s == "(":
            res[i] = stack[-1][0]
    return res


def _named_pass(text: str, toks: list[Tok], sigs: dict) -> list:
    edits = []
    inits = _type_bases(toks)
    n = len(toks)
    for i, t in enumerate(toks):
        if t.kind != "id" or i + 1 >= n or toks[i + 1].s != "(":
            continue
        if i in inits:
            cls, base = inits[i]
            key = base if t.s == "base" else cls
        elif t.s in _CALL_KW:
            continue
        else:
            key = t.s
        cands = sigs.get(key) if key else None
        c = _close(toks, i + 1)
        if not cands or c < 0 or c == i + 2:
            continue
        args = _split_args(toks, i + 2, c)
        named = []                                        # name or None per argument
        for lo, hi in args:
            if hi - lo > 2 and toks[lo].kind == "id" and toks[lo + 1].s == ":":
                named.append(toks[lo].s)
            else:
                named.append(None)
        if not any(named) or any(hi <= lo for lo, hi in args):
            continue
        plans = set()
        fills = {}
        for sig in cands:
            if any(p.special for p in sig) or len(sig) < len(args):
                continue
            names = [p.name for p in sig]
            pos, ok = [], True
            for k, nm in enumerate(named):
                if nm is None:
                    pos.append(k)
                elif nm in names:
                    pos.append(names.index(nm))
                else:
                    ok = False
                    break
            if not ok or len(set(pos)) != len(pos):
                continue
            hole = [q for q in range(max(pos) + 1) if q not in pos]
            if any(sig[q].default is None for q in hole):
                continue
            plans.add(tuple(pos))
            fills[tuple(pos)] = {q: sig[q].default for q in hole}
        if len(plans) != 1:
            continue
        pos = next(iter(plans))
        fill = fills[pos]
        if any(not (re.fullmatch(r"[\w.]+|\"[^\"]*\"|-?\d[\w.]*|default(\([\w.<>?, ]*\))?|null|true|false|\w+\.\w+", d))
               for d in fill.values()):
            continue
        texts = [text[toks[lo + (2 if named[k] else 0)].a:toks[hi - 1].b] for k, (lo, hi) in enumerate(args)]
        if list(pos) == sorted(pos) and not fill:
            for k, (lo, hi) in enumerate(args):
                if named[k]:
                    edits.append((toks[lo].a, toks[lo + 2].a, ""))
            continue
        if list(pos) != sorted(pos) and not all(_simple_arg(x) for x in texts):
            continue                                       # reordering would reorder side effects
        slots = {pos[k]: texts[k] for k in range(len(pos))}
        slots.update(fill)
        edits.append((toks[args[0][0]].a, toks[args[-1][1] - 1].b,
                      ", ".join(slots[q] for q in range(max(pos) + 1))))
    return edits


def named_arguments(text: str, sigs: dict) -> str:
    """Pass named arguments by position, using the declarations in `sigs` (see signatures()).

    A call is rewritten only if every declaration of that name which fits agrees on where each
    argument goes.  Skipped arguments take the declared default when it is a simple literal or path;
    arguments that would move are only moved when they have no side effects.
    """
    if not re.search(r"[(,]\s*[A-Za-z_]\w*\s*:(?!:)", text):
        return text
    return _fixpoint(text, lambda t, k: _named_pass(t, k, sigs))


# ---------------------------------------------------------------------------
# nullable_attributes:  [NotNullWhen(returnValue: false)] ...  ->  (removed)
# ---------------------------------------------------------------------------

_NULLABLE_ATTRS = frozenset((
    "NotNullWhen", "MaybeNullWhen", "NotNull", "MaybeNull", "AllowNull", "DisallowNull",
    "NotNullIfNotNull", "MemberNotNull", "MemberNotNullWhen", "DoesNotReturn", "DoesNotReturnIf",
    "ReturnNotNullIfParameterNotNull"))


def _nullable_attr_pass(text: str, toks: list[Tok]) -> list:
    edits = []
    for i, t in enumerate(toks):
        if t.kind != "op" or t.s != "[" or i + 1 >= len(toks):
            continue
        c = _close(toks, i)
        if c < 0:
            continue
        j = i + 1
        if toks[j].kind == "id" and toks[j].s in ("return", "param", "field", "property") \
                and toks[j + 1].s == ":":
            j += 2
        k = j
        while k < c and toks[k].kind == "id" and toks[k].s in _NULLABLE_ATTRS:
            if k + 1 < c and toks[k + 1].s == "(":
                e = _close(toks, k + 1)
                if e < 0 or e >= c:
                    break
                k = e + 1
            else:
                k += 1
            if k < c and toks[k].s == ",":
                k += 1
        if k == c and c > i + 1:
            end = toks[c].b
            while end < len(text) and text[end] in " \t":
                end += 1
            edits.append((t.a, end, ""))
    return edits


def nullable_attributes(text: str) -> str:
    """Remove the nullable-flow attributes ([NotNullWhen(..)], [DoesNotReturn], ...): analysis hints only."""
    if "[" not in text:
        return text
    return _fixpoint(text, _nullable_attr_pass)


# ---------------------------------------------------------------------------

# Applied in order by build.py's normalize stage.
NORMALIZERS = [
    file_scoped_namespace,
    interpolated_strings,
    null_coalescing,
    null_conditional_stmt,
    null_conditional_cmp,
    null_conditional_expr,
    interlocked_cas,
    ctor_chaining,
    is_null,
    throw_to_abort,
    nullable_attributes,
    strip_string_nullable,
]


def normalize(text: str) -> str:
    for fn in NORMALIZERS:
        text = fn(text)
    return text
