# Integration train — making ecosystem-wide 100% parity provable

## Why this exists

The MobilityDB ecosystem (MobilityDB · MEOS-API · PyMEOS-CFFI · PyMEOS ·
MobilityDuck · MobilitySpark · MobilityAPI · JMEOS · GoMEOS · MEOS.NET ·
MEOS.js · MobilityDB-BerlinMOD · the stream-side platforms MobilityFlink
· MobilityKafka · MobilityNebula) carries a fan of individually-correct
**open** PRs. Each is green *in isolation*, but:

- every PR's CI builds against a `master` that lacks the *other* PRs'
  content (PyMEOS CI builds MEOS from MobilityDB master → lacks the
  extended-type C surface; PyMEOS code is broken vs MEOS master — the
  rename skew);
- the maintainer is the only merge gate — no automated merge;
- per-PR independence means **nobody assembles and verifies the
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

| Wave | Content | Status |
|---|---|---|
| **0** | MobilityDB extended-type C surface (stack #1081→#1085, then #1051→#951) | **PROVEN** — 2699 fns, MEOS-API PR #10 21/21, `from_mfjson`/ctors uniform |
| **1** | PyMEOS-CFFI MEOS-1.4 substrate (regenerate vs Wave-0) | IN_FLIGHT (PyMEOS-CFFI #18, #19) |
| **2** | **CRITICAL PATH** — PyMEOS MEOS-1.4 bump (#81/#82): kills the rename skew | BLOCKING_ALL |
| **3** | PyMEOS features: #85, #87, #88, #89→#90→#91, #84 (+ MobilityDuck #146/#147 for #84 interop) | GREEN_IN_ISOLATION, gated on Wave 2 |
| **4** | Downstream bindings (MobilityDuck 47 / MobilitySpark 10 / MobilityAPI 6 / JMEOS 6 / GoMEOS 4 / MEOS.NET 3 open PRs) | GREEN_IN_ISOLATION; JMEOS is the lone repo under structural-migration pressure (5/6 CONFL) |
| **5** | Service-agent + data-lake + stream layers (built on Wave-4 anchor) | ANCHOR_DEFINED (MEOS-API #4-7, #12-13; PyMEOS #84 + MobilityDuck #146/#147/#158; stream-layer planned-band) |

**Wave 2 is the single universal unblock.** Every PyMEOS parity claim is
downstream of it; nothing else accelerates 100% parity more. Build the
bump against the composed Wave 0 (not bare master) so it is done once,
correctly, against the final C surface.

**Waves 4 and 5** consume Wave 0's MEOS-1.4 C surface via the MEOS-API
`meos-idl.json` catalog. Each Wave-4 binding is bump-independent within
its own repo; the cross-binding gate is that the regenerated
`meos-idl.json` is byte-identical across them (proves single SoT).

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
MEOS-1.4 bump). Wave 4 (downstream bindings) is green-in-isolation
across MobilityDuck / MobilitySpark / MobilityAPI / GoMEOS / MEOS.NET;
JMEOS (5/6 CONFL) is under structural-migration pressure post the
multi-module restructure. Wave 5 (service-agent + data-lake + stream
layers) is anchor-defined and downstream of Wave 4. There is **no
remaining correctness work** — every deliverable is verified correct
in isolation; the gap is purely integration ordering plus the
maintainer-only merge gate, which this train reduces to: *merge in
wave order; CI turns green at each wave.*
