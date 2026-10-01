# Service-projection enrichment

The libclang parser can only report what is written in the C headers:
function signatures, structs, enums. Projecting MEOS onto a network service
(OpenAPI / MCP / gRPC) needs information the headers do **not** contain — what
each function *is*, how each opaque type crosses the wire, and whether an
operation can be served *statelessly*.

`parser/enrich.py` derives that metadata from the parsed catalog. It runs
**before** `merge_meta`, so every field below can be overridden per
function/type from `meta/meos-meta.json`.

> All values here are heuristic defaults. They are intended to be *curated*
> over time through `meta/meos-meta.json`; the heuristics give a correct-by-
> default starting point and keep coverage measurable.

## 1. Function `category`

Each function gets one `category` (first matching rule wins):

| category         | rule (by name / signature)                                  |
|------------------|-------------------------------------------------------------|
| `lifecycle`      | name starts `meos_` (init/finalize/configuration)           |
| `index`          | name starts `rtree_`                                         |
| `io`             | name matches an in/out encoding pattern (`_in`, `_out`, `_from_mfjson`, `_as_hexwkb`, `_from_wkb`, …) |
| `aggregate`      | name ends `_transfn`/`_combinefn`/`_finalfn`, or `_tagg`/`_collect` |
| `predicate`      | comparison/topological/temporal predicate by name (`*_eq`, `*_lt`, `contains_*`, `intersects_*`, `dwithin_*`, `ever_*`, `always_*`, `t{eq,contains,…}_*`, …) or returns `bool`. MEOS predicates return `int`, so name patterns — not the return type — drive this. |
| `constructor`    | name ends `_make`/`_copy`                                    |
| `setop`          | name starts `union_`/`intersection_`/`minus_`/`difference_` |
| `conversion`     | name contains `_to_`/`_from_`/`_from_base`/`_as_`           |
| `accessor`       | name ends with a component/property pattern (`_value`, `_srid`, `_duration`, `_num_*`, …) |
| `transformation` | default: value → value of the same family                   |

## 2. `typeEncodings`

A top-level map: class → the functions a binding reads and writes its values with, and
the trailing inputs each takes. `parser/enrich.py` builds the map from the shape of the
C functions; `parser/codecs.py` completes it once the SQL signatures and the bound
literals are in the catalog.

```json
"typeEncodings": {
  "Set": {
    "encodings": ["text", "wkb"],
    "decoders": { "wkb": "set_from_hexwkb" },
    "decoderAux": { "wkb": [] },
    "encoders": { "wkb": "set_as_hexwkb" },
    "encoderAux": { "wkb": [{"name": "variant", "kind": "integer", "default": 4}] },
    "readers": { "text": { "intset": "intset_in", "floatset": "floatset_in", "...": "..." } },
    "readerAux": { "text": { "intset": [], "floatset": [], "...": "..." } },
    "writers": { "text": { "intset": "intset_out", "floatset": "floatset_out", "...": "..." } },
    "writerAux": { "text": { "floatset": [{"name": "maxdd", "kind": "integer", "default": 15}] } },
    "in": "set_from_hexwkb", "in_aux": [],
    "out": "set_as_hexwkb", "out_aux": [{"name": "variant", "kind": "integer", "default": 4}],
    "bytes": { "decoder": "set_from_wkb", "encoder": "set_as_wkb",
               "encoderAux": [{"name": "variant", "kind": "integer", "default": 4}] }
  }
}
```

- **decoder** — a public `const char * (+ aux) → T` (`*_in`, `*_from_mfjson`,
  `*_from_hexwkb`, …); **encoder** — a public `T (+ aux) → char *` (`*_out`,
  `*_as_mfjson`, `*_as_hexwkb`, …). A size the function writes back is an
  out-parameter, stated in its `shape.outParams` as for any other function.
- **A PostgreSQL type passed by value** (`TimestampTz`, `Timestamp`, `TimeADT`,
  `DateADT`) is a class like a pointer type, its decoder returning it and its
  encoder taking it without a pointer (`timestamptz_in` / `timestamptz_out`).
  PostgreSQL's spelling `pg_X` yields to the name MEOS states it under, `X`,
  when both serve a class (`timetz_in`, never `pg_timetz_in`).
- **`readers` / `writers`** — a class several SQL types share (`Set`, `Span`,
  `SpanSet`, `Temporal`) reads and writes each type through that type's own public
  function, keyed by the SQL type its signature returns (a reader) or takes (a writer).
  An encoding with a `readers` or `writers` entry has no single `decoders` or
  `encoders` entry. Within one SQL type, the function the encoding table names first
  wins (`cbuffer_out` before `cbuffer_as_ewkt`), then the narrower one
  (`cbufferset_out` before `spatialset_out`); two that tie stop the catalog. An
  encoding whose functions carry no SQL signature (`GSERIALIZED`, whose geometry and
  geography are PostGIS's types) keeps the function the C shapes give it.
- **`in` / `out`** — the class's single decoder and encoder in the order `text` >
  `mfjson` > `wkb`, absent when no single function serves the whole class.
- **Trailing inputs** — every decoder, encoder, reader and writer states the inputs
  after its value by name with the value a binding passes, in `decoderAux`,
  `encoderAux`, `readerAux` and `writerAux` (and `in_aux` / `out_aux`,
  `bytes.encoderAux`). A binding builds each call from the function's own parameters,
  filling each trailing input by name, and refuses one the catalog does not fill.
- **Default of a trailing input** — the literal MEOS's own calls pass for a
  parameter of its name, where every call passing a literal passes the same one
  (`parser/boundargs.py` `extract_call_literals`): every literal `typmod` is -1,
  PostgreSQL's unspecified type modifier, so `interval_in`, `pg_timetz_in` and
  `numeric_in` read with -1. A name passed two literals (`srid`, `maxdd`) or one
  macro takes the formatting default: `maxdd` and `precision` 15, flags 0, `srs`
  NULL.
- **`variant`** of a WKB writer — the value the type's own `send` binds
  (`WKB_EXTENDED`, 4, keeping the SRID), else the value its SQL hex writer passes when
  the byte order is left out (`asHexWKB(raster, endian DEFAULT '')` passes 0).
- **A type of its own** — a value whose slot carries a `typedef` (`H3Index`,
  `Quadbin`, `S2CellId`, each a `uint64_t`) is a class keyed by that name, read and
  written by its own functions.

The class's `encodings`, `in` and `out` are folded onto each `structs[*]` entry as
`serialization`.

## 3. `network` and `wire`

For every function:

```json
"category": "predicate",
"network": { "exposable": true, "method": "POST", "reason": null },
"wire": {
  "params": [
    { "name": "temp1", "kind": "serialized", "cType": "Temporal *",
      "decode": "temporal_in", "encodings": ["mfjson","text","wkb"] },
    { "name": "temp2", "kind": "serialized", "cType": "Temporal *",
      "decode": "temporal_in", "encodings": ["mfjson","text","wkb"] }
  ],
  "result": { "kind": "json", "json": "boolean" }
}
```

`wire` element `kind`:

| kind         | meaning                                              | JSON Schema hint |
|--------------|------------------------------------------------------|------------------|
| `json`       | scalar; `json` ∈ integer/number/boolean/string. Enum types add `"enum": "<EnumName>"` | direct |
| `serialized` | opaque value carried as a string in its `encodings`; `decode`/`encode` names the MEOS function | `{"type":"string"}` + media type |
| `array`      | JSON array of `element` (an `Elem` builder param, or an `Elem **`+count return) | `{"type":"array","items":…}` |
| `void`       | no return value                                      | `204` |
| `unsupported`| cannot be represented in a stateless request/response| — |

> **Canonical spellings.** libclang emits canonical C, not source aliases:
> `struct Temporal *` (not `Temporal *`), `unsigned char` (not `uint8_t`),
> `long` (not `int64_t`), and MEOS uses `int` for booleans. The heuristics
> match canonical spellings; `struct`/`union`/`enum` qualifiers are stripped
> so `typeEncodings` keys are clean (`Temporal`, not `struct Temporal`).
> `enum` parameters are scalars; only declared structs become `serialized`.

### Exposability

`network.exposable` is `true` iff **every** parameter is `json` or
`serialized` **and** the result is `json`, `serialized`, or `void`.
Otherwise `exposable` is `false` and `reason` lists the blockers
(deduplicated, `;`-joined):

| reason                       | cause                                                       |
|------------------------------|-------------------------------------------------------------|
| `lifecycle` / `index`        | library plumbing, not a domain operation                    |
| `array-or-out-param:<name>`  | parameter is a pointer to a scalar/array or an out-parameter (`T **`, `int *`, `double *`, …) |
| `no-decoder:<type>`          | opaque parameter type has no parser function                |
| `no-encoder:<type>`          | opaque return type has no serializer function               |
| `unsupported-return:<type>`  | return cannot be represented on the wire                    |

> **Out-parameters.** A common MEOS accessor shape —
> `bool f(.., T *result)` (or `T **result`) — returns its value through a
> trailing out-parameter, with the `bool`/`int` return acting as a presence
> flag (`void` = always present). Two safe shapes are recognised: a
> **scalar** `T *result` becomes the JSON `result`; an **opaque**
> `T **result` becomes a `serialized` result via the type's encoder. In
> both cases `from_outparam`/`out_ctype`/`presence_return` annotate the
> wire result, the function is `exposable`, and a false presence return
> maps to *no value* (HTTP 204). This recovers the public `*_value_n`,
> box-bound, and `*_value_at`/geo-accessor families.

> **Input-array builders.** A builder taking `(Elem **arr, int count)`
> becomes one wire param of `kind: "array"` (element = the serialized
> `Elem`); the `count` is implicit (the JSON array length). This recovers
> `*_make` / `*_merge_array` / `*arr_to_*` builders whose element type is
> decodable.

> **Array returns.** An accessor `Elem **f(.., int *count)` returning a
> freshly-allocated element array becomes a `result` of `kind: "array"`
> (element = the serialized `Elem`, `count_outparam` names the byref
> length). Recovers `temporal_instants`/`segments`/`sequences`,
> `tgeo_values`, `geo_pointarr`, … whose element type is encodable.

This is the precise, machine-checkable boundary of what a generator can emit
today. Functions still blocked only by `array-or-out-param` (multi- or
array out-params, builder `T **`+count) are the candidates for the next,
hand-designed composite-endpoint unit; everything `exposable` can be
generated mechanically.

## 4. Overriding

`merge_meta` applies `meta/meos-meta.json` *after* enrichment, so any derived
field can be corrected by hand:

```json
{
  "functions": {
    "temporal_at_value": { "category": "transformation" },
    "some_fn": { "network": { "exposable": false, "reason": "side-effect" } }
  }
}
```

## 5. Catalog summary

`enrich_idl` adds an `enrichment` block for coverage tracking. Run against
the live MobilityDB `master` catalog (2672 functions, 47 structs, 6 enums):

```json
"enrichment": {
  "categoryCounts": { ... },
  "publicFunctions": 2161,
  "internalFunctions": 511,
  "exposableFunctions": 1963
}
```

MEOS has two API surfaces: the **public user API** (`meos.h` + the public
type headers, 2161 functions) and the **internal programmer API**
(`meos_internal*.h`, 511 functions — type-erased `Datum`-generic,
undocumented for end users). A network service projects the *user* API, so
internal functions are **policy-excluded** (`reason: internal`, like
`lifecycle`/`index`); `132/133` `Datum` functions are internal and never
belonged in the parity denominator.

So **1963 / 2161 = 91% of the public API** projects onto a stateless
endpoint as-is — verified by a strict invariant (every exposable function
has only scalar/enum/serialized/array params with a real decoder, and an
encodable result; 0 violations; 0 internal leaks). The 209-function public
remainder is dominated by **irregular** signatures with no clean stateless
shape (mixed/odd `array-or-out-param`, raw-array `unsupported-return`,
out-params of codec-less types) plus `SkipList` aggregate state and
`lifecycle`/`index` plumbing — all excluded with a truthful `reason`,
never silently mis-called.

`report.py` emits `output/meos-coverage.json`: an **actionable worklist**.
Every non-exposable public function carries a `class` and a concrete
`suggest` — the precise upstream regularization that closes it — and
`byClass` ranks classes by leverage. Live (`master`, gap 198):

| class | n | upstream action |
|---|---|---|
| `out-param-naming` | 76 | rename the lone out-parameter to `result` (one-liner each) |
| `plumbing` | 37 | none — `lifecycle`/`index`, intentionally not exposed |
| `stateful` | 30 | none — aggregate state; needs a stateful endpoint, not a stateless RPC |
| `array-return-shape` | 21 | add a trailing `int *count` (+ element encoder) |
| `multi-out` | 13 | return a struct, or split into single-result accessors |
| `other` / `no-codec` / `array-shape` / `internal-generic` | 11+7+3+1 | add a 1-arg `T_in`/`T_out`; keep `Datum`-generic internal |

**Honest ceiling.** ~91% is the *safe principled* maximum for this layer.
The `out-param-naming` 76 are **not** closed on this side on purpose:
a name-agnostic "trailing pointer ⇒ out-parameter" rule would misread
genuine pointer *inputs* as out-parameters — silently wrong answers.
Correctness-over-coverage makes these a cheap **upstream** rename, now
enumerated. Everything remaining is therefore upstream (precisely
specified above) or definitional (`stateful`/`plumbing` — correct,
labelled exclusions, never silent). The catalog regenerates from headers,
so upstream fixes flow in automatically on the next run (no coupling).

### Compensation register (retirement path)

Each heuristic exists only to absorb a current irregularity and is
**deleted** once the irregularity is uniformized upstream:

| compensation | absorbs | retire when upstream… |
|---|---|---|
| `header_types.reconcile` / `_preserved_opaque` | stub `#define`s erasing `Interval`/`text`/`Datum` to `int` | the public headers no longer route opaque types through `int` stubs |
| `_aux_specs` (default `maxdd`/mfjson flags) | in/out wrappers carrying formatting args | I/O wrappers are pure `(str)->T`/`(T)->str` |
| typed-decode (`tbool_in` for polymorphic `Temporal`) | generic `temporal_in` needing a `meosType` tag | a tag-free polymorphic decoder exists |
| out-param / array-builder / array-return shapes | irregular out/array signatures | signatures follow the canonical shapes in the worklist |

`tests/test_coverage_gate.py` asserts coverage does not regress.

> **Type reconciliation.** The PostgreSQL stub headers `#define`
> `Interval`/`text`/`TimestampTz`/… to `int` *before* libclang parses, so
> those opaque pointers reached the catalog as `int *` —
> indistinguishable from a real `int *` out-parameter. `parser/
> header_types.py` re-scans the header *source* and restores the true
> named type wherever libclang produced a bare scalar but the source
> declares a distinct named pointer (scalar typedefs like `TimestampTz`
> are deliberately left resolved). This is primarily a *correctness* fix —
> `add_timestamptz_interval`'s `interv` is now honestly `Interval`, not a
> phantom `int *`. `text *` is then treated as a JSON string (it *is* one),
> and the opaque-codec gate spans *any* named non-scalar pointer type (not
> just parsed structs), so reconciled types register their own in/out
> (`Interval` ↔ `pg_interval_in`/`interval_out`). Together this lifts
> verified public coverage to 1963/2161 (91%).
