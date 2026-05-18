# MCP tool-manifest projection

`generator/mcp.py` turns the **enriched** catalog (`network` / `wire` /
`typeEncodings`, see [`enrichment.md`](enrichment.md)) into a **Model Context
Protocol (MCP)** tool manifest — one tool per stateless-exposable MEOS
function, so an LLM/agent can call the MEOS value algebra directly.

```bash
python run.py                 # enriched catalog -> output/meos-idl.json
python generate_mcp.py        #              -> output/meos-mcp.json
```

Pure `dict` → `dict` (no libclang, no MEOS runtime); deterministic
(tools sorted by name) so generated diffs are reviewable.

## Why a separate generator (not the OpenAPI one)

MCP `inputSchema` must be a **self-contained** JSON Schema per tool — MCP
clients do not resolve external `#/components/...` `$ref`s. So enums and
opaque-type schemas are **inlined** into each tool rather than referenced.
The projection rules are otherwise the same model as
[`openapi.md`](openapi.md); only the rendering differs.

## Projection rules

| MEOS concept | MCP tool |
|---|---|
| stateless-exposable function | one tool, `name = function` |
| `doc` (or synthesized) | `description`; serialized args add a "passed as serialized strings" hint so the model formats them correctly |
| parameter | `inputSchema.properties` entry (all `required`, `additionalProperties:false`, JSON Schema 2020-12) |
| `wire` scalar / enum | inline `{"type": …}` / `{"type":"string","enum":[real C constant names]}` |
| `wire` serialized | `{"type":"string"}` + a description naming the type and its encodings (text/MF-JSON/HexWKB) |
| `wire` array (builder `(Elem **,count)`) | `{"type":"array","items":<element schema>}`; the C `count` is the array length |
| out-parameter result (`from_outparam`) | the out-param value is the tool result (scalar or serialized); `presence_return` false ⇒ no value |
| result | `outputSchema` = `{type:object, properties:{result:…}}`; `void` ⇒ no `outputSchema` |
| purity | `annotations`: `readOnlyHint`/`idempotentHint` true, `destructiveHint`/`openWorldHint` false |
| dispatch metadata | `x-meos.category`, `x-meos.decode` (param → MEOS parse fn), `x-meos.encode` (result serialize fn) |

A runtime serves a call by JSON-decoding the arguments, running each
`x-meos.decode` on the serialized strings, invoking the function, and
`x-meos.encode` on the result — nothing beyond this manifest is needed.

## Coverage (live MobilityDB `master`)

2161 **public** functions → **1952 tools (85%)**; the internal
`meos_internal*.h` programmer API (511 fns, `Datum`-generic) is
policy-excluded. Spans `predicate`, `transformation`, `accessor`, `io`,
`setop`, `conversion`, `constructor`, `aggregate`. The remaining public
functions carry a truthful `reason` and are overridable via
`meta/meos-meta.json`.

## Limitations / roadmap

- `x-meos` is a namespaced extension to the MCP tool object (clients ignore
  unknown keys); the `tools` array itself is spec-pure.
- No MCP **server** here — this PR delivers the manifest/contract; a
  generated stdio/HTTP MCP server (decode → call → encode) is the next unit.
- Encoding uses the generic root (`temporal_out`, correct for every
  subtype); decoding a polymorphic argument uses a *typed* wrapper
  (`tbool_in`) because the generic `temporal_in` needs a semantic type tag.
  A mismatched subtype yields a clean error, never a wrong result; carrying
  the subtype on the wire for universal decode is the remaining gap.
- Tool count (1829) exceeds what some clients comfortably list; a curated
  subset / namespacing by `category` is a sensible later refinement.
