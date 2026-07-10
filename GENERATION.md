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

## Consuming MEOS from a binding (provision-meos)

A binding **never commits** `meos-idl.json` (or a `libmeos.so`). Both are derived artifacts of
one MobilityDB commit, and a committed copy is drift waiting to happen. Instead a binding records
the **MobilityDB commit it targets** and derives the catalog — and, for native/FFI bindings, an
installed libmeos — in CI via the shared composite action
`MobilityDB/MEOS-API/.github/actions/provision-meos@master`. One coordinate in, catalog *and*
native library out: they are generated from the same ref every run, so they always match — zero
drift.

The action checks out `MobilityDB@<ref>`, runs `run.py` to emit `output/meos-idl.json`, and
optionally builds and installs all-families libmeos. Its interface:

- inputs: `mobilitydb-ref` (required — SHA or branch), `build-libmeos` (`"true"`/`"false"`,
  default `"false"`), `families` (default `-DALL=ON`, for the optional libmeos build).
- outputs: `catalog-path` (absolute path to the generated `meos-idl.json`) and `libmeos-prefix`
  (`/usr/local` when `build-libmeos=true`, else empty).

### Minimal CI recipe

```yaml
- name: Resolve the MEOS source commit
  id: meos
  run: echo "sha=$(tr -d '[:space:]' < tools/meos-source-commit.txt)" >> "$GITHUB_OUTPUT"
- name: Provision MEOS
  id: provision
  uses: MobilityDB/MEOS-API/.github/actions/provision-meos@master
  with:
    mobilitydb-ref: ${{ steps.meos.outputs.sha }}
    build-libmeos: "true"   # true for native/FFI bindings; false for pure-catalog codegen
# catalog consumers then stage the derived catalog where their generator reads it, e.g.:
#   cp "${{ steps.provision.outputs.catalog-path }}" <path/to/meos-idl.json>
# then run the binding's own generator + tests.
```

`mobilitydb-ref` can be a **pinned SHA** — read from a tracked `meos-source-commit.txt` as above,
which makes the run reproducible — or plain `master` to **track latest**. Either way there is no
drift: the catalog (and libmeos) are regenerated from that same ref in the same run. Native/FFI
bindings pass `build-libmeos: "true"` — libmeos installs under `/usr/local` (`libmeos-prefix`),
and its install also stages `spatial_ref_sys.csv` so SRIDs resolve at runtime. Pure-catalog
bindings leave `build-libmeos` at its default and consume only `catalog-path`.

### Two archetypes

- **Catalog-deriving** — the binding drops its committed `meos-idl.json` and derives it in CI,
  copying `catalog-path` to where its generator reads it before generating sources:
  MobilitySpark (`cp $catalog-path tools/meos-idl.json`, PR #37) and JMEOS (stages to
  `codegen/input/meos-idl.json`, PR #44).
- **libmeos-only** — the binding has no catalog of its own (its facades come from `javap` over
  the JMEOS jar, not from a catalog) and uses the action purely to get libmeos installed with
  `build-libmeos: "true"`: MobilityFlink (PR #41) and MobilityKafka (PR #21).

### Adding a new binding

(a) add the two CI steps above; (b) point your generator at `catalog-path` (or `cp` it into
place); (c) `git rm` any committed `meos-idl.json` / `libmeos.so` and add them to `.gitignore`.
