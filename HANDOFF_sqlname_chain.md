# Handoff → MobilityDB/MEOS session: SQL-name chain + Doxygen `@csqlfn` irregularities

**From:** MobilityDuck session · **Re:** ecosystem naming policy (the `MEOS-C → MobilityDB-C → SQL` name chain)
**Status:** report only — the source edits below are **yours** (MobilityDB repo). No changes pushed to MobilityDB by me.

## 1. Context — the naming policy this supports
Every function has up to three names, linked in the C sources by Doxygen tags:

| Layer | Form | Example | Tag |
|---|---|---|---|
| MEOS C (lib) | lowercase snake | `tdistance_tnumber_number` | carries `@csqlfn #<wrapper>` |
| MobilityDB C (PG wrapper, `PG_FUNCTION_ARGS`) | PascalCase (avoids symbol clash) | `Tdistance_tnumber_number` | carries `@sqlfn`/`@sqlop` |
| MobilityDB SQL (user) | lowerCamel, overloaded | `tDistance` + op `<->` | — |

SQL names are **frozen** (production); bindings conform *to* MobilityDB. The chain is the source of truth for every binding's names.

## 2. What I built (MEOS-API, for your awareness — not requiring action)
A catalog extractor on branch `feat/sql-name-chain` (fork `estebanzimanyi/MEOS-API`): `parser/sqlnames.py` + a `run.py` step that attaches `mobilitydb` / `sql` / `sqlop` to each IDL function from the `@csqlfn`/`@sqlfn`/`@sqlop` tags. It now resolves the chain for **1589** functions. (I'll PR this separately; it only *reads* your tags.)

## 3. The irregularity to fix in MobilityDB source (the actual ask)
`@csqlfn` is placed on **core Datum-generic** functions in addition to the binding-callable typed datum-hiding wrappers in `*_meos.c`. Since a Datum-generic is **not** binding-callable (per the external-typed-wrapper rule), its `@csqlfn` is redundant and makes the chain attach a SQL name to an uncallable function.

**Rule:** `@csqlfn` belongs **only** on binding-callable functions (the typed `*_meos.c` wrappers), never on the core Datum-generic.

### (A) 31 — safe to de-tag now (a `*_meos.c` wrapper already carries the same `@csqlfn`)
- **24** comparison-family, `meos/src/temporal/temporal_compops.c`: `always_{ne,ge,gt,le,lt}_{base_temporal,temporal_base}` and `ever_{ne,ge,gt,le,lt}_{base_temporal,temporal_base}` — wrappers `always_ne_int_tint`/`_float_tfloat`/`_text_ttext` (etc.) keep the tag.
- **7** others: `set_make`, `set_value_n` (set.c); `numspan_shift_scale` (span.c); `union_span_value` (span_ops.c); `adjacent_value_spanset` (spanset_ops.c); `temporal_value_n` (temporal.c); `tinstant_value_at_timestamptz` (tinstant.c); `tsequence_value_at_timestamptz` (tsequence.c); `tsequenceset_value_n` (tsequenceset.c); `tnumberinst_shift_value` (tinstant.c); `tnumberseq_shift_scale_value` (tsequence.c).

→ Edit: remove the `@csqlfn …` line from each core Datum-generic's Doxygen block.

### (B) 4 — need a typed `*_meos.c` wrapper added first, then de-tag
No `*_meos.c` carrier exists, so don't just delete the tag: `tbox_expand_value` (tbox.c), `number_tstzspan_to_tbox` (tbox.c), `number_tbox`→`Number_to_tbox` (tbox.c), `tnumber_value_time_boxes` (temporal_tile.c).

## 4. How to verify
After the edits, re-run the MEOS-API extractor — the count of "Datum-param functions carrying a SQL name" should drop from 35 → 4 (then → 0 once the (B) wrappers exist). That number is a standing regression gate for this class of irregularity.
