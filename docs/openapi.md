# OpenAPI projection

`generator/openapi.py` turns the **enriched** catalog (`meos-idl.json` with
`category` / `network` / `wire` / `typeEncodings` — see
[`enrichment.md`](enrichment.md)) into an **OpenAPI 3.1** document. It is the
concrete realisation of "OpenAPI is a projection of MEOS-API": the canonical
semantic catalog is the single source, OpenAPI is one rendering of it.

```bash
python run.py                 # enriched catalog -> output/meos-idl.json
python generate_openapi.py    #             -> output/meos-openapi.json
```

Pure `dict` → `dict`: no libclang, no MEOS runtime, deterministic output
(sorted paths/schemas) so generated diffs are reviewable.

## Projection rules

| MEOS concept | OpenAPI |
|---|---|
| stateless-exposable function | one `POST /{function}` operation, `operationId = function` |
| `category` | operation `tags` + `x-meos-category`; spec-level `tags` list |
| parameter | property of the JSON request body (all required, `additionalProperties:false`) |
| `wire.kind = json` scalar | `{"type": integer\|number\|boolean\|string}` |
| `wire` enum | `$ref` to a component enum schema (string, real C constant names) |
| `wire.kind = serialized` | `allOf` → `$ref` to the type's component schema, plus `x-meos-decode` (request) / `x-meos-encode` (response) |
| `wire.kind = array` (builder `(Elem **,count)`) | `{"type":"array","items":<element schema>}` + `x-meos-decode`; the C `count` is the array length |
| out-parameter result (`from_outparam`) | the out-param value is the response (scalar JSON or serialized); `presence_return` false ⇒ `204` |
| `wire.result.kind = void` | `204 No Content` |
| any error | `default` → `#/components/responses/MeosError` |
| `typeEncodings[T]` | `components.schemas.T` = `{"type":"string", x-meos-encodings, x-meos-in, x-meos-out}` |

RPC-style, not resource-style, is deliberate: MEOS is a value algebra, so a
function ≈ an **OGC API – Processes** "process". A resource model
(OGC API – Moving Features collections) is a different projection, layered
later (and already partly served by
[MobilityAPI](https://github.com/MobilityDB/MobilityAPI)).

## `x-meos-*` extensions

The spec is self-describing for downstream generators (server, MCP, gRPC):

- `info.x-meos-coverage` — `{functions, exposed}`.
- operation `x-meos-category`, `x-meos-encode`.
- serialized request property `x-meos-decode` — the MEOS parse function.
- component schema `x-meos-encodings` / `x-meos-in` / `x-meos-out` — the wire
  encodings and the MEOS in/out function names.

A server generator marshals a request by calling `x-meos-decode` on each
serialized string, invoking the function, and calling `x-meos-encode` on the
result — no extra metadata needed beyond this document.

## Coverage (live MobilityDB `master`)

2161 **public** functions → **1952 operations (85%)** — the internal
`meos_internal*.h` programmer API (511 fns, `Datum`-generic) is
policy-excluded. Tagged across `predicate`, `transformation`, `accessor`,
`io`, `conversion`, `setop`, `aggregate`, `constructor`. The remaining
public functions (multi-out/array builders, opaque-no-codec, polymorphic
input-decode, lifecycle/index) carry a truthful `reason` and are
overridable via `meta/meos-meta.json`.

## Limitations / roadmap

- **No OpenAPI conformance validation** in-tree yet (structural checks only:
  every path a single `POST` with responses, all `$ref`s resolve). Adding
  `openapi-spec-validator` to CI is a follow-up.
- **MCP tool manifest** — the same `wire`/`typeEncodings` model maps directly
  onto MCP tool schemas; a sibling generator is the natural next unit.
- **Runtime server** — a generated marshaling server (decode → call → encode)
  is out of scope here; this PR delivers the *contract*, not the server.
- **OGC API – Moving Features** resource projection is a separate effort.
- Preferred in/out per type currently follows catalog scan order (e.g.
  `tbool_in` may be picked over `temporal_in`); both are valid decoders, but
  refining the preference is a small enrichment-side follow-up.
