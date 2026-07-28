#!/usr/bin/env bash
# refresh-jvm-chain.sh — refresh one JVM consumer (MobilityFlink / MobilitySpark /
# MobilityKafka) against the latest MEOS API, end to end, with one command.
#
# It is the whole chain
#     MobilityDB  ->  MEOS-API (catalog + libmeos)  ->  JMEOS (jar)  ->  consumer (facades)
# composed from the pieces that already define each leg — this adds no derivation logic of
# its own, only the sequencing and the sibling checkouts, so it cannot drift from CI:
#   1. tools/provision-meos.sh   builds libmeos and the catalog from a MobilityDB checkout;
#   2. JMEOS tools/regen-from-catalog.sh   builds the jar the consumers bind;
#   3. the consumer's own Maven build   regenerates its facades at generate-sources.
#
# By default every sibling is taken at its latest upstream default branch, so the refresh
# tracks the newest MEOS API. Point --mdb / --jmeos (or $MDB / $JMEOS) at an existing checkout
# to refresh against in-flight work on any branch instead — e.g. a MEOS function you have not
# merged yet.
#
# The consumer describes its last leg in tools/refresh.conf (sourced), a few shell lines:
#     ENGINE=flink                          # flink | spark | kafka
#     BUILD_DIR=flink-processor             # Maven module dir, relative to the repo root ("." = root)
#     JMEOS_COORDS=org.jmeos:meos:1.0       # groupId:artifactId:version the build resolves the jar as
#     BUILD_CMD='mvn -B -Dmeos.lib.dir="$PREFIX/lib" -Dmeos.enabled=true clean test'
# BUILD_CMD runs from <consumer>/<BUILD_DIR> with $PREFIX (the libmeos install prefix),
# $CATALOG and $JAR exported.
#
# Usage:
#   tools/refresh-jvm-chain.sh --consumer <path> [options]
#
# Options:
#   --consumer <path>   The JVM consumer repo to refresh (has tools/refresh.conf). Default: $PWD.
#   --work-dir <path>   Scratch for sibling clones and the libmeos prefix.
#                       Default: <consumer>/.meos-chain.
#   --mdb <path>        Existing MobilityDB checkout (any branch), used as-is. Env: MDB.
#                       Otherwise MobilityDB is cloned at --mdb-ref into the work dir.
#   --jmeos <path>      Existing JMEOS checkout, used as-is. Env: JMEOS.
#                       Otherwise JMEOS is cloned at --jmeos-ref into the work dir.
#   --meos-api <path>   MEOS-API repo (holds provision-meos.sh). Default: this script's repo.
#   --mdb-ref <ref>     Ref to clone MobilityDB at when --mdb is not given. Default: master.
#   --jmeos-ref <ref>   Ref to clone JMEOS at when --jmeos is not given. Default: main.
#   --families <flags>  cmake family flags for the libmeos build. Default: -DALL=ON.
#   --skip-tests        Build the consumer without running its test suite (SKIP_TESTS=1 for BUILD_CMD).
#   --force             Rebuild libmeos even when the MobilityDB commit is unchanged.
#   -h, --help
set -euo pipefail

usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p;}' "$0"; }

CONSUMER="$PWD"
WORK_DIR=""
MDB="${MDB:-}"
JMEOS="${JMEOS:-}"
MEOSAPI="${MEOSAPI:-$(cd "$(dirname "$0")/.." && pwd)}"
MDB_REF="master"
JMEOS_REF="main"
FAMILIES="-DALL=ON"
SKIP_TESTS=0
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --consumer)  CONSUMER="$2"; shift 2 ;;
    --work-dir)  WORK_DIR="$2"; shift 2 ;;
    --mdb)       MDB="$2"; shift 2 ;;
    --jmeos)     JMEOS="$2"; shift 2 ;;
    --meos-api)  MEOSAPI="$(cd "$2" && pwd)"; shift 2 ;;
    --mdb-ref)   MDB_REF="$2"; shift 2 ;;
    --jmeos-ref) JMEOS_REF="$2"; shift 2 ;;
    --families)  FAMILIES="$2"; shift 2 ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    --force)     FORCE=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "refresh-jvm-chain.sh: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

step() { echo; echo "==> $*" >&2; }

CONSUMER="$(cd "$CONSUMER" && pwd)"
[ -f "$CONSUMER/tools/refresh.conf" ] || {
  echo "refresh-jvm-chain.sh: '$CONSUMER' has no tools/refresh.conf" >&2; exit 2; }
[ -f "$MEOSAPI/tools/provision-meos.sh" ] || {
  echo "refresh-jvm-chain.sh: no tools/provision-meos.sh under --meos-api '$MEOSAPI'" >&2; exit 2; }
: "${WORK_DIR:=$CONSUMER/.meos-chain}"
mkdir -p "$WORK_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

# A sibling is either an existing checkout (used as-is, any branch) or a fresh clone of the
# upstream default branch into the work dir, kept current on re-runs.
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

step "Resolving siblings (MobilityDB @ ${MDB:-$MDB_REF}, JMEOS @ ${JMEOS:-$JMEOS_REF}, MEOS-API $MEOSAPI)"
MDB="$(resolve_repo MobilityDB https://github.com/MobilityDB/MobilityDB "$MDB_REF" "$MDB")"
JMEOS="$(resolve_repo JMEOS https://github.com/MobilityDB/JMEOS "$JMEOS_REF" "$JMEOS")"
MDB_COMMIT="$(git -C "$MDB" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "MobilityDB $MDB ($MDB_COMMIT)" >&2

PREFIX="$WORK_DIR/prefix"
CATALOG="$WORK_DIR/meos-idl.json"
LIBMEOS="$PREFIX/lib/libmeos.so"
STAMP="$WORK_DIR/.mdb-commit"

step "Python dependencies for the catalog parse"
python3 -m pip install --quiet -r "$MEOSAPI/requirements.txt"

# libmeos + catalog: the slow leg. Skip it when the MobilityDB commit and the built library
# are both already current, unless --force. The stamp carries the commit AND the family flags,
# so changing --families rebuilds rather than reusing a library with a different surface.
STAMP_KEY="$MDB_COMMIT $FAMILIES"
if [ "$FORCE" = 0 ] && [ -f "$LIBMEOS" ] && [ -f "$CATALOG" ] \
   && [ "$(cat "$STAMP" 2>/dev/null)" = "$STAMP_KEY" ]; then
  step "libmeos + catalog already current for $MDB_COMMIT ($FAMILIES) (use --force to rebuild)"
else
  step "Building libmeos + catalog from MobilityDB $MDB_COMMIT ($FAMILIES)"
  "$MEOSAPI/tools/provision-meos.sh" \
    --mdb-src "$MDB" \
    --build-libmeos \
    --families "$FAMILIES" \
    --parse-prefix "$PREFIX" \
    --catalog-out "$CATALOG"
  echo "$STAMP_KEY" > "$STAMP"
fi

# Read the consumer's last-leg description.
ENGINE=""; BUILD_DIR="."; JMEOS_COORDS=""; BUILD_CMD=""
# shellcheck source=/dev/null
. "$CONSUMER/tools/refresh.conf"
[ -n "$JMEOS_COORDS" ] || { echo "refresh.conf: JMEOS_COORDS is required" >&2; exit 2; }
[ -n "$BUILD_CMD" ]    || { echo "refresh.conf: BUILD_CMD is required" >&2; exit 2; }

step "Building and installing the JMEOS jar ($JMEOS_COORDS) from the catalog"
CATALOG="$CATALOG" LIBMEOS="$LIBMEOS" "$JMEOS/tools/regen-from-catalog.sh"
IFS=: read -r G A V <<EOF
$JMEOS_COORDS
EOF
mvn -q -f "$JMEOS/pom.xml" install:install-file \
  -Dfile="$JMEOS/jar/JMEOS.jar" -DgroupId="$G" -DartifactId="$A" -Dversion="$V" -Dpackaging=jar

step "Staging the catalog into the consumer and building the ${ENGINE:-consumer} facades"
mkdir -p "$CONSUMER/tools"
cp "$CATALOG" "$CONSUMER/tools/meos-idl.json"

export PREFIX CATALOG
export JAR="$JMEOS/jar/JMEOS.jar"
[ "$SKIP_TESTS" = 1 ] && export SKIP_TESTS=1
( cd "$CONSUMER/$BUILD_DIR" && eval "$BUILD_CMD" )

step "Done — ${ENGINE:-consumer} refreshed against MobilityDB $MDB_COMMIT"
echo "  catalog : $CONSUMER/tools/meos-idl.json" >&2
echo "  libmeos : $LIBMEOS" >&2
echo "  jmeos   : $JMEOS/jar/JMEOS.jar ($JMEOS_COORDS)" >&2
