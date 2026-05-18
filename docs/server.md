# Runtime server

`server/` is the projection that **executes**. It builds its entire routing,
request-validation and dispatch table from the *enriched* catalog
(`network` / `wire` — see [`enrichment.md`](enrichment.md)), the same single
source the OpenAPI and MCP generators consume.

```bash
python run.py                                 # enriched catalog
python serve.py                               # serve on 127.0.0.1:8080
MEOS_LIBRARY_PATH=/path/libmeos.so python serve.py 0.0.0.0 9000
```

Per `POST /{function}` it runs the universal pipeline the `wire` model
implies:

1. validate the JSON body against the parameter model;
2. `engine.decode` each serialized string → opaque handle;
3. `engine.invoke` the function with scalars + handles;
4. `engine.encode` an opaque result → string;
5. reply `{"result": …}` · `204` for void · `400 {"error","code"}` on a
   MEOS/validation error · `404` for an unknown operation.

`GET /healthz` reports engine and operation count. Stdlib `http.server`
only (no new dependencies) — a reference/embeddable server, not a tuned
production stack.

## The engine seam

All MEOS work is behind `server/engine.py`:

| Engine | Use |
|---|---|
| `CtypesEngine` | **Real.** `dlopen`s a built `libmeos` and calls `x-meos.decode` / function / `x-meos.encode` by symbol. Every opaque value is an anonymous `void *` — no struct layout is ever needed, because the catalog already reduced every exposable function to *scalars + decode/encode of opaque pointers*. Selected when `MEOS_LIBRARY_PATH` is set. |
| `StubEngine` | No MEOS build: routes/validation/error-mapping run; MEOS calls return deterministic placeholders. Default, and what makes the server runnable/testable without a compiled MEOS. |

## What is validated

- **Generation, routing, validation, dispatch, error mapping** — built from
  the live MobilityDB `master` catalog (**1963 operations** = 91% of the
  public API; internal `meos_internal*.h` is policy-excluded; 0 malformed),
  exercised end-to-end over real HTTP sockets with a recording engine
  (`tests/test_server.py`).
- **Real `libmeos` end-to-end** — against an installed
  `/usr/local/lib/libmeos.so` (`tests/test_engine_integration.py`,
  skipped unless `MEOS_LIBRARY_PATH` is set):

  ```
  POST /temporal_copy {"temp":"{t@2000-01-01, f@2000-01-03, t@2000-01-05}"}
    -> 200 {"result":"{t@2000-01-01 00:00:00+01, f@..., t@...}"}
       # full path: decode(tbool_in) -> invoke -> encode(temporal_out, maxdd=15)
  POST /temporal_num_instants {"temp":"{t@2000-01-01, f@2000-01-03, ...}"}
    -> 200 {"result": 3}
  POST /temporal_num_instants {"temp":"garbage"}
    -> 400 {"error":"Missing delimeter character '@': garbage","code":22}
  GET  /healthz  (after the bad request)  -> 200   # process survived

  floatset_value_n(decode "{1.0, 2.5, 3.0}", n=2, double *result)
    -> present=True, result=2.5     # scalar value via byref out-parameter
    -> n=99: present=False          # -> HTTP 204 (no value)
  geoset_value_n(decode "{Point(1 1), Point(2 2)}", n=1, GSERIALIZED **result)
    -> present=True, encode(geo_as_ewkt) -> "POINT(1 1)"  # opaque out-param
  temporal_merge_array(["t@2000-01-01","f@2000-01-03"])   # JSON list
    -> decode each -> C array -> 200 {"result":"{t@..., f@...}"}
  temporal_sequences("{[t@..., f@...], [t@...]}")          # Elem **+count
    -> byref count -> 200 {"result":["...","..."]}          # JSON array
  ```

  The whole pipeline runs on real MEOS, including the **generic
  `temporal_out`** with its `maxdd` aux defaulted — `test_engine_
  integration` round-trips a `tbool` *and* a `tfloat` through it, proving
  it serialises any subtype. A malformed input becomes a `400` (the
  installed non-fatal error handler) instead of `exit()`ing the server.
- `CtypesEngine` marshalling (including aux args) is additionally
  unit-tested against a fake library.

## Limitations / roadmap

- **Polymorphic decoding is subtype-narrow (input side only).** Coverage
  is **1963/2161 = 91% of the public API**: formatting aux args are
  defaulted (generic `temporal_out`, `*_out(.., maxdd)`), scalar *and*
  opaque out-parameter accessors are projected through their byref result,
  input-array builders take a JSON list, array returns (`Elem **`+count)
  become a JSON array, and the internal
  `meos_internal*.h` programmer API is
  policy-excluded. The remaining limitation: polymorphic types whose only
  generic decoder needs a semantic type tag (`temporal_in(str, meosType)`,
  the typed-set decoders) decode a serialized **argument** with a typed
  helper (`tbool_in`, a `bigint`-set parser). A mismatched subtype yields a
  clean MEOS `400`, never a crash or wrong answer. Carrying the subtype on
  the wire for universal decoding is the remaining future work toward full
  parity; the residual non-exposable set is otherwise genuinely
  non-stateless (array/multi-out builders, `Datum`-internal, plumbing).
- Response is wrapped `{"result": …}` (matches the MCP `outputSchema`
  envelope); an unwrapped mode matching the bare OpenAPI 200 schema is a
  trivial follow-up.
- `CtypesEngine` integer width per function uses `c_long`; functions needing
  exact `int32`/`size_t` widths may need per-function refinement (an
  enrichment-side concern).
- Stdlib server is single-process; production deployment (ASGI/WSGI,
  concurrency, auth) is intentionally out of scope — the value here is the
  *correct-by-construction* contract execution, not the transport.
- Memory ownership: results are encoded then dropped; wiring MEOS
  `pfree`/free of returned pointers into `CtypesEngine` is a follow-up.
