"""A function keeps only the signatures of the types it serves."""
import json
import unittest
from pathlib import Path

from parser.typescope import (EVERY_OVERLOAD, SQL_ALIASES, declared_scopes,
                              signatures_for, sql_spellings)

META = Path(__file__).resolve().parent.parent / 'meta' / 'type-scope.json'

# `Set_values` is the body behind getValues(intset), getValues(cbufferset) and
# fourteen more, so its signature list is the union over all of them.
SET_VALUES = [
    {'args': ['intset'], 'ret': 'integer[]', 'sqlName': 'getValues'},
    {'args': ['cbufferset'], 'ret': 'cbuffer[]', 'sqlName': 'getValues'},
    {'args': ['npointset'], 'ret': 'npoint[]', 'sqlName': 'getValues'},
]


class SignatureFilterTests(unittest.TestCase):

    def test_a_typed_function_keeps_only_its_own_overload(self):
        kept = signatures_for('intset_values', SET_VALUES, {'intset'})
        self.assertEqual([s['args'] for s in kept], [['intset']])

    def test_each_sibling_keeps_a_different_overload(self):
        for scope, arg in (({'cbufferset'}, 'cbufferset'), ({'npointset'}, 'npointset')):
            kept = signatures_for('x', SET_VALUES, scope)
            self.assertEqual([s['args'] for s in kept], [[arg]])

    def test_a_generic_function_keeps_every_overload(self):
        kept = signatures_for('temporal_shift_time', SET_VALUES, EVERY_OVERLOAD)
        self.assertEqual(len(kept), len(SET_VALUES))

    def test_a_scope_serving_none_of_them_keeps_none(self):
        """What a mistagged @csqlfn looks like: the function names a wrapper
        whose overloads it does not serve."""
        self.assertEqual(signatures_for('span_to_spanset', SET_VALUES, {'intspan'}), [])

    def test_the_return_type_also_places_a_signature(self):
        """An I/O function is named by what it returns, not by its cstring argument."""
        sigs = [{'args': ['cstring'], 'ret': 'bigintset', 'sqlName': 'bigintset_in'},
                {'args': ['cstring'], 'ret': 'intset', 'sqlName': 'intset_in'}]
        kept = signatures_for('bigintset_in', sigs, {'bigintset'})
        self.assertEqual([s['ret'] for s in kept], ['bigintset'])

    def test_a_scope_matches_the_sql_spelling_of_its_types(self):
        """SQL says `integer` where meostype_name says `int4`."""
        self.assertEqual(sql_spellings({'int4'}), {'int4', 'integer'})
        sigs = [{'args': ['integer'], 'ret': 'intspan', 'sqlName': 'span'}]
        self.assertEqual(len(signatures_for('int_to_span', sigs, {'int4'})), 1)

    def test_every_sql_alias_target_differs_from_its_meos_spelling(self):
        for meos, sql in SQL_ALIASES.items():
            self.assertNotEqual(meos, sql)


class DeclaredScopeTests(unittest.TestCase):

    def test_the_declared_file_matches_its_schema_shape(self):
        doc = json.loads(META.read_text())
        for name, entry in doc['scopes'].items():
            self.assertTrue(entry['note'], f'{name} states no reason')
            types = entry['types']
            self.assertTrue(types == EVERY_OVERLOAD or isinstance(types, list),
                            f'{name} has an unusable scope')

    def test_declared_scopes_reads_every_entry(self):
        doc = json.loads(META.read_text())
        self.assertEqual(set(declared_scopes()), set(doc['scopes']))


if __name__ == '__main__':
    unittest.main()
