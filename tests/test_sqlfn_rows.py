"""The columns of the row a SQL function returns, each fed by a named C value.

PostgreSQL declares the columns of a record-returning function as its OUT
arguments, and those of a function returning a composite type as the members
of that type. An OUT argument is no input: it leaves `args`, `argDefaults` and
`required` and becomes a column. Each column then names the C value feeding it,
matched by type, so the SQL column order and the C parameter order need not
agree: `from` is `return` (the value or array the function returns) or an
out-parameter, with `element` or `field` inside it. A column the wrapper
computes rather than reads is stated in `meta/sql-columns.json`: `ordinal`
numbers the rows from 1, `offset` is a constant the wrapper adds. A row with a
column nothing feeds, or fed from two C values alike, stops the catalog.

The catalog signature is built as #_attach of tests/test_sqlfn_setof.py builds
it, and the declarations are validated as #SchemaTests of tests/test_covering.py
validates its descriptor. Plain unittest, no pytest dependency; synthetic
sources via a temp dir.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from parser.sqlfn import (_wrapper_sql_sigs, attach_row_sources, attach_sqlfn_map,
                          declared_columns)

ROOT = Path(__file__).resolve().parents[1]
COLUMNS = ROOT / "meta" / "sql-columns.json"
SCHEMA = ROOT / "meta" / "sql-columns.schema.json"

MEOS_C = """
/**
 * @ingroup meos_geo_rel_ever
 * @brief Return the pairs of indices of temporal geometries that are ever disjoint
 * @csqlfn #Edisjoint_tgeoarr_tgeoarr()
 */
int *
edisjoint_tgeoarr_tgeoarr(const Temporal **arr1, int count1,
  const Temporal **arr2, int count2, int *count)
{
}
"""

MDB_C = """
/**
 * @brief Return the pairs of indices of temporal geometries that are ever disjoint
 * @sqlfn eDisjointPairs()
 */
Datum
Edisjoint_tgeoarr_tgeoarr(PG_FUNCTION_ARGS)
{
}
"""

MDB_SQL = """
CREATE TYPE index_tbox AS (
  index integer,
  tile tbox
);
CREATE FUNCTION eDisjointPairs(tgeometry[], tgeometry[], OUT i integer, OUT j integer)
  RETURNS SETOF record
  AS 'MODULE_PATHNAME', 'Edisjoint_tgeoarr_tgeoarr'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION tDisjointPairs(tgeometry[], tgeometry[], OUT i integer, OUT j integer,
    OUT periods tstzspanset)
  RETURNS SETOF record
  AS 'MODULE_PATHNAME', 'Tdisjoint_tgeoarr_tgeoarr'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION timeTiles(tbox, interval, timestamptz DEFAULT '2000-01-03')
  RETURNS SETOF index_tbox
  AS 'MODULE_PATHNAME', 'Tbox_time_tiles'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION unnest(intset)
  RETURNS SETOF integer
  AS 'MODULE_PATHNAME', 'Set_unnest'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
"""


def _sigs():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "x.sql").write_text(MDB_SQL)
        return _wrapper_sql_sigs(d)


class ColumnDeclarationTests(unittest.TestCase):

    def test_out_arguments_are_columns_not_args(self):
        s = _sigs()["Edisjoint_tgeoarr_tgeoarr"][0]
        self.assertEqual(s["args"], ["tgeometry[]", "tgeometry[]"])
        self.assertEqual(s["required"], 2)
        self.assertEqual(s["argDefaults"], [None, None])
        self.assertEqual(s["columns"], [("i", "integer"), ("j", "integer")])

    def test_every_out_argument_is_a_column(self):
        s = _sigs()["Tdisjoint_tgeoarr_tgeoarr"][0]
        self.assertEqual(s["args"], ["tgeometry[]", "tgeometry[]"])
        self.assertEqual(s["columns"], [("i", "integer"), ("j", "integer"),
                                        ("periods", "tstzspanset")])

    def test_a_composite_return_type_gives_its_members(self):
        s = _sigs()["Tbox_time_tiles"][0]
        self.assertEqual(s["ret"], "index_tbox")
        self.assertEqual(s["columns"], [("index", "integer"), ("tile", "tbox")])
        self.assertEqual(s["argDefaults"], [None, None, "'2000-01-03'"])

    def test_one_value_has_no_columns(self):
        self.assertIsNone(_sigs()["Set_unnest"][0]["columns"])

    def test_the_catalog_signature_lists_its_columns(self):
        idl = {"functions": [{"name": "edisjoint_tgeoarr_tgeoarr", "api": "public"}]}
        with tempfile.TemporaryDirectory() as d:
            meos, mdb, sql = (Path(d) / "meos" / "src", Path(d) / "mdb", Path(d) / "sql")
            for p in (meos / "temporal", mdb, sql):
                p.mkdir(parents=True)
            (meos / "temporal" / "meos_catalog.c").write_text("")
            (meos / "x.c").write_text(MEOS_C)
            (mdb / "y.c").write_text(MDB_C)
            (sql / "z.sql").write_text(MDB_SQL)
            idl, _, _ = attach_sqlfn_map(idl, str(meos), str(mdb), str(sql))
        sig = idl["functions"][0]["sqlSignatures"][0]
        self.assertEqual(sig["args"], ["tgeometry[]", "tgeometry[]"])
        self.assertEqual(sig["columns"], [{"name": "i", "type": "integer"},
                                          {"name": "j", "type": "integer"}])
        self.assertNotIn("argDefaults", sig)


# The catalog facts the matching reads: the C type of each class, the temporal
# types, and the structs a returned array can hold.
CATALOG = {
    "objectModel": {"classes": {
        "TBox": {"cType": "TBox"},
        "TsTzSpanSet": {"cType": "SpanSet"},
    }},
    "temporalTypes": {"tbigint": {}, "tgeometry": {}},
    "structs": [
        {"name": "Match", "fields": [{"name": "i", "cType": "int"},
                                     {"name": "j", "cType": "int"}]},
        {"name": "TBox", "fields": [{"name": "period", "cType": "Span"},
                                    {"name": "span", "cType": "Span"}]},
    ],
}


def _cols(*pairs):
    return [{"name": n, "type": t} for n, t in pairs]


def _count():
    return {"name": "count", "cType": "int *"}


def _attach(func, declared=None):
    idl = copy.deepcopy(CATALOG)
    idl["functions"] = [func]
    idl, n = attach_row_sources(idl, declared or {})
    return idl["functions"][0]["sqlSignatures"][0]["columns"], n


VALUE_TIME_SPLIT = {
    "name": "tbigint_value_time_split",
    "sqlfn": "valueTimeSplit",
    "params": [{"name": "temp", "cType": "const Temporal *"},
               {"name": "vsize", "cType": "int64_t"},
               {"name": "value_bins", "cType": "int64_t **"},
               {"name": "time_bins", "cType": "TimestampTz **"},
               _count()],
    "returnType": {"c": "Temporal **"},
    "shape": {"arrayReturn": {"element": {"c": "Temporal *"}},
              "outputArrays": [{"param": "value_bins"}, {"param": "time_bins"}],
              "outParams": ["value_bins", "time_bins", "count"]},
    "sqlSignatures": [{"args": ["tbigint", "bigint"], "ret": "number_time_tbigint",
                       "retSet": True,
                       "columns": _cols(("number", "bigint"), ("time", "timestamptz"),
                                        ("tnumber", "tbigint"))}],
}


def _pairs(with_periods):
    params = [{"name": "arr1", "cType": "const Temporal **"},
              {"name": "count1", "cType": "int"},
              {"name": "arr2", "cType": "const Temporal **"},
              {"name": "count2", "cType": "int"},
              _count()]
    shape = {"arrayReturn": {"element": {"c": "int"}, "groupSize": 2},
             "outParams": ["count"]}
    cols = [("i", "integer"), ("j", "integer")]
    if with_periods:
        params.append({"name": "periods", "cType": "SpanSet ***"})
        shape["outParams"].append("periods")
        shape["outputArrays"] = [{"param": "periods"}]
        cols.append(("periods", "tstzspanset"))
    return {"name": "tdisjoint_tgeoarr_tgeoarr" if with_periods
                    else "edisjoint_tgeoarr_tgeoarr",
            "sqlfn": "tDisjointPairs" if with_periods else "eDisjointPairs",
            "params": params, "returnType": {"c": "int *"}, "shape": shape,
            "sqlSignatures": [{"args": ["tgeometry[]", "tgeometry[]"], "ret": "record",
                               "retSet": True, "columns": _cols(*cols)}]}


PAIRS_OFFSET = {"i": {"offset": 1}, "j": {"offset": 1}}


class RowSourceTests(unittest.TestCase):

    def test_columns_match_their_source_by_type_not_by_position(self):
        """The SQL row is (number, time, tnumber); C returns the fragments and
        writes the bins to its out-parameters."""
        cols, n = _attach(copy.deepcopy(VALUE_TIME_SPLIT))
        self.assertEqual(n, 1)
        self.assertEqual([(c["name"], c["from"]) for c in cols],
                         [("number", "value_bins"), ("time", "time_bins"),
                          ("tnumber", "return")])

    def test_a_flattened_pair_feeds_one_column_per_element(self):
        cols, _ = _attach(_pairs(False), {"eDisjointPairs": PAIRS_OFFSET})
        self.assertEqual(cols, [
            {"name": "i", "type": "integer", "from": "return", "element": 0, "offset": 1},
            {"name": "j", "type": "integer", "from": "return", "element": 1, "offset": 1}])

    def test_an_out_parameter_array_feeds_the_column_after_the_pair(self):
        cols, _ = _attach(_pairs(True), {"tDisjointPairs": PAIRS_OFFSET})
        self.assertEqual(cols[2], {"name": "periods", "type": "tstzspanset",
                                   "from": "periods"})
        self.assertEqual([c.get("element") for c in cols[:2]], [0, 1])

    def test_a_struct_element_feeds_one_column_per_field(self):
        func = {"name": "temporal_dyntimewarp_path", "sqlfn": "dynTimeWarpPath",
                "params": [{"name": "temp1", "cType": "const Temporal *"}, _count()],
                "returnType": {"c": "Match *"},
                "shape": {"arrayReturn": {"element": {"c": "Match"}},
                          "outParams": ["count"]},
                "sqlSignatures": [{"args": ["tgeometry"], "ret": "warp", "retSet": True,
                                   "columns": _cols(("i", "integer"), ("j", "integer"))}]}
        cols, _ = _attach(func)
        self.assertEqual([(c["from"], c["field"]) for c in cols],
                         [("return", "i"), ("return", "j")])

    def test_a_fixed_array_feeds_one_column_per_element(self):
        func = {"name": "pose_quaternion", "sqlfn": "quaternion",
                "params": [{"name": "pose", "cType": "const Pose *"}, _count()],
                "returnType": {"c": "double *"},
                "shape": {"arrayReturn": {"element": {"c": "double"}},
                          "outParams": ["count"]},
                "sqlSignatures": [{"args": ["pose"], "ret": "quaternion",
                                   "columns": _cols(("W", "float"), ("X", "float"),
                                                    ("Y", "float"), ("Z", "float"))}]}
        cols, _ = _attach(func)
        self.assertEqual([(c["from"], c["element"]) for c in cols],
                         [("return", k) for k in range(4)])

    def test_an_ordinal_column_is_declared_and_a_class_is_one_value(self):
        """A `TBox` tile is one value of the class, not its fields."""
        func = {"name": "tintbox_time_tiles", "sqlfn": "timeTiles",
                "params": [{"name": "box", "cType": "const TBox *"}, _count()],
                "returnType": {"c": "TBox *"},
                "shape": {"arrayReturn": {"element": {"c": "TBox"}},
                          "outParams": ["count"]},
                "sqlSignatures": [{"args": ["tbox"], "ret": "index_tbox", "retSet": True,
                                   "columns": _cols(("index", "integer"), ("tile", "tbox"))}]}
        cols, _ = _attach(func, {"index_tbox": {"index": {"from": "ordinal"}}})
        self.assertEqual(cols, [
            {"name": "index", "type": "integer", "from": "ordinal"},
            {"name": "tile", "type": "tbox", "from": "return"}])

    def test_a_found_flag_feeds_no_column(self):
        """A `bool` returned beside out-parameters says whether there is a row."""
        func = {"name": "tpoint_as_mvtgeom", "sqlfn": "asMVTGeom",
                "params": [{"name": "temp", "cType": "const Temporal *"},
                           {"name": "gsarr", "cType": "GSERIALIZED **"},
                           {"name": "timesarr", "cType": "int64_t **"}, _count()],
                "returnType": {"c": "bool"},
                "shape": {"outputArrays": [{"param": "timesarr"}],
                          "outParams": ["gsarr", "timesarr", "count"]},
                "sqlSignatures": [{"args": ["tgeometry"], "ret": "geom_times",
                                   "columns": _cols(("geom", "geometry"),
                                                    ("times", "bigint[]"))}]}
        cols, _ = _attach(func)
        self.assertEqual([(c["name"], c["from"]) for c in cols],
                         [("geom", "gsarr"), ("times", "timesarr")])

    def test_a_row_without_columns_is_left_alone(self):
        func = copy.deepcopy(VALUE_TIME_SPLIT)
        del func["sqlSignatures"][0]["columns"]
        idl = copy.deepcopy(CATALOG)
        idl["functions"] = [func]
        _, n = attach_row_sources(idl, {})
        self.assertEqual(n, 0)


class UnfedRowTests(unittest.TestCase):

    def test_a_column_no_c_value_fits_stops_the_catalog(self):
        func = copy.deepcopy(VALUE_TIME_SPLIT)
        func["sqlSignatures"][0]["columns"][0]["type"] = "text"
        with self.assertRaisesRegex(ValueError, "valueTimeSplit.*number_time_tbigint"):
            _attach(func)

    def test_a_column_two_c_values_fit_stops_the_catalog(self):
        func = copy.deepcopy(VALUE_TIME_SPLIT)
        func["params"][3] = {"name": "time_bins", "cType": "int64_t **"}
        func["sqlSignatures"][0]["columns"][1]["type"] = "bigint"
        with self.assertRaises(ValueError):
            _attach(func)

    def test_a_c_value_feeding_no_column_stops_the_catalog(self):
        func = copy.deepcopy(VALUE_TIME_SPLIT)
        del func["sqlSignatures"][0]["columns"][1]
        with self.assertRaises(ValueError):
            _attach(func)

    def test_an_undeclared_offset_is_not_invented(self):
        """Without its declaration the pair reads the C indices as they are."""
        cols, _ = _attach(_pairs(False))
        self.assertNotIn("offset", cols[0])


class DeclaredColumnsTests(unittest.TestCase):

    def test_the_declarations_validate(self):
        import jsonschema
        jsonschema.validate(json.loads(COLUMNS.read_text()),
                            json.loads(SCHEMA.read_text()))

    def test_an_unknown_source_is_refused(self):
        import jsonschema
        doc = json.loads(COLUMNS.read_text())
        doc["rows"]["index_tbox"]["columns"]["index"] = {"from": "position"}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(doc, json.loads(SCHEMA.read_text()))

    def test_the_declarations_are_read_by_row(self):
        declared = declared_columns(COLUMNS)
        self.assertEqual(declared["index_tbox"], {"index": {"from": "ordinal"}})
        self.assertEqual(declared["tDisjointPairs"], PAIRS_OFFSET)


if __name__ == "__main__":
    unittest.main()
