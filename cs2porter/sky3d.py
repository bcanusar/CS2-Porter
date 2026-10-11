"""3D skybox: the room of the sky_camera becomes a map of its own.

The compiler gives every sealed region of a map its own area (vbsp FindAreas_r floods the
leaves an entity can reach; area portals split a region further). The empty leaves of the
sky_camera's area, and of the areas its area portals lead to, are the skybox room. The brushes
and entities that touch that room are taken out of the map and go into the skybox map; a brush
that also touches the rest of the map stays and is copied.

The skybox map keeps the coordinates of the original map up to the .vmap edits, which move it
so the sky_camera sits on the origin (vmap.finish_skybox_map)."""

import math
import re
import struct

from . import vmf as vmfmod

LUMP_LEAFS = 10
LUMP_AREAS = 20
LUMP_AREAPORTALS = 21
CONTENTS_SOLID = 0x1

SKY_SUFFIX = "_cs2porter_skybox"
# main map entities copied into the skybox map (the importer turns env_fog_controller into
# env_gradient_fog and makes env_sky from the worldspawn's skyname)
COPIED_CLASSES = ("light_environment", "env_fog_controller")
COPY_STEP = 32.0            # the copies stand in a row next to the sky_camera
SPAWN_CLASSES = ("info_player_terrorist", "info_player_counterterrorist", "info_player_start",
                 "info_player_deathmatch", "info_teleport_destination")
# point entities of the room that stay in the main map: map wide settings and logic work from
# anywhere in Source 1 (a light_environment or fog stored in the skybox room is copied as well)
STAY_PREFIXES = ("light_environment", "env_fog_controller", "env_tonemap_controller", "color_correction",
                 "shadow_control", "env_wind", "env_global", "env_hudhint", "env_message", "env_fade",
                 "env_shake", "env_screenoverlay", "env_soundscape", "ambient_generic", "logic_", "game_",
                 "math_", "filter_", "point_servercommand", "point_clientcommand", "point_template",
                 "player_", "info_map_parameters")
TOUCH = 1.0                 # a brush this close to a room leaf touches it
POINT_HALF = 8.0            # a point entity in a solid leaf (inside a wall) counts by a box this big
_PLANE_RE = re.compile(r"\(([^)]*)\)")


def sky_map_name(map_name):
    return f"{map_name}{SKY_SUFFIX}"


class Room:
    def __init__(self, camera, scale, boxes, others):
        self.camera = camera            # sky_camera origin
        self.scale = scale
        self.boxes = boxes              # [(mins, maxs)] of the room's empty leaves
        self.others = others            # [(mins, maxs)] of the other empty leaves
        self.lo = tuple(min(b[0][k] for b in boxes) for k in range(3))
        self.hi = tuple(max(b[1][k] for b in boxes) for k in range(3))


def _leaves(info):
    data = info.read_lump(LUMP_LEAFS)
    size = 56 if info.lumps[LUMP_LEAFS][2] == 0 else 32
    out = []
    for o in range(0, len(data) - size + 1, size):
        contents, _cluster, area_flags = struct.unpack_from("<ihh", data, o)
        out.append((contents, area_flags & 0x1FF, struct.unpack_from("<3h", data, o + 8),
                    struct.unpack_from("<3h", data, o + 14)))
    return out


def _linked_areas(info, area):
    """The area and every area its area portals lead to."""
    areas = info.read_lump(LUMP_AREAS)
    portals = info.read_lump(LUMP_AREAPORTALS)
    links = [struct.unpack_from("<2i", areas, o) for o in range(0, len(areas) - 7, 8)]
    others = [struct.unpack_from("<2H", portals, o)[1] for o in range(0, len(portals) - 11, 12)]
    seen, todo = {area}, [area]
    while todo:
        a = todo.pop()
        if not 0 <= a < len(links):
            continue
        first, count = links[a][1], links[a][0]
        for b in others[first:first + count]:
            if b and b not in seen:
                seen.add(b)
                todo.append(b)
    return seen


def _leaf_at(tracer, p):
    node = tracer.head
    while node >= 0:
        pn, c0, c1 = tracer.nodes[node]
        nx, ny, nz, d = tracer.planes[pn]
        node = c0 if nx * p[0] + ny * p[1] + nz * p[2] - d >= 0 else c1
    return -node - 1


def _origin(ent):
    v = vmfmod._vec(ent.get("origin"))
    return v


def find_room(root, info, tracer):
    """Room of the map's sky_camera, None when the map has none (or it is not in a room of its
    own: a spawn point in it, or nothing else left)."""
    cams = [e for e in root.children("entity") if e.classname == "sky_camera" and _origin(e)]
    if not cams or tracer is None:
        return None
    # each sky_camera puts itself at the head of the server's list and the head is the one the
    # game shows: with several, the last one of the map
    cams = cams[-1:]
    cam = _origin(cams[0])
    leaves = _leaves(info)
    leaf = _leaf_at(tracer, cam)
    if not 0 <= leaf < len(leaves) or leaves[leaf][0] & CONTENTS_SOLID or not leaves[leaf][1]:
        return None
    areas = _linked_areas(info, leaves[leaf][1])
    boxes = [(lo, hi) for c, a, lo, hi in leaves if a in areas and not c & CONTENTS_SOLID]
    others = [(lo, hi) for c, a, lo, hi in leaves if a and a not in areas and not c & CONTENTS_SOLID]
    if not boxes or not others:
        return None
    for e in root.children("entity"):
        if e.classname in SPAWN_CLASSES:
            p = _origin(e)
            if p and leaves[_leaf_at(tracer, p)][1] in areas:
                return None
    try:
        scale = float(cams[0].get("scale") or 16)
    except ValueError:
        scale = 16.0
    return Room(cam, scale, boxes, others)


# --- brushes -----------------------------------------------------------------------------

def _solid_shape(solid):
    """(mins, maxs, planes) of a brush; planes [(normal, dist)] point out of it (empty for a
    displacement: only its box counts)."""
    pts, raw = [], []
    for side in solid.children("side"):
        v = [p for p in (vmfmod._vec(m) for m in _PLANE_RE.findall(side.get("plane", ""))) if p]
        if len(v) >= 3:
            raw.append(v[:3])
            pts += v[:3]
    if not pts:
        return None
    disp = any(s.child("dispinfo") is not None for s in solid.children("side"))
    if disp:
        from .blend import displacement_triangles
        pts += [p for tri in displacement_triangles(solid) for p in tri]
    lo = tuple(min(p[k] for p in pts) for k in range(3))
    hi = tuple(max(p[k] for p in pts) for k in range(3))
    if disp:
        return lo, hi, []
    c = tuple((lo[k] + hi[k]) * 0.5 for k in range(3))
    planes = []
    for a, b, d in raw:
        u = [b[k] - a[k] for k in range(3)]
        w = [d[k] - a[k] for k in range(3)]
        n = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
        ln = math.sqrt(sum(x * x for x in n))
        if ln < 1e-9:
            continue
        n = tuple(x / ln for x in n)
        dist = sum(n[k] * a[k] for k in range(3))
        if sum(n[k] * c[k] for k in range(3)) > dist:
            n, dist = tuple(-x for x in n), -dist
        planes.append((n, dist))
    return lo, hi, planes


def _touches(shape, boxes):
    """True when the brush touches one of the boxes: their bounds meet and no plane of the
    brush has the whole box outside."""
    lo, hi, planes = shape
    for blo, bhi in boxes:
        if any(lo[k] > bhi[k] + TOUCH or hi[k] < blo[k] - TOUCH for k in range(3)):
            continue
        sep = False
        for n, d in planes:
            # the box corner deepest along -n
            near = sum(n[k] * (blo[k] if n[k] > 0 else bhi[k]) for k in range(3))
            if near - d > TOUCH:
                sep = True
                break
        if not sep:
            return True
    return False


def _near(boxes, lo, hi, pad):
    return [b for b in boxes if all(b[0][k] <= hi[k] + pad and b[1][k] >= lo[k] - pad for k in range(3))]


def _point_in(tracer, leaves_area, areas, p, room):
    """'room', 'other' or None for a point entity."""
    leaf = _leaf_at(tracer, p)
    if 0 <= leaf < len(leaves_area):
        a = leaves_area[leaf]
        if a:
            return "room" if a in areas else "other"
    box = (tuple(v - POINT_HALF for v in p), tuple(v + POINT_HALF for v in p), [])
    if _touches(box, room.boxes):
        return "other" if _touches(box, room.others) else "room"
    return None


def _is_solid(item):
    return isinstance(item, vmfmod.VNode) and item.name.lower() == "solid"


def _is_entity(item):
    return isinstance(item, vmfmod.VNode) and item.name.lower() == "entity"


def split(root, info, tracer, room):
    """Takes the room's brushes and entities out of root. Returns the skybox map's VMF root and
    counts {'brushes', 'entities', 'shared'}. root is only changed once every brush and entity
    has been looked at."""
    leaves = _leaves(info)
    areas = _linked_areas(info, leaves[_leaf_at(tracer, room.camera)][1])
    leaves_area = [0 if c & CONTENTS_SOLID else a for c, a, _lo, _hi in leaves]
    others = _near(room.others, room.lo, room.hi, 64.0)

    def where(solid):
        shape = _solid_shape(solid)
        if shape is None:
            return None
        lo, hi = shape[0], shape[1]
        if any(lo[k] > room.hi[k] + TOUCH or hi[k] < room.lo[k] - TOUCH for k in range(3)):
            return None
        if not _touches(shape, room.boxes):
            return None
        return "both" if _touches(shape, _near(others, lo, hi, TOUCH)) else "room"

    # --- where everything is ('room', 'both' or None) ---
    world = root.child("world")
    side = {}
    for holder in ([world] if world is not None else []) + [e for e in root.items if _is_entity(e)]:
        for s in holder.children("solid"):
            side[id(s)] = where(s)
    points = set()
    for e in root.items:
        if _is_entity(e) and not e.children("solid") and not e.classname.lower().startswith(STAY_PREFIXES):
            p = _origin(e)
            if p and _point_in(tracer, leaves_area, areas, p, room) == "room":
                points.add(id(e))

    # --- the skybox map ---
    counts = {"brushes": 0, "entities": 0, "shared": 0}
    sky = vmfmod.VNode("root")
    for node in root.children():
        if node.name.lower() not in ("world", "entity", "cameras", "cordon", "cordons"):
            sky.items.append(vmfmod._copy_node(node))
    sky_world = vmfmod.VNode("world")
    sky.items.append(sky_world)
    if world is not None:
        sky_world.items = [list(i) for i in world.items if isinstance(i, list)]
        keep = []
        for item in world.items:
            w = side.get(id(item)) if _is_solid(item) else None
            if w == "room":
                sky_world.items.append(item)
                counts["brushes"] += 1
                continue
            if w == "both":
                sky_world.items.append(vmfmod._copy_node(item))
                counts["shared"] += 1
            keep.append(item)
        world.items = keep

    keep = []
    for item in root.items:
        if not _is_entity(item):
            keep.append(item)
            continue
        if id(item) in points:
            sky.items.append(item)
            counts["entities"] += 1
            continue
        if not any(side.get(id(s)) for s in item.children("solid")):
            keep.append(item)
            continue
        # a brush entity is split by its brushes (one func_detail may hold brushes all over the
        # map): the room's go, the shared ones are copied
        part, rest = vmfmod.VNode(item.name), []
        for i in item.items:
            w = side.get(id(i)) if _is_solid(i) else "keys"
            if w == "room":
                part.items.append(i)
                continue
            if w == "both":
                part.items.append(vmfmod._copy_node(i))
                counts["shared"] += 1
            elif w == "keys":
                part.items.append(vmfmod._copy_node(i) if isinstance(i, vmfmod.VNode) else list(i))
            rest.append(i)
        sky.items.append(part)
        counts["entities"] += 1
        item.items = rest
        if item.children("solid"):
            keep.append(item)
    root.items = keep

    # the main map's sun and fog, in a row next to the sky_camera
    n = 0
    for ent in root.children("entity"):
        if ent.classname in COPIED_CLASSES:
            n += 1
            copy = vmfmod._copy_node(ent)
            p = room.camera
            copy.set("origin", " ".join(vmfmod._fmt(v) for v in (p[0] + COPY_STEP * n, p[1], p[2])))
            sky.items.append(copy)
    return sky, counts

