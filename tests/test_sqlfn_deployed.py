"""A function's `sqlfn` is the one SQL name its signatures carry.

A wrapper's @sqlfn names one member of the family it serves, so a per-type function
read through it (`floatset_in` under `Set_in`, tagged `intset_in()`) would carry a
family member's name. #state_deployed_sqlfn states the name the function's own
CREATE FUNCTION deploys, as #attach_sqlfn_map stamps it on each signature, and keeps
the tag where the signatures carry several names or the record is a backing tag
#classify_backing_sqlfn marks.

Plain unittest, no pytest dependency; synthetic records, then the generated catalog.
"""
import json
import unittest
from pathlib import Path

from parser.sqlfn import state_deployed_sqlfn

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"


def _state(*functions):
    idl, n = state_deployed_sqlfn({"functions": list(functions)})
    return {f["name"]: f for f in idl["functions"]}, n


class DeployedNameTests(unittest.TestCase):

    def test_the_one_name_the_signatures_carry_is_the_sqlfn(self):
        fns, n = _state({"name": "floatset_in", "sqlfn": "intset_in",
                         "sqlSignatures": [{"args": ["cstring"], "ret": "floatset",
                                            "sqlName": "floatset_in"}]})
        self.assertEqual(fns["floatset_in"]["sqlfn"], "floatset_in")
        self.assertEqual(fns["floatset_in"]["sqlSignatures"],
                         [{"args": ["cstring"], "ret": "floatset"}])
        self.assertEqual(n, 1)

    def test_signatures_carrying_several_names_keep_the_tag(self):
        sigs = [{"args": ["tgeompoint", "geometry", "float"], "ret": "boolean",
                 "sqlName": "eDwithin"},
                {"args": ["tgeompoint", "geometry", "float"], "ret": "boolean",
                 "sqlName": "aDwithin"}]
        fns, n = _state({"name": "ea_dwithin_tgeo_geo", "sqlfn": "eDwithin",
                         "sqlSignatures": [dict(s) for s in sigs]})
        self.assertEqual(fns["ea_dwithin_tgeo_geo"]["sqlfn"], "eDwithin")
        self.assertEqual(fns["ea_dwithin_tgeo_geo"]["sqlSignatures"], sigs)
        self.assertEqual(n, 0)

    def test_a_backing_tag_stays_beside_its_public_name(self):
        fns, n = _state({"name": "adjacent_tbox_tnumber", "sqlfn": "adjacent_bbox",
                         "sqlfnBackingOnly": True, "publicSqlName": "adjacent",
                         "sqlSignatures": [{"args": ["tbox", "tint"], "ret": "boolean",
                                            "sqlName": "adjacent"}]})
        self.assertEqual(fns["adjacent_tbox_tnumber"]["sqlfn"], "adjacent_bbox")
        self.assertEqual(fns["adjacent_tbox_tnumber"]["sqlSignatures"][0]["sqlName"],
                         "adjacent")
        self.assertEqual(n, 0)

    def test_a_function_without_signatures_keeps_its_tag(self):
        fns, n = _state({"name": "temporal_in", "sqlfn": "tint_in"})
        self.assertEqual(fns["temporal_in"]["sqlfn"], "tint_in")
        self.assertEqual(n, 0)


class DeployedNameCatalogTests(unittest.TestCase):
    """Over the generated catalog, the per-type functions carry their own SQL name and
    no signature restates the function's sqlfn."""

    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        self.fns = {f["name"]: f for f in json.loads(IDL.read_text())["functions"]}

    def test_per_type_functions_carry_their_own_name(self):
        for name, sqlfn in (("floatset_in", "floatset_in"), ("tfloat_in", "tfloat_in"),
                            ("tfloatinst_make", "tfloat"), ("tfloat_values", "valueSet"),
                            ("contains_cbuffer_cbuffer", "cbuffer_contains")):
            self.assertEqual(self.fns[name]["sqlfn"], sqlfn, name)

    def test_backing_tags_keep_their_family_name(self):
        f = self.fns["adjacent_tbox_tnumber"]
        self.assertEqual((f["sqlfn"], f["publicSqlName"]), ("adjacent_bbox", "adjacent"))

    def test_no_function_names_a_family_member_its_signatures_do_not(self):
        """The condition #state_deployed_sqlfn of parser/sqlfn.py reads, asked of the
        whole catalog: signatures all carrying one name other than sqlfn, a backing
        tag aside."""
        other = [f["name"] for f in self.fns.values()
                 if f.get("sqlSignatures") and not f.get("sqlfnBackingOnly")
                 and len(names := {s.get("sqlName", f["sqlfn"])
                                   for s in f["sqlSignatures"]}) == 1
                 and names != {f["sqlfn"]}]
        self.assertEqual(other, [])


if __name__ == "__main__":
    unittest.main()
