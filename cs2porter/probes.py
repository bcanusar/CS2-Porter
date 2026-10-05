"""Light probe rooms: combined light probe volumes (cubemap + light probes for players, weapons
and other moving objects) over the whole map, each baking its cubemap in a small white room with
a light, out of sight.

A single volume over a big map does not bake on some maps, so the map is split into several
volumes around its geometry (at most MAX_SIZE units a side, Low Resolution voxels, which also
keeps weapons from turning pink in published maps). Neighbouring volumes overlap by 2 x
EDGE_FADE and fade out over their last EDGE_FADE units, so moving from one to the next is
seamless; the geometry itself is always inside the full strength part of a volume.

Every volume gets its own room: a 256 unit white box with its faces turned inwards and a light
in the middle. A room sits where the compiled map has no world (outside the sealed map), or when
that is far from its volume, inside a thick brush in it (a wall, the ground); and only where the
shadow it could cast in the sun's direction does not reach the space players are in. The room is
the volume's origin, which must be inside the volume: a volume whose room is outside it grows to
take the room in.
"""

import math

from . import vmf as vmfmod

CELL = 256.0                # geometry grid for splitting the map
MAX_SIZE = 8192.0           # largest side of a volume
EDGE_FADE = 256.0           # volumes reach this far past the geometry and fade out over it
ROOM_HALF = 128.0           # the room is a 256 unit cube
ROOM_CLEAR = 64.0           # free space around a room
ROOM_STEP = 512.0           # spacing of the places tried for a room
ROOM_GAP = 448.0            # rooms keep this far apart
WORLD_LIMIT = 16000.0       # rooms stay inside the editor's grid
SUN_STEP = 256.0
MAX_GROW = 1024.0           # a volume grows at most this much for a room outside the map ...
INNER_TRIES = 400           # ... else its room goes inside a brush in it (places tried)

VOXEL_SIZE = "108.0"        # "Low Resolution"
LIGHT_LUMENS = "150"
LIGHT_RANGE = "512"

# brush entities whose brushes are not geometry players see
_NOT_GEOMETRY = ("func_areaportal", "func_areaportalwindow", "func_viscluster", "func_occluder",
                 "func_precipitation", "func_ladder", "func_clip_vphysics", "func_vehicleclip")
_PROP_CLASSES = ("prop_static", "prop_dynamic", "prop_dynamic_override", "prop_physics",
                 "prop_physics_multiplayer", "prop_physics_override", "prop_detail")
_PROP_HALF = 64.0


def _visible_brush(sides):
    return any(not vmfmod.normalize_material(s.get("material", "")).startswith("tools/") for s in sides)


def geometry_boxes(root):
    """[(mins, maxs)] of what players see: brushes with a visible face, displacements, props."""
    from .blend import displacement_triangles
    import re
    boxes = []
    holders = [root.child("world")]
    for e in root.children("entity"):
        cls = e.classname
        if cls in vmfmod.EFFECT_CLASSES or cls in _NOT_GEOMETRY or cls.startswith("trigger_"):
            continue
        if cls in _PROP_CLASSES:
            p = vmfmod._vec(e.get("origin"))
            if p:
                boxes.append((tuple(v - _PROP_HALF for v in p), tuple(v + _PROP_HALF for v in p)))
            continue
        holders.append(e)
    for holder in holders:
        if holder is None:
            continue
        for solid in holder.children("solid") + [s for h in holder.children("hidden") for s in h.children("solid")]:
            sides = solid.children("side")
            pts = []
            if any(s.child("dispinfo") is not None for s in sides):
                for tri in displacement_triangles(solid):
                    pts.extend(tri)
            elif _visible_brush(sides):
                for side in sides:
                    for m in re.findall(r"\(([^)]*)\)", side.get("plane", "")):
                        p = vmfmod._vec(m)
                        if p:
                            pts.append(p)
            if pts:
                boxes.append((tuple(min(p[k] for p in pts) for k in range(3)),
                              tuple(max(p[k] for p in pts) for k in range(3))))
    return boxes


def _cells(boxes):
    cells = set()
    for lo, hi in boxes:
        a = [int(math.floor(lo[k] / CELL)) for k in range(3)]
        b = [int(math.floor(hi[k] / CELL)) for k in range(3)]
        if (b[0] - a[0] + 1) * (b[1] - a[1] + 1) * (b[2] - a[2] + 1) > 200000:
            continue                       # a huge brush (a ground plate far bigger than the map)
        for x in range(a[0], b[0] + 1):
            for y in range(a[1], b[1] + 1):
                for z in range(a[2], b[2] + 1):
                    cells.add((x, y, z))
    return cells


def _split(cells, out, max_cells):
    lo = [min(c[k] for c in cells) for k in range(3)]
    hi = [max(c[k] for c in cells) for k in range(3)]
    size = [hi[k] - lo[k] + 1 for k in range(3)]
    if max(size) <= max_cells:
        out.append((lo, hi))
        return
    axis = max(range(3), key=lambda k: size[k])
    coords = sorted({c[axis] for c in cells})
    # an empty slab between two parts of the map is the best place to cut
    gaps = [(coords[i + 1] - coords[i], i) for i in range(len(coords) - 1) if coords[i + 1] - coords[i] > 1]
    if gaps:
        cut = coords[max(gaps)[1]]
    else:
        cut = coords[len(coords) // 2 - 1] if len(coords) > 1 else coords[0]
    left = [c for c in cells if c[axis] <= cut]
    right = [c for c in cells if c[axis] > cut]
    if not left or not right:
        out.append((lo, hi))
        return
    _split(left, out, max_cells)
    _split(right, out, max_cells)


def volumes(boxes):
    """[(mins, maxs)] of the volumes that cover the geometry."""
    cells = _cells(boxes)
    if not cells:
        return []
    parts = []
    _split(list(cells), parts, max(1, int((MAX_SIZE - 2 * EDGE_FADE) // CELL)))
    out = []
    for lo, hi in parts:
        out.append((tuple(lo[k] * CELL - EDGE_FADE for k in range(3)),
                    tuple((hi[k] + 1) * CELL + EDGE_FADE for k in range(3))))
    return out


def _take_in(vol, room):
    """The volume grown to hold its room (with the room's free space around it)."""
    lo, hi = vol
    if room is None:
        return lo, hi
    r = ROOM_HALF + ROOM_CLEAR
    return (tuple(min(lo[k], room[k] - r) for k in range(3)), tuple(max(hi[k], room[k] + r) for k in range(3)))


def sun_direction(root):
    """Direction the sun light travels in (the light_environment, as the Source 1 compiler
    reads it); straight down when the map has none or it does not point down."""
    for e in root.children("entity"):
        if e.classname != "light_environment":
            continue
        ang = vmfmod._vec(e.get("angles")) or (0.0, 0.0, 0.0)
        pitch = vmfmod._float(e.get("pitch"), 0.0) or ang[0]
        yaw = vmfmod._float(e.get("angle"), 0.0) or ang[1]
        p, y = math.radians(pitch), math.radians(yaw)
        d = (math.cos(y) * math.cos(p), math.sin(y) * math.cos(p), math.sin(p))
        if d[2] < -0.05:
            return d
    return (0.0, 0.0, -1.0)


def _grid(boxes, pad):
    """Points spread over every box, at least pad inside its sides (just the middle of a box that
    is smaller)."""
    pts = set()
    for lo, hi in boxes:
        axes = []
        for k in range(3):
            a, b = lo[k] + pad, hi[k] - pad
            if a > b:
                a = b = (lo[k] + hi[k]) * 0.5
            n = min(int((b - a) // ROOM_STEP) + 1, 12)
            axes.append([a + (b - a) * (i + 0.5) / n for i in range(n)])
        pts.update((round(x), round(y), round(z)) for x in axes[0] for y in axes[1] for z in axes[2])
    return pts


class _Placer:
    def __init__(self, tracer, world, sun, vols):
        self.tracer = tracer
        self.world = world          # (mins, maxs) of the compiled map: the tree is only right inside it
        self.sun = sun
        self.taken = []
        self._valid = {}
        self.cands = self._candidates(vols)
        # inside brushes (thick walls, ground): only when the space outside the map is far
        self.inner = list(_grid(tracer.brush_boxes, ROOM_HALF))

    def _candidates(self, vols):
        """Places to try: the space outside the sealed map inside the world's bounds (from the
        compiled map's tree), and just outside the world's bounds next to every volume."""
        r = ROOM_HALF + ROOM_CLEAR
        pts = _grid([b for b in self.tracer.void_boxes if all(b[1][k] - b[0][k] >= 2 * r for k in range(3))], r)
        wl, wh = self.world
        for lo, hi in vols:
            c = [(lo[k] + hi[k]) * 0.5 for k in range(3)]
            for k in range(3):
                for side in (wl[k] - r - 16, wh[k] + r + 16):
                    p = list(c)
                    p[k] = side
                    pts.add(tuple(round(v) for v in p))
        return [p for p in pts if all(abs(p[k]) + r <= WORLD_LIMIT for k in range(3))]

    def _clip(self, lo, hi):
        wl, wh = self.world
        a = [max(lo[k], wl[k]) for k in range(3)]
        b = [min(hi[k], wh[k]) for k in range(3)]
        return None if any(a[k] >= b[k] for k in range(3)) else (a, b)

    def _in(self, lo, hi, void_only):
        """The box is all outside the sealed map (void_only) or all outside it or inside brushes;
        outside the compiled map's bounds counts as outside the sealed map."""
        part = self._clip(lo, hi)
        return part is None or self.tracer.box_in(part[0], part[1], void_only)

    def fits(self, c, inner=False):
        if (c, inner) not in self._valid:
            self._valid[(c, inner)] = self._fits(c, inner)
        return self._valid[(c, inner)]

    def _fits(self, c, inner):
        r = ROOM_HALF + ROOM_CLEAR
        if any(abs(c[k]) + r > WORLD_LIMIT for k in range(3)):
            return False
        if not self._in([v - r for v in c], [v + r for v in c], not inner):
            return False
        # the room's shadow in the sun's direction must not reach the space players are in
        wl, wh = self.world
        far = max(wh[k] - wl[k] for k in range(3)) * 2.0
        dist = SUN_STEP
        while dist < far:
            p = [c[k] + self.sun[k] * dist for k in range(3)]
            if any(p[k] < wl[k] - r and self.sun[k] <= 0 or p[k] > wh[k] + r and self.sun[k] >= 0
                   for k in range(3)):
                break                   # left the map for good
            if not self._in([v - r for v in p], [v + r for v in p], False):
                return False
            dist += SUN_STEP
        return True

    def _free(self, c):
        return all(max(abs(c[k] - t[k]) for k in range(3)) >= ROOM_GAP for t in self.taken)

    def place(self, vol):
        lo, hi = vol
        center = [(lo[k] + hi[k]) * 0.5 for k in range(3)]
        r = ROOM_HALF + ROOM_CLEAR

        def order(c):
            # inside the volume first, then the nearest; below before above
            out = math.sqrt(sum(max(lo[a] - c[a], 0, c[a] - hi[a]) ** 2 for a in range(3)))
            near = math.sqrt(sum((c[a] - center[a]) ** 2 for a in range(3)))
            return out, c[2] > center[2], near

        best = next((c for c in sorted(self.cands, key=order) if self._free(c) and self.fits(c)), None)
        grow = max(max(lo[k] - best[k] + r, best[k] + r - hi[k], 0.0) for k in range(3)) if best else None
        if best is None or grow > MAX_GROW:
            # the volume would grow a lot: a place inside a brush within the volume instead
            inner = [c for c in self.inner if all(lo[k] + r <= c[k] <= hi[k] - r for k in range(3))]
            inner.sort(key=lambda c: (c[2], sum((c[a] - center[a]) ** 2 for a in range(2))))
            tries = 0
            for c in inner:
                if not self._free(c):
                    continue
                if self.fits(c, True):
                    best = c
                    break
                tries += 1
                if tries >= INNER_TRIES:
                    break
        if best is not None:
            self.taken.append(best)
        return best


def plan(root, tracer=None):
    """[(volume mins, volume maxs, room center or None)] for the map. tracer: lighting.BspTracer
    of the compiled map; without it the rooms go under the map."""
    vols = volumes(geometry_boxes(root))
    if not vols:
        return []
    out = []
    if tracer is not None and tracer.world is not None:
        placer = _Placer(tracer, tracer.world, sun_direction(root), vols)
        for v in vols:
            room = placer.place(v)
            out.append(_take_in(v, room) + (room,))
        return out
    # no compiled map: a room under every volume, below the lowest geometry
    bottom = round(min(v[0][2] for v in vols) - 2 * ROOM_STEP)
    taken = []
    for v in vols:
        c = [round((v[0][k] + v[1][k]) * 0.5) for k in range(2)] + [bottom]
        while any(max(abs(c[k] - t[k]) for k in range(3)) < ROOM_GAP for t in taken):
            c[0] += ROOM_STEP
        c = tuple(c)
        ok = all(abs(c[k]) + ROOM_HALF + ROOM_CLEAR <= WORLD_LIMIT for k in range(3))
        if ok:
            taken.append(c)
        out.append(_take_in(v, c if ok else None) + (c if ok else None,))
    return out
