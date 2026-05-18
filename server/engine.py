"""MEOS execution engines.

The runtime server is contract-driven: it builds routes/validation from the
enriched catalog and, per call, performs the universal pipeline the `wire`
model implies — *decode each serialized argument, invoke the function, encode
the result*. The actual MEOS calls happen behind the ``Engine`` seam.

Two engines ship:

- ``CtypesEngine`` — the real one: ``dlopen`` a built ``libmeos`` and call
  ``x-meos.decode`` / the function / ``x-meos.encode`` by symbol. Requires a
  compiled MEOS shared library (set ``MEOS_LIBRARY_PATH``); the marshalling
  is unit-tested against a fake library but, by design, the literal native
  linkage is environment-gated.
- ``StubEngine`` — no MEOS runtime: lets the server, routing, request
  validation and error mapping run and be exercised without a build. It
  returns deterministic placeholders and is **not** a computation engine.

Engine contract (all tags are simple strings the app layer assigns from the
``wire`` model: ``ptr`` for a decoded opaque value, ``str``/``int``/
``double``/``bool``/``enum`` for scalars, ``void`` for no result):

    decode(fn_name, value:str)            -> handle          (opaque)
    invoke(fn_name, args:[(tag,val)], rt) -> handle|scalar|None
    encode(fn_name, handle)               -> str
"""

from __future__ import annotations


class MeosError(Exception):
    """A MEOS-level failure; mapped by the server to HTTP 400."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class Engine:
    # ``aux`` is a list of ``(tag, value)`` for the trailing formatting args
    # of the in/out wrapper (e.g. ``[("int", 15)]`` for ``temporal_out``'s
    # ``maxdd``), supplied from the catalog's ``decode_aux``/``encode_aux``.
    def decode(self, fn_name: str, value: str, aux=()):
        raise NotImplementedError

    def invoke(self, fn_name: str, args: list, result_tag: str):
        raise NotImplementedError

    def encode(self, fn_name: str, handle, aux=()) -> str:
        raise NotImplementedError

    def invoke_outparam(self, fn_name: str, args: list, out_ctype: str,
                        presence: bool):
        """``bool f(.., T *result)`` — return ``(present, value)``."""
        raise NotImplementedError

    def invoke_array(self, fn_name: str, args: list):
        """``Elem **f(.., int *count)`` — return a list of element handles."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class StubEngine(Engine):
    """Non-computing placeholder so the server runs without a MEOS build."""

    name = "stub"

    def decode(self, fn_name, value, aux=()):
        return {"_stub_handle": fn_name, "value": value}

    def invoke(self, fn_name, args, result_tag):
        if result_tag == "void":
            return None
        if result_tag == "ptr":
            return {"_stub_handle": fn_name}
        return {"str": "", "int": 0, "double": 0.0,
                "bool": False, "enum": ""}.get(result_tag, "")

    def encode(self, fn_name, handle, aux=()):
        return f"<stub:{fn_name}>"

    def invoke_outparam(self, fn_name, args, out_ctype, presence):
        return True, 0

    def invoke_array(self, fn_name, args):
        return []


class CtypesEngine(Engine):
    """Calls a built libmeos via ctypes, driven entirely by the wire model.

    Every opaque value is an anonymous ``void *`` — no struct layout is ever
    needed, because the catalog already reduced every exposable function to
    *scalars + decode/encode of opaque pointers*.
    """

    name = "ctypes"

    def __init__(self, library_path: str):
        import ctypes

        self._ct = ctypes
        self.lib = ctypes.CDLL(library_path)
        self._argmap = {
            "ptr": ctypes.c_void_p, "str": ctypes.c_char_p,
            "int": ctypes.c_long, "double": ctypes.c_double,
            "bool": ctypes.c_int, "enum": ctypes.c_int,
        }
        self._retmap = {
            "ptr": ctypes.c_void_p, "str": ctypes.c_char_p,
            "int": ctypes.c_long, "double": ctypes.c_double,
            "bool": ctypes.c_int,
        }
        self._last_error = None

        if hasattr(self.lib, "meos_initialize"):
            self.lib.meos_initialize.restype = None
            try:
                self.lib.meos_initialize()
            except TypeError:
                self.lib.meos_initialize(None)

        # MEOS's default error handler calls exit() — a single bad input
        # would kill the whole server. Replace it with one that records the
        # error so the request can be turned into a 400 instead.
        if hasattr(self.lib, "meos_initialize_error_handler"):
            handler_t = ctypes.CFUNCTYPE(
                None, ctypes.c_int, ctypes.c_int, ctypes.c_char_p)

            def _on_error(level, code, msg):
                self._last_error = (
                    int(code),
                    msg.decode(errors="replace") if msg else "MEOS error")

            self._err_cb = handler_t(_on_error)  # keep a ref alive
            self.lib.meos_initialize_error_handler.argtypes = [handler_t]
            self.lib.meos_initialize_error_handler.restype = None
            self.lib.meos_initialize_error_handler(self._err_cb)

    def _sym(self, name: str):
        try:
            return getattr(self.lib, name)
        except AttributeError as e:
            raise MeosError(f"unknown MEOS symbol: {name}", 404) from e

    def _raise_if_error(self) -> None:
        if self._last_error is not None:
            code, msg = self._last_error
            self._last_error = None
            raise MeosError(msg, code)

    def _aux_ctypes(self, aux):
        types = [self._argmap[t] for t, _ in aux]
        vals = [v.encode() if t == "str" and isinstance(v, str) else v
                for t, v in aux]
        return types, vals

    def decode(self, fn_name, value, aux=()):
        self._last_error = None
        f = self._sym(fn_name)
        atypes, avals = self._aux_ctypes(aux)
        f.argtypes = [self._ct.c_char_p, *atypes]
        f.restype = self._ct.c_void_p
        h = f(value.encode(), *avals)
        self._raise_if_error()
        if not h:
            raise MeosError(f"{fn_name} failed to parse input")
        return h

    def invoke(self, fn_name, args, result_tag):
        self._last_error = None
        f = self._sym(fn_name)
        ct = self._ct
        argtypes, cargs = [], []
        for tag, val in args:
            if tag == "ptrarray":            # Elem **arr from a JSON list
                arr = (ct.c_void_p * len(val))(*[ct.c_void_p(h)
                                                 for h in val])
                argtypes.append(ct.POINTER(ct.c_void_p))
                cargs.append(arr)
            else:
                argtypes.append(self._argmap[tag])
                cargs.append(val.encode() if tag == "str"
                             and isinstance(val, str) else val)
        f.argtypes = argtypes
        f.restype = self._retmap.get(result_tag)  # None == void
        r = f(*cargs)
        self._raise_if_error()
        return r

    def encode(self, fn_name, handle, aux=()):
        self._last_error = None
        f = self._sym(fn_name)
        atypes, avals = self._aux_ctypes(aux)
        f.argtypes = [self._ct.c_void_p, *atypes]
        f.restype = self._ct.c_char_p
        r = f(handle, *avals)
        self._raise_if_error()
        return r.decode() if r else None

    def _pointee_ctype(self, out_ctype: str):
        ct = self._ct
        base = " ".join(
            out_ctype.replace("const", "").replace("*", " ").split())
        return {
            "double": ct.c_double, "float": ct.c_float,
            "long long": ct.c_longlong, "unsigned long": ct.c_ulong,
            "long": ct.c_long, "unsigned int": ct.c_uint,
            "short": ct.c_short, "unsigned char": ct.c_ubyte,
            "signed char": ct.c_byte,
        }.get(base, ct.c_int)

    def invoke_outparam(self, fn_name, args, out_ctype, presence):
        self._last_error = None
        f = self._sym(fn_name)
        ct = self._ct
        # T **result -> the slot holds an opaque pointer (c_void_p);
        # T *result -> a scalar slot.
        slot = (ct.c_void_p() if out_ctype.count("*") >= 2
                else self._pointee_ctype(out_ctype)())
        f.argtypes = [self._argmap[t] for t, _ in args] + [ct.POINTER(
            type(slot))]
        f.restype = ct.c_int if presence else None
        cargs = [v.encode() if t == "str" and isinstance(v, str) else v
                 for t, v in args]
        ret = f(*cargs, ct.byref(slot))
        self._raise_if_error()
        present = bool(ret) if presence else True
        return present, slot.value

    def invoke_array(self, fn_name, args):
        # Elem **f(leading..., int *count): MEOS allocates the array and
        # writes the length through the trailing byref count.
        self._last_error = None
        f = self._sym(fn_name)
        ct = self._ct
        n = ct.c_int()
        f.argtypes = [self._argmap[t] for t, _ in args] + [
            ct.POINTER(ct.c_int)]
        f.restype = ct.c_void_p
        cargs = [v.encode() if t == "str" and isinstance(v, str) else v
                 for t, v in args]
        ret = f(*cargs, ct.byref(n))
        self._raise_if_error()
        count = n.value
        if not ret or count <= 0:
            return []
        arr = ct.cast(ret, ct.POINTER(ct.c_void_p))
        return [arr[i] for i in range(count)]


def from_env() -> Engine:
    """``CtypesEngine`` if ``MEOS_LIBRARY_PATH`` is set, else ``StubEngine``."""
    import os

    path = os.environ.get("MEOS_LIBRARY_PATH")
    return CtypesEngine(path) if path else StubEngine()
