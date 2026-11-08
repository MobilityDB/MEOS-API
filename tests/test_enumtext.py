"""The text pair of each enumeration (parser/enumtext.py)."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.enumtext import attach_enum_text


def _fn(name, ret, *params):
    """A catalog function of the C types given, as #_fn of tests/test_object_model.py."""
    return {"name": name, "returnType": {"c": ret, "canonical": ret},
            "params": [{"name": f"a{i}", "cType": p, "canonical": p}
                       for i, p in enumerate(params)]}


class EnumTextTest(unittest.TestCase):

    def _idl(self, *fns):
        return {"functions": list(fns), "enums": [
            {"name": "interpType", "values": [{"name": "INTERP_NONE", "value": 0}]},
            {"name": "IndexSearchOp", "values": [{"name": "INDEX_OVERLAPS", "value": 0}]},
            {"name": "tempSubtype", "values": [{"name": "TINSTANT", "value": 1}]},
            {"name": "spatialRel", "values": [{"name": "INTERSECTS", "value": 0}]},
        ]}

    def test_an_enumeration_with_both_functions_states_its_pair(self):
        # The two functions named from the enumeration, lowercased, with the signatures
        # `const char *f(<enum>)` and `<enum> f(const char *)`
        idl, n = attach_enum_text(self._idl(
            _fn("interptype_name", "const char *", "interpType"),
            _fn("interptype_from_string", "interpType", "const char *"),
            _fn("indexsearchop_name", "const char *", "IndexSearchOp"),
            _fn("indexsearchop_from_string", "IndexSearchOp", "const char *")))
        text = {e["name"]: e.get("text") for e in idl["enums"]}
        self.assertEqual(n, 2)
        self.assertEqual(text["interpType"],
                         {"name": "interptype_name", "fromString": "interptype_from_string"})
        self.assertEqual(text["IndexSearchOp"], {"name": "indexsearchop_name",
                                                 "fromString": "indexsearchop_from_string"})

    def test_a_pair_of_another_signature_or_half_a_pair_states_none(self):
        # tempsubtype_from_string answers through a bool and an out parameter, so the
        # enumeration states no pair; spatialRel has neither function
        idl, n = attach_enum_text(self._idl(
            _fn("tempsubtype_name", "const char *", "tempSubtype"),
            _fn("tempsubtype_from_string", "bool", "const char *", "int16 *"),
            _fn("interptype_name", "const char *", "interpType")))
        self.assertEqual(n, 0)
        self.assertTrue(all("text" not in e for e in idl["enums"]))


if __name__ == "__main__":
    unittest.main()
