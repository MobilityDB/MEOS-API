"""State the SQL functions composed over the functions of another type.

A SQL function with no C symbol whose body converts its arguments and calls one other
SQL function is a composition: `eDisjoint(tpose, geometry)` is
`SELECT eDisjoint($1::tgeompoint, $2)`, a `tpose` answered by the relationship of the
temporal point it casts to. Every step is a MEOS function the catalog carries, so a
binding builds the function from the steps and never reads SQL. The catalog states each
composition in the top-level list `compositions`:

    {"sqlName": "eDisjoint", "args": ["tpose", "geometry"], "ret": "boolean", ...,
     "operands": [{"arg": 0, "casts": ["tpose_to_tpoint"]}, {"arg": 1}],
     "call": "edisjoint_tgeo_geo"}

`operands` holds one entry per argument of the call: the argument `arg` of the
composition (counting from 0) passed through the MEOS functions `casts` in order, or a
`value` the body passes as it is. `call` is the MEOS function called; a composition
without it answers the end of the cast chain of its one operand (`centroid(tpcpoint)` is
`SELECT $1::tgeompoint`). `restore` returns an answer over the cast type to the type of
argument `of`: the time of the answer, read by the MEOS function `time`, restricts that
argument through the MEOS function `at`. A split restores the column `from` of each row
the call returns into the column `column` of the row the composition returns:

    SELECT r.point, atTime($1, getTime(r.tpoint))
    FROM spaceSplit($1::tgeompoint, $2, $3, $4, $5, $6, $7) AS r

and `nearestApproachInstant(tpcpoint, geometry)` restores the instant the call returns,
`SELECT atTime($1, getTimestamp(nearestApproachInstant($1::tgeompoint, $2)))`.

The body is read from the deployed `.in.sql` statements, the surface PostgreSQL creates.
Each step resolves through the SQL signatures the catalog already carries: `$1::T` names
the function of the `CREATE CAST` from the argument's type to `T`, `f($1)` and the call
name a SQL function by name and argument types, and the step is the public MEOS function
carrying that signature. A composition whose call is itself a composition takes the
steps of that composition: `spaceSplit(tpose, xsize, sorigin, ...)`, which forwards to
`spaceSplit(tpose, xsize, 0, 0, sorigin, ...)`, states that function's cast, call and
restore over its own arguments and the two literals.

A body is a composition when it takes this form and converts an argument, restores, or
calls a composition. A body of this form with a step that resolves to no public MEOS
function stops the catalog: a binding would call something MEOS does not provide. Every
other body is no composition: one passing its arguments to a C-backed function as they
are, and a program of several steps (a set difference, a CASE, a subquery)."""
import re
from itertools import permutations

from parser.sqlfn import _c_base, _fits, _sql_ctypes, sql_signature, sql_statements
from parser.typescope import SQL_ALIASES

_SCHEMA = re.compile(r"@extschema@\.")
_CAST = re.compile(r"CREATE\s+CAST\s*\(\s*([\w.]+)\s+AS\s+([\w.]+)\s*\)\s*"
                   r"WITH\s+FUNCTION\s+([\w.]+)\s*\(([^)]*)\)", re.I)
_TOKEN = re.compile(r"\s*(?:(\$\d+)|('(?:[^']|'')*')|(-?\d+(?:\.\d+)?(?![\w.]))|"
                    r"([A-Za-z_]\w*)|(::|[(),.;]))")
_KEYWORDS = {"select", "from", "as"}
_LITERALS = {"true", "false", "null"}


def _type(t):
    """One spelling for each SQL type (`float8` and `float`), through #SQL_ALIASES of
    parser/typescope.py, without the type modifier PostgreSQL ignores when it resolves
    a function (`geometry(Point)` is `geometry`)."""
    t = re.sub(r"\s*\(.*\)$", "", t.strip().lower())
    return SQL_ALIASES.get(t, t)


class _Misfit(Exception):
    """A body outside the form of a composition."""


class _Parser:
    """The expressions of a composition body: `$k`, a literal, `f(e, ...)`, `e::T` and a
    row column `r.c`, in a `SELECT e, ... [FROM f(...) AS r]`."""

    def __init__(self, body):
        text, self.toks, i = _SCHEMA.sub("", body).strip().rstrip(";").strip(), [], 0
        while i < len(text):
            m = _TOKEN.match(text, i)
            if not m or m.end() == i:
                raise _Misfit(f"no token at {text[i:i + 20]!r}")
            arg, string, number, word, punct = m.groups()
            if arg:
                self.toks.append(("arg", int(arg[1:]) - 1))
            elif string or number:
                self.toks.append(("lit", string or number))
            elif word:
                low = word.lower()
                self.toks.append(("kw", low) if low in _KEYWORDS else
                                 ("lit", low.upper()) if low in _LITERALS else
                                 ("word", word))
            else:
                self.toks.append(("p", punct))
            i = m.end()
        self.i = 0

    def peek(self, kind=None, value=None):
        tok = self.toks[self.i] if self.i < len(self.toks) else (None, None)
        return tok if ((kind is None or tok[0] == kind)
                       and (value is None or tok[1] == value)) else None

    def take(self, kind, value=None):
        tok = self.peek(kind, value)
        if tok is None:
            raise _Misfit(f"expected {value or kind}")
        self.i += 1
        return tok[1]

    def expr(self):
        if self.peek("arg"):
            node = ("arg", self.take("arg"))
        elif self.peek("lit"):
            node = ("lit", self.take("lit"))
        else:
            name = self.take("word")
            if self.peek("p", "."):
                self.take("p", ".")
                node = ("col", name, self.take("word"))
            else:
                self.take("p", "(")
                args = []
                while not self.peek("p", ")"):
                    args.append(self.expr())
                    if not self.peek("p", ")"):
                        self.take("p", ",")
                self.take("p", ")")
                node = ("call", name, args)
        while self.peek("p", "::"):
            self.take("p", "::")
            node = ("cast", node, self.take("word"))
        return node

    def statement(self):
        """(select list, FROM call or None, FROM alias or None)."""
        self.take("kw", "select")
        items = [self.expr()]
        while self.peek("p", ","):
            self.take("p", ",")
            items.append(self.expr())
        source = alias = None
        if self.peek("kw", "from"):
            self.take("kw", "from")
            source = self.expr()
            self.take("kw", "as")
            alias = self.take("word")
        if self.i != len(self.toks):
            raise _Misfit("text after the statement")
        return items, source, alias


class _Resolver:
    """Names each step of a composition by the public MEOS function carrying its SQL
    signature."""

    def __init__(self, idl, stmts, vocab, composites, text_casts):
        self.by_sig, self.sqlc = {}, _sql_ctypes(idl)
        for f in idl.get("functions", []):
            for s in f.get("sqlSignatures") or ():
                name = (s.get("sqlName") or f.get("sqlfn") or "").lower()
                self.by_sig.setdefault(name, []).append((f, s))
        self.casts = text_casts
        self.bodies = {}
        for sqlname, argdecls, ret, wrapper, retset, body in stmts:
            if wrapper is None and body:
                sig = sql_signature(sqlname, argdecls, ret, retset, vocab, composites)
                self.bodies.setdefault(sqlname.lower(), []).append((sig, body))
        # The SQL functions MobilityDB declares: those of the statements read and those
        # whose signatures the catalog carries, read from the same statements.
        self.declared = {stmt[0].lower() for stmt in stmts} | set(self.by_sig)
        self.done, self.active = {}, set()

    def foreign(self, node):
        """Whether expression `node` calls a function MobilityDB does not declare (a
        PostGIS function such as `ST_Transform`): a body calling one composes over
        PostGIS, whose functions MEOS does not carry."""
        if node[0] == "call":
            return (node[1].lower() not in self.declared
                    or any(self.foreign(a) for a in node[2]))
        return node[0] == "cast" and self.foreign(node[1])

    @staticmethod
    def _fits(sig, types):
        """Whether a call passing arguments of `types` (None for a literal) reaches
        `sig`, its defaults allowing fewer."""
        args, dflts = sig["args"], sig.get("argDefaults") or [None] * len(sig["args"])
        if not len(types) <= len(args) or any(d is None for d in dflts[len(types):]):
            return False
        return all(t is None or _type(t) == _type(a) for t, a in zip(types, args))

    def function(self, name, types):
        """(MEOS function, its SQL signature) of the C-backed SQL function `name` over
        `types`, or None when no C-backed signature fits. Raises when two signatures
        fit, or when the one that fits is carried by no public MEOS function or by two."""
        fits = {}
        for f, s in self.by_sig.get(name.lower(), ()):
            if self._fits(s, types):
                fits.setdefault(tuple(s["args"]), []).append(f)
        if not fits:
            return None
        if len(fits) > 1:
            raise _Misfit(f"{name}({', '.join(t or '?' for t in types)}) fits "
                          + "; ".join(f"({', '.join(a)})" for a in fits))
        (args, funcs), = fits.items()
        public = [f for f in funcs if f.get("api") == "public"]
        if len(public) != 1:
            raise _Misfit(f"{name}({', '.join(args)}) is carried by "
                          f"{len(public)} public MEOS functions")
        sig = next(s for f, s in self.by_sig[name.lower()]
                   if f is public[0] and tuple(s["args"]) == args)
        return public[0], sig

    def params(self, func, sig, ops):
        """The operands of a call of `func` through `sig`, one per C input parameter of
        `func` in its order, each naming the parameter it feeds, `param`. A literal
        `sig` binds (`boundArgs`) is a `value` operand. The others are `ops`, in order
        when every operand's SQL type fits its parameter's C type (#_fits of
        parser/sqlfn.py), else in the one order in which each fits, as a signature
        whose wrapper passes its arguments in another order than the C function takes
        them (`nearestApproachDistance(geometry, tgeompoint)` of `nad_tgeo_geo(temp,
        gs)`). Arguments the call leaves out take the defaults of `sig`."""
        shape = func.get("shape") or {}
        bound = sig.get("boundArgs") or shape.get("boundArgs") or {}
        out = set(shape.get("outParams") or ())
        inputs = [p for p in func.get("params") or ()
                  if p["name"] not in out and p["name"] not in bound]
        dflts = sig.get("argDefaults") or [None] * len(sig["args"])
        ops = ops + [{"value": dflts[i], "type": None} for i in range(len(ops), len(sig["args"]))]
        if len(ops) != len(inputs):
            raise _Misfit(f"{func['name']} takes {len(inputs)} arguments, the call "
                          f"passes {len(ops)}")
        types = [o["type"] or a for o, a in zip(ops, sig["args"])]

        def fit(order):
            return all(_fits(_type(t), *_c_base(p.get("cType")), self.sqlc)
                       for t, p in zip(types, order))
        if fit(inputs):
            order = inputs
        else:
            orders = [o for o in permutations(inputs) if fit(o)]
            if len(orders) != 1:
                raise _Misfit(f"{func['name']}({', '.join(types)}): {len(orders)} "
                              f"orders of its parameters fit")
            order = orders[0]
        fed = {p["name"]: {**o, "param": p["name"]} for o, p in zip(ops, order)}
        return [fed.get(p["name"]) or {"value": bound[p["name"]], "type": None,
                                       "param": p["name"]}
                for p in func.get("params") or () if p["name"] not in out]

    def composition(self, name, types):
        """The composition `name` over `types` states, or None when no body fits."""
        fits = [(sig, body) for sig, body in self.bodies.get(name.lower(), ())
                if self._fits(sig, types)]
        if len(fits) != 1:
            return None
        sig, body = fits[0]
        return self.compose(sig, body)

    def compose(self, sig, body):
        """The composition entry of `sig` with `body`, None for a body that is no
        composition. Raises _Misfit for a composition whose steps do not resolve."""
        key = (sig["sqlName"].lower(), tuple(sig["args"]))
        if key in self.done:
            return self.done[key]
        if key in self.active:
            raise _Misfit("calls itself")
        self.active.add(key)
        try:
            entry = self._compose(sig, body)
        finally:
            self.active.discard(key)
        self.done[key] = entry
        return entry

    def step(self, operand, name, conv):
        """`operand` passed through the SQL function `name` of one argument (a cast when
        `conv` names its target type): the MEOS function, appended to its casts."""
        src = operand["type"]
        if conv:
            if _type(src) == _type(name):
                return operand                                  # a cast to its own type
            fn = self.casts.get((_type(src), _type(name)))
            if fn is None:
                raise _Misfit(f"no CREATE CAST from {src} to {name}")
            name = fn
        found = self.function(name, [src])
        if found is None:
            raise _Misfit(f"{name}({src}) has no C-backed signature")
        return {**operand, "casts": operand.get("casts", []) + [found[0]["name"]],
                "type": found[1]["ret"]}

    def operand(self, node, sig):
        """The operand an argument expression of a call states: {arg, casts, type} or
        {value, type: None}."""
        kind = node[0]
        if kind == "arg":
            if node[1] >= len(sig["args"]):
                raise _Misfit(f"${node[1] + 1} beyond the arguments")
            return {"arg": node[1], "type": sig["args"][node[1]]}
        if kind == "lit":
            return {"value": node[1], "type": None}
        if kind == "cast":
            return self.step(self.operand(node[1], sig), node[2], conv=True)
        if kind == "call" and len(node[2]) == 1:
            return self.step(self.operand(node[2][0], sig), node[1], conv=False)
        raise _Misfit("an argument computed by more than a cast chain")

    def call(self, node, sig):
        """(operands, callee MEOS function or None, callee return, callee columns,
        restore of the callee's own composition) of the call `node` over `sig`'s
        arguments. A callee that is a composition lends its steps."""
        name, args = node[1], node[2]
        ops = [self.operand(a, sig) for a in args]
        types = [o["type"] for o in ops]
        found = self.function(name, types)
        if found is not None:
            func, csig = found
            return (self.params(func, csig, ops), func["name"], csig["ret"],
                    csig.get("columns"), None)
        inner = self.composition(name, types)
        if inner is None:
            raise _Misfit(f"{name}({', '.join(t or '?' for t in types)}) resolves to "
                          f"neither a C-backed signature nor a composition")
        merged = []
        for o in inner["operands"]:
            if "value" in o:
                merged.append(dict(o))
                continue
            outer = ops[o["arg"]]
            both = outer.get("casts", []) + o.get("casts", [])
            if "value" in outer:
                if both:
                    raise _Misfit("a literal passed through a cast")
                merged.append({"value": outer["value"], "param": o["param"]})
            else:
                merged.append({"arg": outer["arg"], **({"casts": both} if both else {}),
                               "param": o["param"]})
        restore = inner.get("restore")
        if restore:
            outer = ops[restore["of"]]
            if "value" in outer or outer.get("casts"):
                raise _Misfit("a restore of an argument the call converts")
            restore = {**restore, "of": outer["arg"]}
        return merged, inner.get("call"), inner["ret"], inner.get("columns"), restore

    def _compose(self, sig, body):
        try:
            items, source, alias = _Parser(body).statement()
        except _Misfit:
            return None                                         # a program, not a form
        if any(self.foreign(n) for n in items + ([source] if source else [])):
            return None                                         # composed over PostGIS
        restore = None
        if source is not None:
            # SELECT r.a, [r.b, ...] at($k, time(r.c)) FROM f(...) AS r
            if source[0] != "call":
                return None
            ops, fn, cret, ccols, inner_restore = self.call(source, sig)
            if inner_restore or not ccols or len(items) != len(ccols):
                raise _Misfit("a restore over a call whose rows it does not match")
            cnames = [c["name"] if isinstance(c, dict) else c[0] for c in ccols]
            ctypes = [c["type"] if isinstance(c, dict) else c[1] for c in ccols]
            onames = [c[0] if isinstance(c, (list, tuple)) else c["name"]
                      for c in (sig.get("columns") or ())]
            for pos, item in enumerate(items):
                if item == ("col", alias, cnames[pos]):
                    continue
                if restore is not None:
                    raise _Misfit("two restored columns")
                restore = self.restore(item, sig, ("col", alias, cnames[pos]), ctypes[pos])
                restore = {"column": onames[pos] if pos < len(onames) else None,
                           "from": cnames[pos], **restore}
            if restore is None:
                return None                                     # a pass-through query
        else:
            if len(items) != 1:
                return None
            top = items[0]
            scalar = self.scalar_restore(top, sig)
            if scalar is not None:
                ops, fn, cret, restore = scalar
            elif top[0] == "call":
                if not any(self.converts(a) for a in top[2]):
                    # No argument is converted: a forward, unless it reaches a composition.
                    try:
                        if self.function(top[1], [self.operand(a, sig)["type"]
                                                  for a in top[2]]) is not None:
                            return None
                    except _Misfit:
                        return None
                ops, fn, cret, _, restore = self.call(top, sig)
            elif top[0] in ("arg", "cast"):
                ops, fn = [self.operand(top, sig)], None
                if not ops[0].get("casts"):
                    return None
                cret = ops[0]["type"]
            else:
                return None
        # A call reaching a composition takes its casts, so it converts as well.
        if not (any(o.get("casts") for o in ops) or restore):
            return None                                         # a plain forward
        used = {o["arg"] for o in ops if "arg" in o} | ({restore["of"]} if restore else set())
        if used != set(range(len(sig["args"]))):
            raise _Misfit(f"arguments {sorted(set(range(len(sig['args']))) - used)} unused")
        if restore is None and _type(cret or "") != _type(sig["ret"] or ""):
            raise _Misfit(f"answers {cret} where the function returns {sig['ret']}")
        entry = {k: sig[k] for k in ("sqlName", "args", "required", "argDefaults", "ret")}
        if sig.get("retSet"):
            entry["retSet"] = True
        if sig.get("columns"):
            entry["columns"] = [{"name": n, "type": t} for n, t in sig["columns"]]
        entry["operands"] = [{k: v for k, v in o.items() if k != "type"} for o in ops]
        if fn is not None:
            entry["call"] = fn
        if restore:
            entry["restore"] = restore
        return entry

    @staticmethod
    def converts(node):
        """Whether the argument expression `node` passes an argument through a cast or
        a function of one argument."""
        return node[0] == "cast" or (node[0] == "call" and len(node[2]) == 1)

    def restore(self, node, sig, column, ctype):
        """{of, time, at} of `at($k, time(<column>))`, the answer at `column` restored to
        argument k."""
        if not (node[0] == "call" and len(node[2]) == 2 and node[2][0][0] == "arg"
                and node[2][1][0] == "call" and len(node[2][1][2]) == 1
                and node[2][1][2][0] == column):
            raise _Misfit("a column computed by more than a restore")
        k = node[2][0][1]
        time = self.function(node[2][1][1], [ctype])
        if time is None:
            raise _Misfit(f"{node[2][1][1]}({ctype}) has no C-backed signature")
        at = self.function(node[1], [sig["args"][k], time[1]["ret"]])
        if at is None:
            raise _Misfit(f"{node[1]}({sig['args'][k]}, {time[1]['ret']}) has no "
                          f"C-backed signature")
        return {"of": k, "time": time[0]["name"], "at": at[0]["name"]}

    def scalar_restore(self, node, sig):
        """(operands, callee, callee return, restore) of `at($k, time(f(...)))`, the
        value the call returns restored to argument k; None for another expression."""
        if not (node[0] == "call" and len(node[2]) == 2 and node[2][0][0] == "arg"
                and node[2][1][0] == "call" and len(node[2][1][2]) == 1
                and node[2][1][2][0][0] == "call"):
            return None
        inner = node[2][1][2][0]
        ops, fn, cret, _, inner_restore = self.call(inner, sig)
        if inner_restore:
            raise _Misfit("a restore of a restored answer")
        k = node[2][0][1]
        time = self.function(node[2][1][1], [cret])
        if time is None:
            raise _Misfit(f"{node[2][1][1]}({cret}) has no C-backed signature")
        at = self.function(node[1], [sig["args"][k], time[1]["ret"]])
        if at is None:
            raise _Misfit(f"{node[1]}({sig['args'][k]}, {time[1]['ret']}) has no "
                          f"C-backed signature")
        return ops, fn, cret, {"of": k, "time": time[0]["name"], "at": at[0]["name"]}


def _text_casts(sql_src):
    """{(source type, target type): cast function SQL name} of every `CREATE CAST ...
    WITH FUNCTION f(source)` under `sql_src`; a cast whose function takes more than the
    source (a typmod) is left out."""
    from pathlib import Path
    from parser.sqlfn import _strip_sql_comments
    out = {}
    for sf in sorted(Path(sql_src).rglob("*.sql")):
        text = _SCHEMA.sub("", _strip_sql_comments(sf.read_text(errors="ignore")))
        for src, dst, fn, fargs in _CAST.findall(text):
            if len([a for a in fargs.split(",") if a.strip()]) == 1:
                out[(_type(src), _type(dst))] = fn
    return out


def attach_compositions(idl, sql_src):
    """(idl with its top-level `compositions`, count, bodies of several steps left out).
    Raises ValueError naming every composition a step of which resolves to no public
    MEOS function."""
    stmts, vocab, composites = sql_statements(sql_src)
    res = _Resolver(idl, stmts, vocab, composites, _text_casts(sql_src))
    out, errors = [], []
    for name in sorted(res.bodies):
        for sig, body in res.bodies[name]:
            try:
                entry = res.compose(sig, body)
            except _Misfit as e:
                errors.append(f"{sig['sqlName']}({', '.join(sig['args'])}): {e}")
                continue
            if entry is not None:
                out.append(entry)
    if errors:
        raise ValueError("SQL compositions with a step no public MEOS function takes:\n  "
                         + "\n  ".join(errors))
    idl["compositions"] = out
    return idl, len(out)


_CAST_CALL = re.compile(r"^\s*(\w+)\s*\(\s*(\w+)\s*\)\s*$")
_C_LITERAL = re.compile(r"^(?:true|false|-?\d+(?:\.\d+)?)$")


def attach_wrapper_compositions(idl, mdb_src, sql_src, meos_src):
    """(idl, count): the signatures a C wrapper states by converting an argument through a
    public cast before it calls its MEOS function, moved from that function's
    ``sqlSignatures`` to the top-level ``compositions`` #attach_compositions builds.

    ``Eintersects_tpose_geo`` reads a tpose, casts it with ``tpoint = tpose_to_tpoint(temp)``
    and calls ``eintersects_tgeo_geo(tpoint, gs)``, which states the wrapper in its
    ``@csqlfn``: ``eIntersects(tpose, geometry)`` is then the composition
    ``{"operands": [{"arg": 0, "casts": ["tpose_to_tpoint"], "param": "temp"},
    {"arg": 1, "param": "gs"}], "call": "eintersects_tgeo_geo"}``, the form a SQL body
    casting its argument takes, so a binding passes the function a value of the type it
    reads. PostgreSQL keeps the C wrapper, whose planner support function an inlined SQL
    body cannot reach. Each signature is traced to the wrapper whose CREATE FUNCTION
    states it (#_signature_wrapper of parser/boundargs.py) and each argument of the call to
    the SQL argument it carries (#_caller_index); a cast is a public catalog function of
    one parameter. A C literal the wrapper passes, as ``true`` for the ``spheroid`` of
    ``edwithin_tgeo_geo``, is a value operand. A signature whose wrapper casts nothing
    stays where it is."""
    from parser.boundargs import (_call_args, _caller_index, _signature_wrapper,
                                  extract_wrappers)
    from parser.sqlfn import _meos_to_mdb, _wrapper_sql_sigs
    wrappers = extract_wrappers(mdb_src)
    m2d = _meos_to_mdb(meos_src)
    w2sig = _wrapper_sql_sigs(sql_src)
    public = {f["name"]: f for f in idl["functions"] if f.get("api") == "public"}
    out = idl.setdefault("compositions", [])
    n = 0
    for func in idl["functions"]:
        claimed = [w for w in [func.get("mdbC")] + list(m2d.get(func["name"]) or ()) if w]
        sigs = func.get("sqlSignatures") or []
        if not claimed or not sigs:
            continue
        outs = set((func.get("shape") or {}).get("outParams") or ())
        params = [p["name"] for p in func.get("params") or () if p["name"] not in outs]
        keep = []
        for sig in sigs:
            w = _signature_wrapper(func, sig, claimed, w2sig)
            body = wrappers.get(w) if w else None
            args = _call_args(body, func["name"]) if body else None
            operands = None
            if args is not None and len(args) == len(params):
                operands, cast_seen = [], False
                for a, p in zip(args, params):
                    a = a.strip()
                    rhs = [m.group(1) for m in re.finditer(
                        r"(?<![\w.>])" + re.escape(a) + r"\s*=(?!=)\s*([^;]+);", body)]
                    hit = _CAST_CALL.match(rhs[0]) if len(rhs) == 1 else None
                    cast = hit.group(1) if hit else None
                    if _C_LITERAL.match(a):
                        operands.append({"value": a, "param": p})
                    elif cast in public and len(public[cast].get("params") or ()) == 1:
                        operands.append({"arg": _caller_index(body, hit.group(2)),
                                         "casts": [cast], "param": p})
                        cast_seen = True
                    else:
                        operands.append({"arg": _caller_index(body, a), "param": p})
                if not cast_seen or any("arg" in o and o["arg"] is None for o in operands):
                    operands = None
            if operands is None:
                keep.append(sig)
                continue
            nargs = len(sig.get("args") or ())
            dflt = list(sig.get("argDefaults") or [None] * nargs)
            out.append({"sqlName": sig.get("sqlName") or func.get("sqlfn"),
                        "args": list(sig.get("args") or ()),
                        "required": sum(1 for d in dflt if d is None),
                        "argDefaults": dflt, "ret": sig.get("ret"),
                        "operands": operands, "call": func["name"]})
            n += 1
        func["sqlSignatures"] = keep
    return idl, n
