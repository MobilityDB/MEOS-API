# Index searches

An index over boxes answers a topological or position predicate by searching
for the stored boxes the predicate can accept. MEOS names those searches in the
enum `IndexSearchOp` (`meos/include/meos.h`), one value per search, and the doc
comment of each value names the operator it serves:

```c
INDEX_OVERLAPS,  /**< Find stored boxes that overlap the query, `&&` operator */
INDEX_OVERLEFT,  /**< Find stored boxes that do not extend to the right of the query, `&<` operator */
```

The in-memory RTree and SPTree of MEOS take an `IndexSearchOp`; MobilityDuck
routes a predicate to its index through the same enum. The catalog states, for
every SQL signature an index can answer, which search answers it, so an engine
reads its routing from the catalog instead of keeping a table of its own.

## In the catalog

Each boolean two-argument SQL signature that backs an operator an
`IndexSearchOp` value serves, over the signature's own argument types, carries

```json
"indexSearch": {"columnLeft": "<IndexSearchOp>", "columnRight": "<IndexSearchOp>" | null}
```

- `columnLeft` is the search for `column <op> query`, the indexed column the
  first argument: the value whose comment names the operator.
- `columnRight` is the search for `query <op> column`: the value whose comment
  names the operator's `COMMUTATOR`, since `query <op> column` holds exactly
  when `column <commutator> query` does.

The operator comes from the `CREATE OPERATOR` declaration, which names the SQL
function backing it and its argument types (`&<` over `(stbox, stbox)` has
`PROCEDURE = stboxOverleft`), not from the function's `sqlop`: a MEOS function
backing several wrappers carries the `sqlop` of the first, while each signature
carries its own SQL name. For example

| Function | Signature | `columnLeft` | `columnRight` |
|---|---|---|---|
| `contains_stbox_stbox` | `(stbox, stbox)` | `INDEX_CONTAINS` | `INDEX_CONTAINED_BY` |
| `left_stbox_stbox` | `(stbox, stbox)` | `INDEX_LEFT` | `INDEX_RIGHT` |
| `overleft_stbox_stbox` | `(stbox, stbox)` | `INDEX_OVERLEFT` | `null` |

An overlapping ordering (`&<`, `&>`, `&<|`, `|&>`, `&</`, `/&>`, `&<#`, `#&>`)
declares no commutator: `query &< column` asks for a stored box whose end is not
before the query's end, which no search states. Its `columnRight` is null and an
engine scans, as PostgreSQL does.

A signature that returns something other than a boolean carries no
`indexSearch`, even over an operator symbol a search serves: `#>>` reading a
`jsonb` path and `@>` lifted to a temporal boolean over `tjsonb` are no
predicates.

## Errors

The map from operator to search is the one statement every engine reads, so the
parse fails when an `IndexSearchOp` value names no operator in its comment, two
values name the same operator, or one signature backs two operators a search
serves.

## How a generator uses it

A generator emits, per signature carrying `indexSearch`, the routing of
`column <op> query` to `columnLeft` and of `query <op> column` to `columnRight`,
and a scan where `columnRight` is null. It reads the enum value names from the
catalog's `IndexSearchOp`; it never maps operator symbols by hand.
