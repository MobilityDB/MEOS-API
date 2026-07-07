"""Extract per-parameter nullability from the MEOS C Doxygen as the SoT.

A MEOS function parameter accepts NULL iff its Doxygen ``@param`` line says so,
e.g. ``@param[in] srs Spatial reference system, may be `NULL```.  This is the
single source of truth the codegens consume — grounded in the C code, keyed by
parameter name, and cross-checked in MobilityDB against the PG layer (a SQL
function declared without ``STRICT`` + the wrapper's ``PG_ARGISNULL`` guards).

The extractor walks the MEOS sources, pairs each Doxygen block with the function
it documents, and records the params whose description carries a NULL note.  The
result feeds ``shape.nullable`` in the IDL so every binding can guard the param.
"""
from __future__ import annotations

import glob
import re
from pathlib import Path

# Doxygen block immediately followed by a function definition (``name(...) {``).
_FUNC = re.compile(
    r'/\*\*(?P<doc>.*?)\*/\s*\n'
    r'(?:[A-Za-z_][\w\s\*]*?\n)?'          # optional return-type line
    r'(?P<name>[a-z][a-z0-9_]*)\s*\('
    r'(?P<params>[^;{]*?)\)\s*\{',
    re.S)
# One @param entry: capture the (possibly comma-separated) names + description.
_PARAM = re.compile(
    r'@param\[[^\]]*\]\s+(?P<names>\w+(?:\s*,\s*\w+)*)\s+(?P<desc>.*?)'
    r'(?=\n\s*\*\s*@|\*/|\Z)', re.S)
_NULLISH = re.compile(r'may be\s+`?NULL`?|can be\s+`?NULL`?|`?NULL`?\s+is allowed'
                      r'|or\s+`?NULL`?', re.I)


def extract_nullable(meos_root: str | Path) -> dict[str, list[str]]:
    """Return ``{function: [nullable params]}`` from the MEOS C sources under
    ``meos_root`` (scans both ``src`` and ``include``)."""
    root = Path(meos_root)
    out: dict[str, list[str]] = {}
    files = glob.glob(str(root / "src/**/*.c"), recursive=True)
    files += glob.glob(str(root / "include/**/*.h"), recursive=True)
    for f in files:
        txt = Path(f).read_text(errors="ignore")
        for m in _FUNC.finditer(txt):
            name = m.group("name")
            for pm in _PARAM.finditer(m.group("doc")):
                if not _NULLISH.search(pm.group("desc")):
                    continue
                for p in (n.strip() for n in pm.group("names").split(",")):
                    out.setdefault(name, [])
                    if p and p not in out[name]:
                        out[name].append(p)
    return out


def merge_nullable(idl: dict, meos_root: str | Path) -> tuple[dict, int]:
    """Fold the extracted nullability into each function's ``shape.nullable``."""
    nul = extract_nullable(meos_root)
    n = 0
    for func in idl["functions"]:
        params = nul.get(func["name"])
        if not params:
            continue
        present = {p["name"] for p in func.get("params", [])}
        keep = [p for p in params if p in present]
        if keep:
            func.setdefault("shape", {})["nullable"] = keep
            n += len(keep)
    return idl, n
