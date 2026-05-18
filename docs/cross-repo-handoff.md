# Cross-repo handoff — consuming meos-api.json toward ecosystem 100%

meos-api.json is the **single source of truth**. Its job in the
ecosystem-100% effort is done: it publishes the canonical contracts and
machine-readable worklists below. Everything else is *execution in other
repos*, owned by **that repo's own session** — one session per repo, never
cross-repo from a single session (collides with the parallel sessions
already on those repos). This brief tells each downstream session exactly
what to read and how to verify "done".

## The artifacts (regenerate, don't hand-edit)

```
python run.py              # -> output/meos-idl.json   (catalog; carries `portableAliases`)
python report.py           # -> output/meos-coverage.json   (structural worklist)
python portable_parity.py  # -> output/meos-portable-parity.json   (bare-name parity)
```

| Artifact | Source PR | Contents |
|---|---|---|
| `meta/portable-aliases.json` → `meos-idl.json#/portableAliases` | #8 | canonical operator→bare-name dialect: `byOperator`, `byBareName`, `families`, `explicitBacking`, `scope`, 29 pairs |
| `meos-idl.json#/functions[].{network,wire,api}` | #4 | per-function projectability + decode/encode/array/out-param wire model |
| `meos-coverage.json#/worklist` | #4 | every non-exposable public function with a `class` + concrete upstream `suggest`, ranked by `byClass` |
| `meos-portable-parity.json` | #8 | each bare name → backing family; **29/29 = 100%** verified |
| OpenAPI / MCP / runtime server | #5 / #6 / #7 | generated from the same catalog |

## Per-repo tasks and acceptance checks

**MobilityDuck — session scoped to MobilityDuck**
Input: `portableAliases.byOperator`. Task: register the **exact bare
names** (drop type-qualified forms like `spanOverlaps`). Done = every
operator in `byOperator` is callable by its bare name; parity verified with
the same prefix logic as `portable_parity.py`; **0 unbacked**.

**MobilitySpark — session scoped to MobilitySpark**
Same as Duck: register bare-name UDFs under the exact `byOperator` names.
Done = 0 unbacked vs `portableAliases`.

**PyMEOS / JMEOS / MEOS.NET — one session per binding**
Codegen from `meos-idl.json` (`functions` + `portableAliases`) so every
binding emits **identical** bare names. Done = generated symbol set ⊇
`portableAliases.bareNames`, no per-binding exceptions.

**MobilityDB — session scoped to MobilityDB**
Input: `meos-coverage.json#/worklist`. Task: signature uniformization,
highest leverage first — start with class `out-param-naming` (lone
out-parameter → `result`), then `array-return-shape` (add trailing
`int *count`), then `multi-out` (return a struct). Done = the class shrinks
to 0 on the next meos-api.json regeneration (the catalog auto-adopts; no
coupling). `stateful`/`plumbing` classes are correct exclusions, not gaps.

**3-platform BerlinMOD — its own session**
After Duck/Spark expose the names, re-run the portable suite
(`doc/rfc/sql-portability/berlinmod/`). Done = identical results across all
three platforms using the bare names only.

## Non-negotiable invariants (every downstream session)

- **Scope is in scope.** `cbuffer`, `npoint`, `pose`, `rgeo` are full
  user-facing temporal types — covered like every other type. PR #1075
  aliases all six families (1303). **Never** exclude them from a parity
  headline; an audit that "defers" them is incomplete work to close
  (stated as a gap with a plan), never an accepted end state.
- `trgeometry` is the user-facing name; internal funcs keep `trgeo_` — do
  not normalize the internal prefix.
- Aliases must **reuse each operator's own backing function** (equivalence
  by construction), never reimplement.
- 100% parity is the bar: every operator has its bare name on every engine,
  no gaps, no headline exclusions — partial is incomplete work with a plan,
  never a stopping point.
- Clean PRs: one squashed commit per topic, amend + force-push, **no AI
  attribution** anywhere; never block on review/merge — continue on issued
  PRs.

## Bootstrapping a downstream session

Memories are **per project directory** — a session opened in another repo
does **not** inherit this project's standing rules. So each session must be
started with a self-contained prompt that carries the rules explicitly.
Small contracts (the 29-pair `byOperator`) can be pasted inline; large
ones (the worklist) — have the session fetch this repo and regenerate.

Template (fill `<…>`):

> You work **only** on `<REPO>`, one task. Scope strictly to this repo;
> never touch other ecosystem repos (other sessions own them). Don't wait
> for PR review/merge — continue on issued PRs. Clean PRs: one squashed
> commit per topic, amend + force-push, **no AI attribution** anywhere
> (author as the user). 100% parity is the bar — partial is incomplete
> work stated with a plan, never a stopping point.
> **Hard invariant:** `cbuffer`/`npoint`/`pose`/`rgeo` are full
> user-facing temporal types, **in scope**, covered like every other type
> — never exclude them from any parity headline. `trgeometry` is
> user-facing; internal `trgeo_` is not normalized. Aliases reuse each
> operator's own backing function (equivalence by construction).
> Source of truth: `MobilityDB/MEOS-API` → `docs/cross-repo-handoff.md`
> and `<ARTIFACT>`. **Save these standing rules to this project's memory.**
> Task: `<SPECIFIC>`. Done when: `<ACCEPTANCE CHECK>`.

Filled example (MobilityDuck): *…task: register the exact bare names from
`portableAliases.byOperator` (drop type-qualified forms like
`spanOverlaps`). Done when: every operator in `byOperator` is callable by
its bare name, parity-checked with the `portable_parity.py` prefix logic,
0 unbacked, across all six type families.*

## Provenance

Discussion MobilityDB#861 · RFC #920 (`doc/rfc/sql-portability/README.md`,
branch `rfc/sql-portability`) · native in MobilityDB#1075 · manual
chapter MobilityDB#1078. Acceptance tooling in this repo:
`tests/test_portable_parity.py`, `tests/test_coverage_gate.py`.
