"""The name each SQL signature takes where Spark or Flink cannot take the PostgreSQL one.

The wrappers, their SQL declarations and the MEOS functions claiming them are written to a
temporary root, as #IndexSearchTests of tests/test_indexsearch.py writes its declarations; the
type facts are built in the fields #TypeFacts reads, as #cell_facts of tests/test_typescope.py
builds them; the contract tests read the generated catalog. Plain unittest, no pytest
dependency.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.altsqlfn import _tag_names, attach_alt_sql_names, wrapper_names
from parser.typescope import TypeFacts

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"

WRAPPERS = """
/**
 * @brief Return a temporal value rounded
 * @sqlfn round()
 * @altsqlfn floatRound(), geoRound(), poseRound()
 */
Datum
Temporal_round(PG_FUNCTION_ARGS)
{
}

/**
 * @brief Return a temporal geo rotated around the z axis
 * @sqlfn rotateZ(), rotate()
 * @altsqlfn geoRotateZ(), geoRotate()
 */
Datum
Tgeo_rotate_z(PG_FUNCTION_ARGS)
{
}

/**
 * @brief Return the absolute value of a temporal number
 * @sqlfn abs()
 * @altsqlfn intAbs(), floatAbs()
 */
Datum
Tnumber_abs(PG_FUNCTION_ARGS)
{
}

/**
 * @brief Return the hash of a set
 * @sqlfn hash()
 * @altsqlfn setHash()
 */
Datum
Set_hash(PG_FUNCTION_ARGS)
{
}

/**
 * @brief Return the start value
 * @sqlfn startValue()
 */
Datum
Temporal_start_value(PG_FUNCTION_ARGS)
{
}
"""

SQL = """
CREATE FUNCTION round(tfloat, integer) RETURNS tfloat AS 'MODULE_PATHNAME', 'Temporal_round' LANGUAGE C;
CREATE FUNCTION round(tgeompoint, integer) RETURNS tgeompoint AS 'MODULE_PATHNAME', 'Temporal_round' LANGUAGE C;
CREATE FUNCTION round(trgeometry, integer) RETURNS trgeometry AS 'MODULE_PATHNAME', 'Temporal_round' LANGUAGE C;
CREATE FUNCTION round(tpose[], integer) RETURNS tpose[] AS 'MODULE_PATHNAME', 'Temporal_round' LANGUAGE C;
CREATE FUNCTION round(tbool, integer) RETURNS tbool AS 'MODULE_PATHNAME', 'Temporal_round' LANGUAGE C;
CREATE FUNCTION rotateZ(tgeompoint, float) RETURNS tgeompoint AS 'MODULE_PATHNAME', 'Tgeo_rotate_z' LANGUAGE C;
CREATE FUNCTION rotate(tgeompoint, float) RETURNS tgeompoint AS 'MODULE_PATHNAME', 'Tgeo_rotate_z' LANGUAGE C;
CREATE FUNCTION abs(tint) RETURNS tint AS 'MODULE_PATHNAME', 'Tnumber_abs' LANGUAGE C;
CREATE FUNCTION abs(tfloat) RETURNS tfloat AS 'MODULE_PATHNAME', 'Tnumber_abs' LANGUAGE C;
CREATE FUNCTION hash(intset) RETURNS integer AS 'MODULE_PATHNAME', 'Set_hash' LANGUAGE C;
CREATE FUNCTION hash(floatset) RETURNS integer AS 'MODULE_PATHNAME', 'Set_hash' LANGUAGE C;
"""

MEOS = """
/**
 * @csqlfn #Temporal_round()
 */
Temporal *
temporal_round(const Temporal *temp, int maxdd)
{
}

/**
 * @csqlfn #Tgeo_rotate_z()
 */
Temporal *
tgeo_rotate_z(const Temporal *temp, double angle)
{
}

/**
 * @csqlfn #Tnumber_abs()
 */
Temporal *
tnumber_abs(const Temporal *temp)
{
}

/**
 * @csqlfn #Set_hash()
 */
uint32
set_hash(const Set *s)
{
}
"""


def facts():
    """The type facts the prefixes resolve against, in the fields #TypeFacts reads from
    meos_catalog.c: the base types, their sets and the class predicate of the geo types."""
    f = TypeFacts.__new__(TypeFacts)
    f.name = {}
    f.names = {'int4', 'float8', 'geometry', 'geography', 'pose', 'intset', 'floatset'}
    f.klass = {'geo_basetype': {'geometry', 'geography'}}
    f.validate = {}
    f.container = {}
    return f


def sig(args, ret, name=None):
    s = {"args": list(args), "ret": ret}
    if name:
        s["sqlName"] = name
    return s


def catalog():
    """The functions as #attach_sqlfn_map leaves them, with the startValue signatures stating the
    value of each temporal type at an instant and the type registry the base of each set."""
    return {
        "typeRelations": {"byBase": {
            "int4": {"set": "intset", "temporal": ["tint"]},
            "float8": {"set": "floatset", "temporal": ["tfloat"]},
            "geometry": {"temporal": ["tgeompoint"]},
            "pose": {"temporal": ["tpose", "trgeometry"]}}},
        "functions": [
            {"name": "temporal_round", "sqlfn": "round", "sqlSignatures": [
                sig(["tfloat", "integer"], "tfloat"), sig(["tgeompoint", "integer"], "tgeompoint"),
                sig(["trgeometry", "integer"], "trgeometry"),
                sig(["tpose[]", "integer"], "tpose[]"), sig(["tbool", "integer"], "tbool")]},
            {"name": "tgeo_rotate_z", "sqlfn": "rotateZ", "sqlSignatures": [
                sig(["tgeompoint", "float"], "tgeompoint"),
                sig(["tgeompoint", "float"], "tgeompoint", "rotate")]},
            {"name": "tnumber_abs", "sqlfn": "abs", "sqlSignatures": [
                sig(["tint"], "tint"), sig(["tfloat"], "tfloat")]},
            {"name": "set_hash", "sqlfn": "hash", "sqlSignatures": [
                sig(["intset"], "integer"), sig(["floatset"], "integer")]},
            {"name": "temporal_start_value", "sqlfn": "startValue", "sqlSignatures": [
                sig(["tint"], "integer"), sig(["tfloat"], "float"),
                sig(["tgeompoint"], "geometry(Point)"), sig(["tpose"], "pose"),
                sig(["trgeometry"], "geometry"), sig(["tbool"], "boolean")]}]}


class AltSqlNameTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for sub, name, text in (("mdb", "w.c", WRAPPERS), ("sql", "s.in.sql", SQL),
                                ("meos/src", "m.c", MEOS)):
            (root / sub).mkdir(parents=True)
            (root / sub / name).write_text(text)
        self.idl, self.n, self.errors = attach_alt_sql_names(
            catalog(), root / "meos" / "src", root / "mdb", root / "sql", facts())
        self.fns = {f["name"]: f["sqlSignatures"] for f in self.idl["functions"]}

    def tearDown(self):
        self.tmp.cleanup()

    def alt(self, fn, i):
        return self.fns[fn][i].get("altSqlName")

    def test_a_tag_lists_every_name_to_the_next_tag(self):
        block = " * @sqlfn rotateZ(), rotate()\n * @altsqlfn geoRotateZ(),\n *   geoRotate()\n"
        self.assertEqual(_tag_names(block, "sqlfn"), ["rotateZ", "rotate"])
        self.assertEqual(_tag_names(block, "altsqlfn"), ["geoRotateZ", "geoRotate"])
        self.assertEqual(wrapper_names(Path(self.tmp.name) / "mdb")["Tgeo_rotate_z"],
                         (["rotateZ", "rotate"], ["geoRotateZ", "geoRotate"]))
        self.assertNotIn("Temporal_start_value", wrapper_names(Path(self.tmp.name) / "mdb"))

    def test_several_sqlfn_names_pair_by_position(self):
        self.assertEqual(self.alt("tgeo_rotate_z", 0), "geoRotateZ")
        self.assertEqual(self.alt("tgeo_rotate_z", 1), "geoRotate")

    def test_one_alternative_name_reaches_every_signature(self):
        self.assertEqual(self.alt("set_hash", 0), "setHash")
        self.assertEqual(self.alt("set_hash", 1), "setHash")

    def test_the_base_type_of_the_first_argument_selects_among_several(self):
        self.assertEqual(self.alt("tnumber_abs", 0), "intAbs")
        self.assertEqual(self.alt("tnumber_abs", 1), "floatAbs")
        self.assertEqual(self.alt("temporal_round", 0), "floatRound")
        # the startValue return type, its modifier dropped, names the geometry
        self.assertEqual(self.alt("temporal_round", 1), "geoRound")
        # an array is read through its element type
        self.assertEqual(self.alt("temporal_round", 3), "poseRound")

    def test_a_trgeometry_takes_the_geometry_it_answers_at_an_instant(self):
        # a reference geometry and a temporal pose: its instants store a pose, its value is a geometry
        self.assertEqual(self.alt("temporal_round", 2), "geoRound")

    def test_a_signature_no_name_selects_stops_the_catalog(self):
        self.assertIsNone(self.alt("temporal_round", 4))
        self.assertEqual(len(self.errors), 1)
        self.assertIn("round(tbool, integer)", self.errors[0])
        self.assertEqual(self.n, 10)

    def test_a_wrapper_with_no_alternative_name_states_none(self):
        self.assertFalse(any("altSqlName" in s for s in self.fns["temporal_start_value"]))


class AltSqlNameContractTests(unittest.TestCase):
    """Over the generated catalog."""

    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        idl = json.loads(IDL.read_text())
        self.alts = {}
        for f in idl["functions"]:
            for s in f.get("sqlSignatures") or ():
                if "altSqlName" in s:
                    key = ((s.get("sqlName") or f["sqlfn"]), tuple(s["args"]))
                    self.alts.setdefault(key, set()).add(s["altSqlName"])

    def test_every_signature_takes_one_name(self):
        self.assertTrue(self.alts)
        self.assertEqual([k for k, v in self.alts.items() if len(v) != 1], [])

    def test_the_rounding_takes_the_base_type_of_its_value(self):
        self.assertEqual(self.alts[("round", ("tfloat", "integer"))], {"floatRound"})
        self.assertEqual(self.alts[("round", ("trgeometry", "integer"))], {"geoRound"})
        self.assertEqual(self.alts[("round", ("tcbuffer", "integer"))], {"cbufferRound"})


if __name__ == "__main__":
    unittest.main()
