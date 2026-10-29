"""Attach the base-to-collection type-relation registry from ``meos_catalog.c``.

A base type ``T`` is the single parameter of four independent template classes —
``Temporal<T>``, ``Set<T>``, ``Span<T>`` and ``SpanSet<T>``. The catalog array
``MEOS_RELTYPE_CATALOG`` in ``meos_catalog.c`` is indexed by ``MeosType`` and
names, at the entry of each type, the types related to it; ``MEOS_TYPE_NAMES``
maps a ``MeosType`` to its public name. Reading the relations out of the entries
and resolving through the names yields, for each base type name, the names of
its set, span, span set and temporal types.

The ``temporal`` role names every temporal type whose instants store a value of the
base, the inverse of ``temptype_basetype``, so it is a list: the instants of
``tgeompoint`` and ``tgeometry`` store a geometry, and those of ``tpose`` and
``trgeometry`` a pose. A ``trgeometry`` is not ``Temporal<pose>``: it is the
concatenation of a reference geometry and a temporal pose, each instant storing the
pose that places the geometry, so its value at an instant is a geometry while
``getValue`` reads the stored pose.

This is the static metadata a binding generator needs to pick the concrete
collection type of a value-domain result — ``SpanSet<float>`` is ``floatspanset``
— with no hand-coding: every binding is a projection of the catalog, so the
mapping belongs in the catalog rather than in each generator.
"""
import os
import re
from pathlib import Path

_NAME_RE = re.compile(r'\[\s*(T_\w+)\s*\]\s*=\s*"([^"]+)"')
_ROW_RE = re.compile(r'\[\s*(T_\w+)\s*\]\s*=\s*\{(.*?)\}', re.S)
_FIELD_RE = re.compile(r'\.\s*(\w+)\s*=\s*(T_\w+)')
# A release line older than the type-indexed catalog (stable-1.3) states each relation as
# its own table of {outer, inner} pairs headed by the relation's two names,
# `/* settype   basetype */`, which together are the field the type-indexed entry names.
_PAIR_TABLE_RE = re.compile(r'_CATALOG\s*\[\]\s*=\s*\{\s*/\*\s*(\w+)\s+(\w+)\s*\*/(.*?)\n\};',
                            re.S)
_PAIR_RE = re.compile(r'\{\s*(T_\w+)\s*,\s*(T_\w+)\s*\}')

#: The spelling of the parameter type of a membership predicate, `temporal_type(MeosType
#: type)`: `meosType` on a release line older than the rename (stable-1.3).
MEOSTYPE_PARAM = r'(?:MeosType|meosType)'

#: The relation field of ``reltype_catalog_struct`` naming each role, and the field naming
#: its inverse. The catalog records both directions — a relation and its inverse are read
#: from the entry of each of the two types — so either field alone yields the same pair.
_RELATIONS = (
    # role,      forward field (on the left type), inverse field (on the right type)
    ("set",     "basetype_settype",     "settype_basetype"),
    ("span",    "basetype_spantype",    "spantype_basetype"),
    ("spanset", "spantype_spansettype", "spansettype_spantype"),
)


def type_names(text: str) -> dict:
    """The ``MeosType`` enum-name to public-name map from ``MEOS_TYPE_NAMES``."""
    m = re.search(r'MEOS_TYPE_NAMES\s*\[\]\s*=\s*\{(.*?)\};', text, re.S)
    return dict(_NAME_RE.findall(m.group(1))) if m else {}


def catalog_rows(text: str, array: str) -> list:
    """The ``[T_X] = { .field = T_Y, ... }`` entries of the type-indexed catalog array.

    Returned in file order, which is ``MeosType`` order, the ordering the catalog enforces.
    A release line without the type-indexed ``MEOS_RELTYPE_CATALOG`` states the same
    relations as pair tables, which yield the same entries, ordered by ``MEOS_TYPE_NAMES``,
    the array that lists every type in ``MeosType`` order there. A pair table names its
    relation in its header comment, so ``text`` keeps its block comments.
    """
    m = re.search(re.escape(array) + r'\s*\[\]\s*=\s*\{(.*?)\};', text, re.S)
    if m:
        return [(t, dict((f, v) for f, v in _FIELD_RE.findall(body)))
                for t, body in _ROW_RE.findall(m.group(1))]
    if array != "MEOS_RELTYPE_CATALOG":
        return []
    rows = {}
    for outer_role, inner_role, pairs in _PAIR_TABLE_RE.findall(text):
        for outer, inner in _PAIR_RE.findall(pairs):
            rows.setdefault(outer, {})["%s_%s" % (outer_role, inner_role)] = inner
    order = {t: i for i, t in enumerate(type_names(text))}
    return sorted(rows.items(), key=lambda row: order.get(row[0], len(order)))


def temptype_basetypes(cat_src: str) -> dict:
    """Each temporal type's base type, as ``MEOS_RELTYPE_CATALOG`` states it.

    The forward direction of the relation :func:`attach_type_relations` reads
    the inverse of, published so that a caller wanting one temporal type's base
    — the drift gate checking the lattice's leaves — reads the catalog through
    the same parser rather than matching the array's shape a second time.
    """
    return {t: fields["temptype_basetype"]
            for t, fields in catalog_rows(cat_src, "MEOS_RELTYPE_CATALOG")
            if "temptype_basetype" in fields}


def bbox_types(cat_src: str) -> list:
    """The bounding-box types, as ``MEOS_RELTYPE_CATALOG`` states them.

    The `type_bboxtype` column names the box a type stores, so its distinct
    values are exactly the box types MEOS has — which is what a companion
    class is needed for, and what a hand list of them drifts from.
    """
    seen, out = set(), []
    for _, fields in catalog_rows(cat_src, "MEOS_RELTYPE_CATALOG"):
        bbox = fields.get("type_bboxtype")
        if bbox and bbox != "T_UNKNOWN" and bbox not in seen:
            seen.add(bbox)
            out.append(bbox)
    return out


def locate_temporal_source(src_root: Path | None, filename: str) -> Path | None:
    """A ``meos/src/temporal`` source path from the resolved root, or the ``MDB_SRC_ROOT`` checkout.

    The object-model resolver returns the ``meos/src`` directory when it can, but on the
    installed-headers build path it cannot (the headers carry no source tree), while the provisioning
    still checks out the full repository under ``MDB_SRC_ROOT``. Consulting that env var too keeps the
    registry present in both build paths.
    """
    candidates = []
    if src_root is not None:
        candidates.append(Path(src_root) / "temporal" / filename)
    mdb = os.environ.get("MDB_SRC_ROOT")
    if mdb:
        candidates.append(Path(mdb) / "meos" / "src" / "temporal" / filename)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def locate_catalog(src_root: Path | None) -> Path | None:
    """The ``meos_catalog.c`` path, the type registry's own source."""
    return locate_temporal_source(src_root, "meos_catalog.c")


_PREDICATE_RE = re.compile(r'\nbool\s*\n(\w+)\(' + MEOSTYPE_PARAM + r' type\)\s*\{(.*?)\n\}', re.S)
_TERM_RE = re.compile(r'^(?:type\s*==\s*(T_\w+)|(\w+)\(\s*type\s*\))$')


def type_predicates(text: str, names: dict) -> dict:
    """The members of every membership predicate of ``meos_catalog.c``, in MeosType order.

    A membership predicate is a ``bool <name>(MeosType type)`` whose body returns a
    disjunction of ``type == T_X`` tests and of calls to other membership predicates
    (`numspan_type`, `tnumber_type`, `spatialset_type`, ...): it states a category of types,
    the sets of numbers or the spans a temporal box carries, and the catalog publishes each
    category with its members so that a binding reads which types a category holds where
    MEOS states it. A predicate whose body does anything else is not a membership statement
    and is left out rather than approximated, as #object_model.predicate_types reads only a
    predicate it names.
    """
    bodies = {}
    for name, body in _PREDICATE_RE.findall(text):
        body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
        # A family guard (`#if POINTCLOUD` ... `#endif`) admits its members in the build with
        # every family, which is the build the catalog describes; an alternative (`#else`) is
        # not a membership the catalog can state.
        if re.search(r'^\s*#\s*(else|elif)\b', body, re.M):
            continue
        body = re.sub(r'^\s*#\s*(if|ifdef|ifndef|endif)\b.*$', '', body, flags=re.M)
        m = (re.fullmatch(r'\s*return\s*\(?(.*?)\)?\s*;\s*', body, re.S)
             or re.fullmatch(r'\s*bool\s+(\w+)\s*=\s*\(?(.*?)\)?\s*;\s*return\s+\1\s*;\s*',
                             body, re.S))
        if not m:
            continue
        terms = [t.strip() for t in re.split(r'\|\|', m.group(m.lastindex))]
        parsed = [_TERM_RE.match(t) for t in terms]
        if all(parsed):
            bodies[name] = [(p.group(1), p.group(2)) for p in parsed]

    order = {t: i for i, t in enumerate(names)}
    resolved = {}

    def members(name, seen=()):
        if name in resolved:
            return resolved[name]
        if name in seen or name not in bodies:
            return None
        out = set()
        for literal, call in bodies[name]:
            if literal:
                out.add(literal)
            else:
                sub = members(call, seen + (name,))
                if sub is None:
                    return None
                out |= sub
        resolved[name] = out
        return out

    result = {}
    for name in bodies:
        types = members(name)
        if types and all(t in names for t in types):
            result[name] = [names[t] for t in sorted(types, key=lambda t: order[t])]
    return dict(sorted(result.items()))


def attach_type_relations(idl: dict, src_root: Path | None) -> dict:
    """Attach ``idl["typeRelations"]`` from the ``MEOS_RELTYPE_CATALOG`` array.

    Degrades to no attachment — never a fabricated map — when the source tree is
    not available, mirroring the honest-signal contract of the object-model scan.
    A located catalog that yields no relation is a parse that has lost the array,
    not a catalog without types, and raises rather than attaching an empty
    registry: the consumers read the registry to resolve a concrete collection
    type, so an empty one silently degrades every one of them.
    """
    catalog = locate_catalog(src_root)
    if catalog is None:
        return idl

    text = re.sub(r"//.*", "", catalog.read_text(errors="ignore"))
    names = type_names(text)
    rows = catalog_rows(text, "MEOS_RELTYPE_CATALOG")

    # Each entry names the types related to the type it is indexed by, in both directions.
    related = {role: {} for role, _, _ in _RELATIONS}
    temp_of_base = {}
    for meos_type, fields in rows:
        for role, forward, inverse in _RELATIONS:
            if forward in fields:
                related[role][meos_type] = fields[forward]
            if inverse in fields:
                related[role][fields[inverse]] = meos_type
        # A base names no temporal type of its own, and the instants of SEVERAL may store it:
        # those of tgeompoint and tgeometry a geometry, those of tpose and trgeometry (a
        # reference geometry and a temporal pose) a pose. So
        # the temporal role is the inverse of temptype_basetype, and it is every type naming
        # that base, in MeosType order. Keeping one of them drops the others from the registry
        # entirely, and a generator projecting the temporal types out of it then emits a
        # surface missing a type MEOS has.
        if "temptype_basetype" in fields:
            temp_of_base.setdefault(fields["temptype_basetype"], []).append(meos_type)

    set_of_base = related["set"]
    span_of_base = related["span"]
    # A span set reaches its base through its span.
    spanset_of_base = {}
    for base, span in span_of_base.items():
        spanset = related["spanset"].get(span)
        if spanset is not None:
            spanset_of_base[base] = spanset

    by_base = {}
    for base in set(set_of_base) | set(span_of_base) | set(temp_of_base):
        base_name = names.get(base)
        if base_name is None:
            continue
        record = {}
        # The temporal role is a list for every base, one entry or several, so a consumer
        # reads one shape rather than branching on how many temporal types a base happens
        # to have today.
        temporals = [names[t] for t in temp_of_base.get(base, ()) if names.get(t) is not None]
        if temporals:
            record["temporal"] = temporals
        for role, mapping in (("set", set_of_base), ("span", span_of_base),
                              ("spanset", spanset_of_base)):
            inst = mapping.get(base)
            if inst is not None and names.get(inst) is not None:
                record[role] = names[inst]
        by_base[base_name] = record

    if not by_base:
        raise ValueError(
            f"{catalog}: no type relation parsed from MEOS_RELTYPE_CATALOG — the catalog's "
            "shape has changed and parser/typerelations.py no longer reads it")

    idl["typeRelations"] = {"byBase": dict(sorted(by_base.items())),
                            "predicates": type_predicates(text, names)}
    return idl
