<p align="center">
<img width="450" src="https://user-images.githubusercontent.com/46729679/109719841-17b7dd00-7b5e-11eb-8f5e-87eb2d4d1be9.png" alt="Roslyn logo">
</p>

<h1 align="center">The .NET Compiler Platform</h1>

Roslyn is the open-source implementation of C# for compilers with an API surface for building code analysis tools.
- Designed to work with: https://github.com/crustos/CCSharp


## Building without .NET (work in progress)

`build.py` builds a subset of this tree without a .NET SDK. The plan has two halves:

- code that fits the C# subset supported by [Crust](https://github.com/brentharts/crust) is translated by `tools/csrust.py` (C# -> C++ -> Rust-like -> C), with [coost](https://github.com/crustos/coost) providing `string`;
- code that cannot be ported to that subset runs under [DNA](https://github.com/crustos/DotNetAnywhere), a CIL interpreter written in C.

Today the pipeline runs through **partition** and can report how much of the Crust half translates. The **compile** and **link** stages are stubs, and the DNA half is not wired up yet.

### Quick start

Clone [crust](https://github.com/brentharts/crust) and [coost](https://github.com/crustos/coost) next to this repo, or point `CRUST_ROOT` / `COOST_ROOT` at them.

```
python3 -I build.py            # collect -> merge -> normalize -> partition
python3 -I build.py check      # translate the Crust partition with csrust --coost (about 6 minutes)
python3 build.py stats         # size of the selected source set
python3 build.py clean         # remove build/
python3 tests/test_normalize.py
python3 tests/test_partial_merge.py   # about 50 seconds, needs the Roslyn tree
```

Everything is written under `build/` (git-ignored); your sources are never modified.

### Stages

| Stage | What it does | Output |
|---|---|---|
| collect | Selects `.cs` files from `src/Compilers/Core/Portable` and `src/Compilers/CSharp/Portable`, minus `obj`, `bin` and `Generated` | `build/sources.txt` |
| merge | Resolves `#if` with only `CRUST` defined, then merges each `partial` type into one declaration (`tools/partial_merge.py`); partial methods are fused with their implementation | `build/merged/`, `build/merged_sources.txt` |
| normalize | Mechanical rewrites into the Crust subset (`tools/normalize.py`, below) | `build/normalized/`, `build/final_sources.txt` |
| partition | Files that still use `is` go to DNA, the rest to Crust | `build/crust_sources.txt`, `build/dna_sources.txt` |
| compile, link | Not implemented | |

`check` is not a stage: it runs Crust's translator over the Crust partition (`--all` includes the DNA files), writes `build/check.json`, and prints the first blocker per file. It emulates Crust's one-translation-unit mode for `ref`/`out` by declaring, per file, the `ref`/`out` method names that file calls.

### What `normalize` rewrites

All rewrites are token-based, work without type information, and keep line numbers stable.

| Rewrite | Notes |
|---|---|
| File-scoped `namespace X;` | Becomes a block |
| `string?` | Becomes `string` |
| `$"..{x}.."` | Becomes concatenation; format and alignment specifiers are left alone |
| `a ?? b`, `a ??= b` | For plain paths `a` |
| `a?.M();` as a statement | Becomes an `if` |
| `a?.B == literal` / `is null` | Explicit null checks |
| `P?.chain` as a value | `?? 0` / `?? false` is exact; `?? R` on a call-free chain reads the chain twice; plain `P?.c` assumes a reference type |
| `x is null`, `x is not null` | Become `== null` / `!= null` |
| `Interlocked.CompareExchange(ref P, V, C);` | Single-threaded: `if (P == C) { P = V; }` (no atomicity; `V` is evaluated only on a swap) |
| `: this(..)` | The target constructor is inlined into the caller |
| Named arguments | Passed by position using the declarations found in the tree; skipped parameters take their default |
| `throw ExceptionUtilities.X(..);`, `throw new T(..);` | Become `abort();`, so code that catches those exceptions would behave differently |
| `[NotNullWhen(..)]`, `[DoesNotReturn]`, ... | Removed (analysis hints only) |

Rewrites that cannot be done exactly without types are left alone and show up as blockers in `check`.

### Where it stands

Of 2084 collected files, 1725 remain after the merge (359 are absorbed into another file). The partition sends 1170 to Crust and 555 to DNA. With coost and the `ref`/`out` emulation, 275 of the 1170 Crust files translate.

First blockers among the rest, roughly: coost string forms (50), `ref` to library methods (21), `?.` on non-plain receivers (14), `params` (14), `??` on call results (12), `ReadOnlySpan<T>` (11), range-`for` (10), `yield` (10).

Not done yet: `params`, `yield`, `[Conditional("DEBUG")]` elimination, the DNA half, the Crust/DNA interface across files, and the compile and link stages.

### .NET Foundation

This project is part of the [.NET Foundation](http://www.dotnetfoundation.org/projects) along with other projects like [the .NET Runtime](https://github.com/dotnet/runtime/).
