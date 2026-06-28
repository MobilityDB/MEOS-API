#!/usr/bin/env bash
# ecosystem-generate.sh — regenerate the WHOLE MobilityDB binding ecosystem from a pin.
#
# Usage:  tools/ecosystem-generate.sh <ecosystem-pin-tag | sha> [WORKDIR]
#
# It runs the chain in DEPENDENCY ORDER (the JVM consumers after the JMEOS jar; PyMEOS after
# PyMEOS-CFFI). Each binding OWNS its regeneration (its own tools/regen-from-pin.sh, driven by
# its own tools/pin/compose-order.txt); this script only sequences them and produces the
# catalog + libmeos.so the chain starts from.
#
# GH is the only source of truth: every repo is fetched fresh from GitHub at its FRONTIER
# branch (recorded below + in each binding's compose-order.txt). WORKDIR defaults to a $HOME
# path (NEVER /tmp — it is reaped).
set -euo pipefail

PIN_REF="${1:?usage: ecosystem-generate.sh <pin-tag|sha> [workdir]}"
WORK="${2:-$HOME/ecosystem-gen}"
MDB="${MDB_REPO:-$HOME/src/MobilityDB}"
MEOSAPI="$(cd "$(dirname "$0")/.." && pwd)"   # this repo
mkdir -p "$WORK"

# Repo -> frontier branch (the codegen lives in these OPEN PRs until merged; verified 2026-06-25).
# Once a binding's generator PR merges, switch its frontier to the default branch.
#   binding            owner/repo                       frontier-branch
#   JMEOS              estebanzimanyi/JMEOS             feat/facade-surface-22a        (PR #28)
#   GoMEOS             estebanzimanyi/GoMEOS            codegen/flat-wrappers-22a       (PR #5)
#   MEOS.NET           estebanzimanyi/MEOS.NET          work/portable-aliases           (PR #5)
#   PyMEOS-CFFI        estebanzimanyi/PyMEOS-CFFI       bump/meos-1.4                   (PR #19)
#   PyMEOS             estebanzimanyi/PyMEOS            feat/oo-dispatch-consumer       (PR #95)
#   MobilitySpark      estebanzimanyi/MobilitySpark     feat/generated-dispatch         (PR #28)
#   MobilityFlink      estebanzimanyi/MobilityFlink     consolidate/flink-benchmark     (PR #31)
#   MobilityKafka      estebanzimanyi/MobilityKafka     consolidate/kafka-benchmark     (no PR — topology gap)
#   MobilityNebula     estebanzimanyi/MobilityNebula    feat/nebula-codegen-generator-infra (PR #170)
#   meos-rs            MobilityDB/meos-rs               main  (bindgen today; catalog migration pending)
#   MobilityDuck       MobilityDB/MobilityDuck          main  (generator is WIP, not yet on GH)

log() { printf '\n=== %s ===\n' "$*" >&2; }
regen() { # owner/repo  frontier  -- runs the binding's own regen-from-pin.sh
  local slug="$1" branch="$2" dir="$WORK/${1##*/}"
  rm -rf "$dir"; git clone --quiet --branch "$branch" "https://github.com/$slug" "$dir" \
    || { echo "SKIP $slug ($branch unavailable)"; return 0; }
  if [ -x "$dir/tools/regen-from-pin.sh" ]; then
    ( cd "$dir" && CATALOG="$CATALOG" LIBMEOS="$LIBMEOS" JMEOS_JAR="${JMEOS_JAR:-}" \
        tools/regen-from-pin.sh "$PIN" ) || echo "WARN: $slug regen returned non-zero"
  else
    echo "NOTE: $slug has no tools/regen-from-pin.sh yet (frontier $branch) — see its GENERATION.md"
  fi
}

# ── PHASE 0 — pin -> catalog + libmeos.so (the root every binding consumes) ──
log "PHASE 0: resolve pin + build catalog + libmeos"
git -C "$MDB" fetch origin --tags --force --quiet
PIN="$(git -C "$MDB" rev-parse "${PIN_REF}^{commit}" 2>/dev/null || git -C "$MDB" rev-parse "$PIN_REF")"
PINWT="$WORK/pin"; rm -rf "$PINWT"; git -C "$MDB" worktree add --detach "$PINWT" "$PIN"
( cd "$MEOSAPI" && MDB_SRC_ROOT="$PINWT" python3 run.py "$PINWT/meos/include" )
export CATALOG="$MEOSAPI/output/meos-idl.json"
# all-families libmeos (the runtime every binding loads); see generation-starts-from-building-so
cmake -S "$PINWT" -B "$PINWT/build-allfam" -DMEOS=ON -DCBUFFER=ON -DJSON=ON -DNPOINT=ON \
  -DPOSE=ON -DRGEO=ON -DQUADBIN=ON -DH3=ON \
  -DH3_INCLUDE_DIR=/usr/include/h3 -DH3_LIBRARY=/usr/lib/x86_64-linux-gnu/libh3.so >/dev/null
cmake --build "$PINWT/build-allfam" --target meos -j"$(nproc)"
export LIBMEOS="$PINWT/build-allfam/libmeos.so"

# ── PHASE 1 — JMEOS jar + the non-JVM bindings (all consume the catalog) ──
log "PHASE 1: JMEOS jar + non-JVM bindings"
regen estebanzimanyi/JMEOS       feat/facade-surface-22a
export JMEOS_JAR="$WORK/JMEOS/jar/JMEOS.jar"   # produced by JMEOS's regen-from-pin.sh
regen estebanzimanyi/GoMEOS      codegen/flat-wrappers-22a
regen estebanzimanyi/MEOS.NET    work/portable-aliases
regen estebanzimanyi/PyMEOS-CFFI bump/meos-1.4
regen MobilityDB/meos-rs         main
regen MobilityDB/MobilityDuck    main
regen estebanzimanyi/MobilityNebula feat/nebula-codegen-generator-infra

# ── PHASE 2 — consumers of phase-1 artifacts ──
log "PHASE 2: PyMEOS (needs CFFI) + JVM stream consumers (need the JMEOS jar)"
regen estebanzimanyi/PyMEOS         feat/oo-dispatch-consumer
regen estebanzimanyi/MobilitySpark  feat/generated-dispatch
regen estebanzimanyi/MobilityFlink  consolidate/flink-benchmark
regen estebanzimanyi/MobilityKafka  consolidate/kafka-benchmark

log "DONE — catalog=$CATALOG libmeos=$LIBMEOS jmeos=$JMEOS_JAR ; per-binding output under $WORK"
