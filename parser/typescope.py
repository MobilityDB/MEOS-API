"""Derive the SQL type scope of a MEOS function.

One PG wrapper commonly backs a whole per-type family: `Set_values` is the body
behind `getValues(intset)`, `getValues(cbufferset)` and fourteen more, while the
MEOS side spells one typed function per type (`intset_values`,
`cbufferset_values`, ...). Attaching a wrapper's whole signature list to every
MEOS function that names it would tell a binding that `intset_values` serves
cbufferset — so each function's signatures are filtered to the types it actually
serves, and that set is its TYPE SCOPE.

The scope is read from what MEOS itself states, never from the function's name:

  * its own `VALIDATE_<TYPE>` macro,
  * the `MeosType` literals in its body, resolved through `meostype_name`,
  * a class predicate it calls (`tnumber_type`, `tspatial_type`, ...), whose
    members the catalog lists,
  * its C parameter types, resolved through the catalog's base-type relations.

A function whose scope none of those state is UNDERIVABLE: it is either generic
over every overload or simply unclassified, and the two are indistinguishable
from outside. Rather than guess — one guess keeps a wrong signature list, the
other drops a real registration — such a function must be listed in
`meta/type-scope.json`, and `require_scopes` fails on any that is not.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# A declared scope of "*" means the function serves every overload its wrapper
# declares — the legitimate generic case, stated rather than guessed.
EVERY_OVERLOAD = '*'

_META = Path(__file__).resolve().parent.parent / 'meta' / 'type-scope.json'

_TYPE_NAME = re.compile(r'\[(T_[A-Z0-9_]+)\]\s*=\s*"([a-z0-9_]+)"')
_PREDICATE = re.compile(r'^(\w+)\(MeosType type\)\n\{\n(.*?)\n\}', re.S | re.M)
_MEOS_TYPE = re.compile(r'\bT_[A-Z0-9_]+\b')
_VALIDATE = re.compile(r'\bVALIDATE_[A-Z0-9_]+\b')
_C_PARAM = re.compile(r'\b(?:const\s+)?(\w+)\s*\*?\s*\w+\s*[,)]')
_FN_OPEN = re.compile(r'^(\w+)\((.*)$')

# The catalog fields relating a container type to the type it is built over.
_RELATIONS = ('settype_basetype', 'spantype_basetype', 'temptype_basetype',
              'spansettype_spantype')

# `T_UNKNOWN` is the catalog's placeholder, not a type a function can serve.
_NOT_A_TYPE = {'unknown'}

# The C spelling of each MEOS base type. `GSERIALIZED` covers both geometry and
# geography, which is why a C parameter of that type widens to the pair.
C_BASE_TYPES = {
    'int': 'int4', 'int32': 'int4', 'int64': 'int8', 'double': 'float8',
    'bool': 'bool', 'text': 'text', 'DateADT': 'date',
    'TimestampTz': 'timestamptz',
    'GSERIALIZED': ('geometry', 'geography'),
}

# PostgreSQL's spelling of the MEOS base types: a SQL signature says `integer`
# where `meostype_name` says `int4`, so a scope must be compared in both.
SQL_ALIASES = {'int4': 'integer', 'int8': 'bigint', 'float8': 'float',
               'bool': 'boolean'}


def sql_spellings(types):
    """Every SQL spelling of a scope, so it can be matched against a signature."""
    return set(types) | {SQL_ALIASES[t] for t in types if t in SQL_ALIASES}


class TypeFacts:
    """The type vocabulary, class predicates and container relations, read from
    MEOS's own catalog rather than restated here."""

    def __init__(self, meos_src: str | Path):
        src = (Path(meos_src) / 'src/temporal/meos_catalog.c').read_text(errors='ignore')
        self.name = {e: n for e, n in _TYPE_NAME.findall(src) if n not in _NOT_A_TYPE}
        self.names = set(self.name.values())
        self.klass = {}
        for m in _PREDICATE.finditer(src):
            members = {self.name[t] for t in _MEOS_TYPE.findall(m.group(2))
                       if t in self.name}
            if members:
                self.klass[m.group(1)] = members
        self.container = {}
        for field in _RELATIONS:
            pat = rf'\[(T_[A-Z0-9_]+)\]\s*=\s*\{{[^}}]*\.{field}\s*=\s*(T_[A-Z0-9_]+)'
            for outer, inner in re.findall(pat, src):
                if outer in self.name and inner in self.name:
                    self.container.setdefault(self.name[inner], set()).add(self.name[outer])
        self.validate = self._read_validate_macros(Path(meos_src) / 'include')

    def _read_validate_macros(self, include_dir: Path) -> dict:
        """What each `VALIDATE_*` macro constrains its argument to, read from the
        macro's own definition. `VALIDATE_INTSET` names a single type through
        `ensure_set_isof_type(s, T_INTSET)`; `VALIDATE_TGEO` names a whole class
        through `ensure_tgeo_type_all` — a name-shaped guess would see only the
        first kind and miss every class macro."""
        out = {}
        for hf in include_dir.rglob('*.h'):
            text = hf.read_text(errors='ignore')
            for m in re.finditer(r'#\s*define\s+(VALIDATE_[A-Z0-9_]+)\((.*?)\n(?=\s*#|\s*extern|\n)',
                                 text, re.S):
                name, body = m.group(1), m.group(2)
                types = {self.name[t] for t in _MEOS_TYPE.findall(body)
                         if t in self.name}
                for pred, members in self.klass.items():
                    if re.search(rf'\b(?:ensure_)?{re.escape(pred)}\s*\(', body):
                        types |= members
                if types:
                    out.setdefault(name, set()).update(types)
        return out

    def widen(self, types: set[str]) -> set[str]:
        """A type plus every container built over it: a function stating
        `T_INT8` serves bigint, and the bigintset/bigintspan/tbigint built on
        it, transitively."""
        out, queue = set(types), list(types)
        while queue:
            for c in self.container.get(queue.pop(), ()):
                if c not in out:
                    out.add(c)
                    queue.append(c)
        return out


def read_bodies(meos_src: str | Path) -> tuple[dict, dict]:
    """Every MEOS function's body text and parameter list, keyed by name."""
    bodies, params = {}, {}
    for cf in Path(meos_src, 'src').rglob('*.c'):
        cur, buf = None, []
        for line in cf.read_text(errors='ignore').split('\n'):
            m = _FN_OPEN.match(line)
            if m:
                cur, buf = m.group(1), []
                params.setdefault(cur, m.group(2))
            if cur is not None:
                buf.append(line)
                if line == '}':
                    bodies.setdefault(cur, '\n'.join(buf))
                    cur = None
    return bodies, params


def scope_of(name: str, facts: TypeFacts, bodies: dict, params: dict,
             c_types: dict = C_BASE_TYPES) -> tuple[set | None, str]:
    """This function's type scope and the signal that states it, or
    ``(None, 'none')`` when MEOS states nothing."""
    body = bodies.get(name)
    if body is None:
        return None, 'none'

    stated = {t for macro in _VALIDATE.findall(body)
              for t in facts.validate.get(macro, ())}
    if stated:
        return facts.widen(stated), 'validate'

    literals = {facts.name[t] for t in _MEOS_TYPE.findall(body) if t in facts.name}
    if literals:
        return facts.widen(literals), 'meostype'

    members = {t for pred, types in facts.klass.items()
               if re.search(rf'\b(?:ensure_)?{re.escape(pred)}\s*\(', body)
               for t in types}
    if members:
        return members, 'class'

    # A C parameter names a base type (`int64 i` -> int8, `const Cbuffer *cb` ->
    # cbuffer), and the catalog says which containers are built over it.
    from_params = set()
    for token in _C_PARAM.findall(params.get(name, '')):
        base = c_types.get(token)
        if base is not None:
            from_params.update((base,) if isinstance(base, str) else base)
        elif token.lower() in facts.names:
            from_params.add(token.lower())
    if from_params:
        widened = facts.widen(from_params)
        if widened - from_params:
            return widened, 'cparam'

    return None, 'none'


def declared_scopes(path: str | Path = _META) -> dict:
    """The scopes stated in `meta/type-scope.json`, keyed by function name."""
    doc = json.loads(Path(path).read_text())
    return {name: entry['types'] for name, entry in doc['scopes'].items()}


def resolve_scope(name, facts, bodies, params, declared):
    """This function's scope, from MEOS's own signals or from the declared file.

    Returns ``(types, signal)`` where `types` is a set, or ``EVERY_OVERLOAD`` for
    a function declared generic, or ``None`` when nothing states it."""
    stated = declared.get(name)
    if stated is not None:
        return (EVERY_OVERLOAD if stated == EVERY_OVERLOAD else set(stated)), 'declared'
    return scope_of(name, facts, bodies, params)


def require_scopes(claimants, facts, bodies, params, declared):
    """Fail on any claimant of a shared wrapper whose scope nothing states.

    Guessing here is what the whole mechanism exists to avoid: assuming "all"
    reinstates the wrong-signature bug, assuming "none" silently drops real
    registrations. Both are invisible downstream, so an underivable claimant is
    an error the catalog refuses to emit until someone states the answer in
    `meta/type-scope.json`."""
    missing = sorted(n for n in claimants
                     if resolve_scope(n, facts, bodies, params, declared)[0] is None)
    if missing:
        raise ValueError(
            'type scope underivable for %d function(s); state each in '
            'meta/type-scope.json: %s' % (len(missing), ', '.join(missing)))


def signatures_for(name, sigs, scope):
    """The subset of a wrapper's signatures this function serves.

    A signature belongs to the function when the scope covers any type it names —
    its arguments or its return — compared in both MEOS and SQL spellings."""
    if scope == EVERY_OVERLOAD:
        return list(sigs)
    covered = sql_spellings(scope)
    kept = []
    for sig in sigs:
        named = set(sig.get('args') or ())
        if sig.get('ret'):
            named.add(sig['ret'])
        if named & covered:
            kept.append(sig)
    return kept
