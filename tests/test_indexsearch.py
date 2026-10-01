"""The index search each topological and position signature states, with the indexed column
on either side of its operator.

The synthetic catalog carries the ``IndexSearchOp`` values with their doc comments and the
functions with their ``sqlop`` and SQL signatures, as #CodecTests of tests/test_codecs.py
builds its own; the SQL declarations are written to a temporary root, as #BoundArgsTests of
tests/test_boundargs.py writes its wrappers; the contract tests read the generated catalog.
Plain unittest, no pytest dependency.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.indexsearch import attach_index_search, index_searches, operator_decls

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"

SQL = """
CREATE OPERATOR && (
  PROCEDURE = overlaps, LEFTARG = stbox, RIGHTARG = stbox,
  COMMUTATOR = &&, RESTRICT = tspatial_sel, JOIN = tspatial_joinsel
);
CREATE OPERATOR @> (
  PROCEDURE = contains, LEFTARG = stbox, RIGHTARG = stbox,
  COMMUTATOR = <@
);
CREATE OPERATOR << (
  PROCEDURE = stboxLeft, LEFTARG = stbox, RIGHTARG = stbox,
  COMMUTATOR = '>>'
);
-- CREATE OPERATOR &< ( PROCEDURE = stboxOverleft, LEFTARG = tbox, RIGHTARG = tbox, COMMUTATOR = &> );
CREATE OPERATOR &< (
  PROCEDURE = stboxOverleft, LEFTARG = stbox, RIGHTARG = stbox
);
"""


def enum(**docs):
    return {"name": "IndexSearchOp", "values": [
        {"name": n, "value": i, **({"doc": d} if d else {})}
        for i, (n, d) in enumerate(docs.items())]}


SEARCHES = enum(
    INDEX_OVERLAPS="Find stored boxes that overlap the query, `&&` operator",
    INDEX_CONTAINS="Find stored boxes that contain the query, `@>` operator",
    INDEX_CONTAINED_BY="Find stored boxes contained by the query, `<@` operator",
    INDEX_LEFT="Find stored boxes strictly left of the query, `<<` operator",
    INDEX_RIGHT="Find stored boxes strictly right of the query, `>>` operator",
    INDEX_OVERLEFT="Find stored boxes that do not extend to the right of the query, `&<` operator")


def fn(name, sqlfn, *argsets, ret="boolean"):
    return {"name": name, "sqlfn": sqlfn,
            "sqlSignatures": [a if isinstance(a, dict) else {"args": list(a), "ret": ret}
                              for a in argsets]}


class IndexSearchTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "box.in.sql").write_text(SQL)
        idl = {"enums": [SEARCHES], "functions": [
            fn("overlaps_stbox_stbox", "overlaps", ("stbox", "stbox")),
            fn("contains_stbox_stbox", "contains", ("stbox", "stbox")),
            # the signature's own SQL name, not the function's, backs the operator
            fn("left_stbox_stbox", "stboxBefore",
               {"args": ["stbox", "stbox"], "ret": "boolean", "sqlName": "stboxLeft"}),
            fn("overleft_stbox_stbox", "stboxOverleft", ("stbox", "stbox")),
            fn("stbox_hash", "stbox_hash", ("stbox",)),
            fn("overlaps_tbox_tbox", "overlaps", ("tbox", "tbox")),
            fn("contains_tstzspan_stbox", "contains", ("stbox", "stbox"), ret="tbool")]}
        self.idl, self.n, self.errors = attach_index_search(idl, self.tmp.name)
        self.sig = {f["name"]: f["sqlSignatures"][0] for f in self.idl["functions"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_commutator_names_the_search_with_the_column_on_the_right(self):
        self.assertEqual(self.sig["contains_stbox_stbox"]["indexSearch"],
                         {"columnLeft": "INDEX_CONTAINS", "columnRight": "INDEX_CONTAINED_BY"})
        # a quoted commutator reads as a bare one
        self.assertEqual(self.sig["left_stbox_stbox"]["indexSearch"],
                         {"columnLeft": "INDEX_LEFT", "columnRight": "INDEX_RIGHT"})
        self.assertEqual(self.sig["overlaps_stbox_stbox"]["indexSearch"]["columnRight"],
                         "INDEX_OVERLAPS")

    def test_an_operator_without_commutator_has_no_search_with_the_column_on_the_right(self):
        # the commented declaration giving `&<` a commutator over tbox is no declaration
        self.assertEqual(self.sig["overleft_stbox_stbox"]["indexSearch"],
                         {"columnLeft": "INDEX_OVERLEFT", "columnRight": None})

    def test_no_operator_or_no_declaration_states_nothing(self):
        self.assertNotIn("indexSearch", self.sig["stbox_hash"])
        self.assertNotIn("indexSearch", self.sig["overlaps_tbox_tbox"])
        # a value-returning signature over the operator's types is no predicate
        self.assertNotIn("indexSearch", self.sig["contains_tstzspan_stbox"])
        self.assertEqual((self.n, self.errors), (4, []))

    def test_the_declarations_read_bare_and_quoted_commutators(self):
        decls = operator_decls(self.tmp.name)
        self.assertEqual(decls[("stboxleft", "stbox", "stbox")], [("<<", ">>")])
        self.assertEqual(decls[("stboxoverleft", "stbox", "stbox")], [("&<", None)])
        self.assertNotIn(("stboxoverleft", "tbox", "tbox"), decls)

    def test_a_value_naming_no_operator_or_one_another_names_is_an_error(self):
        _, errors = index_searches({"enums": [enum(
            INDEX_OVERLAPS="Find stored boxes that overlap the query, `&&` operator",
            INDEX_SAME="Find stored boxes whose extent equals the query",
            INDEX_ADJACENT="Find stored boxes sharing a boundary, `&&` operator")]})
        self.assertEqual(len(errors), 2)


class IndexSearchContractTests(unittest.TestCase):
    """Over the generated catalog."""

    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        idl = json.loads(IDL.read_text())
        self.fns = {f["name"]: f for f in idl["functions"]}
        self.searches, _ = index_searches(idl)

    def test_every_index_search_names_one_operator(self):
        self.assertEqual(len(self.searches), 21)

    def test_the_box_predicates_state_their_search(self):
        sig = lambda n: self.fns[n]["sqlSignatures"][0]["indexSearch"]
        self.assertEqual(sig("overleft_stbox_stbox"),
                         {"columnLeft": "INDEX_OVERLEFT", "columnRight": None})
        self.assertEqual(sig("contains_stbox_stbox"),
                         {"columnLeft": "INDEX_CONTAINS", "columnRight": "INDEX_CONTAINED_BY"})
        self.assertEqual(sig("left_stbox_stbox"),
                         {"columnLeft": "INDEX_LEFT", "columnRight": "INDEX_RIGHT"})

    def test_no_overlapping_ordering_states_a_search_with_the_column_on_the_right(self):
        over = {v for v in self.searches.values() if v.startswith("INDEX_OVER")
                and v != "INDEX_OVERLAPS"}
        stated = [(f["name"], s["args"]) for f in self.fns.values()
                  for s in f.get("sqlSignatures") or []
                  if (s.get("indexSearch") or {}).get("columnLeft") in over
                  and s["indexSearch"]["columnRight"] is not None]
        self.assertEqual(len(over), 8)
        self.assertEqual(stated, [])

    def test_a_value_returning_signature_states_no_search(self):
        self.assertEqual([(f["name"], s["args"]) for f in self.fns.values()
                          for s in f.get("sqlSignatures") or []
                          if s.get("indexSearch") and s.get("ret") != "boolean"], [])


if __name__ == "__main__":
    unittest.main()
