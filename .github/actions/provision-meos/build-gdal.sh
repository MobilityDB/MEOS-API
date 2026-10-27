#!/usr/bin/env bash
# build-gdal.sh — build the GDAL libmeos links, from its release source, without GEOS, into a
# prefix the provision-meos action caches and installs under /usr/local. MEOS calls no GEOS
# function, and the GDAL a distribution packages (libgdal-dev on Ubuntu) links GEOS, so a
# libmeos linked against it loads GEOS with it; this GDAL loads none.
#
# The configuration is the one MEOS.js builds its GDAL with: every optional driver off, the
# libraries GDAL carries internally used in place of external ones, and only PROJ, SQLite and
# zlib taken from the system, which libmeos already needs. MEOS reads raster bands through the
# generic GDAL API and the built-in drivers (MEM, VRT, GTiff), so the core is the whole
# requirement. The libraries are shared, as the hosted bindings link libmeos.so.
#
# The release is pinned by its SHA-256, so the build is the same on every runner and a changed
# file fails the step rather than building something else.
#
# Usage:
#   build-gdal.sh <prefix>
#
# Environment:
#   CMAKE_BUILD_PARALLEL_LEVEL   build jobs (default: the number of processors)
set -euo pipefail

PREFIX="${1:?usage: build-gdal.sh <prefix>}"
VERSION=3.12.0
SHA256=44c95baabe6c1a047c1ebe5043e38dd73e4936bc4c481db14efbfd03342eab73
URL="https://github.com/OSGeo/gdal/releases/download/v$VERSION/gdal-$VERSION.tar.gz"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

curl -fsSL --retry 3 --max-time 600 -o "$WORK/gdal.tar.gz" "$URL"
echo "$SHA256  $WORK/gdal.tar.gz" | sha256sum -c -
tar xzf "$WORK/gdal.tar.gz" -C "$WORK"

cmake -S "$WORK/gdal-$VERSION" -B "$WORK/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DBUILD_SHARED_LIBS=ON \
  -DBUILD_APPS=OFF \
  -DBUILD_TESTING=OFF \
  -DBUILD_DOCS=OFF \
  -DBUILD_PYTHON_BINDINGS=OFF \
  -DBUILD_JAVA_BINDINGS=OFF \
  -DBUILD_CSHARP_BINDINGS=OFF \
  -DGDAL_BUILD_OPTIONAL_DRIVERS=OFF \
  -DOGR_BUILD_OPTIONAL_DRIVERS=OFF \
  -DGDAL_USE_EXTERNAL_LIBS=OFF \
  -DGDAL_USE_INTERNAL_LIBS=ON \
  -DGDAL_USE_PROJ=ON \
  -DGDAL_USE_GEOS=OFF \
  -DGDAL_USE_SQLITE3=ON \
  -DGDAL_USE_ZLIB=ON
cmake --build "$WORK/build" -j "${CMAKE_BUILD_PARALLEL_LEVEL:-$(nproc)}"
cmake --install "$WORK/build"
