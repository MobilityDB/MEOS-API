"""State the index search each topological and position SQL signature maps to.

An index over boxes answers a predicate by searching for the stored boxes the predicate can
accept, and MEOS names those searches in the enum ``IndexSearchOp`` (``INDEX_OVERLAPS``,
``INDEX_CONTAINED_BY``, ...), whose doc comment names the operator each serves:
``INDEX_OVERLEFT, /**< ..., `&<` operator */``. A ``CREATE OPERATOR`` declaration names the SQL
function backing it and the argument types (``&<`` over ``(stbox, stbox)`` has ``PROCEDURE =
stboxOverleft``), so the boolean SQL signature of that name over those types states

    indexSearch: {columnLeft: <IndexSearchOp>, columnRight: <IndexSearchOp> | null}

``columnLeft`` is the search for ``column <op> query``, the indexed column the first argument:
the value whose comment names the operator. ``columnRight`` is the search for ``query <op>
column``: the value whose comment names the operator's ``COMMUTATOR``, read from the same
declaration, since ``query <op> column`` holds exactly when ``column <commutator> query``
does. An operator declaring no commutator, as the overlapping orderings (``&<``, ``&>``, ...)
declare none, has no search with the column on the right: ``query &< column`` asks for a
stored box whose end is not before the query's end, which no search states, so
``columnRight`` is null and an engine scans, as PostgreSQL does.

The declaration rather than the function's ``sqlop`` decides the operator: a MEOS function
backing several wrappers carries the ``sqlop`` of the first, while each signature carries
its own SQL name.

The operators are read as #sql_statements of parser/sqlfn.py reads the functions: every
``*.sql`` under the SQL root with its comments blanked, each statement bounded by its ``;``.
"""
from __future__ import annotations

import re
from pathlib import Path

from parser.sqlfn import _strip_sql_comments

# `CREATE OPERATOR <symbol> (` opens an operator declaration; its clauses run to the `)` the
# statement's `;` closes.
_CREATE_OP = re.compile(r"CREATE\s+OPERATOR\s+(?:[\w@]+\.)?([^\s(]+)\s*\(", re.I)
_CLAUSE = re.compile(r"(\w+)\s*=\s*('(?:[^']|'')*'|[^,)\s]+)", re.S)
# The operator an `IndexSearchOp` value serves, named in its doc comment: "`&&` operator".
_DOC_OPERATOR = re.compile(r"`([^`]+)`\s+operator")


def _type_name(arg: str) -> str:
    """The SQL type or function an operator clause names, its schema and quotes removed and
    lowercased, as PostgreSQL folds an unquoted name."""
    arg = arg.strip().strip("'\"")
    return arg.split(".")[-1].lower()


def operator_decls(sql_src: str | Path) -> dict[tuple[str, str, str], list[tuple[str, str | None]]]:
    """``{(procedure, leftarg, rightarg): [(symbol, commutator or None), ...]}`` for every
    binary ``CREATE OPERATOR`` under ``sql_src``."""
    decls: dict[tuple[str, str, str], list[tuple[str, str | None]]] = {}
    sql_src = Path(sql_src)
    if not sql_src.exists():
        return decls
    for sf in sorted(sql_src.rglob("*.sql")):
        text = _strip_sql_comments(sf.read_text(errors="ignore"))
        for m in _CREATE_OP.finditer(text):
            end = text.find(";", m.end())
            body = text[m.end(): end if end != -1 else len(text)]
            clauses = {k.upper(): v.strip("'").replace("''", "'")
                       for k, v in _CLAUSE.findall(body)}
            proc = clauses.get("PROCEDURE") or clauses.get("FUNCTION")
            if not proc or "LEFTARG" not in clauses or "RIGHTARG" not in clauses:
                continue
            key = (_type_name(proc), _type_name(clauses["LEFTARG"]),
                   _type_name(clauses["RIGHTARG"]))
            decls.setdefault(key, []).append((m.group(1), clauses.get("COMMUTATOR")))
    return decls


def index_searches(idl: dict) -> tuple[dict[str, str], list[str]]:
    """(``{operator symbol: IndexSearchOp value}``, errors) from the doc comments of the
    ``IndexSearchOp`` values the catalog carries. A value naming no operator, or one operator
    two values name, is an error: the map is the one statement every engine reads."""
    enum = next((e for e in idl.get("enums", []) if e.get("name") == "IndexSearchOp"), None)
    if enum is None:
        return {}, []
    by_symbol: dict[str, str] = {}
    errors = []
    for v in enum.get("values", []):
        found = _DOC_OPERATOR.findall(v.get("doc") or "")
        if len(found) != 1:
            errors.append(f"IndexSearchOp {v['name']} names {len(found)} operators in its doc")
            continue
        if found[0] in by_symbol:
            errors.append(f"IndexSearchOp {v['name']} and {by_symbol[found[0]]} both name "
                          f"`{found[0]}`")
            continue
        by_symbol[found[0]] = v["name"]
    return by_symbol, errors


def attach_index_search(idl: dict, sql_src: str | Path) -> tuple[dict, int, list[str]]:
    """(idl, signatures stated, errors): set ``indexSearch`` on every boolean two-argument
    SQL signature backing, over its argument types, an operator an ``IndexSearchOp`` value
    serves. A signature backing two such operators is an error: it would state two searches."""
    by_symbol, errors = index_searches(idl)
    if not by_symbol:
        return idl, 0, errors
    decls = operator_decls(sql_src)
    n = 0
    for f in idl.get("functions", []):
        for sig in f.get("sqlSignatures") or []:
            args = sig.get("args") or []
            # a predicate only: a value-returning operator (`#>>` reading a jsonb path,
            # `@>` lifted to a temporal boolean) is no index search
            if len(args) != 2 or (sig.get("ret") or "").lower() != "boolean":
                continue
            name = (sig.get("sqlName") or f.get("sqlfn") or "").lower()
            ops = [(op, com) for op, com in decls.get((name, args[0].lower(), args[1].lower()), [])
                   if op in by_symbol]
            if not ops:
                continue
            if len(ops) > 1:
                errors.append(f"{name}({args[0]}, {args[1]}) backs "
                              f"{', '.join(op for op, _ in ops)}")
                continue
            op, commutator = ops[0]
            sig["indexSearch"] = {
                "columnLeft": by_symbol[op],
                "columnRight": by_symbol.get(commutator) if commutator else None,
            }
            n += 1
    return idl, n, errors
