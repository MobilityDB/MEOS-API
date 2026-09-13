"""Extract per-parameter BOUND LITERALS from the MobilityDB PostgreSQL wrappers.

A MEOS function's C signature can be WIDER than the SQL surface a binding
exposes: the MobilityDB PG wrapper reads only some arguments from the caller
(via ``PG_GETARG_*``) and BINDS the remaining scalar inputs to fixed literals.
``valueAtTimestamp(temp, t)`` is 2-arg in SQL, yet the wrapper
``Temporal_value_at_timestamptz`` calls
``temporal_value_at_timestamptz(temp, t, true, &result)`` — ``strict`` is bound
to ``true`` and ``result`` is an out-param.

``shape.outParams`` (``parser/outparam.py``) already folds the trailing
out-param.  This pass captures the OTHER hidden inputs — the bound literals — as
``shape.boundArgs`` = ``{param_name: literal}``, the exact sibling of
``shape.outParams``.  A downstream binding generator, which exposes only the SQL
args, emits the bound literal for each such param
(``fn(temp, t, /*strict*/true, &out)``) instead of hand-writing it.

The wrapper C body is the single source of truth.  For each positional argument
of the ``<meos_fn>(...)`` call inside the wrapper: an argument sourced from
``PG_GETARG_*`` (directly, or via a local so-assigned) is a CALLER arg and is
skipped; ``&name`` is an out-param (already in ``outParams``) and is skipped;
only a genuine LITERAL (``true``/``false``, a number, ``NULL`` or an UPPERCASE
enum/macro) is recorded.

A wrapper that does not call the MEOS function by name delegates to a shared
helper, and the literal it binds sits at the DELEGATION rather than at the MEOS
call: ``Tjsonb_object_field`` is ``return Tjsonb_object_field_common(fcinfo,
false)`` and the helper calls ``tjsonb_object_field(temp, key, astext,
null_handle)``.  Such a wrapper is followed one hop: the literals it passes are
matched to the helper's parameters by position, and an argument of the MEOS call
naming one of those parameters resolves to the literal behind it.  Two wrappers
sharing a helper (``…_object_field`` and ``…_object_field_text``) bind the same
parameter to different literals, so the pair is read as the one SQL surface each
wrapper names rather than merged.

A wrapper can also supply the value of an argument the SQL signature omits:
``Tspatial_as_text_common`` starts ``dbl_dig_for_wkt`` from
``OUT_DEFAULT_DECIMAL_DIGITS`` and reads argument 1 only under
``if (PG_NARGS() > 1 && ! PG_ARGISNULL(1))``, so ``asEWKT(th3index)`` calls
``tspatial_as_ewkt(temp, OUT_DEFAULT_DECIMAL_DIGITS)``.  Such a local is recorded on
each SQL signature stating at most ``k`` arguments, where it is the literal the MEOS
call reads; a signature stating argument ``k`` passes the caller's value.

A wrapper can reach the functions its ``@csqlfn`` tags name through an internal generic
that none of them is: ``Numset_shift`` calls ``numset_shift_scale(s, shift, 0, true,
false)``, while the tag names ``intset_shift_scale``, ``floatset_shift_scale`` and their
siblings, each taking the generic's parameters under the same names in the same order.
When a wrapper calls no member of its group and delegates to no helper, the MEOS
definition of each function it calls is read, and a function whose parameter names equal
a member's is the generic that member wraps: its literals bind to the member by parameter
name.
"""
from __future__ import annotations

import re
from pathlib import Path

# `Datum <Name>(PG_FUNCTION_ARGS) {` opens a PG wrapper.
_WRAP = re.compile(r"Datum\s+(?P<name>\w+)\s*\(\s*PG_FUNCTION_ARGS\s*\)\s*\{")
# Any local the wrapper ASSIGNS (`var = ...`, excluding `==`): every value the
# wrapper feeds the MEOS call is either read from the caller (`PG_GETARG_*`) or
# derived into such a local (e.g. `char *hexwkb = text2cstring(PG_GETARG_TEXT_P(0))`),
# so an argument that names an assigned local is caller-sourced, never a literal.
_ASSIGNED = re.compile(r"\b(?P<var>\w+)\s*=(?!=)")
# Literals worth binding (checked in order): boolean, number, UPPERCASE enum/macro.
_TRUE = re.compile(r"^(?:true|TRUE)$")
_FALSE = re.compile(r"^(?:false|FALSE)$")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_ENUM = re.compile(r"^[A-Z][A-Z0-9_]+$")
_IDENT = re.compile(r"^\w+$")
# A shared helper takes the call info plus the parameters the wrappers bind.
_HELPER = re.compile(r"Datum\s+(?P<name>\w+)\s*\(\s*FunctionCallInfo\s+\w+"
                     r"(?P<rest>[^)]*)\)\s*\{")
# ... and a wrapper delegates to it by passing that same call info straight through.
_DELEG = re.compile(r"\b(?P<name>\w+)\s*\(\s*fcinfo\s*(?P<args>,[^;]*?)?\)\s*;")


def _body(text: str, brace_pos: int) -> str:
    """Return the brace-balanced body starting at ``brace_pos`` (the ``{`` index)."""
    depth = 0
    for i in range(brace_pos, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[brace_pos + 1:i]
    return text[brace_pos + 1:]


def _split_args(s: str) -> list[str]:
    """Split a call's argument list on top-level commas (paren/bracket aware)."""
    args: list[str] = []
    depth = 0
    cur: list[str] = []
    for c in s:
        if c in "([":
            depth += 1
            cur.append(c)
        elif c in ")]":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    tail = "".join(cur).strip()
    if tail:
        args.append(tail)
    return args


def _call_args(body: str, fn: str) -> list[str] | None:
    """Return the positional args of the first ``fn(...)`` call in ``body``, else None.

    Word-boundary anchored so ``temporal_before_timestamptz`` does not match the
    RGEO arm ``trgeometry_before_timestamptz``; case-sensitive so it does not
    match the (Uppercase) wrapper name."""
    m = re.search(r"(?<!\w)" + re.escape(fn) + r"\s*\(", body)
    if not m:
        return None
    start = m.end() - 1  # the '('
    depth = 0
    for i in range(start, len(body)):
        c = body[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return _split_args(body[start + 1:i])
    return None


def extract_wrappers(mdb_src: str | Path) -> dict[str, str]:
    """Return ``{wrapper_name: body_text}`` for every PG wrapper under ``mdb_src``."""
    out: dict[str, str] = {}
    for cf in Path(mdb_src).rglob("*.c"):
        text = cf.read_text(errors="ignore")
        for m in _WRAP.finditer(text):
            out[m.group("name")] = _body(text, m.end() - 1)
    return out


def extract_helpers(mdb_src: str | Path) -> dict[str, tuple[str, list[str]]]:
    """``{helper_name: (body_text, [param names after the call info])}`` for every shared
    wrapper helper under ``mdb_src``."""
    out: dict[str, tuple[str, list[str]]] = {}
    for cf in Path(mdb_src).rglob("*.c"):
        text = cf.read_text(errors="ignore")
        for m in _HELPER.finditer(text):
            rest = (m.group("rest") or "").strip()
            names: list[str] = []
            if rest.startswith(","):
                for decl in _split_args(rest[1:]):
                    tok = decl.replace("*", " ").split()
                    if tok:
                        names.append(tok[-1])
            out[m.group("name")] = (_body(text, m.end() - 1), names)
    return out


def _delegated(body: str, helpers: dict[str, tuple[str, list[str]]]):
    """``(helper_body, {helper_param: literal})`` when ``body`` delegates to a shared
    helper, passing the call info through and binding the rest to literals."""
    for m in _DELEG.finditer(body):
        entry = helpers.get(m.group("name"))
        if entry is None:
            continue
        hbody, hparams = entry
        raw = (m.group("args") or "").strip()
        vals = _split_args(raw[1:]) if raw.startswith(",") else []
        subst = {}
        for pname, val in zip(hparams, vals):
            lit = _literal(val)
            if lit is not None:
                subst[pname] = lit
        if subst:
            return hbody, subst
    return None, {}


# A call to a lowercase C function, the form every MEOS function takes.
_CALLEE = re.compile(r"\b(?P<name>[a-z][a-z0-9_]*)\s*\(")


def _param_name(decl: str) -> str | None:
    """The name a C parameter declaration introduces: ``const Set *s`` gives ``s``,
    ``void (*fn)(void *)`` gives ``fn``; ``void`` and ``...`` introduce none."""
    decl = decl.strip()
    if not decl or decl in ("void", "..."):
        return None
    fp = re.search(r"\(\s*\*\s*(\w+)\s*\)", decl)
    if fp:
        return fp.group(1)
    ids = re.findall(r"[A-Za-z_]\w*", decl.split("[")[0])
    return ids[-1] if ids else None


def extract_param_lists(meos_src: str | Path) -> dict[str, list[str]]:
    """``{function: [parameter names, in order]}`` for every documented MEOS definition
    under ``meos_src``, read with the definition pattern ``parser.outparam`` scans."""
    from parser.outparam import _FUNC
    out: dict[str, list[str]] = {}
    for f in sorted(Path(meos_src).rglob("*.c")):
        for m in _FUNC.finditer(f.read_text(errors="ignore")):
            names = [_param_name(p) for p in _split_args(m.group("params"))]
            if all(names):
                out.setdefault(m.group("name"), names)
    return out


def _literal(arg: str) -> str | None:
    """Normalise a call argument to the literal to record, or None if not a literal."""
    if _TRUE.match(arg):
        return "true"
    if _FALSE.match(arg):
        return "false"
    if arg == "NULL" or _NUMBER.match(arg) or _ENUM.match(arg):
        return arg
    return None


def _guards(body: str) -> list[tuple[int, int, int]]:
    """``(k, start, end)`` for every statement ``body`` runs only under
    ``if (PG_NARGS() > k ...)``: the span of the brace block or of the single statement
    the test guards."""
    out: list[tuple[int, int, int]] = []
    for m in re.finditer(r"\bif\s*\(", body):
        depth, i = 0, m.end() - 1
        for i in range(m.end() - 1, len(body)):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    break
        g = re.match(r"\s*PG_NARGS\s*\(\s*\)\s*>\s*(\d+)\s*(?:&&|$)",
                     body[m.end():i])
        if not g:
            continue
        j = i + 1
        while j < len(body) and body[j].isspace():
            j += 1
        if body.startswith("{", j):
            end = j + len(_body(body, j)) + 2
        else:
            end = body.find(";", j) + 1
        out.append((int(g.group(1)), j, end))
    return out


def _guarded_default(body: str, var: str) -> tuple[int, str] | None:
    """``(k, literal)`` when local ``var`` starts from a literal and every later assignment
    of it sits under ``if (PG_NARGS() > k ...)``.  A SQL signature omitting argument ``k``
    never runs those assignments, so the MEOS call reads the literal, as a SQL DEFAULT
    would supply it: ``Tspatial_as_text_common`` starts ``dbl_dig_for_wkt`` from
    ``OUT_DEFAULT_DECIMAL_DIGITS`` and reads argument 1 only when the call carries it."""
    v = re.escape(var)
    if re.search(r"&\s*" + v + r"\b|(?<![\w.>])" + v +
                 r"\s*(?:\+\+|--|(?:[-+*/%&|^]|<<|>>)=)|(?:\+\+|--)\s*" + v + r"\b",
                 body):
        return None
    assigns = list(re.finditer(r"(?<![\w.>])" + v + r"\s*=(?!=)", body))
    if len(assigns) < 2:
        return None
    guards = _guards(body)
    init = re.match(r"\s*([^;]+?)\s*;", body[assigns[0].end():])
    lit = _literal(init.group(1)) if init else None
    if lit is None or any(s <= assigns[0].start() < e for _, s, e in guards):
        return None
    ks = set()
    for a in assigns[1:]:
        hit = [k for k, s, e in guards if s <= a.start() < e]
        if not hit:
            return None
        ks.update(hit)
    return (ks.pop(), lit) if len(ks) == 1 else None


def _wrapper_bound(body: str, func: dict, drift: list,
                   documented: dict[str, set],
                   subst: dict[str, str] | None = None,
                   guarded: dict[str, tuple[int, str]] | None = None) -> dict[str, str]:
    """The literals wrapper ``body`` binds in its call to ``func['name']``, keyed by
    ``func``'s parameter name. Empty if the wrapper does not call ``func`` by name.

    A local the wrapper reads from argument ``k`` only when the call carries it
    (``_guarded_default``) is caller-sourced for a signature stating ``k`` and a literal
    for one omitting it; it goes into ``guarded`` as ``{param: (k, literal)}``.

    ``documented`` maps a MEOS function to the set of its ``@param``-documented parameter
    names (``parser.outparam.extract_param_names``). A bare-identifier argument bound to a
    documented parameter is a value the wrapper reads from the caller or derives — an
    array length (``@param[in] count``), an aggregate state (``@param[in,out] state``) —
    never a hard-coded literal, so it is skipped systematically. Only a bare identifier
    for an UNDOCUMENTED parameter is reported as drift (the exceptional manual gap)."""
    args = _call_args(body, func["name"])
    if not args:
        return {}
    subst = subst or {}
    assigned = {m.group("var") for m in _ASSIGNED.finditer(body)}
    doc_params = documented.get(func["name"], frozenset())
    params = func.get("params", [])
    bound: dict[str, str] = {}
    for i, a in enumerate(args):
        if i >= len(params):
            break
        pname = params[i].get("name")
        if not pname:
            continue
        if a in subst:
            # a helper parameter the delegating wrapper bound to a literal
            bound[pname] = subst[a]
            continue
        if a in assigned and _IDENT.match(a) and guarded is not None:
            dflt = _guarded_default(body, a)
            if dflt is not None:
                guarded.setdefault(pname, dflt)
                continue
        if a.startswith("&") or "PG_GETARG" in a or a in assigned:
            continue  # out-param or caller-sourced local
        lit = _literal(a)
        if lit is not None:
            bound[pname] = lit
        elif _IDENT.match(a) and pname not in doc_params:
            # a bare identifier that is not caller-sourced, not a literal, and not a
            # documented @param (caller-read / derived value) — report for a look.
            drift.append((func["name"], pname, "unclassified-arg: " + a))
    return bound


def _group_bound(body: str, group: list, helpers: dict, drift: list,
                 documented: dict[str, set], generics: dict | None = None):
    """``(bound, guarded)``: the literals wrapper ``body`` binds, keyed by parameter name,
    and the ``{param: (k, literal)}`` it supplies when the call omits argument ``k``, read
    from its call to whichever member of ``group`` it names (branches such as the RGEO
    ternary agree, and the first wins), or from its delegation to a shared helper when it
    names none, or from its call to the generic the members wrap when it does neither --
    a function none of them is whose parameter names, in ``generics``, equal a member's."""
    bound: dict[str, str] = {}
    guarded: dict[str, tuple[int, str]] = {}
    for func in group:
        for k, v in _wrapper_bound(body, func, drift, documented,
                                   guarded=guarded).items():
            bound.setdefault(k, v)
    if bound or guarded:
        return bound, guarded
    # The wrapper names no MEOS call of its own: it delegates, and the literal it binds
    # sits at that delegation.
    hbody, subst = _delegated(body, helpers)
    if hbody is not None:
        for func in group:
            for k, v in _wrapper_bound(hbody, func, drift, documented, subst,
                                       guarded).items():
                bound.setdefault(k, v)
    if bound or guarded or not generics:
        return bound, guarded
    # The wrapper calls a generic its typed members wrap: a function none of them is,
    # taking a member's parameters under the same names in the same order.
    members = {tuple(p.get("name") for p in f.get("params", [])) for f in group}
    names = {f["name"] for f in group}
    for m in _CALLEE.finditer(body):
        callee = m.group("name")
        plist = generics.get(callee)
        if callee in names or not plist or tuple(plist) not in members:
            continue
        twin = {"name": callee, "params": [{"name": n} for n in plist]}
        for k, v in _wrapper_bound(body, twin, drift, documented,
                                   guarded=guarded).items():
            bound.setdefault(k, v)
        if bound or guarded:
            break
    return bound, guarded


def _signature_wrapper(func: dict, sig: dict, claimed: list, w2sig: dict) -> str | None:
    """The wrapper whose CREATE FUNCTION states ``sig``: the first of ``claimed`` that
    registers it, as ``attach_sqlfn_map`` keeps the first of two wrappers registering
    the same overload. None when no claimed wrapper states it."""
    key = (sig.get("sqlName") or func.get("sqlfn"), tuple(sig.get("args") or ()),
           sig.get("ret"))
    for w in claimed:
        for s in w2sig.get(w) or ():
            if (s["sqlName"], tuple(s["args"]), s["ret"]) == key:
                return w
    return None


def merge_boundargs(idl: dict, mdb_src: str | Path,
                    documented: dict[str, set] | None = None,
                    sql_src: str | Path | None = None,
                    meos_src: str | Path | None = None) -> tuple[dict, int, list]:
    """Fold wrapper-bound literals into ``shape.boundArgs`` or, where a function's
    wrappers disagree, into each SQL signature's ``boundArgs``.

    Every function sharing a PG wrapper has the SAME SQL contract, so a literal the
    wrapper binds (keyed by parameter name) applies to all of them — crucially the
    per-base-type collapse siblings (``tbool``/``tint``/… ``_value_at_timestamptz``) that
    a binding dispatches to for a typed result but that the wrapper never calls by name
    (it calls the generic ``temporal_value_at_timestamptz``). Only members that actually
    own a parameter of that name receive the literal.

    One MEOS function can back several wrappers, one per operand order or per
    ever/always half, and each binds its own literals: ``Concat_jsonb_jsonbset`` passes
    ``invert`` as ``INVERT`` and ``Concat_jsonbset_jsonb`` as ``INVERT_NO``. Given
    ``sql_src`` and ``meos_src``, every wrapper a function's ``@csqlfn`` names is read and
    each SQL signature is traced to the wrapper whose CREATE FUNCTION states it.
    ``shape.boundArgs`` holds the literals when every one of those wrappers binds the
    same ones, as ``sqlReturnType`` holds a return only when every overload agrees;
    otherwise the function-level map is absent and each signature carries its own
    wrapper's literals as ``boundArgs``. Without the two sources only ``mdbC`` is read.

    Returns ``(idl, count, drift)`` where ``drift`` lists
    ``(function, param, reason)`` call arguments the pass could not classify as a
    caller arg / out-param / literal (a bare identifier that is neither) — a
    signal to inspect, never trusted as a bound value.

    ``documented`` (from ``parser.outparam.extract_param_names``) maps a MEOS function to
    its ``@param``-documented parameter names; a bare identifier bound to one of those is a
    caller-read / derived value and is skipped, so drift is confined to genuinely
    undocumented parameters."""
    from parser.sqlfn import _meos_to_mdb, _wrapper_sql_sigs
    documented = documented or {}
    wrappers = extract_wrappers(mdb_src)
    helpers = extract_helpers(mdb_src)
    m2d = _meos_to_mdb(meos_src) if meos_src else {}
    w2sig = _wrapper_sql_sigs(sql_src) if sql_src else {}
    drift: list[tuple[str, str, str]] = []
    claimed: dict[str, list] = {}
    groups: dict[str, list] = {}
    for func in idl["functions"]:
        primary = func.get("mdbC")
        if not primary:
            continue
        ws = [primary] + [w for w in m2d.get(func["name"]) or () if w != primary]
        claimed[func["name"]] = ws
        for w in ws:
            groups.setdefault(w, []).append(func)
    generics = extract_param_lists(meos_src) if meos_src else {}
    wbound = {w: _group_bound(wrappers[w], group, helpers, drift, documented, generics)
              for w, group in groups.items() if w in wrappers}
    n = 0
    for func in idl["functions"]:
        ws = claimed.get(func["name"])
        if not ws:
            continue
        pnames = {p.get("name") for p in func.get("params", [])}

        def own(w, sig=None):
            bound, guarded = wbound.get(w) or ({}, {})
            out = {k: v for k, v in bound.items() if k in pnames}
            if sig is not None:
                nargs = len(sig.get("args") or ())
                out.update({k: lit for k, (pos, lit) in guarded.items()
                            if k in pnames and nargs <= pos})
            return out

        sigs = func.get("sqlSignatures") or []
        sig_ws = [_signature_wrapper(func, s, ws, w2sig) or ws[0] for s in sigs]
        per_sig = [own(w, s) for s, w in zip(sigs, sig_ws)] or [own(ws[0])]
        if len({tuple(sorted(b.items())) for b in per_sig}) == 1:
            bound = per_sig[0]
            if bound:
                func.setdefault("shape", {})["boundArgs"] = bound
                n += len(bound)
            continue
        for s, b in zip(sigs, per_sig):
            if b:
                s["boundArgs"] = b
                n += len(b)
    return idl, n, list(dict.fromkeys(drift))


# `#define NAME <literal>`: an object-like macro whose body is one integer, float or
# boolean literal, the form every bound flag and default takes (`#define REST_AT true`,
# `#define OUT_DEFAULT_DECIMAL_DIGITS 15`). A function-like macro has `(` right after its
# name and does not match.
_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(?P<name>[A-Z][A-Z0-9_]*)[ \t]+"
                     r"(?P<val>[^\s/]+)[ \t]*(?:/[*/].*)?$", re.M)


def _define_value(tok: str):
    """The JSON value of a macro body, or None when it is not a single literal."""
    if tok in ("true", "TRUE"):
        return True
    if tok in ("false", "FALSE"):
        return False
    try:
        return int(tok, 0)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        return None


def resolve_bound_names(idl: dict, include_root: str | Path) -> tuple[dict, int, list]:
    """Carry in ``idl["macros"]`` the value of every macro a bound literal names.

    A ``boundArgs`` value can be a macro name (``atfunc: REST_AT``, ``maxdd:
    OUT_DEFAULT_DECIMAL_DIGITS``) defined in a header the parse does not read: the installed
    headers a libmeos build parses carry none of ``temporal/temporal.h``, so a binding
    generator reading that catalog finds the name and no value for it. Each name that is
    neither a catalog macro nor an enum member is looked up among the ``#define`` lines of
    the MEOS source headers under ``include_root`` and, when every definition gives it the
    same single literal, recorded as a macro with that value (``true`` and ``false`` as
    booleans).

    Returns ``(idl, count, unresolved)``: the number of names recorded, and the names left
    without a value (no definition, a body that is not one literal, or definitions that
    disagree)."""
    from parser.extractors import _family_of
    known = {m["name"] for m in idl.get("macros", [])}
    known |= {v["name"] for e in idl.get("enums", []) for v in e.get("values") or []
              if isinstance(v, dict)}
    wanted = set()
    for f in idl.get("functions", []):
        maps = [(f.get("shape") or {}).get("boundArgs") or {}]
        maps += [s.get("boundArgs") or {} for s in f.get("sqlSignatures") or []]
        for b in maps:
            wanted |= {v for v in b.values()
                       if _ENUM.match(v) and v not in ("NULL", "TRUE", "FALSE")
                       and v not in known}
    defs: dict[str, list] = {}
    for h in sorted(Path(include_root).rglob("*.h")):
        text = h.read_text(errors="ignore")
        for m in _DEFINE.finditer(text):
            if m.group("name") in wanted:
                line = text.count("\n", 0, m.start()) + 1
                defs.setdefault(m.group("name"), []).append(
                    (_define_value(m.group("val")), h, line))
    n, unresolved = 0, []
    for name in sorted(wanted):
        vals = [v for v, _, _ in defs.get(name, [])]
        if not vals or None in vals or any(type(v) is not type(vals[0]) or v != vals[0]
                                           for v in vals):
            unresolved.append(name)
            continue
        val, h, line = defs[name][0]
        idl.setdefault("macros", []).append({
            "name": name, "file": h.name, "family": _family_of(str(h), line),
            "vendored": False, "value": val})
        n += 1
    return idl, n, unresolved
