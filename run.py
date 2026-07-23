import os
import sys
import json
import subprocess
from pathlib import Path

from parser.parser import parse_all_headers, merge_meta
from parser.portable import attach_portable_aliases, classify_backing_sqlfn
from parser.covering import attach_temporal_covering
from parser.typerecover import recover_collapsed_types, normalize_canonical
from parser.header_types import reconcile
from parser.shapeinfer import infer_shapes
from parser.nullable import merge_nullable
from parser.outparam import extract_param_names, merge_outparams
from parser.boundargs import merge_boundargs
from parser.enrich import enrich_idl
from parser.sqlfn import (attach_sqlfn_map, attach_aggfn_map, lint_ea_sqlfn,
                          lint_positional_sqlfn, lint_sqlfn_case_collisions)
from parser.doxygroup import attach_groups
from parser.extractors import find_unlisted_foreign_structs
from parser.object_model import attach_object_model, find_mobilitydb_src
from parser.typerelations import attach_type_relations


HEADERS_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./meos/include")
META_PATH     = Path("./meta/meos-meta.json")
PORTABLE_PATH = Path("./meta/portable-aliases.json")
COVERING_PATH = Path("./meta/temporal-covering.json")
OBJMODEL_PATH = Path("./meta/object-model.json")
OUTPUT_DIR  = Path("./output")

# MobilityDB C sources for the error-contract scan. Explicit argv[2] wins;
# otherwise resolved (env / _mobilitydb sparse checkout / src sibling).
# Absent → honest source-unavailable signal, never a fabricated empty set.
MOBILITYDB_SRC = (Path(sys.argv[2]) if len(sys.argv) > 2
                  else find_mobilitydb_src(HEADERS_DIR))


def _source_commit():
    """The MobilityDB commit these headers/sources were derived from, stamped into the catalog so
    ANY consumer (a binding, or the non-stale gate) proves the artifact's freshness by comparing it
    to live upstream master — never by inspecting the directory a vendored copy happens to sit in
    (that proxy is blind to a catalog vendored inside an unrelated binding repo). Resolved from the
    git checkout that actually provided the source; None if the source is not a git checkout (e.g. a
    release tarball) — the consumer then treats freshness as unprovable, the correct safe default."""
    cands = []
    for base in (os.environ.get("MDB_SRC_ROOT"), str(HEADERS_DIR), str(MOBILITYDB_SRC)):
        if not base:
            continue
        p = Path(base).resolve()
        cands += [p, p.parent, p.parent.parent]
    for d in cands:
        try:
            r = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        sha = r.stdout.strip()
        if r.returncode == 0 and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
            return sha
    return None


def _public_pgtypes_headers() -> tuple[Path, ...]:
    """MobilityDB's vendored PostgreSQL base-type headers, when they sit outside HEADERS_DIR.

    MobilityDB keeps the PostgreSQL 18 base types in a `pgtypes/` library at the repo root and
    installs `pgtypes.h` and the `pg_*.h` set into the include prefix, so they are as public as
    `meos.h`. Reading an INSTALLED prefix therefore already covers them and this adds nothing;
    reading the source tree points at `meos/include`, which does not contain them, and without
    this the catalog silently loses the whole base-type surface (`interval_make`, the base I/O
    functions, …) — the same ref then yields two different catalogs depending on the header root.
    """
    root = Path(os.environ.get("MDB_SRC_ROOT", "./_mobilitydb")) / "pgtypes"
    if not root.is_dir():
        return ()
    seen = {p.name for p in HEADERS_DIR.glob("**/*.h")}
    candidates = sorted(root.glob("pg_*.h")) + [root / "pgtypes.h"]
    headers = [h for h in candidates
               # pg_config*.h carry PostgreSQL's build configuration, not API; the
               # install set leaves them behind and so does the catalog.
               if h.is_file() and not h.name.startswith("pg_config")
               and h.name not in seen]
    return tuple(headers)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Parse C headers
    print(f"[1/4] Parsing {HEADERS_DIR}...", file=sys.stderr)
    pgtypes = _public_pgtypes_headers()
    if pgtypes:
        print(f"      + {len(pgtypes)} vendored pgtypes headers from "
              f"{pgtypes[0].parent}", file=sys.stderr)
    idl = parse_all_headers(HEADERS_DIR, pgtypes)

    # 1b. Recover PG-vendored C types the preprocessor collapsed to int
    #     (bool / int64 / Timestamp(Tz) / H3Index) from the header text.
    #     No-op when the headers parse those types correctly.
    idl, rec = recover_collapsed_types(idl, HEADERS_DIR)
    if rec["returns"] or rec["params"]:
        print(f"      recovered {rec['returns']} return types, "
              f"{rec['params']} params from collapsed int", file=sys.stderr)

    # 1c. Restore opaque pointer types the PG stub headers #define'd to int,
    #     from the header source. Scalar typedefs already resolved by
    #     recover_collapsed_types (H3Index/Quadbin -> uint64_t) are left intact.
    idl = reconcile(idl, HEADERS_DIR)

    # 1d. Re-spell each slot's `canonical` as the MEOS typedef its `cType` names,
    #     not libclang's platform resolution. The self-contained (installed)
    #     header parse resolves TimestampTz -> long and Jsonb * -> varlena *; the
    #     source parse leaves them as the typedef. Deriving canonical from the
    #     faithful cType makes both parses agree, so a binding generator (which
    #     keys on canonical) marshals timestamps/jsonb rather than dropping them.
    idl, ncanon = normalize_canonical(idl)
    if ncanon:
        print(f"      normalized {ncanon} canonical spellings to the cType typedef",
              file=sys.stderr)

    # 1d. Generate the codegen `shape` from the signatures + Doxygen, replacing
    #     the hand-maintained meta stub.  outputArrays/arrayReturn come from the
    #     parameter forms; nullable comes from the C `@param ... may be NULL` SoT.
    idl, sh = infer_shapes(idl)
    print(f"      inferred shape: {sh['arrayReturn']} array returns, "
          f"{sh['outputArrays']} output arrays", file=sys.stderr)
    # The `may be NULL` / `@param[out]` Doxygen tags live in the MEOS C *source*
    # (meos/src/**/*.c), not the parsed header tree. On the build-libmeos path
    # HEADERS_DIR is the INSTALLED headers (generated meos_export.h, no src/), so
    # scan from the source checkout (MDB_SRC_ROOT/meos) — the same root the
    # @sqlfn/@ingroup maps use — and fall back to the header tree's parent when
    # the headers are themselves a source checkout (no MDB_SRC_ROOT).
    _src_root = (Path(os.environ["MDB_SRC_ROOT"]) / "meos"
                 if os.environ.get("MDB_SRC_ROOT") else HEADERS_DIR.parent)
    _doxy_root = _src_root if (_src_root / "src").is_dir() else HEADERS_DIR.parent
    idl, nn = merge_nullable(idl, _doxy_root)
    print(f"      nullable params from Doxygen `may be NULL`: {nn}",
          file=sys.stderr)
    idl, no, out_drift = merge_outparams(idl, _doxy_root)
    print(f"      out params from Doxygen `@param[out]`: {no}", file=sys.stderr)
    if out_drift:
        print(f"      ⚠ {len(out_drift)} @param[out] tag(s) disagree with the C signature "
              f"(manual-maintenance drift — clean at the MEOS source):", file=sys.stderr)
        for fn, pn, reason in out_drift:
            print(f"          {fn}({pn}) — {reason}", file=sys.stderr)

    # 1e. Attach the doxygen @ingroup groups BEFORE enrich: the catalog `api`
    #     field derives from the group (public unless the group is
    #     `meos_internal_*` or absent), so the group must already be attached when
    #     enrich computes api and the api-dependent network projection.
    _grp_root = Path(os.environ.get("MDB_SRC_ROOT", "./_mobilitydb"))
    if (_grp_root / "meos" / "src").exists():
        idl, ngrp = attach_groups(idl, _grp_root / "meos" / "src",
                                  _grp_root / "pgtypes")
        print(f"      attached {ngrp} doxygen @ingroup groups", file=sys.stderr)

    # 1f. Derive service-projection metadata (category / encodings / network).
    #     Runs before the merge so manual annotations override the heuristics.
    idl = enrich_idl(idl)

    # 2. Merge with manual metadata
    if META_PATH.exists():
        print(f"[2/4] Merging with {META_PATH}...", file=sys.stderr)
        idl = merge_meta(idl, META_PATH)
    else:
        print(f"[2/4] No meta found at {META_PATH}, skipping.", file=sys.stderr)

    # 3. Attach the canonical portable bare-name mapping (codegen truth)
    print(f"[3/4] Attaching portable aliases from {PORTABLE_PATH}...",
          file=sys.stderr)
    idl = attach_portable_aliases(idl, PORTABLE_PATH)

    # 4. Attach the SQL-name map (@sqlfn/@sqlop) from the vendored source.
    #    The source root is overridable (MDB_SRC_ROOT) so a binding can point the
    #    @sqlfn/@ingroup extraction at the SAME pinned checkout as the headers,
    #    keeping the catalog reproducibly equivalent to that pin.
    SRC_ROOT = Path(os.environ.get("MDB_SRC_ROOT", "./_mobilitydb"))
    MEOS_SRC = SRC_ROOT / "meos" / "src"
    MDB_SRC = SRC_ROOT / "mobilitydb" / "src"
    SQL_SRC = SRC_ROOT / "mobilitydb" / "sql"
    if MEOS_SRC.exists() and MDB_SRC.exists():
        idl, nsql, sqlfn_multi = attach_sqlfn_map(idl, MEOS_SRC, MDB_SRC, SQL_SRC)
        print(f"[4/4] Attached {nsql} @sqlfn SQL names", file=sys.stderr)
        # Aggregate identity: @csqlaggfn names the SQL aggregate (setUnion /
        # spanUnion / spansetUnion) each transition/combine/final function
        # implements, so an aggregate member is distinguishable from the identically
        # named binary set/span union function. One-hop, faithful to the source tag.
        idl, nagg = attach_aggfn_map(idl, MEOS_SRC)
        print(f"      Attached {nagg} @csqlaggfn aggregate names", file=sys.stderr)
        # Guard: a copy-paste @csqlfn in meos/src can point an ever/always function at
        # the opposite-prefix wrapper (eintersects_* tagged #Aintersects_*), flipping its
        # SQL name and breaking the binding overload dispatch. The parser is faithful, so
        # surface the SOURCE mistag here rather than ship a wrong catalog silently.
        ea_bad = lint_ea_sqlfn(idl)
        if ea_bad:
            print(f"      ⚠ {len(ea_bad)} @csqlfn e/a-prefix mistag(es) in meos/src "
                  f"(fix at source — wrong @sqlfn resolved):", file=sys.stderr)
            for cname, sf in ea_bad:
                print(f"        {cname} -> @sqlfn {sf}", file=sys.stderr)
        # Guard: @sqlfn names that differ only by case (e.g. tDistance vs tdistance)
        # are the SAME SQL function (PostgreSQL folds the identifier) but DISTINCT
        # binding names — a case-insensitive engine (Spark SQL) registers both under
        # one UDF, so one silently shadows the other. Invisible in SQL; surface the
        # casing straggler here, to be fixed at the MEOS-C @sqlfn source.
        case_bad = lint_sqlfn_case_collisions(idl, sqlfn_multi)
        if case_bad:
            print(f"      ⚠ {len(case_bad)} @sqlfn case-collision(s) (pick ONE canonical "
                  f"spelling at the MEOS-C source — binding-breaking otherwise):", file=sys.stderr)
            for _lo, spellings in case_bad:
                print(f"        {' vs '.join(spellings)}", file=sys.stderr)
        # Guard: a relative-position function whose name prefix (before_/left_/...)
        # disagrees with its resolved @sqlfn — a shared value/time position wrapper
        # whose single @sqlfn mis-names the other axis (before_span_* -> left). The
        # name prefix is the SoT; fix the @csqlfn / add a dedicated wrapper at source.
        pos_bad = lint_positional_sqlfn(idl)
        if pos_bad:
            print(f"      ⚠ {len(pos_bad)} positional name/@sqlfn mismatch(es) (a time-axis "
                  f"function resolved to the value name or vice versa — fix at source):",
                  file=sys.stderr)
            for cname, sf in pos_bad:
                print(f"        {cname} -> @sqlfn {sf}", file=sys.stderr)

        # Now that both the @sqlfn/@sqlop map (step 4) and the portable bare-name map
        # (step 3) are attached, classify the shared bbox-topological BACKING tags
        # (same_bbox/contains_bbox/…) so bindings register the bare public name, not the
        # catalog-only backing tag.
        idl = classify_backing_sqlfn(idl)
        nbo = sum(1 for f in idl.get("functions", []) if f.get("sqlfnBackingOnly"))
        print(f"      Flagged {nbo} bbox-topological backing @sqlfn tag(s) "
              f"(sqlfnBackingOnly)", file=sys.stderr)

        # A PG wrapper can BIND a MEOS input to a fixed literal instead of exposing
        # it as a SQL argument (valueAtTimestamp hides `strict=true`). Capture those
        # bound literals from the wrapper body as `shape.boundArgs`, the input-side
        # sibling of `shape.outParams`, so a binding emits the literal it can no
        # longer read off the (narrower) SQL signature. Needs `mdbC` (step 4).
        idl, nba, ba_drift = merge_boundargs(idl, MDB_SRC,
                                             extract_param_names(_doxy_root))
        print(f"      Bound-literal args from PG wrappers `shape.boundArgs`: {nba}",
              file=sys.stderr)
        if ba_drift:
            print(f"      ⚠ {len(ba_drift)} wrapper call arg(s) unclassified "
                  f"(neither caller arg, out-param, nor literal — inspect):", file=sys.stderr)
            for fn, pn, reason in ba_drift:
                print(f"          {fn}({pn}) — {reason}", file=sys.stderr)


    # Surface any forward-declared external ABI struct pointer in the API, so a
    # new one is classified explicitly instead of diverging per binding.
    unlisted = find_unlisted_foreign_structs(idl)
    if unlisted:
        print(f"      WARNING: unlisted external struct pointer(s) in the API: "
              f"{', '.join(unlisted)} — classify them explicitly so bindings "
              f"handle them uniformly", file=sys.stderr)

    # 6. Attach the temporal-covering descriptor (Parquet/Iceberg projection)
    print(f"      Attaching temporal covering from {COVERING_PATH}...",
          file=sys.stderr)
    idl = attach_temporal_covering(idl, COVERING_PATH)

    # 7. Derive the explicit object model (class lattice + methods + error
    #    contract) from the implicit MEOS prefix convention.
    print(f"[7/7] Deriving object model from {OBJMODEL_PATH} "
          f"(error scan: {MOBILITYDB_SRC})...", file=sys.stderr)
    idl = attach_object_model(idl, OBJMODEL_PATH, MOBILITYDB_SRC)

    # Attach the base-to-collection type-relation registry (a base T to its set, span, span set
    # and temporal types), derived from the meos_catalog.c positional arrays. A binding projects
    # the concrete collection type of a value-domain result from this, never hard-coding it.
    idl = attach_type_relations(idl, MOBILITYDB_SRC)

    # Stamp the MobilityDB source commit so the catalog is SELF-DESCRIBING about its freshness:
    # a consumer proves it is current by comparing sourceCommit to live upstream master, never by
    # inspecting whatever directory a vendored copy sits in. None when the source is not a git
    # checkout (freshness then unprovable — the consumer's correct safe default).
    idl["sourceCommit"] = _source_commit()
    print(f"      sourceCommit = {idl['sourceCommit'] or '(source not a git checkout — unstamped)'}",
          file=sys.stderr)

    idl_path = OUTPUT_DIR / "meos-idl.json"
    with open(idl_path, "w") as f:
        json.dump(idl, f, indent=2)
    print(f"      → {idl_path} written", file=sys.stderr)

    pa = idl.get("portableAliases", {}).get("count", 0)
    cov = idl.get("temporalCovering", {}).get("count", 0)
    exposable = idl.get("enrichment", {}).get("exposableFunctions", 0)
    om = idl.get("objectModel", {}).get("summary", {})
    print(f"\nDone: {len(idl['functions'])} functions "
          f"({exposable} stateless-exposable), "
          f"{len(idl['structs'])} structs, "
          f"{len(idl['enums'])} enums, "
          f"{len(idl.get('macros', []))} macros, "
          f"{pa} portable bare-name aliases, "
          f"{cov} temporal covering types", file=sys.stderr)
    if om:
        print(f"      object model: {om['classesWithMethods']} classes, "
              f"{om['functionsClassified']}/{om['functionsTotal']} functions "
              f"classified ({om['coveragePct']}%), "
              f"errors: {om['errorStatus']}", file=sys.stderr)


if __name__ == "__main__":
    main()
