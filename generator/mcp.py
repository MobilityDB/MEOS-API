"""MCP tool-manifest generator.

Projects the *enriched* MEOS catalog (`network` / `wire` / `typeEncodings`
from the service-projection pass) onto a Model Context Protocol (MCP) tool
manifest: one tool per stateless-exposable function, so an LLM/agent can
call the MEOS value algebra directly.

Unlike the OpenAPI projection, every tool is **self-contained** — its
`inputSchema` inlines all definitions (no shared `$ref`s), which is what MCP
clients expect. `x-meos` carries the decode/encode function names and
category so a runtime can dispatch a call without any extra metadata.

Pure `dict` → `dict`; no libclang and no MEOS runtime. Deterministic
(tools sorted by name) so generated diffs stay reviewable.
"""

import re

_QUAL_RE = re.compile(r"\b(const|volatile|struct|union|enum)\b")
_PRIM = {"integer": "integer", "number": "number",
         "boolean": "boolean", "string": "string"}


def _clean_type(c_type: str) -> str:
    """``const struct Temporal *`` -> ``Temporal``."""
    return " ".join(_QUAL_RE.sub(" ", c_type).replace("*", " ").split())


def _enum_values(name: str, enums: list) -> list:
    for e in enums:
        if e["name"] == name:
            return [v["name"] for v in e.get("values", [])]
    return []


def _param_schema(p: dict, enums: list) -> dict:
    if p["kind"] == "json":
        if p.get("enum"):
            s = {"type": "string", "title": p["enum"]}
            vals = _enum_values(p["enum"], enums)
            if vals:
                s["enum"] = vals
            return s
        return {"type": _PRIM.get(p.get("json", "string"), "string")}
    if p["kind"] == "array":               # builder (Elem **, count)
        return {"type": "array",
                "items": _param_schema(p["element"], enums)}
    # serialized
    t = _clean_type(p["cType"])
    encs = ", ".join(p.get("encodings", [])) or "text"
    return {
        "type": "string",
        "title": t,
        "description": (
            f"A MEOS {t} value, serialized as {encs} "
            f"(e.g. WKT / MF-JSON / HexWKB)."
        ),
    }


def _describe(fn: dict) -> str:
    doc = fn.get("doc")
    text = doc.strip() if doc else (
        f"MEOS {fn['category']} operation `{fn['name']}`."
    )
    if any(p["kind"] == "serialized" for p in fn["wire"]["params"]):
        text += (" Spatiotemporal arguments are passed as serialized strings "
                 "(text/WKT, MF-JSON, or HexWKB).")
    return text


def _result_schema(result: dict, enums: list):
    if result["kind"] == "json":
        if result.get("enum"):
            s = {"type": "string"}
            vals = _enum_values(result["enum"], enums)
            if vals:
                s["enum"] = vals
            return s
        return {"type": _PRIM.get(result.get("json", "string"), "string")}
    if result["kind"] == "serialized":
        return {"type": "string", "title": _clean_type(result["cType"])}
    return None  # void


def _tool(fn: dict, enums: list) -> dict:
    wire = fn["wire"]
    props, required = {}, []
    for p in wire["params"]:
        props[p["name"]] = _param_schema(p, enums)
        required.append(p["name"])

    tool = {
        "name": fn["name"],
        "description": _describe(fn),
        "inputSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "title": fn["name"],
            "readOnlyHint": True,
            "idempotentHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
        "x-meos": {"category": fn["category"]},
    }

    rs = _result_schema(wire["result"], enums)
    if rs is not None:
        tool["outputSchema"] = {
            "type": "object",
            "properties": {"result": rs},
            "required": ["result"],
        }
    if wire["result"]["kind"] == "serialized":
        tool["x-meos"]["encode"] = wire["result"]["encode"]

    decode = {p["name"]: p["decode"] for p in wire["params"]
              if p["kind"] == "serialized"}
    if decode:
        tool["x-meos"]["decode"] = decode
    return tool


def build_mcp(catalog: dict, *, server_name: str = "meos") -> dict:
    """Build an MCP tool manifest from an enriched catalog."""
    functions = sorted(
        (f for f in catalog.get("functions", [])
         if f.get("network", {}).get("exposable")),
        key=lambda f: f["name"],
    )
    enums = catalog.get("enums", [])
    tools = [_tool(f, enums) for f in functions]
    return {
        "x-meos": {
            "server": server_name,
            "description": (
                "MEOS spatiotemporal value algebra exposed as MCP tools, "
                "generated from the MEOS-API catalog. One tool per "
                "stateless-exposable function; spatiotemporal values are "
                "passed as serialized strings. Generated, do not edit."
            ),
            "coverage": {
                "functions": len(catalog.get("functions", [])),
                "exposed": len(tools),
            },
        },
        "tools": tools,
    }
