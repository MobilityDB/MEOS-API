"""State, on each SQL signature, the name a binding publishes where its engine cannot take the
PostgreSQL one.

A PostgreSQL wrapper states that name beside its SQL name: ``@sqlfn round()`` and
``@altsqlfn floatRound(), geoRound(), ...`` on ``Set_round``. Spark and Flink define or reserve
``round``, ``lower``, ``hash``, ``unnest`` and their kin, so a binding of either engine
registers the function under the alternative name alone, and the catalog states it per SQL
signature:

    altSqlName: floatRound

A tag lists one name or several, in the grammar of ``@sqlfn tintSeq(), tfloatSeq()``: every
``name()`` from the tag to the next tag or the comment's close, as #_tag_names reads it. The
name a signature takes follows from three cases:

  * the wrapper states several ``@sqlfn`` names: each pairs with the alternative name in its
    position, as ``@sqlop`` pairs with ``@sqlfn``, so ``Tgeo_rotate_z`` behind ``rotateZ()``
    and ``rotate()`` gives ``geoRotateZ`` and ``geoRotate``;
  * the wrapper states one alternative name: every signature of the wrapper takes it;
  * the wrapper states several alternative names under one ``@sqlfn`` name, one per kind of
    value it serves (``Tnumber_abs``: ``intAbs(), bigintAbs(), floatAbs()``): a signature takes
    the name whose prefix names the base type of its first argument. The prefix names a base
    type as the catalog spells it (``cbuffer``, ``pose``), through its set type (``int`` for
    ``intset``, ``float`` for ``floatset``) or through the class predicate
    ``<prefix>_basetype`` of meos_catalog.c (``geo`` for geometry and geography). The base
    type of a temporal argument is the value it answers at an instant, the return type of
    its ``startValue``: a ``trgeometry``, the concatenation of a reference geometry and a
    temporal pose, answers a geometry. A set, span or span set reaches its base through the
    type registry, and an array is read through its element type.

Every signature resolves to exactly one name or the catalog stops: an alternative name that
no signature's base type selects, or that several select, would hand a binding a wrong or
ambiguous name.
"""
from __future__ import annotations

import re
from pathlib import Path

from parser.sqlfn import _CSQLFN_END, _meos_to_mdb, _wrapper_sql_sigs
from parser.typescope import SQL_ALIASES, TypeFacts

# A doxygen block closing right before the wrapper's `Datum Name(PG_FUNCTION_ARGS)`.
_WRAPPER_BLOCK = re.compile(r"/\*\*((?:(?!\*/).)*)\*/\s*Datum\s+(\w+)\s*\(\s*PG_FUNCTION_ARGS",
                            re.S)
_NAME = re.compile(r"(\w+)\s*\(\)")
# The MEOS spelling of a type PostgreSQL spells otherwise (`integer` is `int4`).
_MEOS_SPELLING = {sql: meos for meos, sql in SQL_ALIASES.items()}


def _tag_names(block: str, tag: str) -> list[str]:
    """Every ``name()`` a tag of the block lists, from the tag to the next tag or the block's
    end, as #_meos_to_mdb reads the value of ``@csqlfn``."""
    names = []
    for m in re.finditer(rf"@{tag}\b", block):
        tail = block[m.end():]
        end = _CSQLFN_END.search(tail)
        names += _NAME.findall(tail[:end.start()] if end else tail)
    return names


def wrapper_names(mdb_src: str | Path) -> dict[str, tuple[list[str], list[str]]]:
    """``{wrapper: (@sqlfn names, @altsqlfn names)}`` for every wrapper stating ``@altsqlfn``."""
    out = {}
    for cf in sorted(Path(mdb_src).rglob("*.c")):
        for m in _WRAPPER_BLOCK.finditer(cf.read_text(errors="ignore")):
            alts = _tag_names(m.group(1), "altsqlfn")
            if alts:
                out[m.group(2)] = (_tag_names(m.group(1), "sqlfn"), alts)
    return out


def _meos_name(sqltype: str) -> str:
    """The MEOS spelling of a SQL type, which the catalog's type names and registry use, without
    the type modifier (``geometry(Point)`` is ``geometry``), as #_type of
    parser/compositions.py drops it before taking the SQL spelling the other way."""
    t = re.sub(r"\s*\(.*\)$", "", sqltype.strip().lower())
    return _MEOS_SPELLING.get(t, t)


def value_bases(idl: dict) -> dict[str, str]:
    """``{SQL type: base type}``: a temporal type's ``startValue`` return type, a set, span or
    span set type's base in the type registry, each in its MEOS spelling."""
    out = {}
    for base, rel in ((idl.get("typeRelations") or {}).get("byBase") or {}).items():
        for role in ("set", "span", "spanset"):
            if rel.get(role):
                out[rel[role]] = base
    for f in idl.get("functions", []):
        for s in f.get("sqlSignatures") or ():
            if (s.get("sqlName") or f.get("sqlfn")) == "startValue" and len(s["args"]) == 1 \
                    and s.get("ret"):
                out.setdefault(s["args"][0], _meos_name(s["ret"]))
    return out


def prefix_bases(prefix: str, idl: dict, facts: TypeFacts) -> set[str]:
    """The base types an alternative name's prefix names: the type of that name, the base whose
    set type is ``<prefix>set``, and the members of the class predicate ``<prefix>_basetype``."""
    bases = {prefix} if prefix in facts.names else set()
    for base, rel in ((idl.get("typeRelations") or {}).get("byBase") or {}).items():
        if rel.get("set") == prefix + "set":
            bases.add(base)
    return bases | facts.klass.get(prefix + "_basetype", set())


def _prefix(alt: str, sqlname: str) -> str | None:
    """The prefix an alternative name puts before the PostgreSQL name it stands for."""
    if alt.lower().endswith(sqlname.lower()) and len(alt) > len(sqlname):
        return alt[:-len(sqlname)]
    return None


def attach_alt_sql_names(idl: dict, meos_src: str | Path, mdb_src: str | Path,
                         sql_src: str | Path, facts: TypeFacts | None = None
                         ) -> tuple[dict, int, list[str]]:
    """(idl, signatures stated, errors): set ``altSqlName`` on every SQL signature whose
    wrapper states ``@altsqlfn``. ``facts`` defaults to the type facts of the MEOS tree above
    ``meos_src``, as #attach_row_sources defaults its declared columns."""
    names = wrapper_names(mdb_src)
    if not names:
        return idl, 0, []
    m2d = _meos_to_mdb(meos_src)
    w2sig = _wrapper_sql_sigs(sql_src)
    facts = facts or TypeFacts(Path(meos_src).parent)
    bases = value_bases(idl)
    n, errors = 0, []
    for f in idl.get("functions", []):
        for s in f.get("sqlSignatures") or ():
            sqlname = s.get("sqlName") or f.get("sqlfn")
            wrapper = next((w for w in m2d.get(f["name"]) or () if w in names and any(
                ws["sqlName"] == sqlname and ws["args"] == s["args"] for ws in w2sig.get(w, ()))),
                None)
            if wrapper is None:
                continue
            sqlfns, alts = names[wrapper]
            where = f"{wrapper} {sqlname}({', '.join(s['args'])})"
            if len(sqlfns) > 1:
                if len(alts) != len(sqlfns) or sqlname not in sqlfns:
                    errors.append(f"{where}: @sqlfn {sqlfns} does not pair with @altsqlfn {alts}")
                    continue
                alt = alts[sqlfns.index(sqlname)]
            elif len(alts) == 1:
                alt = alts[0]
            else:
                arg = s["args"][0].removesuffix("[]") if s["args"] else ""
                base = bases.get(arg, _meos_name(arg))
                hits = [a for a in alts
                        if (p := _prefix(a, sqlname)) and base in prefix_bases(p, idl, facts)]
                if len(hits) != 1:
                    errors.append(f"{where}: base {base} selects {hits or 'none'} of {alts}")
                    continue
                alt = hits[0]
            s["altSqlName"] = alt
            n += 1
    return idl, n, errors
