import re
import clang.cindex
import json
import tempfile
import os
from pathlib import Path

from parser.extractors import extract_function, extract_struct, extract_enum
from parser.type_resolver import resolve_idl_types

def merge_meta(idl: dict, meta_path: Path) -> dict:
    with open(meta_path) as f:
        meta = json.load(f)

    for fn in idl["functions"]:
        if fn["name"] in meta.get("functions", {}):
            fn.update(meta["functions"][fn["name"]])

    for struct in idl["structs"]:
        if struct["name"] in meta.get("types", {}):
            struct.update(meta["types"][struct["name"]])

    return idl


def parse_meos(entry: Path, include_dir: Path) -> dict:
    index = clang.cindex.Index.create()
    tu = index.parse(str(entry), args=[
        "-x", "c",
        "-std=c11",
        f"-I{include_dir}",
        "-DMEOS",
    ])

    # Collect all .h files belonging to the project
    own_files = {str(p.resolve()) for p in include_dir.glob("**/*.h")}

    # First pass: build a mapping "anonymous struct location -> typedef name"
    typedef_map: dict[str, str] = {}
    for node in tu.cursor.walk_preorder():
        loc = node.location.file
        if not loc or str(Path(loc.name).resolve()) not in own_files:
            continue
        if node.kind == clang.cindex.CursorKind.TYPEDEF_DECL:
            canonical = node.underlying_typedef_type.get_canonical().spelling
            m = re.search(r"\(unnamed at ([^)]+)\)", canonical)
            if m:
                typedef_map[m.group(1)] = node.spelling

    functions, structs, enums = [], [], []

    for node in tu.cursor.walk_preorder():
        loc = node.location.file
        if not loc or str(Path(loc.name).resolve()) not in own_files:
            continue  # skip stdlib, system headers, etc.

        if node.kind == clang.cindex.CursorKind.FUNCTION_DECL:
            functions.append(extract_function(node))

        elif node.kind == clang.cindex.CursorKind.STRUCT_DECL and node.spelling:
            struct = extract_struct(node)
            # Resolve anonymous struct names via typedef_map
            m = re.search(r"\(unnamed at ([^)]+)\)", struct["name"])
            if m:
                typedef_name = typedef_map.get(m.group(1))
                if typedef_name:
                    struct["name"] = typedef_name
                else:
                    continue
            structs.append(struct)

        elif node.kind == clang.cindex.CursorKind.ENUM_DECL and node.spelling:
            enums.append(extract_enum(node))


    # Deduplicate by name (keep first occurrence)
    def _dedup(items: list) -> list:
        seen: set[str] = set()
        result = []
        for item in items:
            if item["name"] not in seen:
                seen.add(item["name"])
                result.append(item)
        return result

    functions = _dedup(functions)
    structs   = _dedup(structs)
    enums     = _dedup(enums)

    idl = {"functions": functions, "structs": structs, "enums": enums}

    # Resolve types if the mappings file exists
    mappings_path = Path("./meta/type-mappings.json")
    return resolve_idl_types(idl, mappings_path)


# Minimal stand-in for system headers so libclang does not fall back to
# treating undeclared identifiers as int.  stdbool.h is the load-bearing one:
# without it every `bool`-returning function is parsed with result_type
# TypeKind.INT.
#
# The postgres integer typedefs are the same hazard: without a real
# pg_config.h, postgres/c.h never typedefs int64, so int64 (and every
# type built on it: TimestampTz, Timestamp, TimeADT, DateADT, ...)
# collapses to implicit int and timestamp parameters are emitted 32-bit.
# These mirror MobilityDB's postgres/c.h (LP64 branch), timestamp_def.h
# and date.h exactly.
_SYSTEM_HEADER_STUBS = """
#ifndef bool
#define bool _Bool
#endif
#ifndef true
#define true 1
#endif
#ifndef false
#define false 0
#endif
typedef unsigned long size_t;
typedef signed char int8;
typedef signed short int16;
typedef signed int int32;
typedef long int int64;
typedef unsigned char uint8;
typedef unsigned short uint16;
typedef unsigned int uint32;
typedef unsigned long int uint64;
typedef float float4;
typedef double float8;
typedef int64 Timestamp;
typedef int64 TimestampTz;
typedef int64 TimeADT;
typedef int64 TimeOffset;
typedef int32 DateADT;
"""


def build_entry_point(headers_dir: Path) -> str:
    lines = [_SYSTEM_HEADER_STUBS]
    for h in sorted(headers_dir.glob("**/*.h")):
        lines.append(f'#include "{h.resolve()}"')
    return "\n".join(lines)


def parse_all_headers(headers_dir: Path) -> dict:
    entry_src = build_entry_point(headers_dir)

    with tempfile.NamedTemporaryFile(suffix=".h", mode="w", delete=False) as f:
        f.write(entry_src)
        tmp_path = f.name

    try:
        return parse_meos(Path(tmp_path), headers_dir)
    finally:
        os.unlink(tmp_path)