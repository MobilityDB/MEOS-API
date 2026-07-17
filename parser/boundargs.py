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
enum/macro) is recorded.  A wrapper that does not call the MEOS function by name
(it delegates to a shared helper) yields no ``boundArgs``.
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


def _literal(arg: str) -> str | None:
    """Normalise a call argument to the literal to record, or None if not a literal."""
    if _TRUE.match(arg):
        return "true"
    if _FALSE.match(arg):
        return "false"
    if arg == "NULL" or _NUMBER.match(arg) or _ENUM.match(arg):
        return arg
    return None


def _wrapper_bound(body: str, func: dict, drift: list,
                   documented: dict[str, set]) -> dict[str, str]:
    """The literals wrapper ``body`` binds in its call to ``func['name']``, keyed by
    ``func``'s parameter name. Empty if the wrapper does not call ``func`` by name.

    ``documented`` maps a MEOS function to the set of its ``@param``-documented parameter
    names (``parser.outparam.extract_param_names``). A bare-identifier argument bound to a
    documented parameter is a value the wrapper reads from the caller or derives — an
    array length (``@param[in] count``), an aggregate state (``@param[in,out] state``) —
    never a hard-coded literal, so it is skipped systematically. Only a bare identifier
    for an UNDOCUMENTED parameter is reported as drift (the exceptional manual gap)."""
    args = _call_args(body, func["name"])
    if not args:
        return {}
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


def merge_boundargs(idl: dict, mdb_src: str | Path,
                    documented: dict[str, set] | None = None) -> tuple[dict, int, list]:
    """Fold wrapper-bound literals into each function's ``shape.boundArgs``.

    Functions are grouped by the PG wrapper they share (``mdbC``). Every function in a
    group has the SAME SQL contract, so a literal the wrapper binds (keyed by parameter
    name) applies to ALL of them — crucially the per-base-type collapse siblings
    (``tbool``/``tint``/… ``_value_at_timestamptz``) that a binding dispatches to for a
    typed result but that the wrapper never calls by name (it calls the generic
    ``temporal_value_at_timestamptz``). Only members that actually own a parameter of that
    name receive the literal.

    Returns ``(idl, count, drift)`` where ``drift`` lists
    ``(function, param, reason)`` call arguments the pass could not classify as a
    caller arg / out-param / literal (a bare identifier that is neither) — a
    signal to inspect, never trusted as a bound value.

    ``documented`` (from ``parser.outparam.extract_param_names``) maps a MEOS function to
    its ``@param``-documented parameter names; a bare identifier bound to one of those is a
    caller-read / derived value and is skipped, so drift is confined to genuinely
    undocumented parameters."""
    documented = documented or {}
    wrappers = extract_wrappers(mdb_src)
    n = 0
    drift: list[tuple[str, str, str]] = []
    groups: dict[str, list] = {}
    for func in idl["functions"]:
        w = func.get("mdbC")
        if w:
            groups.setdefault(w, []).append(func)
    for wname, group in groups.items():
        body = wrappers.get(wname)
        if body is None:
            continue
        # the wrapper's bound literals, keyed by param name, from whichever group member(s)
        # the wrapper calls by name (branches — e.g. the RGEO ternary — agree, first wins)
        wbound: dict[str, str] = {}
        for func in group:
            for k, v in _wrapper_bound(body, func, drift, documented).items():
                wbound.setdefault(k, v)
        if not wbound:
            continue
        for func in group:
            pnames = {p.get("name") for p in func.get("params", [])}
            bound = {k: v for k, v in wbound.items() if k in pnames}
            if bound:
                func.setdefault("shape", {})["boundArgs"] = bound
                n += len(bound)
    return idl, n, drift
