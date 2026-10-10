#!/usr/bin/env python3
"""Build a minimal subset of Roslyn without .NET.

Usage:
    python3 build.py              # run all stages
    python3 build.py merge        # stop after the merge stage
    python3 build.py normalize    # stop after the normalize stage
    python3 build.py check        # run every file through csrust, report blockers
    python3 build.py list         # list the selected source files
    python3 build.py stats        # summarize the selected source set
    python3 build.py clean        # remove the build directory

Stages: collect -> merge -> normalize -> compile -> link.

  collect   select the .cs files (INCLUDE_DIRS / EXCLUDE_PATTERNS)
  merge     resolve #if with DEFINES, then merge `partial` types into one
            declaration each (tools/partial_merge.py). Rewritten files go to
            build/merged/; the list of files later stages should read is
            build/merged_sources.txt. A rewritten file's .map says which
            original file and line each output line came from.
  normalize small rewrites Crust needs (tools/normalize.py): `string?` ->
            `string`, file-scoped namespaces, `$""` strings, and the forms of
            `??` / `?.` that can be rewritten without type information.
            Changed files go to build/normalized/; the final list is
            build/final_sources.txt.
  partition files that still use `is` go to DNA, the rest to Crust:
            build/crust_sources.txt and build/dna_sources.txt.
  compile   not implemented yet (waiting on the toolchain plan)
  link      not implemented yet

`check` is not a stage: it runs Crust's C# translator (tools/csrust.py, with
--coost for strings) over the final file list and prints how many files
translate and what stops the rest. Results: build/check.json. Crust lowers
`ref`/`out` only for methods declared in the text it translates, so each file
is given a stub class declaring every Roslyn method name that takes one (what
translating all files as one unit would provide); --no-ref-stubs turns that off.
Locations: ../crust and ../coost, or $CRUST_ROOT / $COOST_ROOT.
"""

import argparse
import collections
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
BUILD = ROOT / "build"
MERGED = BUILD / "merged"
NORMALIZED = BUILD / "normalized"

CRUST = Path(os.environ.get("CRUST_ROOT", ROOT.parent / "crust"))
COOST = Path(os.environ.get("COOST_ROOT", ROOT.parent / "coost"))

sys.path.insert(0, str(ROOT / "tools"))
import normalize  # noqa: E402
import partial_merge  # noqa: E402

# Conditional-compilation symbols that are defined. Crust defines CRUST and
# nothing else, so the merge step resolves #if the same way Crust will.
DEFINES = ["CRUST"]

# ---------------------------------------------------------------------------
# Subset definition
#
# Directories (relative to ROOT) whose .cs files are part of the build, and
# glob patterns to leave out. Start small and grow the subset as it compiles.
# ---------------------------------------------------------------------------

INCLUDE_DIRS = [
    "src/Compilers/Core/Portable",
    "src/Compilers/CSharp/Portable",
]

# Patterns are matched against the path relative to ROOT, using POSIX slashes.
EXCLUDE_PATTERNS = [
    "*/obj/*",
    "*/bin/*",
    "*/Generated/*",          # generated syntax/bound nodes: revisit later
]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def collect_sources() -> list[Path]:
    """Return the sorted list of .cs files in the current subset."""
    files = []
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            sys.exit(f"error: include dir not found: {d}")
        for p in base.rglob("*.cs"):
            r = rel(p)
            if any(fnmatch.fnmatch(r, pat) for pat in EXCLUDE_PATTERNS):
                continue
            files.append(p)
    return sorted(files)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def stage_collect() -> list[Path]:
    files = collect_sources()
    BUILD.mkdir(exist_ok=True)
    manifest = BUILD / "sources.txt"
    manifest.write_text("\n".join(rel(p) for p in files) + "\n")
    print(f"[collect] {len(files)} source files -> {rel(manifest)}")
    return files


def read_source(path: Path) -> str:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return f.read()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def stage_merge(files: list[Path]) -> list[Path]:
    """Merge partial types. Returns the files later stages should compile."""
    if MERGED.exists():
        shutil.rmtree(MERGED)
    res = partial_merge.merge_partials({rel(p): read_source(p) for p in files},
                                       defines=DEFINES)
    out: list[Path] = []
    dropped = 0
    for p in files:
        r = rel(p)
        if r not in res.files:
            out.append(p)                    # unchanged (or unparseable)
        elif res.files[r] is None:
            dropped += 1                     # nothing left in it after merging
        else:
            dest = MERGED / r
            write_text(dest, res.files[r])
            write_text(dest.with_name(dest.name + ".map"),
                       "".join(f"{o}\t{f}\t{ln}\n" for o, f, ln in res.maps[r]))
            out.append(dest)
    manifest = BUILD / "merged_sources.txt"
    manifest.write_text("\n".join(rel(p) for p in out) + "\n")
    rewritten = len(res.files) - dropped
    print(f"[merge] {res.types_merged} partial types merged from "
          f"{res.parts_merged + res.types_merged} parts; "
          f"{res.single_part} single-part partials unmarked; "
          f"{rewritten} files rewritten, {dropped} dropped -> {len(out)} files "
          f"({rel(manifest)})")
    for path, msg in sorted(res.errors.items()):
        print(f"[merge] warning: {path} left as is: {msg}", file=sys.stderr)
    for path in res.leftover:
        print(f"[merge] warning: `partial` remains in {path}", file=sys.stderr)
    return out


def stage_normalize(files: list[Path]) -> list[Path]:
    """Apply tools/normalize.py. Returns the final list of files."""
    if NORMALIZED.exists():
        shutil.rmtree(NORMALIZED)
    out: list[Path] = []
    changed = 0
    texts: dict[Path, tuple[str, str]] = {}
    sigs: dict = {}
    for p in files:                                   # per-file rewrites
        text = read_source(p)
        new = normalize.normalize(text)
        texts[p] = (text, new)
        normalize.merge_signatures(sigs, normalize.signatures(new))
    for p in files:                                   # rewrites that need every file's declarations
        text, new = texts[p]
        new = normalize.named_arguments(new, sigs)
        if new == text:
            out.append(p)
            continue
        dest = NORMALIZED / rel(p)
        write_text(dest, new)
        out.append(dest)
        changed += 1
    manifest = BUILD / "final_sources.txt"
    manifest.write_text("\n".join(rel(p) for p in out) + "\n")
    print(f"[normalize] {changed} files rewritten -> {len(out)} files ({rel(manifest)})")
    return out


def uses_type_pattern(text: str) -> bool:
    """True if the code (not comments or strings) still has an `is` test or pattern."""
    return any(t.kind == "id" and t.s == "is" for t in normalize.tokenize(text))


def stage_partition(files: list[Path], quiet: bool = False) -> tuple[list[Path], list[Path]]:
    """Split the final files: Crust subset vs. files that run under DNA.

    A file goes to DNA when it still contains `is` (type tests and patterns need a runtime
    type, which the Crust subset does not have). The decision is per file; the lists are
    build/crust_sources.txt and build/dna_sources.txt.
    """
    crust: list[Path] = []
    dna: list[Path] = []
    for p in files:
        (dna if uses_type_pattern(read_source(p)) else crust).append(p)
    (BUILD / "crust_sources.txt").write_text("\n".join(rel(p) for p in crust) + "\n")
    (BUILD / "dna_sources.txt").write_text("\n".join(rel(p) for p in dna) + "\n")
    if not quiet:
        print(f"[partition] {len(crust)} files for Crust (build/crust_sources.txt), "
              f"{len(dna)} for DNA (build/dna_sources.txt)")
    return crust, dna


def stage_compile(files: list[Path]) -> None:
    # TODO: depends on the toolchain plan (no .NET available).
    print("[compile] not implemented yet")


def stage_link() -> None:
    # TODO
    print("[link] not implemented yet")


def prepare() -> list[Path]:
    files = stage_normalize(stage_merge(stage_collect()))
    stage_partition(files)
    return files


def cmd_build(_args) -> None:
    files = prepare()
    stage_compile(files)
    stage_link()


def cmd_merge(_args) -> None:
    stage_merge(stage_collect())


def cmd_normalize(_args) -> None:
    prepare()


# Crust's own test for "a method declared with a ref/out parameter"
# (tools/cs2cpp.py, _lower_ref_out): name-based, no receiver or type.
_REF_PARAM = re.compile(r"(?<=[(,])(\s*)(?:ref|out)\s+([A-Za-z_][\w.]*(?:\s*<[^()<>]*>)?"
                        r"(?:\s*\[\s*\])*\s*\*?)\s+([A-Za-z_]\w*)(?=\s*[,)=])")
_METHOD = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(([^()]*)\)\s*(?:\{|=>|;)")


_REF_CALL = re.compile(r"(?<![\w])([A-Za-z_]\w*)\s*\((?:[^()]|\([^()]*\))*?\b(?:ref|out)\s")


def ref_names(files: list[Path]) -> set[str]:
    """Names of all methods declared with a ref/out parameter."""
    names: set[str] = set()
    for p in files:
        msk = partial_merge.mask_source(read_source(p))
        for m in _METHOD.finditer(msk):
            if _REF_PARAM.search("(" + m.group(2) + ")"):
                names.add(m.group(1))
    return names


def ref_stub(names: set[str], text: str) -> str:
    """One line of C#: a class declaring the ref/out methods this file calls."""
    msk = partial_merge.mask_source(text)
    used = sorted({m.group(1) for m in _REF_CALL.finditer(msk)} & names)
    if not used:
        return ""
    decls = " ".join(f"void {n}(ref int a);" for n in used)
    return f"\nclass __RefSigs {{ {decls} }}\n"


def cmd_check(args) -> None:
    csrust = CRUST / "tools" / "csrust.py"
    if not csrust.is_file():
        sys.exit(f"error: {csrust} not found (clone crust next to this repo or set CRUST_ROOT)")
    extra = []
    if not args.no_coost:
        if not (COOST / "include").is_dir():
            sys.exit(f"error: coost not found at {COOST} (set COOST_ROOT or pass --no-coost)")
        extra = ["--coost", str(COOST)]
    files = prepare()
    names = set() if args.no_ref_stubs else ref_names(files)   # all files: DNA-side declarations count too
    if not args.all:
        files = stage_partition(files, quiet=True)[0]                      # only what is meant for Crust

    def probe(path: Path):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "o.c"
            src = path
            text = read_source(path)
            stub = ref_stub(names, text) if names else ""
            if stub:                       # same file name, so messages keep it
                src = Path(td) / path.name
                write_text(src, text + stub)
            try:
                r = subprocess.run([sys.executable, "-I", str(csrust), str(src), "-o", str(out)] + extra,
                                   capture_output=True, text=True, timeout=120, cwd=td)
            except subprocess.TimeoutExpired:
                return rel(path), -1, "timeout"
            msg = ""
            if r.returncode != 0:
                msg = out.read_text(errors="replace") if out.exists() else ""
                msg = (msg or r.stderr or r.stdout).strip()
                if msg.startswith("Traceback"):          # the exception is the useful line
                    msg = "crash: " + msg.splitlines()[-1]
            return rel(path), r.returncode, msg.splitlines()[0][:300] if msg else ""

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        results = list(ex.map(probe, files))
    BUILD.mkdir(exist_ok=True)
    (BUILD / "check.json").write_text(json.dumps(
        [{"file": f, "ok": rc == 0, "msg": m} for f, rc, m in results], indent=1))
    ok = sum(1 for _, rc, _ in results if rc == 0)
    print(f"[check] {ok}/{len(results)} files translate "
          f"({'with' if extra else 'without'} coost, {'with' if names else 'without'} ref/out stubs); "
          f"details in build/check.json")
    blockers = collections.Counter()
    for _, rc, m in results:
        if rc != 0:
            blockers[re.sub(r"^[^:]*:\d+:\s*", "", m)[:70]] += 1
    print("first blocker per file:")
    for m, n in blockers.most_common(args.top):
        print(f"  {n:5}  {m}")


def cmd_list(_args) -> None:
    for p in collect_sources():
        print(rel(p))


def cmd_stats(_args) -> None:
    files = collect_sources()
    by_dir: dict[str, list[int]] = {}
    for p in files:
        key = "/".join(rel(p).split("/")[:5])
        n = sum(1 for _ in p.open("rb"))
        s = by_dir.setdefault(key, [0, 0])
        s[0] += 1
        s[1] += n
    for key, (count, lines) in sorted(by_dir.items()):
        print(f"{key:55} {count:5} files {lines:9} lines")
    print(f"{'TOTAL':55} {len(files):5} files "
          f"{sum(v[1] for v in by_dir.values()):9} lines")


def cmd_clean(_args) -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
        print(f"removed {rel(BUILD)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    for name, fn in [("build", cmd_build), ("merge", cmd_merge), ("normalize", cmd_normalize),
                     ("list", cmd_list), ("stats", cmd_stats), ("clean", cmd_clean)]:
        sub.add_parser(name).set_defaults(fn=fn)
    chk = sub.add_parser("check")
    chk.add_argument("--no-coost", action="store_true", help="translate without coost (strings refused)")
    chk.add_argument("--no-ref-stubs", action="store_true",
                     help="do not declare Roslyn's ref/out method names to each file")
    chk.add_argument("--all", action="store_true",
                     help="also check the files the partition sends to DNA")
    chk.add_argument("--top", type=int, default=12, help="how many blockers to list")
    chk.set_defaults(fn=cmd_check)
    ap.set_defaults(fn=cmd_build)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
