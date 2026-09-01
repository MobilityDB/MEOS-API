"""Read the optional type families from MobilityDB's own ``ALL`` list.

MobilityDB declares every optional family once, in the ``if(ALL) foreach(_family
...)`` list of its top-level ``CMakeLists.txt`` — the list ``-DALL=ON`` expands
to. That list is the single source of truth for which families exist, and this
module is the ecosystem's only reader of it: the catalog publishes what it finds
as ``families``, and every consumer — this parser's compile flags, its family
classification, the tests, and each downstream binding — projects that field
rather than restating the list.

Restating it is what drifts. A family added to MobilityDB (``RASTER``,
``S2CELL``) leaves any written-down copy silently short, and a short copy
classifies that family's whole surface as ``CORE``. Reading the list means a new
family reaches every consumer with no edit here.

The family token is the whole name: a family lives in ``meos/src/<token
lowercased>/`` and is fronted by the public header ``meos_<token lowercased>.h``,
so the subdirectory and header of a family not yet written are already known.
"""
import os
import re
from functools import lru_cache
from pathlib import Path

# The `if(ALL)` block of MobilityDB's top-level CMakeLists.txt, whose `foreach`
# names every optional family. Matched together so that a `foreach` elsewhere in
# the file cannot be read as the family list.
_ALL_BLOCK_RE = re.compile(
    r"if\s*\(\s*ALL\s*\).*?foreach\s*\(\s*_family\s+([^)]*)\)", re.S)


class FamiliesUnavailable(RuntimeError):
    """MobilityDB's CMakeLists.txt could not be read.

    Raised rather than degraded to a default list: a short family list is
    invisible in the output — the missing families are classified ``CORE`` and
    every consumer inherits that — so there is no honest fallback.
    """


def read_all_families(cmakelists: Path) -> tuple[str, ...]:
    """The family tokens named by ``cmakelists``'s ``if(ALL) foreach`` list."""
    text = Path(cmakelists).read_text(errors="ignore")
    m = _ALL_BLOCK_RE.search(text)
    if not m:
        raise FamiliesUnavailable(
            f"{cmakelists} carries no `if(ALL) foreach(_family ...)` list — "
            "the family source of truth moved; update parser/families.py to "
            "read it where it now lives")
    return tuple(sorted(re.findall(r"[A-Z][A-Z0-9_]*", m.group(1))))


_headers_dir: Path | None = None


def use_headers_dir(headers_dir) -> None:
    """Name the header tree being parsed, so the checkout can be found from it.

    MobilityDB's public headers live at ``<root>/meos/include``, so a caller
    parsing a checkout it made itself has already named the root two levels up
    and need not also export a variable. Clears what the previous root derived.
    """
    global _headers_dir
    _headers_dir = Path(headers_dir) if headers_dir else None
    all_families.cache_clear()
    subdir_family.cache_clear()
    header_family.cache_clear()


def find_mobilitydb_root() -> Path | None:
    """The MobilityDB checkout root, i.e. the directory holding CMakeLists.txt.

    First existing of: ``$MDB_SRC_ROOT`` (what the provisioning exports), the
    ``$MOBILITYDB_SRC`` source root's grandparent, the root above the header
    tree named by :func:`use_headers_dir`, and the ``_mobilitydb`` /
    ``_mobilitydb_src`` checkouts setup.py and the CI action create. Each is
    tested for CMakeLists.txt, so a header tree staged away from its checkout
    (the installed prefix a native build parses) falls through to the next
    rather than answering for it. A sparse checkout carries the repository's
    root files whatever its cone selects, so CMakeLists.txt is present in each.
    """
    candidates = []
    for env in ("MDB_SRC_ROOT", "MOBILITYDB_SRC"):
        val = os.environ.get(env)
        if val:
            p = Path(val)
            # $MOBILITYDB_SRC names meos/src; $MDB_SRC_ROOT names the root.
            candidates += [p, p.parent.parent]
    if _headers_dir is not None:
        candidates.append(_headers_dir.parent.parent)
    candidates += [Path("_mobilitydb"), Path("_mobilitydb_src")]
    for c in candidates:
        if (c / "CMakeLists.txt").exists():
            return c
    return None


@lru_cache(maxsize=1)
def all_families() -> tuple[str, ...]:
    """Every optional family MobilityDB's ``ALL=ON`` build enables."""
    root = find_mobilitydb_root()
    if root is None:
        raise FamiliesUnavailable(
            "MobilityDB's CMakeLists.txt is not reachable — set $MDB_SRC_ROOT "
            "to the checkout root (tools/provision-meos.sh does) or run "
            "setup.py to create the _mobilitydb checkout")
    return read_all_families(root / "CMakeLists.txt")


@lru_cache(maxsize=1)
def subdir_family() -> dict:
    """``meos/src`` subdirectory name -> family token."""
    return {f.lower(): f for f in all_families()}


@lru_cache(maxsize=1)
def header_family() -> dict:
    """Top-level public header name -> family token."""
    return {f"meos_{f.lower()}.h": f for f in all_families()}
