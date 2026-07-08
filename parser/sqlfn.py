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


def _wrapper_sql_sigs(sql_src):
    """MobilityDB-C wrapper name -> (sqlArity, sqlArityMax, {sqlReturnType, ...}) from the
    CREATE FUNCTION definitions. The SQL signature is the binding-facing arity, NOT the C
    one: an arg with a DEFAULT clause is optional, and any trailing C param absent from the
    SQL form is a C-only out-param (e.g. the `size_t *` of the *_as_hexwkb family). So a
    generator binding the @sqlfn name must expose sqlArity..sqlArityMax args, NOT the full C
    list. The RETURNS type is likewise binding-facing: the C signature loses the concrete SQL
    subtype for polymorphic `Temporal *` returns. Across overloads of one wrapper, take
    min-required / max-total arity and the UNION of return types."""
    out = {}
    sql_src = Path(sql_src)
    if not sql_src.exists():
        return out
    for sf in sorted(sql_src.rglob("*.sql")):
        text = sf.read_text(errors="ignore")
        for m in _CREATE_FN.finditer(text):
            i, depth, start = m.end(), 1, m.end()
            while i < len(text) and depth:
                depth += (text[i] == "(") - (text[i] == ")")
                i += 1
            wm = _AS_WRAPPER.search(text, i, i + 400)
            if not wm:
                continue  # SQL-language ($$...$$) wrapper has no C symbol — skip
            wrapper = wm.group(1)
            args = [a for a in _split_top_commas(text[start:i - 1]) if a.strip()]
            required = sum(1 for a in args if not re.search(r"\bDEFAULT\b", a, re.I))
            # The SQL return type sits between the arg-list close `)` and the `AS`
            # clause (`) RETURNS <type> AS '...','<Wrapper>'`). It is the binding-facing
            # SQL subtype, which the C signature does NOT carry for the polymorphic
            # `Temporal *`-returning functions (getX -> tfloat, centroid -> tgeompoint):
            # the C return is a bare `Temporal *`, so without this a generator cannot
            # render the concrete SQL result type. The CREATE FUNCTION is the SoT.
            rt = None
            rm = re.match(r"RETURNS\s+(?:SETOF\s+)?(.+)$",
                          text[i:wm.start()].strip(), re.I | re.S)
            if rm:
                rt = " ".join(rm.group(1).split())
            prev = out.get(wrapper)
            if prev:
                rets = prev[2] | ({rt} if rt else set())
                out[wrapper] = (min(prev[0], required), max(prev[1], len(args)), rets)
            else:
                out[wrapper] = (required, len(args), {rt} if rt else set())
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
        sig = w2sig.get(wrappers[0])
        if sig:
            f["sqlArity"], f["sqlArityMax"], rets = sig
            # The binding-facing SQL return type (the CREATE FUNCTION `RETURNS` clause).
            # Lets a generator render the concrete SQL subtype for a polymorphic
            # `Temporal *` C return (getX -> tfloat, centroid -> tgeompoint). One wrapper
            # normally has a single return type; record all if overloads disagree.
            if len(rets) == 1:
                f["sqlReturnType"] = next(iter(rets))
            elif len(rets) > 1:
                f["sqlReturnTypeAll"] = sorted(rets)
        if pairs[0][1]:
            f["sqlop"] = pairs[0][1]
        # Shared wrapper OR ever/always pair exposing >1 SQL name: record them all.
        if len(pairs) > 1:
            f["sqlfnAll"] = [s for s, _ in pairs]
        if len(wrappers) > 1:
            f["mdbCAll"] = wrappers
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


def lint_sqlfn_case_collisions(idl):
    """Return [(lower, [spelling, ...])] for @sqlfn names that collide
    case-insensitively but differ in case (e.g. tDistance vs tdistance).

    PostgreSQL folds unquoted identifiers to lower case, so the two spell the
    SAME SQL function and the clash is invisible in SQL / pg_regress. But the
    binding name is taken case-SENSITIVELY, and case-insensitive engines (Spark
    SQL, …) register every spelling under one UDF — so one silently shadows the
    other. A canonical binding name must have exactly ONE spelling; surface a
    casing straggler here before it reaches a binding."""
    by_lower = {}
    for f in idl["functions"]:
        for sf in [f.get("sqlfn"), *f.get("sqlfnAll", [])]:
            if sf:
                by_lower.setdefault(sf.lower(), set()).add(sf)
    return sorted((lo, sorted(sp)) for lo, sp in by_lower.items() if len(sp) > 1)
