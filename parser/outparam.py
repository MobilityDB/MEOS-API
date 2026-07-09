"""Extract per-parameter OUT direction from the MEOS C Doxygen ``@param[out]`` as the SoT.

A MEOS function parameter is an OUTPUT parameter iff its Doxygen line says
``@param[out] name ...``.  This is the single source of truth the codegens consume to
FOLD an out-param — the wrapper allocates the buffer, passes it, and reads the value
back — instead of exposing it as a caller argument.  It replaces the per-binding
guesswork (JMEOS's hardcoded ``result``/``size_out`` name whitelist, a type/position
heuristic) with one explicit signal grounded in the C code, keyed by parameter name.
It feeds ``shape.outParams`` in the IDL, the exact sibling of ``shape.nullable``.

The Doxygen tags are MANUALLY MAINTAINED, so the flag is emitted ONLY when the tag
AGREES with the C signature — the param must be a NON-CONST POINTER.  A tag on a
by-value param (``@param[out] count int``) or a ``const`` pointer is a documentation
error: it is dropped and reported for cleanup, never trusted.  This cross-check is what
keeps a stray annotation from corrupting every binding's signature.
"""
from __future__ import annotations

import glob
import re
from pathlib import Path

# Doxygen block immediately followed by a function definition (mirrors nullable.py).
_FUNC = re.compile(
    r'/\*\*(?P<doc>.*?)\*/\s*\n'
    r'(?:[A-Za-z_][\w\s\*]*?\n)?'          # optional return-type line
    r'(?P<name>[a-z][a-z0-9_]*)\s*\('
    r'(?P<params>[^;{]*?)\)\s*\{',
    re.S)
# One @param[out] entry: capture the (possibly comma-separated) parameter names.
_POUT = re.compile(r'@param\[out\]\s+(?P<names>\w+(?:\s*,\s*\w+)*)', re.S)


def extract_outparams(meos_root: str | Path) -> dict[str, list[str]]:
    """Return ``{function: [params tagged @param[out]]}`` from the MEOS C sources under
    ``meos_root`` (scans both ``src`` and ``include``)."""
    root = Path(meos_root)
    out: dict[str, list[str]] = {}
    files = glob.glob(str(root / "src/**/*.c"), recursive=True)
    files += glob.glob(str(root / "include/**/*.h"), recursive=True)
    for f in files:
        txt = Path(f).read_text(errors="ignore")
        for m in _FUNC.finditer(txt):
            name = m.group("name")
            for pm in _POUT.finditer(m.group("doc")):
                for p in (n.strip() for n in pm.group("names").split(",")):
                    if p:
                        out.setdefault(name, [])
                        if p not in out[name]:
                            out[name].append(p)
    return out


def _nonconst_ptr(canon: str) -> bool:
    return "*" in canon and "const" not in canon


def merge_outparams(idl: dict, meos_root: str | Path) -> tuple[dict, int, list]:
    """Fold the extracted out-params into each function's ``shape.outParams`` — but ONLY
    the params that are BOTH tagged ``@param[out]`` AND a non-const pointer in the C
    signature.  Returns ``(idl, count, drift)`` where ``drift`` lists the
    ``(function, param, reason)`` manual-maintenance discrepancies to clean at the source:
    a ``@param[out]`` on a by-value/const param (``not-a-non-const-pointer``), or a tag
    whose name is absent from the signature (``name-not-in-signature`` — e.g. the header
    declares ``size`` while the ``.c`` doc says ``size_out``)."""
    tagged = extract_outparams(meos_root)
    n = 0
    drift: list[tuple[str, str, str]] = []
    for func in idl["functions"]:
        names = {p["name"]: p for p in func.get("params", [])}
        keep = []
        for pn in tagged.get(func["name"], []):
            p = names.get(pn)
            if p is None:
                if names:  # only a real function with a param list; skip decl-less noise
                    drift.append((func["name"], pn, "name-not-in-signature"))
                continue
            if _nonconst_ptr(p.get("canonical", "")):
                keep.append(pn)
            else:
                drift.append((func["name"], pn, "not-a-non-const-pointer: "
                              + p.get("canonical", "")))
        if keep:
            func.setdefault("shape", {})["outParams"] = keep
            n += len(keep)
    return idl, n, drift
