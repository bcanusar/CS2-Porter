"""Reads CS2's entity definitions (game/csgo/csgo.fgd and its includes) to know which keys
each entity class has. Used to remove the keys Source 1 maps leave behind."""

import os
import re

_TOKEN_RE = re.compile(r'//[^\n]*|"(?:[^"\\]|\\.)*"|[\[\](){}=:,+]|[^\s\[\](){}=:,+"]+')

# keys Hammer keeps for every entity, whatever the class definition says (spawnflags is not
# one of them: Hammer lists it as unused on classes without flags, e.g. light_omni2)
ALWAYS_KEEP = {"classname", "targetname", "origin", "angles", "scales", "id", "parentname",
               "parentattachmentname", "uselocaloffset", "vscripts", "model", "hammeruniqueid"}


def _tokens(text):
    line = 1
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        tok = m.group(0)
        if tok.startswith("//"):
            continue
        yield tok, line


class FGD:
    def __init__(self):
        self.classes = {}       # name -> {"bases": [...], "keys": set, "removed": set}

    def keys(self, cls, _seen=None):
        """Every key of a class (lower case) with its base classes, or None if unknown."""
        c = self.classes.get(cls.lower())
        if c is None:
            return None
        seen = _seen if _seen is not None else set()
        if cls.lower() in seen:
            return set()
        seen.add(cls.lower())
        out = set(c["keys"])
        for b in c["bases"]:
            out |= self.keys(b, seen) or set()
        return out - c["removed"]

    # --- parsing -----------------------------------------------------------------
    def load(self, path, dirs, _done=None):
        done = _done if _done is not None else set()
        key = os.path.normcase(os.path.abspath(path))
        if key in done or not os.path.isfile(path):
            return
        done.add(key)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            toks = list(_tokens(f.read()))
        i = 0
        while i < len(toks):
            tok, _ln = toks[i]
            if not tok.startswith("@"):
                i += 1
                continue
            kind = tok.lower()
            if kind == "@include" and i + 1 < len(toks):
                name = toks[i + 1][0].strip('"')
                for d in [os.path.dirname(path)] + list(dirs):
                    p = os.path.join(d, name)
                    if os.path.isfile(p):
                        self.load(p, dirs, done)
                        break
                i += 2
                continue
            # header up to the '[' that opens the body (outside parentheses)
            # helpers before the '=' may hold { ... } blocks with '=' and '[' in them: only
            # tokens outside every bracket count (depth 0)
            j, depth, header = i + 1, 0, []
            while j < len(toks):
                t, _l = toks[j]
                if t in "({" or (t == "[" and depth > 0):
                    depth += 1
                elif t in ")}" or (t == "]" and depth > 0):
                    depth -= 1
                elif depth == 0 and (t == "[" or t.startswith("@")):
                    break
                header.append((t, depth))
                j += 1
            if j >= len(toks) or toks[j][0] != "[":
                i = j
                continue
            end = self._body_end(toks, j)
            if kind.endswith("class"):
                self._add_class(kind, header, toks[j + 1:end - 1])
            i = end
        return self

    @staticmethod
    def _body_end(toks, j):
        depth = 0
        for k in range(j, len(toks)):
            t = toks[k][0]
            if t in "[{":
                depth += 1
            elif t in "]}":
                depth -= 1
                if depth == 0:
                    return k + 1
        return len(toks)

    def _add_class(self, kind, header, body):
        bases = []
        eq = next((k for k, (t, d) in enumerate(header) if t == "=" and d == 0), None)
        if eq is None or eq + 1 >= len(header):
            return
        toks = [t for t, _d in header]
        for k, (t, d) in enumerate(header[:eq]):
            if d == 0 and t.lower() == "base" and k + 1 < eq and toks[k + 1] == "(":
                m = k + 2
                while m < eq and toks[m] != ")":
                    if toks[m] != ",":
                        bases.append(toks[m].lower())
                    m += 1
        name = toks[eq + 1].lower()
        keys, removed = set(), set()
        depth, last_line = 0, None
        for k, (t, ln) in enumerate(body):
            first = ln != last_line         # first token of its line: a key starts a line
            last_line = ln
            if t in "[{(":
                depth += 1
            elif t in "]})":
                depth -= 1
            elif depth == 0 and first and k + 1 < len(body) and body[k + 1][0] == "(" \
                    and t.lower() not in ("input", "output") and not t.startswith('"'):
                typ = body[k + 2][0].lower() if k + 2 < len(body) else ""
                (removed if typ == "remove_key" else keys).add(t.lower())
        if kind == "@overrideclass" and name in self.classes:
            c = self.classes[name]
            c["keys"] |= keys
            c["removed"] = (c["removed"] | removed) - keys
            return
        self.classes[name] = {"bases": bases, "keys": keys, "removed": removed}


_CACHE = {}


def load_cs2(cs2_dir):
    """FGD of CS2 (csgo.fgd with its includes), or None when it is not found."""
    game = os.path.join(cs2_dir or "", "game")
    main = os.path.join(game, "csgo", "csgo.fgd")
    if not os.path.isfile(main):
        return None
    key = (os.path.normcase(main), os.path.getmtime(main))
    if key not in _CACHE:
        _CACHE[key] = FGD().load(main, [os.path.join(game, "csgo"), os.path.join(game, "core")])
    return _CACHE[key]
