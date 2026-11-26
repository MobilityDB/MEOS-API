"""State every cast MobilityDB declares, and the public MEOS function each one calls.

An engine binding (MobilityDuck, the JVM engines, MobilityNebula) registers the casts of the
SQL surface as MobilityDB declares them with ``CREATE CAST``, so ``sqlCasts`` states them
once, in the order of their source and target types:

- ``source`` and ``target``: the two SQL types, each in the one spelling #_type of
  parser/compositions.py gives it (``float`` for ``float8``).
- ``context``: ``implicit`` (``AS IMPLICIT``), ``assignment`` (``AS ASSIGNMENT``) or
  ``explicit`` (neither), the contexts PostgreSQL applies a cast in.
- ``sqlFunction``: the SQL function of ``WITH FUNCTION``, its ``name`` and its ``args``; a
  cast ``WITHOUT FUNCTION`` has none and states ``binary``, its value kept as it is stored.
- ``typmod``: on a cast whose function takes the type modifier after the value
  (``tgeompoint(tgeompoint, integer)``), which only a PostgreSQL column carries.
- ``function``: the public MEOS function the SQL function calls, the one whose C wrapper is
  the SQL function's (``mdbC``) and one of whose signatures takes the SQL function's
  arguments; a generic wrapper serving several typed functions (``Value_to_set``) is told
  apart by the signature. A cast with no such function, over a type MEOS does not have
  (``int4range``, ``numeric``) or through a wrapper no public function states, has none.

The casts are read under the same built SQL files #_text_casts reads, and each SQL function
from the CREATE FUNCTION statements #sql_statements of parser/sqlfn.py yields.
"""
import re

from parser.compositions import _SCHEMA, _type
from parser.families import built_files
from parser.sqlfn import _bare_type, _strip_sql_comments, sql_statements

_CAST_STMT = re.compile(
    r"CREATE\s+CAST\s*\(\s*([\w.]+)\s+AS\s+([\w.]+)\s*\)\s*"
    r"(?:(WITHOUT\s+FUNCTION)|WITH\s+FUNCTION\s+([\w.]+)\s*\(([^)]*)\))"
    r"\s*(AS\s+IMPLICIT|AS\s+ASSIGNMENT)?", re.I)


def _context(clause):
    """The context of a cast from its trailing clause"""
    if not clause:
        return "explicit"
    return "implicit" if clause.split()[-1].lower() == "implicit" else "assignment"


def _functions(idl, sql_src):
    """{(SQL name, (argument types)): public MEOS function} of every CREATE FUNCTION whose C
    wrapper one public function states with a signature taking those arguments"""
    by_wrapper = {}
    for f in idl.get("functions", []):
        if f.get("mdbC") and f.get("api") == "public":
            by_wrapper.setdefault(f["mdbC"], []).append(f)
    stmts, _, _ = sql_statements(sql_src)
    out = {}
    for st in stmts:
        name, argdecls, wrapper = st[0], st[1], st[3]
        args = tuple(_type(_bare_type(a)) for a in argdecls)
        found = {f["name"] for f in by_wrapper.get(wrapper, [])
                 if any(tuple(_type(a) for a in s.get("args", [])) == args
                        for s in f.get("sqlSignatures") or [])}
        if len(found) == 1:
            out[(name, args)] = found.pop()
    return out


def attach_sql_casts(idl, sql_src):
    """(idl with its top-level ``sqlCasts`` (module docstring), count, count with a MEOS
    function)"""
    functions = _functions(idl, sql_src)
    casts = {}
    for sf in sorted(built_files(sql_src, "*.sql")):
        text = _SCHEMA.sub("", _strip_sql_comments(sf.read_text(errors="ignore")))
        for src, dst, without, fn, fargs, ctx in _CAST_STMT.findall(text):
            entry = {"source": _type(src), "target": _type(dst), "context": _context(ctx)}
            if without:
                entry["binary"] = True
            else:
                args = [_type(a.strip()) for a in fargs.split(",") if a.strip()]
                entry["sqlFunction"] = {"name": fn, "args": args}
                if len(args) > 1:
                    entry["typmod"] = True
                found = functions.get((fn, tuple(args)))
                if found:
                    entry["function"] = found
            casts[(entry["source"], entry["target"])] = entry
    idl["sqlCasts"] = [casts[k] for k in sorted(casts)]
    return idl, len(casts), sum("function" in e for e in casts.values())
