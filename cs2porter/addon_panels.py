"""Tools that work on an addon in place: the shader converter and the soundevent editor."""
import copy
import ctypes
import os
import shutil
import tkinter as tk
from tkinter import filedialog, ttk

from . import config, shaders, soundevents as se
from .i18n import t
from .valve_tools import ValveTools


def _c(name):
    """A color of the open theme (app.py keeps them as module names)."""
    from . import app
    return getattr(app, name)


def _ui(name):
    from . import app
    return getattr(app, name)


def _content(app, addon):
    return os.path.join(app.cfg.get("cs2_dir", ""), "content", "csgo_addons", addon)


class AddonBar(ttk.Frame):
    """Addon picker row: the addons of the CS2 folder, refresh and open folder."""

    def __init__(self, master, app, on_change, key):
        super().__init__(master, style="Inner.TFrame")
        self.app, self.on_change, self.key = app, on_change, key
        ttk.Label(self, text=t("ap_addon"), style="Inner.TLabel").pack(side="left")
        self.cb = ttk.Combobox(self, state="readonly", width=34, postcommand=self.fill)
        self.cb.pack(side="left", padx=(6, 6))
        self.cb.bind("<<ComboboxSelected>>", lambda e: self._changed())
        ttk.Button(self, text=t("ap_refresh"), command=lambda: self.on_change(self.get(), True)).pack(side="left")
        ttk.Button(self, text=t("ap_open"), command=self._open).pack(side="left", padx=(6, 0))
        self.fill()
        keep = getattr(app, "_panel_addon", {})
        cur = keep.get(key) or (app.v_addon.get().strip() if hasattr(app, "v_addon") else "")
        if cur in self.cb["values"]:
            self.cb.set(cur)
        self._last = self.get()

    def fill(self):
        cs2 = self.app.cfg.get("cs2_dir", "")
        self.cb["values"] = config.list_addons(cs2) if config.is_valid_cs2(cs2) else []

    def get(self):
        return self.cb.get().strip()

    def _changed(self):
        new = self.get()
        if new == self._last:
            return
        if not self.on_change(new, False):
            self.cb.set(self._last)
            return
        self._last = new
        self.app.__dict__.setdefault("_panel_addon", {})[self.key] = new

    def _open(self):
        a = self.get()
        if a and os.path.isdir(_content(self.app, a)):
            _ui("open_in_explorer")(_content(self.app, a))


def _tree(master, cols, heads, widths, show="headings", height=12, on_scroll=None):
    wrap = ttk.Frame(master, style="Inner.TFrame")
    tree = ttk.Treeview(wrap, columns=cols, show=show, height=height, selectmode="extended")
    for c in cols:
        w, stretch = widths.get(c, (120, False))
        tree.heading(c, text=heads.get(c, ""), anchor="w")
        tree.column(c, width=w, stretch=stretch, anchor="w")
    ys = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)

    def yset(first, last):
        ys.set(first, last)
        if on_scroll:
            on_scroll()
    tree.configure(yscrollcommand=yset)
    tree.pack(side="left", fill="both", expand=True)
    ys.pack(side="right", fill="y")
    return wrap, tree


# =============================================================================
# Shader converter
# =============================================================================
class ShaderPanel(ttk.Frame):
    """The materials of an addon with their shader; the checked ones move to another shader.
    Textures a shader has no place for stay in the file (shaders.py), so a material can go back."""

    def __init__(self, master, app):
        super().__init__(master, style="Inner.TFrame")
        self.app = app
        self.rows = []
        self.bar = AddonBar(self, app, self.load, "shader")
        self.bar.pack(fill="x", pady=(0, 8))

        tb = ttk.Frame(self, style="Inner.TFrame")
        tb.pack(fill="x", pady=(0, 6))
        self.lbl_count = ttk.Label(tb, text="", style="InnerMuted.TLabel")
        self.lbl_count.pack(side="left")
        self.query = tk.StringVar()
        ttk.Entry(tb, textvariable=self.query, width=22).pack(side="right", padx=(6, 12))
        ttk.Label(tb, text=t("table_search"), style="Inner.TLabel").pack(side="right")
        self.filter = ttk.Combobox(tb, state="readonly", width=26)
        self.filter.pack(side="right", padx=(6, 12))
        ttk.Label(tb, text=t("sc_show"), style="Inner.TLabel").pack(side="right")
        self.filter.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.query.trace_add("write", lambda *a: self.refresh())

        act = ttk.Frame(self, style="Inner.TFrame")
        act.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Label(act, text=t("sc_target"), style="Inner.TLabel").pack(side="left")
        self.target = ttk.Combobox(act, state="readonly", width=30,
                                   values=[t("shader_" + s) for s in config.SHADER_CHOICES])
        self.target.current(0)
        self.target.pack(side="left", padx=(6, 16))
        self.v_alpha = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(act, text=t("sc_alphatest"), variable=self.v_alpha, style="Inner.TCheckbutton")
        cb.pack(side="left", padx=(0, 16))
        _ui("ToolTip")(cb, t("sc_alphatest_tip"))
        self.v_compile = tk.BooleanVar(value=True)
        ttk.Checkbutton(act, text=t("ap_compile_after"), variable=self.v_compile,
                        style="Inner.TCheckbutton").pack(side="left", padx=(0, 16))
        self.btn = ttk.Button(act, text=t("sc_convert"), style="Accent.TButton", command=self.convert)
        self.btn.pack(side="right")
        if app.busy():
            self.btn.config(state="disabled")

        wrap, self.tree = _tree(self, ("check", "name", "shader", "status", "path"),
                                {"check": "", "name": t("flcol_name"), "shader": t("sc_col_shader"),
                                 "status": t("flcol_status"), "path": t("flcol_path")},
                                {"check": (34, False), "name": (240, False), "shader": (190, False),
                                 "status": (260, False), "path": (300, True)}, on_scroll=self._boxes_later)
        wrap.pack(fill="both", expand=True)
        self.tree.column("check", anchor="center")
        # a click on a heading sorts by that column, a second click turns the order around
        self.sort_col, self.sort_desc = None, False
        self._heads = {"name": t("flcol_name"), "shader": t("sc_col_shader"), "status": t("flcol_status"),
                       "path": t("flcol_path")}
        for c in self._heads:
            self.tree.heading(c, command=lambda c=c: self._sort_by(c))
        # real check boxes laid over the check cells of the visible rows (like the tool lists),
        # and one over the heading that checks or unchecks every shown row
        self._boxes, self._box_job = [], None
        self.v_head = tk.BooleanVar(value=False)
        self.head_box = ttk.Checkbutton(self.tree, variable=self.v_head, style="CellHead.TCheckbutton",
                                        takefocus=0, cursor="hand2", command=self._head_clicked)
        self.head_box.place(x=17, y=12, anchor="center")
        _ui("ToolTip")(self.head_box, t("sc_check_all_tip"))
        for ev in ("<Configure>", "<<TreeviewSelect>>", "<B1-Motion>", "<ButtonRelease-1>", "<Map>"):
            self.tree.bind(ev, self._boxes_later, add="+")
        for st, color in (("done", _c("OK")), ("skip", _c("WARN")), ("error", _c("ERR")), ("same", _c("MUTED"))):
            self.tree.tag_configure(st, foreground=color)
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<Button-3>", self._menu)
        self.tree.bind("<space>", lambda e: (self._toggle_rows(self.tree.selection()), "break")[1])
        self.load(self.bar.get(), True)

    # --- rows -------------------------------------------------------------------
    def load(self, addon, _refresh=False):
        self.rows = []
        cdir = _content(self.app, addon) if addon else ""
        if cdir and os.path.isdir(cdir):
            for d, subs, files in os.walk(cdir):
                subs[:] = [s for s in subs if not s.startswith(".")]
                for f in files:
                    if not f.lower().endswith(".vmat"):
                        continue
                    p = os.path.join(d, f)
                    try:
                        with open(p, "r", encoding="utf-8", errors="replace") as fh:
                            sh = shaders.shader_of(fh.read()) or "?"
                    except OSError:
                        continue
                    rel = os.path.relpath(p, cdir).replace("\\", "/")
                    self.rows.append({"path": p, "rel": rel, "name": f[:-5], "shader": sh, "checked": False,
                                      "status": "", "tag": ""})
        self.rows.sort(key=lambda r: r["rel"].lower())
        # each shader of the addon with its count, so one shader's materials can be shown alone
        counts = {}
        for r in self.rows:
            counts[r["shader"]] = counts.get(r["shader"], 0) + 1
        self._shaders = [None] + sorted(counts)
        vals = [t("sc_all") + f" ({len(self.rows)})"] + [f"{sh} ({counts[sh]})" for sh in self._shaders[1:]]
        self.filter["values"] = vals
        # wide enough for the longest shader name with its count, so the list is not cut off
        self.filter.configure(width=max(26, max(len(v) for v in vals) + 3))
        self.filter.current(0)
        self.refresh()
        return True

    def _visible(self):
        q = self.query.get().strip().lower()
        k = self.filter.current()
        f = self._shaders[k] if 0 <= k < len(getattr(self, "_shaders", ())) else None
        out = []
        for i, r in enumerate(self.rows):
            if f and r["shader"] != f:
                continue
            if q and q not in r["rel"].lower():
                continue
            out.append(i)
        return out

    def _sort_by(self, col):
        if col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, False
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for c, label in self._heads.items():
            arrow = (" ▼" if self.sort_desc else " ▲") if c == self.sort_col else ""
            self.tree.heading(c, text=label + arrow)
        vis = self._visible()
        if self.sort_col:
            col = "rel" if self.sort_col == "path" else self.sort_col
            vis.sort(key=lambda i: (str(self.rows[i][col]).lower(), self.rows[i]["rel"].lower()),
                     reverse=self.sort_desc)
        for i in vis:
            r = self.rows[i]
            self.tree.insert("", "end", iid=str(i), values=("", r["name"], r["shader"], r["status"], r["rel"]),
                             tags=(r["tag"],))
        self._count()

    def _set_row(self, i):
        r = self.rows[i]
        if self.tree.exists(str(i)):
            self.tree.item(str(i), values=("", r["name"], r["shader"], r["status"], r["rel"]), tags=(r["tag"],))

    def _count(self):
        # only the shown rows count and get converted: a filter never converts hidden ones
        vis = self._visible()
        n = sum(self.rows[i]["checked"] for i in vis)
        self.lbl_count.config(text=t("sc_count", n=n, total=len(vis)))
        self.v_head.set(bool(vis) and all(self.rows[i]["checked"] for i in vis))
        self._boxes_later()

    # --- the check boxes ------------------------------------------------------
    def _boxes_later(self, _e=None):
        if self._box_job is None:
            self._box_job = self.after_idle(self._place_boxes)

    def _new_box(self):
        var = tk.BooleanVar()
        cb = ttk.Checkbutton(self.tree, variable=var, style="Cell.TCheckbutton", takefocus=0, cursor="hand2")

        def clicked():
            if cb.iid is not None:
                sel = self.tree.selection()
                self._toggle_rows(sel if cb.iid in sel else (cb.iid,), bool(var.get()))
        cb.configure(command=clicked)
        cb.bind("<MouseWheel>", lambda e: (self.tree.yview_scroll(int(-e.delta / 120), "units"), "break")[1])
        cb.iid = None
        return cb, var

    def _place_boxes(self):
        self._box_job = None
        if not self.tree.winfo_exists():
            return
        sel = set(self.tree.selection())
        n, started = 0, False
        for iid in self.tree.get_children(""):
            if not iid.isdigit():
                continue
            bb = self.tree.bbox(iid, "check")
            if not bb:
                if started:
                    break       # the rows below are out of view too
                continue
            started = True
            if n == len(self._boxes):
                self._boxes.append(self._new_box())
            cb, var = self._boxes[n]
            cb.iid = iid
            var.set(self.rows[int(iid)]["checked"])
            cb.configure(style="CellSel.TCheckbutton" if iid in sel else "Cell.TCheckbutton")
            x, y, w, h = bb
            cb.place(x=x + w // 2, y=y + h // 2, anchor="center")
            n += 1
        for cb, _v in self._boxes[n:]:
            cb.place_forget()
            cb.iid = None
        # the heading box sits in the middle of the heading row (above the first row)
        self.head_box.place_configure(y=self._head_mid())

    def _head_mid(self):
        """Middle of the heading row. Measured on the first row; with no rows, on a row put in
        for a moment, so the box stays where it is whether the list is empty or not."""
        if getattr(self, "_head_y", None):
            return self._head_y
        kids = self.tree.get_children("")
        probe = None
        if not kids:
            probe = self.tree.insert("", "end", iid="probe", values=("",) * len(self.tree["columns"]))
        self.tree.update_idletasks()
        bb = self.tree.bbox(probe or kids[0])
        if probe:
            self.tree.delete(probe)
        if bb and bb[1] > 4:
            self._head_y = bb[1] // 2
            return self._head_y
        return 12

    def _menu(self, e):
        """Right click on a material: its folder, the file, copying and checking."""
        iid = self.tree.identify_row(e.y)
        if not iid.isdigit():
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        sel = [i for i in self.tree.selection() if i.isdigit()]
        rows = [self.rows[int(i)] for i in sel]
        r = self.rows[int(iid)]
        copy, menu = _ui("copy_text"), _ui("popup_menu")(self)

        def open_file():
            try:
                os.startfile(r["path"])  # noqa: S606
            except OSError:
                _ui("open_in_explorer")(r["path"])
        menu.add_command(label=t("show_in_folder"), command=lambda: _ui("open_in_explorer")(r["path"]))
        menu.add_command(label=t("sc_open_file"), command=open_file)
        menu.add_separator()
        names = [x["name"] for x in rows]
        paths = [os.path.normpath(x["path"]) for x in rows]
        menu.add_command(label=t("menu_copy_name") if len(rows) == 1 else t("menu_copy_names", n=len(rows)),
                         command=lambda: copy(self, "\n".join(names)))
        menu.add_command(label=t("menu_copy_path") if len(rows) == 1 else t("menu_copy_paths", n=len(rows)),
                         command=lambda: copy(self, "\n".join(paths)))
        menu.add_separator()
        menu.add_command(label=t("sc_check_sel"), command=lambda: self._toggle_rows(sel, True))
        menu.add_command(label=t("sc_uncheck_sel"), command=lambda: self._toggle_rows(sel, False))
        if r["shader"] in self._shaders:
            menu.add_separator()
            menu.add_command(label=t("sc_only_shader", shader=r["shader"]), command=lambda: (
                self.filter.current(self._shaders.index(r["shader"])), self.refresh()))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _toggle_rows(self, iids, on=None):
        """Checks (on True), unchecks (False) or switches (None, by the first row) the rows."""
        iids = [i for i in iids if self.tree.exists(i)]
        if not iids:
            return
        if on is None:
            on = not self.rows[int(iids[0])]["checked"]
        for i in iids:
            self.rows[int(i)]["checked"] = on
        self._count()

    def _click(self, e):
        """A click anywhere in a check cell works like a click on its box."""
        if self.tree.identify_region(e.x, e.y) != "cell" or self.tree.identify_column(e.x) != "#1":
            return None
        iid = self.tree.identify_row(e.y)
        if iid:
            sel = self.tree.selection()
            self._toggle_rows(sel if iid in sel else (iid,))
        return "break"

    def _set_all(self, on):
        for i in self._visible():
            self.rows[i]["checked"] = on
        self._count()

    def _head_clicked(self):
        self._set_all(bool(self.v_head.get()))

    # --- converting -----------------------------------------------------------
    @staticmethod
    def _reason(why):
        out = []
        for w in why:
            if w == "no color texture":
                out.append(t("sc_why_color"))
            elif w.startswith("F_"):
                out.append(w)
            else:
                out.append(t("sc_why_shader", shader=w))
        return ", ".join(out)

    def convert(self):
        todo = [self.rows[i] for i in self._visible() if self.rows[i]["checked"]]
        if not todo:
            self.app._msg(t("t_shader_name"), t("sc_none_checked"), "info")
            return
        target = config.SHADER_CHOICES[max(self.target.current(), 0)] + ".vfx"
        alpha = self.v_alpha.get()
        done = []
        for r in todo:
            try:
                with open(r["path"], "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                if shaders.shader_of(text) == target:
                    r["status"], r["tag"] = t("sc_st_same"), "same"
                    continue
                new, why = shaders.convert(text, target, alphatest=alpha)
                if new is None:
                    r["status"], r["tag"] = t("sc_st_skip", why=self._reason(why)), "skip"
                    continue
                with open(r["path"], "w", encoding="utf-8", newline="\n") as f:
                    f.write(new)
                r["shader"], r["status"], r["tag"] = target, t("sc_st_done"), "done"
                done.append(r["path"])
            except OSError as e:
                r["status"], r["tag"] = str(e), "error"
        self.refresh()
        skipped = sum(r["tag"] == "skip" for r in todo)
        self.app.log(t("sc_log", n=len(done), skipped=skipped, shader=target[:-4]), "ok" if done else "warn")
        if done and self.v_compile.get():
            _compile(self.app, done, "shader_compile.txt", t("t_shader_name"))


def _compile(app, files, list_name, title):
    """Compiles content files of an addon into its game folder as a job of the app."""
    cs2 = app.cfg.get("cs2_dir", "")

    def job(ctx):
        valve = ValveTools(cs2, ctx.log, ctx.cancel)
        if not valve.has_resourcecompiler:
            ctx.log(t("ap_no_compiler"), "warn")
            return
        ctx.log(t("ap_compiling", n=len(files)), "head")
        lst = os.path.join(config.work_root(), list_name)
        os.makedirs(os.path.dirname(lst), exist_ok=True)
        ok, failed, _out = valve.compile_resources(files, lst)
        ctx.log(t("ap_compiled", n=len(files) - failed, failed=failed), "ok" if ok else "warn")

    app._start_job(job, title, None, indeterminate=True)


# =============================================================================
# Soundevent editor
# =============================================================================
def _mci(cmd):
    try:
        ctypes.windll.winmm.mciSendStringW(cmd, None, 0, None)
    except (AttributeError, OSError):
        pass


def play_sound(path):
    _mci("close cs2p_snd")
    _mci(f'open "{path}" type mpegvideo alias cs2p_snd')
    _mci("play cs2p_snd")


def stop_sound():
    _mci("close cs2p_snd")


# value when a property is turned on and no base gives one
_DEFAULTS = {"volume": 1.0, "pitch": 1.0, "type": "csgo_mega", "mixgroup": "Amb_Common",
             "position": [0.0, 0.0, 0.0], "retrigger_interval_min": 1.0, "retrigger_interval_max": 10.0,
             "randomize_position_min_radius": 500.0, "randomize_position_max_radius": 1000.0,
             "retrigger_radius": 10000.0, "reverb_wet": 1.0, "dsp_preset": "reverb_22_outsideOpen",
             "distance_volume_mapping_curve": se.curve_slopes([(0.0, 1.0), (1000.0, 0.0)]),
             "time_volume_mapping_curve": se.curve_slopes([(0.0, 0.0), (0.3, 1.0)]),
             "fadetime_volume_mapping_curve": se.curve_slopes([(0.0, 1.0), (0.2, 0.0)]),
             "distance_unfiltered_stereo_mapping_curve": se.curve_slopes([(0.0, 0.0), (1000.0, 0.0)])}
# spin steps
_STEP = {"delay": 0.5, "retrigger_interval_min": 0.5, "retrigger_interval_max": 0.5,
         "randomize_position_min_radius": 50, "randomize_position_max_radius": 50, "retrigger_radius": 100}


def _default(key):
    kind = se.PROP_KIND.get(key)
    if key in _DEFAULTS:
        return copy.deepcopy(_DEFAULTS[key])
    return {"float": 0.0, "bool": False, "files": [], "events": [], "vec3": [0.0, 0.0, 0.0]}.get(kind, "")


def _as_list(v):
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str) and x]
    return [v] if isinstance(v, str) and v else []


def _float(text):
    try:
        return float(str(text).strip().replace(",", "."))
    except ValueError:
        return None


class CurveEditor(tk.Canvas):
    """A curve of an event: click adds a point, drag moves it, right click removes it.
    Points are (x, y) with y from 0 to 1; x is distance or time up to the range field."""

    W, H, PAD = 380, 150, 26

    def __init__(self, master, rows, on_change, unit, enabled=True):
        super().__init__(master, width=self.W, height=self.H, bg=_c("FIELD"), highlightthickness=1,
                         highlightbackground=_c("BORDER"), bd=0)
        self.on_change, self.unit, self.enabled = on_change, unit, enabled
        self.pts = sorted((float(r[0]), float(r[1])) for r in rows if isinstance(r, list) and len(r) >= 2)
        top = max([p[0] for p in self.pts] + [0.0])
        self.xmax = top * 1.15 if top > 0 else (1.0 if unit == "seconds" else 1000.0)
        self.drag = None
        if enabled:
            self.bind("<Button-1>", self._press)
            self.bind("<B1-Motion>", self._move)
            self.bind("<ButtonRelease-1>", self._release)
            self.bind("<Button-3>", self._remove)
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self.delete("hover"))
        self.draw()

    def set_range(self, xmax):
        if xmax and xmax > 0:
            self.xmax = xmax
            self.draw()

    def _sx(self, x):
        return self.PAD + x / self.xmax * (self.W - 2 * self.PAD)

    def _sy(self, y):
        return self.H - self.PAD - y * (self.H - 2 * self.PAD)

    def _wx(self, sx):
        return max(0.0, (sx - self.PAD) / (self.W - 2 * self.PAD) * self.xmax)

    def _wy(self, sy):
        return min(1.0, max(0.0, (self.H - self.PAD - sy) / (self.H - 2 * self.PAD)))

    def draw(self):
        self.delete("all")
        muted, fg = _c("MUTED"), _c("ACCENT") if self.enabled else _c("MUTED")
        x0, x1, y0, y1 = self.PAD, self.W - self.PAD, self._sy(0), self._sy(1)
        self.create_rectangle(x0, y1, x1, y0, outline=_c("BORDER"))
        for f in (0.25, 0.5, 0.75):
            self.create_line(x0, self._sy(f), x1, self._sy(f), fill=_c("BORDER"), dash=(2, 3))
        self.create_text(x0 - 4, y1, text="1", anchor="e", fill=muted, font=("Segoe UI", 8))
        self.create_text(x0 - 4, y0, text="0", anchor="e", fill=muted, font=("Segoe UI", 8))
        self.create_text(x1, y0 + 4, text=f"{se.num(round(self.xmax, 3))} {t('se_unit_' + self.unit)}",
                         anchor="ne", fill=muted, font=("Segoe UI", 8))
        if self.pts:
            line = [(self._sx(0), self._sy(self.pts[0][1]))] if self.pts[0][0] > 0 else []
            line += [(self._sx(x), self._sy(y)) for x, y in self.pts]
            line.append((x1, self._sy(self.pts[-1][1])))
            if len(line) > 1:
                self.create_line(*[c for p in line for c in p], fill=fg, width=2)
            for x, y in self.pts:
                sx, sy = self._sx(x), self._sy(y)
                self.create_oval(sx - 4, sy - 4, sx + 4, sy + 4, fill=fg, outline="")

    def _near(self, e):
        for i, (x, y) in enumerate(self.pts):
            if abs(self._sx(x) - e.x) <= 7 and abs(self._sy(y) - e.y) <= 7:
                return i
        return None

    def _press(self, e):
        i = self._near(e)
        if i is None:
            self.pts.append((round(self._wx(e.x), 3), round(self._wy(e.y), 3)))
            self.pts.sort()
            i = self._near(e)
        self.drag = i
        self.draw()

    def _move(self, e):
        if self.drag is None:
            return
        self.pts[self.drag] = (round(self._wx(min(e.x, self.W - self.PAD)), 3), round(self._wy(e.y), 3))
        self.draw()
        self._hover(e)

    def _release(self, _e):
        if self.drag is not None:
            self.pts.sort()
            self.drag = None
            self.draw()
            self.on_change(se.curve_slopes(self.pts))

    def _remove(self, e):
        i = self._near(e)
        if i is not None and len(self.pts) > 1:
            del self.pts[i]
            self.draw()
            self.on_change(se.curve_slopes(self.pts))

    def _hover(self, e):
        self.delete("hover")
        if self.PAD <= e.x <= self.W - self.PAD:
            self.create_text(self.W - 4, 4, anchor="ne", tags="hover", fill=_c("FG"), font=("Segoe UI", 8),
                             text=f"{se.num(round(self._wx(e.x), 2))}, {se.num(round(self._wy(e.y), 2))}")


class SoundPanel(ttk.Frame):
    """Soundevents of an addon: the list of its events and every property of the open event.
    New events come from the add window, which lists the addon's own sounds first and the
    template's sounds below them."""

    def __init__(self, master, app):
        super().__init__(master, style="Inner.TFrame")
        self.app = app
        cs2 = app.cfg.get("cs2_dir", "")
        self.tmpl_names, self.dsp, self.tmpl_files = se.template_info(cs2) if cs2 else (set(), [], set())
        p = se.events_path(se.template_dir(cs2))
        try:
            self.tmpl_events = se.parse(se.read_text(p)) if os.path.isfile(p) else {}
        except se.KV3Error:
            self.tmpl_events = {}
        self.addon = ""
        self.cur = None
        self.editor_vars = []
        self.bar = AddonBar(self, app, self.load, "sound")
        self.bar.pack(fill="x", pady=(0, 8))

        act = ttk.Frame(self, style="Inner.TFrame")
        act.pack(side="bottom", fill="x", pady=(10, 0))
        self.btn_compile = ttk.Button(act, text=t("se_save"), style="Accent.TButton",
                                      command=lambda: self.save(compile_after=True))
        self.btn_compile.pack(side="left")
        _ui("ToolTip")(self.btn_compile, t("se_save_tip"))
        ttk.Button(act, text=t("se_revert"), style="Action.TButton", command=self._revert).pack(side="left", padx=(8, 0))
        if app.busy():
            self.btn_compile.config(state="disabled")
        self.lbl_state = ttk.Label(act, text="", style="InnerMuted.TLabel")
        self.lbl_state.pack(side="left", padx=(16, 0))

        body = ttk.Frame(self, style="Inner.TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)

        # events
        ttk.Label(body, text=t("se_events"), style="InnerSection.TLabel").grid(row=0, column=0, sticky="w")
        left = ttk.Frame(body, style="Inner.TFrame")
        left.grid(row=1, column=0, sticky="nsw")
        e1 = ttk.Frame(left, style="Inner.TFrame")
        e1.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(e1, text=t("se_add_sound"), style="Accent.TButton", command=self._add_sounds).pack(side="left")
        ttk.Button(e1, text=t("se_duplicate"), style="Action.TButton", command=self._duplicate).pack(side="left", padx=(6, 0))
        ttk.Button(e1, text=t("se_delete"), style="Danger.TButton", command=self._delete).pack(side="left", padx=(6, 0))
        wrap, self.ev = _tree(left, ("base",), {"base": t("se_base")}, {"base": (150, False)}, show="tree headings")
        self.ev.heading("#0", text=t("flcol_name"), anchor="w")
        self.ev.column("#0", width=230, stretch=True)
        wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.ev.tag_configure("tmpl", foreground=_c("MUTED"))
        self.ev.tag_configure("group", foreground=_c("ACCENT"))
        self.ev.tag_configure("changed", foreground=_c("WARN"))
        self.ev.bind("<<TreeviewSelect>>", lambda e: self._event_selected())
        self.ev.bind("<Delete>", lambda e: self._delete())
        wrap.drop_paths = self.ev.drop_paths = self._import_paths

        # the open event; with none open, a short hint and the add button stand in its place
        self.lbl_props = ttk.Label(body, text=t("se_props"), style="InnerSection.TLabel")
        self.lbl_props.grid(row=0, column=1, sticky="w", padx=(16, 0))
        # the sound of the open event, always in sight above its settings
        self.player = ttk.Frame(body, style="Inner.TFrame")
        self.player.grid(row=0, column=1, sticky="e")
        ttk.Label(self.player, text=t("se_sound"), style="Inner.TLabel").pack(side="left", padx=(0, 6))
        self.cb_play = ttk.Combobox(self.player, state="readonly", width=38)
        self.cb_play.pack(side="left")
        ttk.Button(self.player, text=t("se_play"), style="Action.TButton",
                   command=self._play_cur).pack(side="left", padx=(8, 0))
        ttk.Button(self.player, text=t("se_stop"), style="Action.TButton",
                   command=stop_sound).pack(side="left", padx=(6, 0))
        self.player.grid_remove()
        self.sf = _ui("ScrollFrame")(body, style="Inner.TFrame", bg=_c("CARD"))
        self.sf.bar_gap = 14
        self.sf.grid(row=1, column=1, sticky="nsew", padx=(16, 0), pady=(4, 0))
        self.empty = ttk.Frame(body, style="Inner.TFrame")
        self.empty.grid(row=1, column=1, sticky="nsew", padx=(16, 0), pady=(4, 0))
        hint = ttk.Frame(self.empty, style="Inner.TFrame")
        hint.place(relx=0.5, rely=0.4, anchor="center")
        ttk.Label(hint, text=t("se_empty_title"), style="InnerSection.TLabel").pack()
        ttk.Label(hint, text=t("se_empty_hint"), style="InnerMuted.TLabel", wraplength=360,
                  justify="center").pack(pady=(4, 10))
        ttk.Button(hint, text=t("se_add_sound"), style="Accent.TButton", command=self._add_sounds).pack()

        app.btn_tool = self.btn_compile
        self.drop_paths = self._import_paths
        self.work = None
        state = getattr(app, "_se_state", None)
        if state and state.get("addon") == self.bar.get():
            self.__dict__.update({k: v for k, v in state.items() if k != "addon"})
            self.addon = state["addon"]
            self._fill_events()
            self._show_editor()
            self._mark()
        else:
            self.load(self.bar.get(), True)
        self.bind("<Destroy>", self._gone, add="+")

    # --- state ----------------------------------------------------------------
    def _gone(self, e):
        if e.widget is self:
            stop_sound()
            self.app._se_state = {"addon": self.addon, "text": self.text, "events": self.events,
                                  "work": self.work, "origin": self.origin, "sound_files": self.sound_files,
                                  "cur": self.cur}

    def _changed_event(self, n):
        old = self.origin.get(n)
        return old != n or self.events.get(old) != self.work[n]

    def dirty(self):
        if self.work is None:
            return False
        return set(self.origin.values()) != set(self.events) or any(self._changed_event(n) for n in self.work)

    def _mark(self):
        self.lbl_state.config(text=t("se_unsaved") if self.dirty() else "")

    def load(self, addon, force=False):
        if not force and getattr(self, "work", None) is not None and self.dirty():
            if not self.app._ask(t("t_sound_name"), t("se_drop_changes")):
                return False
        self.addon = addon
        self.cdir = _content(self.app, addon) if addon else ""
        self.text, self.events = "", {}
        path = se.events_path(self.cdir) if self.cdir else ""
        if path and os.path.isfile(path):
            self.text = se.read_text(path)
            try:
                self.events = se.parse(self.text)
            except se.KV3Error as e:
                self.app._msg(t("t_sound_name"), t("se_bad_file", err=str(e)), "error")
                self.text, self.events = None, {}
        self.work = copy.deepcopy(self.events)
        self.origin = {n: n for n in self.events}
        self.sound_files = se.list_sound_files(self.cdir) if self.cdir else []
        self.cur = None
        self._fill_events()
        self._show_editor()
        self._mark()
        return True

    def _revert(self):
        if self.dirty() and self.app._ask(t("t_sound_name"), t("se_drop_changes")):
            self.load(self.addon, True)

    # --- adding sounds ----------------------------------------------------------
    def _pick_sounds(self, title, ok_text, kind=True, select=()):
        """A window that lists the sounds that can go into an event: the addon's own ones on top,
        the template's below. Sound files can be brought in from there too. Returns
        (sound paths, preset index) or None when it is closed."""
        if not self._need_file():
            return None
        top = tk.Toplevel(self)
        top.withdraw()
        top.title(title)
        top.configure(bg=_c("CARD"))
        top.transient(self.winfo_toplevel())
        top.minsize(460, 380)
        scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        pad = int(16 * scale)
        result = {}

        bottom = ttk.Frame(top, style="Inner.TFrame")
        bottom.pack(side="bottom", fill="x", padx=pad, pady=(8, pad))
        mid = ttk.Frame(top, style="Inner.TFrame")
        mid.pack(side="bottom", fill="x", padx=pad, pady=(8, 0))
        bar = ttk.Frame(top, style="Inner.TFrame")
        bar.pack(fill="x", padx=pad, pady=(pad, 6))
        wrap, tree = _tree(top, (), {}, {}, show="tree", height=14)
        wrap.pack(fill="both", expand=True, padx=pad)
        tree.column("#0", width=int(400 * scale), stretch=True)
        tree.tag_configure("group", foreground=_c("ACCENT"))
        tree.tag_configure("none", foreground=_c("MUTED"))

        def fill(chosen=()):
            tree.delete(*tree.get_children())
            own = [r for r, _p in self.sound_files if r.lower() not in self.tmpl_files]
            tmpl = [r for r, _p in self.sound_files if r.lower() in self.tmpl_files]
            g = tree.insert("", "end", iid="g:own", text=t("se_custom_sounds"), open=True, tags=("group",))
            for rel in own:
                tree.insert(g, "end", iid="s:" + rel, text=rel)
            if not own:
                tree.insert(g, "end", iid="none", text=t("se_no_sounds"), tags=("none",))
            if tmpl:
                g = tree.insert("", "end", iid="g:tmpl", text=t("se_default_sounds"), open=True, tags=("group",))
                for rel in tmpl:
                    tree.insert(g, "end", iid="s:" + rel, text=rel)
            keep = ["s:" + c for c in chosen if tree.exists("s:" + c)]
            if keep:
                tree.selection_set(keep)
                tree.see(keep[0])

        def chosen():
            return [i[2:] for i in tree.selection() if i.startswith("s:")]

        def play():
            rels = chosen()
            if rels:
                play_sound(os.path.join(self.cdir, "sounds", *rels[0].split("/")))

        def bring_in():
            ps = filedialog.askopenfilenames(parent=top, title=t("se_import"),
                                             filetypes=[(t("se_ft_sound"), "*.wav *.mp3"), (t("all_files"), "*.*")])
            if ps:
                fill(self._copy_sounds(list(ps)))

        ttk.Button(bar, text=t("se_import"), command=bring_in).pack(side="left")
        ttk.Button(bar, text=t("se_play"), command=play).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text=t("se_stop"), command=stop_sound).pack(side="left", padx=(6, 0))
        ttk.Label(bar, text=t("se_pick_hint"), style="InnerMuted.TLabel").pack(side="right")

        preset = None
        if kind:
            ttk.Label(mid, text=t("se_kind"), style="Inner.TLabel").pack(side="left")
            preset = ttk.Combobox(mid, state="readonly", width=34, values=[t(k) for _b, k in se.PRESETS])
            preset.current(0)
            preset.pack(side="left", padx=(6, 0))
            desc = ttk.Label(top, text="", style="InnerMuted.TLabel", wraplength=int(420 * scale), justify="left")
            desc.pack(side="bottom", fill="x", padx=pad, pady=(4, 0), before=mid)

            def show_desc(_e=None):
                desc.config(text=t(se.PRESETS[max(preset.current(), 0)][1] + "_desc"))
            preset.bind("<<ComboboxSelected>>", show_desc)
            show_desc()

        def done():
            rels = chosen()
            if not rels:
                self.app._msg(title, t("se_pick_sound"), "info")
                return
            result["v"] = (rels, preset.current() if preset is not None else 0)
            close()

        def close():
            stop_sound()
            try:
                top.grab_release()
            except tk.TclError:
                pass
            top.destroy()

        ttk.Button(bottom, text=t("dlg_cancel"), command=close).pack(side="right")
        ttk.Button(bottom, text=ok_text, style="Accent.TButton", command=done).pack(side="right", padx=(0, 8))
        tree.bind("<Double-1>", lambda e: play() if tree.identify_row(e.y).startswith("s:") else None)
        tree.bind("<Return>", lambda e: done())
        top.bind("<Escape>", lambda e: close())
        top.protocol("WM_DELETE_WINDOW", close)
        wrap.drop_paths = tree.drop_paths = lambda ps: fill(self._copy_sounds(ps))
        fill(select)

        top.update_idletasks()
        w, h = max(top.winfo_reqwidth(), int(480 * scale)), max(top.winfo_reqheight(), int(460 * scale))
        root = self.winfo_toplevel()
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 3
        top.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        top.deiconify()
        try:
            top.grab_set()
        except tk.TclError:
            pass
        tree.focus_set()
        self.wait_window(top)
        return result.get("v")

    def _used(self):
        used = {}
        for d in self.work.values():
            for v in _as_list(d.get("vsnd_files_track_01")):
                used[v.lower()] = used.get(v.lower(), 0) + 1
        return used

    def _copy_sounds(self, paths):
        """Copies sound files (or the ones in dropped folders) into the addon's sounds folder.
        Returns their names there."""
        files = []
        for p in paths:
            if os.path.isdir(p):
                for d, _s, fs in os.walk(p):
                    files += [os.path.join(d, f) for f in fs if f.lower().endswith(se.SOUND_EXTS)]
            elif p.lower().endswith(se.SOUND_EXTS):
                files.append(p)
        dst_dir = os.path.join(self.cdir, "sounds")
        os.makedirs(dst_dir, exist_ok=True)
        added = []
        for f in files:
            name = os.path.basename(f).lower().replace(" ", "_")
            dst = os.path.join(dst_dir, name)
            if os.path.normcase(os.path.abspath(f)) != os.path.normcase(os.path.abspath(dst)):
                if not os.path.isfile(dst):
                    shutil.copy2(f, dst)
            added.append(name)
        self.sound_files = se.list_sound_files(self.cdir)
        if added:
            self.lbl_state.config(text=t("se_imported", n=len(added)))
        return added

    def _import_paths(self, paths):
        """Dropped sound files: copied into the addon, then the add window opens with them picked."""
        if not self.cdir:
            self.app._msg(t("t_sound_name"), t("ap_pick_addon"), "info")
            return
        if self._need_file():
            self._add_sounds(self._copy_sounds(paths))

    def _add_sounds(self, select=()):
        """Each picked sound becomes a new event of the picked kind, and the last one opens."""
        got = self._pick_sounds(t("se_add_title"), t("se_add"), True, select)
        if not got:
            return
        rels, idx = got
        base = se.PRESETS[max(idx, 0)][0]
        last = None
        for rel in rels:
            name = se.event_name(os.path.splitext(os.path.basename(rel))[0], set(self.work) | self.tmpl_names)
            self.work[name] = {"base": base, "volume": 1.0, "vsnd_files_track_01": se.vsnd_of(rel)}
            last = name
        self._select(last)

    # --- event list -----------------------------------------------------------
    def _fill_events(self):
        self.ev.delete(*self.ev.get_children())
        own = [n for n in self.work if n not in self.tmpl_names]
        tmpl = [n for n in self.work if n in self.tmpl_names]
        for n in own:
            changed = self._changed_event(n)
            self.ev.insert("", "end", iid="e:" + n, text=n, values=(self.work[n].get("base", ""),),
                           tags=("changed",) if changed else ())
        if not own:
            self.ev.insert("", "end", iid="none", text=t("se_no_events"), tags=("tmpl",))
        if tmpl:
            g = self.ev.insert("", "end", iid="g:tmpl", text=t("se_template_events"), open=False, tags=("group",))
            for n in tmpl:
                self.ev.insert(g, "end", iid="e:" + n, text=n, values=(self.work[n].get("base", ""),),
                               tags=("tmpl",))
        if self.cur and self.ev.exists("e:" + self.cur):
            self.ev.selection_set("e:" + self.cur)
            self.ev.see("e:" + self.cur)

    def _event_selected(self):
        sel = [i for i in self.ev.selection() if i.startswith("e:")]
        name = sel[0][2:] if sel else None
        if name != self.cur:
            self.cur = name
            self._show_editor()

    def _select(self, name):
        self.cur = name
        self._fill_events()
        self._show_editor()
        self._mark()

    def _need_file(self):
        if not self.cdir:
            self.app._msg(t("t_sound_name"), t("ap_pick_addon"), "info")
            return False
        if self.text is None:
            self.app._msg(t("t_sound_name"), t("se_bad_file", err=""), "error")
            return False
        return True

    def _duplicate(self):
        if not self.cur:
            return
        name = se.event_name(self.cur + "_copy", set(self.work) | self.tmpl_names)
        self.work[name] = copy.deepcopy(self.work[self.cur])
        self._select(name)

    def _delete(self):
        if not self.cur:
            return
        users = [n for n, d in self.work.items() if d.get("base") == self.cur or self.cur in _as_list(
            d.get("soundevent_01"))]
        text = t("se_delete_ask", name=self.cur)
        if users:
            text += "\n\n" + t("se_delete_used", names=", ".join(users[:8]))
        if not self.app._ask(t("se_delete"), text, "warning"):
            return
        self.work.pop(self.cur, None)
        self.origin.pop(self.cur, None)
        self._select(None)

    # --- saving ----------------------------------------------------------------
    def _bases_missing(self, text_events):
        """Template bases the events use that the file does not have (with their own bases)."""
        need, todo = [], [d.get("base") for d in self.work.values()]
        while todo:
            b = todo.pop()
            if isinstance(b, str) and b and b not in self.work and b not in text_events and b not in need \
                    and b in self.tmpl_events:
                need.append(b)
                todo.append(self.tmpl_events[b].get("base"))
        return need

    def save(self, compile_after=False):
        if not self._need_file():
            return
        self._commit_name()
        text = self.text or ""
        if not text.strip():
            tp = se.events_path(se.template_dir(self.app.cfg.get("cs2_dir", "")))
            text = se.read_text(tp) if os.path.isfile(tp) else ""
            base_events = se.parse(text) if text else {}
            # a new file keeps only the template's bases, not its example events
            for n in list(base_events):
                if n.startswith("ambient_example."):
                    text = se.remove_event(text, n)
        kept = set(self.origin.values())
        for old in self.events:
            if old not in kept:
                text = se.remove_event(text, old)
        for name, data in self.work.items():
            old = self.origin.get(name)
            if old is None or old != name or self.events.get(old) != data:
                text = se.set_event(text, name, data, old)
        for b in self._bases_missing(se.parse(text) if text.strip() else {}):
            text = se.set_event(text, b, self.tmpl_events[b])
        try:
            se.parse(text)
        except se.KV3Error as e:
            self.app._msg(t("t_sound_name"), t("se_bad_file", err=str(e)), "error")
            return
        path = se.events_path(self.cdir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        cur = self.cur
        n = len(self.work)
        self.load(self.addon, True)
        self._select(cur if cur in self.work else None)
        self.lbl_state.config(text=t("se_saved", n=n))
        self.app.log(t("se_saved_log", path=path), "ok")
        if compile_after:
            used = self._used()
            files = [path] + [p for r, p in self.sound_files if used.get(se.vsnd_of(r).lower())]
            _compile(self.app, files, "soundevents_compile.txt", t("t_sound_name"))

    # --- property editor ---------------------------------------------------------
    def _commit_name(self):
        v = getattr(self, "v_name", None)
        if v is None or not self.cur:
            return
        new = v.get().strip()
        if not new or new == self.cur:
            return
        if new in self.work:
            self.app._msg(t("t_sound_name"), t("se_name_taken", name=new), "warning")
            v.set(self.cur)
            return
        old = self.cur
        self.work = {(new if k == old else k): d for k, d in self.work.items()}
        if old in self.origin:
            self.origin[new] = self.origin.pop(old)
        for d in self.work.values():
            if d.get("base") == old:
                d["base"] = new
            kids = d.get("soundevent_01")
            if isinstance(kids, list) and old in kids:
                d["soundevent_01"] = [new if k == old else k for k in kids]
            elif kids == old:
                d["soundevent_01"] = new
        self.cur = new
        self._fill_events()
        self._mark()

    def _show_code(self):
        """The open event as it is written into soundevents.vsndevts (with unsaved changes)."""
        if not self.cur or self.cur not in self.work:
            return
        self._commit_name()
        code = se.dump_event(self.cur, self.work[self.cur]).replace("\t", "    ")
        top = tk.Toplevel(self)
        top.withdraw()
        top.title(t("se_code_title", name=self.cur))
        top.configure(bg=_c("CARD"))
        top.transient(self.winfo_toplevel())
        scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        pad = int(16 * scale)
        bottom = ttk.Frame(top, style="Inner.TFrame")
        bottom.pack(side="bottom", fill="x", padx=pad, pady=(8, pad))
        ttk.Label(top, text=t("se_code_hint"), style="InnerMuted.TLabel", wraplength=int(560 * scale),
                  justify="left").pack(anchor="w", padx=pad, pady=(pad, 6))
        box = ttk.Frame(top, style="Inner.TFrame")
        box.pack(fill="both", expand=True, padx=pad)
        ys = ttk.Scrollbar(box, orient="vertical")
        ys.pack(side="right", fill="y")
        txt = tk.Text(box, wrap="none", font=("Consolas", 10), bg=_c("FIELD"), fg=_c("FG"),
                      insertbackground=_c("FG"), relief="flat", highlightthickness=1,
                      highlightbackground=_c("BORDER"), highlightcolor=_c("BORDER"),
                      padx=8, pady=6, yscrollcommand=ys.set, height=min(28, code.count("\n") + 2), width=72)
        ys.configure(command=txt.yview)
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", code)
        txt.configure(state="disabled")

        def copy_all():
            top.clipboard_clear()
            top.clipboard_append(code)
            btn_copy.configure(text=t("se_code_copied"))

        def close():
            try:
                top.grab_release()
            except tk.TclError:
                pass
            top.destroy()
        ttk.Button(bottom, text=t("se_close"), style="Accent.TButton", command=close).pack(side="right")
        btn_copy = ttk.Button(bottom, text=t("se_copy"), style="Action.TButton", command=copy_all)
        btn_copy.pack(side="right", padx=(0, 8))
        top.bind("<Escape>", lambda e: close())
        top.bind("<Return>", lambda e: close())
        top.protocol("WM_DELETE_WINDOW", close)
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        root = self.winfo_toplevel()
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 3
        top.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        top.deiconify()
        try:
            top.grab_set()
        except tk.TclError:
            pass
        self.wait_window(top)

    def _all_events(self):
        merged = dict(self.tmpl_events)
        merged.update(self.work)
        return merged

    def _changed(self, rebuild=False):
        self._mark()
        if self.cur and self.ev.exists("e:" + self.cur):
            d = self.work[self.cur]
            self.ev.item("e:" + self.cur, values=(d.get("base", ""),),
                         tags=("tmpl",) if self.cur in self.tmpl_names else ("changed",))
        self._update_player()
        if rebuild:
            self._show_editor()

    def _cur_files(self):
        """Sound files of the open event (its own, or the ones it gets from its base)."""
        d = self.work[self.cur]
        v = d.get("vsnd_files_track_01")
        if v is None:
            v, _src = self._inherited("vsnd_files_track_01", self._all_events())
        return [x for x in _as_list(v) if isinstance(x, str) and x]

    def _update_player(self):
        if not self.cur or self.work is None or self.cur not in self.work:
            self.player.grid_remove()
            return
        files = self._cur_files()
        self.cb_play.configure(values=files)
        if self.cb_play.get() not in files:
            self.cb_play.set(files[0] if files else t("se_no_sound_file"))
        self.player.grid()

    def _play_cur(self):
        v = self.cb_play.get().lower()
        path = next((p for r, p in self.sound_files if se.vsnd_of(r).lower() == v), None)
        if not path:    # a sound of the template the addon has no copy of
            tdir = se.template_dir(self.app.cfg.get("cs2_dir", ""))
            path = next((p for r, p in se.list_sound_files(tdir) if se.vsnd_of(r).lower() == v), None)
        if path and os.path.isfile(path):
            play_sound(path)
        else:
            self.lbl_state.config(text=t("se_sound_missing"))

    def _show_editor(self):
        y = self.sf.canvas.yview()[0] if self.sf.winfo_exists() else 0
        for w in self.sf.inner.winfo_children():
            w.destroy()
        self.v_name = None
        inner = self.sf.inner
        if not self.cur or self.cur not in self.work:
            self.empty.tkraise()
            self.lbl_props.grid_remove()
            self._update_player()
            return
        self.sf.tkraise()
        self.lbl_props.grid()
        self._update_player()
        d = self.work[self.cur]
        top = ttk.Frame(inner, style="Inner.TFrame")
        top.pack(fill="x", pady=(2, 6))
        ttk.Label(top, text=t("se_name"), style="Inner.TLabel").pack(side="left")
        self.v_name = tk.StringVar(value=self.cur)
        e = ttk.Entry(top, textvariable=self.v_name, width=40)
        e.pack(side="left", padx=(6, 0), fill="x", expand=True)
        e.bind("<Return>", lambda _e: self._commit_name())
        e.bind("<FocusOut>", lambda _e: self._commit_name())
        ttk.Button(top, text=t("se_code"), style="Action.TButton", command=self._show_code).pack(
            side="left", padx=(8, 0))
        ttk.Label(inner, text=t("se_inherit_hint"), style="InnerMuted.TLabel", wraplength=520,
                  justify="left").pack(anchor="w", pady=(0, 4))
        events = self._all_events()
        for g in se.GROUPS:
            ttk.Label(inner, text=t("se_group_" + g), style="InnerSection.TLabel").pack(anchor="w", pady=(10, 2))
            grid = ttk.Frame(inner, style="Inner.TFrame")
            grid.pack(fill="x")
            grid.columnconfigure(1, weight=1)
            r = 0
            for key, kind, group in se.PROPS:
                if group != g:
                    continue
                self._prop_row(grid, r, key, kind, d, events)
                r += 1
        self._other_rows(inner, d)
        self._wheel_scrolls(inner)
        self.sf.update_idletasks()
        self.sf.canvas.yview_moveto(y)

    def _wheel_scrolls(self, w):
        """Spin boxes and lists change their value on the mouse wheel. In the settings the wheel
        scrolls the page instead, so a field that comes under the pointer is not changed."""
        if isinstance(w, (ttk.Spinbox, ttk.Combobox)):
            w.bind("<MouseWheel>", lambda e: (self.sf._wheel(e), "break")[1])
        for c in w.winfo_children():
            self._wheel_scrolls(c)

    def _inherited(self, key, events):
        base = self.work[self.cur].get("base")
        if not isinstance(base, str) or not base:
            return None, None
        return se.resolve(events, base, key, {self.cur})

    def _prop_row(self, grid, r, key, kind, d, events):
        is_set = key in d
        inh, src = self._inherited(key, events)
        value = d[key] if is_set else inh
        on = tk.BooleanVar(value=is_set)
        label = t("sp_" + key)
        cb = ttk.Checkbutton(grid, text=label, variable=on, style="Inner.TCheckbutton")
        cb.grid(row=r, column=0, sticky="nw", padx=(0, 10), pady=2)
        tip = key + ("" if is_set or src is None else "\n" + t("se_from_base", base=src))
        _ui("ToolTip")(cb, tip)
        cell = ttk.Frame(grid, style="Inner.TFrame")
        cell.grid(row=r, column=1, sticky="we", pady=2)

        def redraw():
            self._changed()
            cb.destroy()
            cell.destroy()
            self._prop_row(grid, r, key, kind, d, events)
            self._wheel_scrolls(grid)

        def setv(v, rebuild=False):
            d[key] = v
            if key == "base":
                # the base goes first, like in the template
                items = [(k, x) for k, x in d.items() if k != "base"]
                d.clear()
                d["base"] = v
                d.update(items)
            if not on.get():
                on.set(True)
            if rebuild and key != "base":
                grid.after_idle(redraw)
            else:
                self._changed(rebuild)

        def toggle():
            if on.get():
                d[key] = copy.deepcopy(inh) if inh is not None else _default(key)
            else:
                d.pop(key, None)
            if key == "base":
                self._changed(True)     # every inherited value changes with it
            else:
                redraw()
        cb.configure(command=toggle)
        muted = not is_set
        if kind in ("float",):
            v = tk.StringVar(value=se.num(value) if isinstance(value, (int, float)) else "")
            sp = ttk.Spinbox(cell, textvariable=v, from_=-1e6, to=1e6, increment=_STEP.get(key, 0.05), width=12)
            sp.pack(side="left")

            def fl(*_a, v=v):
                x = _float(v.get())
                if x is not None and (not on.get() or d.get(key) != x):
                    if on.get() or x != inh:
                        setv(x)
            v.trace_add("write", fl)
            self._hint(cell, muted, src)
        elif kind == "bool":
            v = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(cell, text=t("se_on"), variable=v, style="Inner.TCheckbutton",
                            command=lambda v=v: setv(bool(v.get()))).pack(side="left")
            self._hint(cell, muted, src)
        elif kind in ("choice", "base"):
            if kind == "base":
                vals = sorted((n for n in events if n != self.cur), key=lambda n: (n not in self.tmpl_names, n.lower()))
            elif key == "dsp_preset":
                vals = self.dsp
            else:
                vals = list(se.CHOICES.get(key, ()))
            v = tk.StringVar(value=value if isinstance(value, str) else "")
            line = ttk.Frame(cell, style="Inner.TFrame")
            line.pack(fill="x")
            box = ttk.Combobox(line, textvariable=v, values=vals, width=36)
            box.pack(side="left", fill="x", expand=True)
            box.bind("<<ComboboxSelected>>", lambda e, v=v: setv(v.get().strip(), key == "base"))
            box.bind("<FocusOut>", lambda e, v=v: (v.get().strip() != (d.get(key) if on.get() else value)) and
                     v.get().strip() and setv(v.get().strip(), key == "base"))
            box.bind("<Return>", lambda e, v=v: v.get().strip() and setv(v.get().strip(), key == "base"))
            if kind == "base" and isinstance(value, str):
                desc = next((t(k + "_desc") for b, k in se.PRESETS if b == value), "")
                if desc:
                    ttk.Label(cell, text=desc, style="InnerMuted.TLabel", wraplength=480,
                              justify="left").pack(anchor="w", pady=(2, 0))
            self._hint(line, muted, src)
        elif kind == "vec3":
            vals = value if isinstance(value, list) and len(value) == 3 else [0, 0, 0]
            vs = [tk.StringVar(value=se.num(x)) for x in vals]
            for i, (ax, v) in enumerate(zip("XYZ", vs)):
                ttk.Label(cell, text=ax, style="InnerMuted.TLabel").pack(side="left", padx=(0 if i == 0 else 8, 3))
                ttk.Spinbox(cell, textvariable=v, from_=-1e6, to=1e6, increment=16, width=10).pack(side="left")

            def vec(*_a, vs=vs):
                xs = [_float(v.get()) for v in vs]
                if None not in xs and (on.get() or xs != vals):
                    setv(xs)
            for v in vs:
                v.trace_add("write", vec)
            self._hint(cell, muted, src)
        elif kind in ("files", "events"):
            self._list_cell(cell, key, kind, _as_list(value), setv, muted, src, events)
        elif kind == "curve":
            rows = value if isinstance(value, list) else []
            cv = CurveEditor(cell, rows, lambda rows: setv(rows), se.CURVE_X.get(key, "units"))
            cv.pack(side="left", anchor="w")
            side = ttk.Frame(cell, style="Inner.TFrame")
            side.pack(side="left", anchor="n", padx=(8, 0))
            ttk.Label(side, text=t("se_range"), style="InnerMuted.TLabel").pack(anchor="w")
            rv = tk.StringVar(value=se.num(round(cv.xmax, 3)))
            sp = ttk.Spinbox(side, textvariable=rv, from_=0.01, to=1e6,
                             increment=0.1 if cv.unit == "seconds" else 100, width=9)
            sp.pack(anchor="w")
            rv.trace_add("write", lambda *a, rv=rv, cv=cv: cv.set_range(_float(rv.get())))
            ttk.Label(side, text=t("se_curve_hint"), style="InnerMuted.TLabel", wraplength=150,
                      justify="left").pack(anchor="w", pady=(6, 0))
            if muted and src:
                ttk.Label(side, text=t("se_from_base", base=src), style="InnerMuted.TLabel", wraplength=150,
                          justify="left").pack(anchor="w", pady=(6, 0))

    @staticmethod
    def _hint(cell, muted, src):
        if muted:
            ttk.Label(cell, text=t("se_from_base", base=src) if src else t("se_not_set"),
                      style="InnerMuted.TLabel").pack(side="left", padx=(10, 0))

    def _list_cell(self, cell, key, kind, items, setv, muted, src, events):
        box = tk.Listbox(cell, height=max(3, min(len(items), 8)), bg=_c("FIELD"), fg=_c("MUTED") if muted else _c("FG"),
                         selectbackground=_c("ROW_SEL"), selectforeground=_c("FG"), highlightthickness=1,
                         highlightbackground=_c("BORDER"), bd=0, activestyle="none", exportselection=False)
        for x in items:
            box.insert("end", x)
        box.pack(fill="x", expand=True)
        row = ttk.Frame(cell, style="Inner.TFrame")
        row.pack(fill="x", pady=(4, 0))

        def store(new):
            if kind == "files":
                setv(new[0] if len(new) == 1 else new, True)
            else:
                setv(new if len(new) != 1 else new, True)

        def remove():
            idx = set(box.curselection())
            store([x for i, x in enumerate(items) if i not in idx])

        def move(step):
            idx = box.curselection()
            if len(idx) != 1:
                return
            i, j = idx[0], idx[0] + step
            if 0 <= j < len(items):
                new = list(items)
                new[i], new[j] = new[j], new[i]
                store(new)

        if kind == "files":
            def pick():
                got = self._pick_sounds(t("se_add_title"), t("se_add"), kind=False)
                if got:
                    store(items + [se.vsnd_of(r) for r in got[0] if se.vsnd_of(r) not in items])
            ttk.Button(row, text=t("se_add_sound"), style="Small.TButton", command=pick).pack(side="left")
            pv = tk.StringVar()
            ent = ttk.Entry(row, textvariable=pv, width=22)
            ent.pack(side="left", padx=(6, 0), fill="x", expand=True)
            _ui("ToolTip")(ent, t("se_path_tip"))

            def add_path():
                p = pv.get().strip().replace("\\", "/")
                if p:
                    if not p.lower().endswith(".vsnd"):
                        p = se.vsnd_of(p[7:] if p.lower().startswith("sounds/") else p)
                    store(items + [p])
            ttk.Button(row, text=t("se_add"), style="Small.TButton", command=add_path).pack(side="left", padx=(4, 0))
        else:
            ev = ttk.Combobox(row, values=[n for n in events if n != self.cur], width=28)
            ev.pack(side="left", fill="x", expand=True)
            ttk.Button(row, text=t("se_add"), style="Small.TButton",
                       command=lambda: ev.get().strip() and store(items + [ev.get().strip()])).pack(
                side="left", padx=(4, 0))
        ttk.Button(row, text="↑", style="Small.TButton", width=3, command=lambda: move(-1)).pack(
            side="left", padx=(6, 0))
        ttk.Button(row, text="↓", style="Small.TButton", width=3, command=lambda: move(1)).pack(
            side="left", padx=(2, 0))
        ttk.Button(row, text=t("se_remove"), style="Small.TButton", command=remove).pack(side="left", padx=(6, 0))
        if muted:
            ttk.Label(cell, text=t("se_from_base", base=src) if src else t("se_not_set"),
                      style="InnerMuted.TLabel").pack(anchor="w")

    def _other_rows(self, inner, d):
        """Keys the editor has no field for, as KV3 text."""
        other = {k: v for k, v in d.items() if k not in se.PROP_KIND}
        ttk.Label(inner, text=t("se_group_other"), style="InnerSection.TLabel").pack(anchor="w", pady=(10, 2))
        ttk.Label(inner, text=t("se_other_hint"), style="InnerMuted.TLabel", wraplength=520,
                  justify="left").pack(anchor="w")
        txt = tk.Text(inner, height=max(3, min(14, 2 + sum(len(se._pair(k, v, "")) for k, v in other.items()))),
                      bg=_c("FIELD"), fg=_c("FG"), insertbackground=_c("FG"), highlightthickness=1,
                      highlightbackground=_c("BORDER"), bd=0, font=("Consolas", 9), wrap="none", undo=True)
        txt.pack(fill="x", pady=(4, 0))
        txt.insert("1.0", "\n".join(line for k, v in other.items() for line in se._pair(k, v, "")))
        err = ttk.Label(inner, text="", style="InnerMuted.TLabel", foreground=_c("ERR"))
        err.pack(anchor="w")

        def commit(_e=None):
            try:
                new = se._Parser("{\n" + txt.get("1.0", "end") + "\n}").value()
            except se.KV3Error as e:
                err.config(text=t("se_other_bad", err=str(e)))
                return
            err.config(text="")
            bad = [k for k in new if k in se.PROP_KIND]
            if new == other and not bad:
                return
            for k in other:
                d.pop(k, None)
            d.update(new)
            self._changed(bool(bad))
        txt.bind("<FocusOut>", commit)
