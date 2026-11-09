"""Mark the signatures of the four functions a type's ``CREATE TYPE`` binds.

PostgreSQL reads and writes a type through the four functions its ``CREATE TYPE`` names, and
calls them implicitly: ``INPUT`` and ``OUTPUT`` for its text, ``RECEIVE`` and ``SEND`` for its
binary form. Their names are canonical, ``<stem>_in``, ``<stem>_out``, ``<stem>_recv`` and
``<stem>_send``, and so are their signatures, those PostgreSQL requires of each:

    <stem>_in(cstring [, oid, integer])    RETURNS <type>
    <stem>_out(<type>)                     RETURNS cstring
    <stem>_recv(internal [, oid, integer]) RETURNS <type>
    <stem>_send(<type>)                    RETURNS bytea

where the stem is the type itself (``intset_in``) or a class the type belongs to, one of the
prefixes the object model's lattice states for a class whose temporal types hold it
(``temporal_out(tint)``, ``temporal_send(tint)``). Each such entry of a function's
``sqlSignatures`` gains

    typeSupport: "input" | "output" | "receive" | "send"

The dialect never names these functions (rule 3 of the input and output notes of MobilityDB,
``doc/contributing/io/00-INDEX.md``): every engine and binding reaches each through its
portable reader or writer, ``<type>FromText`` and ``asText``, ``<type>FromBinary`` and
``asBinary``, which the same function backs under another signature. A binding reads the mark
and registers no function for a marked signature, and keeps no rule of its own for telling one.

The pass is built as #attach_enum_text of parser/enumtext.py is, a function's canonical name
and signature read against the catalog, and states for all four roles the name convention by
which #_variant of parser/codecs.py finds a type's ``send``.
"""
from __future__ import annotations

import re

# The role of each suffix, by the CREATE TYPE parameter that binds it.
ROLES = {"in": "input", "out": "output", "recv": "receive", "send": "send"}

_NAME = re.compile(r"^(\w+)_(in|out|recv|send)$")


def _class_stems(idl: dict) -> dict:
    """The class prefixes each temporal SQL type admits as a stem: ``{tint: {temporal,
    tnumber, ...}}``, from the object model's lattice (``T_TINT`` is ``tint``)."""
    lattice = (idl.get("objectModel") or {}).get("lattice") or {}
    stems: dict = {}
    for node in lattice.values():
        for temptype in node.get("temptypes") or []:
            sql = temptype[2:].lower() if temptype.startswith("T_") else temptype.lower()
            stems.setdefault(sql, set()).update(node.get("prefixes") or [])
    return stems


def role_of(sig: dict, name: str, class_stems: dict) -> str | None:
    """The CREATE TYPE role of signature ``sig`` published as ``name``, else None."""
    m = _NAME.match(name or "")
    if not m:
        return None
    stem, suffix = m.group(1), m.group(2)
    args, ret = sig.get("args") or [], sig.get("ret")
    if suffix in ("in", "recv"):
        first = "cstring" if suffix == "in" else "internal"
        if not args or args[0] != first or args[1:] not in ([], ["oid", "integer"]):
            return None
        typ = ret
    else:
        if len(args) != 1 or ret != ("cstring" if suffix == "out" else "bytea"):
            return None
        typ = args[0]
    if not typ or (stem != typ and stem not in class_stems.get(typ, ())):
        return None
    return ROLES[suffix]


def attach_type_support(idl: dict) -> tuple[dict, dict]:
    """Mark each signature a type's CREATE TYPE binds; answers the count of each role."""
    class_stems = _class_stems(idl)
    counts = {role: 0 for role in ROLES.values()}
    for f in idl.get("functions", []):
        for sig in f.get("sqlSignatures") or []:
            sig.pop("typeSupport", None)
            role = role_of(sig, sig.get("sqlName") or f.get("sqlfn"), class_stems)
            if role:
                sig["typeSupport"] = role
                counts[role] += 1
    return idl, counts
