"""State the SQL aggregates and the MEOS function behind each of their roles.

A SQL aggregate is assembled from up to three functions, each a role chapter 18 of the
MobilityDB manual (doc/portable_sql.xml, "Aggregations") names: a transition function,
a combine function for parallel aggregation and a final function. PostgreSQL adds the
functions writing and reading a partial state, which travels between its workers. The
catalog states every `CREATE AGGREGATE` in the top-level list `aggregates`:

    {"sqlName": "tCount", "args": ["tgeompoint"], "ret": "tint", "stype": "internal",
     "transition": {"sqlName": "tCountTransition", "meos": "temporal_tcount_transfn"},
     "combine": {"sqlName": "tcount_combinefn", "meos": "temporal_tcount_combinefn"},
     "final": {"sqlName": "tint_tagg_finalfn", "meos": "temporal_tagg_finalfn"},
     "serialize": {"sqlName": "taggstate_serialize", "meos": null},
     "deserialize": {"sqlName": "taggstate_deserialize", "meos": null}}

`args` are the types of the arguments a caller passes, `ret` the type of the answer:
what the final function returns, or the state type for an aggregate without one
(`extent`). A role the aggregate does not define is absent. A role names the SQL
function PostgreSQL calls, `sqlName`, and the public MEOS function carrying that
function's SQL signature, `meos`, None for one no public MEOS function carries: a
function of PostgreSQL itself (`array_agg_transfn`, `array_agg_combine`), or one of
the extension over PostgreSQL's own memory (`taggstate_serialize`). PostgreSQL refuses
an aggregate naming a function it does not have, so every role names one. A binding
builds an aggregate from the MEOS functions of its roles and never reads SQL.

Each role resolves as PostgreSQL resolves it, by name and argument types: the
transition function over the state type and the aggregate's arguments, the combine
function over two states, the final function over the state (and the aggregate's
arguments under `FINALFUNC_EXTRA`), the serialize function over the state and the
deserialize function over `bytea` and the state."""
import re

from parser.compositions import _type
from parser.sqlfn import _split_top_commas, _strip_sql_comments, sql_signature, sql_statements

_CREATE_AGG = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?AGGREGATE\s+(\w+)\s*\(", re.I)
_OPTION = re.compile(r"^\s*(\w+)\s*(?:=\s*(.+?))?\s*$", re.S)

# The options of a CREATE AGGREGATE naming a function, under the role each one plays.
_ROLES = {"sfunc": "transition", "combinefunc": "combine", "finalfunc": "final",
          "serialfunc": "serialize", "deserialfunc": "deserialize"}


def _create_agg_stmts(text):
    """Yield (sqlName, [raw arg decls], {option: value}) for every CREATE AGGREGATE in
    `text`, its option names lower-cased; the argument list is read as #_create_fn_stmts
    of parser/sqlfn.py reads a CREATE FUNCTION's."""
    for m in _CREATE_AGG.finditer(text):
        i, depth, start = m.end(), 1, m.end()
        while i < len(text) and depth:
            depth += (text[i] == "(") - (text[i] == ")")
            i += 1
        argdecls = [a for a in _split_top_commas(text[start:i - 1]) if a.strip()]
        open_ = text.find("(", i)
        j, depth = open_ + 1, 1
        while j < len(text) and depth:
            depth += (text[j] == "(") - (text[j] == ")")
            j += 1
        options = {}
        for opt in _split_top_commas(text[open_ + 1:j - 1]):
            om = _OPTION.match(opt)
            if om:
                options[om.group(1).lower()] = (om.group(2) or "").strip()
        yield m.group(1), argdecls, options


def _agg_statements(sql_src):
    """Every CREATE AGGREGATE under `sql_src`, as #_create_agg_stmts yields it."""
    out = []
    for sf in sorted(sql_src.rglob("*.sql")):
        out.extend(_create_agg_stmts(_strip_sql_comments(sf.read_text(errors="ignore"))))
    return out


def attach_aggregates(idl, sql_src):
    """(idl with its top-level `aggregates`, count). Raises ValueError naming every
    aggregate with no state type, or with a role two public MEOS functions carry."""
    stmts, vocab, composites = sql_statements(sql_src)
    declared = {}
    for sqlname, argdecls, ret, _, retset, _ in stmts:
        sig = sql_signature(sqlname, argdecls, ret, retset, vocab, composites)
        declared[(sqlname.lower(), tuple(_type(a) for a in sig["args"]))] = sig
    carried = {}
    for f in idl.get("functions", []):
        if f.get("api") != "public":
            continue
        for s in f.get("sqlSignatures") or ():
            name = (s.get("sqlName") or f.get("sqlfn") or "").lower()
            carried.setdefault((name, tuple(_type(a) for a in s["args"])), []).append(f["name"])

    out, errors = [], []
    for sqlname, argdecls, options in _agg_statements(sql_src):
        args = sql_signature(sqlname, argdecls, None, False, vocab, composites)["args"]
        stype = options.get("stype")
        if not stype:
            errors.append(f"{sqlname}({', '.join(args)}): no STYPE")
            continue
        extra = options.get("finalfunc_extra") is not None
        called = {"transition": [stype] + args, "combine": [stype, stype],
                  "final": [stype] + (args if extra else []),
                  "serialize": [stype], "deserialize": ["bytea", stype]}
        entry = {"sqlName": sqlname, "args": args, "ret": None, "stype": stype}
        for option, role in _ROLES.items():
            fn = options.get(option)
            if not fn:
                continue
            # A function may be written with its argument types,
            # `SFUNC = appendInstantTransition(th3index, th3index)`; its name is the role's.
            fn = re.sub(r"\s*\(.*\)\s*$", "", fn, flags=re.S).split(".")[-1]
            key = (fn.lower(), tuple(_type(a) for a in called[role]))
            meos = carried.get(key) or []
            if len(meos) > 1:
                errors.append(f"{sqlname}({', '.join(args)}): {option} "
                              f"{fn}({', '.join(called[role])}) is carried by "
                              f"{', '.join(sorted(meos))}")
                break
            entry[role] = {"sqlName": fn, "meos": meos[0] if meos else None}
            if role == "final":
                entry["ret"] = declared[key]["ret"] if key in declared else None
        else:
            if "final" not in entry:
                entry["ret"] = stype
            out.append(entry)
    if errors:
        raise ValueError("SQL aggregates the catalog cannot state:\n  "
                         + "\n  ".join(errors))
    idl["aggregates"] = out
    return idl, len(out)
