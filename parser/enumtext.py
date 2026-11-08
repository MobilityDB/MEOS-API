"""State the pair of functions that write and read the values of each enumeration as text.

A public function taking an enumeration (``tsequence_make`` an ``interpType``, ``rtree_search``
an ``IndexSearchOp``) takes a value a host can spell only as a number unless MEOS states its
text. MEOS states it by two functions named from the enumeration, lowercased:

    const char *<enum>_name(<enum> value);       /* the text of a value */
    <enum> <enum>_from_string(const char *str);  /* the value of a text */

``interptype_name`` / ``interptype_from_string``, ``indexsearchop_name`` /
``indexsearchop_from_string``. Each enumeration whose two functions the catalog holds with
those signatures gains

    text: {"name": "<enum>_name", "fromString": "<enum>_from_string"}

so a binding passes and answers the value as the SQL functions do, as text, through MEOS's own
functions, and keeps no names of its own. An enumeration lacking either function, or holding
one of another signature, has no ``text``: a binding passes its values as numbers.

This states for an enumeration what #state_type_encodings of parser/codecs.py states for a
type, the functions that write and read its values as text, and reads a slot as that one does
(#_cls of parser/codecs.py, #_ptr_depth of parser/enrich.py).
"""
from __future__ import annotations

from parser.codecs import _cls
from parser.enrich import _ptr_depth


def _slot(slot: dict) -> tuple:
    """The class of a slot and its pointer depth: `(interpType, 0)`, `(char, 1)`."""
    c = slot.get("canonical") or slot.get("c") or slot.get("cType") or ""
    return _cls(slot), _ptr_depth(c)


def _is_name(fn: dict, enum: str) -> bool:
    """Whether the function answers the text of a value: `const char *f(<enum>)`."""
    params = fn.get("params") or []
    return (len(params) == 1 and _slot(params[0]) == (enum, 0)
            and _slot(fn.get("returnType", {})) == ("char", 1))


def _is_from_string(fn: dict, enum: str) -> bool:
    """Whether the function answers the value of a text: `<enum> f(const char *)`."""
    params = fn.get("params") or []
    return (len(params) == 1 and _slot(params[0]) == ("char", 1)
            and _slot(fn.get("returnType", {})) == (enum, 0))


def attach_enum_text(idl: dict) -> tuple[dict, int]:
    """Give each enumeration the ``text`` pair the catalog holds for it; answers the count."""
    by_name = {f["name"]: f for f in idl.get("functions", [])}
    n = 0
    for enum in idl.get("enums", []):
        name, low = enum["name"], enum["name"].lower()
        to_text, from_text = by_name.get(low + "_name"), by_name.get(low + "_from_string")
        enum.pop("text", None)
        if to_text and from_text and _is_name(to_text, name) and _is_from_string(from_text, name):
            enum["text"] = {"name": low + "_name", "fromString": low + "_from_string"}
            n += 1
    return idl, n
