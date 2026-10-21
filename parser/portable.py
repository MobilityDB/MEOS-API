"""Portable bare-name dialect — the single codegen source of truth.

`meta/portable-aliases.json` is the curated, authoritative operator →
bare-name mapping (RFC #920; native in MobilityDB via PR #1075). Folding it
into the catalog means every binding/engine generates the *identical* bare
names, so a user learns one reference and assumes the rest. A position or a
topological operator has one name per class instead (setLeft … stboxLeft,
setOverlaps … stboxOverlaps), derived from the catalog's @sqlfn/@sqlop tags by
``attach_position_names``.

This is curated canonical data, not a heuristic — it is preserved verbatim
and only *derived* lookups are added (no guessing of C symbols: upstream
aliases reuse each operator's own backing function, equivalence by
construction). Pure dict → dict; no libclang.
"""

import json
from pathlib import Path


def attach_portable_aliases(idl: dict, path: Path) -> dict:
    """Attach ``idl["portableAliases"]`` from the canonical mapping file."""
    if not Path(path).exists():
        return idl
    data = json.loads(Path(path).read_text())

    pairs = [p for fam in data["families"].values() for p in fam]
    by_operator = {p["operator"]: p["bareName"] for p in pairs}
    by_bare_name = {p["bareName"]: p["operator"] for p in pairs}

    # Integrity: the mapping must be bijective (no operator or bare name may
    # map two ways) — a collision would make codegen ambiguous.
    if len(by_operator) != len(pairs) or len(by_bare_name) != len(pairs):
        raise ValueError("portable-aliases: duplicate operator or bareName")

    # A position or a topological operator has one SQL name per class of its operands
    # (setLeft … stboxLeft, setOverlaps … stboxOverlaps), so it has no bare name: it is
    # absent from the families above, and its operator and stem are each unique.
    positions = [p for fam in data["positionFamilies"].values() for p in fam]
    by_position_operator = {p["operator"]: p["position"] for p in positions}
    if (len(by_position_operator) != len(positions)
            or len(set(by_position_operator.values())) != len(positions)
            or set(by_position_operator) & set(by_operator)):
        raise ValueError("portable-aliases: duplicate position operator or position")

    idl["portableAliases"] = {
        "provenance": data["provenance"],
        "families": data["families"],
        "positionFamilies": data["positionFamilies"],
        "alreadyCanonical": data["alreadyCanonical"],
        "explicitBacking": data.get("explicitBacking", {}),
        "scope": data["scope"],          # cbuffer/npoint/pose/rgeo in scope
        "notes": data["notes"],
        "byOperator": by_operator,       # "#=" -> "tEqual"
        "byBareName": by_bare_name,      # "tEqual" -> "#="
        "bareNames": sorted(by_bare_name),
        "count": len(pairs),
        "byPositionOperator": by_position_operator,   # "<<" -> "left", "&&" -> "overlaps"
    }
    return idl


def attach_position_names(idl: dict) -> dict:
    """Derive the SQL names of each position or topological operator, by class.

    A position or a topological operator has one SQL name per class of its operands,
    the class followed by the stem: ``<<`` is setLeft, spanLeft, spansetLeft, tboxLeft,
    stboxLeft and tpcboxLeft (MobilityDB#2717), ``&&`` is setOverlaps, spanOverlaps,
    spansetOverlaps, tboxOverlaps, stboxOverlaps and tpcboxOverlaps (MobilityDB#2930),
    a temporal operand taking the class of its bounding box. The names are the
    ``@sqlfn`` tags of the functions whose ``@sqlop`` is the operator, and the class is
    the name less its stem. Adds ``portableAliases.positionNames``: operator -> class
    -> SQL name.

    A release line spells the names one way throughout: master's ``<class><Stem>``
    (spanLeft), or the ``<class>_<stem>`` of a release before the class names
    (span_left, temporal_left on stable-1.3). The spelling of the catalog is the one
    its names take; a name in neither form, or in the other one, raises, as does an
    operator that no function carries: each means the @sqlfn/@sqlop tags and the
    mapping disagree, which a binding would otherwise inherit silently.

    MUST run AFTER ``attach_sqlfn_map`` (sqlfn/sqlop) AND ``attach_portable_aliases``
    (byPositionOperator).
    """
    pa = idl.get("portableAliases")
    if not pa:
        return idl
    by_position_operator = pa["byPositionOperator"]
    names = {op: {} for op in by_position_operator}
    spelled = {}                      # spelling -> the first function taking it
    for f in idl.get("functions", []):
        op = (f.get("sqlop") or "").replace("\\", "")
        sqlfn = f.get("sqlfn") or ""
        if op not in by_position_operator or not sqlfn:
            continue
        pos = by_position_operator[op]
        cls = ""
        for spelling, suffix in (("<class><Stem>", pos[0].upper() + pos[1:]),
                                 ("<class>_<stem>", "_" + pos)):
            if sqlfn.endswith(suffix) and len(sqlfn) > len(suffix):
                cls = sqlfn[:-len(suffix)]
                break
        if not cls:
            raise ValueError(f"portable-aliases: {f['name']} has @sqlop {op} and "
                             f"@sqlfn {sqlfn}, which is not "
                             f"<class>{pos[0].upper() + pos[1:]} or <class>_{pos}")
        spelled.setdefault(spelling, f["name"])
        if len(spelled) > 1:
            other = next(s for s in spelled if s != spelling)
            raise ValueError(f"portable-aliases: {f['name']} spells @sqlfn {sqlfn} as "
                             f"{spelling}, {spelled[other]} as {other}")
        names[op][cls] = sqlfn
    missing = sorted(op for op, by_class in names.items() if not by_class)
    if missing:
        raise ValueError("portable-aliases: no function has @sqlop "
                         + " ".join(missing))
    pa["positionNames"] = {op: dict(sorted(by_class.items()))
                           for op, by_class in names.items()}
    return idl


def classify_backing_sqlfn(idl: dict) -> dict:
    """Mark the shared bounding-box BACKING ``@sqlfn`` tags of a bare name.

    A function behind an operator with a bare name (``~=``, ``same``) may carry a
    SHARED C ``@sqlfn`` tag named ``<op>_bbox`` (same_bbox). That tag is NEVER emitted
    as a ``CREATE FUNCTION`` — the deployed, user-facing SQL name is the operator's bare
    portable alias. The raw ``sqlfn`` is therefore a backing name, not a public one; a
    binding that registers it leaks a function MobilityDB does not expose. Flag those
    records with ``sqlfnBackingOnly`` + the ``publicSqlName`` (the bare alias) so every
    binding uniformly registers the bare name + operator and drops the ``_bbox`` tag. A
    topological operator (``&&``, ``@>``, ``<@``, ``-|-``) has names by class
    (MobilityDB#2930): its ``@sqlfn`` tags are its public names, and a ``_bbox`` tag on
    it is refused by #attach_position_names.

    Not a heuristic: grounded in two catalog-native facts — the ``_bbox`` shared-backing
    convention AND the operator→bareName map. ``publicSqlName`` is defined for every
    record flagged, since only an operator of that map is flagged.

    MUST run AFTER ``attach_sqlfn_map`` (sqlfn/sqlop) AND ``attach_portable_aliases``
    (byOperator) — it reads all three.
    """
    by_operator = (idl.get("portableAliases") or {}).get("byOperator") or {}
    if not by_operator:
        return idl
    for f in idl.get("functions", []):
        sqlfn = f.get("sqlfn") or ""
        op = f.get("sqlop") or ""
        if sqlfn.endswith("_bbox") and op in by_operator:
            f["sqlfnBackingOnly"] = True
            f["publicSqlName"] = by_operator[op]
    return idl
