"""Momentum Mod triggers (ahop_ / surf maps made for Momentum Mod).

- trigger_momentum_teleport -> trigger_teleport when it stops the player and sets the view
  angles (what trigger_teleport does without a landmark); otherwise a trigger_multiple the
  map's script teleports with
- trigger_momentum_speedthreshold -> trigger_multiple whose OnThreshold outputs become OnUser1;
  the map's script checks the player's speed and fires them
- trigger_momentum_slide -> trigger_multiple; the map's script takes the ground friction away
  inside it and lets the player slide down slopes

The script is made with the triggers' settings, compiled into the addon's game folder
(scripts/cs2porter_momentum.vjs_c) and added to the map with a point_script (SCRIPT_REF).
"""

import json
import os

SCRIPT_FILE = "scripts/cs2porter_momentum.js"
SCRIPT_REF = "scripts/cs2porter_momentum.vjs"
NAME_PREFIX = "cs2porter_momentum_"

TELEPORT = "trigger_momentum_teleport"
SPEED = "trigger_momentum_speedthreshold"
SLIDE = "trigger_momentum_slide"


def _on(ent, key, default="1"):
    v = (ent.get(key) or default).strip()
    try:
        return float(v) != 0
    except ValueError:
        return default != "0"


def _num(ent, key, default):
    try:
        return float((ent.get(key) or "").strip())
    except ValueError:
        return default


def _set_class(ent, cls):
    ent.items[ent.index_of_key("classname")] = ["classname", cls]


def _script_name(ent, used, entry):
    """The trigger's name for the script: its own one, unless another trigger of the same name
    needs other settings (then a new one; inputs sent to the old name miss it)."""
    name = (ent.get("targetname") or "").strip()
    if not name or used.get(name.lower(), entry) != entry:
        name = f"{NAME_PREFIX}{len(used) + 1}"
        while name.lower() in used:
            name += "_"
        ent.set("targetname", name)
    used[name.lower()] = entry
    return name


def convert(root, data):
    """Converts the Momentum triggers of a parsed VMF. data receives the script's settings:
    {"teleport": [...], "speed": [...], "slide": [...]}. Returns (teleports, speed, slide)."""
    counts = [0, 0, 0]
    used = {}
    for ent in root.children("entity"):
        cls = ent.classname
        if cls == TELEPORT:
            stop, resetang = _on(ent, "stop"), _on(ent, "resetang")
            if stop and resetang:
                _set_class(ent, "trigger_teleport")
            else:
                entry = {"target": (ent.get("target") or "").strip(), "stop": stop, "resetang": resetang}
                _set_class(ent, "trigger_multiple")
                entry = dict(entry, name=_script_name(ent, used, entry))
                data.setdefault("teleport", []).append(entry)
                ent.remove_key("target")
            for k in ("stop", "resetang"):
                ent.remove_key(k)
            counts[0] += 1
        elif cls == SPEED:
            entry = {"above": (ent.get("AboveOrBelow") or "0").strip() != "1",
                     "horizontal": _on(ent, "Horizontal", "0"), "hspeed": _num(ent, "HorizontalSpeed", 0.0),
                     "vertical": _on(ent, "Vertical", "0"), "vspeed": _num(ent, "VerticalSpeed", 0.0),
                     "think": _on(ent, "OnThink", "0"), "interval": max(_num(ent, "Interval", 0.1), 0.0)}
            _set_class(ent, "trigger_multiple")
            entry = dict(entry, name=_script_name(ent, used, entry))
            data.setdefault("speed", []).append(entry)
            for conns in ent.children("connections"):
                for item in conns.items:
                    if isinstance(item, list) and item[0].lower() == "onthreshold":
                        item[0] = "OnUser1"
            counts[1] += 1
        elif cls == SLIDE:
            entry = {"slide": True}
            _set_class(ent, "trigger_multiple")
            entry = dict(entry, name=_script_name(ent, used, entry))
            data.setdefault("slide", []).append(entry["name"])
            counts[2] += 1
    for k in ("teleport", "speed"):
        if k in data:
            # one entry per name (split triggers share theirs)
            seen = {}
            for e in data[k]:
                seen.setdefault(e["name"].lower(), e)
            data[k] = list(seen.values())
    if "slide" in data:
        data["slide"] = sorted({n.lower(): n for n in data["slide"]}.values())
    return tuple(counts)


def needs_script(data):
    return bool(data and any(data.get(k) for k in ("teleport", "speed", "slide")))


def write_script(content_dir, data):
    """Writes the script with the triggers' settings into the addon's content folder. Returns
    its path."""
    path = os.path.join(content_dir, *SCRIPT_FILE.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    table = json.dumps({k: data.get(k, []) for k in ("teleport", "speed", "slide")}, indent=4)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(SCRIPT.replace("/*DATA*/", table))
    return path


def install(content_dir, game_dir, valve, data, list_path):
    """Compiles the script into <game_dir>/scripts (like the bhop script, only the compiled file
    stays in the addon). The compiler writes beside the content path, so the source is written
    there for the compile and removed after it. Without a game folder or the compiler the
    source stays in the content folder (the map compile compiles it).
    Returns (path, compiled)."""
    src = write_script(content_dir, data)
    if not game_dir or valve is None or not valve.has_resourcecompiler:
        return src, False
    out = os.path.join(game_dir, *SCRIPT_REF.split("/")) + "_c"
    try:
        os.remove(out)
    except OSError:
        pass
    try:
        valve.compile_resources([src], list_path)
    finally:
        compiled = os.path.isfile(out)
        if compiled:
            os.remove(src)
            try:
                os.rmdir(os.path.dirname(src))      # only when nothing else is in it
            except OSError:
                pass
    return (out, True) if compiled else (src, False)


SCRIPT = r"""// Written by CS2 Porter: the Momentum Mod triggers of this map.
//   teleport: trigger_momentum_teleport that keeps the velocity or the view angles
//   speed:    trigger_momentum_speedthreshold (OnThreshold is the trigger's OnUser1)
//   slide:    trigger_momentum_slide (no ground friction inside, slopes pull the player down)
import { Instance, Console, CSPlayerPawn } from "cs_script/point_script";

const DATA = /*DATA*/;

const GRAVITY = 800;
const MAX_STEP = 0.1;
const MIN_SLOPE_Z = 0.7;

let connections = [];
let speedTouch = new Map();     // "name|slot" -> { entry, trigger, pawn, count, next }
let slideTouch = new Map();     // slot -> { pawn, count, speed }
let lastThink = undefined;

function slotOf(pawn) {
    const controller = pawn.GetPlayerController();
    return controller ? controller.GetPlayerSlot() : undefined;
}

function playerOf(inputData) {
    const a = inputData.activator;
    return a && a.IsValid() && a instanceof CSPlayerPawn ? a : undefined;
}

function horizontal(v) {
    return Math.sqrt(v.x * v.x + v.y * v.y);
}

// --- teleport ----------------------------------------------------------------------------

function teleport(entry, pawn) {
    const dest = Instance.FindEntityByName(entry.target);
    if (!dest || !dest.IsValid()) return;
    const values = { position: dest.GetAbsOrigin() };
    if (entry.resetang) values.angles = dest.GetAbsAngles();
    if (entry.stop) values.velocity = { x: 0, y: 0, z: 0 };
    pawn.Teleport(values);
}

// --- speed threshold ---------------------------------------------------------------------

function checkSpeed(st) {
    const v = st.pawn.GetAbsVelocity();
    const e = st.entry;
    const pass = (speed, limit) => e.above ? speed > limit : speed < limit;
    if (e.horizontal && pass(horizontal(v), e.hspeed)) fire(st);
    if (e.vertical && pass(Math.abs(v.z), e.vspeed)) fire(st);
}

function fire(st) {
    Instance.EntFireAtTarget({ target: st.trigger, input: "FireUser1", activator: st.pawn, caller: st.trigger });
}

function speedStart(entry, trigger, pawn) {
    const slot = slotOf(pawn);
    if (slot === undefined) return;
    const key = entry.name.toLowerCase() + "|" + slot;
    let st = speedTouch.get(key);
    if (st) {
        st.count++;
        return;
    }
    st = { entry: entry, trigger: trigger, pawn: pawn, count: 1,
           next: Instance.GetGameTime() + Math.max(entry.interval, 0.01) };
    speedTouch.set(key, st);
    checkSpeed(st);
}

function speedEnd(entry, pawn) {
    const slot = slotOf(pawn);
    if (slot === undefined) return;
    const key = entry.name.toLowerCase() + "|" + slot;
    const st = speedTouch.get(key);
    if (st && --st.count <= 0) speedTouch.delete(key);
}

// --- slide -------------------------------------------------------------------------------

function slideStart(pawn) {
    const slot = slotOf(pawn);
    if (slot === undefined) return;
    const st = slideTouch.get(slot);
    if (st) {
        st.count++;
        return;
    }
    slideTouch.set(slot, { pawn: pawn, count: 1, speed: horizontal(pawn.GetAbsVelocity()) });
}

function slideEnd(pawn) {
    const slot = slotOf(pawn);
    if (slot === undefined) return;
    const st = slideTouch.get(slot);
    if (st && --st.count <= 0) slideTouch.delete(slot);
}

function slide(st, dt) {
    const pawn = st.pawn;
    const v = pawn.GetAbsVelocity();
    let speed = horizontal(v);
    if (pawn.GetGroundEntity() === undefined) {
        st.speed = speed;
        return;
    }
    let vx = v.x, vy = v.y;
    // the ground friction of this tick is given back (a bigger loss is a hit, it stays)
    const friction = (Console.GetConvarNumber("sv_friction") ?? 5.2) * dt
        * Math.max(st.speed, Console.GetConvarNumber("sv_stopspeed") ?? 80);
    if (speed > 1 && st.speed > speed && st.speed - speed <= friction * 1.5 + 1) {
        const scale = st.speed / speed;
        vx *= scale;
        vy *= scale;
    }
    // on a slope gravity pulls along the ground
    const pos = pawn.GetAbsOrigin();
    const tr = Instance.TraceLine({ start: { x: pos.x, y: pos.y, z: pos.z + 2 },
                                    end: { x: pos.x, y: pos.y, z: pos.z - 16 }, ignorePlayers: true });
    if (tr.didHit && tr.normal.z < 0.999 && tr.normal.z >= MIN_SLOPE_Z) {
        const pull = GRAVITY * dt * tr.normal.z;
        vx += tr.normal.x * pull;
        vy += tr.normal.y * pull;
    }
    st.speed = Math.sqrt(vx * vx + vy * vy);
    if (vx !== v.x || vy !== v.y) pawn.Teleport({ velocity: { x: vx, y: vy, z: v.z } });
}

// --- setup -------------------------------------------------------------------------------

function connect(name, start, end) {
    for (const trigger of Instance.FindEntitiesByName(name)) {
        connections.push(Instance.ConnectOutput(trigger, "OnStartTouch", (d) => {
            const pawn = playerOf(d);
            if (pawn) start(trigger, pawn);
        }));
        if (end) {
            connections.push(Instance.ConnectOutput(trigger, "OnEndTouch", (d) => {
                const pawn = playerOf(d);
                if (pawn) end(trigger, pawn);
            }));
        }
    }
}

function init() {
    for (const id of connections) {
        if (id !== undefined && id !== 0) Instance.DisconnectOutput(id);
    }
    connections = [];
    speedTouch.clear();
    slideTouch.clear();
    for (const e of DATA.teleport) connect(e.name, (t, p) => teleport(e, p));
    for (const e of DATA.speed) connect(e.name, (t, p) => speedStart(e, t, p), (t, p) => speedEnd(e, p));
    for (const name of DATA.slide) connect(name, (t, p) => slideStart(p), (t, p) => slideEnd(p));
}

Instance.OnActivate(init);
Instance.OnScriptReload({ after: init });
Instance.OnRoundStart(init);

Instance.SetThink(() => {
    const now = Instance.GetGameTime();
    Instance.SetNextThink(now);
    const dt = lastThink === undefined ? 0 : Math.min(Math.max(now - lastThink, 0), MAX_STEP);
    lastThink = now;
    for (const [key, st] of speedTouch) {
        if (!st.pawn.IsValid() || !st.trigger.IsValid() || !st.pawn.IsAlive()) {
            speedTouch.delete(key);
            continue;
        }
        if (st.entry.think && now >= st.next) {
            st.next = now + Math.max(st.entry.interval, 0.01);
            checkSpeed(st);
        }
    }
    for (const [slot, st] of slideTouch) {
        if (!st.pawn.IsValid() || !st.pawn.IsAlive()) {
            slideTouch.delete(slot);
            continue;
        }
        slide(st, dt);
    }
});
Instance.SetNextThink(Instance.GetGameTime());
"""
