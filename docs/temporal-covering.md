# Temporal-covering descriptor

`meta/temporal-covering.json` is the **single codegen source of truth**
(RFC #870 TemporalParquet / #913 Temporal Data Lake) for projecting a MEOS
temporal column into Parquet/Iceberg **covering columns**. The pipeline
folds it into `meos-idl.json` as `temporalCovering`. Every binding/engine
(PyMEOS, JMEOS, MobilityDuck, MobilitySpark, …) generates the **identical**
covering schema from this one mapping, so a temporal table prunes the same
way on every platform — no per-engine covering code to maintain.

## What it is

A temporal value is stored on disk as a canonical MEOS-WKB `BLOB`. Iceberg
and Parquet cannot prune on a `BLOB`. The covering descriptor names, per
temporal-type **class**, the primitive columns to *materialise alongside*
the value — the bounding box and SRID — which Iceberg collects as manifest
statistics and Parquet as row-group min/max. A bbox/time predicate then
prunes whole files and row groups with **no spatial-aware engine**
(GeoParquet 1.1 `covering.bbox`; MVB v3 measured this as ~10× faster than
the `ST_Intersects` path).

The mapping is keyed by **class**, not by type — adding a type is one entry
in its class:

| Class | Box | Types | Covering columns |
|---|---|---|---|
| `spatial` | `STBOX` via `tspatial_to_stbox` | tgeompoint, tgeogpoint, tgeometry, tgeography, tcbuffer, tnpoint, tpose, trgeometry | `xmin xmax ymin ymax [zmin zmax] tmin tmax srid` |
| `number` | `TBOX` via `tnumber_to_tbox` | tint, tfloat, tbigint | `vmin vmax tmin tmax` |

The canonical value column is unchanged and lossless; covering columns are
denormalised derivations of the value's box. `zmin`/`zmax` are emitted only
for 3D values (`when: hasZ`).

## In the catalog

`temporalCovering` carries the verbatim `classes`, plus derived lookups for
codegen:

```json
"temporalCovering": {
  "valueCodec": { "asHexWkb": "temporal_as_hexwkb",
                  "fromHexWkb": "temporal_from_hexwkb" },
  "byType": { "tgeompoint": { "class": "spatial", "box": {...},
                              "srid": "tspatial_srid", "columns": [...] }, ... },
  "symbols": ["stbox_xmin", "tbox_xmin", "tspatial_to_stbox", ...],
  "count": 11
}
```

- `byType` — `"tgeompoint"` → its class, box converter, SRID accessor, and
  covering columns (each with its MEOS bbox accessor and SQL type). A
  generator reads this directly; it never re-derives the mapping.
- `symbols` — every MEOS C symbol the descriptor depends on. The covering
  parity audit (`tools/covering_parity.py`) checks each is exported by the
  catalog and each covered type is a real `MeosType` — a miss is reported as
  a worklist (add/export the accessor in MEOS), never a fabricated pass.

## How a generator uses it

For a column `traj TGEOMPOINT`, emit alongside the WKB value column:

```sql
xmin = stbox_xmin(tspatial_to_stbox(traj)), xmax = stbox_xmax(...),
ymin = stbox_ymin(...),  ymax = stbox_ymax(...),
tmin = stbox_tmin(...),  tmax = stbox_tmax(...),
srid = tspatial_srid(traj)
```

(each engine in its own idiom — DuckDB generated columns, a Spark UDF
projection, a PyMEOS writer), plus the `temporal` and GeoParquet `geo` /
`covering.bbox` file metadata keys from `metadataKeys`.

## Not yet covered

- **Time-only** (`tbool`, `ttext`): a `tmin`/`tmax` covering needs a span
  lower/upper bound accessor; `temporal_to_tstzspan` is exported but a span
  bound accessor is not. Surfaced as a MEOS export gap (close in MEOS C),
  not filled binding-side.
- **Point-cloud / cell-index** (`tpcpoint`, `tpcpatch`, `th3index`,
  `tquadbin`): fold into the `spatial` class once the catalog confirms a
  uniform temporal→`STBOX` converter for these families.
