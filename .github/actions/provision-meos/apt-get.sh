#!/usr/bin/env bash
# apt-get.sh — run one `sudo apt-get` command with a bound on its wall time and a retry, so a
# stalled or unreachable package mirror fails the step in minutes rather than holding the job
# until GitHub's six-hour limit. Every apt-get call of the provision-meos action and of this
# repository's workflows goes through it; a composite action step accepts no `timeout-minutes`,
# so the bound lives here, in the one place each consumer runs.
#
# The per-request options bound one download (`Acquire::*::Timeout`) and the wait for the dpkg
# lock (`DPkg::Lock::Timeout`), not the command: a mirror serving at a crawl passes every
# per-request timeout and still takes as long as it likes. `timeout` bounds the command itself.
# An attempt that exceeds its bound or fails is retried, and the step fails once every attempt is
# spent; a runner whose mirror is slow but serving completes within the bound, which is set well
# above a normal install (four minutes for the whole step).
#
# Usage:
#   apt-get.sh <apt-get arguments...>        e.g. apt-get.sh update -qq
#
# Environment:
#   APT_TIMEOUT_UPDATE   seconds an `update` attempt may take (default 600)
#   APT_TIMEOUT          seconds any other attempt may take (default 600)
#   APT_ATTEMPTS         attempts before giving up (default 3)
#   APT_ARCHIVES         directory the downloaded .deb files are kept in (default: apt's own
#                        /var/cache/apt/archives); a runner-writable directory a cache saves and
#                        restores, so a later run installs from it instead of the mirror
set -uo pipefail

attempts="${APT_ATTEMPTS:-3}"
archive_opts=()
if [[ -n "${APT_ARCHIVES:-}" ]]; then
  mkdir -p "$APT_ARCHIVES/partial"
  archive_opts=(-o "Dir::Cache::archives=$APT_ARCHIVES" -o APT::Keep-Downloaded-Packages=true)
fi
if [[ "${1:-}" == "update" ]]; then
  bound="${APT_TIMEOUT_UPDATE:-600}"
else
  bound="${APT_TIMEOUT:-600}"
fi

for ((i = 1; i <= attempts; i++)); do
  timeout --kill-after=30 "$bound" sudo apt-get \
    -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 \
    -o DPkg::Lock::Timeout=300 "${archive_opts[@]}" "$@"
  status=$?
  if [[ $status -eq 0 ]]; then
    exit 0
  fi
  if [[ $status -eq 124 || $status -eq 137 ]]; then
    echo "::warning::apt-get $1 exceeded ${bound}s (attempt $i of $attempts)" >&2
  else
    echo "::warning::apt-get $1 failed with status $status (attempt $i of $attempts)" >&2
  fi
done
echo "::error::apt-get $* did not complete in $attempts attempts of at most ${bound}s" >&2
exit "$status"
