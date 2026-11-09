"""The signatures of the four functions a type's CREATE TYPE binds (parser/typesupport.py).

Built as tests/test_enumtext.py is, its #EnumTextTest a catalog of hand-made functions run
through the pass and its marks read back."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.typesupport import attach_type_support


def _fn(name, *sigs, sqlfn=None):
    """A catalog function whose sqlSignatures are `(sqlName, args, ret)` triples, as #_fn of
    tests/test_enumtext.py builds one from its C types."""
    return {"name": name, "sqlfn": sqlfn or sigs[0][0],
            "sqlSignatures": [{"sqlName": n, "args": list(a), "ret": r} for n, a, r in sigs]}


def _roles(fn):
    return [s.get("typeSupport") for s in fn["sqlSignatures"]]


class TypeSupportTest(unittest.TestCase):

    def _run(self, *fns):
        idl = {"functions": list(fns), "objectModel": {"lattice": {
            "Temporal": {"prefixes": ["temporal"], "temptypes": ["T_TINT", "T_TBOOL"]},
            "TNumber": {"prefixes": ["tnumber"], "temptypes": ["T_TINT"]},
        }}}
        return attach_type_support(idl)

    def test_the_four_functions_of_a_type_are_marked_by_their_role(self):
        fin = _fn("intset_in", ("intset_in", ["cstring"], "intset"),
                  ("intsetFromText", ["text"], "intset"))
        fout = _fn("intset_out", ("intset_out", ["intset"], "cstring"),
                   ("asText", ["intset"], "text"))
        frecv = _fn("set_from_wkb", ("intset_recv", ["internal"], "intset"),
                    ("intsetFromBinary", ["bytea"], "intset"))
        fsend = _fn("set_as_wkb", ("intset_send", ["intset"], "bytea"),
                    ("asBinary", ["intset", "text"], "bytea"))
        _, counts = self._run(fin, fout, frecv, fsend)
        self.assertEqual(_roles(fin), ["input", None])
        self.assertEqual(_roles(fout), ["output", None])
        self.assertEqual(_roles(frecv), ["receive", None])
        self.assertEqual(_roles(fsend), ["send", None])
        self.assertEqual(counts, {"input": 1, "output": 1, "receive": 1, "send": 1})

    def test_the_three_argument_forms_of_input_and_receive_are_marked(self):
        f = _fn("tint_in", ("tint_in", ["cstring", "oid", "integer"], "tint"),
                ("tint_recv", ["internal", "oid", "integer"], "tint"))
        self._run(f)
        self.assertEqual(_roles(f), ["input", "receive"])

    def test_a_class_stem_is_marked_for_a_type_of_that_class(self):
        f = _fn("temporal_out", ("temporal_out", ["tint"], "cstring"),
                ("temporal_send", ["tbool"], "bytea"),
                ("tnumber_out", ["tint"], "cstring"))
        self._run(f)
        self.assertEqual(_roles(f), ["output", "send", "output"])

    def test_a_class_stem_is_not_marked_for_a_type_outside_the_class(self):
        f = _fn("tnumber_out", ("tnumber_out", ["tbool"], "cstring"))
        self._run(f)
        self.assertEqual(_roles(f), [None])

    def test_a_stem_naming_another_type_is_not_marked(self):
        f = _fn("intset_in", ("intset_in", ["cstring"], "floatset"))
        self._run(f)
        self.assertEqual(_roles(f), [None])

    def test_a_signature_outside_the_protocol_of_its_role_is_not_marked(self):
        f = _fn("geo_in", ("geo_in", ["text"], "geo"), ("geo_out", ["geo"], "text"),
                ("geo_send", ["geo", "text"], "bytea"),
                ("geo_recv", ["bytea"], "geo"))
        self._run(f)
        self.assertEqual(_roles(f), [None, None, None, None])

    def test_the_name_of_a_signature_defaults_to_the_sqlfn(self):
        f = {"name": "intset_out", "sqlfn": "intset_out",
             "sqlSignatures": [{"args": ["intset"], "ret": "cstring"}]}
        self._run(f)
        self.assertEqual(_roles(f), ["output"])

    def test_a_rerun_restates_the_mark(self):
        f = _fn("intset_out", ("intset_out", ["intset"], "cstring"))
        f["sqlSignatures"][0]["typeSupport"] = "input"
        self._run(f)
        self.assertEqual(_roles(f), ["output"])
        g = _fn("asText", ("asText", ["intset"], "text"))
        g["sqlSignatures"][0]["typeSupport"] = "output"
        self._run(g)
        self.assertEqual(_roles(g), [None])


if __name__ == "__main__":
    unittest.main()
