"""Extract the MEOS-C -> MobilityDB-C -> SQL name chain from the Doxygen tags.

Every MobilityDB function carries up to three names, linked in the C sources by
Doxygen tags:

  * the MEOS C library function (lowercase snake, e.g. ``tdistance_tnumber_number``)
    documents ``@csqlfn #<MobilityDB-C name>()`` -> the PG wrapper it backs;
  * the MobilityDB C wrapper (PascalCase + ``PG_FUNCTION_ARGS``, e.g.
    ``Tdistance_tnumber_number`` -- Pascal-cased to avoid a symbol clash with the
    linked MEOS symbol) documents ``@sqlfn <SQLname>()`` and optionally
    ``@sqlop @p <operator>`` -> the user-facing SQL name (lowerCamel, overloaded)
    and operator.

So the canonical SQL name + operator of every MEOS function is machine-derivable
from one source of truth, and bindings translate names from it instead of
hand-maintaining per-binding name maps.  See docs/ECOSYSTEM_NAMING_POLICY.md.
"""
import re
from pathlib import Path

# A Doxygen block `/** ... */` immediately followed by the declaration it
# documents, captured up to the first `(`.  Pairing the block with the function
# RIGHT AFTER it (rather than a loose `.*?` that can cross into a neighbouring
# function) is what makes the @csqlfn/@sqlfn association correct.
_DOC_FN = re.compile(r"/\*\*(.*?)\*/[ \t]*\n(.*?)\(", re.S)
_IDENT = re.compile(r"[A-Za-z_]\w*")
_CSQLFN_TAG = re.compile(r"@csqlfn\s+#(\w+)")          # first wrapper this MEOS fn backs
_SQLFN_TAG = re.compile(r"@sqlfn\s+(\w+)")
_SQLOP_TAG = re.compile(r"@sqlop\s+@p\s+(\S+)")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _doc_fn_pairs(text: str):
    """Yield (doc_text, function_name) for each Doxygen block immediately
    followed by a function definition. The function name is the identifier just
    before the `(` of that definition."""
    for m in _DOC_FN.finditer(text):
        ids = _IDENT.findall(m.group(2))
        if ids:
            yield m.group(1), ids[-1]


def extract_sql_chain(src_root: Path) -> dict:
    """Return ``{meos_c_name: {"mobilitydb", "sql", "sqlop"}}`` for every function
    whose name chain is documented in ``src_root`` (a MobilityDB checkout with
    ``meos/src`` and ``mobilitydb/src``)."""
    meos_src = src_root / "meos" / "src"
    mdb_src = src_root / "mobilitydb" / "src"
    if not meos_src.is_dir() or not mdb_src.is_dir():
        return {}

    # MEOS-C library name -> MobilityDB-C wrapper name   (@csqlfn, in meos/src).
    # The tag is ON the MEOS fn (lowercase), and MANY typed datum-hiding wrappers
    # (tdistance_tint_int, tdistance_tfloat_float, in *_meos.c) back ONE wrapper
    # (Tdistance_tnumber_number) -> key by the MEOS name to keep every variant.
    # Block-based pairing (doc block + the function right after it) prevents
    # mis-associating the tag with a neighbouring function.
    meos_to_mdb: dict[str, str] = {}
    for f in meos_src.rglob("*.c"):
        for doc, fn in _doc_fn_pairs(_read(f)):
            t = _CSQLFN_TAG.search(doc)
            if t and fn[:1].islower():
                meos_to_mdb.setdefault(fn, t.group(1))

    # MobilityDB-C wrapper name -> (SQL name, operator)   (@sqlfn/@sqlop, in mobilitydb/src)
    mdb_to_sql: dict[str, tuple[str, str | None]] = {}
    for f in mdb_src.rglob("*.c"):
        for doc, fn in _doc_fn_pairs(_read(f)):
            s = _SQLFN_TAG.search(doc)
            if s:
                op = _SQLOP_TAG.search(doc)
                mdb_to_sql.setdefault(fn, (s.group(1), op.group(1) if op else None))

    chain: dict[str, dict] = {}
    for meos, mdb in meos_to_mdb.items():
        sqlinfo = mdb_to_sql.get(mdb)
        if sqlinfo:
            sql, op = sqlinfo
            entry = {"mobilitydb": mdb, "sql": sql}
            if op:
                entry["sqlop"] = op
            chain[meos] = entry
    return chain


def merge_sql_names(idl: dict, src_root: Path):
    """Attach ``mobilitydb`` (PG wrapper), ``sql`` (SQL name) and ``sqlop`` to each
    IDL function whose chain is documented.  Returns ``(idl, n_attached)``.
    No-op (returns 0) when the sources are unavailable."""
    chain = extract_sql_chain(src_root)
    n = 0
    for fn in idl.get("functions", []):
        c = chain.get(fn["name"])
        if c:
            fn.update(c)
            n += 1
    return idl, n
