"""Infer per-function output-array *shape* from the C signatures.

MEOS array-returning functions follow one fixed convention, so the shape the
codegens need is fully derivable from the headers — no hand-maintained table:

    TYPE  *f(..., int *count)                 -> returns an array of ``count``
    TYPE **f(..., TYPE **extra, int *count)   -> primary array return PLUS one
                                                  or more parallel out-arrays
    f(..., TYPE **values, int count, ...)     -> reads an array of ``count``

The output length is always passed *by pointer* (``int *count``); an *input*
array instead carries its length *by value* (``int count``).  That pointer/value
distinction is exactly how a written-back out-array is told apart from a
read-only in-array — e.g. ``temporal_time_split(..., TimestampTz **time_bins,
int *count)`` (out) versus ``tsequence_make(const TInstant **instants, int
count, ...)`` (in).

Alongside ``lengthFrom`` the primary array return also carries its ``element``
type — the return with exactly one pointer level stripped, resolved as a
first-class ``{c, canonical}`` object (mirroring ``returnType``/``params``).  A
binding thus composes its native list/array over ``element.canonical`` through
its EXISTING per-type marshaller, never re-parsing the return string — the
zero-heuristic collection boundary (see binding-is-thin-io-shell-over-meos).

This replaces the ``meta/meos-meta.json`` shape entries, which had drifted to a
3-function stub and silently mis-classified every out-array as an input
parameter, breaking the split / space-split / mvtgeom / normalize families in
every binding generated from the IDL.
"""
from __future__ import annotations


# Parameters that accept NULL by MEOS convention regardless of the function.
# ``srs`` is the optional spatial-reference string of every ``*_as_*json`` /
# text output function — passing NULL means "no CRS".  Nullability is otherwise
# semantic (not signature-derivable), so this stays a narrow, named convention
# rather than a blanket rule; extend only when a binding's tests prove a param
# is passed None.
_NULLABLE_BY_CONVENTION = {"srs"}


def _out_count_param(func: dict) -> str | None:
    """Return the name of the by-pointer output count param, if the function
    has one.  This is the marker that the function returns array(s)."""
    for p in func.get("params", []):
        if p["name"] == "count" and p.get("cType", "").strip() == "int *":
            return p["name"]
    return None


def _is_written_back_array(p: dict) -> bool:
    """A non-const pointer parameter the callee ALLOCATES and writes back, i.e.
    a parallel output array.

    The callee writes ``*p = <the array>``, and an array of ``E`` is spelled
    ``E *``, so such a parameter is spelled ``E **`` — one pointer level for the
    array and one for the write-back.  Stripping both leaves ``E``, and ``E`` is
    what says whether the callee can have allocated the array at all: a binding
    reads an array of by-value scalars (``TimestampTz **bins`` -> ``TimestampTz``)
    or an array of pointers (``SpanSet ***result`` -> ``SpanSet *``), and nothing
    else.  A MEOS value type left bare — ``Jsonb **values`` -> ``Jsonb`` — is
    neither: MEOS holds such a value by reference, never by value in an array,
    so that parameter is an array the CALLER allocates and the callee only
    fills.  Reading it as callee-allocated makes every binding hand the callee
    one element's worth of storage and take the first element it writes for the
    address of the array."""
    ct = p.get("cType", "")
    if "**" not in ct or ct.lstrip().startswith("const"):
        return False
    element = _strip_one_ptr(_strip_one_ptr(ct))
    return element.endswith("*") or _bare(element) in _ELEMENT_SCALARS


def _strip_one_ptr(ctype: str) -> str:
    """Remove exactly one trailing ``*`` (with any surrounding space) — the
    inverse of "an array of ``E`` is spelled ``E *``".  ``double *`` -> ``double``
    (a by-value element); ``struct TInstant **`` -> ``struct TInstant *`` (an
    array of element pointers).  Mechanical and canonical, NOT a heuristic."""
    s = ctype.rstrip()
    if s.endswith("*"):
        s = s[:-1].rstrip()
    return s


#: The C scalars an array of by-value elements is made of.  A pointer to one of
#: these beside a length is an array; a pointer to a MEOS value type beside an
#: integer is a value and a number, as ``text_left(text *txt, int n)`` is.
#: ``char *`` is a string in every binding and never an array of characters.
_ELEMENT_SCALARS = frozenset({
    "bool", "int8", "int8_t", "uint8", "uint8_t", "short", "int16", "int16_t",
    "uint16", "uint16_t", "int", "int32", "int32_t", "uint32", "uint32_t",
    "float", "Oid", "DateADT", "long", "int64", "int64_t", "uint64",
    "uint64_t", "double", "float8", "Datum", "Timestamp", "TimestampTz",
    "TimeADT", "TimeOffset", "size_t",
})

#: The by-value integer spellings a length is written in.
_LENGTH_TYPES = frozenset({
    "int", "int32", "int32_t", "uint32", "uint32_t", "int64", "int64_t",
    "uint64", "uint64_t", "size_t", "int16",
})


def _bare(ctype: str) -> str:
    return " ".join((ctype or "").replace("const ", "").split())


def _input_arrays(func: dict) -> list:
    """The array ARGUMENTS a function reads, with the parameter each takes its
    length from.

    An input array is a parameter that is an array of pointers (``TYPE **``) or
    of by-value scalars (``uint8_t *``, ``int64_t *``), followed by a by-value
    integer.  That the length is by VALUE is what tells an argument apart from
    a written-back out-array, whose length is by POINTER — the same distinction
    this module already reads in the other direction.

    Without it a binding matches the LENGTH PARAMETER'S NAME, and the names
    disagree: ``count``, ``size``, ``ngeoms``, ``keys_len``, ``path_len``,
    ``pixels_size``, ``wkb_size``, ``count1``.  Every one of them is a length,
    and a binding that knows only some of them silently drops the rest.

    A RUN of arrays shares the one length that follows it.  Arrays read in
    parallel are declared together and counted once — ``jsonb_make_two_arg(text
    **keys, text **values, int count)`` pairs the two element by element, and
    ``tpointseq_make_coords`` reads four — so the length belongs to every array
    of the run, not only to the one the count happens to sit beside.  Where a
    family counts each array separately the run is one long and this says what
    it always said: ``edwithin_tgeoarr_tgeoarr(arr1, count1, arr2, count2, …)``
    keeps ``arr1`` on ``count1``.
    """
    params = func.get("params", [])

    def is_array(prm) -> bool:
        ctype = _bare(prm.get("cType"))
        if ctype.endswith("**"):
            return ctype not in ("char **", "void **")
        return ctype.endswith("*") and ctype[:-1].strip() in _ELEMENT_SCALARS

    out = []
    start = 0
    while start < len(params):
        if not is_array(params[start]):
            start += 1
            continue
        end = start
        while end < len(params) and is_array(params[end]):
            end += 1
        if end < len(params) and _bare(params[end].get("cType")) in _LENGTH_TYPES:
            for prm in params[start:end]:
                out.append({
                    "param": prm["name"],
                    "lengthFrom": {"kind": "param", "name": params[end]["name"]},
                    # The element reads as the return's does — the type with one
                    # pointer level off and no `const`, which belongs to the
                    # argument rather than to the element type a binding marshals.
                    "element": {
                        "c": _strip_one_ptr(_bare(prm.get("cType"))),
                        "canonical": _strip_one_ptr(
                            _bare(prm.get("canonical") or prm.get("cType"))),
                    },
                })
        start = end
    return out


def _is_index_pair_return(func: dict, count: str) -> bool:
    """Whether the ``int *`` return is a FLATTENED array of index PAIRS.

    The NxN kernels (``*_tgeoarr_tgeoarr``) take one or more ``(TYPE **arr, int n)``
    array arguments and answer which elements of one array relate to which of the
    other.  Their ``@param[out] count`` is the number of resulting index PAIRS while
    the returned ``int *`` holds ``2 * count`` ints, ``[i0, j0, i1, j1, ...]`` — so a
    consumer that reads ``count`` ints reads half the answer.  Nothing in
    ``lengthFrom`` says that, which is why every binding either re-derived the factor
    or marshalled these by hand.

    The convention is structural, so it is derived rather than listed: an ``int *``
    return, an ``int *count`` out-parameter, and at least one ``(TYPE **, int)``
    argument pair — the shape that makes an index into each input array meaningful.
    """
    params = func.get("params", [])
    arrays = 0
    for i, prm in enumerate(params):
        c = (prm.get("cType") or "").replace(" ", "")
        if c.endswith("**") and i + 1 < len(params):
            nxt = (params[i + 1].get("cType") or "").replace(" ", "")
            if nxt == "int":
                arrays += 1
    if arrays < 1:
        return False
    return any((prm.get("cType") or "").replace(" ", "") == "int*"
               and prm.get("name") == count for prm in params)


def infer_shapes(idl: dict) -> tuple[dict, dict]:
    """Populate ``func['shape']`` with ``arrayReturn``/``outputArrays`` derived
    from the signatures.  Returns ``(idl, stats)``.  Idempotent and additive:
    only the array-output families are touched, everything else is untouched."""
    n_arr = n_oa = n_ia = 0
    for func in idl["functions"]:
        inputs = _input_arrays(func)
        if inputs:
            func.setdefault("shape", {})["inputArrays"] = inputs
            n_ia += len(inputs)
        count = _out_count_param(func)
        if not count:
            continue  # not array-returning; nothing more to infer
        shape = func.setdefault("shape", {})
        # The primary pointer return takes its length from the output count.
        rtype = func.get("returnType", {})
        ret = rtype.get("c", "")
        if ret.rstrip().endswith("*"):
            ar = shape.setdefault("arrayReturn", {})
            ar["lengthFrom"] = {"kind": "param", "name": count}
            # Element type = the return with exactly one pointer level stripped,
            # resolved canonically so every binding reads a first-class
            # ``{c, canonical}`` type object (mirroring ``returnType``/``params``)
            # and routes it through its EXISTING per-type marshaller — never
            # re-parsing the return string.  ``double *`` -> ``double`` (by-value);
            # ``struct TInstant **`` -> ``struct TInstant *`` (array of pointers).
            ar["element"] = {
                "c": _strip_one_ptr(ret),
                "canonical": _strip_one_ptr(
                    rtype.get("canonical", ret)),
            }
            # How many elements make up ONE unit of `lengthFrom`.  Omitted when it is
            # 1 (the ordinary case); 2 for the NxN kernels, whose count is a number
            # of index PAIRS over a flattened `[i0, j0, i1, j1, ...]` return.  A
            # binding multiplies `lengthFrom` by it and never re-derives the factor.
            if _strip_one_ptr(ret).strip() == "int" and _is_index_pair_return(func, count):
                ar["groupSize"] = 2
            n_arr += 1
        # Parallel written-back out-arrays (``TYPE **extra`` alongside count).
        out = [{"param": p["name"]} for p in func["params"]
               if p["name"] != count and _is_written_back_array(p)]
        if out:
            shape["outputArrays"] = out
            n_oa += len(out)
    return idl, {"arrayReturn": n_arr, "outputArrays": n_oa,
                 "inputArrays": n_ia}
