# Temporal-covering descriptor

`meta/temporal-covering.json` is the **single codegen source of truth**
(RFC #870 TemporalParquet / #913 Temporal Data Lake) for projecting a MEOS
temporal column into the **covering columns** of TemporalParquet 2.0.0. The
pipeline folds it into `meos-idl.json` as `temporalCovering`. Every
binding/engine (PyMEOS, JMEOS, MobilityDuck, MobilitySpark, …) generates the
**identical** covering schema from this one mapping, so a temporal table
prunes the same way on every platform — no per-engine covering code to
maintain.

## What it is

A temporal value is stored on disk as a canonical MEOS-WKB `BLOB`. Iceberg
and Parquet cannot prune on a `BLOB`. The covering descriptor names, per
temporal-type **class**, the columns to *materialise alongside* the value —
struct columns at the root of the schema, named after the temporal column,
whose fields Iceberg collects as manifest statistics and Parquet as
row-group min/max. A bbox/time predicate on those fields then prunes whole
files and row groups with **no spatial-aware engine** (MVB v3 measured this
as ~10× faster than the `ST_Intersects` path).

The mapping is keyed by **class**, not by type — adding a type is one entry
in its class:

| Class | Box | Types | Covering columns |
|---|---|---|---|
| `spatial` | `STBOX` via `tspatial_to_stbox` | tgeompoint, tgeogpoint, tgeometry, tgeography, tcbuffer, tnpoint, tpose, trgeometry | `{col}_bbox` {`xmin`, `ymin`, [`zmin`,] `xmax`, `ymax`[, `zmax`]} · `{col}_tspan` {`tmin`, `tmax`} · `srid` |
| `number` | `TBOX` via `tnumber_to_tbox` | tint, tfloat, tbigint | `{col}_vspan` {`vmin`, `vmax`} · `{col}_tspan` {`tmin`, `tmax`} |
| `timeOnly` | — | tbool, ttext | `{col}_tspan` {`tmin`, `tmax`} |

`{col}_bbox` is a GeoParquet bounding box column: its fields are `DOUBLE`, in
the order shown, it has the repetition of its temporal column, and it holds a
value exactly when the temporal column does. `zmin`/`zmax` are emitted only
for 3D values (`when: hasZ`). `srid` is a plain column beside the coverings.

`{col}_vspan` holds the value bounds in the base type of the temporal type,
read off the value with `tint_min_value`/`tint_max_value` (`int`),
`tbigint_min_value`/`tbigint_max_value` (`bigint`) and
`tfloat_min_value`/`tfloat_max_value` (`double`). A bound that went through
`double` would lose exactness above 2^53 for `tbigint`, and a minimum
rounded upwards would let pruning skip a matching row. Because the three
number types differ in base type, the `vspan` covering gives its fields per
type (`byType`); a covering whose fields hold for the whole class gives them
once (`fields`).
The canonical value column is unchanged and lossless; covering columns are
denormalised derivations of the value's box.

## In the catalog

`temporalCovering` carries the verbatim `classes`, plus derived lookups for
codegen:

```json
"temporalCovering": {
  "valueCodec": { "asHexWkb": "temporal_as_hexwkb",
                  "fromHexWkb": "temporal_from_hexwkb" },
  "byType": { "tgeompoint": { "class": "spatial", "box": {...},
                              "srid": "tspatial_srid",
                              "coverings": [...], "columns": [...] }, ... },
  "symbols": ["stbox_xmin", "tbox_xmin", "tspatial_to_stbox", ...],
  "count": 13
}
```

- `byType` — `"tgeompoint"` → its class, box converter, SRID accessor, its
  coverings (each with its key, its column name and its fields, every field
  with its MEOS accessor and SQL type) and its plain columns. A generator
  reads this directly; it never re-derives the mapping.
- `symbols` — every MEOS C symbol the descriptor depends on. The covering
  parity audit (`tools/covering_parity.py`) checks each is exported by the
  catalog and each covered type is a real `MeosType` — a miss is reported as
  a worklist (add/export the accessor in MEOS), never a fabricated pass.

The parser rejects a `bbox` covering whose fields are not a GeoParquet
bounding box column's, in its order, and a class that declares a covering
twice.

## How a generator uses it

`generate_covering.py` projects the catalog onto the language-agnostic
contract: per type, each covering with its column name and its fields in
order, every field with the MEOS expression that derives it from `VALUE`.
For a column `traj TGEOMPOINT`, a generator substitutes `VALUE` with the
column reference and `{col}` with its name, and emits alongside the WKB
value column:

```sql
traj_bbox  = {xmin: stbox_xmin(tspatial_to_stbox(traj)),
              ymin: stbox_ymin(...), xmax: stbox_xmax(...), ymax: stbox_ymax(...)},
traj_tspan = {tmin: stbox_tmin(tspatial_to_stbox(traj)), tmax: stbox_tmax(...)},
srid       = tspatial_srid(traj)
```

(each engine in its own idiom — a DuckDB struct column, a Spark UDF
projection, a PyMEOS writer), and declares the coverings in the `temporal`
file metadata key under `covering`, with `bbox` in GeoParquet's form.

## Not yet covered

- **Point-cloud / cell-index** (`tpcpoint`, `tpcpatch`, `th3index`,
  `tquadbin`): fold into the `spatial` class once the catalog confirms a
  uniform temporal→`STBOX` converter for these families.
