# MEOS-API generation — the catalog producer and the ecosystem chain

MEOS-API is the **root** of the per-binding generator policy: it is the **catalog producer**,
not a generated binding. Every other repo is a projection *of* this catalog.

## The policy (ecosystem-wide)

Every MobilityDB language/surface binding is a **pure projection of the MEOS-API catalog**,
and each binding owns its own generator, in its own repo, in a canonical layout. The single
source of truth is the **catalog** this repo produces: `output/meos-idl.json`, generated from
the MEOS C headers.

## What MEOS-API generates

`run.py <meos/include>` parses the MEOS public headers with libclang and emits
`output/meos-idl.json`: every function, struct, and enum with signatures, ownership, shape
(output arrays / nullability), recovered collapsed C types (`bool`/`int64`/`Timestamp`/…
that the preprocessor flattens to `int`), `@ingroup` groups, the `@sqlfn` SQL-name map, and
the portable bare-name aliases. The `generator/` modules project the catalog onto the
language-**agnostic** service contracts (OpenAPI, MCP, the runtime server, the OGC Moving
Features projection) — the surfaces that need no foreign toolchain. Language bindings live in
their own repos and generate from this catalog.

## The chain (do not invert)

```
MobilityDB pin
  -> MEOS-API   run.py  -> output/meos-idl.json   (+ libmeos.so built from the same pin)
       -> JMEOS  (jar)  -> { MobilitySpark, MobilityFlink, MobilityKafka }
       -> PyMEOS-CFFI   -> PyMEOS
       -> GoMEOS / MEOS.NET / meos-rs / MobilityDuck / MobilityNebula
```

## Turnkey: regenerate the whole ecosystem from a pin

`tools/ecosystem-generate.sh <PIN>` runs the chain in dependency order: build the catalog
(`run.py`) + `libmeos.so` from the pin, then invoke each binding's own
`tools/regen-from-pin.sh` in the order above (the JVM consumers after the JMEOS jar; PyMEOS
after PyMEOS-CFFI). Each binding owns its regeneration; this script only sequences them. See
the script header for the repo + frontier-branch table it drives (each binding's frontier is
recorded in its own `tools/pin/compose-order.txt`).

## Pinning

The catalog is reproducible from a MobilityDB `ecosystem-pin-*`:
`MDB_SRC_ROOT=<pin-worktree> python3 run.py <pin-worktree>/meos/include`. MEOS-API's own
`tools/pin/compose-order.txt` governs *this repo's* enrichment/projection PR accumulate.
