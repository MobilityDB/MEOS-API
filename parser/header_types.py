"""Recover opaque parameter/return types from the header *source*.

The PostgreSQL stub headers ``#define`` several opaque types (``Interval``,
``text``, ``TimestampTz`` …) to ``int`` *before* libclang parses, so the
typedef name is destroyed: a ``const Interval *`` argument reaches the
catalog as ``const int *``, indistinguishable from a real ``int *``
out-parameter and impossible to project correctly.

The public MEOS headers, however, declare every function on a regular
``extern <ret> <name>(<params>);`` line with the *true* spellings. This
module re-scans that source and reconciles the catalog: where libclang
produced a bare scalar but the header says a distinct **named pointer**
type, the header is the truth. Scalar typedefs (``TimestampTz``, ``int64``,
enums) are deliberately left as their resolved scalar — only mis-rendered
*opaque pointers* are restored.

Pure text + ``re``; no libclang.
"""

import re
from pathlib import Path

_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
_DECL = re.compile(
    r"\bextern\b\s+(?P<sig>[^;{}]+?\([^;{}]*\))\s*;", re.DOTALL)
_FUNC = re.compile(r"^(?P<ret>.+?)\b(?P<name>\w+)\s*\((?P<params>.*)\)$",
                   re.DOTALL)

_SCALARS = {
    "void", "bool", "_Bool", "char", "signed char", "unsigned char",
    "short", "unsigned short", "int", "unsigned int", "long",
    "unsigned long", "long long", "unsigned long long", "size_t",
    "float", "double", "long double",
    # scalar typedefs we *want* left resolved to their integer form
    "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32",
    "uint64", "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t",
    "uint16_t", "uint32_t", "uint64_t", "TimestampTz", "TimeADT",
    "DateADT", "Timestamp", "Datum", "meosType", "interpType",
}


def _norm(t: str) -> str:
    return " ".join(t.replace("*", " * ").split())


def _base(t: str) -> str:
    return " ".join(
        re.sub(r"\b(const|volatile|struct|union|enum)\b", " ", t)
        .replace("*", " ").split()
    )


def _split_params(s: str) -> list:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _param_type(p: str) -> str:
    p = p.strip()
    if p in ("void", ""):
        return "void"
    p = re.sub(r"\[[^\]]*\]", " *", p)            # arr[] -> arr *
    m = re.match(r"^(.*?)(\w+)\s*$", p, re.DOTALL)  # strip trailing name
    return _norm(m.group(1) if m and "*" not in m.group(2) else p)


def scan_headers(headers_dir: Path) -> dict:
    """``{func_name: {"ret": type, "params": [type, …]}}`` from source."""
    out: dict = {}
    for h in sorted(Path(headers_dir).glob("**/*.h")):
        text = _COMMENT.sub(" ", h.read_text(errors="replace"))
        for d in _DECL.finditer(text):
            m = _FUNC.match(" ".join(d.group("sig").split()))
            if not m:
                continue
            params = [_param_type(x) for x in _split_params(m.group("params"))]
            out[m.group("name")] = {
                "ret": _norm(m.group("ret")),
                "params": params if params != ["void"] else [],
            }
    return out


def _restore(decl_canon: str, header_t: str, enums: set) -> str | None:
    """Return the header type if ``decl_canon`` is a scalar but the header
    says a distinct named *pointer* opaque type, else ``None``."""
    cb = _base(decl_canon)
    hb, hd = _base(header_t), header_t.count("*")
    if (hd >= 1 and hb and hb not in _SCALARS and hb not in enums
            and cb in _SCALARS):
        return _norm(header_t)
    return None


def reconcile(idl: dict, headers_dir: Path) -> dict:
    """Restore opaque pointer types the stub headers erased to ``int``."""
    headers = scan_headers(headers_dir)
    enums = {e["name"] for e in idl.get("enums", [])}
    for fn in idl.get("functions", []):
        h = headers.get(fn["name"])
        if not h:
            continue
        rt = fn["returnType"]
        fixed = _restore(rt["canonical"], h["ret"], enums)
        if fixed:
            rt["c"] = rt["canonical"] = fixed
        for p, ht in zip(fn.get("params", []), h["params"]):
            fixed = _restore(p["canonical"], ht, enums)
            if fixed:
                p["cType"] = p["canonical"] = fixed
    return idl
