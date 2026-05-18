# Portable bare-name dialect

`meta/portable-aliases.json` is the **single codegen source of truth**
(RFC #920) for the canonical portable bare-name dialect. The pipeline folds
it into `meos-idl.json` as `portableAliases`. Every binding/engine
(PyMEOS, JMEOS, MEOS.NET, MobilityDuck, MobilitySpark, …) generates the
**identical** bare names from this one mapping, so a user learns one
reference and can assume the rest behaves the same — no per-engine
exceptions to memorise.

## What it is

For one-query-three-platforms portability, a SQL operator must be callable
by a stable bare function name. The mapping is **operator → bare name**, by
family, and is **type-agnostic** (it applies to every temporal type):

| Family | Operator → bare name |
|---|---|
| Topology | `&&`→`overlaps` `@>`→`contains` `<@`→`contained` `-\|-`→`adjacent` |
| Time position | `<<#`→`before` `#>>`→`after` `&<#`→`overbefore` `#&>`→`overafter` |
| Space X | `<<`→`left` `>>`→`right` `&<`→`overleft` `&>`→`overright` |
| Space Y | `<<\|`→`below` `\|>>`→`above` `&<\|`→`overbelow` `\|&>`→`overabove` |
| Space Z | `<</`→`front` `/>>`→`back` `&</`→`overfront` `/&>`→`overback` |
| Temporal comparison | `#=`→`teq` `#<>`→`tne` `#<`→`tlt` `#<=`→`tle` `#>`→`tgt` `#>=`→`tge` |
| Distance | `<->`→`tdistance` `\|=\|`→`nearestApproachDistance` |
| Same | `~=`→`same` |

29 operator→bare-name pairs. Already-canonical (no aliasing needed):
`ever_*`/`always_*` (`?=`/`%=`), `eIntersects`, `atTime`, restriction and
spatial-relationship functions.

## In the catalog

`portableAliases` carries the verbatim `families`, plus derived bijective
lookups for codegen:

```json
"portableAliases": {
  "byOperator": { "&&": "overlaps", "#=": "teq", "~=": "same", ... },
  "byBareName": { "overlaps": "&&", "teq": "#=", "same": "~=", ... },
  "bareNames":  ["above", "adjacent", ..., "tdistance", "tge", "tne"],
  "count": 29, "provenance": {...}, "scope": {...}, "notes": [...]
}
```

The mapping is preserved exactly — **no C-symbol guessing**. Upstream
generates each alias by reusing the operator's *own* backing C function
(equivalence by construction; mirror MobilityDB
`tools/portable_aliases/generate.py` + its 100%-coverage audit).

## Scope (the corrected rule)

`cbuffer`, `npoint`, `pose`, `rgeo` are **full user-facing temporal types
and are in scope** — covered like every other type. MobilityDB PR #1075
already aliases all six families (`temporal`, `geo`, `cbuffer`, `npoint`,
`pose`, `rgeo` — 1303 aliases). They must **not** be excluded from any
parity headline. An upstream/audit note that "defers" or "jointly excludes"
them is a known error being corrected: where another engine defers them,
that is incomplete work to close (a gap with a plan), never an accepted
end state.

`trgeometry` is the user-facing name; internal functions keep the
`trgeo_` prefix — do **not** normalize the internal prefix.

## Parity audit

`portable_parity.py` is the meos-api.json analogue of MobilityDB's
`tools/portable_aliases/generate.py --check`: it cross-references every
bare name against the catalog's function families (by the MEOS bare-name
prefix convention) and writes `output/meos-portable-parity.json`.

Live result: **29 / 29 = 100%** — every operator's bare name is backed in
the catalog (28 directly by prefix; `nearestApproachDistance` via the
*verified* `explicitBacking` entry `nad` — the `nad_*` family, 35
functions, confirmed present, not guessed). A bare name whose C family
prefix differs is resolved through `explicitBacking`, never false-flagged
as a gap and never silently dropped; `tests/test_portable_parity.py`
gates this (no bare name may be unclassified or regressed).

## Provenance

Discussion MobilityDB#861 · RFC #920
(`doc/rfc/sql-portability/README.md`, branch `rfc/sql-portability`) ·
native in MobilityDB#1075 · manual chapter MobilityDB#1078.
`tests/test_portable.py` validates the mapping and guards the scope rule.
