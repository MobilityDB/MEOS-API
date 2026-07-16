"""Attach the doxygen module group (@ingroup) to the MEOS-API catalog.

Every MEOS-C function carries an `@ingroup meos_<group>` doxygen tag in its
source comment block (meos/src). Those groups ARE the structure of the MEOS
reference manual / doxygen XML — e.g. `meos_setspan_accessor`,
`meos_temporal_comp_ever`, `meos_geo_constructor`. Carrying the group into the
catalog lets every binding organize its generated surface the SAME way the
manual does, so a function is found in the same place across all tools.

The base-type functions the MEOS umbrella headers export (`text_out`, `date_in`,
`timestamp_cmp_internal`, …) live in the vendored `pgtypes/` tree at the repo
root, NOT under `meos/src`, and they ALSO carry `@ingroup meos_base_*` tags.
`_name_to_group` therefore scans every root it is given, so a binding can pass
both `meos/src` and `pgtypes/` and pick up the base surface too.

Adds per function (when found): `group`. The `meos_internal_*` groups are
MEOS-internal, not user-facing — they are tagged like any other so a binding
can filter them out, but they are NOT a separate concept here.
"""
import re
from pathlib import Path

_INGROUP = re.compile(r"@ingroup\s+(meos_\w+)")
# After the doxygen close `*/`, the function definition can be separated from the
# comment by preprocessor directives (`#if MEOS` … `#endif`) and/or ordinary C
# comments — the vendored pgtypes files guard the MEOS-build twin of a symbol
# that way. Skip any run of such lines, then an optional return-type line (no
# parens/braces/;/=), then `name(`. DOTALL lets a multi-line `/* … */` comment
# be skipped as one unit.
# A skippable line is blank or holds only a preprocessor directive / comment
# (the directive/comment part is optional so a blank line matches too). `[^\S\n]`
# is horizontal whitespace only — it matches `\r` so CRLF sources work, without
# letting a unit swallow the following return-type or name line.
_SKIP = r"(?:[^\S\n]*(?:\#[^\n]*|//[^\n]*|/\*.*?\*/)?[^\S\n]*\n)*"
_FNDEF = re.compile(
    r"\*/[^\S\n]*\n" + _SKIP + r"(?:[^\n(){};=]+\n)?(\w+)\s*\(",
    re.DOTALL,
)


def _name_to_group(*srcs):
    """MEOS-C function name -> doxygen @ingroup group (first occurrence wins).

    Scans every source root given (e.g. `meos/src` and `pgtypes/`).
    """
    out = {}
    for src in srcs:
        if not src or not Path(src).exists():
            continue
        for cf in Path(src).rglob("*.c"):
            text = cf.read_text(errors="ignore")
            for m in _INGROUP.finditer(text):
                grp = m.group(1)
                fm = _FNDEF.search(text, m.end())
                if fm:
                    out.setdefault(fm.group(1), grp)
    return out


def attach_groups(idl, *srcs):
    n2g = _name_to_group(*srcs)
    n = 0
    for f in idl["functions"]:
        g = n2g.get(f["name"])
        if g:
            f["group"] = g
            n += 1
    return idl, n
