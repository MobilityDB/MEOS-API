"""Attach the SQL-name map (@sqlfn / @sqlop) to the MEOS-API catalog.

The catalog carries MEOS-C function names + C signatures, but bindings that
emit a SQL/UDF surface (MobilityDB SQL, MobilitySpark UDFs, MobilityDuck, …)
need the user-facing SQL name and operator. Both are machine-extractable from
the doxygen tag chain that already pervades the source:

  MEOS-C fn  --@csqlfn #MobilityDB_C()-->  MobilityDB-C wrapper
  MobilityDB-C wrapper  --@sqlfn sqlName() / @sqlop @p <op>-->  SQL name + op

So: in meos/src `@csqlfn #Wrapper()` sits above the MEOS-C function (→ MEOS-C →
Wrapper); in mobilitydb/src `@sqlfn name()` + `@sqlop @p <op>` sit above
`Datum Wrapper(PG_FUNCTION_ARGS)` (→ Wrapper → name, op). Join on Wrapper.

Adds per function (when the chain resolves): `sqlfn`, `sqlop`, `mdbC`.
"""
import json
import re
from pathlib import Path

from parser.shapeinfer import _out_count_param
from parser.typescope import (C_BASE_TYPES, TypeFacts, declared_scopes, read_bodies,
                              sql_spellings,
                              require_scopes, resolve_scope, signatures_for)

# A @csqlfn tag carries one OR MORE #Wrapper() references — comma- or
# space-separated, and possibly continued across doxygen lines — because a single
# MEOS function can back several wrappers (the ever/always pair eDisjoint/aDisjoint
# share one ea_* function; the shift/scale/shift_scale trio share one C function).
# The tag value runs from @csqlfn up to the next doxygen tag or the comment close.
_CSQLFN = re.compile(r"@csqlfn\b")
_CSQLFN_REF = re.compile(r"#(\w+)\s*\(\)")
_CSQLFN_END = re.compile(r"@\w|\*/")
# @csqlaggfn names the SQL AGGREGATE(s) a transition/combine/final function
# implements, following the standard PostgreSQL aggregate model
# (<aggregate>Transition / Combine / Final). Unlike @csqlfn — which points at a PG
# wrapper that then carries the @sqlfn (two hops) — @csqlaggfn is ONE hop: the
# #Name() reference IS the SQL aggregate-role name (#setUnionTransition()). A member
# shared by two aggregates (the spanset union finalfn) carries both, so collect all
# references (reusing _CSQLFN_REF / _CSQLFN_END, the same value grammar).
_CSQLAGGFN = re.compile(r"@csqlaggfn\b")
# After the doxygen close, the MEOS-C definition. The return type may sit on its
# own line (`bool\nleft_tpcbox_tpcbox(`) OR on the same line as the name
# (`bool tpcbox_eq(const TPCBox *box1, ...)`, the one-line predicate style). Match
# both: an optional return-type line, then an optional same-line type prefix
# (word/space/`*` only), then `name(`. Without the same-line case a one-line def is
# not matched and its @csqlfn silently attaches to the NEXT matchable definition,
# collapsing several wrappers onto one MEOS function (the tpcbox_eq..ge comparison
# operators lost their SQL name that way).
_FNDEF = re.compile(r"\*/\s*\n(?:[^\n(){};=]+\n)?(?:[\w\s*]+?\s)?(\w+)\s*\(")
_SQLFN = re.compile(r"@sqlfn\s+(\w+)\s*\(\)")
# @sqlaggfn names the SQL AGGREGATE a PG wrapper serves, on the wrapper of one of
# its transition/combine/final members. It is the aggregate counterpart of
# @sqlfn on the SAME block: @sqlfn states the CREATE FUNCTION the wrapper backs
# (tcount_transfn), @sqlaggfn the CREATE AGGREGATE that function implements
# (tCount). Without it the aggregate name has nowhere to live but @sqlfn, which
# then holds a name no CREATE FUNCTION carries and leaves a consumer to tell an
# aggregate from a function by the C symbol's suffix. Same value grammar as
# @sqlfn — bare `name()`, never `#Name()` — so a block may name several.
_SQLAGGFN = re.compile(r"@sqlaggfn\s+(\w+)\s*\(\)")
# The operator stops at a comma, mirroring `_SQLFN`'s `(\w+)\s*\(\)`: a block naming several
# SQL functions lists their operators the same comma-separated way
# (`@sqlop @p ->, @p ->>` beside `@sqlfn a(), b()`), and a PostgreSQL operator name never
# contains a comma. `(\S+)` ran past it and published the comma as part of the operator.
_SQLOP = re.compile(r"@sqlop\s+@p\s+([^\s,]+)")
_DATUM = re.compile(r"Datum\s+(\w+)\s*\(\s*PG_FUNCTION_ARGS")
# `CREATE [OR REPLACE] FUNCTION name(` — the SQL-facing signature; the wrapper it
# binds is in the trailing `AS 'MODULE_PATHNAME', '<Wrapper>'`.
_CREATE_FN = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(\w+)\s*\(", re.I)
_AS_WRAPPER = re.compile(r"AS\s+'[^']*'\s*,\s*'(\w+)'", re.I)
# A CREATE FUNCTION attribute that may follow RETURNS <type> before the body.
_RET_ATTR = re.compile(
    r"\b(?:SUPPORT|LANGUAGE|WINDOW|IMMUTABLE|STABLE|VOLATILE|LEAKPROOF|CALLED|RETURNS\s+NULL|"
    r"STRICT|SECURITY|PARALLEL|COST|ROWS|TRANSFORM|SET)\b", re.I)


def _split_top_commas(s):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


_ARGMODE = re.compile(r"^(?:IN|OUT|INOUT|VARIADIC)\s+", re.I)
# The argument modes naming a column of the row a function returns: PostgreSQL
# declares a record-returning function's columns as OUT (or INOUT) arguments.
_OUTMODE = re.compile(r"^(?:OUT|INOUT)\s+", re.I)
# `CREATE TYPE name AS (` — a composite type, whose members are the columns of the
# row a function returning it gives.
_CREATE_COMPOSITE = re.compile(r"CREATE\s+TYPE\s+(\w+)\s+AS\s*\(", re.I)


def _arg_default(decl):
    """The literal default expression of an arg declaration, or None for a required arg.
    Complement of `_bare_type` (same split, the other side): `integer DEFAULT 0` -> `0`,
    `text DEFAULT NULL` -> `NULL`, `float` -> None. Kept verbatim from the SQL source
    (no interpretation) so a consumer of an optional trailing arg has its omitted value."""
    a = _ARGMODE.sub("", decl.strip())
    parts = re.split(r"\bDEFAULT\b|=", a, maxsplit=1, flags=re.I)
    return parts[1].strip() if len(parts) > 1 and parts[1].strip() else None


def _bare_type(decl):
    """A CREATE FUNCTION arg declaration with its argmode and DEFAULT / `= expr` clause
    stripped — leaving `[argname] argtype`, argtype possibly multi-word (double precision)."""
    a = _ARGMODE.sub("", decl.strip())
    return re.split(r"\bDEFAULT\b|=", a, maxsplit=1, flags=re.I)[0].strip()


def _is_in_arg(decl):
    """Whether a CREATE FUNCTION argument is passed by the caller: every mode but OUT."""
    return not re.match(r"^OUT\s+", decl.strip(), re.I)


def _column(decl):
    """`(name, type)` of a column declaration: an OUT argument (`OUT i integer`) or a
    composite member (`value integer`, `times bigint[]`)."""
    name, _, typ = _bare_type(decl).partition(" ")
    return name, " ".join(typ.split())


def _composite_types(text):
    """Yield (typeName, [(column, type), ...]) for every composite type in `text`."""
    for m in _CREATE_COMPOSITE.finditer(text):
        i, depth = m.end(), 1
        while i < len(text) and depth:
            depth += (text[i] == "(") - (text[i] == ")")
            i += 1
        yield m.group(1), [_column(c) for c in _split_top_commas(text[m.end():i - 1])
                           if c.strip()]


def _arg_type(decl, vocab):
    """The concrete SQL type of one argument, resolved MECHANICALLY (no hardcoded type
    list). `vocab` is the .in.sql's own type surface, gathered from the unambiguous
    positions (single-token bare args + every RETURNS clause). The type is the longest
    trailing run of tokens that is in `vocab`; any leading tokens are the optional
    argument NAME (`dist float` -> float, `lowerInc boolean` -> boolean)."""
    a = _bare_type(decl)
    if not a or a in vocab:
        return a
    toks = a.split()
    for k in range(len(toks)):
        cand = " ".join(toks[k:])
        if cand in vocab:
            return cand
    return toks[-1] if toks else a


def _strip_sql_comments(text):
    """`text` with its SQL comments blanked and every newline kept.

    The SQL counterpart of #strip_comments of parser/temporaltypes.py: a
    `-- comment` runs to the end of its line and a `/* comment */` to its close,
    PostgreSQL nesting one block comment inside another. A string literal is
    stepped over, its quote doubled inside it (`'it''s'`), so a comment opener
    in a literal stays part of the literal. A statement commented out is no
    declaration, and a commented line inside one is no part of it."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        elif text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif text[i] == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if text.startswith("''", j):
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _create_fn_stmts(text):
    """Yield (sqlName, [raw arg decls], returnType|None, wrapper|None, retSet) for every
    CREATE FUNCTION in `text`, each parsed STATEMENT-BOUNDED (to its terminating `;`).
    returnType is the type of one returned row; retSet is True for `RETURNS SETOF`,
    PostgreSQL's `proretset`, a function returning any number of such rows.
    Bounding to the `;` is what stops a `LANGUAGE SQL` default-arg overload (whose own
    `AS 'SELECT ...'` has no C symbol) from bleeding its RETURNS/AS across the boundary
    into the next C-backed statement — the cross-statement mis-attribution that produced
    garbage return types. wrapper is None for a LANGUAGE SQL / $$ body (no C symbol)."""
    for m in _CREATE_FN.finditer(text):
        sqlname = m.group(1)
        i, depth, start = m.end(), 1, m.end()
        while i < len(text) and depth:
            depth += (text[i] == "(") - (text[i] == ")")
            i += 1
        arg_close = i - 1
        semi = text.find(";", i)
        tail = text[i:semi if semi != -1 else len(text)]        # ') RETURNS <t> AS ...'
        wm = _AS_WRAPPER.search(tail)
        wrapper = wm.group(1) if wm else None
        rm = re.match(r"\s*RETURNS\s+(SETOF\s+)?(.+?)\s+AS\b", tail, re.I | re.S)
        ret = " ".join(rm.group(2).split()) if rm else None
        retset = bool(rm and rm.group(1))
        if ret:
            # PostgreSQL lets the function attributes come in any order, so an
            # attribute may sit between RETURNS and AS rather than after the body.
            # MobilityDB writes SUPPORT after `AS 'MODULE_PATHNAME'` everywhere but
            # `aTouches(tcbuffer, cbuffer)`, which puts it first and so parsed as the
            # return type `boolean SUPPORT tspatial_supportfn`. Keep only the type.
            ret = _RET_ATTR.split(ret, maxsplit=1)[0].strip() or ret
        argdecls = [a for a in _split_top_commas(text[start:arg_close]) if a.strip()]
        yield sqlname, argdecls, ret, wrapper, retset


def _wrapper_sql_sigs(sql_src):
    """MobilityDB-C wrapper name -> list of per-overload SQL signatures
    {sqlName, args:[type,...], required, ret, retSet, columns}, straight from the CREATE FUNCTION
    statements. The .in.sql CREATE FUNCTION set IS the exact SQL registration surface,
    so a binding emits ONE registration per signature over the concrete arg types with
    NO type-scope heuristic — e.g. `minInstant` lands on exactly its four overloads
    {tint,tbigint,tfloat,ttext}, never over tbool or the geo types. `required` counts the
    non-DEFAULT args (args beyond it are SQL-optional); `ret` is the concrete SQL subtype
    the polymorphic `Temporal *` C return loses. `args` are the arguments a caller
    passes; the OUT arguments are no input but the columns of the row returned, and
    `columns` lists them, or the members of the composite type returned, as
    (name, type) pairs, None for a function returning one value. Two passes: gather
    the type vocabulary and the composite types, then resolve every arg's type
    against the vocabulary."""
    out = {}
    sql_src = Path(sql_src)
    if not sql_src.exists():
        return out
    stmts, vocab, composites = [], set(), {}
    for sf in sorted(sql_src.rglob("*.sql")):
        text = _strip_sql_comments(sf.read_text(errors="ignore"))
        composites.update(_composite_types(text))
        for sqlname, argdecls, ret, wrapper, retset in _create_fn_stmts(text):
            stmts.append((sqlname, argdecls, ret, wrapper, retset))
            if ret:
                vocab.add(ret)                                  # a RETURNS clause is always a type
            for a in argdecls:
                bt = _bare_type(a)
                if bt and " " not in bt:
                    vocab.add(bt)                               # a single-token arg is always a type
    for sqlname, argdecls, ret, wrapper, retset in stmts:
        if wrapper is None:
            continue                                            # LANGUAGE SQL / $$ body — no C symbol
        indecls = [a for a in argdecls if _is_in_arg(a)]
        args = [_arg_type(a, vocab) for a in indecls]
        arg_defaults = [_arg_default(a) for a in indecls]
        required = sum(1 for a in indecls if not re.search(r"\bDEFAULT\b", a, re.I))
        outcols = [_column(_OUTMODE.sub("", a.strip())) for a in argdecls
                   if _OUTMODE.match(a.strip())]
        columns = outcols or composites.get(ret)
        out.setdefault(wrapper, []).append(
            {"sqlName": sqlname, "args": args, "required": required,
             "argDefaults": arg_defaults, "ret": ret, "retSet": retset,
             "columns": columns if columns and len(columns) > 1 else None})
    return out


def _meos_to_mdb(meos_src):
    """MEOS-C function name -> ordered list of MobilityDB-C wrapper names (from
    @csqlfn). One MEOS function can back more than one wrapper — the ever/always
    pair eDisjoint/aDisjoint share a single ea_* function tagged
    `@csqlfn #Edisjoint_…() #Adisjoint_…()` — so each @csqlfn carries one or more
    #Wrapper() references; collect them all (mirrors _mdb_to_sql collecting every
    @sqlfn rather than the first)."""
    out = {}
    for cf in Path(meos_src).rglob("*.c"):
        text = cf.read_text(errors="ignore")
        for m in _CSQLFN.finditer(text):
            tail = text[m.end():]
            end = _CSQLFN_END.search(tail)
            value = tail[:end.start()] if end else tail
            wrappers = _CSQLFN_REF.findall(value)
            if not wrappers:
                continue
            fm = _FNDEF.search(text, m.end())
            if not fm:
                continue
            lst = out.setdefault(fm.group(1), [])
            for w in wrappers:
                if w not in lst:
                    lst.append(w)
    return out


def _meos_agg_names(meos_src):
    """MEOS-C aggregate function name -> ordered list of SQL aggregate-role names
    (from @csqlaggfn). One hop: the #Name() references ARE the SQL names
    (#setUnionTransition()), so there is no wrapper indirection to resolve — unlike
    _meos_to_mdb, whose #Wrapper() references need a second _mdb_to_sql hop. A member
    shared by two aggregates (spanset_union_finalfn) carries several names."""
    out = {}
    for cf in Path(meos_src).rglob("*.c"):
        text = cf.read_text(errors="ignore")
        for m in _CSQLAGGFN.finditer(text):
            tail = text[m.end():]
            end = _CSQLFN_END.search(tail)
            value = tail[:end.start()] if end else tail
            names = _CSQLFN_REF.findall(value)
            if not names:
                continue
            fm = _FNDEF.search(text, m.end())
            if not fm:
                continue
            lst = out.setdefault(fm.group(1), [])
            for nm in names:
                if nm not in lst:
                    lst.append(nm)
    return out


def _mdb_to_sql(mdb_src):
    """MobilityDB-C wrapper name -> ordered list of (sqlfn, sqlop).

    A shared PG wrapper can carry more than one @sqlfn (e.g. Temporal_derivative
    is exposed as both derivative() and speed()), so collect ALL of them rather
    than the first — otherwise the mapped SQL name is order-dependent.
    """
    out = {}
    for cf in Path(mdb_src).rglob("*.c"):
        text = cf.read_text(errors="ignore")
        for m in _SQLFN.finditer(text):
            sqlfn = m.group(1)
            # @sqlop lives in the SAME doxygen block (before the closing */).
            close = text.find("*/", m.end())
            block = text[m.start():close] if close != -1 else text[m.start():m.start() + 800]
            op = _SQLOP.search(block)
            dm = _DATUM.search(text, close if close != -1 else m.end())
            if dm:
                entry = (sqlfn, op.group(1) if op else None)
                lst = out.setdefault(dm.group(1), [])
                if entry not in lst:
                    lst.append(entry)
    return out


def _mdb_to_agg(mdb_src):
    """MobilityDB-C wrapper name -> ordered list of SQL aggregate names.

    Mirrors `_mdb_to_sql` on the @sqlaggfn tag: the same doxygen block carries
    @sqlfn for the CREATE FUNCTION and @sqlaggfn for the CREATE AGGREGATE, so a
    wrapper resolves to both without either name displacing the other. A member
    shared by several aggregates lists them, and duplicates collapse."""
    out = {}
    for cf in Path(mdb_src).rglob("*.c"):
        text = cf.read_text(errors="ignore")
        for m in _SQLAGGFN.finditer(text):
            close = text.find("*/", m.end())
            dm = _DATUM.search(text, close if close != -1 else m.end())
            if dm:
                lst = out.setdefault(dm.group(1), [])
                if m.group(1) not in lst:
                    lst.append(m.group(1))
    return out


_DOXY_BLOCK = re.compile(r"/\*\*.*?\*/", re.S)


def _meos_direct_sql(meos_src):
    """MEOS-C function name -> (sqlfn|None, sqlop|None) from a DIRECT @sqlfn /
    @sqlop tag in meos/src — one hop, mirroring @csqlaggfn. This is the tag form
    for a surface whose PostgreSQL registration is DEFERRED to a host extension
    (the h3index scalar functions defer to h3-pg), so no PG wrapper exists to
    carry the tag: the canonical SQL name lives with the MEOS function itself.
    A block that also carries @csqlfn keeps the two-hop wrapper chain (skipped
    here), and attach_sqlfn_map consults this map ONLY for functions the wrapper
    chain did not resolve — fill-only, never an override."""
    out = {}
    for cf in Path(meos_src).rglob("*.c"):
        text = cf.read_text(errors="ignore")
        for bm in _DOXY_BLOCK.finditer(text):
            block = bm.group(0)
            if "@csqlfn" in block or "@csqlaggfn" in block:
                continue
            # Anchor on @sqlfn exactly as the wrapper-side scan does (_mdb_to_sql):
            # an @sqlop-only block carries no SQL NAME and stays out of the map on
            # both sides, so the fallback restores names without inventing new
            # operator-only attributions.
            sm = _SQLFN.search(block)
            if not sm:
                continue
            om = _SQLOP.search(block)
            fm = _FNDEF.search(text, bm.end() - 2)
            if not fm:
                continue
            out.setdefault(fm.group(1),
                           (sm.group(1), om.group(1) if om else None))
    return out


_COLUMNS_META = Path(__file__).resolve().parent.parent / "meta" / "sql-columns.json"


def declared_columns(path=_COLUMNS_META):
    """The column facts stated in `meta/sql-columns.json`, keyed by the composite
    type a function returns, or by its SQL name when it returns `record`."""
    doc = json.loads(Path(path).read_text())
    return {key: entry["columns"] for key, entry in doc["rows"].items()}


def _c_base(ctype):
    """A C type without `const`, `struct` and its pointer levels, with the
    `<stdint.h>` spellings read as MEOS's own (`int64_t` is `int64`), and the number of
    pointer levels: `const Temporal **` is (`Temporal`, 2)."""
    t = re.sub(r"\b(?:const|struct)\b", "", ctype or "")
    stars = t.count("*")
    t = " ".join(t.replace("*", " ").split())
    return re.sub(r"^(u?int(?:8|16|32|64))_t$", r"\1", t), stars


# The cell ids MEOS declares by their own name, `typedef uint64 H3Index` and alike,
# which #_TYPE_MAP of parser/typerecover.py spells `uint64_t` catalog-wide, beside the
# SQL type each one is.
_CELL_IDS = {"H3Index": "h3index", "Quadbin": "quadbin", "S2CellId": "s2cell"}


def _sql_ctypes(idl):
    """SQL type name -> the C types a value of it can arrive as, read from the
    catalog: a class is its `cType` (the object model names `TInt`, `TsTzSpanSet`,
    `Geometry` after the SQL types, lower-cased), every temporal type a `Temporal`,
    a base type its C spelling (#C_BASE_TYPES of parser/typescope.py, in
    PostgreSQL's spelling through #sql_spellings), a cell id `uint64`, and every
    base type of the type relations also a `Datum`."""
    out = {}
    for cls, rec in ((idl.get("objectModel") or {}).get("classes") or {}).items():
        if rec.get("cType"):
            out.setdefault(cls.lower(), set()).add(_c_base(rec["cType"])[0])
    for temptype in idl.get("temporalTypes") or {}:
        out.setdefault(temptype, set()).add("Temporal")
    for sqltype in _CELL_IDS.values():
        out.setdefault(sqltype, set()).add("uint64")
    for c, meos in C_BASE_TYPES.items():
        for m in (meos if isinstance(meos, tuple) else (meos,)):
            for name in sql_spellings({m}):
                out.setdefault(name, set()).add(c)
    for base in ((idl.get("typeRelations") or {}).get("byBase") or {}):
        for name in sql_spellings({base}):
            out.setdefault(name, set()).add("Datum")
    return out


def _row_slots(func, retset, width, struct, classes):
    """The C values that can feed the columns of one row of `func`, in the order a
    row lists them: each is (source, C type, pointer levels) where `source` is the
    column's `from` and, inside one C value, its `element` or `field`.

    A set of rows reads one element of each array per row: the returned array
    (split into `groupSize` elements per row when it is flattened, into the fields
    of its struct when it is an array of structs) and each out-parameter array.
    One row reads the returned value, each element of a returned fixed array (a
    quaternion), and each out-parameter, an array out-parameter whole; a `bool`
    returned beside out-parameters says whether there is a row, and feeds none.
    The count of an array feeds no column, and a struct a class stands for (a
    `TBox` tile) is one value, not its fields."""
    shape = func.get("shape") or {}
    ar = shape.get("arrayReturn")
    params = [(p["name"], p.get("cType")) for p in func.get("params") or ()]
    length = _out_count_param(func)
    outs = set(shape.get("outParams") or ())
    outarrays = {a["param"] for a in shape.get("outputArrays") or ()}
    slots = []
    if ar:
        elem, stars = _c_base(ar["element"]["c"])
        if ar.get("groupSize"):
            slots += [({"from": "return", "element": k}, elem, stars)
                      for k in range(ar["groupSize"])]
        elif struct and not stars and elem not in classes:
            slots += [({"from": "return", "field": f["name"]}, *_c_base(f["cType"]))
                      for f in struct["fields"]]
        elif not retset and not outarrays:
            slots += [({"from": "return", "element": k}, elem, stars)
                      for k in range(width)]
        else:
            slots.append(({"from": "return"}, elem, stars))
    else:
        ret, stars = _c_base((func.get("returnType") or {}).get("c"))
        if ret not in ("void", "bool") or not outs:
            slots.append(({"from": "return"}, ret, stars))
    for name, ctype in params:
        if name in outs and name != length:
            base, stars = _c_base(ctype)
            # An out-parameter points at what it returns: one pointer level less. A
            # set of rows reads one element of an array out-parameter per row.
            stars -= 1 + (retset and name in outarrays)
            slots.append(({"from": name}, base, stars))
    return slots


def _fits(sqltype, cbase, stars, sqlc):
    """Whether a C value of base type `cbase` behind `stars` pointer levels can be a
    value of SQL type `sqltype`: a `Datum` or a by-value base type bare (`int`,
    `TimestampTz`), a struct bare (a `TBox` array element) or behind one pointer
    (`Temporal *`, `SpanSet *`), an SQL array a C array of its elements."""
    if sqltype.endswith("[]"):
        return stars >= 1 and _fits(sqltype[:-2], cbase, stars - 1, sqlc)
    if cbase not in sqlc.get(sqltype, ()):
        return False
    return stars == 0 if cbase == "Datum" else stars <= 1


def _column_sources(func, sig, sqlc, declared, struct):
    """The columns of the row `sig` returns, each naming the C value feeding it by
    `from`: `return`, the value or array the function returns, or an out-parameter,
    and `element` or `field` inside it. Each column is fed by the first C value of
    its type not already feeding one, so the SQL column order and the C parameter
    order need not agree: `valueTimeSplit` rows `(number, time, tnumber)` read the
    out-parameters `value_bins`, `time_bins` and the returned fragments,
    `tDisjointPairs` rows `(i, j, periods)` the two elements of the returned index
    pair and the out-parameter `periods`. None when a column fits no C value, when
    it fits values of two C sources, or when a C value feeds no column.

    `meta/sql-columns.json` states what only the wrapper does: a column numbering
    the rows from 1, as PostgreSQL's WITH ORDINALITY (`"from": "ordinal"`, the index
    of a tile), and a constant the wrapper adds (`"offset": 1`, turning a C array
    index into a SQL array position)."""
    stated = declared.get(sig["ret"]) or declared.get(sig.get("sqlName") or func.get("sqlfn")) or {}
    cols = sig["columns"]
    fed = [c for c in cols if "from" not in stated.get(c["name"], {})]
    classes = {c for cs in sqlc.values() for c in cs}
    slots = _row_slots(func, sig.get("retSet", False), len(fed), struct, classes)
    used, out = set(), {}
    for c in fed:
        fits = [k for k, (_, base, stars) in enumerate(slots)
                if k not in used and _fits(c["type"], base, stars, sqlc)]
        if not fits or len({slots[k][0]["from"] for k in fits}) > 1:
            return None
        used.add(fits[0])
        out[c["name"]] = dict(slots[fits[0]][0])
    if len(used) != len(slots):
        return None
    result = []
    for c in cols:
        entry = {"name": c["name"], "type": c["type"]}
        entry.update(out.get(c["name"], {}))
        entry.update(stated.get(c["name"], {}))
        result.append(entry)
    return result


def attach_row_sources(idl, declared=None):
    """Name the C value feeding each column of every row a SQL signature returns.

    Runs once the object model and the type relations are attached, since a column
    is matched to a C value by type. A row whose columns no C value and no
    declaration feeds stops the catalog: guessing a column's source would hand a
    binding a row PostgreSQL does not return."""
    declared = declared_columns() if declared is None else declared
    sqlc = _sql_ctypes(idl)
    structs = {s["name"]: s for s in idl.get("structs") or ()}
    unfed, n = [], 0
    for f in idl["functions"]:
        ar = (f.get("shape") or {}).get("arrayReturn")
        struct = structs.get(_c_base(ar["element"]["c"])[0]) if ar else None
        for s in f.get("sqlSignatures") or ():
            if not s.get("columns"):
                continue
            columns = _column_sources(f, s, sqlc, declared, struct)
            if columns is None:
                unfed.append(f"{f['name']}: {s.get('sqlName', f.get('sqlfn'))}"
                             f"({', '.join(s['args'])}) RETURNS {s['ret']}")
            else:
                s["columns"] = columns
                n += 1
    if unfed:
        raise ValueError(
            "SQL rows with a column MEOS states no source for; state it in "
            "meta/sql-columns.json:\n  " + "\n  ".join(sorted(set(unfed))))
    return idl, n


def attach_sqlfn_map(idl, meos_src, mdb_src, sql_src=None):
    m2d = _meos_to_mdb(meos_src)
    d2s = _mdb_to_sql(mdb_src)
    w2sig = _wrapper_sql_sigs(sql_src) if sql_src else {}
    direct = _meos_direct_sql(meos_src)
    n = 0
    # One wrapper commonly backs a whole per-type family — `Set_values` is the body
    # behind getValues(intset), getValues(cbufferset) and fourteen more — so a
    # wrapper's signature list is the union over its claimants, not the surface of
    # any one of them. Each function therefore keeps only the signatures its own
    # TYPE SCOPE covers; scopes MEOS does not state are declared in
    # meta/type-scope.json, and an underivable claimant fails generation rather
    # than silently taking the union or nothing.
    scope_facts = scope_bodies = scope_params = None
    declared = {}
    shared_wrappers = set()
    if w2sig:
        meos_root = Path(meos_src).parent
        scope_facts = TypeFacts(meos_root)
        scope_bodies, scope_params = read_bodies(meos_root)
        declared = declared_scopes()
        claimed = {}
        for f in idl["functions"]:
            if f.get("api") != "public":
                continue
            # EVERY wrapper the function claims, not just the first: a wrapper is
            # shared whenever two functions name it, in whatever position. Reading
            # only the first left a wrapper claimed second by everyone (Numset_scale,
            # named after Numset_shift by all five numeric set types) out of
            # `shared_wrappers`, so its signatures went unfiltered.
            for w in m2d.get(f["name"]) or ():
                claimed.setdefault(w, []).append(f["name"])
        shared_wrappers = {w for w, names in claimed.items()
                           if len(names) > 1 and len(w2sig.get(w) or ()) > 1}
        require_scopes([n for w in shared_wrappers for n in claimed[w]],
                       scope_facts, scope_bodies, scope_params, declared)
    # Transient map: MEOS function name -> every SQL name it resolves to, for the
    # functions that fan out (a shared wrapper / ever-always pair). This is NOT
    # catalog output — every binding reads only the primary `sqlfn` — it is working
    # data handed to the case-collision lint, which must see every spelling.
    multi = {}
    for f in idl["functions"]:
        wrappers = m2d.get(f["name"])
        # A MEOS function can back several wrappers (the ever/always pair), each
        # carrying its own @sqlfn; collect the (sqlfn, sqlop) pairs across all of
        # them in order, keeping the primary (first) wrapper for back-compat.
        pairs = []
        for w in wrappers or []:
            for entry in d2s.get(w, []):
                if entry not in pairs:
                    pairs.append(entry)
        if not pairs:
            # Fill-only fallback: a direct @sqlfn / @sqlop on the MEOS function
            # itself (no PG wrapper — the PG registration is deferred to a host
            # extension). Never reached when the wrapper chain resolves.
            dsql = direct.get(f["name"])
            if not dsql:
                continue
            sqlfn, sqlop = dsql
            if sqlfn:
                f["sqlfn"] = sqlfn
            if sqlop:
                f["sqlop"] = sqlop
            n += 1
            continue
        f["mdbC"] = wrappers[0]
        f["sqlfn"] = pairs[0][0]
        # The SQL-facing arity (required..total). Lets a generator expose the SQL
        # signature instead of the wider C one: args beyond sqlArity are SQL-optional
        # (DEFAULT), and C params beyond sqlArityMax are C-only out-params.
        # The registration surface is the union over EVERY wrapper the function
        # claims, not the first one's alone. One MEOS function commonly backs a
        # whole SET of wrappers — the ever/always pair (eDwithin + aDwithin over
        # one `ea_dwithin_*`), the shift/scale/shiftScale trio over one
        # `*_shift_scale`, send + asBinary over one `*_as_wkb` — and each wrapper
        # registers its OWN CREATE FUNCTION overloads. Keeping only `wrappers[0]`
        # dropped every sibling wrapper's overloads, so half of each ever/always
        # pair and two thirds of each shift/scale trio were invisible to bindings.
        # It is also what made a COMMUTED wrapper unrepresentable: `NAD_stbox_tgeo`
        # is a second wrapper over the one `nad_tgeo_stbox`, so even a correct
        # `@csqlfn #NAD_tgeo_stbox() #NAD_stbox_tgeo()` could not have carried the
        # argument-swapped overload through.
        # Each wrapper is scope-filtered on its own — a scope answers "which types
        # does this function serve", which is per wrapper — and the union is
        # de-duplicated, since two wrappers may legitimately register the same
        # overload under the same name.
        scoped = False
        sigs, seen = [], set()
        for w in wrappers:
            wsigs = w2sig.get(w)
            if not wsigs:
                continue
            # Only a public claimant of a shared wrapper is filtered: those are the
            # functions a binding projects, and the ones require_scopes has proven a
            # scope for. An internal function is not part of any binding surface.
            if w in shared_wrappers and f.get("api") == "public":
                scope, _ = resolve_scope(f["name"], scope_facts, scope_bodies,
                                         scope_params, declared)
                if scope is not None:
                    wsigs = signatures_for(f["name"], wsigs, scope)
                    scoped = True
            for s in wsigs:
                key = (s["sqlName"], tuple(s["args"]), s["ret"], s["retSet"])
                if key not in seen:
                    seen.add(key)
                    sigs.append(s)
        if sigs:
            f["sqlArity"] = min(s["required"] for s in sigs)
            f["sqlArityMax"] = max(len(s["args"]) for s in sigs)
            # The binding-facing SQL return type (the CREATE FUNCTION `RETURNS` clause).
            # Lets a generator render the concrete SQL subtype for a polymorphic
            # `Temporal *` C return (getX -> tfloat, centroid -> tgeompoint). One wrapper
            # normally has a single return type; record all if overloads disagree.
            rets = {s["ret"] for s in sigs if s["ret"]}
            if len(rets) == 1:
                f["sqlReturnType"] = next(iter(rets))
            elif len(rets) > 1:
                f["sqlReturnTypeAll"] = sorted(rets)
            # The EXACT per-overload SQL signatures — the mechanical registration surface.
            # A binding emits one registration per entry over the concrete arg types, with
            # NO type-scope heuristic (minInstant lands on exactly its {tint,tbigint,tfloat,
            # ttext} overloads), under the entry's own SQL name.
            # One C wrapper very commonly backs a per-type NAME FAMILY: Temporal_to_tinstant
            # is exposed as tintInst/tbigintInst/.../ttextInst — one CREATE FUNCTION per base
            # type, all resolving to the one wrapper — and likewise the constructor, I/O
            # (From{Binary,HexWKB,MFJSON}, _in/_out/_recv/_send) and transform families. The
            # single @sqlfn doxygen tag names only ONE representative, so keep EVERY overload
            # and stamp it with `sqlName` when the wrapper backs more than one distinct name;
            # a binding registers each `<T>Inst`/`<T>Seq`/... by its own name rather than
            # hand-writing the non-representative types. Single-name wrappers stay {args,ret}
            # unchanged (their name is the function's `sqlfn`), so the common case is a no-op.
            # `argDefaults` (the per-arg literal SQL default, None for a required arg) is
            # attached ONLY for a signature that actually has an optional arg, so a binding
            # can render the shorter overload of a SQL-optional argument with its omitted
            # value; default-free signatures stay {args, ret} unchanged.
            # Scope filtering already reduced `sigs` to the overloads this function
            # serves, and a per-type function's own overload carries its OWN SQL name
            # (`bigintset_in`), not the representative the @sqlfn tag names
            # (`intset_in`). Dropping a name that differs from `sqlfn` would discard
            # exactly the signature the filter just proved belongs here, so a filtered
            # function keeps all of them and stamps the name whenever it differs.
            fam_names = {s["sqlName"] for s in sigs}
            multiname = len(fam_names) > 1 or (scoped and fam_names != {f["sqlfn"]})
            # The CREATE FUNCTION statements are what the extension deploys, so when
            # an unshared wrapper's signatures all carry ONE SQL name, that name is
            # this function's SQL surface whatever its tag spells. A tag names the
            # family rather than the function for the five topological bounding-box
            # backings — `@sqlfn contains_bbox` over a wrapper the extension exposes
            # as `contains`, the suffix separating `contains` from `contains_rid` in
            # the tag namespace — and trails the surface for a name still awaiting a
            # rename. Comparing against the tag alone drops every signature in both
            # cases, leaving the arity of a surface the catalog then cannot state.
            surface = f["sqlfn"]
            if surface not in fam_names and len(fam_names) == 1:
                surface = next(iter(fam_names))
            own = []
            for s in sigs:
                if not multiname and s["sqlName"] != surface:
                    continue
                entry = {"args": s["args"], "ret": s["ret"]}
                if s["retSet"]:
                    entry["retSet"] = True
                if s["columns"]:
                    entry["columns"] = [{"name": c, "type": t} for c, t in s["columns"]]
                if any(d is not None for d in s["argDefaults"]):
                    entry["argDefaults"] = s["argDefaults"]
                if multiname or s["sqlName"] != f["sqlfn"]:
                    entry["sqlName"] = s["sqlName"]
                own.append(entry)
            if own:
                f["sqlSignatures"] = own
        if pairs[0][1]:
            f["sqlop"] = pairs[0][1]
        # A shared wrapper / ever-always pair exposes >1 SQL name for this one MEOS
        # function. That fan-out is transient lint input, not catalog output (every
        # binding reads only the primary `sqlfn`), so collect it here and never write
        # it to the catalog — the singular `sqlfn` is the one canonical name per entry.
        if len(pairs) > 1:
            multi[f["name"]] = [s for s, _ in pairs]
        n += 1
    return idl, n, multi


def attach_aggfn_map(idl, meos_src):
    """Attach `sqlAggRole` — the aggregate-ROLE name(s) each aggregate function
    implements, read faithfully from @csqlaggfn in meos/src. This gives an
    aggregate member its own catalog identity (setUnionTransition, spanUnionFinal)
    distinct from the identically named binary set/span union FUNCTION, and lets a
    binding reconstruct the standard PostgreSQL aggregate model (a <aggregate> with
    its Transition / Combine / Final members) instead of guessing from name
    suffixes. A member shared by two aggregates (spanset_union_finalfn) carries a
    list. Faithful reader: the name is recorded verbatim, no derivation.

    The ROLE is what this field holds — `setUnionTransition` is the transition
    member OF the `setUnion` aggregate. The aggregate itself is `sqlAgg`, named
    the way the SQL surface names one where a scalar shares its spelling
    (merge / mergeAgg, tMin / tMinAgg)."""
    a2n = _meos_agg_names(meos_src)
    n = 0
    for f in idl["functions"]:
        names = a2n.get(f["name"])
        if names:
            f["sqlAggRole"] = names
            n += 1
    return idl, n


def attach_sqlaggfn_map(idl, meos_src, mdb_src):
    """Attach `sqlAgg` — the SQL AGGREGATE(s) a function's PG wrapper serves,
    read from @sqlaggfn in mobilitydb/src over the same @csqlfn chain that
    resolves `sqlfn`.

    `Agg` is how the SQL surface itself names an aggregate where a scalar shares
    its spelling (merge / mergeAgg, tMin / tMinAgg), so it is the word for the
    field that holds one. This is the name a binding registers (`tCount`), and
    it is a different fact from both neighbours it sits beside:

      `sqlfn`       the CREATE FUNCTION the wrapper backs (`tcount_transfn`) —
                    the aggregate's transition member, which no user calls;
      `sqlAggRole`  the aggregate-ROLE name from MEOS's @csqlaggfn
                    (`setUnionTransition`), naming the member WITHIN its
                    aggregate rather than the aggregate.

    Keeping them apart is what lets a binding tell an aggregate from a function
    without reading the C symbol's suffix, and it is why `sqlfn` can state the
    function it actually backs. Faithful reader: recorded verbatim, no
    derivation, and absent for every function whose wrapper carries no tag."""
    m2d = _meos_to_mdb(meos_src)
    d2a = _mdb_to_agg(mdb_src)
    n = 0
    for f in idl["functions"]:
        names = []
        for w in m2d.get(f["name"]) or ():
            for a in d2a.get(w) or ():
                if a not in names:
                    names.append(a)
        if names:
            f["sqlAgg"] = names
            n += 1
    return idl, n


# MEOS-C ever/always spatial-relationship functions are named <e|a><verb>_...; their
# @csqlfn must point at the matching <E|A><verb>_... wrapper. A copy-paste @csqlfn in
# meos/src (e.g. eintersects_tgeo_geo tagged #Aintersects_tgeo_geo) silently flips the
# resolved @sqlfn from eX to aX — which then drops the real overload from the eX dispatch
# group and lets a wrong subtype backing be reached (a runtime "must be of type ..." error
# in the bindings). The parser is faithful, so guard the SOURCE here: flag any function
# whose name e/a prefix disagrees with its resolved @sqlfn e/a prefix.
_EA_FAMILY = re.compile(
    r"^(e|a)(intersects|disjoint|contains|contained|covers|coveredby|touches|"
    r"dwithin|within|equals|crosses|overlaps)_")


def lint_ea_sqlfn(idl):
    """Return [(meos_c_name, sqlfn)] where the function's ever/always (e/a) name prefix
    contradicts its resolved @sqlfn — a source @csqlfn mistag in meos/src."""
    bad = []
    for f in idl["functions"]:
        sf = f.get("sqlfn")
        m = _EA_FAMILY.match(f["name"])
        if sf and m and re.match(r"^[ea][A-Z]", sf) and sf[0] != m.group(1):
            bad.append((f["name"], sf))
    return bad


# Relative-position MEOS-C functions are named <op>_...; their @csqlfn must point at
# the <Op>_... wrapper carrying the matching @sqlfn. The same class of copy-paste as
# lint_ea_sqlfn bites the time axis: a 1-D span reuses ONE value wrapper for both its
# value axis (left/right) and its time axis (before/after), so a time function tagged
# `@csqlfn #Left_span_value()` resolves to the value name `left` and the binding emits
# `left(tstzspan,...)` instead of `before(...)`. The function-name prefix is the SoT.
_POSITIONAL_OPS = {
    "left", "right", "overleft", "overright",
    "before", "after", "overbefore", "overafter",
    "below", "above", "overbelow", "overabove",
    "front", "back", "overfront", "overback",
}
_POSITIONAL_NAME = re.compile(
    r"^(" + "|".join(sorted(_POSITIONAL_OPS, key=len, reverse=True)) + r")_")


def lint_positional_sqlfn(idl):
    """Return [(meos_c_name, sqlfn)] where a relative-position function's name prefix
    (before_/left_/...) contradicts its resolved @sqlfn — a source @csqlfn mistag that
    mis-names one axis of a shared value/time position wrapper."""
    bad = []
    for f in idl["functions"]:
        sf = f.get("sqlfn")
        m = _POSITIONAL_NAME.match(f["name"])
        if sf and m and sf in _POSITIONAL_OPS and sf != m.group(1):
            bad.append((f["name"], sf))
    return bad


# A MEOS-C function's name ends in the container it takes — `_tstzset` a timestamptz
# set, `_tstzspanset` a span set — and its @csqlfn must name a wrapper over that same
# container. A copy-paste from the neighbouring block names the sibling container's
# wrapper instead, and nothing catches it: the wrapper exists, it is reachable, and its
# arity matches, so the catalog silently carries the SQL surface of the wrong overload
# (trgeometry_at_tstzset answering atTime(trgeometry, tstzspanset), leaving the
# timestamptz-set overload named by nothing).
# A CONCRETE function name over a GENERIC wrapper is the norm rather than a mistag —
# `adjacent_span_timestamptz` names `Adjacent_span_value` because one wrapper serves
# every base type — so both sides are read as a container FAMILY and only a
# disagreement between families is reported. That is what separates the seven real
# mistags from the forty names whose suffixes merely differ.
_CONTAINER_FAMILY = {
    "tstzspanset": "spanset", "tstzspan": "span", "tstzset": "set",
    "timestamptz": "value", "spanset": "spanset", "span": "span",
    "set": "set", "value": "value",
}
_CONTAINER_SUFFIX = tuple(sorted(_CONTAINER_FAMILY, key=len, reverse=True))


def _container_family(name):
    """The container family the name ends in, or None when it names no container."""
    low = name.lower()
    for suf in _CONTAINER_SUFFIX:
        if low.endswith("_" + suf):
            return _CONTAINER_FAMILY[suf]
    return None


def lint_container_family_csqlfn(idl):
    """Return [(meos_c_name, wrapper)] where the container family the function's name
    ends in contradicts the family its resolved wrapper ends in — a source @csqlfn
    mistag naming the sibling container's wrapper."""
    bad = []
    for f in idl["functions"]:
        wrapper = f.get("mdbC")
        if not wrapper:
            continue
        fam_fn, fam_wrapper = _container_family(f["name"]), _container_family(wrapper)
        if fam_fn and fam_wrapper and fam_fn != fam_wrapper:
            bad.append((f["name"], wrapper))
    return bad


def lint_sqlfn_case_collisions(idl, multi=None):
    """Return [(lower, [spelling, ...])] for @sqlfn names that collide
    case-insensitively but differ in case (e.g. tDistance vs tdistance).

    PostgreSQL folds unquoted identifiers to lower case, so the two spell the
    SAME SQL function and the clash is invisible in SQL / pg_regress. But the
    binding name is taken case-SENSITIVELY, and case-insensitive engines (Spark
    SQL, …) register every spelling under one UDF — so one silently shadows the
    other. A canonical binding name must have exactly ONE spelling; surface a
    casing straggler here before it reaches a binding.

    `multi` (from attach_sqlfn_map) maps a fan-out function to every SQL name it
    resolves to, so a straggler that appears only as a secondary name is still
    caught even though the catalog now stores only the primary `sqlfn`."""
    multi = multi or {}
    by_lower = {}
    for f in idl["functions"]:
        for sf in [f.get("sqlfn"), *multi.get(f["name"], [])]:
            if sf:
                by_lower.setdefault(sf.lower(), set()).add(sf)
    return sorted((lo, sorted(sp)) for lo, sp in by_lower.items() if len(sp) > 1)
