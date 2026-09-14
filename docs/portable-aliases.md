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
| Temporal comparison | `#=`→`tEqual` `#<>`→`tNotEqual` `#<`→`tLessThan` `#<=`→`tLessEqual` `#>`→`tGreaterThan` `#>=`→`tGreaterEqual` |
| Ever comparison | `?=`→`eEqual` `?<>`→`eNotEqual` `?<`→`eLessThan` `?<=`→`eLessEqual` `?>`→`eGreaterThan` `?>=`→`eGreaterEqual` |
| Always comparison | `%=`→`aEqual` `%<>`→`aNotEqual` `%<`→`aLessThan` `%<=`→`aLessEqual` `%>`→`aGreaterThan` `%>=`→`aGreaterEqual` |
| Distance | `<->`→`tDistance` `\|=\|`→`nearestApproachDistance` |
| Same | `~=`→`same` |

25 operator→bare-name pairs. Already-canonical (no aliasing needed):
`eIntersects`, `atTime`, restriction and spatial-relationship functions.

## Position operators

A position operator has one name per class of its operands instead: the
class followed by the position, a temporal operand taking the class of its
bounding box (MobilityDB#2717). `<<` is `setLeft`, `spanLeft`,
`spansetLeft`, `tboxLeft`, `stboxLeft` and `tpcboxLeft`; the Y and Z
positions exist for `stbox` and `tpcbox`. The mapping holds each operator
with its position, the stem those names and the MEOS C functions share
(`left_set_set`, `left_tspatial_tspatial`), under `positionFamilies`:

| Family | Operator → position |
|---|---|
| Time position | `<<#`→`before` `#>>`→`after` `&<#`→`overbefore` `#&>`→`overafter` |
| Space X | `<<`→`left` `>>`→`right` `&<`→`overleft` `&>`→`overright` |
| Space Y | `<<\|`→`below` `\|>>`→`above` `&<\|`→`overbelow` `\|&>`→`overabove` |
| Space Z | `<</`→`front` `/>>`→`back` `&</`→`overfront` `/&>`→`overback` |

The catalog derives the names by class from the `@sqlfn` tags of the
functions whose `@sqlop` is the operator (`positionNames`, 64 names for the
16 operators). A name that is not the class followed by the position, or an
operator that no function carries, stops the pipeline: either means the
tags and the mapping disagree.

## In the catalog

`portableAliases` carries the verbatim `families` and `positionFamilies`,
plus derived lookups for codegen:

```json
"portableAliases": {
  "byOperator": { "&&": "overlaps", "#=": "tEqual", "~=": "same", ... },
  "byBareName": { "overlaps": "&&", "tEqual": "#=", "same": "~=", ... },
  "bareNames":  ["aEqual", "aGreaterEqual", ..., "tLessEqual", "tLessThan", "tNotEqual"],
  "count": 25,
  "byPositionOperator": { "<<": "left", "<<#": "before", ... },
  "positionNames": {
    "<<": { "set": "setLeft", "span": "spanLeft", "spanset": "spansetLeft",
            "stbox": "stboxLeft", "tbox": "tboxLeft", "tpcbox": "tpcboxLeft" },
    "<<|": { "stbox": "stboxBelow", "tpcbox": "tpcboxBelow" }, ...
  },
  "provenance": {...}, "scope": {...}, "notes": [...]
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
prefix convention), and every position operator against the family its
position prefixes (`left_*`, `before_*`) with its SQL names by class, and
writes `output/meos-portable-parity.json`. A bare name whose C family
prefix differs is backed by the functions whose `@sqlfn` is the bare name
(`tEqual` by the `teq_*` family, `eEqual` by `ever_eq_*`, `tDistance` by
`tdistance_*`), else through `explicitBacking` (`nearestApproachDistance`
through `nad`, the `nad_*` family), and one that none of them resolves is
flagged `needs-explicit-backing` — never silently dropped.
`tests/test_portable_parity.py` gates this: no operator may be
unclassified, and over a derived catalog every operator is backed.

## Provenance

Discussion MobilityDB#861 · RFC #920
(`doc/rfc/sql-portability/README.md`, branch `rfc/sql-portability`) ·
native in MobilityDB#1075 · manual chapter MobilityDB#1078.
`tests/test_portable.py` validates the mapping and guards the scope rule.
