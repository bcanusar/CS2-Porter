"""Source 1 KeyValues parser (VMT, gameinfo.txt, refs lists).

The result is a tree that keeps the order and allows repeated keys:
    KVNode(name, children=[KVNode | (key, value)])
"""

import re


class KVNode:
    __slots__ = ("name", "items")

    def __init__(self, name=""):
        self.name = name
        self.items = []          # [(key, str) | KVNode]

    # --- helpers -----------------------------------------------------
    def values(self):
        return [i for i in self.items if isinstance(i, tuple)]

    def blocks(self):
        return [i for i in self.items if isinstance(i, KVNode)]

    def get(self, key, default=None):
        k = key.lower()
        for item in self.items:
            if isinstance(item, tuple) and item[0].lower() == k:
                return item[1]
        return default

    def block(self, name):
        n = name.lower()
        for item in self.items:
            if isinstance(item, KVNode) and item.name.lower() == n:
                return item
        return None

    def __repr__(self):
        return f"KVNode({self.name!r}, {len(self.items)} items)"


_TOKEN_RE = re.compile(
    r'"([^"\n]*)"'              # quoted (no escape handling, like Valve: "models\props\")
    r'|(\{|\})'                 # brace
    r'|(//[^\n]*)'              # comment
    r'|(\[[^\]\n]*\])'          # condition [$WIN32]
    r'|([^\s{}"]+)'             # unquoted
)


def tokenize(text):
    for m in _TOKEN_RE.finditer(text):
        quoted, brace, comment, cond, bare = m.groups()
        if comment is not None or cond is not None:
            continue
        if quoted is not None:
            yield ("str", quoted)
        elif brace is not None:
            yield (brace, brace)
        else:
            yield ("str", bare)


def parse(text):
    """Turns text into a KVNode tree. Tolerates broken files as much as possible."""
    if text.startswith("﻿"):
        text = text[1:]
    root = KVNode("")
    stack = [root]
    pending_key = None
    for kind, val in tokenize(text):
        cur = stack[-1]
        if kind == "{":
            node = KVNode(pending_key if pending_key is not None else "")
            cur.items.append(node)
            stack.append(node)
            pending_key = None
        elif kind == "}":
            if pending_key is not None:
                cur.items.append((pending_key, ""))
                pending_key = None
            if len(stack) > 1:
                stack.pop()
        else:
            if pending_key is None:
                pending_key = val
            else:
                cur.items.append((pending_key, val))
                pending_key = None
    return root


def parse_file(path):
    with open(path, "rb") as f:
        data = f.read()
    return parse(decode_bytes(data))


def decode_bytes(data):
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")
