from pathlib import Path

from parser.sqlnames import extract_sql_chain, merge_sql_names


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_typed_wrappers_resolve_datum_generic_does_not(tmp_path):
    # Two typed datum-hiding wrappers (in *_meos.c) back one PG wrapper; the
    # Datum-generic carries no @csqlfn -> only the wrappers get the chain.
    _write(tmp_path / "meos/src/tnumber_distance_meos.c", """
/**
 * @csqlfn #Tdistance_tnumber_number()
 */
Temporal *
tdistance_tint_int(const Temporal *temp, int i)
{ return 0; }

/**
 * @csqlfn #Tdistance_tnumber_number()
 */
Temporal *
tdistance_tfloat_float(const Temporal *temp, double d)
{ return 0; }
""")
    _write(tmp_path / "meos/src/tnumber_distance.c", """
Temporal *
tdistance_tnumber_number(const Temporal *temp, Datum value)
{ return 0; }
""")
    _write(tmp_path / "mobilitydb/src/tnumber_distance.c", """
/**
 * @sqlfn tDistance()
 * @sqlop @p <->
 */
Datum
Tdistance_tnumber_number(PG_FUNCTION_ARGS)
{ return 0; }
""")
    chain = extract_sql_chain(tmp_path)
    assert chain["tdistance_tint_int"] == {
        "mobilitydb": "Tdistance_tnumber_number", "sql": "tDistance", "sqlop": "<->"}
    assert chain["tdistance_tfloat_float"]["sql"] == "tDistance"
    assert "tdistance_tnumber_number" not in chain  # Datum-internal, no chain


def test_merge_attaches_to_matching_idl_functions(tmp_path):
    _write(tmp_path / "meos/src/a_meos.c", """
/**
 * @csqlfn #Foo_bar()
 */
int
foo_bar_int(int i)
{ return 0; }
""")
    _write(tmp_path / "mobilitydb/src/b.c", """
/**
 * @sqlfn fooBar()
 */
Datum
Foo_bar(PG_FUNCTION_ARGS)
{ return 0; }
""")
    idl = {"functions": [{"name": "foo_bar_int"}, {"name": "unrelated"}]}
    idl, n = merge_sql_names(idl, tmp_path)
    assert n == 1
    assert idl["functions"][0]["sql"] == "fooBar"
    assert "sql" not in idl["functions"][1]


def test_missing_sources_is_noop(tmp_path):
    idl = {"functions": [{"name": "x"}]}
    _, n = merge_sql_names(idl, tmp_path)
    assert n == 0
