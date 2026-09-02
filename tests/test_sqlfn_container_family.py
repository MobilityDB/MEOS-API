"""Regression tests for the container-family @csqlfn lint in parser/sqlfn.py.

A MEOS function's name ends in the container it takes, and its @csqlfn must name
a wrapper over that same container. Naming the sibling container's wrapper passes
every other check — the wrapper exists, it is reachable, its arity matches — so
the catalog silently carries the SQL surface of a different overload.

The refuting cases matter as much as the flagged one. A CONCRETE function name
over a GENERIC wrapper is how the whole value surface is written, so a lint that
reads suffixes literally reports the tree rather than a defect: forty names
disagree by suffix and seven by container family.

Plain unittest, no pytest dependency; synthetic catalog records.
"""
import unittest

from parser.sqlfn import _container_family, lint_container_family_csqlfn


def _idl(records):
    return {"functions": [dict(name=n, mdbC=w) for n, w in records]}


class ContainerFamilyTests(unittest.TestCase):

    def test_sibling_container_wrapper_is_flagged(self):
        """The defect: a Set function naming the span-set wrapper."""
        bad = lint_container_family_csqlfn(_idl([
            ("trgeometry_at_tstzset", "Temporal_at_tstzspanset"),
        ]))
        self.assertEqual(bad, [("trgeometry_at_tstzset", "Temporal_at_tstzspanset")])

    def test_generic_wrapper_of_a_concrete_name_is_not_flagged(self):
        """A timestamptz function naming the generic value wrapper is the norm.

        This is the case that makes a literal suffix comparison useless: one
        wrapper serves every base type, so its name says `value` where the
        function's says `timestamptz`.
        """
        self.assertEqual(lint_container_family_csqlfn(_idl([
            ("adjacent_span_timestamptz", "Adjacent_span_value"),
            ("union_set_timestamptz", "Union_set_value"),
        ])), [])

    def test_same_family_spelled_differently_is_not_flagged(self):
        """`tstzset` and `set` are one family, as are `tstzspanset` and `spanset`."""
        self.assertEqual(lint_container_family_csqlfn(_idl([
            ("distance_tstzset_tstzset", "Distance_set_set"),
            ("distance_tstzspanset_tstzspan", "Distance_spanset_span"),
        ])), [])

    def test_a_name_ending_in_no_container_is_not_flagged(self):
        """`union_set_pcpoint` ends in a type, not a container, so it says nothing.

        Its commuted twin `union_pcpoint_set` does end in one, which is why the
        pointcloud pairs flag one side and not the other.
        """
        self.assertEqual(lint_container_family_csqlfn(_idl([
            ("union_set_pcpoint", "Union_set_value"),
            ("temporal_start_instant", "Temporal_start_instant"),
        ])), [])

    def test_a_function_naming_no_wrapper_is_not_flagged(self):
        """An untagged function has nothing to disagree with."""
        self.assertEqual(lint_container_family_csqlfn(
            {"functions": [{"name": "trgeometry_at_tstzset"}]}), [])

    def test_the_families_a_name_can_end_in(self):
        self.assertEqual(_container_family("x_tstzspanset"), "spanset")
        self.assertEqual(_container_family("x_tstzspan"), "span")
        self.assertEqual(_container_family("x_tstzset"), "set")
        self.assertEqual(_container_family("x_timestamptz"), "value")
        self.assertEqual(_container_family("X_Set_Value"), "value")
        self.assertIsNone(_container_family("x_pcpoint"))
        self.assertIsNone(_container_family("tstzset"))


if __name__ == "__main__":
    unittest.main()
