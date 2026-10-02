"""A function keeps only the signatures of the types it serves."""
import json
import unittest
from pathlib import Path

from parser.typescope import (EVERY_OVERLOAD, SQL_ALIASES, TypeFacts, declared_scopes, scope_of,
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


def cell_facts():
    """The type facts of the three cell grids as meos_catalog.c states them: each base type and
    the set built over it, in the fields #TypeFacts reads from that file, so no tree is needed."""
    facts = TypeFacts.__new__(TypeFacts)
    grids = ('h3index', 'quadbin', 's2cell')
    facts.name = {}
    facts.names = set(grids) | {g + 'set' for g in grids}
    facts.klass, facts.validate = {}, {}
    facts.container = {g: {g + 'set'} for g in grids}
    return facts


class CParamScopeTests(unittest.TestCase):
    """A value-first set operation delegating to its set-first twin states its scope only through
    its C parameter, as union_bigint_set does through `int64`; built as #SignatureFilterTests is,
    over synthetic inputs."""

    def scope(self, fn, ctype):
        params = {fn: '%s cell, const Set *s)' % ctype}
        bodies = {fn: 'Set *\n%s(%s cell, const Set *s)\n{\n  return x(s, cell);\n}' % (fn, ctype)}
        return scope_of(fn, cell_facts(), bodies, params)

    def test_every_cell_grid_names_its_base_type_through_its_c_typedef(self):
        self.assertEqual(self.scope('union_h3index_set', 'H3Index'),
                         ({'h3index', 'h3indexset'}, 'cparam'))
        self.assertEqual(self.scope('union_quadbin_set', 'Quadbin'),
                         ({'quadbin', 'quadbinset'}, 'cparam'))
        # the C typedef is not the type name lowercased: S2CellId spells s2cell
        self.assertEqual(self.scope('union_s2cell_set', 'S2CellId'),
                         ({'s2cell', 's2cellset'}, 'cparam'))


if __name__ == '__main__':
    unittest.main()
