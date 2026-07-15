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


def find_unlisted_foreign_structs(idl) -> list:
    # A MEOS type is typedef'd, so its declared ``cType`` appears bare (``Pose
    # *``) in at least one signature; a foreign, forward-declared ABI struct is
    # never typedef'd, so it only ever appears elaborated (``struct Foo *``).
    # Any base name seen only in the elaborated form is an external type the
    # bindings handle divergently (permissive ones map it to a raw pointer,
    # conservative ones skip it).  Surface it so it is classified explicitly
    # instead of silently diverging per binding.
    elaborated, bare = set(), set()
    for fn in idl.get("functions", []):
        spellings = [p.get("cType") for p in fn.get("params", [])]
        spellings.append(fn.get("returnType", {}).get("c"))
        for sp in spellings:
            if not isinstance(sp, str) or "*" not in sp:
                continue
            base = re.sub(r"\b(const|struct)\b|\*", " ", sp).strip()
            if not base:
                continue
            (elaborated if re.search(r"\bstruct\b", sp) else bare).add(base)
    return sorted(elaborated - bare)


def _bool_norm(spelling: str) -> str:
    # Normalise clang's ``_Bool`` keyword to the ``bool`` spelling the catalog
    # uses, token-wise so pointer/const forms (``_Bool *``, ``const _Bool *``)
    # normalise too — clang spells the pointee's underlying keyword, not the
    # ``bool`` macro.
    return re.sub(r"\b_Bool\b", "bool", spelling)


def _c_spelling(ty) -> str:
    # Return the declared C spelling, with ``_Bool`` normalised to ``"bool"``.
    # Two bool representations arise depending on which postgres_int_defs.h is
    # in play:
    # - PostgreSQL headers: ``typedef char bool``  -> spelling already ``"bool"``
    # - Stub header:        ``#define bool _Bool`` -> spelling is ``"_Bool"``
    return _bool_norm(ty.spelling)


# Canonical spellings of plain C scalars/builtins.
_SCALAR_CANON = {
    "void", "_Bool", "bool", "char", "signed char", "unsigned char",
    "short", "unsigned short", "int", "unsigned int", "long",
    "unsigned long", "long long", "unsigned long long",
    "float", "double", "long double",
}
# Named opaque types that the PostgreSQL *stub* headers collapse to a bare
# scalar even without a pointer (type-erased values). Kept by name so they
# read as themselves, not as the stub's underlying integer.
_EXPLICIT_OPAQUE = {"Datum"}


def _strip(s: str) -> str:
    return " ".join(
        re.sub(r"\b(const|volatile|struct|union|enum)\b", " ", s)
        .replace("*", " ").split()
    )


def _preserved_opaque(ty) -> str | None:
    """Keep the *declared* name of opaque types the PG stubs canonicalise to
    a bare scalar (``Interval *`` / ``text *`` -> ``const int *``, ``Datum``
    -> ``unsigned long``). A pointer whose typedef'd pointee resolves to a
    plain scalar is, in practice, always a stubbed opaque struct — so the
    declared spelling is the truthful one. Genuine scalar pointers
    (``int *result``) are unaffected: their pointee is a builtin, not a
    distinct typedef name.
    """
    if ty.kind == clang.cindex.TypeKind.POINTER:
        pointee = ty.get_pointee()
        dname = _strip(pointee.spelling)
        cname = _strip(pointee.get_canonical().spelling)
        if (dname and dname not in _SCALAR_CANON and "(" not in dname
                and cname in _SCALAR_CANON and dname != cname):
            return ty.spelling.replace("_Bool", "bool")
        return None
    if _strip(ty.spelling) in _EXPLICIT_OPAQUE:
        return _strip(ty.spelling)
    return None


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
    preserved = _preserved_opaque(ty)
    if preserved is not None:
        return _bool_norm(preserved)
    return _bool_norm(_canonical_spelling(ty))


# -----------------------------------------------------------------------------
# Family classification
#
# MobilityDB groups its optional type families in dedicated subdirectories of
# ``meos/include`` (``cbuffer/``, ``npoint/``, ``pose/``, ``rgeo/``, ``h3/``,
# ``quadbin/``, ``pointcloud/``, ``json/``, ``raster/``), each fronted by a
# top-level public ``meos_<family>.h`` header. The tree layout is therefore the
# single source of truth for family membership: a binding gates a family in or
# out purely by this field, so edge builds can drop unused families (e.g.
# ``POINTCLOUD``) to shrink their footprint. Everything else — the temporal
# core, the base ``geo``/tpoint types the families build on, and the shared
# top-level headers — is ``CORE`` and always emitted.
# -----------------------------------------------------------------------------
_SUBDIR_FAMILY = {
    "cbuffer": "CBUFFER",
    "npoint": "NPOINT",
    "pose": "POSE",
    "rgeo": "RGEO",
    "h3": "H3",
    "quadbin": "QUADBIN",
    "pointcloud": "POINTCLOUD",
    "json": "JSON",
    "raster": "RASTER",
}

_TOPLEVEL_FAMILY = {
    "meos_cbuffer.h": "CBUFFER",
    "meos_npoint.h": "NPOINT",
    "meos_pose.h": "POSE",
    "meos_rgeo.h": "RGEO",
    "meos_h3.h": "H3",
    "meos_quadbin.h": "QUADBIN",
    "meos_pointcloud.h": "POINTCLOUD",
    "meos_json.h": "JSON",
    "meos_raster.h": "RASTER",
}


def _family_of(loc_path: str) -> str:
    """Classify the declaring header into its optional family, or ``CORE``.

    The family is taken from the header's parent directory (the canonical
    grouping); the top-level ``meos_<family>.h`` public headers are mapped by
    name. Anything unmatched (temporal core, base geo, shared headers) is
    ``CORE`` and always emitted.
    """
    path = Path(loc_path)
    fam = _SUBDIR_FAMILY.get(path.parent.name)
    if fam is not None:
        return fam
    return _TOPLEVEL_FAMILY.get(path.name, "CORE")


def extract_function(node) -> dict:
    return {
        "name": node.spelling,
        "file": Path(node.location.file.name).name,
        "family": _family_of(node.location.file.name),
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
        "family": _family_of(node.location.file.name),
        "fields": [
            {
                "name": f.spelling,
                "cType": _c_spelling(f.type),
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
        "family": _family_of(node.location.file.name),
        "values": [
            {
                "name": v.spelling,
                "value": v.enum_value,
            }
            for v in node.get_children()
            if v.kind == clang.cindex.CursorKind.ENUM_CONSTANT_DECL
        ],
    }


def extract_macro(node) -> dict | None:
    """Extract a public object-like integer ``#define`` macro.

    The MEOS public headers expose a handful of integer constants as
    ``#define`` object-like macros rather than ``enum`` values — the WKB / WKT
    output-variant flags (``WKB_NDR``, ``WKB_EXTENDED`` …), the ``MEOS_FLAG_*``
    bit masks, and similar. They are part of the public API surface (a binding's
    FFI must carry them: ``meos-rs`` uses ``WKB_EXTENDED`` / ``WKB_NDR`` /
    ``WKB_XDR`` to select a WKB variant), but ``enum`` extraction never sees them
    because they are preprocessor definitions.

    Only object-like macros whose body is a single integer literal are
    extracted (matching the constants a binding can project as a typed
    constant); function-like macros and expression / string macros are skipped.
    """
    tokens = [t.spelling for t in node.get_tokens()]
    # tokens[0] is the macro name; a function-like macro has "(" immediately after.
    if len(tokens) < 2 or tokens[1] == "(":
        return None
    body = "".join(tokens[1:])
    try:
        value = int(body, 0)
    except ValueError:
        return None
    return {
        "name": node.spelling,
        "file": Path(node.location.file.name).name,
        "family": _family_of(node.location.file.name),
        "value": value,
    }
