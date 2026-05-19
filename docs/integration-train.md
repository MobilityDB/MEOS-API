# Integration train — making ecosystem-wide 100% parity provable

## Why this exists

The MobilityDB ecosystem (MobilityDB · MEOS-API · PyMEOS-CFFI · PyMEOS ·
MobilityDuck) currently carries a fan of individually-correct **open**
PRs. Each is green *in isolation*, but:

- every PR's CI builds against a `master` that lacks the *other* PRs'
  content (PyMEOS CI builds MEOS from MobilityDB master → lacks the
  extended-type C surface; PyMEOS code is broken vs MEOS master — the
  rename skew);
- no session can self-merge;
- per-session independence means **nobody assembles and verifies the
  integrated whole**.

So "100% parity" is true per-branch yet **unprovable as a system**. This
train operationalizes MobilityDB discussion **#895 (wave-based merge
plan)**: a dependency-ordered manifest + a one-command verifier so parity
is demonstrated *at one point*, and the maintainer gets an ordered,
de-risked merge sequence instead of N cross-dependent PRs reading red.

## Artifacts

- [`meta/integration-train.json`](../meta/integration-train.json) — the
  PR dependency DAG, per-wave compose recipe, gates, owners, merge order.
- [`verify-train.sh`](../verify-train.sh) — composes the train and runs
  each wave's gate. Honesty contract: a wave is `PASS` only when its gate
  is just-run green here; otherwise `BLOCKED:<reason>` with the exact
  gate it needs. Nothing faked or silently skipped.

## The waves

| Wave | Content | Status | Owner |
|---|---|---|---|
| **0** | MobilityDB extended-type C surface (stack #1081→#1085, then #1051→#951) | **PROVEN** — 2699 fns, MEOS-API PR #10 21/21, `from_mfjson`/ctors uniform | maintainer-merge gated |
| **1** | PyMEOS-CFFI MEOS-1.4 substrate (regenerate vs Wave-0) | in-flight | live PyMEOS session |
| **2** | **CRITICAL PATH** — PyMEOS MEOS-1.4 bump (#81/#82): kills the rename skew | blocking all | live PyMEOS session |
| **3** | PyMEOS features: #85, #87, #88, #89→#90→#91, #84 (+ MobilityDuck #146/#147 for the #84 interop) | green-in-isolation, gated on Wave 2 | mixed |

**Wave 2 is the single universal unblock.** Every PyMEOS parity claim is
downstream of it; nothing else accelerates 100% parity more. Build the
bump against the composed Wave 0 (not bare master) so it is done once,
correctly, against the final C surface.

## Branch base

This branch is **stacked on `feat/object-model` (MEOS-API PR #10)** — the
Wave-0 gate asserts the object-model classification (`from_mfjson` →
TCbuffer/TNpoint/TPose, concrete `*inst_make` constructors), which is
PR #10's pipeline. PR #10 (object model) + PR #8 (portable-aliases SoT)
are the catalog anchor of the train; see
`meta/integration-train.json#/catalog_anchor`.

## Running it

```bash
python3 setup.py        # one-time: fetch the MobilityDB sources
./verify-train.sh       # Wave 0 fully verified here; Waves 1-3 report
                        # honest BLOCKED + the exact gate each needs
PYMEOS_ENV=<bump-ready PyMEOS clone> ./verify-train.sh   # post-Wave-2
```

## Current status

Wave 0 is **proven**. Waves 1–3 are entirely gated on Wave 2 (the
MEOS-1.4 bump). There is **no remaining correctness work** — every
deliverable is verified correct in isolation; the gap is purely
integration ordering plus the maintainer-only merge gate, which this
train reduces to: *merge in wave order; CI turns green at each wave.*
