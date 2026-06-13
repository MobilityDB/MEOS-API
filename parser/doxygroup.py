"""Attach the doxygen module group (@ingroup) to the MEOS-API catalog.

Every MEOS-C function carries an `@ingroup meos_<group>` doxygen tag in its
source comment block (meos/src). Those groups ARE the structure of the MEOS
reference manual / doxygen XML — e.g. `meos_setspan_accessor`,
`meos_temporal_comp_ever`, `meos_geo_constructor`. Carrying the group into the
catalog lets every binding organize its generated surface the SAME way the
manual does, so a function is found in the same place across all tools.

Adds per function (when found): `group`. The `meos_internal_*` groups are
MEOS-internal, not user-facing — they are tagged like any other so a binding
can filter them out, but they are NOT a separate concept here.
"""
import re
from pathlib import Path

_INGROUP = re.compile(r"@ingroup\s+(meos_\w+)")
# Same shape as sqlfn._FNDEF: after the doxygen close, an optional return-type
# line (no parens/braces/;/=), then `name(`.
_FNDEF = re.compile(r"\*/\s*\n(?:[^\n(){};=]+\n)?(\w+)\s*\(")


def _name_to_group(meos_src):
    """MEOS-C function name -> doxygen @ingroup group (first occurrence wins)."""
    out = {}
    for cf in Path(meos_src).rglob("*.c"):
        text = cf.read_text(errors="ignore")
        for m in _INGROUP.finditer(text):
            grp = m.group(1)
            fm = _FNDEF.search(text, m.end())
            if fm:
                out.setdefault(fm.group(1), grp)
    return out


def attach_groups(idl, meos_src):
    n2g = _name_to_group(meos_src)
    n = 0
    for f in idl["functions"]:
        g = n2g.get(f["name"])
        if g:
            f["group"] = g
            n += 1
    return idl, n
