"""The SQL declarations are read without their comments.

A CREATE FUNCTION commented out is no declaration: the extension does not
create it, so no binding may register it. A commented line inside a
declaration is no part of it: `asMVTGeom(tgeompoint, ...)` keeps an older
`-- RETURNS tgeompoint` above its `RETURNS geom_times`. The parser blanks every
`--` and `/* */` comment, keeping the newlines, and steps over string literals,
so a comment opener inside a literal stays part of the literal.

Plain unittest, no pytest dependency; synthetic SQL via a temp dir, as
#test_wrapper_sql_sigs_records_arg_defaults of tests/test_sql_defaults.py.
"""
import tempfile
import unittest
from pathlib import Path

from parser.sqlfn import _strip_sql_comments, _wrapper_sql_sigs

SQL = """
/* CREATE FUNCTION tdirection(tgeompoint)
  RETURNS tfloat
  AS 'MODULE_PATHNAME', 'Tpoint_tdirection'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE; */
-- CREATE FUNCTION asMVTGeom(tgeo tgeometry, bounds stbox)
-- RETURNS geom_times
-- AS 'MODULE_PATHNAME','Tpoint_as_mvtgeom'
-- LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION asMVTGeom(tpoint tgeompoint, bounds stbox)
-- RETURNS tgeompoint
RETURNS geom_times
AS 'MODULE_PATHNAME','Tpoint_as_mvtgeom'
LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION stops(tgeompoint, maxdist float DEFAULT 0.0,
    -- a comment between two arguments
    minduration interval DEFAULT '0 minutes')
  RETURNS tgeompoint
  AS 'MODULE_PATHNAME', 'Temporal_stops'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
"""


class StripSqlCommentsTests(unittest.TestCase):

    def test_a_line_comment_and_a_block_comment_are_blanked(self):
        self.assertEqual(_strip_sql_comments("a -- b\nc /* d\ne */ f"),
                         "a     \nc     \n     f")

    def test_block_comments_nest(self):
        self.assertEqual(_strip_sql_comments("/* a /* b */ c */x").strip(), "x")

    def test_a_comment_opener_in_a_literal_stays(self):
        text = "DEFAULT 'a -- b /* c' -- d"
        self.assertEqual(_strip_sql_comments(text), "DEFAULT 'a -- b /* c'     ")

    def test_a_doubled_quote_does_not_close_the_literal(self):
        text = "'it''s -- here' -- gone"
        self.assertEqual(_strip_sql_comments(text), "'it''s -- here'        ")


class CommentedDeclarationTests(unittest.TestCase):

    def _sigs(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.sql").write_text(SQL)
            return _wrapper_sql_sigs(d)

    def test_a_declaration_commented_out_registers_nothing(self):
        sigs = self._sigs()
        self.assertNotIn("Tpoint_tdirection", sigs)
        self.assertEqual([s["args"] for s in sigs["Tpoint_as_mvtgeom"]],
                         [["tgeompoint", "stbox"]])

    def test_a_commented_line_is_no_part_of_the_declaration(self):
        sigs = self._sigs()
        self.assertEqual(sigs["Tpoint_as_mvtgeom"][0]["ret"], "geom_times")
        s = sigs["Temporal_stops"][0]
        self.assertEqual(s["args"], ["tgeompoint", "float", "interval"])
        self.assertEqual(s["ret"], "tgeompoint")


if __name__ == "__main__":
    unittest.main()
