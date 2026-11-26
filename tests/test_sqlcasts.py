"""The casts MobilityDB declares and the public MEOS function each calls
(parser/sqlcasts.py #attach_sql_casts).

The synthetic SQL is written in a temporary directory as #CompositionTests of
tests/test_compositions.py writes it; the contract tests read the generated catalog, as
tests/test_sqltypes.py does. Plain unittest, no pytest dependency.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.sqlcasts import attach_sql_casts

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"

CASTS = """
CREATE FUNCTION tint(tfloat)
  RETURNS tint
  AS 'MODULE_PATHNAME', 'Tfloat_to_tint'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE CAST (tfloat AS tint) WITH FUNCTION tint(tfloat);

CREATE FUNCTION set(integer)
  RETURNS intset
  AS 'MODULE_PATHNAME', 'Value_to_set'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION set(float8)
  RETURNS floatset
  AS 'MODULE_PATHNAME', 'Value_to_set'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE CAST (integer AS intset) WITH FUNCTION set(integer) AS IMPLICIT;
CREATE CAST (float8 AS floatset) WITH FUNCTION set(float8) AS IMPLICIT;

CREATE FUNCTION tint(tint, integer)
  RETURNS tint
  AS 'MODULE_PATHNAME', 'Temporal_enforce_typmod'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE CAST (tint AS tint) WITH FUNCTION tint(tint, integer) AS IMPLICIT;

CREATE CAST (quadbin AS bigint) WITHOUT FUNCTION AS ASSIGNMENT;

CREATE FUNCTION span(int4range)
  RETURNS intspan
  AS 'MODULE_PATHNAME', 'Range_to_span'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE CAST (int4range AS intspan) WITH FUNCTION span(int4range);
"""


def fn(name, wrapper, sigs, api="public"):
    return {"name": name, "api": api, "mdbC": wrapper,
            "sqlSignatures": [{"args": a} for a in sigs]}


def _idl():
    return {"functions": [
        fn("tfloat_to_tint", "Tfloat_to_tint", [["tfloat"]]),
        fn("int_to_set", "Value_to_set", [["integer"]]),
        fn("float_to_set", "Value_to_set", [["float"]]),
        fn("value_set", "Value_to_set", [["integer"], ["float"]], api="internal"),
    ]}


class SqlCastTests(unittest.TestCase):
    """Each cast of a synthetic SQL file, with its context and its function."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sql = Path(self.tmp.name)
        (sql / "casts.in.sql").write_text(CASTS)
        self.idl, self.n, self.nfn = attach_sql_casts(_idl(), sql)
        self.casts = {(c["source"], c["target"]): c for c in self.idl["sqlCasts"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts(self):
        self.assertEqual((self.n, self.nfn), (6, 3))

    def test_a_cast_through_its_wrapper(self):
        self.assertEqual(self.casts[("tfloat", "tint")], {
            "source": "tfloat", "target": "tint", "context": "explicit",
            "sqlFunction": {"name": "tint", "args": ["tfloat"]},
            "function": "tfloat_to_tint"})

    def test_a_generic_wrapper_told_apart_by_the_signature(self):
        self.assertEqual(self.casts[("integer", "intset")]["function"], "int_to_set")
        self.assertEqual(self.casts[("float", "floatset")]["function"], "float_to_set")
        self.assertEqual(self.casts[("float", "floatset")]["context"], "implicit")

    def test_a_typmod_cast_has_no_function(self):
        c = self.casts[("tint", "tint")]
        self.assertTrue(c["typmod"])
        self.assertEqual(c["sqlFunction"]["args"], ["tint", "integer"])
        self.assertNotIn("function", c)

    def test_a_binary_cast(self):
        self.assertEqual(self.casts[("quadbin", "bigint")], {
            "source": "quadbin", "target": "bigint", "context": "assignment",
            "binary": True})

    def test_a_type_meos_does_not_have(self):
        self.assertNotIn("function", self.casts[("int4range", "intspan")])

    def test_ordered_by_source_and_target(self):
        keys = [(c["source"], c["target"]) for c in self.idl["sqlCasts"]]
        self.assertEqual(keys, sorted(keys))


@unittest.skipUnless(IDL.exists(), "output/meos-idl.json not generated")
class SqlCastContractTests(unittest.TestCase):
    """The casts of the generated catalog."""

    @classmethod
    def setUpClass(cls):
        idl = json.loads(IDL.read_text())
        cls.casts = {(c["source"], c["target"]): c for c in idl.get("sqlCasts", [])}
        cls.names = {f["name"]: f for f in idl["functions"]}

    def test_every_function_is_public(self):
        for c in self.casts.values():
            if "function" in c:
                self.assertEqual(self.names[c["function"]]["api"], "public", c)

    def test_known_casts(self):
        self.assertEqual(self.casts[("tfloat", "tint")]["function"], "tfloat_to_tint")
        self.assertEqual(self.casts[("tstzset", "tstzspan")]["function"], "set_to_span")
        self.assertTrue(self.casts[("tgeompoint", "tgeompoint")]["typmod"])
        self.assertTrue(self.casts[("quadbin", "bigint")]["binary"])


if __name__ == "__main__":
    unittest.main()
