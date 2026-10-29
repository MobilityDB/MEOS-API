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
each SQL signature of a number of arguments for which the wrapper never replaces it,
where it is the literal the MEOS call reads; a signature of another number passes the
caller's value. A test of ``PG_NARGS()`` decides the numbers each statement runs for,
an ``else`` running for the others: ``Temporal_app_tinst_transfn`` reads ``maxt`` from
argument 3 under ``PG_NARGS() == 4`` and ``maxdist`` from argument 3 in its ``else``, so
``appendInstantTransition(tint, tint, text, interval)`` binds ``maxdist`` to ``-1.0``.

A wrapper can reach the functions its ``@csqlfn`` tags name through an internal generic
that none of them is: ``Numset_shift`` calls ``numset_shift_scale(s, shift, 0, true,
false)``, while the tag names ``intset_shift_scale``, ``floatset_shift_scale`` and their
siblings, each taking the generic's parameters under the same names in the same order.
When a wrapper calls no member of its group and delegates to no helper, the MEOS
definition of each function it calls is read, and a function whose parameter names equal
a member's is the generic that member wraps: its literals bind to the member by parameter
name.

A function MEOS takes from PostgreSQL is public under PostgreSQL's name only in MEOS, where
it calls its ``pg_`` twin, which takes the same parameters in the same order; the extension,
which links PostgreSQL's own function of that name, calls the twin:
``Jsonb_path_exists_common`` calls ``pg_jsonb_path_exists(jb, jp, vars, silent, tz)``
for ``jsonb_path_exists``. A call to the twin is read as a call to the member.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

from parser.families import built_files

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
# A shared helper takes the call info plus the parameters the wrappers bind, whatever it
# returns: a `Datum` helper answers for the wrapper, a `void` one lays the state a
# set-returning wrapper then streams (`Tgeo_split_start`, `Stbox_tiles_start`).
_HELPER_HEAD = re.compile(r"\b(?P<name>[A-Za-z_]\w*)\s*\(\s*FunctionCallInfo\s+\w+")
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
    """Return ``{wrapper_name: body_text}`` for every PG wrapper under ``mdb_src``, each
    call of an argument helper read as the statements it runs (#_expand_arg_helpers)."""
    helpers = _raw_helpers(str(mdb_src))
    out: dict[str, str] = {}
    for cf in built_files(mdb_src, "*.c"):
        text = cf.read_text(errors="ignore")
        for m in _WRAP.finditer(text):
            out[m.group("name")] = _expand_arg_helpers(_body(text, m.end() - 1), helpers)
    return out


@functools.lru_cache(maxsize=None)
def _raw_helpers(mdb_src: str) -> dict[str, tuple[str, list[str]]]:
    """``{helper_name: (body_text, [param names after the call info])}`` for every function
    under ``mdb_src`` taking the call info first, as written."""
    out: dict[str, tuple[str, list[str]]] = {}
    for cf in built_files(mdb_src, "*.c"):
        text = cf.read_text(errors="ignore")
        for m in _HELPER_HEAD.finditer(text):
            # the parameter list closes at its balanced parenthesis, so a function-pointer
            # parameter, `Raster * (*func)(const Raster *, int, ...)`, stays one parameter
            close = _close_paren(text, m.end("name") + text[m.end("name"):].index("("))
            brace = _skip_space(text, close + 1)
            if not text.startswith("{", brace):
                continue
            rest = text[m.end():close].strip()
            names: list[str] = []
            if rest.startswith(","):
                for decl in _split_args(rest[1:]):
                    fnptr = re.search(r"\(\s*\*\s*(\w+)\s*\)", decl)
                    tok = decl.replace("*", " ").split()
                    if fnptr:
                        names.append(fnptr.group(1))
                    elif tok:
                        names.append(tok[-1])
            out[m.group("name")] = (_body(text, brace), names)
    return out


def extract_helpers(mdb_src: str | Path) -> dict[str, tuple[str, list[str]]]:
    """``{helper_name: (body_text, [param names after the call info])}`` for every shared
    wrapper helper under ``mdb_src``, each call of an argument helper in its body read as
    the statements it runs (#_expand_arg_helpers)."""
    raw = _raw_helpers(str(mdb_src))
    return {name: (_expand_arg_helpers(body, raw), names)
            for name, (body, names) in raw.items()}


# A call handing the call info to a helper, followed by the helper's other arguments.
_ARG_CALL = re.compile(r"\b(?P<name>[A-Za-z_]\w*)\s*\(\s*fcinfo\s*,")
# The number of arguments of the call kept in a local: `int nargs = PG_NARGS();`.
_NARGS_LOCAL = re.compile(r"\b(?:int\s+)?(?P<var>\w+)\s*=\s*PG_NARGS\s*\(\s*\)\s*;")
# A value chosen by the number of arguments: `band = nargs >= 4 ? PG_GETARG_INT32(1) : 1;`.
_NARGS_CHOICE = re.compile(
    r"(?P<lhs>(?<![\w.>])\w+)\s*=(?!=)\s*\(?\s*(?P<test>PG_NARGS\s*\(\s*\)\s*"
    r"(?:==|!=|>=|<=|>|<)\s*\d+)\s*\)?\s*\?\s*(?P<then>[^:;]+?)\s*:\s*(?P<else>[^;]+?)\s*;")


def _nargs_choices(text: str) -> str:
    """``text`` with each value chosen by the number of arguments written as the value of
    the other numbers followed by its replacement under that test, the shape
    #_guarded_default reads: ``bool segmentize = (PG_NARGS() == 2) ?
    PG_GETARG_BOOL(1) : false;`` reads as ``bool segmentize = false; if (PG_NARGS() == 2)
    segmentize = PG_GETARG_BOOL(1);``."""
    return _NARGS_CHOICE.sub(lambda c: f"{c['lhs']} = {c['else']}; "
                             f"if ({c['test']}) {c['lhs']} = {c['then']};", text)


def _inline_arg_helper(hbody: str, hparams: list[str], args: list[str]) -> str:
    """The statements of the argument helper ``hbody`` called with ``args`` (the arguments
    after the call info, matched to ``hparams``), written as statements of its caller.

    An argument helper fills locals of its caller through their addresses:
    ``value_count_args(fcinfo, false, &band, &exclude_nodata, &searchvalues, &nsearch,
    &roundto)`` sets ``*band = nargs >= 4 ? PG_GETARG_INT32(1) : 1``. Each ``*p`` and
    ``(*p)`` reads as the local whose address ``p`` receives, a bare ``p`` as that address,
    a parameter given a literal as the literal, the local holding ``PG_NARGS()`` as
    ``PG_NARGS()``, and a value chosen by the number of arguments as the value of the other
    numbers followed by its replacement under that test, the shape #_guarded_default reads:
    ``band = 1; if (PG_NARGS() >= 4) band = PG_GETARG_INT32(1);``. A ``return`` leaves
    nothing the caller runs."""
    text = _COMMENT.sub(" ", hbody)
    m = _NARGS_LOCAL.search(text)
    if m:
        text = text[:m.start()] + text[m.end():]
        text = re.sub(r"\b" + re.escape(m.group("var")) + r"\b", "PG_NARGS()", text)
    for p, a in zip(hparams, args):
        a = a.strip()
        if a.startswith("&") and _IDENT.match(a[1:].strip()):
            local = a[1:].strip()
            pe = re.escape(p)
            text = re.sub(r"\(\s*\*\s*" + pe + r"\s*\)|(?<![\w)\]])\*\s*" + pe + r"\b"
                          r"|(?<![\w.>])" + pe + r"\b",
                          lambda x, local=local: local if x.group().lstrip("(").lstrip()
                          .startswith("*") else "&" + local, text)
        else:
            lit = _literal(a)
            if lit is not None:
                text = re.sub(r"(?<![\w.>])" + re.escape(p) + r"\b", lit, text)
    text = re.sub(r"\breturn\b[^;]*;", ";", text)
    return "{" + _nargs_choices(text) + "}"


def _expand_arg_helpers(body: str, helpers: dict[str, tuple[str, list[str]]]) -> str:
    """``body`` with each call of an argument helper read as the statements the helper runs
    (#_inline_arg_helper), placed before the statement holding the call, the call itself
    reading as ``true``. An argument helper is one the call hands the address of a local
    for each parameter it writes through: ``jsonb_path_args(fcinfo, &vars, &silent)``
    sets ``*vars`` and ``*silent``. A call handing it no address is a delegation
    (#_delegated) and stays. A value ``body`` itself chooses by the number of arguments
    reads as an inlined helper's does (#_nargs_choices)."""
    for _ in range(8):
        for m in _ARG_CALL.finditer(body):
            entry = helpers.get(m.group("name"))
            if entry is None:
                continue
            hbody, hparams = entry
            open_ = body.index("(", m.start())
            close = _close_paren(body, open_)
            args = _split_args(body[open_ + 1:close])[1:]
            outs = [p for p, a in zip(hparams, args) if a.strip().startswith("&")]
            if not outs or not all(re.search(r"\*\s*" + re.escape(p) + r"\s*=(?!=)", hbody)
                                   for p in outs):
                continue
            start = max(body.rfind(c, 0, m.start()) for c in ";{}") + 1
            body = (body[:start] + _inline_arg_helper(hbody, hparams, args) +
                    body[start:m.start()] + "true" + body[close + 1:])
            break
        else:
            return _nargs_choices(body)
    return _nargs_choices(body)


def _delegated(body: str, helpers: dict[str, tuple[str, list[str]]]):
    """``(helper_body, {helper_param: literal}, {helper_param: (arities, literal)})`` when
    ``body`` delegates to a shared helper, passing the call info through: the helper
    parameters the wrapper binds to literals, and those it binds to a local it reads from
    an argument only for the numbers of arguments ``arities`` (#_guarded_default), the
    literal the local starts from being what a signature of another number passes on.
    The helper body comes as the call makes it run (#_specialize), a function the wrapper
    hands a function-pointer parameter, ``&raster_tpi``, read as the function it calls."""
    assigned = {a.group("var") for a in _ASSIGNED.finditer(body)}
    for m in _DELEG.finditer(body):
        entry = helpers.get(m.group("name"))
        if entry is None:
            continue
        hbody, hparams = entry
        raw = (m.group("args") or "").strip()
        vals = _split_args(raw[1:]) if raw.startswith(",") else []
        subst, gsubst, fnptrs = {}, {}, {}
        for pname, val in zip(hparams, vals):
            val = val.strip()
            lit = _literal(val)
            if lit is not None:
                subst[pname] = lit
            elif (val.startswith("&") and _IDENT.match(val[1:].strip())
                  and val[1:].strip() not in assigned):
                fnptrs[pname] = val[1:].strip()
            elif _IDENT.match(val):
                dflt = _guarded_default(body, val)
                if dflt is not None:
                    gsubst[pname] = dflt
        if subst or gsubst or fnptrs:
            return _specialize(hbody, subst, fnptrs), subst, gsubst
    return None, {}, {}


# A call to a lowercase C function, the form every MEOS function takes. A keyword
# followed by a parenthesis (`if (`, `while (`, `return (`, `sizeof (`) opens no call:
# read as one, the condition of `nad_tint_tbox`'s first `if` paired with the wrapper's
# `if (datum_eq(...))` as the kernel both of them call.
_CALLEE = re.compile(r"\b(?!(?:if|else|for|while|do|switch|case|return|sizeof)\b)"
                     r"(?P<name>[a-z][a-z0-9_]*)\s*\(")


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
    """Normalise a call argument to the literal to record, or None if not a literal. A
    comment naming the parameter, as ``union_tpcbox_tpcbox(a, b, /* strict */ false)``
    writes it, is read as the blank #_guards reads a comment as."""
    arg = _COMMENT.sub(" ", arg).strip()
    if _TRUE.match(arg):
        return "true"
    if _FALSE.match(arg):
        return "false"
    if arg == "NULL" or _NUMBER.match(arg) or _ENUM.match(arg):
        return arg
    return None


# The numbers of arguments a SQL function can be called with: PostgreSQL's FUNC_MAX_ARGS.
_ARITIES = frozenset(range(101))
# A test of the number of arguments of the call: `PG_NARGS() == 4`.
_NARGS_TEST = re.compile(r"PG_NARGS\s*\(\s*\)\s*(==|!=|>=|<=|>|<)\s*(\d+)")
_NARGS_OPS = {"==": lambda n, k: n == k, "!=": lambda n, k: n != k,
              ">=": lambda n, k: n >= k, "<=": lambda n, k: n <= k,
              ">": lambda n, k: n > k, "<": lambda n, k: n < k}


def _arities(op: str, k: int) -> frozenset:
    """The numbers of arguments for which ``PG_NARGS() <op> k`` holds."""
    return frozenset(n for n in _ARITIES if _NARGS_OPS[op](n, k))


def _split_top(expr: str, op: str) -> list[str]:
    """``expr`` split on the operator ``op`` where it sits outside every parenthesis."""
    out, depth, start, i = [], 0, 0, 0
    while i < len(expr):
        c = expr[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and expr.startswith(op, i):
            out.append(expr[start:i])
            start = i + len(op)
            i = start
            continue
        i += 1
    out.append(expr[start:])
    return out


def _close_paren(text: str, open_pos: int) -> int:
    """The index of the parenthesis closing the one at ``open_pos``."""
    depth = 0
    for i in range(open_pos, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _statement_end(body: str, j: int) -> int:
    """The end of the statement starting at ``j``: a brace block, an ``if`` with its
    ``else``, or a single statement up to its semicolon."""
    if body.startswith("{", j):
        return j + len(_body(body, j)) + 2
    m = re.match(r"if\s*\(", body[j:])
    if m:
        end = _statement_end(body, _skip_space(body, _close_paren(body, j + m.end() - 1) + 1))
        e = _skip_space(body, end)
        if re.match(r"else\b", body[e:]):
            return _statement_end(body, _skip_space(body, e + 4))
        return end
    return body.find(";", j) + 1


@functools.lru_cache(maxsize=None)
def _guards(body: str) -> tuple[tuple[frozenset, int, int], ...]:
    """``(arities, start, end)`` for every statement ``body`` runs only for the numbers of
    arguments ``arities``: the span of the brace block or of the single statement an
    ``if`` guards, the test of which states ``PG_NARGS()`` in a conjunct, and of the
    ``else`` of a test that states nothing but ``PG_NARGS()``, which runs for the other
    numbers. ``Temporal_app_tinst_transfn`` reads ``maxt`` from argument 3 under
    ``if (PG_NARGS() == 4)`` and ``maxdist`` from argument 3 in its ``else``, both under
    ``if (PG_NARGS() > 3)``. Comments are read as the blanks #_COMMENT replaces them by,
    one for each character, so ``else /* PG_NARGS() == 5 */`` opens its block after the
    comment and every span stays a span of ``body``."""
    body = _COMMENT.sub(lambda c: " " * len(c.group()), body)
    out: list[tuple[frozenset, int, int]] = []
    for m in re.finditer(r"\bif\s*\(", body):
        close = _close_paren(body, m.end() - 1)
        cond = body[m.end():close]
        if len(_split_top(cond, "||")) > 1:
            continue
        conjuncts = [c.strip() for c in _split_top(cond, "&&")]
        tests = [_NARGS_TEST.fullmatch(c) for c in conjuncts]
        if not any(tests):
            continue
        runs = _ARITIES
        for t in tests:
            if t:
                runs = runs & _arities(t.group(1), int(t.group(2)))
        j = _skip_space(body, close + 1)
        end = _statement_end(body, j)
        out.append((runs, j, end))
        e = _skip_space(body, end)
        if all(tests) and re.match(r"else\b", body[e:]):
            k = _skip_space(body, e + 4)
            out.append((_ARITIES - runs, k, _statement_end(body, k)))
    return tuple(out)


def _runs_at(body: str, pos: int) -> frozenset:
    """The numbers of arguments for which the statement of ``body`` at ``pos`` runs."""
    runs = _ARITIES
    for r, s, e in _guards(body):
        if s <= pos < e:
            runs = runs & r
    return runs


def _decided(cond: str) -> bool | None:
    """The truth of condition ``cond`` once a wrapper's literals stand in it, or None when
    it still depends on the call: ``false && ! PG_ARGISNULL(4)`` is false and
    ``true || x`` true, as #_guards splits a test on its top-level operators."""
    c = cond.strip()
    while c.startswith("(") and _close_paren(c, 0) == len(c) - 1:
        c = c[1:-1].strip()
    if c in ("true", "false"):
        return c == "true"
    if c.startswith("!") and not c.startswith("!="):
        v = _decided(c[1:])
        return None if v is None else not v
    ors = _split_top(c, "||")
    if len(ors) > 1:
        vals = [_decided(o) for o in ors]
        return True if True in vals else (False if all(v is False for v in vals) else None)
    ands = _split_top(c, "&&")
    if len(ands) > 1:
        vals = [_decided(a) for a in ands]
        return False if False in vals else (True if all(vals) else None)
    # Two constants compared: the type #specialize_argtypes states for an argument against
    # the one a test names, `T_TIMESTAMPTZ == T_TSTZSPAN`, or two numbers
    eq = (re.fullmatch(r"(T_[A-Z0-9_]+)\s*(==|!=)\s*(T_[A-Z0-9_]+)", c)
          or re.fullmatch(r"(-?\d+)\s*(==|!=)\s*(-?\d+)", c))
    if eq:
        return (eq.group(1) == eq.group(3)) == (eq.group(2) == "==")
    return None


# An assignment of a value chosen by a condition: `int i = has_extent ? 1 : 0;`.
_CHOICE = re.compile(
    r"=(?!=)\s*(?P<cond>\((?:[^()]|\([^()]*\))*\)|!?\s*\w+)\s*\?\s*(?P<then>[^:;]+?)\s*:"
    r"\s*(?P<else>[^;]+?)\s*;")


def _number_reads(seg: str, inc: re.Pattern, c: int,
                  plain: re.Pattern | None = None) -> tuple[str, int] | None:
    """``seg`` with each read of a running index, ``i++``, written as the number it holds,
    starting from ``c``, and the number after the last read, or None when a branch leaves
    it undecided: both arms of a test advance it by as many reads, as
    ``Stbox_constructor`` reads the period from a span or a timestamp, one argument
    either way. A read that does not advance it, ``plain`` (#_number_indices builds both
    patterns), is the number it holds there: ``get_fn_expr_argtype(fcinfo->flinfo, i)``
    after four reads is argument 4."""
    out, i = "", 0
    test = re.compile(r"\bif\s*\(")
    while True:
        mi, mf = inc.search(seg, i), test.search(seg, i)
        mp = plain.search(seg, i) if plain else None
        if mp and (not mi or mp.start() < mi.start()) and (not mf or mp.start() < mf.start()):
            out += seg[i:mp.start()] + str(c)
            i = mp.end()
            continue
        if not mi:
            return out + seg[i:], c
        if mf and mf.start() < mi.start():
            close = _close_paren(seg, mf.end() - 1)
            j = _skip_space(seg, close + 1)
            end = _statement_end(seg, j)
            e = _skip_space(seg, end)
            has_else = re.match(r"else\b", seg[e:]) is not None
            k = _skip_space(seg, e + 4) if has_else else end
            stop = _statement_end(seg, k) if has_else else end
            a = _number_reads(seg[j:end], inc, c, plain)
            b = _number_reads(seg[k:stop], inc, c, plain) if has_else else ("", c)
            if a is None or b is None or a[1] != b[1]:
                return None
            out += seg[i:j] + a[0] + seg[end:k] + b[0]
            i, c = stop, a[1]
            continue
        out += seg[i:mi.start()] + str(c)
        i, c = mi.end(), c + 1


def _number_indices(text: str) -> str:
    """``text`` with the SQL arguments it reads through an index local stated by number:
    a running index, ``int i = 0; ... PG_GETARG_FLOAT8(i++)``, numbered read by read
    (#_number_reads), and an index assigned once, ``int i = 0; ...
    PG_GETARG_TEXT_P(2 + i)``, added in, so #_direct_indices reads each argument."""
    for m in re.finditer(r"\bint\s+(?P<v>\w+)\s*=\s*(?P<n>\d+)\s*;", text):
        v, n = m.group("v"), int(m.group("n"))
        inc = re.compile(r"(?<![\w.>])" + re.escape(v) + r"\s*\+\+")
        plain = re.compile(r"(?<![\w.>])" + re.escape(v) + r"\b(?!\s*\+\+)")
        rest = text[m.end():]
        if re.search(r"(?<![\w.>])" + re.escape(v) + r"\s*(?:=(?!=)|--|[-+*/]=)", rest):
            continue
        if inc.search(rest):
            numbered = _number_reads(rest, inc, n, plain)
            if numbered is not None:
                text = text[:m.end()] + numbered[0]
            continue
        text = text[:m.end()] + re.sub(
            r"(PG_GETARG_\w+\s*\(\s*)(\d+)\s*\+\s*" + re.escape(v) + r"\b|"
            r"(PG_GETARG_\w+\s*\(\s*)" + re.escape(v) + r"\s*\+\s*(\d+)",
            lambda x: (x.group(1) or x.group(3)) + str(int(x.group(2) or x.group(4)) + n),
            rest)
    return text


def _specialize(hbody: str, subst: dict[str, str], fnptrs: dict[str, str]) -> str:
    """The shared helper ``hbody`` as the wrapper's call makes it run: each parameter
    ``subst`` binds reads as its literal, each function-pointer parameter ``fnptrs`` binds
    calls the function the wrapper hands it, and each test and choice those literals
    decide keeps its branch alone (#_decided). ``Raster_terrain_index(fcinfo, false,
    &raster_tpi)`` runs ``i = 0``, ``extent = NULL`` and ``raster_tpi(rast, band, extent,
    ...)``, and ``Jsonbset_set_common(fcinfo, false)`` never reads the null treatment of
    its lax twin. Comments are read as blanks, one for each character, as #_guards reads
    them."""
    text = _COMMENT.sub(lambda c: " " * len(c.group()), hbody)
    for p, fn in fnptrs.items():
        text = re.sub(r"(?<![\w.>])" + re.escape(p) + r"\s*\(", fn + "(", text)
    for p, lit in subst.items():
        text = re.sub(r"(?<![\w.>])" + re.escape(p) + r"\b", lit, text)
    text = _CHOICE.sub(lambda c: c.group() if _decided(c["cond"]) is None else
                       "= " + (c["then"] if _decided(c["cond"]) else c["else"]) + ";",
                       text)
    pos = 0
    while True:
        m = re.compile(r"\bif\s*\(").search(text, pos)
        if not m:
            return _number_indices(text)
        close = _close_paren(text, m.end() - 1)
        verdict = _decided(text[m.end():close])
        j = _skip_space(text, close + 1)
        end = _statement_end(text, j)
        e = _skip_space(text, end)
        has_else = re.match(r"else\b", text[e:]) is not None
        k = _skip_space(text, e + 4) if has_else else end
        stop = _statement_end(text, k) if has_else else end
        if verdict is None:
            pos = m.end()
            continue
        kept = text[j:end] if verdict else (text[k:stop] if has_else else ";")
        text = text[:m.start()] + kept + text[stop:]
        pos = m.start()


_ARGTYPE = re.compile(r"oid_meostype\s*\(\s*get_fn_expr_argtype\s*\(\s*fcinfo\s*->\s*flinfo"
                      r"\s*,\s*(\d+)\s*\)\s*\)")


def specialize_argtypes(hbody: str, args: list[str], members: set[str]) -> str:
    """The helper ``hbody`` (#_specialize's output) as one SQL signature of argument types
    ``args`` makes it run: the type the wrapper reads of argument ``k``,
    ``oid_meostype(get_fn_expr_argtype(fcinfo->flinfo, k))``, is the member of ``MeosType``
    naming ``args[k]`` (``timestamptz`` is ``T_TIMESTAMPTZ``), a local assigned that type
    once reads as it, and each test the types decide keeps its branch alone, as
    #_specialize keeps the branch a literal decides. ``Stbox_constructor`` reads the period
    of ``stboxT(timestamptz)`` through ``timestamptz_to_span`` and that of
    ``stboxT(tstzspan)`` as the span it is. A type ``members`` does not hold stays
    undecided."""
    text = _ARGTYPE.sub(
        lambda m: ("T_" + args[int(m.group(1))].upper()
                   if int(m.group(1)) < len(args)
                   and "T_" + args[int(m.group(1))].upper() in members else m.group(0)),
        hbody)
    decl = re.compile(r"\b\w+\s+(?P<v>\w+)\s*=\s*(?P<t>T_[A-Z0-9_]+)\s*;")
    pos = 0
    while (m := decl.search(text, pos)):
        v, rest = m.group("v"), text[m.end():]
        pos = m.end()
        if re.search(r"(?<![\w.>])" + re.escape(v) + r"\s*(?:=(?!=)|--|\+\+|[-+*/]=)", rest):
            continue
        text = text[:m.end()] + re.sub(r"(?<![\w.>])" + re.escape(v) + r"\b", m.group("t"), rest)
    return _specialize(text, {}, {})


def _guarded_default(body: str, var: str) -> tuple[frozenset, str] | None:
    """``(arities, literal)`` when local ``var`` starts from a literal and every later
    assignment of it runs only for the numbers of arguments ``arities`` (#_guards).  A SQL
    signature of another number of arguments never runs those assignments, so the MEOS
    call reads the literal, as a SQL DEFAULT would supply it: ``Tspatial_as_text_common``
    starts ``dbl_dig_for_wkt`` from ``OUT_DEFAULT_DECIMAL_DIGITS`` and reads argument 1
    only when the call carries it, and ``Temporal_app_tinst_transfn`` starts ``maxdist``
    from ``-1.0`` and reads it only for five arguments."""
    v = re.escape(var)
    if re.search(r"&\s*" + v + r"\b|(?<![\w.>])" + v +
                 r"\s*(?:\+\+|--|(?:[-+*/%&|^]|<<|>>)=)|(?:\+\+|--)\s*" + v + r"\b",
                 body):
        return None
    assigns = list(re.finditer(r"(?<![\w.>])" + v + r"\s*=(?!=)", body))
    if not assigns:
        return None
    # an initialiser ends at the comma of a declaration of several locals, `double xmin =
    # 0, xmax = 0;`, as at its semicolon
    init = re.match(r"\s*([^;,]+?)\s*[;,]", body[assigns[0].end():])
    lit = _literal(init.group(1)) if init else None
    if lit is None or _runs_at(body, assigns[0].start()) != _ARITIES:
        return None
    if len(assigns) == 1:
        # assigned once, from a literal, by every call: the literal for every number of
        # arguments, as `period` stays NULL in `Stbox_constructor` for a box without T
        return frozenset(), lit
    runs: frozenset = frozenset()
    for a in assigns[1:]:
        r = _runs_at(body, a.start())
        if r == _ARITIES:
            return None
        runs = runs | r
    return runs, lit


def _wrapper_bound(body: str, func: dict, drift: list,
                   documented: dict[str, set],
                   subst: dict[str, str] | None = None,
                   guarded: dict[str, tuple[int, str]] | None = None,
                   gsubst: dict[str, tuple[int, str]] | None = None) -> dict[str, str]:
    """The literals wrapper ``body`` binds in its call to ``func['name']``, keyed by
    ``func``'s parameter name. Empty if the wrapper does not call ``func`` by name.

    A local the wrapper reads from an argument only for the numbers of arguments
    ``arities`` (``_guarded_default``) is caller-sourced for a signature of one of them and
    a literal for any other; it goes into ``guarded`` as ``{param: (arities, literal)}``. When
    ``body`` is a helper the wrapper delegates to, ``gsubst`` holds the helper parameters
    such a local reaches (#_delegated), and a call argument naming one of them goes into
    ``guarded`` the same way.

    ``documented`` maps a MEOS function to the set of its ``@param``-documented parameter
    names (``parser.outparam.extract_param_names``). A bare-identifier argument bound to a
    documented parameter is a value the wrapper reads from the caller or derives — an
    array length (``@param[in] count``), an aggregate state (``@param[in,out] state``) —
    never a hard-coded literal, so it is skipped systematically. Only a bare identifier
    for an UNDOCUMENTED parameter is reported as drift (the exceptional manual gap)."""
    args = _call_args(body, func["name"]) or _call_args(body, "pg_" + func["name"])
    if not args:
        return {}
    subst = subst or {}
    gsubst = gsubst or {}
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
        if a in gsubst and guarded is not None:
            # a helper parameter the delegating wrapper feeds from a guarded local
            guarded.setdefault(pname, gsubst[a])
            continue
        if a in assigned and _IDENT.match(a) and guarded is not None:
            dflt = _guarded_default(body, a)
            if dflt is not None:
                guarded.setdefault(pname, dflt)
                continue
        if (a in assigned and _IDENT.match(a)
                and pname in ((func.get("shape") or {}).get("nullable") or ())
                and not _assigned_from_argument(body, a)
                and not re.search(r"(?<![\w.>])" + re.escape(a) + r"\s*=(?!=)[^;]*\b"
                                  + re.escape(func["name"]) + r"\s*\(", body)
                and _caller_index(body, a) in _directly_carried(
                    body, [o for o in args if o != a], assigned)):
            # a value the host derives from an SQL argument another parameter carries
            # itself, for a parameter the function reads as NULL where the caller gives
            # none, as Temporal_as_mfjson fills srs from the table spatial_ref_sys by the
            # SRID of the temporal value temp carries: a binding passes NULL and the
            # function derives it itself
            bound[pname] = "NULL"
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
    and the ``{param: (arities, literal)}`` it supplies for a call of a number of arguments
    outside ``arities``, read
    from its call to whichever member of ``group`` it names (branches such as the RGEO
    ternary agree, and the first wins), or from its delegation to a shared helper when it
    names none, or from its call to the generic the members wrap when it does neither --
    a function none of them is whose parameter names, in ``generics``, equal a member's.

    A helper can reach none of the members either: a set-returning wrapper hands its
    arguments to a helper that lays the state it streams through the members' own
    initialiser (`Tgeo_split_start` calls `tgeo_space_time_split_init`, not
    `tgeo_space_split`). The initialiser takes the members' parameters under the same
    names among others, so each argument of that call binds to the member parameter of
    its name, as the generic twin binds by name."""
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
    hbody, subst, gsubst = _delegated(body, helpers)
    if hbody is not None:
        for func in group:
            for k, v in _wrapper_bound(hbody, func, drift, documented, subst,
                                       guarded, gsubst).items():
                bound.setdefault(k, v)
        if not (bound or guarded) and generics:
            member_params = {p.get("name") for f in group for p in f.get("params", [])}
            names = {f["name"] for f in group}
            for m in _CALLEE.finditer(hbody):
                callee = m.group("name")
                plist = generics.get(callee)
                if callee in names or not plist or not set(plist) & member_params:
                    continue
                twin = {"name": callee,
                        "params": [{"name": n if n in member_params else None}
                                   for n in plist]}
                for k, v in _wrapper_bound(hbody, twin, drift, documented, subst,
                                           guarded, gsubst).items():
                    bound.setdefault(k, v)
                if bound or guarded:
                    break
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
           sig.get("ret"), bool(sig.get("retSet")))
    for w in claimed:
        for s in w2sig.get(w) or ():
            if (s["sqlName"], tuple(s["args"]), s["ret"], s["retSet"]) == key:
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
                out.update({k: lit for k, (runs, lit) in guarded.items()
                            if k in pnames and nargs not in runs})
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


# A local a wrapper sets from the class of a temporal type: `interp =
# temptype_supports_linear(temptype) ? LINEAR : STEP`.
_TYPE_CHOICE = r"(?<![\w.>]){var}\s*=(?!=)\s*temptype_supports_linear\s*\(\s*(\w+)\s*\)" \
               r"\s*\?\s*(\w+)\s*:\s*(\w+)\s*;"
# The temporal type of the call, read from its return or from argument i:
# `temptype = oid_meostype(get_fn_expr_rettype(fcinfo->flinfo))`.
_CALL_TYPE = r"(?<![\w.>]){var}\s*=(?!=)\s*oid_meostype\s*\(\s*get_fn_expr_(rettype|argtype)" \
             r"\s*\(\s*fcinfo\s*->\s*flinfo\s*(?:,\s*(\d+)\s*)?\)\s*\)"


def _type_choice(body: str, var: str) -> tuple[int | None, str, int | None, str, str] | None:
    """``(k, source, i, linear, other)`` when local ``var`` of wrapper ``body`` is set to
    ``temptype_supports_linear(t) ? linear : other`` and ``t`` is the temporal type of the
    call's return (``source`` "ret") or of its argument ``i`` (``source`` "arg"); ``k`` is
    the SQL argument the wrapper otherwise reads ``var`` from (#_caller_index), None when it
    reads none."""
    m = re.search(_TYPE_CHOICE.format(var=re.escape(var)), body)
    if not m:
        return None
    t = re.search(_CALL_TYPE.format(var=re.escape(m.group(1))), body)
    if not t:
        return None
    k = _caller_index(body, var)
    return (k, "ret" if t.group(1) == "rettype" else "arg",
            int(t.group(2)) if t.group(2) else None, m.group(2), m.group(3))


def attach_type_derived_args(idl: dict, mdb_src: str | Path,
                             sql_src: str | Path | None = None,
                             meos_src: str | Path | None = None) -> tuple[dict, int]:
    """(idl, number of arguments bound) once each SQL signature carries in ``boundArgs`` the
    value a wrapper derives from the temporal type of the signature.

    A wrapper that reads no interpolation from the call takes the one the type supports,
    ``interp = temptype_supports_linear(temptype) ? LINEAR : STEP`` with ``temptype`` the
    type the function returns (``Tsequenceset_constructor_gaps``) or the type of one of
    its arguments (``Temporal_append_tinstant``), and reads argument ``k`` instead only
    when the call carries it. A signature stating at most ``k`` arguments therefore passes
    LINEAR or STEP as fixed as a literal, the class #attach_temporal_types states for its
    temporal type (``linear``, read from ``temptype_supports_linear``), so it is bound on
    the signature as #merge_boundargs binds a literal the wrapper starts a local from. Runs
    once the temporal types are attached; the wrappers are traced as #merge_boundargs
    traces them."""
    from parser.sqlfn import _meos_to_mdb, _wrapper_sql_sigs
    wrappers = extract_wrappers(mdb_src)
    m2d = _meos_to_mdb(meos_src) if meos_src else {}
    w2sig = _wrapper_sql_sigs(sql_src) if sql_src else {}
    linear = {t: rec.get("linear") for t, rec in (idl.get("temporalTypes") or {}).items()}
    n = 0
    for func in idl["functions"]:
        primary = func.get("mdbC")
        if not primary:
            continue
        ws = [primary] + [w for w in m2d.get(func["name"]) or () if w != primary]
        params = [p.get("name") for p in func.get("params", [])]
        for sig in func.get("sqlSignatures") or []:
            w = _signature_wrapper(func, sig, ws, w2sig) or ws[0]
            body = wrappers.get(w)
            if not body:
                continue
            args = _call_args(body, func["name"]) or _call_args(body, "pg_" + func["name"])
            for pos, a in enumerate(args or ()):
                if pos >= len(params) or not _IDENT.match(a):
                    continue
                choice = _type_choice(body, a)
                if choice is None:
                    continue
                k, source, i, lin, other = choice
                sargs = sig.get("args") or []
                if k is not None and len(sargs) > k:
                    continue
                t = sig.get("ret") if source == "ret" else (
                    sargs[i] if i is not None and i < len(sargs) else None)
                if t not in linear:
                    continue
                bound = sig.setdefault("boundArgs", {})
                if params[pos] not in bound:
                    bound[params[pos]] = lin if linear[t] else other
                    n += 1
    return idl, n


# A local a wrapper takes from argument k unless it is NULL:
# `quadbin = PG_ARGISNULL(1) ? 0 : PG_GETARG_QUADBIN(1)`.
_ISNULL_CHOICE = r"(?<![\w.>]){var}\s*=(?!=)\s*PG_ARGISNULL\s*\(\s*(\d+)\s*\)\s*\?\s*([^:;]+?)\s*:"
# A local stating whether argument k is given: `has_nodata = ! PG_ARGISNULL(4)`.
_PRESENCE = re.compile(r"^(!\s*)?PG_ARGISNULL\s*\(\s*(\d+)\s*\)$")
# A local a wrapper takes from a presence flag:
# `nodataval = has_nodata ? PG_GETARG_FLOAT8(4) : 0.0`.
_FLAG_CHOICE = r"(?<![\w.>]){var}\s*=(?!=)\s*(\w+)\s*\?\s*([^:;]+?)\s*:\s*([^;]+?)\s*;"


def _presence_flag(body: str, var: str) -> tuple[int, str] | None:
    """(k, value) when the local ``var`` of wrapper ``body`` states whether SQL argument ``k``
    is given, every assignment of it reading ``! PG_ARGISNULL(k)`` (value ``true``) or
    ``PG_ARGISNULL(k)`` (value ``false``), the value being the one it holds when the argument
    is given; None otherwise. ``Raster_reclass`` passes ``has_nodata = ! PG_ARGISNULL(4)``
    beside the value of argument 4, so one SQL argument feeds two C parameters."""
    rhs = [m.group(1).strip() for m in
           re.finditer(r"(?<![\w.>])" + re.escape(var) + r"\s*=(?!=)\s*([^;]+);", body)]
    found = {(int(m.group(2)), "true" if m.group(1) else "false")
             for m in (_PRESENCE.match(r) for r in rhs) if m}
    if len(found) == 1 and len(rhs) == 1:
        return found.pop()
    return None


# A condition whose value no argument changes: `false`, or `5 >= 4` once the number of
# arguments is known (#_fold_nargs).
_CONST_COND = re.compile(r"^\s*(?:(?P<b>true|false)|(?P<a>\d+)\s*(?P<op>==|!=|>=|<=|>|<)\s*"
                         r"(?P<c>\d+))\s*$")


def _fold_nargs(body: str, nargs: int) -> str:
    """``body`` read for a call of ``nargs`` arguments, as #_guards reads the statements a
    number of arguments runs: ``PG_NARGS()`` as that number, a sum or difference of two
    numbers as its value, and a local set once to a number as that number where it indexes
    an argument, so ``int s = PG_NARGS() - 2`` makes ``PG_ARGISNULL(s)`` read
    ``PG_ARGISNULL(3)`` for five arguments."""
    b = re.sub(r"PG_NARGS\s*\(\s*\)", str(nargs), _COMMENT.sub(" ", body))
    arith = re.compile(r"(?<![\w.])(\d+)\s*([-+])\s*(\d+)(?![\w.])")
    while True:
        m = arith.search(b)
        if not m:
            break
        v = int(m.group(1)) + (int(m.group(3)) if m.group(2) == "+" else -int(m.group(3)))
        b = b[:m.start()] + str(v) + b[m.end():]
    for m in re.finditer(r"(?<![\w.>])(\w+)\s*=(?!=)\s*(\d+)\s*;", b):
        var = re.escape(m.group(1))
        if len(re.findall(r"(?<![\w.>])" + var + r"\s*=(?!=)", b)) == 1:
            b = re.sub(r"(PG_ARGISNULL|PG_GETARG_\w+)\s*\(\s*" + var + r"\s*\)",
                       lambda x, v=m.group(2): f"{x.group(1)}({v})", b)
    return b


def _if_spans(body: str):
    """``(condition, then_span, else_span or None)`` for every ``if`` of ``body``, its
    statements delimited as #_guards delimits them (#_statement_end)."""
    for m in re.finditer(r"\bif\s*\(", body):
        close = _close_paren(body, m.end() - 1)
        j = _skip_space(body, close + 1)
        end = _statement_end(body, j)
        e = _skip_space(body, end)
        other = None
        if re.match(r"else\b", body[e:]):
            k = _skip_space(body, e + 4)
            other = (k, _statement_end(body, k))
        yield body[m.end():close], (j, end), other


def _notnull_default(body: str, var: str, nargs: int) -> tuple[int, str] | None:
    """``(k, literal)`` when, for a call of ``nargs`` arguments (#_fold_nargs), the local
    ``var`` starts from ``literal`` and every later write of it, an assignment or its
    address handed on, runs only where SQL argument ``k`` is not NULL: under a test
    conjoining ``! PG_ARGISNULL(k)``, or in the ``else`` of ``PG_ARGISNULL(k)``. It is the
    sibling of #_guarded_default, whose later assignments run only for some numbers of
    arguments. A statement a condition no argument changes never runs is no write
    (``if (false)``, the branch an argument helper takes for a scalar search value).
    ``Raster_value_count_rows`` starts ``searchvalues`` from NULL and ``nsearch`` from 0
    and reads them from argument ``PG_NARGS() - 2`` only when it is not NULL, so a NULL
    argument passes NULL and 0."""
    b = _fold_nargs(body, nargs)
    v = re.escape(var)
    writes = [(m.start(), m.group().startswith("&")) for m in
              re.finditer(r"(?<![\w.>])" + v + r"\s*=(?!=)|&\s*" + v + r"\b", b)]
    if len(writes) < 2 or writes[0][1]:
        return None
    # an initialiser ends at the comma of a declaration of several locals, `int nrows = 0,
    # ncols = 0;`, as at its semicolon, as #_guarded_default reads it
    init = re.match(r"\s*([^;,]+?)\s*[;,]", b[b.index("=", writes[0][0]) + 1:])
    lit = _literal(init.group(1)) if init else None
    if lit is None:
        return None
    dead, given = [], {}
    for cond, then, other in _if_spans(b):
        c = _CONST_COND.match(cond)
        if c:
            holds = (c.group("b") == "true" if c.group("b") else
                     _NARGS_OPS[c.group("op")](int(c.group("a")), int(c.group("c"))))
            dead.append(other if holds else then)
            continue
        for conj in _split_top(cond, "&&"):
            g = re.fullmatch(r"\s*!\s*PG_ARGISNULL\s*\(\s*(\d+)\s*\)\s*", conj)
            if g:
                given.setdefault(int(g.group(1)), []).append(then)
        g = re.fullmatch(r"\s*PG_ARGISNULL\s*\(\s*(\d+)\s*\)\s*", cond)
        if g and other:
            given.setdefault(int(g.group(1)), []).append(other)

    # whether the position lies in one of the spans, as #_runs_at tests a guard
    def inside(pos, spans):
        return any(s is not None and s[0] <= pos < s[1] for s in spans)

    if inside(writes[0][0], dead) or any(inside(writes[0][0], s) for s in given.values()):
        return None
    live = [p for p, _ in writes[1:] if not inside(p, dead)]
    for k, spans in given.items():
        if live and all(inside(p, spans) for p in live):
            return k, lit
    return None


def _null_value(body: str, var: str, k: int, sig: dict, linear: dict) -> str | None:
    """The literal local ``var`` of wrapper ``body`` holds when SQL argument ``k`` is NULL,
    or None when the wrapper states none: the literal it starts from where every assignment
    replacing it for the number of arguments of ``sig`` reads argument ``k``
    (#_guarded_default), the interpolation of the signature's temporal type it takes when
    the call carries no ``k`` (#_type_choice), the literal of
    ``var = PG_ARGISNULL(k) ? literal : ...``, the value of a presence flag of ``k``
    (#_presence_flag) when the argument is not given, the literal of the branch
    ``var = flag ? ... : ...`` takes when that flag states it is not given, or the literal
    it starts from where it is replaced only when ``k`` is not NULL (#_notnull_default)."""
    nd = _notnull_default(body, var, len(sig.get("args") or ()))
    if nd is not None:
        return nd[1] if nd[0] == k else None
    flag = _presence_flag(body, var)
    if flag is not None:
        return ("false" if flag[1] == "true" else "true") if flag[0] == k else None
    m = re.search(_FLAG_CHOICE.format(var=re.escape(var)), body)
    if m:
        f = _presence_flag(body, m.group(1))
        if f is not None and f[0] == k:
            return _literal((m.group(3) if f[1] == "true" else m.group(2)).strip())
    g = _guarded_default(body, var)
    if g is not None:
        nargs = len(sig.get("args") or ())
        if nargs in g[0] and _caller_index(body, var, nargs=nargs) == k:
            return g[1]
    c = _type_choice(body, var)
    if c is not None and c[0] == k:
        _, source, i, lin, other = c
        sargs = sig.get("args") or []
        t = sig.get("ret") if source == "ret" else (
            sargs[i] if i is not None and i < len(sargs) else None)
        if t in linear:
            return lin if linear[t] else other
    m = re.search(_ISNULL_CHOICE.format(var=re.escape(var)), body)
    if m and int(m.group(1)) == k:
        return _literal(m.group(2).strip())
    return None


def attach_null_default_binds(idl: dict, mdb_src: str | Path, sql_src: str | Path,
                              meos_src: str | Path | None = None) -> tuple[dict, int]:
    """(idl, number of positions stated) once each SQL signature states in
    ``nullDefaultBinds`` what its wrapper passes for an argument left to a NULL default.

    A SQL argument declared ``DEFAULT NULL`` reaches the wrapper as NULL when the call
    leaves it out, and the wrapper chooses what the MEOS function receives:
    ``tintSeqSetGaps(tint[], maxt interval DEFAULT NULL, maxdist float DEFAULT NULL)``
    passes ``maxt`` NULL and ``maxdist`` -1.0 (#_null_value). For each such position ``k``
    of a signature whose CREATE FUNCTION is not STRICT (a STRICT one answers NULL without
    calling the wrapper), ``nullDefaultBinds[k]`` maps every C parameter argument ``k``
    feeds (#_caller_index) to that value, stated only when the wrapper gives every one of
    them a value. A parameter stating whether the argument is given (#_presence_flag) is
    one the argument feeds too, and ``presentBinds[k]`` states the value it takes when the
    argument is given: ``reclass(..., nodataval float DEFAULT NULL)`` passes ``nodataval``
    0.0 and ``has_nodata`` false when it is NULL, the argument and ``has_nodata`` true
    otherwise. Runs once the temporal types are attached; the wrappers are traced as
    #merge_boundargs traces them."""
    from parser.sqlfn import _meos_to_mdb, _wrapper_sql_sigs, _wrapper_sql_strict
    wrappers = extract_wrappers(mdb_src)
    helpers = extract_helpers(mdb_src)
    m2d = _meos_to_mdb(meos_src) if meos_src else {}
    w2sig = _wrapper_sql_sigs(sql_src)
    w2strict = _wrapper_sql_strict(sql_src)
    linear = {t: rec.get("linear") for t, rec in (idl.get("temporalTypes") or {}).items()}
    n = 0
    for func in idl["functions"]:
        primary = func.get("mdbC")
        if not primary:
            continue
        ws = [primary] + [w for w in m2d.get(func["name"]) or () if w != primary]
        params = [p.get("name") for p in func.get("params", [])]
        for sig in func.get("sqlSignatures") or []:
            nulls = [k for k, d in enumerate(sig.get("argDefaults") or [])
                     if d is not None and d.strip().upper() == "NULL"]
            if not nulls:
                continue
            w = _signature_wrapper(func, sig, ws, w2sig) or ws[0]
            key = (sig.get("sqlName") or func.get("sqlfn"), tuple(sig.get("args") or ()),
                   sig.get("ret"), bool(sig.get("retSet")))
            body = wrappers.get(w)
            if not body or (w2strict.get(w) or {}).get(key, True):
                continue
            args = _call_args(body, func["name"]) or _call_args(body, "pg_" + func["name"])
            if not args:
                # the wrapper hands its call info to a shared helper calling the function,
                # as merge_boundargs follows it (#_delegated)
                for d in _DELEG.finditer(body):
                    if d.group("name") in helpers:
                        body = helpers[d.group("name")][0]
                        args = (_call_args(body, func["name"]) or
                                _call_args(body, "pg_" + func["name"]))
                        break
            if not args:
                continue
            nargs = len(sig.get("args") or ())
            fed, flags = {}, {}
            for pos, a in enumerate(args):
                if pos < len(params) and _IDENT.match(a):
                    k = _caller_index(body, a, nargs=nargs)
                    if k is None:
                        flag = _presence_flag(body, a)
                        if flag is not None:
                            k = flag[0]
                            flags.setdefault(k, {})[params[pos]] = flag[1]
                    if k is None:
                        nd = _notnull_default(body, a, nargs)
                        if nd is not None:
                            k = nd[0]
                    if k is not None:
                        fed.setdefault(k, []).append((params[pos], a))
            binds, present = {}, {}
            for k in nulls:
                vals = {p: _null_value(body, a, k, sig, linear) for p, a in fed.get(k, ())}
                if vals and all(v is not None for v in vals.values()):
                    binds[str(k)] = vals
                    if k in flags:
                        present[str(k)] = flags[k]
            if binds:
                sig["nullDefaultBinds"] = binds
                n += len(binds)
            if present:
                sig["presentBinds"] = present
    return idl, n


# The SQL argument a wrapper reads: `PG_GETARG_<T>(k)`, or a helper handed the call info
# with the index, as `input_interp_string(fcinfo, 1)` reads argument 1.
_GETARG = re.compile(r"PG_GETARG_\w+\s*\(\s*(\d+)\s*\)|\bfcinfo\s*,\s*(\d+)\s*\)")
# A leading C cast, `(TInstant **) temparr_extract(...)`.
_CAST = re.compile(r"^\(\s*[\w\s*]+\)\s*")


def _direct_indices(expr: str) -> set[int]:
    return {int(a or b) for a, b in _GETARG.findall(expr)}


def _assigned_from_argument(body: str, var: str) -> bool:
    """True when an assignment of the local ``var`` in the wrapper ``body`` reads an SQL
    argument itself, as ``option = PG_GETARG_INT32(1)`` does, rather than deriving its value
    from other locals, as ``srs = get_srs_cache_by_srid(fcinfo, srid, true)`` does: the first
    step #_caller_index takes, without the walk through other locals it then makes. An
    argument read by a running index, as ``sorigin = PG_GETARG_GSERIALIZED_P(i++)``, reads
    one too."""
    rhs = re.finditer(r"(?<![\w.>])" + re.escape(var) + r"\s*=(?!=)\s*([^;]+);", body)
    return any(_direct_indices(m.group(1)) or "PG_GETARG" in m.group(1) for m in rhs)


def _directly_carried(body: str, args: list[str], assigned: set) -> set[int]:
    """The SQL arguments the call arguments ``args`` of a wrapper ``body`` carry themselves:
    an argument reading ``PG_GETARG_<T>(k)``, or a local an assignment of which reads it,
    as #_assigned_from_argument reads it, and not a value derived from such a local."""
    out: set[int] = set()
    for o in args:
        o = o.strip()
        if "PG_GETARG" in o or (o in assigned and _IDENT.match(o)
                                and _assigned_from_argument(body, o)):
            k = _caller_index(body, o)
            if k is not None:
                out.add(k)
    return out


def _caller_index(body: str, arg: str, depth: int = 0, nargs: int | None = None) -> int | None:
    """The SQL argument the call argument ``arg`` of a wrapper ``body`` carries, or None.

    An argument reading ``PG_GETARG_<T>(k)`` carries ``k``. A local carries the argument
    its assignments read directly, a later ``interp = input_interp_string(fcinfo, 1)``
    over the default it starts from; a local read from other locals only, as ``instants``
    from ``temparr_extract(array, &count)``, carries what those locals carry, as
    #_wrapper_bound reads an assigned local as caller-sourced. One argument, else None.
    Given ``nargs``, only the assignments a call of that many arguments runs are read
    (#_guards): ``Temporal_app_tinst_transfn`` reads ``maxt`` from argument 3 for four
    arguments and from argument 4 for five."""
    arg = _CAST.sub("", arg.strip())
    found = _direct_indices(arg)
    if found:
        return found.pop() if len(found) == 1 else None
    if not _IDENT.match(arg) or depth > 3:
        return None
    rhs = [m.group(1) for m in
           re.finditer(r"(?<![\w.>])" + re.escape(arg) + r"\s*=(?!=)\s*([^;]+);", body)
           if nargs is None or nargs in _runs_at(body, m.start())]
    direct = set().union(*(_direct_indices(r) for r in rhs)) if rhs else set()
    if direct:
        return direct.pop() if len(direct) == 1 else None
    via = set()
    for r in rhs:
        for ident in set(re.findall(r"\b[a-z_]\w*\b", r)) - {arg}:
            if re.search(r"(?<![\w.>])" + re.escape(ident) + r"\s*=(?!=)", body):
                k = _caller_index(body, ident, depth + 1, nargs)
                if k is not None:
                    via.add(k)
    return via.pop() if len(via) == 1 else None


def extract_meos_bodies(meos_src: str | Path) -> dict[str, str]:
    """``{function: body_text}`` for every documented MEOS definition under ``meos_src``,
    read with the definition pattern #extract_param_lists reads."""
    from parser.outparam import _FUNC
    out: dict[str, str] = {}
    for f in sorted(Path(meos_src).rglob("*.c")):
        text = f.read_text(errors="ignore")
        for m in _FUNC.finditer(text):
            out.setdefault(m.group("name"), _COMMENT.sub(" ", _body(text, m.end() - 1)))
    return out


def _kernel_args(body: str, func: dict, meos_body: str | None) -> list[str | None] | None:
    """The call arguments of the wrapper ``body`` standing for the parameters of ``func``,
    one per parameter, where the two meet at a shared kernel rather than the wrapper calling
    ``func``, one hop down as #_delegated follows a wrapper into its shared helper:
    ``Distance_value_set`` calls ``distance_set_value(s, value)`` over the value it reads
    first, and ``distance_set_int`` calls the same kernel over ``s`` and its integer ``i``
    converted, so ``i`` stands where the wrapper passes ``value``. The kernel is the first
    callee of ``meos_body`` the wrapper calls with as many arguments; a parameter named in no
    argument of that call, or in two, stands for None."""
    if not meos_body:
        return None
    names = [p["name"] for p in func.get("params", [])]
    for m in _CALLEE.finditer(meos_body):
        margs = _call_args(meos_body, m.group("name"))
        wargs = _call_args(body, m.group("name"))
        if not margs or not wargs or len(margs) != len(wargs):
            continue
        out: list[str | None] = []
        for n in names:
            pos = [j for j, a in enumerate(margs) if n in re.findall(r"\b[A-Za-z_]\w*\b", a)]
            out.append(wargs[pos[0]] if len(pos) == 1 else None)
        if any(a is not None for a in out):
            return out
    return None


def _expr_caller_index(body: str, expr: str) -> int | None:
    """The SQL argument an expression handed to the kernel carries, the one argument the
    locals it reads carry (#_caller_index): ``Raster_make`` passes
    ``(const uint8_t *) VARDATA_ANY(pixels)``, which carries argument 0 through
    ``pixels = PG_GETARG_BYTEA_PP(0)``. None for a bare local, which #_caller_index reads,
    and where the locals carry no argument or several."""
    expr = _CAST.sub("", expr.strip())
    if _IDENT.match(expr):
        return None
    found = set()
    for ident in set(re.findall(r"\b[a-z_]\w*\b", expr)):
        if re.search(r"(?<![\w.>])" + re.escape(ident) + r"\s*=(?!=)", body):
            k = _caller_index(body, ident)
            if k is not None:
                found.add(k)
    return found.pop() if len(found) == 1 else None


def _sql_arg_params(body: str, func: dict, meos_body: str | None = None) -> list[str] | None:
    """The C parameters of ``func`` in the order of the SQL arguments the wrapper ``body``
    reads for them, or None when that is their C order or cannot be read: the call
    arguments carrying SQL arguments 0 to n-1, one each. They are read off the wrapper's call
    of ``func``, else off the kernel the wrapper and ``meos_body``, the body of ``func``,
    both call (#_kernel_args)."""
    args = _call_args(body, func["name"]) or _kernel_args(body, func, meos_body)
    if not args:
        return None
    params = func.get("params", [])
    # The length of an input array travels with it, so it carries no argument of its own
    lengths = {(a.get("lengthFrom") or {}).get("name")
               for a in (func.get("shape") or {}).get("inputArrays") or []}
    by_k: dict[int, str] = {}
    for a, p in zip(args, params):
        if a is None or a.strip().startswith("&") or _literal(a.strip()) is not None \
                or p["name"] in lengths:
            continue
        k = _caller_index(body, a)
        if k is None:
            k = _expr_caller_index(body, a)
        if k is not None:
            if k in by_k:
                return None
            by_k[k] = p["name"]
    if not by_k or sorted(by_k) != list(range(len(by_k))):
        return None
    order = [by_k[k] for k in sorted(by_k)]
    in_c = [p["name"] for p in params if p["name"] in order]
    return order if order != in_c else None


def merge_sql_arg_params(idl: dict, mdb_src: str | Path,
                         sql_src: str | Path | None = None,
                         meos_src: str | Path | None = None) -> tuple[dict, int]:
    """(idl, count): the C parameters of a function in the order of the SQL arguments its
    wrapper reads for them, where that is not their C order: ``tgeogpointSeq(tgeogpoint[],
    text, boolean, boolean)`` reads the instants, the interpolation and the two inclusions,
    while ``tsequence_make`` takes the instants, their count, the two inclusions and then the
    interpolation. A binding pairing the SQL arguments with the C parameters by position
    pairs them by these names instead.

    Each SQL signature is traced to the wrapper whose CREATE FUNCTION states it, as
    #merge_boundargs traces its literals: ``shape.sqlArgParams`` holds the order when every
    signature reads the same one, else each signature that reads another carries its own
    ``sqlArgParams`` (``Concat_jsonb_jsonbset`` reads the jsonb first and
    ``Concat_jsonbset_jsonb`` the set, over one ``concat_jsonbset_jsonb``). Stated for the
    exception only, as ``boundArgs`` is: where it is absent, the SQL arguments follow the C
    parameters."""
    from parser.sqlfn import _meos_to_mdb, _wrapper_sql_sigs
    wrappers = extract_wrappers(mdb_src)
    helpers = extract_helpers(mdb_src)
    m2d = _meos_to_mdb(meos_src) if meos_src else {}
    w2sig = _wrapper_sql_sigs(sql_src) if sql_src else {}
    bodies = extract_meos_bodies(meos_src) if meos_src else {}

    def reading_body(w: str, func: dict) -> str:
        # a wrapper calling no MEOS function of its own reads its arguments in the shared
        # helper it delegates to, as the call makes it run, as #merge_boundargs reads the
        # literals there: `Stbox_constructor_x` reads xmin, ymin, xmax, ymax, srid
        body = wrappers[w]
        if _call_args(body, func["name"]) or _call_args(body, "pg_" + func["name"]):
            return body
        hbody, _, _ = _delegated(body, helpers)
        return hbody or body

    n = 0
    for func in idl["functions"]:
        primary = func.get("mdbC")
        if not primary:
            continue
        ws = [primary] + [w for w in m2d.get(func["name"]) or () if w != primary]
        sigs = func.get("sqlSignatures") or []
        sig_ws = [_signature_wrapper(func, s, ws, w2sig) or primary for s in sigs] or [primary]
        orders = [_sql_arg_params(reading_body(w, func), func, bodies.get(func["name"]))
                  if w in wrappers else None for w in sig_ws]
        if len({tuple(o or ()) for o in orders}) == 1:
            if orders[0]:
                func.setdefault("shape", {})["sqlArgParams"] = orders[0]
                n += 1
            continue
        for s, o in zip(sigs, orders):
            if o:
                s["sqlArgParams"] = o
                n += 1
    return idl, n


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


# A C comment, block or line.
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def extract_call_literals(meos_src: str | Path, functions: list) -> dict[str, str]:
    """``{parameter name: literal}`` for every parameter name to which MEOS's own calls of
    catalog functions pass one literal alone, read argument by argument as
    #_wrapper_bound reads a wrapper's call.

    ``geom_in(wkt, -1)``, ``pg_timestamptz_in(str, -1)`` and every other call passing a
    literal for a ``typmod`` pass ``-1``, PostgreSQL's unspecified type modifier, so
    ``typmod`` reads ``-1``. A name receiving two literals (``srid`` is passed ``0`` and
    ``SRID_UNKNOWN``) states none; an argument that is not a literal is not counted."""
    params = {f["name"]: [p["name"] for p in f.get("params", [])] for f in functions}
    seen: dict[str, set[str]] = {}
    for path in sorted(Path(meos_src).rglob("*.c")):
        text = _COMMENT.sub(" ", path.read_text(errors="ignore"))
        for m in _CALLEE.finditer(text):
            names = params.get(m.group("name"))
            if not names:
                continue
            start, depth = m.end() - 1, 0
            for i in range(start, len(text)):
                depth += (text[i] == "(") - (text[i] == ")")
                if depth == 0:
                    break
            if re.match(r"\s*\{", text[i + 1:]):
                continue                     # the definition, not a call
            for name, arg in zip(names, _split_args(text[start + 1:i])):
                lit = _literal(arg)
                if lit is not None:
                    seen.setdefault(name, set()).add(lit)
    return {name: next(iter(lits)) for name, lits in seen.items() if len(lits) == 1}


def attach_call_literals(idl: dict, meos_src: str | Path) -> tuple[dict, int]:
    """Set ``_callLiteral`` on every parameter whose name #extract_call_literals reads one
    literal for. The key is private: #_aux_specs of parser/enrich.py reads it as a trailing
    input's default and run.py removes it before writing the catalog."""
    lits = extract_call_literals(meos_src, idl.get("functions", []))
    n = 0
    for f in idl.get("functions", []):
        for p in f.get("params", []):
            if p["name"] in lits:
                p["_callLiteral"] = lits[p["name"]]
                n += 1
    return idl, n


def strip_call_literals(idl: dict) -> dict:
    """Remove the private ``_callLiteral`` #attach_call_literals sets."""
    for f in idl.get("functions", []):
        for p in f.get("params", []):
            p.pop("_callLiteral", None)
    return idl
