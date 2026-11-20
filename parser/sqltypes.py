"""State, for every SQL type a value of the SQL surface has, how a binding carries it.

Each engine binding (MobilityFlink and MobilitySpark through JMEOS, MobilityNebula,
MobilityDuck) projects the same SQL surface and must carry each SQL type the same way:
read it off the C value a signature pairs it with, cross the engine in one codec, and,
where the C type is laid in one block, keep its memory image. ``sqlTypes`` states that
once, keyed by SQL type, so no binding derives it again:

- ``cType``: the C type the SQL type stands for, read off the signatures that pair it with
  a single C value: a parameter that is one pointer, or the return of a function with no
  out-parameters (#_c_types). A pointer to pointers, or a return an out-parameter counts,
  is an array, and the SQL type opposite it is an array or a record, not a value.
  ``byValue`` marks one passed by value (a cell, ``uint64_t`` behind its typedef).
- ``codec``: the form a value crosses an engine in (#_codec), ``kind`` ``bytes`` (the byte
  codec of the C type, its WKB), ``wkb`` (its hex WKB, the extended one where the C type
  states it, which keeps the SRID) or ``text``, with its ``decoder`` and ``encoder`` and
  the trailing inputs each takes by name (``decoderAux``, ``encoderAux``).
- ``image``: the copy and the size of the block the C type is laid in, the catalog's
  ``typeEncodings[C].image``, for a type carried in a codec and not by value.

The rules are those of JMEOS's ``SqlModel`` (``tools/codegen_jvm.py``: ``_align``, ``_codecs``
and its image), stated here over the public functions, which every binding may call; it reads
the classes #state_type_encodings of parser/codecs.py settles, and the C types through #_base
of parser/enrich.py.
"""
import re
from collections import defaultdict

from parser.enrich import _base, _aux_specs

# The SQL types a binding carries in a type of its language, never through a codec.
SQL_SCALAR = frozenset({
    'boolean', 'integer', 'smallint', 'bigint', 'float', 'double precision', 'text',
    'cstring', 'timestamptz', 'date', 'interval', 'bytea',
})
# The catalog encodings that carry a value as its hex WKB, in the order a codec takes them: the
# extended form where the C type states it, since it keeps the SRID, else the plain one.
HEX_WKB_ENCODINGS = ('hexewkb', 'hexwkb')


def _single_pointer(t):
    """Whether the C type `t` is one pointer to a value (``const Temporal *``), as #_ptr_depth
    of parser/enrich.py counts the pointers of a C type."""
    t = (t or '').strip()
    return t.endswith('*') and t.count('*') == 1


def _deployed_type(t):
    """The SQL type `t` as PostgreSQL deploys a function over it: CREATE FUNCTION discards a
    parenthesized type modifier, so geometry(Point) is geometry and geography(Point)[] is
    geography[]. As #_base reads a C type bare, this reads a SQL type bare."""
    return re.sub(r'^(\w+)\([\w ,]*\)((?:\[\])*)$', r'\1\2', t)


def _byte_arrays(f):
    """The byte-array parameters of `f` (shape.inputArrays over uint8_t, the WKB a reader
    takes, whose length #_byte_reader_aux of parser/codecs.py states as the length of the
    bytes read) with their length parameter, which a binding folds into one bytes value."""
    return {a['param']: (a.get('lengthFrom') or {}).get('name')
            for a in (f.get('shape') or {}).get('inputArrays') or []
            if _base((a.get('element') or {}).get('canonical') or '') == 'uint8_t'
            and (a.get('lengthFrom') or {}).get('kind') == 'param'}


def _visible(f):
    """(the C parameters a SQL argument or a bound literal stands for, the out-parameters),
    the out-parameters being those of the function's shape.outParams, as #_trailing of
    parser/codecs.py leaves them out of a codec's trailing inputs."""
    outs = set((f.get('shape') or {}).get('outParams') or [])
    folded = set(_byte_arrays(f).values())
    return ([p for p in f['params'] if p['name'] not in outs and p['name'] not in folded],
            [p for p in f['params'] if p['name'] in outs])


def _signatures(f):
    """(args, ret, the parameters a wrapper binds) for every SQL signature of `f`, its types as
    PostgreSQL deploys them (#_deployed_type), reading each signature as #_read_type of
    parser/codecs.py does."""
    fbound = (f.get('shape') or {}).get('boundArgs') or {}
    for s in f.get('sqlSignatures') or []:
        ret = s.get('ret')
        present = {p: v for binds in (s.get('presentBinds') or {}).values()
                   for p, v in binds.items()}
        yield ([_deployed_type(a) for a in s.get('args') or []],
               _deployed_type(ret) if ret else ret,
               {**fbound, **(s.get('boundArgs') or {}), **present})


def _c_types(fns, enc):
    """({SQL type: the C type it stands for}, {SQL types passed by value}): the C type each
    SQL type is paired with most often, an argument pairing with the visible C parameters
    the wrapper reads it from, in the catalog's sqlArgParams order where stated. No routine
    of this tree pairs SQL types with C types; the nearest is #_subtype_parents of
    parser/codecs.py, which pairs the classes of the object model."""
    seen = defaultdict(lambda: defaultdict(int))
    byvalue = set()
    for f in fns:
        if not f.get('sqlfn'):
            continue
        vis, outs = _visible(f)
        counts = {(a.get('lengthFrom') or {}).get('name')
                  for a in (f.get('shape') or {}).get('inputArrays') or []}
        for (args, ret, bound), sig in zip(_signatures(f), f.get('sqlSignatures') or []):
            walk = [p for p in vis if p['name'] not in bound and p['name'] not in counts]
            order = sig.get('sqlArgParams') or (f.get('shape') or {}).get('sqlArgParams')
            at = {p['name']: p for p in walk}
            if order and set(order) == set(at):
                walk = [at[n] for n in order]
            if len(args) == len(walk):
                for a, p in zip(args, walk):
                    if _single_pointer(p['canonical']):
                        seen[a][_base(p['canonical'])] += 1
                    elif p.get('typedef') in enc:
                        seen[a][p['typedef']] += 1
                        byvalue.add(p['typedef'])
            rt = f['returnType']
            rc = rt['canonical']
            if ret and not outs and _single_pointer(rc) and _base(rc) != 'char':
                seen[ret][_base(rc)] += 1
            elif ret and not outs and rt.get('typedef') in enc:
                seen[ret][rt['typedef']] += 1
                byvalue.add(rt['typedef'])
    ctype = {s: max(c, key=c.get) for s, c in seen.items() if re.fullmatch(r'\w+', s)}
    return ctype, {s for s, cb in ctype.items() if cb in byvalue}


def _codec(sql, cb, e, callable_, hexwkb, shared):
    """The codec of SQL type `sql` of C type `cb` whose typeEncodings entry is `e`, or None:
    the byte codec of the C type; else its hex WKB where it states a WKB decoder and an
    asHexWKB encoder; else the hex WKB of the SQL type where the C type keys a WKB reader by
    it (geometry: geom_from_hexewkb), the extended form first (HEX_WKB_ENCODINGS); else its
    text, only for a C type a single SQL type stands for, since a text decoder cannot tell
    those SQL types apart. It reads the readers, writers and their inputs #state_type_encodings
    of parser/codecs.py states, as #wire_pair of parser/enrich.py picks a service's pair."""
    dec = e.get('decoders') or {}
    encd = e.get('encoders') or {}
    byt = e.get('bytes') or {}
    if byt.get('decoder') in callable_ and byt.get('encoder') in callable_:
        return {'kind': 'bytes', 'decoder': byt['decoder'], 'encoder': byt['encoder'],
                'decoderAux': [], 'encoderAux': []}
    if dec.get('hexwkb') in callable_ and cb in hexwkb:
        return {'kind': 'wkb', 'decoder': dec['hexwkb'], 'encoder': hexwkb[cb],
                'decoderAux': [], 'encoderAux': []}
    k = next((k for k in HEX_WKB_ENCODINGS
              if ((e.get('readers') or {}).get(k) or {}).get(sql) in callable_
              and encd.get(k) in callable_), None)
    if k:
        return {'kind': 'wkb', 'decoder': e['readers'][k][sql], 'encoder': encd[k],
                'decoderAux': e['readerAux'][k][sql], 'encoderAux': e['encoderAux'][k]}
    if not shared and dec.get('text') in callable_ and encd.get('text') in callable_:
        return {'kind': 'text', 'decoder': dec['text'], 'encoder': encd['text'],
                'decoderAux': e.get('in_aux') or [], 'encoderAux': e.get('out_aux') or []}
    return None


# A C element MEOS reads by another spelling of the same type: int is int32_t on every platform MEOS
# builds on (its int32_in answers an int32_t, the array of integer[] is an int array).
_SAME_C = {'int': 'int32_t'}


def _norm_c(t):
    """The C type `t` bare of const and struct, its pointer kept (``text *``), as #_base reads it
    without the pointer."""
    t = re.sub(r'\b(const|struct)\b', ' ', t or '')
    return ' '.join(t.replace('*', ' * ').split()).replace(' *', '*').replace('*', ' *').strip()


def _array_elements(fns):
    """{SQL scalar: the C type an element of an array of it is laid in}: the element the catalog's
    shape.inputArrays and shape.arrayReturn state opposite a SQL argument or result of that scalar's
    array type, the most frequent where several are stated, a Datum being no element type. The
    signatures pair as #_c_types pairs them."""
    seen = defaultdict(lambda: defaultdict(int))
    for f in fns:
        if not f.get('sqlfn'):
            continue
        shape = f.get('shape') or {}
        arrays = {a['param']: a for a in shape.get('inputArrays') or []}
        vis, _ = _visible(f)
        counts = {(a.get('lengthFrom') or {}).get('name') for a in arrays.values()}
        for (args, ret, bound), sig in zip(_signatures(f), f.get('sqlSignatures') or []):
            walk = [p for p in vis if p['name'] not in bound and p['name'] not in counts]
            order = sig.get('sqlArgParams') or shape.get('sqlArgParams')
            at = {p['name']: p for p in walk}
            if order and set(order) == set(at):
                walk = [at[n] for n in order]
            stated = []
            if len(args) == len(walk):
                stated += [(a, arrays[p['name']].get('element')) for a, p in zip(args, walk)
                           if a.endswith('[]') and p['name'] in arrays]
            if ret and ret.endswith('[]') and shape.get('arrayReturn'):
                stated.append((ret, shape['arrayReturn'].get('element')))
            for a, el in stated:
                c = _norm_c((el or {}).get('canonical'))
                if a[:-2] in SQL_SCALAR and c and c != 'Datum':
                    seen[a[:-2]][c] += 1
    return {s: max(c, key=c.get) for s, c in seen.items()}


def _text_codec(ct, fns, enc, callable_):
    """The text codec of the C element type `ct`, or None: the one the catalog's typeEncodings states
    for it (TimestampTz: timestamptz_in, timestamptz_out), else its one public reader, taking a string
    and answering `ct`, and its one public writer, taking `ct` and answering a string, among the
    functions of category io of the core family, since a SQL scalar is a PostgreSQL built-in type
    MEOS reads and writes in its core (json_in also answers a text, in the JSON family), their trailing
    inputs defaulted as #_aux_specs of parser/enrich.py defaults those of an in/out helper and their
    out-parameters (shape.outParams) left to the binding."""
    e = enc.get(ct) or {}
    if e.get('in') in callable_ and e.get('out') in callable_:
        return {'kind': 'text', 'decoder': e['in'], 'encoder': e['out'],
                'decoderAux': e.get('in_aux') or [], 'encoderAux': e.get('out_aux') or []}
    want = _SAME_C.get(ct, ct)
    readers, writers = [], []
    for f in fns:
        if f['name'] not in callable_ or not f['params'] \
                or f.get('category') != 'io' or f.get('family') != 'CORE':
            continue
        outs = set((f.get('shape') or {}).get('outParams') or [])
        rest = [p for p in f['params'][1:] if p['name'] not in outs]
        aux = _aux_specs(rest)
        if aux is None:
            continue
        first = _norm_c(f['params'][0]['canonical'])
        ret = _norm_c(f['returnType']['canonical'])
        if first == 'char *' and _SAME_C.get(ret, ret) == want:
            readers.append((f['name'], aux))
        elif _SAME_C.get(first, first) == want and ret == 'char *':
            writers.append((f['name'], aux))
    if len(readers) != 1 or len(writers) != 1:
        return None
    return {'kind': 'text', 'decoder': readers[0][0], 'encoder': writers[0][0],
            'decoderAux': readers[0][1], 'encoderAux': writers[0][1]}


def attach_sql_types(idl):
    """Attach ``idl["sqlTypes"]`` (module docstring) and answer the catalog, as
    #state_type_encodings of parser/codecs.py attaches ``typeEncodings``."""
    fns = idl['functions']
    enc = idl.get('typeEncodings') or {}
    callable_ = {f['name'] for f in fns if f.get('api') != 'internal'}
    ctype, byvalue = _c_types(fns, enc)
    hexwkb = {}
    for f in fns:
        if f.get('sqlfn') == 'asHexWKB' and f['params'] and f['name'] in callable_:
            hexwkb.setdefault(_base(f['params'][0]['canonical']), f['name'])
    by_ctype = defaultdict(set)
    for s, cb in ctype.items():
        by_ctype[cb].add(s)
    out = {}
    for sql, cb in sorted(ctype.items()):
        entry = {'cType': cb, 'byValue': sql in byvalue}
        if sql not in SQL_SCALAR and sql != 'internal':
            codec = _codec(sql, cb, enc.get(cb) or {}, callable_, hexwkb, len(by_ctype[cb]) > 1)
            if codec:
                entry['codec'] = codec
                im = (enc.get(cb) or {}).get('image') or {}
                if sql not in byvalue and im.get('copy') in callable_ \
                        and (im.get('size') in callable_ or isinstance(im.get('sizeBytes'), int)):
                    entry['image'] = im
        out[sql] = entry
    for sql, el in sorted(_array_elements(fns).items()):
        codec = _text_codec(el, fns, enc, callable_)
        if codec:
            out.setdefault(sql, {})['element'] = {'cType': el, 'byValue': '*' not in el, 'codec': codec}
    idl['sqlTypes'] = out
    return idl
