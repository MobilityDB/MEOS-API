import re
import clang.cindex
from pathlib import Path


def _canonical_spelling(ty) -> str:
    # Return canonical type spelling, resolving anonymous typedef-struct names.
    canonical = ty.get_canonical().spelling
    if "(unnamed at" not in canonical:
        return canonical
    c = ty.spelling
    base_name = re.sub(r"\bconst\b", "", c).replace("*", "").strip()
    return re.sub(r"\(unnamed at [^)]+\)", base_name, canonical)


# Spellings that always represent a boolean type, regardless of the underlying C representation:
#   - "bool"  -> PostgreSQL style: typedef char bool  (canonical: char/CHAR_S)
#   - "_Bool" -> stub style: #define bool _Bool       (canonical: _Bool/BOOL)
_BOOL_SPELLINGS = {"bool", "_Bool"}


def _c_spelling(ty) -> str:
    # Return the declared C spelling, with ``_Bool`` normalised to ``"bool"``.
    # Two bool representations arise depending on which postgres_int_defs.h is
    # in play:
    # - PostgreSQL headers: ``typedef char bool``  -> spelling already ``"bool"``
    # - Stub header:        ``#define bool _Bool`` -> spelling is ``"_Bool"``
    spelling = ty.spelling
    if spelling == "_Bool":
        return "bool"
    return spelling


def _canonical_c_spelling(ty) -> str:
    # Like ``_canonical_spelling`` but normalises boolean types to ``"bool"``.
    # Handles:
    # - PostgreSQL ``typedef char bool``: spelling ``"bool"``, kind CHAR_S
    # - Stub ``#define bool _Bool``:      spelling ``"_Bool"``, kind BOOL
    spelling = ty.spelling
    if spelling in _BOOL_SPELLINGS:
        return "bool"
    # Fallback: also catch _Bool reached through other typedef chains
    if ty.get_canonical().kind == clang.cindex.TypeKind.BOOL:
        return "bool"
    return _canonical_spelling(ty)


def extract_function(node) -> dict:
    return {
        "name": node.spelling,
        "file": Path(node.location.file.name).name,
        "returnType": {
            "c": _c_spelling(node.result_type),
            "canonical": _canonical_c_spelling(node.result_type),
        },
        "params": [
            {
                "name": arg.spelling or f"arg{i}",
                "cType": _c_spelling(arg.type),
                "canonical": _canonical_c_spelling(arg.type),
            }
            for i, arg in enumerate(node.get_arguments())
        ],
    }


def extract_struct(node) -> dict:
    return {
        "name": node.spelling,
        "file": Path(node.location.file.name).name,
        "fields": [
            {
                "name": f.spelling,
                "cType": f.type.spelling,
                "offset_bits": node.type.get_offset(f.spelling),
            }
            for f in node.get_children()
            if f.kind == clang.cindex.CursorKind.FIELD_DECL
        ],
    }


def extract_enum(node) -> dict:
    return {
        "name": node.spelling,
        "file": Path(node.location.file.name).name,
        "values": [
            {
                "name": v.spelling,
                "value": v.enum_value,
            }
            for v in node.get_children()
            if v.kind == clang.cindex.CursorKind.ENUM_CONSTANT_DECL
        ],
    }