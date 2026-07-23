"""Attach the base-to-collection type-relation registry from ``meos_catalog.c``.

A base type ``T`` is the single parameter of four independent template classes —
``Temporal<T>``, ``Set<T>``, ``Span<T>`` and ``SpanSet<T>``. The positional
catalog arrays in ``meos_catalog.c`` pair each template instance's ``MeosType``
with its base (a span set with its span), and ``MEOS_TYPE_NAMES`` maps a
``MeosType`` to its public name. Inverting the arrays and resolving through the
names yields, for each base type name, the names of its set, span, span set and
temporal types.

This is the static metadata a binding generator needs to pick the concrete
collection type of a value-domain result — ``SpanSet<float>`` is ``floatspanset``
— with no hand-coding: every binding is a projection of the catalog, so the
mapping belongs in the catalog rather than in each generator.
"""
import os
import re
from pathlib import Path

_NAME_RE = re.compile(r'\[\s*(T_\w+)\s*\]\s*=\s*"([^"]+)"')
_PAIR_RE = re.compile(r'\{\s*(T_\w+)\s*,\s*(T_\w+)\s*\}')


def _names(text: str) -> dict:
    """The ``MeosType`` enum-name to public-name map from ``MEOS_TYPE_NAMES``."""
    m = re.search(r'MEOS_TYPE_NAMES\s*\[\]\s*=\s*\{(.*?)\};', text, re.S)
    return dict(_NAME_RE.findall(m.group(1))) if m else {}


def _pairs(text: str, array: str) -> list:
    """The ``{T_A, T_B}`` rows of a positional catalog array, in order."""
    m = re.search(re.escape(array) + r'\s*\[\]\s*=\s*\{(.*?)\};', text, re.S)
    return _PAIR_RE.findall(m.group(1)) if m else []


def _locate_catalog(src_root: Path | None) -> Path | None:
    """The ``meos_catalog.c`` path from the resolved source root, or the ``MDB_SRC_ROOT`` checkout.

    The object-model resolver returns the ``meos/src`` directory when it can, but on the
    installed-headers build path it cannot (the headers carry no source tree), while the provisioning
    still checks out the full repository under ``MDB_SRC_ROOT``. Consulting that env var too keeps the
    registry present in both build paths.
    """
    candidates = []
    if src_root is not None:
        candidates.append(Path(src_root) / "temporal" / "meos_catalog.c")
    mdb = os.environ.get("MDB_SRC_ROOT")
    if mdb:
        candidates.append(Path(mdb) / "meos" / "src" / "temporal" / "meos_catalog.c")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def attach_type_relations(idl: dict, src_root: Path | None) -> dict:
    """Attach ``idl["typeRelations"]`` from the ``meos_catalog.c`` arrays.

    Degrades to no attachment — never a fabricated map — when the source tree is
    not available, mirroring the honest-signal contract of the object-model scan.
    """
    catalog = _locate_catalog(src_root)
    if catalog is None:
        return idl

    text = re.sub(r"//.*", "", catalog.read_text(errors="ignore"))
    names = _names(text)

    # Each array pairs an instance type with the type it is built over: a set,
    # span or temporal with its base; a span set with its span.
    base_of_set = {inst: base for inst, base in _pairs(text, "MEOS_SETTYPE_CATALOG")}
    base_of_span = {inst: base for inst, base in _pairs(text, "MEOS_SPANTYPE_CATALOG")}
    span_of_spanset = {inst: span for inst, span in _pairs(text, "MEOS_SPANSETTYPE_CATALOG")}
    base_of_temp = {inst: base for inst, base in _pairs(text, "MEOS_TEMPTYPE_CATALOG")}

    # Invert to base -> instance; a span set reaches its base through its span.
    set_of_base = {base: inst for inst, base in base_of_set.items()}
    span_of_base = {base: inst for inst, base in base_of_span.items()}
    temp_of_base = {base: inst for inst, base in base_of_temp.items()}
    spanset_of_base = {}
    for spanset, span in span_of_spanset.items():
        base = base_of_span.get(span)
        if base is not None:
            spanset_of_base[base] = spanset

    by_base = {}
    for base in set(set_of_base) | set(span_of_base) | set(temp_of_base):
        base_name = names.get(base)
        if base_name is None:
            continue
        record = {}
        for role, mapping in (("temporal", temp_of_base), ("set", set_of_base),
                              ("span", span_of_base), ("spanset", spanset_of_base)):
            inst = mapping.get(base)
            if inst is not None and names.get(inst) is not None:
                record[role] = names[inst]
        by_base[base_name] = record

    idl["typeRelations"] = {"byBase": dict(sorted(by_base.items()))}
    return idl
