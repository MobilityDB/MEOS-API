"""Recover scalar/pointer C types that parsing collapsed to ``int``.

Two distinct mechanisms erase a PG-vendored type name before the AST is built,
leaving the IDL spelling as ``int`` / ``int *`` / ``int **``:

* The host-symbol-collision build prefix-renames PG types, so ``bool`` /
  ``int64`` / ``Timestamp`` / ``TimestampTz`` / ``H3Index`` reach libclang
  already macro-collapsed.
* ``text`` (a PG ``varlena``) is undeclared to libclang — there is no
  ``pg_config.h`` / ``c.h`` in the parse — so C's implicit-int rule turns
  ``text`` / ``text *`` / ``text **`` into ``int`` / ``int *`` / ``int **``.

Either way the real type name survives in the raw header declaration TEXT, so
this post-parse pass recovers it and rewrites the IDL entry, **preserving the
declaration's ``const`` qualifier and pointer depth**.  It is idempotent and a
no-op on correctly-parsed headers: a slot is only rewritten when its current
IDL spelling is ``int`` with the *same* const/pointer shape the header would
collapse to, and the header declaration spells a recoverable base type.
Genuinely-int functions (e.g. ``intspan_width`` returning ``int``, or
``tint_values`` returning ``int *``) are left untouched because ``int`` is not
a recoverable base name.

Recovered spellings drive the downstream binding generators (JMEOS maps
``int64_t`` / ``uint64_t`` -> ``long`` and ``bool`` -> ``boolean``; MEOS.js
maps ``text *`` to a JS string via cstring2text / text2cstring; ...).
"""
import re
import glob
from pathlib import Path

# Recoverable header base type -> base spelling written into the IDL.
_TYPE_MAP = {
    "bool": "bool",
    "int64": "int64_t",
    "uint32": "uint32_t",
    "uint64": "uint64_t",
    # The fixed-width typedef spelled as itself: libclang resolves a uint32_t slot's
    # `canonical` to the platform builtin "unsigned int" (while `cType` keeps the
    # typedef). Mapping the typedef to itself lets normalize_canonical re-spell
    # `canonical` back to `uint32_t`, so the width is spelled identically catalog-wide
    # (every *_hash returns uint32_t, not one "unsigned int") — bindings key on canonical.
    "uint32_t": "uint32_t",
    "Timestamp": "Timestamp",
    "TimestampTz": "TimestampTz",
    "H3Index": "uint64_t",
    "Quadbin": "uint64_t",
    "S2CellId": "uint64_t",
    "text": "text",
    "GSERIALIZED": "GSERIALIZED",
    "Interval": "Interval",
    "DateADT": "DateADT",
    "Datum": "Datum",
    "size_t": "size_t",
    "GBOX": "GBOX",
    "BOX3D": "BOX3D",
    "AFFINE": "AFFINE",
    "Jsonb": "Jsonb",
    "JsonPath": "JsonPath",
}

# libclang renders a fixed-width integer typedef's fully-resolved `canonical` as the
# platform builtin spelling (uint64_t -> "unsigned long" on LP64). When the c-field is the
# typedef but `canonical` is that platform alias, normalize `canonical` too, so the same
# underlying type is spelled identically catalog-wide -- e.g. the Tcell<T> cell-id accessors
# th3index_start_value (H3Index, from libh3) and tquadbin_start_value (Quadbin) BOTH read
# uint64_t, not one "unsigned long" and the other "uint64_t".
_CANON_ALIAS = {
    "uint64_t": {"unsignedlong", "longunsignedint", "unsignedlonglong"},
    "int64_t": {"long", "longint", "longlong"},
}

_NAMES = "|".join(sorted(_TYPE_MAP, key=len, reverse=True))
# optional const, a recoverable base, optional pointer stars, optional identifier
_DECL_RE = re.compile(
    rf"^(?:(?P<const>const)\s+)?(?P<base>{_NAMES})\s*(?P<stars>\**)\s*\w*$"
)


def _nospace(t):
    return re.sub(r"\s+", "", t or "")


def _recovery(fragment):
    """Return ``(collapsed_idl_type, recovered_idl_type)`` for a declaration
    fragment, or ``None`` when its base type is not recoverable.

        'const text *txt' -> ('const int *', 'const text *')
        'int64'           -> ('int',         'int64_t')
        'TimestampTz *'   -> ('int *',       'TimestampTz *')
        'text **values'   -> ('int **',      'text **')
        'int *count'      -> None        (genuine int)
    """
    m = _DECL_RE.match(fragment.strip())
    if not m:
        return None
    const = "const " if m.group("const") else ""
    stars = m.group("stars") or ""
    suffix = (" " + stars) if stars else ""
    collapsed = f"{const}int{suffix}"
    recovered = f"{const}{_TYPE_MAP[m.group('base')]}{suffix}"
    original = f"{const}{m.group('base')}{suffix}"
    return collapsed, recovered, original


def _parse_header_decls(headers_dir):
    """name -> (ret_recovery, [param_recovery, ...]) from the header text,
    where each recovery is a ``(collapsed, recovered)`` pair or ``None``."""
    decls = {}
    pattern = str(Path(headers_dir) / "**" / "*.h")
    for path in glob.glob(pattern, recursive=True):
        txt = re.sub(r"//.*", "", open(path, errors="ignore").read())
        for m in re.finditer(r"extern\s+(.+?);", txt, re.S):
            d = re.sub(r"\s+", " ", m.group(1)).strip()
            fm = re.match(r"(?P<ret>.+?)\b(?P<name>\w+)\s*\((?P<params>.*)\)$", d)
            if not fm:
                continue
            # split params on top-level commas
            params, depth, cur = [], 0, ""
            for ch in fm.group("params"):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                if ch == "," and depth == 0:
                    params.append(cur)
                    cur = ""
                else:
                    cur += ch
            if cur.strip():
                params.append(cur)
            decls[fm.group("name")] = (
                _recovery(fm.group("ret")),
                [_recovery(p) for p in params if p.strip()],
            )
    return decls


def recover_collapsed_types(idl, headers_dir):
    """Rewrite IDL function types that collapsed to int, from header text.

    Returns ``(idl, stats)`` where stats counts the rewrites performed.
    """
    decls = _parse_header_decls(headers_dir)
    fixed = {"returns": 0, "params": 0}

    def _apply(slot, recovery):
        """Rewrite a return/param slot in place; return 1 if rewritten."""
        if not (recovery and isinstance(slot, dict)):
            return 0
        collapsed, recovered, original = recovery
        key = "c" if "c" in slot else "cType"
        # The base name is either erased to int by the host-collision prefix
        # rename (slot spells `collapsed`), or it survives while only the
        # canonical collapses (slot spells `original`, e.g. a MobilityDB typedef
        # such as Quadbin whose uint64 underlying type was the part that erased).
        recoverable = (_nospace(collapsed), _nospace(original))
        cur = _nospace(slot.get(key))
        # `cur in recoverable`: the base name collapsed to int, or survived as the typedef.
        # `cur == recovered`: libclang already rendered the c-field as the typedef's immediate
        # underlying type (e.g. H3Index -> uint64_t) while leaving `canonical` at the fully
        # resolved platform spelling ("unsigned long") -> fall through to normalize canonical.
        if cur not in recoverable and cur != _nospace(recovered):
            return 0
        # the name the header declares, which #normalize_canonical states as the slot's
        # `typedef` when it names a type of its own
        slot["_declared"] = _base_name(original)
        rewrote = slot.get(key) != recovered
        slot[key] = recovered
        canon = _nospace(slot.get("canonical"))
        if canon in recoverable or canon in _CANON_ALIAS.get(_nospace(recovered), ()):
            rewrote = rewrote or slot.get("canonical") != recovered
            slot["canonical"] = recovered
        return 1 if rewrote else 0

    def patch(fn):
        rec = decls.get(fn.get("name"))
        if not rec:
            return
        ret_rec, param_recs = rec
        fixed["returns"] += _apply(fn.get("returnType"), ret_rec)
        params = fn.get("params") or []
        if len(params) == len(param_recs):
            for p, pr in zip(params, param_recs):
                fixed["params"] += _apply(p, pr)

    def walk(o):
        if isinstance(o, dict):
            if "name" in o and ("returnType" in o or "params" in o):
                patch(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(idl)
    return idl, fixed


# Strip const/struct qualifiers and pointer stars to the bare base name.
_BASE_RE = re.compile(r"\b(?:const|struct|volatile)\b|\*")


def _base_name(t):
    return _BASE_RE.sub(" ", t or "").strip()


# The fixed-width and size types the C standard names (<stdint.h>, <stddef.h>), the
# spellings #_c_base of parser/sqlfn.py reads as MEOS's own: the same width on every
# platform, so a chain of typedefs reaching one is stated by it.
_C_STANDARD = re.compile(r"^(?:u?int(?:8|16|32|64)_t|u?intptr_t|size_t|ptrdiff_t)$")
# C's own scalar type names, which a typedef chain ends at when it meets no name above.
_C_SCALARS = {
    "char", "signed char", "unsigned char", "short", "signed short", "unsigned short",
    "short int", "signed short int", "unsigned short int", "int", "signed", "signed int",
    "unsigned", "unsigned int", "long", "signed long", "unsigned long", "long int",
    "signed long int", "unsigned long int", "long long", "signed long long",
    "unsigned long long", "long long int", "signed long long int",
    "unsigned long long int", "float", "double", "long double", "_Bool", "bool",
}
# A typedef of one scalar or of another name: no struct, union, enum, pointer or array.
_TYPEDEF_DECL = re.compile(
    r"^\s*typedef\s+(?!(?:struct|union|enum)\b)([A-Za-z_][\w ]*?)\s+([A-Za-z_]\w*)\s*;",
    re.M)


def postgres_scalar_names(pgtypes_root):
    """The scalar typedefs of MobilityDB's vendored PostgreSQL, ``pgtypes/``, the
    directory #_public_pgtypes_headers of run.py reads (``TimestampTz``, ``DateADT``,
    ``TimeADT``, ``Oid``, ``Datum``, ``int32``, ``float8``, ...): the names a ``typedef``
    of one scalar or of another name declares there, whichever header declares it."""
    root = Path(pgtypes_root)
    if not root.is_dir():
        return frozenset()
    names = set()
    for path in root.glob("**/*.h"):
        text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", path.read_text(errors="ignore"),
                      flags=re.S)
        names |= {name for _, name in _TYPEDEF_DECL.findall(text)}
    return frozenset(names)


# The floating types of C, which have no fixed-width name of their own.
_C_FLOATS = {"float", "double", "long double"}


def scalar_spelling(name, typedefs, pg_names):
    """How the catalog states the scalar type ``name``, read from its typedef chain, as
    #_recovery reads a collapsed name from its declaration.

    A C standard type (``int64_t``, ``size_t``) is stated by itself. A name defined as
    its own ``<stdint.h>`` name, as PostgreSQL 18's ``c.h`` defines the "historical
    names for types in <stdint.h>" (``typedef int32_t int32``), and a name for a C
    floating type (``typedef double float8``) are stated by the type they name. Any
    other typedef PostgreSQL declares (#postgres_scalar_names) is a type of its own
    (``TimestampTz``, ``DateADT``, ``Oid``, ``Datum``) and keeps its name. A chain
    meeting none of these ends at the C scalar it names. None for a name that is no
    typedef of a scalar."""
    seen, cur = set(), name
    while cur not in seen:
        seen.add(cur)
        if _C_STANDARD.match(cur):
            return cur
        nxt = typedefs.get(cur)
        if nxt is None:
            return cur if cur in _C_SCALARS and cur != name else None
        nxt = " ".join(re.sub(r"\b(?:const|volatile)\b", " ", nxt).split())
        if nxt == cur + "_t" or nxt in _C_FLOATS:
            cur = nxt
            continue
        if cur in pg_names:
            return cur
        cur = nxt
    return None


def normalize_canonical(idl, pg_names=frozenset()):
    """Re-derive each type slot's ``canonical`` from its ``cType`` typedef.

    A scalar typedef is stated by its own typedef chain (#scalar_spelling), as the
    unit the catalog parsed declares it (``_typedefs``, recorded by the parser): a type
    PostgreSQL declares keeps its name (``TimestampTz``, ``TimeADT``), and any other
    reaches the C standard type of its width (``int32`` -> ``int32_t``, ``H3Index`` ->
    ``uint64_t``), never the platform spelling libclang resolves it to (``TimeADT`` ->
    ``long``, 32 bits on Windows). Any other typedef keeps the spelling ``_TYPE_MAP``
    gives it (``Jsonb``, ``GSERIALIZED``), not libclang's (``struct varlena *``).

    A binding generator keys on ``canonical`` and must see the type MEOS declares,
    never a platform width. Idempotent; a no-op on non-typedef slots (``Temporal *``,
    ``int *``). Complements ``recover_collapsed_types``: that recovers a ``cType`` the
    preprocessor erased to ``int``; this trusts a faithful ``cType`` and only
    re-spells ``canonical``.

    A slot whose header names a type of its own over a width, a name neither PostgreSQL
    nor the C standard defines whose chain (#scalar_spelling) reaches a C standard
    integer (``H3Index``, ``Quadbin``, ``S2CellId`` over ``uint64_t``), keeps that name
    as ``typedef``: ``canonical`` states the width, ``typedef`` what the value is, which
    only its own reader produces.
    """
    fixed = 0
    typedefs = idl.pop("_typedefs", None) or {}

    def identity(name):
        """``name`` when it is a type of its own over a C standard integer, read through
        #scalar_spelling."""
        if (not name or name not in typedefs or name in pg_names
                or _C_STANDARD.match(name)):
            return None
        reached = scalar_spelling(name, typedefs, pg_names)
        return name if reached and _C_STANDARD.match(reached) else None

    def want(ctype):
        base = _base_name(ctype)
        mapped = (scalar_spelling(base, typedefs, pg_names) if base in typedefs
                  else None) or _TYPE_MAP.get(base)
        if not mapped:
            return None
        const = "const " if re.search(r"\bconst\b", ctype) else ""
        stars = "".join(c for c in ctype if c == "*")
        return f"{const}{mapped}{(' ' + stars) if stars else ''}"

    def fix(slot):
        nonlocal fixed
        if not (isinstance(slot, dict) and "canonical" in slot):
            return
        ctype = slot.get("cType") or slot.get("c")
        own = identity(slot.pop("_declared", None) or _base_name(ctype))
        if own:
            slot["typedef"] = own
        w = want(ctype) if ctype else None
        if w and _nospace(slot["canonical"]) != _nospace(w):
            slot["canonical"] = w
            fixed += 1

    def walk(o):
        if isinstance(o, dict):
            fix(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(idl)
    return idl, fixed
