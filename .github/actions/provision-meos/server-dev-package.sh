#!/usr/bin/env bash
# server-dev-package.sh — print the package holding the server headers of the PostgreSQL the
# runner image ships: postgresql-server-dev-16 on ubuntu-24.04 (PostgreSQL 16), and
# postgresql-server-dev-18 on ubuntu-26.04 (PostgreSQL 18). The major version is the directory
# the image installs under /usr/lib/postgresql, so the headers libclang reads follow the image
# instead of a version written into the install lists of this action and of this repository's
# workflows. postgresql-server-dev-all cannot stand in for it: the images ship postgresql-common
# from apt.postgresql.org, which the archive's postgresql-server-dev-all does not accept.
#
# Usage:
#   apt-get.sh install -y "$(server-dev-package.sh)"
set -euo pipefail

major="$(find /usr/lib/postgresql -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null |
  grep -E '^[0-9]+$' | sort -n | tail -1 || true)"
if [ -z "$major" ]; then
  echo "server-dev-package.sh: the image installs no PostgreSQL under /usr/lib/postgresql" >&2
  exit 1
fi
echo "postgresql-server-dev-$major"
