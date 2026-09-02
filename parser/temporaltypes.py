"""Attach the temporal-type registry: what MEOS states about each ``Temporal<T>``.

``typeRelations`` names which temporal types exist and over which base. This is
the rest of what a generator needs about each one, and every fact in it is
single-source in the MEOS C:

``meos/src/temporal/meos_catalog.c``
    ``MEOS_TYPE_NAMES`` for the public name, ``MEOS_RELTYPE_CATALOG`` for the
    base type and the bounding box, and four predicates for the classes:
    ``temporal_type`` names the universe, ``tnumber_type`` and
    ``tspatial_type`` the two dispatch classes a binding branches on, and
    ``temptype_supports_linear`` the subset MEOS interpolates linearly between
    samples rather than holding constant.

``meos/src/temporal/type_out.c``
    ``temptype_as_mfjson_sb``, the switch that writes each type's MF-JSON type
    token. That token is what an OGC Moving Features surface reads and writes,
    it differs per type (``MovingPoint``, ``MovingCircularBuffer``,
    ``MovingRigidGeometry``), and MEOS states it in exactly one place. A
    consumer that cannot read it here has to carry a copy, and a copy of a
    per-type table goes stale the moment a family is added.

A type the switch does not name carries no ``mfjson`` key: ``asMFJSON`` has no
form for it, which is a fact about the type rather than a gap in the parse.

Both files are read by anchoring on a definition and counting braces, never by
matching a pattern across a span, and string literals are stepped over so the
``{`` inside ``{\\"type\\":\\"MovingFloat\\",`` is read as text rather than as
structure.
"""
import re
from pathlib import Path

from parser.typerelations import (catalog_rows, locate_temporal_source,
                                  type_names)

#: The predicate naming every temporal type. Its body is the universe this
#: registry covers, so a type absent from it is absent from the registry.
UNIVERSE = "temporal_type"

#: The class of each temporal type, one boolean per predicate that decides it.
#: A renamed predicate raises rather than silently emitting every type as false.
CLASSES = {
    "number": "tnumber_type",
    "spatial": "tspatial_type",
    "linear": "temptype_supports_linear",
}

_ENUM_RE = re.compile(r"\bT_[A-Z0-9_]+\b")
_CASE_RE = re.compile(r"\bcase\s+(T_\w+)\s*:")
_MFJSON_RE = re.compile(r'\{\\"type\\":\\"(\w+)\\"')


def _literal_end(text: str, i: int) -> int:
    """The index just past the string or character literal starting at ``i``."""
    quote = text[i]
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        j += 1
    return len(text)


def strip_comments(text: str) -> str:
    """``text`` with comment bodies blanked and every newline kept.

    A type enumerator named in prose is not a member of a list, and a comment
    opener inside a string literal is part of the string, so literals are
    stepped over rather than scanned.
    """
    out = []
    i = 0
    while i < len(text):
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = len(text) if end < 0 else end + 2
            out.append(re.sub(r"[^\n]", " ", text[i:end]))
            i = end
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            out.append(re.sub(r"[^\n]", " ", text[i:end]))
            i = end
        elif text[i] in "\"'":
            end = _literal_end(text, i)
            out.append(text[i:end])
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def function_body(text: str, name: str) -> str:
    """The braced body of the named function definition.

    The definition is the occurrence of the name in the first column: MEOS
    writes a definition's return type on the line above, so no call site can
    match. The extent is taken by counting braces from the body's own opening
    one, since a regex carries no stack and cannot tell which ``}`` closes it.
    """
    anchor = re.search(r"(?m)^" + re.escape(name) + r"\s*\(", text)
    if anchor is None:
        raise ValueError(f"no definition of {name}()")
    start = text.find("{", anchor.end())
    if start < 0:
        raise ValueError(f"{name}() has no body")
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c in "\"'":
            i = _literal_end(text, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    raise ValueError(f"{name}() has an unterminated body")


def predicate_types(text: str, name: str) -> set:
    """The type enumerators the named boolean predicate answers true for."""
    found = set(_ENUM_RE.findall(function_body(text, name)))
    if not found:
        raise ValueError(f"{name}() names no type enumerator")
    return found


def mfjson_tokens(type_out: str) -> dict:
    """Each type's MF-JSON token, from ``temptype_as_mfjson_sb``.

    The case labels come in groups, since several types share one token: a
    geometry point and a geography point are both a ``MovingPoint``. Labels
    accumulate until a token is written and are assigned together.
    """
    tokens = {}
    pending = []
    for line in function_body(type_out, "temptype_as_mfjson_sb").splitlines():
        case = _CASE_RE.search(line)
        if case:
            pending.append(case.group(1))
            continue
        token = _MFJSON_RE.search(line)
        if token:
            for enum in pending:
                tokens[enum] = token.group(1)
            pending = []
    if not tokens:
        raise ValueError("temptype_as_mfjson_sb() names no type token")
    return tokens


def attach_temporal_types(idl: dict, src_root: Path | None) -> dict:
    """Attach ``idl["temporalTypes"]``, one record per ``Temporal<T>``.

    Degrades to no attachment — never a fabricated registry — when either
    source is unavailable, mirroring the honest-signal contract of the
    relation registry. Located sources that do not parse raise instead, since
    a consumer reading an empty or partial registry silently emits a surface
    missing a type.
    """
    catalog_path = locate_temporal_source(src_root, "meos_catalog.c")
    type_out_path = locate_temporal_source(src_root, "type_out.c")
    if catalog_path is None or type_out_path is None:
        return idl

    catalog = strip_comments(catalog_path.read_text(errors="ignore"))
    type_out = strip_comments(type_out_path.read_text(errors="ignore"))

    names = type_names(catalog)
    rows = dict(catalog_rows(catalog, "MEOS_RELTYPE_CATALOG"))
    universe = predicate_types(catalog, UNIVERSE)
    classes = {role: predicate_types(catalog, fn) for role, fn in CLASSES.items()}
    tokens = mfjson_tokens(type_out)

    registry = {}
    for enum in universe:
        name = names.get(enum)
        if name is None:
            raise ValueError(f"{enum} is a temporal type MEOS_TYPE_NAMES does not name")
        fields = rows.get(enum, {})
        base = fields.get("temptype_basetype")
        if base is None:
            raise ValueError(f"{enum} is a temporal type with no temptype_basetype")
        base_name = names.get(base)
        if base_name is None:
            raise ValueError(f"{enum} has base type {base}, which MEOS_TYPE_NAMES does not name")
        record = {"base": base_name}
        bbox = names.get(fields.get("type_bboxtype"))
        if bbox is not None:
            record["bbox"] = bbox
        token = tokens.get(enum)
        if token is not None:
            record["mfjson"] = token
        for role in CLASSES:
            record[role] = enum in classes[role]
        registry[name] = record

    idl["temporalTypes"] = dict(sorted(registry.items()))
    return idl
