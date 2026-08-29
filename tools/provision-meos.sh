#!/usr/bin/env bash
# provision-meos.sh — derive the MEOS catalog (output/meos-idl.json) from a MobilityDB
# checkout, and optionally build and install libmeos. This is the single, runnable
# definition of that derivation: the provision-meos GitHub action calls it, and a human
# refreshing the chain locally calls the same script, so the CI path and the by-hand path
# cannot drift.
#
# It does not check out MobilityDB, install system packages, or set up Python — those are
# environment bootstrapping (the action does them as its own steps; a developer has the
# build/parse dependencies and `pip install -r requirements.txt` already). This script is
# the reproducible recipe: configure, build, install, parse.
#
# The header source decides the catalog, deliberately: without --build-libmeos the
# source-tree headers are parsed (no build, approximate struct layouts); with it the
# installed headers are parsed (full FFI-accurate layouts). MDB_SRC_ROOT is the source
# checkout either way, for the @ingroup/@sqlfn maps.
#
# Usage:
#   tools/provision-meos.sh --mdb-src <path> [options]
#
# Required:
#   --mdb-src <path>       MobilityDB source checkout to derive from.
#
# Options:
#   --build-libmeos        Build and install libmeos, and parse the *installed* headers.
#   --families <flags>     cmake family flags for the libmeos build (default: -DALL=ON).
#   --parse-prefix <dir>   Clean prefix the MEOS headers (and libmeos) are installed into
#                          for the parse; its include/ holds only MEOS headers, so the
#                          catalog glob does not pull in system headers. Build path only.
#                          Default: <mdb-src>/.prefix.
#   --runtime-prefix <dir> Prefix libmeos is additionally installed into for runtime use.
#                          Installed with sudo when not writable (e.g. /usr/local in CI).
#                          Default: same as --parse-prefix (one install, no sudo).
#   --meos-api <path>      MEOS-API repo root (holds run.py). Default: this script's repo.
#   --catalog-out <path>   Copy the catalog here in addition to <meos-api>/output/meos-idl.json.
#   -h, --help
#
# Outputs (printed as KEY=value, and appended to $GITHUB_OUTPUT when set):
#   catalog-path=<abs path to meos-idl.json>
#   libmeos-prefix=<abs runtime prefix, or empty when --build-libmeos is not given>
set -euo pipefail

usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p;}' "$0"; }

MDB_SRC=""
BUILD_LIBMEOS=0
FAMILIES="-DALL=ON"
PARSE_PREFIX=""
RUNTIME_PREFIX=""
MEOSAPI="$(cd "$(dirname "$0")/.." && pwd)"
CATALOG_OUT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --mdb-src)        MDB_SRC="$2"; shift 2 ;;
    --build-libmeos)  BUILD_LIBMEOS=1; shift ;;
    --families)       FAMILIES="$2"; shift 2 ;;
    --parse-prefix)   PARSE_PREFIX="$2"; shift 2 ;;
    --runtime-prefix) RUNTIME_PREFIX="$2"; shift 2 ;;
    --meos-api)       MEOSAPI="$(cd "$2" && pwd)"; shift 2 ;;
    --catalog-out)    CATALOG_OUT="$2"; shift 2 ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "provision-meos.sh: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$MDB_SRC" ] || { echo "provision-meos.sh: --mdb-src is required" >&2; exit 2; }
[ -d "$MDB_SRC/meos/include" ] || {
  echo "provision-meos.sh: '$MDB_SRC' is not a MobilityDB checkout (no meos/include)" >&2; exit 2; }
MDB_SRC="$(cd "$MDB_SRC" && pwd)"
[ -f "$MEOSAPI/run.py" ] || {
  echo "provision-meos.sh: no run.py under --meos-api '$MEOSAPI'" >&2; exit 2; }

# Install libmeos into a prefix; use sudo only when the prefix is not writable, so a system
# prefix (/usr/local) works in CI and a private prefix needs no privileges.
install_into() {
  local prefix="$1" parent
  parent="$(dirname "$prefix")"
  if [ -w "$prefix" ] || { [ ! -e "$prefix" ] && [ -w "$parent" ]; }; then
    cmake --install "$MDB_SRC/build" --prefix "$prefix"
  else
    sudo cmake --install "$MDB_SRC/build" --prefix "$prefix"
  fi
}

LIBMEOS_PREFIX=""
if [ "$BUILD_LIBMEOS" = 1 ]; then
  : "${PARSE_PREFIX:=$MDB_SRC/.prefix}"
  : "${RUNTIME_PREFIX:=$PARSE_PREFIX}"

  # Optional CI-provisioned toolchain locations: pass each only when it exists, so CI (where
  # they do) stays byte-for-byte and a developer's cmake auto-detects its own instead.
  # The install prefix belongs to the CONFIGURE step, not only to the install:
  # meos.pc is generated from CMAKE_INSTALL_PREFIX at configure time, so a build
  # configured for the default prefix keeps naming /usr/local no matter where
  # `cmake --install --prefix` puts the files. A consumer that follows the
  # installed library's own pkg-config file -- which is how a generated binding
  # discovers the include dir and the family macros -- would then be sent to
  # whatever occupies the machine-wide directory instead of this prefix.
  cfg=(-DCMAKE_BUILD_TYPE=Release -DMEOS=ON -DCMAKE_INSTALL_PREFIX="$PARSE_PREFIX" $FAMILIES)
  [ -e "${H3_LIBRARY:=/usr/lib/x86_64-linux-gnu/libh3.so}" ] && cfg+=(-DH3_LIBRARY="$H3_LIBRARY")
  [ -e "${H3_INCLUDE_DIR:=/usr/include/h3}" ]                && cfg+=(-DH3_INCLUDE_DIR="$H3_INCLUDE_DIR")
  [ -x "${PG_CONFIG:=/usr/lib/postgresql/17/bin/pg_config}" ] && cfg+=(-DPOSTGRESQL_PG_CONFIG="$PG_CONFIG")

  cmake -S "$MDB_SRC" -B "$MDB_SRC/build" "${cfg[@]}"
  cmake --build "$MDB_SRC/build" -j "$(nproc)"

  # The parse prefix holds a clean MEOS-only include/; install there first (writable), then
  # into the runtime prefix if it is a different location (e.g. the shared /usr/local).
  install_into "$PARSE_PREFIX"
  [ "$RUNTIME_PREFIX" != "$PARSE_PREFIX" ] && install_into "$RUNTIME_PREFIX"

  PARSE_HEADERS="$PARSE_PREFIX/include"
  LIBMEOS_PREFIX="$RUNTIME_PREFIX"
else
  # No build: parse the source-tree headers directly.
  PARSE_HEADERS="$MDB_SRC/meos/include"
fi

cd "$MEOSAPI"
MDB_SRC_ROOT="$MDB_SRC" python3 run.py "$PARSE_HEADERS"
CATALOG="$MEOSAPI/output/meos-idl.json"
[ -s "$CATALOG" ] || { echo "::error::catalog not generated or empty at $CATALOG" >&2; exit 1; }

if [ -n "$CATALOG_OUT" ]; then
  mkdir -p "$(dirname "$CATALOG_OUT")"
  cp "$CATALOG" "$CATALOG_OUT"
  CATALOG="$(cd "$(dirname "$CATALOG_OUT")" && pwd)/$(basename "$CATALOG_OUT")"
fi

echo "catalog-path=$CATALOG"
echo "libmeos-prefix=$LIBMEOS_PREFIX"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "catalog-path=$CATALOG"
    echo "libmeos-prefix=$LIBMEOS_PREFIX"
  } >> "$GITHUB_OUTPUT"
fi
echo "Catalog written: $CATALOG ($(wc -c < "$CATALOG") bytes)" >&2
