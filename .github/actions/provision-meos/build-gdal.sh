#!/usr/bin/env bash
# build-gdal.sh — build the GDAL libmeos links, from its release source, without GEOS, into a
# prefix the provision-meos action caches and installs under /usr/local. MEOS calls no GEOS
# function, and the GDAL a distribution packages (libgdal-dev on Ubuntu) links GEOS, so a
# libmeos linked against it loads GEOS with it; this GDAL loads none.
#
# The drivers are the standard raster formats MEOS writes and reads by name (asTIFF, asPNG,
# asJPEG, asGDALRaster and the readers taking the bytes of a file): GTiff with the
# Cloud-Optimized GeoTIFF COG built from its sources, PNG, JPEG, GIF and netCDF, beside the MEM
# and VRT drivers of the core and the GeoJSON and Shapefile vector drivers. Every other optional
# driver is off, the libraries those drivers need are the ones GDAL carries internally (libtiff,
# libgeotiff, libpng, libjpeg, giflib), and only PROJ, SQLite, zlib and the netCDF library
# (libnetcdf-dev, which GDAL carries no copy of) come from the system. The libraries are shared,
# as the hosted bindings link libmeos.so.
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

# GDAL's CheckCXXCompilerFlag passes backslashes to a macro, which CMake 4 reports unless
# CMP0219 is set; the policy is set inside the project, where CMake reads it.
cat > "$WORK/policies.cmake" <<'EOF'
if(POLICY CMP0219)
  cmake_policy(SET CMP0219 NEW)
endif()
EOF

cmake -S "$WORK/gdal-$VERSION" -B "$WORK/build" \
  -DCMAKE_PROJECT_INCLUDE_BEFORE="$WORK/policies.cmake" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DBUILD_SHARED_LIBS=ON \
  -DBUILD_APPS=OFF \
  -DBUILD_TESTING=OFF \
  -DBUILD_PYTHON_BINDINGS=OFF \
  -DBUILD_JAVA_BINDINGS=OFF \
  -DBUILD_CSHARP_BINDINGS=OFF \
  -DGDAL_BUILD_OPTIONAL_DRIVERS=OFF \
  -DOGR_BUILD_OPTIONAL_DRIVERS=OFF \
  -DGDAL_ENABLE_DRIVER_GTIFF=ON \
  -DGDAL_ENABLE_DRIVER_PNG=ON \
  -DGDAL_ENABLE_DRIVER_JPEG=ON \
  -DGDAL_ENABLE_DRIVER_GIF=ON \
  -DGDAL_ENABLE_DRIVER_VRT=ON \
  -DGDAL_ENABLE_DRIVER_NETCDF=ON \
  -DOGR_ENABLE_DRIVER_GEOJSON=ON \
  -DOGR_ENABLE_DRIVER_SHAPE=ON \
  -DGDAL_USE_EXTERNAL_LIBS=OFF \
  -DGDAL_USE_INTERNAL_LIBS=ON \
  -DGDAL_USE_PROJ=ON \
  -DGDAL_USE_GEOS=OFF \
  -DGDAL_USE_SQLITE3=ON \
  -DGDAL_USE_NETCDF=ON \
  -DGDAL_USE_ZLIB=ON \
  -DGDAL_USE_ZLIB_INTERNAL=OFF
cmake --build "$WORK/build" -j "${CMAKE_BUILD_PARALLEL_LEVEL:-$(nproc)}"
cmake --install "$WORK/build"
