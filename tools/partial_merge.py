#!/usr/bin/env python3
"""Merge C# `partial` types into one declaration each.

Crust's C# subset refuses `partial`, and 575 Roslyn files stop there first.
This module rewrites a set of C# sources so that every partial type is a
single ordinary type:

  * the first file (in sorted order) that declares a partial type is its
    carrier; the members of every other part are appended to the carrier's
    body, under a `// ---- from <file> ----` comment;
  * nested partial types are merged the same way, recursively;
  * the `partial` modifier is dropped, modifiers / attributes / base types /
    `where` clauses that only some parts spell out are added to the carrier;
  * `using` directives of the other parts are added to the carrier's file;
  * a file left with nothing but usings and comments is dropped.

The parser is deliberately shallow: it masks comments, strings and char
literals, then walks `{ }` nesting only as deep as namespaces and types. All
other text (members, directives, usings) is kept verbatim as opaque chunks,
so everything that is not a type header passes through byte for byte.

Line numbers are not preserved in rewritten files; `MergeResult.maps` gives,
per rewritten file, which original file and line each output line came from.

    from partial_merge import merge_partials
    result = merge_partials({"a/X.cs": "...", "a/X.Part.cs": "..."})
"""

from __future__ import annotations

import bisect
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field


class ParseError(Exception):
    pass


# ---------------------------------------------------------------------------
# Masking: blank comments, strings, char literals and directive lines
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r'//|/\*|[$@]*"|\'|^[ \t]*#', re.M)
_NOT_NL = re.compile(r"[^\n]")


def _skip_regular(s: str, i: int) -> int:
    i += 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif c == "\n":
            return i
        else:
            i += 1
    return n


def _skip_verbatim(s: str, i: int) -> int:
    i += 1
    n = len(s)
    while i < n:
        if s[i] == '"':
            if s[i + 1:i + 2] == '"':
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _skip_char(s: str, i: int) -> int:
    j = i + 1
    j += 2 if s[j:j + 1] == "\\" else 1
    n = len(s)
    while j < n and s[j] not in "'\n":
        j += 1
    return j + 1 if s[j:j + 1] == "'" else j


def _skip_interp(s: str, i: int, verbatim: bool) -> int:
    i += 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and not verbatim:
            i += 2
        elif c == '"':
            if verbatim and s[i + 1:i + 2] == '"':
                i += 2
                continue
            return i + 1
        elif c == "{":
            if s[i + 1:i + 2] == "{":
                i += 2
            else:
                i = _skip_hole(s, i + 1)
        elif c == "\n" and not verbatim:
            return i
        else:
            i += 1
    return n


def _skip_hole(s: str, i: int) -> int:
    depth = 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        elif c == '"':
            i = _skip_regular(s, i)
            continue
        elif c == "'":
            i = _skip_char(s, i)
            continue
        elif c == "$" and s[i + 1:i + 2] == '"':
            i = _skip_interp(s, i + 1, False)
            continue
        i += 1
    return n


def iter_tokens(s: str):
    """Yield (start, end, token) for each comment, string/char literal and
    preprocessor-directive line in `s`; `token` is what introduced it."""
    i = 0
    n = len(s)
    while True:
        m = _TOKEN.search(s, i)
        if not m:
            return
        a = m.start()
        tok = m.group()
        if tok == "//" or tok.endswith("#"):
            j = s.find("\n", a)
            j = n if j < 0 else j
        elif tok == "/*":
            j = s.find("*/", a + 2)
            j = n if j < 0 else j + 2
        elif tok == "'":
            j = _skip_char(s, a)
        else:
            prefix = tok[:-1]
            q = a + len(prefix)
            if s.startswith('"""', q) and "@" not in prefix:     # raw string
                k = q
                while s[k:k + 1] == '"':
                    k += 1
                e = s.find('"' * (k - q), k)
                j = n if e < 0 else e + (k - q)
            elif "$" in prefix:
                j = _skip_interp(s, q, "@" in prefix)
            elif "@" in prefix:
                j = _skip_verbatim(s, q)
            else:
                j = _skip_regular(s, q)
        j = max(j, a + 1)
        yield a, j, tok
        i = j


def mask_source(s: str, comments_only: bool = False) -> str:
    """Same length as `s`, with comments, literals and directives blanked.

    With `comments_only`, string and char literals and directives are kept."""
    out: list[str] = []
    last = 0
    for a, j, tok in iter_tokens(s):
        out.append(s[last:a])
        keep = comments_only and tok != "//" and tok != "/*"
        out.append(s[a:j] if keep else _NOT_NL.sub(" ", s[a:j]))
        last = j
    out.append(s[last:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Conditional compilation: resolve #if / #elif / #else / #endif
# ---------------------------------------------------------------------------

_COND = re.compile(r"^[ \t]*#[ \t]*(if|elif|else|endif|define|undef)\b(.*)$")
_IDENT = re.compile(r"[A-Za-z_]\w*")


def _eval_cond(expr: str, defs: set) -> bool:
    expr = re.sub(r"//.*$", "", expr).strip()

    def name(m):
        w = m.group()
        if w in ("true", "false"):
            return w.capitalize()
        return f"({w!r} in defs)"

    py = _IDENT.sub(name, expr)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    try:
        return bool(eval(py, {"__builtins__": {}}, {"defs": defs}))
    except Exception as e:                      # noqa: BLE001
        raise ParseError(f"cannot evaluate '#if {expr}'") from e


def preprocess(src: str, defines) -> str:
    """Resolve conditional compilation as Crust does: only `defines` are set.

    Directive lines and inactive lines become empty lines, so line numbers
    are unchanged. Other directives (#nullable, #pragma, #region) are kept.
    """
    defs = set(defines)
    out = []
    stack: list[list[bool]] = []        # [parent_active, any_branch_taken, active]
    active = True
    for line in src.split("\n"):
        m = _COND.match(line.rstrip("\r"))
        if not m:
            out.append(line if active else line[len(line.rstrip("\r")):])
            continue
        kw, rest = m.group(1), m.group(2)
        cr = line[len(line.rstrip("\r")):]
        if kw == "if":
            parent = active
            taken = parent and _eval_cond(rest, defs)
            stack.append([parent, taken, taken])
            active = taken
        elif kw == "elif":
            if not stack:
                raise ParseError("#elif without #if")
            st = stack[-1]
            val = st[0] and not st[1] and _eval_cond(rest, defs)
            st[1] = st[1] or val
            st[2] = val
            active = val
        elif kw == "else":
            if not stack:
                raise ParseError("#else without #if")
            st = stack[-1]
            val = st[0] and not st[1]
            st[1] = True
            st[2] = val
            active = val
        elif kw == "endif":
            if not stack:
                raise ParseError("#endif without #if")
            st = stack.pop()
            active = st[0]
        else:                              # define / undef
            if active:
                sym = rest.split()[0] if rest.split() else ""
                (defs.add if kw == "define" else defs.discard)(sym)
            out.append(line)
            continue
        out.append(cr)
    if stack:
        raise ParseError("unterminated #if")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Tree: namespaces and types, everything else opaque
# ---------------------------------------------------------------------------

@dataclass
class Text:
    text: str
    msk: str
    file: str
    line: int


@dataclass
class Block:
    kind: str                 # "namespace" | "type"
    header: str               # original text before the `{`
    hmask: str                # same, masked
    name: str
    key: str                  # name`arity for types
    partial: bool
    children: list
    file: str
    line: int
    others: list = field(default_factory=list)   # headers of merged parts


_SEMI_BRACE = re.compile(r"[;{}()\[\]]")
_BRACES = re.compile(r"[{}]")
_MODS = r"(?:public|private|protected|internal|static|sealed|abstract|partial|unsafe|readonly|ref|file|new|virtual)"
_TYPE_RE = re.compile(r"(?<![\w.])(class|struct|interface|record)\b(?:\s+(?:class|struct)\b)?\s+([A-Za-z_]\w*)")
_NS_RE = re.compile(r"^\s*namespace\s+([\w.]+)\s*$")
_PRE_RE = re.compile(rf"\s*(?:{_MODS}\s+)*")
_FILE_NS = re.compile(r"\s*namespace\s+([\w.]+)\s*;")
_USING = re.compile(r"(?m)^[ \t]*(?:global[ \t]+)?using\b(?![ \t]*\()[^;]*;")
_EQ = re.compile(r"(?<![=!<>])=(?!=)")


def depth_mask(h: str) -> str:
    """Blank everything inside (...) and [...], delimiters included."""
    out = []
    d = 0
    for ch in h:
        if ch in "([":
            d += 1
            out.append(" ")
        elif ch in ")]":
            d = max(0, d - 1)
            out.append(" ")
        else:
            out.append(" " if d and ch != "\n" else ch)
    return "".join(out)


def _arity(h0: str, end: int) -> tuple[int, int]:
    """If a `<...>` follows position `end`, return (arity, end after '>')."""
    if h0[end:end + 1] != "<":
        return 0, end
    d = 0
    commas = 0
    for k in range(end, len(h0)):
        ch = h0[k]
        if ch == "<":
            d += 1
        elif ch == ">":
            d -= 1
            if d == 0:
                return commas + 1, k + 1
        elif ch == "," and d == 1:
            commas += 1
    return 1, len(h0)


def _type_match(h0: str):
    for m in _TYPE_RE.finditer(h0):
        if m.group(2) == "where":
            continue
        if _PRE_RE.fullmatch(h0[:m.start()]):
            return m
        break
    return None


def classify(hmask: str):
    """-> (kind, name, key, partial); kind is namespace | type | other."""
    h0 = depth_mask(hmask)
    m = _NS_RE.match(h0)
    if m:
        return "namespace", m.group(1), m.group(1), False
    m = _type_match(h0)
    if m:
        ar, _ = _arity(h0, m.end())
        name = m.group(2)
        partial = re.search(r"\bpartial\b", h0[:m.start()]) is not None
        return "type", name, f"{name}`{ar}", partial
    return "other", "", "", False


def _has_eq(hmask: str) -> bool:
    return _EQ.search(depth_mask(hmask)) is not None


class _Ctx:
    def __init__(self, src: str, file: str):
        self.src = src
        self.msk = mask_source(src)
        self.file = file
        self.nl = [i for i, c in enumerate(src) if c == "\n"]

    def line(self, pos: int) -> int:
        return bisect.bisect_left(self.nl, pos) + 1

    def text(self, a: int, b: int) -> Text:
        return Text(self.src[a:b], self.msk[a:b], self.file, self.line(a))


def _match_brace(msk: str, i: int, hi: int) -> int:
    depth = 0
    for m in _BRACES.finditer(msk, i, hi):
        depth += 1 if m.group() == "{" else -1
        if depth == 0:
            return m.start()
    raise ParseError("unbalanced '{'")


def _find_semi(msk: str, i: int, hi: int) -> int:
    depth = 0
    for m in re.compile(r"[;{}]").finditer(msk, i, hi):
        c = m.group()
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                raise ParseError("unbalanced '}'")
        elif depth == 0:
            return m.start()
    raise ParseError("missing ';'")


def _next_code(msk: str, i: int, hi: int) -> str:
    while i < hi and msk[i].isspace():
        i += 1
    return msk[i] if i < hi else ""


def _parse_scope(ctx: _Ctx, lo: int, hi: int) -> list:
    msk = ctx.msk
    items: list = []
    pos = i = lo
    pd = 0                                   # paren / bracket depth in segment
    while True:
        m = _SEMI_BRACE.search(msk, i, hi)
        if not m:
            break
        c = m.group()
        k = m.start()
        if c in "([":
            pd += 1
            i = k + 1
            continue
        if c in ")]":
            pd = max(0, pd - 1)
            i = k + 1
            continue
        if c == ";":
            i = k + 1
            if pd == 0:
                items.append(ctx.text(pos, i))
                pos = i
            continue
        if c == "}":
            raise ParseError("unbalanced '}'")
        j = _match_brace(msk, k, hi)         # c == "{"
        if pd > 0:                           # initializer inside (...) / [...]
            i = j + 1
            continue
        hmask = msk[pos:k]
        kind, name, key, partial = classify(hmask)
        if kind != "other":
            kids = _parse_scope(ctx, k + 1, j)
            items.append(Block(kind, ctx.src[pos:k], hmask, name, key, partial,
                               kids, ctx.file, ctx.line(pos)))
            i = pos = j + 1
            continue
        end = j + 1
        if _has_eq(hmask) or _next_code(msk, end, hi) in ("=", ";"):
            end = _find_semi(msk, end, hi) + 1
        items.append(ctx.text(pos, end))
        i = pos = end
    if pos < hi:
        items.append(ctx.text(pos, hi))
    return items


def parse(src: str, file: str = "<src>") -> list:
    ctx = _Ctx(src, file)
    return _parse_scope(ctx, 0, len(src))


# ---------------------------------------------------------------------------
# Header analysis and merging
# ---------------------------------------------------------------------------

@dataclass
class _Hdr:
    kw_start: int
    mid_end: int
    colon: int | None
    bases_end: int
    where: int | None
    h0: str


def _analyze(b: Block) -> _Hdr:
    h0 = depth_mask(b.hmask)
    m = _type_match(h0)
    if not m:
        raise ParseError(f"cannot read type header in {b.file}:{b.line}")
    _, name_end = _arity(h0, m.end())
    mid_end = name_end
    t = b.hmask[name_end:]
    ls = t.lstrip()
    if ls.startswith("("):                      # record primary constructor
        d = 0
        for k, ch in enumerate(ls):
            d += ch == "("
            d -= ch == ")"
            if d == 0:
                mid_end = name_end + (len(t) - len(ls)) + k + 1
                break
    wm = re.search(r"\bwhere\b", h0[mid_end:])
    where = mid_end + wm.start() if wm else None
    bases_end = where if where is not None else len(b.header)
    cm = re.search(r"(?<!:):(?!:)", h0[mid_end:bases_end])
    colon = mid_end + cm.start() if cm else None
    return _Hdr(m.start(), mid_end, colon, bases_end, where, h0)


def _split_commas(text: str) -> list[str]:
    out, d, cur = [], 0, []
    for ch in text:
        if ch in "<(":
            d += 1
        elif ch in ">)":
            d -= 1
        if ch == "," and d == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _attr_lists(b: Block, kw_start: int) -> list[str]:
    out, i = [], 0
    h = b.hmask
    while i < kw_start:
        if h[i] == "[":
            d = 0
            for k in range(i, len(h)):
                d += h[k] == "["
                d -= h[k] == "]"
                if d == 0:
                    out.append(b.header[i:k + 1])
                    i = k
                    break
        i += 1
    return out


_ACCESS = {"public", "private", "protected", "internal"}


def render_header(b: Block) -> str:
    """The header of a (possibly merged) type, with `partial` removed."""
    if b.kind != "type":
        return b.header
    hd = _analyze(b)
    header = b.header
    edits: list[tuple[int, int, str]] = []
    for m in re.finditer(r"\bpartial\b[ \t]*", hd.h0[:hd.kw_start]):
        edits.append((m.start(), m.end(), ""))

    words = re.findall(r"\w+", hd.h0[:hd.kw_start])
    have = set(words)
    has_access = bool(have & _ACCESS)
    attrs = {_norm(a) for a in _attr_lists(b, hd.kw_start)}
    bases = _split_commas(header[hd.colon + 1:hd.bases_end]) if hd.colon is not None else []
    base_norm = {_norm(x) for x in bases}
    add_mods: list[str] = []
    add_attrs: list[str] = []
    add_bases: list[str] = []
    where_text = None
    for o in b.others:
        oh = _analyze(o)
        for w in re.findall(r"\w+", oh.h0[:oh.kw_start]):
            if w == "partial" or w in have or w in add_mods:
                continue
            if w in _ACCESS and has_access:
                continue
            add_mods.append(w)
        for a in _attr_lists(o, oh.kw_start):
            if _norm(a) not in attrs:
                attrs.add(_norm(a))
                add_attrs.append(a)
        if oh.colon is not None:
            for x in _split_commas(o.header[oh.colon + 1:oh.bases_end]):
                if _norm(x) not in base_norm:
                    base_norm.add(_norm(x))
                    add_bases.append(x)
        if hd.where is None and where_text is None and oh.where is not None:
            where_text = o.header[oh.where:].strip()
    if add_mods:
        edits.append((hd.kw_start, hd.kw_start, " ".join(add_mods) + " "))
    if add_attrs:
        cs = len(b.hmask) - len(b.hmask.lstrip())
        edits.append((cs, cs, "\n".join(add_attrs) + "\n"))
    if where_text:                 # listed first: at a tie it is applied first,
        end = len(header.rstrip())   # so it ends up after the base types
        edits.append((end, end, " " + where_text))
    if add_bases:
        if hd.colon is not None:
            pos = hd.colon + 1 + len(header[hd.colon + 1:hd.bases_end].rstrip())
            edits.append((pos, pos, ", " + ", ".join(add_bases)))
        else:
            edits.append((hd.mid_end, hd.mid_end, " : " + ", ".join(add_bases)))
    for s, e, r in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        header = header[:s] + r + header[e:]
    return header


def _merge_into(carrier: Block, part: Block) -> None:
    carrier.others.append(part)
    carrier.others.extend(part.others)
    nested = {c.key: c for c in carrier.children
              if isinstance(c, Block) and c.kind == "type" and c.partial}
    new: list = [Text(f"\n        // ---- from {part.file} ----\n", "", part.file, part.line)]
    for ch in part.children:
        if isinstance(ch, Block) and ch.kind == "type" and ch.partial:
            if ch.key in nested:
                _merge_into(nested[ch.key], ch)
                continue
            nested[ch.key] = ch
        new.append(ch)
    carrier.children.extend(new)


# ---------------------------------------------------------------------------
# Partial methods: a bodiless declaration plus an implementation are one method
# ---------------------------------------------------------------------------

_PM_RE = re.compile(r"\bpartial\b(?!\s+(?:class|struct|interface|record)\b)")


def _partial_method(t: Text):
    """-> (partial_span, signature, is_declaration) or None for a Text chunk."""
    h = depth_mask(t.msk)
    m = _PM_RE.search(h)
    if not m:
        return None
    k = t.msk.find("(", m.end())
    if k < 0:
        return None
    nm = re.search(r"(\w+)\s*(?:<[^<>()]*>)?\s*$", t.msk[m.end():k])
    if not nm:
        return None
    d = 0
    for e in range(k, len(t.msk)):
        d += t.msk[e] == "("
        d -= t.msk[e] == ")"
        if d == 0:
            break
    else:
        return None
    sig = nm.group(1) + _norm(t.msk[k:e + 1])
    after = t.msk[e + 1:].lstrip()
    return m, sig, after.startswith(";"), k


def _attr_groups(text: str, msk: str, end: int) -> list[str]:
    out, i = [], 0
    while i < end:
        if msk[i] == "[":
            d = 0
            for k in range(i, len(msk)):
                d += msk[k] == "["
                d -= msk[k] == "]"
                if d == 0:
                    out.append(text[i:k + 1])
                    i = k
                    break
        i += 1
    return out


def _pair_partial_methods(children: list) -> bool:
    """Fuse declaration + implementation pairs among these members (recursing
    into nested types). Returns True if anything changed."""
    changed = False
    decls: dict = {}
    for idx, c in enumerate(children):
        if isinstance(c, Block):
            if c.kind == "type":
                changed |= _pair_partial_methods(c.children)
            continue
        info = _partial_method(c)
        if info and info[2]:
            decls.setdefault(info[1], []).append(idx)
    drop = set()
    for idx, c in enumerate(children):
        if not isinstance(c, Text):
            continue
        info = _partial_method(c)
        if not info or info[2]:
            continue
        m, _, _, _ = info
        carry: list[str] = []
        if decls.get(info[1]):
            didx = decls[info[1]].pop(0)
            d = children[didx]
            dm, _, _, _ = _partial_method(d)
            carry = _attr_groups(d.text, d.msk, dm.start())
            have = {_norm(a) for a in _attr_groups(c.text, c.msk, m.start())}
            carry = [a for a in carry if _norm(a) not in have]
            drop.add(didx)
        # else: the declaration is in a file that is not part of the build
        # (e.g. generated code); the implementation stands alone as a method.
        text = c.text[:m.start()] + re.sub(r"^[ \t]*", "", c.text[m.end():], count=1)
        if carry:
            cs = len(c.msk) - len(c.msk.lstrip())
            text = text[:cs] + "\n".join(carry) + "\n" + text[cs:]
        children[idx] = Text(text, mask_source(text), c.file, c.line)
        changed = True
    if drop:
        children[:] = [c for i, c in enumerate(children) if i not in drop]
    return changed


def _pair_in_tree(children: list) -> bool:
    changed = False
    for c in children:
        if isinstance(c, Block):
            if c.kind == "namespace":
                changed |= _pair_in_tree(c.children)
            else:
                changed |= _pair_partial_methods(c.children)
    return changed


# ---------------------------------------------------------------------------
# Whole-file operations
# ---------------------------------------------------------------------------

def _usings(children: list) -> list[tuple[str, str, int]]:
    """(normalized, text, line) of the using directives outside type bodies."""
    out = []
    for c in children:
        if isinstance(c, Text):
            for m in _USING.finditer(c.msk):
                if c.text[:m.start()].count("#if") > c.text[:m.start()].count("#endif"):
                    continue
                txt = c.text[m.start():m.end()].strip()
                out.append((_norm(txt), txt, c.line))
        elif c.kind == "namespace":
            out.extend(_usings(c.children))
    return out


def _has_partial(children: list) -> bool:
    for c in children:
        if isinstance(c, Block):
            if c.kind == "type" and c.partial:
                return True
            if _has_partial(c.children):
                return True
    return False


def _has_content(children: list) -> bool:
    """True if anything besides usings, comments and blank space remains."""
    for c in children:
        if isinstance(c, Block):
            if c.kind == "type" or _has_content(c.children):
                return True
        else:
            rest = _USING.sub("", c.msk)
            rest = _FILE_NS.sub("", rest)
            if rest.strip(" \t\r\n;"):
                return True
    return False


def _render(children: list, out: list) -> None:
    for c in children:
        if isinstance(c, Text):
            out.append((c.text, c.file, c.line))
        else:
            out.append((render_header(c) + "{", c.file, c.line))
            _render(c.children, out)
            out.append(("}", c.file, c.line))


@dataclass
class _File:
    path: str
    children: list
    contrib: list = field(default_factory=list)   # parts merged into this file


@dataclass
class MergeResult:
    files: dict            # path -> new text, or None if the file is dropped
    maps: dict             # path -> [(output line, original file, original line)]
    types_merged: int = 0
    parts_merged: int = 0
    single_part: int = 0
    errors: dict = field(default_factory=dict)      # path -> message
    leftover: list = field(default_factory=list)    # paths still holding `partial`


def merge_partials(sources: dict, defines=None, pair_methods: bool = True) -> MergeResult:
    """Merge the partial types in `sources` ({path: text}).

    With `defines` (an iterable of symbols, possibly empty), conditional
    compilation is resolved first, so only active code is merged.
    `pair_methods` fuses partial method declarations with their implementations.
    """
    parsed: dict[str, _File] = {}
    res = MergeResult({}, {})
    for path in sorted(sources):
        try:
            text = sources[path] if defines is None else preprocess(sources[path], defines)
            parsed[path] = _File(path, parse(text, path))
        except ParseError as e:
            res.errors[path] = str(e)

    reg: dict = {}
    touched: set = set()

    def absorb(children, ns, fn):
        out = []
        for c in children:
            if isinstance(c, Text):
                m = _FILE_NS.match(c.msk)
                if m:
                    ns = ns + (m.group(1),)
            elif c.kind == "namespace":
                c.children = absorb(c.children, ns + (c.name,), fn)
            elif not c.partial and _has_partial(c.children):
                touched.add(fn.path)            # nested partial in a plain type
            elif c.partial:
                key = (ns, c.key)
                if key in reg:
                    cb, cfn = reg[key]
                    _merge_into(cb, c)
                    res.parts_merged += 1
                    touched.add(cfn.path)
                    touched.add(fn.path)
                    if fn is not cfn and fn not in cfn.contrib:
                        cfn.contrib.append(fn)
                    continue
                reg[key] = (c, fn)
                res.single_part += 1
                touched.add(fn.path)
            out.append(c)
        return out

    for fn in parsed.values():
        fn.children = absorb(fn.children, (), fn)

    for fn in parsed.values():
        if pair_methods and _pair_in_tree(fn.children):
            touched.add(fn.path)

    res.types_merged = sum(1 for _, (b, _) in reg.items() if b.others)
    res.single_part -= res.types_merged

    for fn in parsed.values():
        if fn.contrib:
            have = {u[0] for u in _usings(fn.children)}
            extra = []
            for pf in fn.contrib:
                for norm, txt, _ in _usings(pf.children):
                    if norm not in have:
                        have.add(norm)
                        extra.append(txt)
            if extra:
                idx = next((i for i, c in enumerate(fn.children) if isinstance(c, Block)), len(fn.children))
                fn.children.insert(idx, Text("".join(u + "\n" for u in extra), "", fn.path, 1))

    for path in sorted(touched):
        fn = parsed[path]
        if not _has_content(fn.children):
            res.files[path] = None
            continue
        pieces: list = []
        _render(fn.children, pieces)
        text = "".join(p[0] for p in pieces)
        res.files[path] = text
        line, mp = 1, []
        for t, f, ln in pieces:
            mp.append((line, f, ln))
            line += t.count("\n")
        res.maps[path] = mp
        if re.search(r"\bpartial\b\s+(?:class|struct|interface|record)\b", mask_source(text)):
            res.leftover.append(path)
    return res


def member_chunks(text: str, file: str = "<src>") -> Counter:
    """Multiset of whitespace-normalized member chunks (for invariant checks)."""
    c: Counter = Counter()

    def walk(children):
        for ch in children:
            if isinstance(ch, Block):
                walk(ch.children)
            else:
                rest = _USING.sub("", ch.msk)
                rest = _FILE_NS.sub("", rest)
                if rest.strip(" \t\r\n;"):
                    c[_norm(_DIRECTIVE.sub("", mask_source(ch.text, True)))] += 1

    walk(parse(text, file))
    return c


_DIVIDER = re.compile(r"[ \t]*// ---- from [^\n]*\n")
_DIRECTIVE = re.compile(r"(?m)^[ \t]*#[^\n]*\n?")
