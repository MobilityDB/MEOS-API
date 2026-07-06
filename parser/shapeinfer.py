"""Infer per-function output-array *shape* from the C signatures.

MEOS array-returning functions follow one fixed convention, so the shape the
codegens need is fully derivable from the headers — no hand-maintained table:

    TYPE  *f(..., int *count)                 -> returns an array of ``count``
    TYPE **f(..., TYPE **extra, int *count)   -> primary array return PLUS one
                                                  or more parallel out-arrays

The output length is always passed *by pointer* (``int *count``); an *input*
array instead carries its length *by value* (``int count``).  That pointer/value
distinction is exactly how a written-back out-array is told apart from a
read-only in-array — e.g. ``temporal_time_split(..., TimestampTz **time_bins,
int *count)`` (out) versus ``tsequence_make(const TInstant **instants, int
count, ...)`` (in).

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
    """A non-const double (or higher) pointer parameter the callee allocates
    and writes back, i.e. a parallel output array."""
    ct = p.get("cType", "")
    return "**" in ct and not ct.lstrip().startswith("const")


def infer_shapes(idl: dict) -> tuple[dict, dict]:
    """Populate ``func['shape']`` with ``arrayReturn``/``outputArrays`` derived
    from the signatures.  Returns ``(idl, stats)``.  Idempotent and additive:
    only the array-output families are touched, everything else is untouched."""
    n_arr = n_oa = 0
    for func in idl["functions"]:
        count = _out_count_param(func)
        if not count:
            continue  # not array-returning; nothing to infer
        shape = func.setdefault("shape", {})
        # The primary pointer return takes its length from the output count.
        ret = func.get("returnType", {}).get("c", "")
        if ret.rstrip().endswith("*"):
            shape.setdefault("arrayReturn", {})["lengthFrom"] = {
                "kind": "param", "name": count}
            n_arr += 1
        # Parallel written-back out-arrays (``TYPE **extra`` alongside count).
        out = [{"param": p["name"]} for p in func["params"]
               if p["name"] != count and _is_written_back_array(p)]
        if out:
            shape["outputArrays"] = out
            n_oa += len(out)
    return idl, {"arrayReturn": n_arr, "outputArrays": n_oa}
