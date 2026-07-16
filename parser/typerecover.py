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
    "uint64": "uint64_t",
    "Timestamp": "Timestamp",
    "TimestampTz": "TimestampTz",
    "H3Index": "uint64_t",
    "Quadbin": "uint64_t",
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
