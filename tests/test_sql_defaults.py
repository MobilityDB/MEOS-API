"""Regression tests for capturing per-arg SQL DEFAULT values in parser/sqlfn.py.

An optional argument (`integer DEFAULT 0`, `text DEFAULT NULL`) is SQL-optional: a
binding must render the shorter overload with the omitted value substituted. The parser
records the literal default per arg as `argDefaults` (None for a required arg), attached
to a signature only when it has an optional arg, so default-free signatures stay
`{args, ret}` unchanged.

Plain unittest, no pytest dependency; synthetic SQL via a temp dir.
"""
import tempfile
import unittest
from pathlib import Path

from parser.sqlfn import _arg_default, _wrapper_sql_sigs


class ArgDefaultTests(unittest.TestCase):
    def test_arg_default_literal_kept_verbatim(self):
        self.assertEqual(_arg_default("integer DEFAULT 0"), "0")
        self.assertEqual(_arg_default("val integer DEFAULT 0"), "0")     # named arg
        self.assertEqual(_arg_default("text DEFAULT NULL"), "NULL")
        self.assertEqual(_arg_default("withbbox boolean DEFAULT FALSE"), "FALSE")
        self.assertEqual(_arg_default("maxdd integer = 15"), "15")       # `=` form
        self.assertEqual(_arg_default("VARIADIC opts text DEFAULT NULL"), "NULL")

    def test_required_arg_has_no_default(self):
        self.assertIsNone(_arg_default("floatset"))
        self.assertIsNone(_arg_default("s floatset"))
        self.assertIsNone(_arg_default("double precision"))

    def test_wrapper_sql_sigs_records_arg_defaults(self):
        # round(floatset, integer DEFAULT 0): the trailing precision is SQL-optional.
        sql = ("CREATE FUNCTION round(floatset, integer DEFAULT 0)\n"
               "  RETURNS floatset AS 'MODULE_PATHNAME', 'Set_round' LANGUAGE C;\n")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.sql").write_text(sql)
            sigs = _wrapper_sql_sigs(d)
        self.assertIn("Set_round", sigs)
        s = sigs["Set_round"][0]
        self.assertEqual(s["args"], ["floatset", "integer"])
        self.assertEqual(s["argDefaults"], [None, "0"])
        self.assertEqual(s["required"], 1)

    def test_all_required_args_have_all_none_defaults(self):
        sql = ("CREATE FUNCTION set_union(floatset, floatset)\n"
               "  RETURNS floatset AS 'MODULE_PATHNAME', 'Union_set_set' LANGUAGE C;\n")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.sql").write_text(sql)
            sigs = _wrapper_sql_sigs(d)
        s = sigs["Union_set_set"][0]
        self.assertEqual(s["argDefaults"], [None, None])
        self.assertEqual(s["required"], 2)


if __name__ == "__main__":
    unittest.main()
