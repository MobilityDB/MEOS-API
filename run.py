import os
import sys
import json
from pathlib import Path

from parser.parser import parse_all_headers, merge_meta
from parser.portable import attach_portable_aliases, classify_backing_sqlfn
from parser.covering import attach_temporal_covering
from parser.typerecover import recover_collapsed_types
from parser.header_types import reconcile
from parser.shapeinfer import infer_shapes
from parser.nullable import merge_nullable
from parser.enrich import enrich_idl
from parser.sqlfn import attach_sqlfn_map, lint_ea_sqlfn, lint_sqlfn_case_collisions
from parser.doxygroup import attach_groups
from parser.extractors import find_unlisted_foreign_structs
from parser.object_model import attach_object_model, find_mobilitydb_src


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


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Parse C headers
    print(f"[1/4] Parsing {HEADERS_DIR}...", file=sys.stderr)
    idl = parse_all_headers(HEADERS_DIR)

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

    # 1d. Generate the codegen `shape` from the signatures + Doxygen, replacing
    #     the hand-maintained meta stub.  outputArrays/arrayReturn come from the
    #     parameter forms; nullable comes from the C `@param ... may be NULL` SoT.
    idl, sh = infer_shapes(idl)
    print(f"      inferred shape: {sh['arrayReturn']} array returns, "
          f"{sh['outputArrays']} output arrays", file=sys.stderr)
    idl, nn = merge_nullable(idl, HEADERS_DIR.parent)
    print(f"      nullable params from Doxygen `may be NULL`: {nn}",
          file=sys.stderr)

    # 1e. Derive service-projection metadata (category / encodings / network).
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
        idl, nsql = attach_sqlfn_map(idl, MEOS_SRC, MDB_SRC, SQL_SRC)
        print(f"[4/4] Attached {nsql} @sqlfn SQL names", file=sys.stderr)
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
        case_bad = lint_sqlfn_case_collisions(idl)
        if case_bad:
            print(f"      ⚠ {len(case_bad)} @sqlfn case-collision(s) (pick ONE canonical "
                  f"spelling at the MEOS-C source — binding-breaking otherwise):", file=sys.stderr)
            for _lo, spellings in case_bad:
                print(f"        {' vs '.join(spellings)}", file=sys.stderr)

        # Now that both the @sqlfn/@sqlop map (step 4) and the portable bare-name map
        # (step 3) are attached, classify the shared bbox-topological BACKING tags
        # (same_bbox/contains_bbox/…) so bindings register the bare public name, not the
        # catalog-only backing tag.
        idl = classify_backing_sqlfn(idl)
        nbo = sum(1 for f in idl.get("functions", []) if f.get("sqlfnBackingOnly"))
        print(f"      Flagged {nbo} bbox-topological backing @sqlfn tag(s) "
              f"(sqlfnBackingOnly)", file=sys.stderr)

    # 5. Attach the doxygen module group (@ingroup) from the vendored source, so
    #    bindings organize their generated surface like the reference manual.
    if MEOS_SRC.exists():
        idl, ngrp = attach_groups(idl, MEOS_SRC)
        print(f"[5/5] Attached {ngrp} doxygen @ingroup groups", file=sys.stderr)

    # Surface any forward-declared external ABI struct pointer not yet
    # normalised to void * (see parser.extractors._EXTERNAL_OPAQUE_STRUCTS),
    # so a new one is classified explicitly instead of diverging per binding.
    unlisted = find_unlisted_foreign_structs(idl)
    if unlisted:
        print(f"      WARNING: unlisted external struct pointer(s) in the API: "
              f"{', '.join(unlisted)} — add to _EXTERNAL_OPAQUE_STRUCTS to map "
              f"them to void * uniformly across bindings", file=sys.stderr)

    # 6. Attach the temporal-covering descriptor (Parquet/Iceberg projection)
    print(f"      Attaching temporal covering from {COVERING_PATH}...",
          file=sys.stderr)
    idl = attach_temporal_covering(idl, COVERING_PATH)

    # 7. Derive the explicit object model (class lattice + methods + error
    #    contract) from the implicit MEOS prefix convention.
    print(f"[7/7] Deriving object model from {OBJMODEL_PATH} "
          f"(error scan: {MOBILITYDB_SRC})...", file=sys.stderr)
    idl = attach_object_model(idl, OBJMODEL_PATH, MOBILITYDB_SRC)

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
          f"{pa} portable bare-name aliases, "
          f"{cov} temporal covering types", file=sys.stderr)
    if om:
        print(f"      object model: {om['classesWithMethods']} classes, "
              f"{om['functionsClassified']}/{om['functionsTotal']} functions "
              f"classified ({om['coveragePct']}%), "
              f"errors: {om['errorStatus']}", file=sys.stderr)


if __name__ == "__main__":
    main()
