import re
import clang.cindex
import json
import subprocess
import tempfile
import os
from pathlib import Path

from parser.extractors import (
    extract_function,
    extract_struct,
    extract_enum,
    extract_macro,
)
from parser.type_resolver import resolve_idl_types


def _compiler_system_includes() -> list[str]:
    """The C compiler's system header search path (builtin resource dir,
    ``/usr/local/include``, ``/usr/include/<triple>``, ``/usr/include``).

    The pip ``libclang`` wheel ships the shared library but *not* a configured
    system include path, so the amalgamation fails to find ``<stdbool.h>`` /
    ``<setjmp.h>`` / ``size_t`` and every ``bool`` / ``uint8`` / ``Datum`` field
    degrades to ``int`` with ``get_offset`` returning ``-1``. ``provision-meos``
    installs ``clang``, so ask the installed compiler for its own search list
    (``clang -E -v``) rather than guessing paths; every entry there is added as
    ``-isystem`` so a real MEOS compile and this parse see the same headers.
    """
    for cc in ("clang", "cc", "gcc"):
        try:
            out = subprocess.run(
                [cc, "-E", "-x", "c", "-v", os.devnull],
                capture_output=True, text=True, check=True).stderr
        except Exception:
            continue
        dirs, capture = [], False
        for line in out.splitlines():
            if line.startswith("#include <...> search starts here:"):
                capture = True
                continue
            if line.startswith("End of search list."):
                break
            if capture:
                d = line.strip()
                if d and os.path.isdir(d):
                    dirs.append(d)
        if dirs:
            return dirs
    return []


def _clang_extra_args() -> list[str]:
    """Include paths clang needs to resolve MEOS types to their real C types.

    Without a configured search path the MEOS headers' standard-library
    includes (``<stdbool.h>`` / ``<stddef.h>`` / ``size_t``) fail, degrading
    every struct field to ``int`` at offset ``-1`` — unusable for the FFI
    bindings (``#[repr(C)]`` structs, cgo/cffi field access). Add the
    compiler's own system search path, and the external family (H3 / GDAL /
    PROJ) headers when present. ``size_t`` is force-included via
    ``-include stddef.h`` because ``meos.h`` uses it without an explicit
    ``#include``.

    The catalog is derived from the *installed* MEOS headers (the generated
    ``meos_export.h`` written by ``make install``), which splice
    ``postgres_ext_defs.in.h`` in place of the source tree's
    ``#include <postgres.h>`` — so they are self-contained and no PostgreSQL
    server headers are needed here.
    """
    args: list[str] = []

    for d in _compiler_system_includes():
        args.append(f"-isystem{d}")
    if args:
        args += ["-include", "stddef.h"]

    for d in ("/usr/include/h3", "/usr/include/gdal", "/usr/include/proj"):
        if os.path.isdir(d):
            args.append(f"-isystem{d}")

    return args


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


# The optional families MobilityDB's ``ALL=ON`` build enables, each defining a
# ``-D<FAMILY>=1`` compile flag. Mirror that build here so declarations guarded
# by ``#if <FAMILY>`` in the core headers (e.g. ``#if POINTCLOUD`` around
# ``meos_initialize_pointcloud`` in meos.h) enter the catalog — the family
# headers themselves are unguarded, but a handful of core-header declarations
# are gated. Kept in sync with MobilityDB CMakeLists.txt's ``if(ALL) foreach``.
_ALL_FAMILIES = (
    "ARROW", "CBUFFER", "H3", "JSON", "NPOINT", "POINTCLOUD", "POSE",
    "QUADBIN", "RASTER", "RGEO",
)


def parse_meos(entry: Path, include_dir: Path) -> dict:
    index = clang.cindex.Index.create()
    tu = index.parse(str(entry), args=[
        "-x", "c",
        "-std=c11",
        f"-I{include_dir}",
        "-DMEOS",
        # Define the MEOS ``UNUSED`` attribute macro on the command line so it is
        # always in scope: the amalgamated entry point may parse a header that
        # uses ``UNUSED`` (e.g. ``Datum dist UNUSED``) before temporal.h defines
        # it, and an undefined ``UNUSED`` makes clang error on the declarator and
        # silently drop the remaining parameters of that prototype.
        "-DUNUSED=__attribute__((unused))",
        *(f"-D{family}=1" for family in _ALL_FAMILIES),
    ] + _clang_extra_args(),
        # Record ``#define`` macro definitions as cursors so the public
        # object-like integer macros (WKB / WKT variant flags, ``MEOS_FLAG_*``)
        # can be extracted into the catalog — an ``enum`` walk never sees them.
        options=clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)

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

    functions, structs, enums, macros = [], [], [], []

    for node in tu.cursor.walk_preorder():
        loc = node.location.file
        if not loc or str(Path(loc.name).resolve()) not in own_files:
            continue  # skip stdlib, system headers, etc.

        if node.kind == clang.cindex.CursorKind.MACRO_DEFINITION:
            macro = extract_macro(node)
            if macro:
                macros.append(macro)
            continue

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
    macros    = _dedup(macros)

    idl = {"functions": functions, "structs": structs, "enums": enums,
           "macros": macros}

    # Resolve types if the mappings file exists
    mappings_path = Path("./meta/type-mappings.json")
    return resolve_idl_types(idl, mappings_path)


def build_entry_point(headers_dir: Path) -> str:
    lines = []
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