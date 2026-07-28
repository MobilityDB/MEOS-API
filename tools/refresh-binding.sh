#!/usr/bin/env bash
# refresh-binding.sh — refresh ONE MobilityDB binding against the latest MEOS API, end to end,
# with one command. The single generic entry point for every binding (Go, Rust, Python, C#,
# and the JVM consumers): it composes the pieces that already define each leg and adds no
# derivation logic of its own, only the sequencing and the sibling checkouts, so it cannot
# drift from CI:
#   1. tools/provision-meos.sh          derives the catalog (and, for native/FFI bindings,
#                                        builds libmeos) from a MobilityDB checkout;
#   2. [JVM only] JMEOS regen-from-catalog.sh   builds the jar the JVM consumers bind;
#   3. the binding's own generate + build   regenerates its surface from the catalog.
#
# By default MobilityDB is taken at its latest master, so the refresh tracks the newest MEOS
# API. Point --mdb (or $MDB) at an existing checkout to refresh against in-flight work on any
# branch; a binding that pins a MEOS ref sets MDB_REF in its tools/refresh.conf.
#
# The binding describes its last leg in tools/refresh.conf (sourced). All keys are optional
# except BUILD_CMD:
#     ENGINE=go                             # informational label
#     BUILD_DIR=.                           # dir BUILD_CMD runs from, relative to the repo ("." = root)
#     BUILD_LIBMEOS=true                    # true for native/FFI bindings; false for pure-catalog
#     MDB_REF=master                        # MobilityDB ref to derive from (pinned bindings override)
#     FAMILIES=-DALL=ON                     # cmake family flags for the libmeos build
#     CATALOG_DEST=tools/meos-idl.json      # where to stage the catalog, relative to the repo
#     JMEOS_COORDS=org.jmeos:meos:1.0       # JVM only: build+install the JMEOS jar as these coords
#     BUILD_CMD='<generate + build + test>' # run from <repo>/<BUILD_DIR>
# BUILD_CMD runs with $PREFIX (libmeos install prefix, empty when BUILD_LIBMEOS=false),
# $CATALOG (absolute catalog path), $JAR (JVM only) exported; $SKIP_TESTS set with --skip-tests.
#
# Usage:
#   tools/refresh-binding.sh --binding <path> [options]
#
# Options:
#   --binding <path>    The binding repo to refresh (has tools/refresh.conf). Default: $PWD.
#                       (--consumer is accepted as an alias.)
#   --work-dir <path>   Scratch for sibling clones and the libmeos prefix. Default: <binding>/.meos-chain.
#   --mdb <path>        Existing MobilityDB checkout (any branch), used as-is. Env: MDB.
#   --jmeos <path>      Existing JMEOS checkout (JVM bindings), used as-is. Env: JMEOS.
#   --meos-api <path>   MEOS-API repo (holds provision-meos.sh). Default: this script's repo.
#   --mdb-ref <ref>     Override the MobilityDB ref (else refresh.conf MDB_REF, else master).
#   --jmeos-ref <ref>   JMEOS ref when --jmeos is not given. Default: main.
#   --families <flags>  Override the cmake family flags (else refresh.conf FAMILIES, else -DALL=ON).
#   --skip-tests        Build without running the binding's test suite (SKIP_TESTS=1 for BUILD_CMD).
#   --force             Rebuild libmeos even when the MobilityDB commit is unchanged.
#   -h, --help
set -euo pipefail

usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p;}' "$0"; }

BINDING="$PWD"
WORK_DIR=""
MDB="${MDB:-}"
JMEOS="${JMEOS:-}"
MEOSAPI="${MEOSAPI:-$(cd "$(dirname "$0")/.." && pwd)}"
MDB_REF_CLI=""
JMEOS_REF="main"
FAMILIES_CLI=""
SKIP_TESTS=0
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --binding|--consumer) BINDING="$2"; shift 2 ;;
    --work-dir)  WORK_DIR="$2"; shift 2 ;;
    --mdb)       MDB="$2"; shift 2 ;;
    --jmeos)     JMEOS="$2"; shift 2 ;;
    --meos-api)  MEOSAPI="$(cd "$2" && pwd)"; shift 2 ;;
    --mdb-ref)   MDB_REF_CLI="$2"; shift 2 ;;
    --jmeos-ref) JMEOS_REF="$2"; shift 2 ;;
    --families)  FAMILIES_CLI="$2"; shift 2 ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    --force)     FORCE=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "refresh-binding.sh: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

step() { echo; echo "==> $*" >&2; }

BINDING="$(cd "$BINDING" && pwd)"
[ -f "$BINDING/tools/refresh.conf" ] || {
  echo "refresh-binding.sh: '$BINDING' has no tools/refresh.conf" >&2; exit 2; }
[ -f "$MEOSAPI/tools/provision-meos.sh" ] || {
  echo "refresh-binding.sh: no tools/provision-meos.sh under --meos-api '$MEOSAPI'" >&2; exit 2; }
: "${WORK_DIR:=$BINDING/.meos-chain}"
mkdir -p "$WORK_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

# Read the binding's last-leg description; apply defaults, then let CLI flags override.
ENGINE=""; BUILD_DIR="."; BUILD_LIBMEOS="true"; MDB_REF="master"
FAMILIES="-DALL=ON"; CATALOG_DEST="tools/meos-idl.json"; JMEOS_COORDS=""; BUILD_CMD=""
# shellcheck source=/dev/null
. "$BINDING/tools/refresh.conf"
[ -n "$BUILD_CMD" ] || { echo "refresh.conf: BUILD_CMD is required" >&2; exit 2; }
[ -n "$MDB_REF_CLI" ]  && MDB_REF="$MDB_REF_CLI"
[ -n "$FAMILIES_CLI" ] && FAMILIES="$FAMILIES_CLI"

# A sibling is either an existing checkout (used as-is, any branch) or a fresh clone of the
# given ref into the work dir, kept current on re-runs.
resolve_repo() {  # name url ref existing_path -> echoes absolute path
  local name="$1" url="$2" ref="$3" existing="$4" dir
  if [ -n "$existing" ]; then
    (cd "$existing" && pwd); return
  fi
  dir="$WORK_DIR/$name"
  if [ -d "$dir/.git" ]; then
    git -C "$dir" fetch --quiet "$url" "$ref"
    git -C "$dir" checkout --quiet FETCH_HEAD
  else
    git clone --quiet --depth 1 --branch "$ref" "$url" "$dir" 2>/dev/null \
      || git clone --quiet "$url" "$dir"  # fall back to full clone for a non-branch ref
  fi
  (cd "$dir" && pwd)
}

step "Resolving MobilityDB @ ${MDB:-$MDB_REF} (MEOS-API $MEOSAPI)"
MDB="$(resolve_repo MobilityDB https://github.com/MobilityDB/MobilityDB "$MDB_REF" "$MDB")"
MDB_COMMIT="$(git -C "$MDB" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "MobilityDB $MDB ($MDB_COMMIT)" >&2

PREFIX="$WORK_DIR/prefix"
CATALOG="$WORK_DIR/meos-idl.json"
LIBMEOS="$PREFIX/lib/libmeos.so"
STAMP="$WORK_DIR/.mdb-commit"

step "Python dependencies for the catalog parse"
python3 -m pip install --quiet -r "$MEOSAPI/requirements.txt"

# The catalog (and, for native/FFI bindings, libmeos) — the slow leg. Skip it when the commit,
# the family flags and the build-libmeos choice are all already current, unless --force.
STAMP_KEY="$MDB_COMMIT $FAMILIES libmeos=$BUILD_LIBMEOS"
provision_current=0
if [ "$FORCE" = 0 ] && [ -f "$CATALOG" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$STAMP_KEY" ]; then
  if [ "$BUILD_LIBMEOS" != "true" ] || [ -f "$LIBMEOS" ]; then provision_current=1; fi
fi
if [ "$provision_current" = 1 ]; then
  step "catalog + libmeos already current for $MDB_COMMIT ($FAMILIES) (use --force to rebuild)"
else
  step "Deriving the catalog from MobilityDB $MDB_COMMIT ($FAMILIES, build-libmeos=$BUILD_LIBMEOS)"
  if [ "$BUILD_LIBMEOS" = "true" ]; then
    "$MEOSAPI/tools/provision-meos.sh" --mdb-src "$MDB" --build-libmeos \
      --families "$FAMILIES" --parse-prefix "$PREFIX" --catalog-out "$CATALOG"
  else
    "$MEOSAPI/tools/provision-meos.sh" --mdb-src "$MDB" --catalog-out "$CATALOG"
  fi
  echo "$STAMP_KEY" > "$STAMP"
fi
[ "$BUILD_LIBMEOS" = "true" ] || { PREFIX=""; LIBMEOS=""; }

# JVM only: build + install the JMEOS jar the consumers bind.
JAR=""
if [ -n "$JMEOS_COORDS" ]; then
  JMEOS="$(resolve_repo JMEOS https://github.com/MobilityDB/JMEOS "$JMEOS_REF" "$JMEOS")"
  step "Building and installing the JMEOS jar ($JMEOS_COORDS) from the catalog"
  CATALOG="$CATALOG" LIBMEOS="$LIBMEOS" "$JMEOS/tools/regen-from-catalog.sh"
  IFS=: read -r G A V <<EOF
$JMEOS_COORDS
EOF
  mvn -q -f "$JMEOS/pom.xml" install:install-file \
    -Dfile="$JMEOS/jar/JMEOS.jar" -DgroupId="$G" -DartifactId="$A" -Dversion="$V" -Dpackaging=jar
  JAR="$JMEOS/jar/JMEOS.jar"
fi

# Stage the catalog where the generator reads it, when CATALOG_DEST is set. A binding whose
# generator takes the catalog path as an argument sets CATALOG_DEST= (empty) and reads $CATALOG.
if [ -n "$CATALOG_DEST" ]; then
  mkdir -p "$(dirname "$BINDING/$CATALOG_DEST")"
  cp "$CATALOG" "$BINDING/$CATALOG_DEST"
fi

step "Building the ${ENGINE:-binding} surface from the catalog"
export PREFIX CATALOG JAR
[ "$SKIP_TESTS" = 1 ] && export SKIP_TESTS=1
( cd "$BINDING/$BUILD_DIR" && eval "$BUILD_CMD" )

step "Done — ${ENGINE:-binding} refreshed against MobilityDB $MDB_COMMIT"
if [ -n "$CATALOG_DEST" ]; then echo "  catalog : $BINDING/$CATALOG_DEST" >&2
else echo "  catalog : $CATALOG" >&2; fi
[ -n "$LIBMEOS" ] && echo "  libmeos : $LIBMEOS" >&2
[ -n "$JAR" ] && echo "  jmeos   : $JAR ($JMEOS_COORDS)" >&2
