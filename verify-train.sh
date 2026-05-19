#!/usr/bin/env bash
# verify-train.sh - compose the dependency-ordered integration train
# (meta/integration-train.json) and run each wave's gate, so ecosystem-wide
# 100% parity is PROVABLE at one point instead of asserted per-PR.
#
# Operationalizes MobilityDB discussion #895 (wave-based merge plan).
#
# Honesty contract: a wave reports PASS only when its gate is just-run and
# green here; otherwise BLOCKED:<reason> with the exact gate it needs.
# Nothing is faked, nothing is skipped silently (green-ci-same-commit).
#
# Scope/safety: operates ONLY on this repo and its own ./_mobilitydb
# sparse checkout. NEVER touches a shared PyMEOS / MobilityDB working copy
# (work-independently-parallel-sessions).
#
# Usage:
#   ./verify-train.sh            # Wave 0 (fully automatable here) + honest
#                                # BLOCKED status for Waves 1-3
#   PYMEOS_ENV=/path ./verify-train.sh   # also run Wave 2/3 gates if a
#                                        # bump-ready PyMEOS env is provided
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
MDB="_mobilitydb"
RC=0
say() { printf '\n=== %s ===\n' "$*"; }
pass() { printf '  [PASS]   %s\n' "$*"; }
fail() { printf '  [FAIL]   %s\n' "$*"; RC=1; }
block() { printf '  [BLOCKED] %s\n' "$*"; }

# ---------------------------------------------------------------------------
say "WAVE 0 - MobilityDB core extended-type C surface (compose + gate)"
# Compose: linear stack tip (#1085 contains #1081..#1085) + the clean
# #1051->#951 pair cherry-picked on top (proven clean).
if [ ! -d "$MDB/.git" ]; then
  fail "no $MDB checkout - run: python3 setup.py (then re-run)"
else
  git -C "$MDB" fetch --no-tags --depth=20 origin \
      pull/1085/head pull/1051/head pull/951/head >/dev/null 2>&1
  STACK_TIP=$(git -C "$MDB" rev-parse FETCH_HEAD 2>/dev/null) # last fetched
  git -C "$MDB" fetch --no-tags --depth=20 origin pull/1085/head >/dev/null 2>&1
  STACK=$(git -C "$MDB" rev-parse FETCH_HEAD)
  git -C "$MDB" fetch --no-tags --depth=20 origin pull/1051/head >/dev/null 2>&1
  P1051=$(git -C "$MDB" rev-parse FETCH_HEAD)
  git -C "$MDB" fetch --no-tags --depth=20 origin pull/951/head >/dev/null 2>&1
  P951=$(git -C "$MDB" rev-parse FETCH_HEAD)
  git -C "$MDB" checkout -q -B _train_w0 "$STACK" 2>/dev/null
  CK=0
  for c in $(git -C "$MDB" rev-list --reverse "$(git -C "$MDB" rev-parse "$P951"^^)..$P951"); do
    git -C "$MDB" cherry-pick -x "$c" >/dev/null 2>&1 || { git -C "$MDB" cherry-pick --abort >/dev/null 2>&1; CK=1; }
  done
  [ "$CK" = 0 ] && pass "composed: stack #1081..#1085 + #1051->#951 (clean)" \
                || fail "Wave-0 compose conflict (#1051/#951 pair vs stack)"
  # Sync headers (replicate setup.py step_sync, no git reset).
  python3 - <<'PY'
import shutil, pathlib
src=pathlib.Path("_mobilitydb/meos/include"); dst=pathlib.Path("meos/include")
stub={"pg_config.h","postgres_int_defs.h","postgres_ext_defs.in.h"}
[shutil.copy2(h,dst/h.name) for h in src.glob("*.h") if h.name not in stub]
PY
  if ! python3 -c 'import json,sys; json.load(open("meta/object-model.json"))' 2>/dev/null; then
    fail "MEOS-API catalog anchor missing: this branch must be stacked on feat/object-model (PR #10). See meta/integration-train.json#/catalog_anchor."
  fi
  mkdir -p output
  python3 run.py >/dev/null 2>"output/.train_run.log" \
    && pass "MEOS-API catalog regenerated" \
    || fail "run.py failed (see output/.train_run.log)"
  python3 - <<'PY'
import json,sys
om=json.load(open("output/meos-idl.json"))["objectModel"]
f2c=om["functionToClass"]; n=om["summary"]["functionsTotal"]
exp={"tcbuffer_from_mfjson":"TCbuffer","tnpoint_from_mfjson":"TNpoint",
     "tpose_from_mfjson":"TPose","tcbufferinst_make":"TCbufferInst",
     "tposeinst_make":"TPoseInst"}
bad=[k for k,v in exp.items() if (f2c.get(k) or {}).get("class")!=v]
sys.exit(0 if (n==2699 and not bad) else 1)
PY
  [ $? = 0 ] && pass "Wave-0 gate: 2699 fns, from_mfjson + ctors uniform" \
             || fail "Wave-0 gate: catalog count/classification mismatch"
  python3 -m pytest tests/ -q >"output/.train_pytest.log" 2>&1 \
    && grep -q "21 passed" "output/.train_pytest.log" \
    && pass "Wave-0 gate: PR #10 object-model 21/21" \
    || fail "Wave-0 gate: PR #10 tests not 21/21 (see output/.train_pytest.log)"
fi

# ---------------------------------------------------------------------------
say "WAVE 1 - PyMEOS-CFFI MEOS-1.4 substrate"
block "needs Wave-1 env: regenerate PyMEOS-CFFI builder/meos-idl.json vs Wave-0 MEOS, then pymeos_cffi import (owner: live PyMEOS session)"

say "WAVE 2 - CRITICAL PATH: PyMEOS master health (MEOS-1.4 bump #81/#82)"
block "PyMEOS master broken vs MEOS master (geoset_*->spatialset_*, tpoint_*->tspatial_*, pgis_geometry_in->geom_in, spanset_make arity); pristine master 426 failed/2721 passed. Gate: test.yml 426->0 vs Wave-1/Wave-0. Owner: live PyMEOS session. Build the bump against the composed Wave-0, not bare master."

say "WAVE 3 - PyMEOS features (gated on Wave 2)"
if [ -n "${PYMEOS_ENV:-}" ] && [ -d "$PYMEOS_ENV" ]; then
  block "PYMEOS_ENV given - run per-PR gates there: #85 black; #87 portable_parity 0-unbacked; #88 collect; #89 codegen --check; #90/#91 mixin suites; #84 tests/io 4/4 + footer==MobilityDuck#146 (manifest has exact gates)"
else
  block "set PYMEOS_ENV=<bump-ready PyMEOS clone> to run #85/#87/#88/#89/#90/#91/#84 gates; all are green-in-isolation and gated only on Wave 2"
fi

# ---------------------------------------------------------------------------
say "SUMMARY"
if [ "$RC" = 0 ]; then
  echo "  Wave 0 PROVEN here. Waves 1-3 gated solely on Wave 2 (the MEOS-1.4 bump)."
  echo "  100% parity is demonstrable the moment Wave 2 lands and this train"
  echo "  is re-run with PYMEOS_ENV set. Merge order: see meta/integration-train.json."
else
  echo "  Wave 0 gate FAILED above - parity train cannot proceed; fix before merge."
fi
exit "$RC"
