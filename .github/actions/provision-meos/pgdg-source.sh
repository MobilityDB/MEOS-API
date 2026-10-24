#!/usr/bin/env bash
# pgdg-source.sh — add apt.postgresql.org (PGDG), the repository the runner images install their
# PostgreSQL from, as an APT source. The images remove that source once PostgreSQL is installed,
# so the archive's server-dev package, which requires the archive's client of the same version,
# conflicts with the image's PGDG client (postgresql-server-dev-18 on ubuntu-26.04); with the
# source back, the server headers come from the repository the installed PostgreSQL came from.
# The key goes into a keyring the source names (`signed-by`), as the images' own installer does:
# apt-key, which an earlier version of this action used, is gone from apt 3 (Ubuntu 26.04).
#
# Usage:
#   pgdg-source.sh && apt-get.sh update -qq
set -euo pipefail

keyring=/usr/share/keyrings/postgresql.gpg
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor --yes -o "$keyring"
echo "deb [signed-by=$keyring] https://apt.postgresql.org/pub/repos/apt/ $(lsb_release -cs)-pgdg main" |
  sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
