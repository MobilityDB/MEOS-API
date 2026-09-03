"""Whether a MEOS function's answer can be absent, read from the PG wrapper.

A C return type says what a value LOOKS like; it never says whether there is
one.  ``bool tbool_value_at_timestamptz(..., bool *value)`` returns a value and
a flag, ``Temporal *temporal_at_timestamptz(...)`` returns a pointer that may be
NULL, and nothing in either signature distinguishes "no answer here" from "the
answer is false".  Every binding needs that distinction, and without it each one
invents a convention: a nullable in one, an exception in another, a zero value
and a crash in a third.

The PostgreSQL wrapper already states it.  Each wrapper guards
``PG_RETURN_NULL()`` with the condition under which the answer is absent, and
that guard is a TOKEN rather than prose:

    if (! result)                    a null pointer -- no value was produced
    if (! found)                     an out-parameter reporting absence itself
    if (result == DBL_MAX)           the distance sentinel
    if (result < 0)                  a three-valued predicate answering unknown
    if (count == 0)                  an empty array

So the fact is derived, never inferred from a name or a group: the wrapper is
the SQL contract, and this reads it.  The chain is the one ``parser.sqlfn``
already builds -- a MEOS function names its wrapper through ``@csqlfn``, and
``mdbC`` carries that name -- so nothing new is joined here.

``PG_ARGISNULL`` guards are EXCLUDED.  Those propagate a null ARGUMENT, which is
a statement about the input (already carried by ``shape.nullable``) rather than
about whether an answer exists.  Counting them would report a function as
absence-capable because its caller may pass nothing.

Adds ``shape.nullableResult`` -- the guard text -- to every function whose
wrapper carries one.  Its ABSENCE is as meaningful as its presence: a wrapper
with no ``PG_RETURN_NULL`` always produces a value.
"""
from __future__ import annotations

import re
from pathlib import Path

from parser.typescope import read_bodies

_RETURN_NULL = re.compile(r"\bPG_RETURN_NULL\s*\(\s*\)")
_IF_OPEN = re.compile(r"\bif\s*\(")
# An argument-null guard says nothing about whether an answer exists.
_ARGISNULL = re.compile(r"\bPG_ARGISNULL\b")
# How far above a PG_RETURN_NULL its guard may sit.  The guards in the tree are
# on the line before or, when the condition wraps, two or three lines above; a
# wider window would start attributing an unrelated earlier `if`.
_LOOKBACK = 4
# A wrapper that states no guard of its own hands `fcinfo` to a shared helper
# that does. The helper is named in the body, so the hop is read rather than
# guessed.
_DELEGATES = re.compile(r"^\s*return\s+(\w+)\s*\(\s*fcinfo\b", re.M)
# Chains are one or two deep in the tree; the cap keeps a malformed source from
# walking forever, and the visited set already stops a cycle.
_MAX_HOPS = 4


def _guard_for(lines: list[str], at: int) -> str | None:
    """The `if` condition guarding the PG_RETURN_NULL on line ``at``."""
    for back in range(0, _LOOKBACK + 1):
        i = at - back
        if i < 0:
            break
        if _IF_OPEN.search(lines[i]):
            guard = " ".join(part.strip() for part in lines[i:at + 1])
            guard = guard[_IF_OPEN.search(guard).start():]
            guard = guard.split("PG_RETURN_NULL")[0].strip()
            return re.sub(r"\s+", " ", guard) or None
    return None


def _own_guard(body: str) -> str | None:
    """The absence guard written in this body, ignoring argument-null checks."""
    lines = body.split("\n")
    for n, line in enumerate(lines):
        if not _RETURN_NULL.search(line):
            continue
        guard = _guard_for(lines, n)
        if guard is None or _ARGISNULL.search(guard):
            continue
        return guard
    return None


def _guard_through(name: str, bodies: dict[str, str]) -> str | None:
    """The guard for ``name``, following the helper it hands ``fcinfo`` to.

    A THIRD of the wrappers state no guard of their own because they delegate:
    ``Temporal_at_timestamptz`` is one line, ``return
    Temporal_restrict_timestamptz(fcinfo, REST_AT);``, and the PG_RETURN_NULL
    sits in that shared helper.  Reading only the wrapper reports those as
    always producing a value -- which is exactly backwards for the restriction
    family, the one whose answer is absent most often.

    The chase is bounded and cycle-safe: a body naming itself, or a ring of
    helpers, ends the walk rather than looping.
    """
    seen: set[str] = set()
    while name and name not in seen and len(seen) <= _MAX_HOPS:
        seen.add(name)
        body = bodies.get(name)
        if body is None:
            return None
        guard = _own_guard(body)
        if guard is not None:
            return guard
        m = _DELEGATES.search(body)
        name = m.group(1) if m else None
    return None


def extract_null_results(mdb_src: str | Path) -> dict[str, str]:
    """Return ``{wrapper: guard}`` for every PG wrapper that can answer NULL."""
    bodies, _ = read_bodies(Path(mdb_src).parent)
    out: dict[str, str] = {}
    for name in bodies:
        guard = _guard_through(name, bodies)
        if guard:
            out[name] = guard
    return out


def attach_null_result(idl, mdb_src):
    """Record, per function, the guard under which its SQL answer is absent.

    Returns ``(idl, n)`` with ``n`` the number of functions given the field.
    """
    guards = extract_null_results(mdb_src)
    n = 0
    for f in idl.get("functions", []):
        wrapper = f.get("mdbC")
        if not wrapper:
            continue
        guard = guards.get(wrapper)
        if not guard:
            continue
        f.setdefault("shape", {})["nullableResult"] = guard
        n += 1
    return idl, n
