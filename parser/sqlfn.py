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

from parser.typescope import (TypeFacts, declared_scopes, read_bodies,
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
        if ret:
            # PostgreSQL lets the function attributes come in any order, so an
            # attribute may sit between RETURNS and AS rather than after the body.
            # MobilityDB writes SUPPORT after `AS 'MODULE_PATHNAME'` everywhere but
            # `aTouches(tcbuffer, cbuffer)`, which puts it first and so parsed as the
            # return type `boolean SUPPORT tspatial_supportfn`. Keep only the type.
            ret = _RET_ATTR.split(ret, maxsplit=1)[0].strip() or ret
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
                key = (s["sqlName"], tuple(s["args"]), s["ret"])
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
            own = []
            for s in sigs:
                if not multiname and s["sqlName"] != f["sqlfn"]:
                    continue
                entry = {"args": s["args"], "ret": s["ret"]}
                if any(d is not None for d in s["argDefaults"]):
                    entry["argDefaults"] = s["argDefaults"]
                if multiname:
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
    """Attach `sqlAgg` — the SQL aggregate-role name(s) each aggregate function
    implements, read faithfully from @csqlaggfn in meos/src. This gives an
    aggregate member its own catalog identity (setUnionTransition, spanUnionFinal)
    distinct from the identically named binary set/span union FUNCTION, and lets a
    binding reconstruct the standard PostgreSQL aggregate model (a <aggregate> with
    its Transition / Combine / Final members) instead of guessing from name
    suffixes. A member shared by two aggregates (spanset_union_finalfn) carries a
    list. Faithful reader: the name is recorded verbatim, no derivation."""
    a2n = _meos_agg_names(meos_src)
    n = 0
    for f in idl["functions"]:
        names = a2n.get(f["name"])
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
