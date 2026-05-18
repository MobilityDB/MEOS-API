# Consuming meos-api.json

`meos-api.json` is the **single source of truth** for the MobilityDB
ecosystem: the language-agnostic contract that every binding and engine
generates from, so a user learns one canonical surface and every platform
reproduces it. This guide is for a downstream generator (PyMEOS, JMEOS,
MEOS.NET, MobilityDuck, MobilitySpark, …): which artifacts to read, what
each contract contains, and how to verify parity.

## The artifacts (regenerate, don't hand-edit)

```
python run.py              # -> output/meos-idl.json            (catalog; carries portableAliases)
python report.py           # -> output/meos-coverage.json       (structural worklist)
python tools/portable_parity.py  # -> output/meos-portable-parity.json (bare-name parity)
```

| Artifact | Contents |
|---|---|
| `meos-idl.json#/portableAliases` | canonical operator→bare-name dialect: `byOperator`, `byBareName`, `families`, `explicitBacking`, `scope` |
| `meos-idl.json#/functions[].{network,wire,api}` | per-function projectability + decode/encode/array/out-param wire model |
| `meos-coverage.json#/worklist` | every non-exposable public function with a `class` + concrete upstream `suggest`, ranked by `byClass` |
| `meos-portable-parity.json` | each bare name → backing family, with the verified parity headline |
| OpenAPI / MCP / runtime server | generated from the same catalog |

## What each consumer produces

**MobilityDuck, MobilitySpark** — register the **exact bare names** from
`portableAliases.byOperator` (drop type-qualified forms like
`spanOverlaps`). Done = every operator in `byOperator` is callable by its
bare name, parity-checked with the same prefix logic as
`portable_parity.py`, **0 unbacked**.

**PyMEOS, JMEOS, MEOS.NET** — code-generate from `meos-idl.json`
(`functions` + `portableAliases`) so every binding emits **identical** bare
names. Done = the generated symbol set ⊇ `portableAliases` bare names, with
no per-binding exceptions.

**MobilityDB** — consume `meos-coverage.json#/worklist` for signature
uniformization, highest leverage first: class `out-param-naming` (lone
out-parameter → `result`), then `array-return-shape` (add trailing
`int *count`), then `multi-out` (return a struct). Done = the class shrinks
to 0 on the next `meos-api.json` regeneration — the catalog auto-adopts, so
there is no coupling. The `stateful` and `plumbing` classes are correct
exclusions, not gaps.

**Portable BerlinMOD** — after Duck and Spark expose the names, re-run the
portable suite (`doc/rfc/sql-portability/berlinmod/`). Done = identical
results across all three platforms using the bare names only.

## Invariants

- **`cbuffer`, `npoint`, `pose`, `rgeo` are full user-facing temporal
  types**, covered like every other type; they are never excluded from a
  parity headline.
- `trgeometry` is the user-facing name; internal functions keep the
  `trgeo_` prefix — the internal prefix is not normalized.
- Aliases **reuse each operator's own backing function** (equivalence by
  construction) — never a reimplementation.
- 100% parity is the bar: every operator has its bare name on every engine,
  with no gaps and no headline exclusions.

## References

Portable SQL dialect RFC: `doc/rfc/sql-portability/README.md`. Native
support and manual chapter land in MobilityDB; acceptance tooling in this
repo: `tests/test_portable_parity.py`, `tests/test_coverage_gate.py`.
