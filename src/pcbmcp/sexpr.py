"""KiCad S-expression okuyucu/yazıcı (bağımlılıksız, küçük).

Ayrıştırılmış biçim: liste = düğüm, `Sym` = tırnaksız atom, `str` = tırnaklı metin,
int/float = sayı. Böylece yazarken neyin tırnaklanacağı kaybolmaz.
"""
from __future__ import annotations

import re


class Sym(str):
    """Tırnaksız atom (ör. yes, passive, line)."""


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def parse(text: str):
    stack: list[list] = [[]]
    for m in _TOKEN.finditer(text):
        lp, rp, s, atom = m.groups()
        if lp:
            stack.append([])
        elif rp:
            node = stack.pop()
            stack[-1].append(node)
        elif s is not None:
            stack[-1].append(s.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n"))
        elif atom is not None:
            if _NUM.match(atom):
                stack[-1].append(float(atom) if "." in atom else int(atom))
            else:
                stack[-1].append(Sym(atom))
    return stack[0][0]


def _atom(v) -> str:
    if isinstance(v, Sym):
        return str(v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return s if s not in ("-0", "") else "0"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def dump(node, indent: int = 0) -> str:
    """Okunur çıktı: kısa düğümler tek satır, diğerleri girintili."""
    if not isinstance(node, list):
        return _atom(node)
    flat = "(" + " ".join(dump(c) for c in node) + ")"
    if len(flat) <= 90 and not any(isinstance(c, list) and len(c) > 3 for c in node):
        return flat
    pad = "\t" * (indent + 1)
    head = []
    i = 0
    while i < len(node) and not isinstance(node[i], list):
        head.append(_atom(node[i]))
        i += 1
    parts = ["(" + " ".join(head)]
    for c in node[i:]:
        parts.append("\n" + pad + dump(c, indent + 1))
    parts.append("\n" + "\t" * indent + ")")
    return "".join(parts)


# -------------------------------------------------------------- gezinme yardımcıları

def head(node) -> str | None:
    return str(node[0]) if isinstance(node, list) and node else None


def children(node, name: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def child(node, name: str):
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def value(node, name: str, default=None):
    c = child(node, name)
    return c[1] if c is not None and len(c) > 1 else default
