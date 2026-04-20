import json
from pathlib import Path


def load_type_mappings(mappings_path: Path) -> dict:
    with open(mappings_path) as f:
        return json.load(f)


def resolve_type(c_type: str, mappings: dict, target: str) -> str:
    # Normalize: strip spaces and common qualifiers
    normalized = c_type.replace("const ", "").replace(" *", "*").strip()

    types = mappings.get("types", {})

    # Try exact match first
    if normalized in types:
        return types[normalized].get(target, c_type)

    # Fall back to generic pointer mapping
    if "*" in normalized:
        return types.get("void*", {}).get(target, c_type)

    return c_type


def resolve_idl_types(idl: dict, mappings_path: Path) -> dict:
    if not mappings_path.exists():
        return idl

    mappings = load_type_mappings(mappings_path)
    targets = ["js", "python", "java"]

    for fn in idl["functions"]:
        rt = fn["returnType"]
        rt["targets"] = {
            t: resolve_type(rt["c"], mappings, t) for t in targets
        }
        for param in fn["params"]:
            param["targets"] = {
                t: resolve_type(param["cType"], mappings, t) for t in targets
            }

    return idl