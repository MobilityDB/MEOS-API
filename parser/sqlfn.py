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
import re
from pathlib import Path

# A @csqlfn tag carries one OR MORE #Wrapper() references — comma- or
# space-separated, and possibly continued across doxygen lines — because a single
# MEOS function can back several wrappers (the ever/always pair eDisjoint/aDisjoint
# share one ea_* function; the shift/scale/shift_scale trio share one C function).
# The tag value runs from @csqlfn up to the next doxygen tag or the comment close.
_CSQLFN = re.compile(r"@csqlfn\b")
_CSQLFN_REF = re.compile(r"#(\w+)\s*\(\)")
_CSQLFN_END = re.compile(r"@\w|\*/")
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
_SQLOP = re.compile(r"@sqlop\s+@p\s+(\S+)")
_DATUM = re.compile(r"Datum\s+(\w+)\s*\(\s*PG_FUNCTION_ARGS")
# `CREATE [OR REPLACE] FUNCTION name(` — the SQL-facing signature; the wrapper it
# binds is in the trailing `AS 'MODULE_PATHNAME', '<Wrapper>'`.
_CREATE_FN = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(\w+)\s*\(", re.I)
_AS_WRAPPER = re.compile(r"AS\s+'[^']*'\s*,\s*'(\w+)'", re.I)


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


def _create_fn_stmts(text):
    """Yield (sqlName, [raw arg decls], returnType|None, wrapper|None) for every
    CREATE FUNCTION in `text`, each parsed STATEMENT-BOUNDED (to its terminating `;`).
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
        rm = re.match(r"\s*RETURNS\s+(?:SETOF\s+)?(.+?)\s+AS\b", tail, re.I | re.S)
        ret = " ".join(rm.group(1).split()) if rm else None
        argdecls = [a for a in _split_top_commas(text[start:arg_close]) if a.strip()]
        yield sqlname, argdecls, ret, wrapper


def _wrapper_sql_sigs(sql_src):
    """MobilityDB-C wrapper name -> list of per-overload SQL signatures
    {sqlName, args:[type,...], required, ret}, straight from the CREATE FUNCTION
    statements. The .in.sql CREATE FUNCTION set IS the exact SQL registration surface,
    so a binding emits ONE registration per signature over the concrete arg types with
    NO type-scope heuristic — e.g. `minInstant` lands on exactly its four overloads
    {tint,tbigint,tfloat,ttext}, never over tbool or the geo types. `required` counts the
    non-DEFAULT args (args beyond it are SQL-optional); `ret` is the concrete SQL subtype
    the polymorphic `Temporal *` C return loses. Two passes: gather the type vocabulary
    from the unambiguous positions, then resolve every arg's type against it."""
    out = {}
    sql_src = Path(sql_src)
    if not sql_src.exists():
        return out
    stmts, vocab = [], set()
    for sf in sorted(sql_src.rglob("*.sql")):
        text = sf.read_text(errors="ignore")
        for sqlname, argdecls, ret, wrapper in _create_fn_stmts(text):
            stmts.append((sqlname, argdecls, ret, wrapper))
            if ret:
                vocab.add(ret)                                  # a RETURNS clause is always a type
            for a in argdecls:
                bt = _bare_type(a)
                if bt and " " not in bt:
                    vocab.add(bt)                               # a single-token arg is always a type
    for sqlname, argdecls, ret, wrapper in stmts:
        if wrapper is None:
            continue                                            # LANGUAGE SQL / $$ body — no C symbol
        args = [_arg_type(a, vocab) for a in argdecls]
        arg_defaults = [_arg_default(a) for a in argdecls]
        required = sum(1 for a in argdecls if not re.search(r"\bDEFAULT\b", a, re.I))
        out.setdefault(wrapper, []).append(
            {"sqlName": sqlname, "args": args, "required": required,
             "argDefaults": arg_defaults, "ret": ret})
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


def attach_sqlfn_map(idl, meos_src, mdb_src, sql_src=None):
    m2d = _meos_to_mdb(meos_src)
    d2s = _mdb_to_sql(mdb_src)
    w2sig = _wrapper_sql_sigs(sql_src) if sql_src else {}
    n = 0
    # Transient map: MEOS function name -> every SQL name it resolves to, for the
    # functions that fan out (a shared wrapper / ever-always pair). This is NOT
    # catalog output — every binding reads only the primary `sqlfn` — it is working
    # data handed to the case-collision lint, which must see every spelling.
    multi = {}
    for f in idl["functions"]:
        wrappers = m2d.get(f["name"])
        if not wrappers:
            continue
        # A MEOS function can back several wrappers (the ever/always pair), each
        # carrying its own @sqlfn; collect the (sqlfn, sqlop) pairs across all of
        # them in order, keeping the primary (first) wrapper for back-compat.
        pairs = []
        for w in wrappers:
            for entry in d2s.get(w, []):
                if entry not in pairs:
                    pairs.append(entry)
        if not pairs:
            continue
        f["mdbC"] = wrappers[0]
        f["sqlfn"] = pairs[0][0]
        # The SQL-facing arity (required..total). Lets a generator expose the SQL
        # signature instead of the wider C one: args beyond sqlArity are SQL-optional
        # (DEFAULT), and C params beyond sqlArityMax are C-only out-params.
        sigs = w2sig.get(wrappers[0])
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
            # The EXACT per-overload SQL signatures for THIS @sqlfn name — the mechanical
            # registration surface. A binding emits one registration per entry over the
            # concrete arg types, with NO type-scope heuristic (minInstant lands on exactly
            # its {tint,tbigint,tfloat,ttext} overloads). One wrapper backs several @sqlfn
            # names (Temporal_to_tinstant <- tintInst/tgeometryInst/...), so keep only the
            # overloads whose CREATE FUNCTION name is this function's @sqlfn.
            # `argDefaults` (the per-arg literal SQL default, None for a required arg) is
            # attached ONLY for a signature that actually has an optional arg, so a binding
            # can render the shorter overload of a SQL-optional argument with its omitted
            # value; default-free signatures stay {args, ret} unchanged.
            own = []
            for s in sigs:
                if s["sqlName"] != f["sqlfn"]:
                    continue
                entry = {"args": s["args"], "ret": s["ret"]}
                if any(d is not None for d in s["argDefaults"]):
                    entry["argDefaults"] = s["argDefaults"]
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
