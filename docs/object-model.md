# The MEOS object model

`meta/object-model.json` is the **single codegen source of truth** for the
class hierarchy implicit in MEOS. The pipeline folds it into
`meos-idl.json` as `objectModel`. Every binding/engine (PyMEOS, JMEOS,
MEOS.NET, MobilityDuck, MobilitySpark, …) derives the **identical**
classes and methods from this one mapping, so the OO surface is no longer
re-curated by hand in each repo.

## Why

MEOS is C: it has no classes. The object model is encoded by *convention*
in three places:

1. **The template axis** — the `Temporal` / `TInstant` / `TSequence` /
   `TSequenceSet` struct family, discriminated by the `subtype` field.
2. **The type-family axis** — the `temptype` discriminator. Its *base
   type* (e.g. `T_TFLOAT` → `T_FLOAT8`) is the missing template
   parameter; this is the inheritance lattice.
3. **The method binding** — a function's name prefix says which class it
   is a method of: `temporal_*` is the late-bound **superclass** (every
   temporal type), `tnumber_*`/`tspatial_*`/`tpoint_*`/`tgeo_*` are the
   abstract families, `tbool_*`/`tint_*`/`tfloat_*`/… are the exact leaf
   types, `tinstant_*`/`tsequence_*`/`tsequenceset_*` are the template
   subtypes.

The most mature hand-built model (PyMEOS) is used as a parity **oracle**,
not the source of truth — it is a strict subset of today's MEOS.

## The lattice

Single-inheritance tree. The base type is the missing template parameter;
the geometry/geodetic distinction is a **trait** axis, not a parent (so
there is no diamond):

The spatial subtree follows the authoritative MobilityDB manual
(Ch. 7 Figure 7.1): `TGeo` is the broad parent of every PostGIS-derived
type; `TPoint` is an API-level intermediate under `TGeo` (see
[Manual reconciliation](#manual-reconciliation)).

```
Temporal                       temporal_type      (the late-bound superclass)
├─ TAlpha                      talpha_type        {tbool, ttext}
│  ├─ TBool                                       base BOOL
│  └─ TText                                       base TEXT
├─ TNumber                     tnumber_type       {tint, tfloat}
│  ├─ TInt                                        base INT4
│  └─ TFloat                                      base FLOAT8
└─ TSpatial                    tspatial_type
   ├─ TGeo                     tgeo_type_all      (PostGIS-derived; manual)
   │  ├─ TPoint                tpoint_type        {tgeompoint, tgeogpoint}
   │  │  ├─ TGeomPoint                             base GEOMETRY  ·geometryBased
   │  │  └─ TGeogPoint                             base GEOGRAPHY ·geodetic
   │  ├─ TGeometry                                 base GEOMETRY  ·geometryBased
   │  └─ TGeography                                base GEOGRAPHY ·geodetic
   ├─ TCbuffer                                     base CBUFFER  (#if CBUFFER)
   ├─ TNpoint                                      base NPOINT   (#if NPOINT)
   ├─ TPose                                        base POSE     (#if POSE)
   └─ TRGeometry                                   base POSE     (#if RGEO)
```

A **concrete class** is the product *leaf × subtype* — `TFloatSeq`,
`TGeomPointInst`, `TRGeometrySeqSet`. Methods of a node are inherited by
all descendants; `objectModel.lattice` carries the derived
`children`/`ancestors`/`depth` so consumers can expand the effective
method set per concrete class.

`cbuffer`, `npoint`, `pose`, `rgeo` are **full leaf classes and in
scope** — never deferred. `trgeometry` is the user-facing name; internal
functions keep the `trgeo_` prefix and are **not** normalized.

## Manual reconciliation

The MobilityDB manual (Ch. 7, *Temporal Geometry Types*, Figure 7.1
"Hierarchy of spatiotemporal types", source `doc/images/tspatial.svg`) is
the **authoritative conceptual model** for the spatial subtree. The model
reconciles to it exactly, with one documented difference:

- The figure is **partial** — spatial-only; it omits the `Temporal` root
  and the whole `TAlpha`/`TNumber` subtree (`OM-M6`). This model is the
  complete superset.
- The figure makes **`TGeo` the broad parent** of `TGeometry`,
  `TGeography`, `TGeomPoint`, `TGeogPoint` ("TGeo and its subtypes …
  derived from the PostGIS types geometry and geography"). The model uses
  the broad C predicate `tgeo_type_all` for `TGeo` class membership;
  the narrow `tgeo_type()` (and the point-rejecting `tgeo_*` functions)
  is the real irregularity, sharpened in `OM-M1`.
- The figure draws no `TPoint` node, but the C API has `tpoint_type()`
  and a 25-function `tpoint_*` family that must bind to a class. The
  model inserts **`TPoint` as an API-level abstract under `TGeo`** — the
  single, documented addition (`OM-M6`).
- `tpcpoint`/`tpcpatch` (temporal point-cloud point/patch) are absent
  from both master MEOS and Figure 7.1 (`OM-M7`); they are out of the
  drift-gated source of truth and derived automatically once MEOS
  defines them — never fabricated.
- Class names use the manual spelling (`TGeo`, `TNpoint`, `TCbuffer`,
  `TPose`, `TRGeometry`); C prefixes (`tnpoint_`, `tcbuffer_`,
  `trgeo_`) are unchanged.

`tests/test_object_model.py::ModelFileTests::test_matches_manual_figure_7_1`
gates this: the model's spatial node set must equal the figure's nodes
plus `TPoint`, with the figure's parent edges intact — so the
reconciliation cannot silently regress.

## Closed algebra: companion hierarchies

MEOS is a closed algebra: temporal operations return and consume spans,
sets and boxes (`tnumber_to_span` → a `Span`, `temporal_time` → a
`TsTzSpanSet`, `tnumber_to_tbox` → `TBox`). The methods cannot be typed
without these, so `objectModel.companions` carries three parallel
hierarchies — `Box` (`TBox`, `STBox`, `TPCBox`), `Collection`
(`Set`/`Span`/`SpanSet` with the concrete int/bigint/float/text/date/
tstz/geo/… leaves) and `Value` — and `objectModel.algebra` records which
companion a temporal family yields.

`Value` holds the values a method names: `Text`, `Jsonb`, `JsonPath`, `Geo` over
`Geometry` and `Geography` (both a `GSERIALIZED`, parented exactly as
`TGeo` parents `TGeometry` and `TGeography`), `Cbuffer`, `Npoint`,
`Nsegment`, `Pose`, `PoseChain`, `Pcpoint`, `Pcpatch` and `Raquet`. A
base value crosses the MEOS boundary as a pointer unless
`basetype_byvalue()` names its type — which is why the DGGS cells, being
a `uint64`, need no class and the ten by-reference base types do.
`DriftGate::test_every_byreference_base_type_has_a_value_class` derives
that set from the two predicates and fails until each has a class, so a
base type MEOS adds reaches the model with no edit to the file.
`Nsegment`, `Raquet` and `JsonPath` are the MeosTypes no membership
predicate admits; the model states them, and the same gate states that it
does. `Pcschema`, `Raster`, `RTree`, `RTreeNNCursor`, `SPTree`, `SPNNCursor` and
`MeosArray` go further: MEOS
registers them in no enum at all, and they say so with a null `temptype`
rather than by leaving the field out. To a binding a pointer to a struct
is one thing — an instance it holds and calls through — whether MEOS
publishes the struct's layout or forward-declares it, which is why they
sit here rather than in a hierarchy of their own; GoMEOS's own wrapper
table draws no such line either.

`Box` is covered the same way, from the `type_bboxtype` column of
`MEOS_RELTYPE_CATALOG`: it names the box each type stores, so its
distinct values are the box types a method answers, and
`DriftGate::test_every_bbox_type_has_a_companion_class` holds the
companions to them. `T_TSTZSPAN` among them is a `Collection` class, so
the gate reads every hierarchy rather than `Box` alone.

## Method assignment

`objectModel.functionToClass` maps every catalog function to the class it
is a method of, by **longest-prefix match** (so `tgeompoint_*` beats
`tgeo_*`, `tsequenceset_*` beats `tsequence_*`, and `tfloatinst_*`
resolves to the concrete `TFloatInst`). The assignment **reuses the
function itself** as the backing symbol — equivalence by construction, no
C-symbol guessing. A function with no prefix match (operator overloads,
`datum_*` base helpers, plumbing) is recorded honestly with
`class: null` and a reason — never force-fitted.

`objectModel.classes.<Class>.cType` names what the class's instances are a
pointer to, since a binding declares every wrapper in terms of it and
working it out per binding is what makes a class the binding cannot type
until it is edited. It is read from the signatures MEOS publishes: a
receiver-role method — accessor, predicate, conversion, restriction,
output — takes the value it is called on first, so the pointee of that
parameter names the type. A class holding constructors alone takes the
answer of the subtype it is a product of (`TFloatInst` → `TInstant`), and
any other its parent's, which is the same C type by construction. All 105
classes resolve one, over the 21 C types MEOS's own signatures name.

### Members answering their receiver's type

A C function on a template answers the template: `intspan_expand` returns a
`Span *` whichever span it expands. Its SQL signatures say more, since
`expand(intspan, integer)` answers an intspan and `expand(datespan, integer)` a
datespan. A method or operator called on a value carries `answersReceiver: true`
when every SQL signature answers the SQL type of its receiver, so a binding can
type the answer as the class it is called on (TypeScript's `this`) rather than
the template its C return type names. 666 entries carry it on the catalog of
MobilityDB 3ef02e46aa.

Where the answer is another template, each receiver gets its own instantiation:
`spanset_span` returns a `Span *`, and `span(intspanset)` answers an intspan,
`span(floatspanset)` a floatspan. Such an entry carries `returnInstantiations`,
one `{receiverClass, returnClass}` pair per distinct pair its SQL signatures
name, so a binding can narrow the answer in each receiver class
(`IntSpanSet.span` answers an IntSpan). An entry carrying `answersReceiver`
carries none, since that flag already states it. 650 entries carry it on the
catalog of MobilityDB 3ef02e46aa.

### Constructors from base values

MEOS names a constructor from the values it is built of `<base>_to_<type>`
(`float_to_tbox`, `float_timestamptz_to_tbox`, `timestamptz_to_stbox`), so no
class prefix starts it. A pass after the operators assigns it to the class
`<type>` is the prefix of, when the function answers a value of that class,
under `<Type>.from<Base>` (`TBox.fromFloat`, `STBox.fromTimestamptz`), the
shape of the readers MEOS names `<type>_from_<format>` (`tbox_from_wkb` is
`TBox.fromWKB`). A template is instantiated from the SQL return type, so
`int_to_set` is `IntSet.fromInt`. A name the class's methods already hold
leaves the function unclassified, and so does one whose last token is no class
(`h3_cell_to_children`). `functionToClass` records the assignment with
`via: "fromBase"`.

### Operators

MEOS names an operator `<op>_<left>_<right>` (`ever_eq_tfloat_float`,
`left_stbox_stbox`, `union_span_span`) or `<op>_<operand>` (`tnot_tbool`),
so no class prefix starts it and the prefix rule leaves it out. Once each
class states its `cType`, a second pass assigns it by the **receiver-typed
rule**: the receiver is the type of the first parameter, read with the same
prefixes, and the member is the leading verb, the name cut at its first type
token, camelCased as every member is (`ever_eq_tfloat_float` → `TFloat.everEq`,
`nad_tfloat_tbox` → `TFloat.nad`, `left_stbox_stbox` → `STBox.left`). The first
parameter must really be a value of that class, so a conversion such as
`timestamptz_to_stbox`, whose first parameter is a timestamp, stays
unclassified.

Where the first operand is no class value (`sub_float_tfloat`, a float minus
a temporal float) the call is the operation reflected onto the right operand,
and its member is `r<op>` (`TFloat.rsub`). This is the spelling the MEOS.js
and PyMEOS hand layers give it (`radd`, `rsub`), so it never shares a name
with `sub_tfloat_float`, which computes the other difference.

The operators sit in `classes.<Class>.operators`, beside `methods`, because
one name there covers every overload the class has (`TFloat.nad` takes a
float, a `TFloat` or a `TBox`), and a binding whose language has no
overloading by argument type cannot emit a `methods` list that holds the
same name twice. Each entry is
`{function, role: "operator", scope: "operator", backing, ooName, receiver,
other}`: `receiver` is `left` or `right`, the operand the method is called
on, and `other` is the type token of the other operand. A binding groups the
entries by `ooName` and chooses the C function by the argument's type.
`functionToClass` records the assignment with `via: "operator"`.

Set, Span, SpanSet and Temporal are templates, and a C operator on one takes
the generic struct: `contains_set_int` and `contains_set_float` both take a
`Set *` and a number, so on Set no argument tells them apart. The template
is instantiated to the type the operator's SQL signatures name for its
receiver, the parameter the rule above chose, read in each signature's own
order where `sqlArgParams` gives one. Every MEOS type has the same spelling
in PostgreSQL, so that SQL type names the class once its type modifier is
dropped (`intset` is IntSet, `geometry(Point)` is Geometry). An operator moves
to the nearest class common to the types its signatures name, when that class
is below the one the rule chose, recording the template in `refinedFrom`
(`contains_set_int` is `IntSet.contains`, `contains_set_geo` over `geomset` and
`geogset` is `GeoSet.contains`). An operator over every instantiation stays on
the template (`contains_span_span` over every span type), so it covers today's
and tomorrow's. An operator SQL defines
for one instantiation only is a member of that one: `tcontains_tgeo_geo` is
declared for `tgeometry` alone, so it is `TGeometry.tcontains` and not a
method of `TGeomPoint`.

The other operand is instantiated the same way: `otherClass` is the nearest
class common to the types its signatures name there. `acontains_geo_tgeo`,
`acontains_geo_tcbuffer` and `acontains_geo_trgeometry` all take a
`Temporal *`, and their `otherClass` is TGeo, TCbuffer and TRGeometry. The
entry carries no `otherClass` where a signature names a type no class stands
for (a base value). An operator MEOS declares and SQL does not has no
signature to read, and its `otherClass` is the class its `other` token is the
prefix of, read as the receiver is read: the token must be a class prefix
exactly and the operand a value of that class. `teq_th3index_th3index`, in
`meos_h3.h` alone, takes a TH3Index.

Where the signatures name several pairs of classes, the nearest common
classes lose which goes with which: `contains_tnumber_numspan` is declared for
`(tint, intspan)`, `(tbigint, bigintspan)` and `(tfloat, floatspan)`, and its
entry is `TNumber.contains` with `otherClass` Span. The entry then carries
`instantiations`, one `{receiverClass, otherClass}` per pair the signatures
name, in sorted order. A binding with overloading emits a member per pair on
its receiver class, each calling the same C function
(`TFloat.contains(FloatSpan)`), and a binding without it tells the overloads
apart by both arguments. An entry whose signatures name one pair carries none:
`refinedFrom` and `otherClass` already state it.

Base-type
arithmetic and comparisons (`add_int32_int32`, `eq_date_timestamp`) have no
class to belong to and stay unclassified.

## Dispatch metadata

For 4 of the 6 temporal-type families the per-member argument→backing
routing is mechanically derivable from the `<member>_<type>_<arg>` C-name
token model, so faithful codegen needs nothing more than
`functionToClass`. The **`geo`** (`TGeomPoint`/`TGeogPoint`) and
**`temporal`** (`TFloat`/`TInt`/`TBool`/`TText`) families encode *editorial*
dispatch decisions that are absent from the C signatures (e.g. a Python
`Point` vs `BaseGeometry` split routing to *different* backings; scalar
arguments passed **by value** with a per-member cast; `IntSet`→`FloatSet`
coercion via the superclass). `objectModel.dispatch` makes that routing a
**catalog fact**, transcribed verbatim from the PyMEOS cross-repo handoff
RFC #94 §3 (the source of truth — extracted from PyMEOS's working
hand-written oracle; never re-derived), so every binding's faithful
generator emits geo/temporal with equivalence by construction instead of
per-binding editorial guesses.

`dispatch.geo` is **single-block** (`dispatch.geo.<member>`; `TGeomPoint`
vs `TGeogPoint` is disambiguated at runtime by `geodeticFromSelf`).
`dispatch.temporal` is **per concrete type** —
`dispatch.temporal.{tfloat,tint,tbool,ttext}.<member>` — fully resolved
(no `<t>`/`<base>` placeholders), because the editorial routing differs
per type (e.g. `tint` coerces Float→Int, the opposite of `tfloat`;
`tint.temporal_equal` takes the value uncast while `tfloat` casts;
`tbool` exposes only `temporal_equal/not_equal`/`at`/`minus`).

Each member has an ordered `dispatch` table (`py` type token → `fn`
backing; optional `argTransform`/`extraArgs`/`coerce`+`via`/
`geodeticFromSelf`; a `py:"scalar"` entry carries `scalarType`, the exact
`isinstance` test, e.g. `"float"`, `"int|float"`, `"bool"`, `"str"`),
plus `fallback` and `result`. The `py` token may be `"scalar"`,
`"self"`, a class name, or `"list[str]"`
(`isinstance(o, list) and isinstance(o[0], str)`). The tables are
transcribed verbatim from the hand-written oracle (RFC #94 §3 + the
complete extended §7) — never derived.

### argTransform vocabulary

`argTransform` is a **closed, named** vocabulary — each binding maps every
name to its own idiom; the set is finite because the editorial decisions
are finite:

| Name | Meaning (PyMEOS idiom shown) |
|---|---|
| `geoToGserialized` | shapely geometry → GSERIALIZED (`geo_to_gserialized($o, <geodetic>)`) |
| `stboxToGeo` | STBox → geometry (`stbox_to_geo($o._inner)`) |
| `scalarCast` | scalar cast to the block's concrete base (`float($o)` for `tfloat`, `int($o)` for `tint`) |
| `scalarValue` | scalar passed by value as-is (`$o`) |
| `textsetMake` | `list[str]` → text set (`textset_make($o)`) |
| `innerPtr` | pass the wrapped C pointer (`$o._inner`) |
| `geodeticFromSelf` | the only runtime-self primitive (PyMEOS → `isinstance(self, TGeogPoint)`) |
| `coerce`+`via:super` | Python-side type coercion then delegate to the superclass method |

## The error contract

MEOS has a single raise mechanism:
`meos_error(int errlevel, int errcode, const char *fmt, ...)`, where
`errcode` is an `errorCode` enum value. `objectModel.errors.codes` carries
the full taxonomy (verbatim, drift-gated against `meos.h`).
`objectModel.errors.raises` is derived by a static scan of the MobilityDB
C sources: the literal `meos_error` codes in each function body, plus one
indirection level through the `ensure_*` argument guards (tagged
`via: "direct" | "ensure"`). If the sources are unavailable the scan is a
no-op and `errors.status = "source-unavailable"` — an honest signal,
never a fabricated empty set.

## Parity audit

`object_model_parity.py` is the object-model analogue of
`portable_parity.py`. It parses the PyMEOS factory (the oracle, never
hard-coded) and writes `output/meos-object-model-parity.json`: every
structural divergence (classes/abstracts/collections MEOS defines that
PyMEOS lacks) as a worklist entry. A divergence already explained by a
curated `corrections` item is `known`; an unexplained one is
`needs-correction`. `tests/test_object_model_parity.py` gates
**0 `needs-correction`** (every divergence has a stated correction) and
that nothing is silently dropped — the analogue of the portable
0-unbacked gate. If the oracle is absent the audit degrades to
`oracle-unavailable` (curated corrections still carried, no fabricated
verdict).

## Irregularities (corrections worklist)

Making the implicit model explicit surfaces irregularities in *both*
MEOS and PyMEOS (a decade of manual evolution). They are carried verbatim
in `objectModel.corrections` as a durable, reviewable worklist
(`OM-M*` = MEOS-side, `OM-P*` = PyMEOS-side), e.g.:

- **OM-M1** the class `TGeo` is broad (manual = `tgeo_type_all`) but the
  narrow C `tgeo_type()` and most `tgeo_*` functions reject points —
  API applicability is narrower than class membership.
- **OM-M2** `tgeometry_type()` means *geometry-based (non-geodetic)*, not
  *is the TGeometry type* — a misnomer paired with `tgeodetic_type()`.
- **OM-M3** `TRGeometry`'s base type is `T_POSE` (base ≠ name).
- **OM-M4** `talpha_type` is a real grouping with no user-facing class.
- **OM-M6** the manual Figure 7.1 is partial (spatial-only) and draws no
  `TPoint`; the model is the superset and adds `TPoint` under `TGeo`.
- **OM-M7** `tpcpoint`/`tpcpatch` are planned but absent from master
  MEOS and the figure — out of the drift-gated SoT until MEOS adds them.
- **OM-P1/P6/P7** PyMEOS lacks the `TGeometry/TGeography/TCbuffer/
  TNpoint/TPose/TRGeometry` leaves, the full Collection hierarchy, and
  the `TSpatial`/`TGeo` abstract intermediates that MEOS defines.

Reporting only — the fixes land as separate PRs in those repos by their
own sessions.

## Drift gate

The curated lattice cannot silently drift from MEOS:
`tests/test_object_model.py::DriftGate` re-derives every membership set
from the MobilityDB sources (the predicate bodies, `MEOS_TEMPTYPE_CATALOG`,
the `tempSubtype` and `errorCode` enums) and asserts the curated meta
matches. (Public model excludes the internal `T_TDOUBLE{2,3,4}`
aggregation types.) Run `python setup.py` to fetch the sources, then
`python3 tests/test_object_model.py`.

## Provenance

Discussion MobilityDB#861 (edge-to-cloud portability). Source of truth:
MobilityDB `meos/src/temporal/meos_catalog.c` (predicates +
`MEOS_TEMPTYPE_CATALOG`) and `meos/include/meos.h` (`tempSubtype`,
`errorCode`). Oracle: PyMEOS `pymeos/factory.py`.
