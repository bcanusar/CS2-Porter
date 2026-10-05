"""CS2 Porter - graphical interface."""

import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
import tkinter as tk
import webbrowser
from tkinter import filedialog, ttk

from . import __version__, bsp as bspmod, config, i18n, pipeline, tools
from .i18n import t
from .valve_tools import Cancelled, ToolError

# --- colors ---------------------------------------------------------------
# set_theme() copies one of these palettes into the module names below
THEMES = {
    "dark": {
        "BG": "#1b1c1f", "PANEL": "#25272b", "CARD": "#2c2f34", "FIELD": "#1f2124", "BORDER": "#3a3d43",
        "FG": "#e7e8ea", "MUTED": "#9a9fa6", "ACCENT": "#f0a53a", "ACCENT_HI": "#ffbb57",
        "ACCENT_DARK": "#c9831f", "OK": "#6cc47a", "WARN": "#e8b84a", "ERR": "#ef6f6f", "INFO": "#cfd3d8",
        "TOOL": "#8aa4c0", "TIP_BG": "#3b3e45", "FOOTER": "#6f747b", "CHIP_OK": "#24382a", "CHIP_BAD": "#3d2626",
        "HOVER": "#373a40", "DISABLED": "#666a70", "ACCENT_OFF": "#5a4a32", "ACCENT_OFF_FG": "#8d8476",
        "DANGER": "#4a2a2a", "DANGER_FG": "#ffd5d5", "DANGER_BORDER": "#6a3636", "DANGER_HOVER": "#5c3030",
        "NAV_HOVER": "#2f3237", "TAB_HOVER": "#232528", "ROW_SEL": "#3d3a33", "HEAD_HOVER": "#34373d",
        "LOG_TIME": "#5d6168", "ON_ACCENT": "#17181a",
    },
    "light": {
        "BG": "#e9ebee", "PANEL": "#f8f9fa", "CARD": "#eef0f2", "FIELD": "#ffffff", "BORDER": "#c9cdd3",
        "FG": "#1f2226", "MUTED": "#666c74", "ACCENT": "#e3901f", "ACCENT_HI": "#f0a53a",
        "ACCENT_DARK": "#b8720f", "OK": "#2c8a45", "WARN": "#a5760a", "ERR": "#c83a3a", "INFO": "#2a2e34",
        "TOOL": "#3d6189", "TIP_BG": "#ffffff", "FOOTER": "#868c94", "CHIP_OK": "#dcefe1", "CHIP_BAD": "#f6dddd",
        "HOVER": "#e1e4e8", "DISABLED": "#a3a8ae", "ACCENT_OFF": "#ead8bd", "ACCENT_OFF_FG": "#a0927c",
        "DANGER": "#f5dddd", "DANGER_FG": "#8c2020", "DANGER_BORDER": "#e0b3b3", "DANGER_HOVER": "#efcaca",
        "NAV_HOVER": "#e6e8eb", "TAB_HOVER": "#dfe2e6", "ROW_SEL": "#f5e2c4", "HEAD_HOVER": "#e2e5e9",
        "LOG_TIME": "#9ba1a8", "ON_ACCENT": "#17181a",
    },
}
STATUS_COLORS = {}


def set_theme(name):
    globals().update(THEMES.get(name) or THEMES["dark"])
    STATUS_COLORS.update({
        "created": OK, "exists": MUTED, "cs2": MUTED, "tool": MUTED, "skip": MUTED,
        "missing": ERR, "error": ERR, "unsupported": WARN, "ok": OK, "found": INFO,
    })


set_theme("dark")


def dark_title_bar(win, dark):
    """Windows 10/11 draws the title bar dark or light by this window attribute."""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = int(win.wm_frame(), 16)
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):               # DWMWA_USE_IMMERSIVE_DARK_MODE (19 on old Windows 10 builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), 4) == 0:
                break
        # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED: redraw the frame now
        ctypes.windll.user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x27)
    except (AttributeError, OSError, ValueError, tk.TclError):
        pass


def flash_taskbar(win, stop=False):
    """Lights the window's taskbar button up (orange, without blinking) until the window is
    brought to the front; nothing when it already is. stop: turns it off."""
    try:
        import ctypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p), ("dwFlags", ctypes.c_uint),
                        ("uCount", ctypes.c_uint), ("dwTimeout", ctypes.c_uint)]

        hwnd = int(win.wm_frame(), 16)
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        if stop:
            # FLASHW_STOP
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0, 0, 0)
        elif user32.GetForegroundWindow() == hwnd:
            return
        else:
            # FLASHW_TRAY once: after the one flash the button stays orange
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x2, 1, 0)
        user32.FlashWindowEx(ctypes.byref(info))
    except (AttributeError, OSError, ValueError, tk.TclError):
        pass


def _enable_dpi():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


def _human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def open_in_explorer(path):
    if not path:
        return
    if os.path.isfile(path):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif os.path.isdir(path):
        os.startfile(path)  # noqa: S606


class _PopupMenu(tk.Menu):
    """Windows draws disabled menu items embossed (with a white shadow): items added with
    state="disabled" stay enabled instead, in a pale color, without a hover color and with no
    command."""

    def add_command(self, cnf=None, **kw):
        if kw.get("state") == "disabled":
            kw.update(state="normal", command=lambda: None, foreground=MUTED, activeforeground=MUTED,
                      activebackground=PANEL)
        super().add_command(cnf or {}, **kw)


def popup_menu(parent):
    """A right click menu in the program's colors."""
    return _PopupMenu(parent, tearoff=0, bg=PANEL, fg=FG, activebackground=ROW_SEL, activeforeground=FG,
                      disabledforeground=MUTED, relief="flat", bd=1)


def copy_text(widget, text):
    widget.clipboard_clear()
    widget.clipboard_append(text)


def entry_menu(e):
    """Right click on a text field: cut, copy, paste, select all, clear."""
    w = e.widget
    try:
        if w.winfo_class() not in ("TEntry", "Entry", "TCombobox"):
            return
        readonly = str(w.cget("state")) in ("readonly", "disabled")
        try:
            has_sel = bool(w.selection_present())
        except (AttributeError, tk.TclError):
            has_sel = False
        try:
            clip = bool(w.clipboard_get())
        except tk.TclError:
            clip = False
    except tk.TclError:
        return
    w.focus_set()
    menu = popup_menu(w)
    menu.add_command(label=t("menu_cut"), command=lambda: w.event_generate("<<Cut>>"),
                     state="normal" if has_sel and not readonly else "disabled")
    menu.add_command(label=t("menu_copy"), command=lambda: w.event_generate("<<Copy>>"),
                     state="normal" if has_sel else "disabled")
    menu.add_command(label=t("menu_paste"), command=lambda: w.event_generate("<<Paste>>"),
                     state="normal" if clip and not readonly else "disabled")
    menu.add_separator()
    menu.add_command(label=t("menu_select_all"), command=lambda: w.selection_range(0, "end"))
    menu.add_command(label=t("menu_copy_all"), command=lambda: copy_text(w, w.get()),
                     state="normal" if w.get() else "disabled")
    if not readonly:
        menu.add_command(label=t("menu_clear"), command=lambda: w.delete(0, "end"),
                         state="normal" if w.get() else "disabled")
    try:
        menu.tk_popup(e.x_root, e.y_root)
    finally:
        menu.grab_release()


# ---------------------------------------------------------------------------
# Helper widgets
# ---------------------------------------------------------------------------

class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _e=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", bg=TIP_BG, fg=FG, padx=8, pady=5, wraplength=380,
                 font=("Segoe UI", 9), highlightthickness=1, highlightbackground=BORDER).pack()

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Dialog:
    """Message and yes / no boxes in the program's colors (the Windows ones are always white)."""

    ICONS = {"info": ("i", "ACCENT"), "warning": ("!", "WARN"), "error": ("×", "ERR"), "question": ("?", "ACCENT")}

    def __init__(self, parent, title, text, kind="info", buttons=("ok",), dark=True):
        self.result = None
        self.top = top = tk.Toplevel(parent)
        top.withdraw()
        top.title(title)
        top.configure(bg=PANEL)
        top.resizable(False, False)
        top.transient(parent)
        scale = max(1.0, parent.winfo_fpixels("1i") / 96.0)

        body = tk.Frame(top, bg=PANEL)
        body.pack(fill="both", expand=True, padx=int(20 * scale), pady=(int(18 * scale), int(10 * scale)))
        size = int(34 * scale)
        symbol, color = self.ICONS.get(kind, self.ICONS["info"])
        icon = tk.Canvas(body, width=size, height=size, bg=PANEL, highlightthickness=0, bd=0)
        icon.create_oval(1, 1, size - 1, size - 1, fill=globals()[color], outline="")
        icon.create_text(size / 2, size / 2, text=symbol, fill=ON_ACCENT, font=("Segoe UI Semibold", 14))
        icon.pack(side="left", anchor="n", padx=(0, int(14 * scale)))
        lines = text.count("\n") + len(text) // 90
        if lines > 18:
            # long lists (folders, notices) scroll instead of making the window taller than the screen
            box = tk.Frame(body, bg=PANEL)
            box.pack(side="left", fill="both", expand=True)
            msg = tk.Text(box, bg=PANEL, fg=FG, relief="flat", wrap="word", font=("Segoe UI", 10), width=64,
                          height=18, borderwidth=0, highlightthickness=0)
            sb = ttk.Scrollbar(box, orient="vertical", command=msg.yview)
            msg.configure(yscrollcommand=sb.set)
            msg.insert("1.0", text)
            msg.configure(state="disabled")
            msg.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
        else:
            tk.Label(body, text=text, bg=PANEL, fg=FG, justify="left", anchor="w", font=("Segoe UI", 10),
                     wraplength=int(460 * scale)).pack(side="left", fill="both", expand=True)

        bar = tk.Frame(top, bg=CARD)
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(side="right", padx=int(14 * scale), pady=int(10 * scale))
        first = None
        for i, key in enumerate(buttons):
            b = ttk.Button(inner, text=t("dlg_" + key), style="Accent.TButton" if i == 0 else "TButton",
                           command=lambda k=key: self._close(k))
            b.pack(side="left", padx=(0 if i == 0 else int(8 * scale), 0))
            first = first or b
        cancel = buttons[-1]
        top.bind("<Return>", lambda _e: self._close(buttons[0]))
        top.bind("<Escape>", lambda _e: self._close(cancel))
        top.protocol("WM_DELETE_WINDOW", lambda: self._close(cancel))

        # centered over the main window
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        dark_title_bar(top, dark)
        top.deiconify()
        top.lift()
        try:
            top.grab_set()
        except tk.TclError:
            pass
        first.focus_set()
        parent.wait_window(top)

    def _close(self, key):
        self.result = key
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()


class ChecklistDialog:
    """Asks which of the files that go with an export are taken along. rows: dicts with "rel",
    "where" (None: not found) and "parent". result: the ticked rows, [] for none, None when the
    export is cancelled."""

    def __init__(self, parent, title, text, rows, dark=True):
        self.result = None
        self.rows = rows
        self.top = top = tk.Toplevel(parent)
        top.withdraw()
        top.title(title)
        top.configure(bg=PANEL)
        top.transient(parent)
        scale = max(1.0, parent.winfo_fpixels("1i") / 96.0)
        pad = int(18 * scale)

        tk.Label(top, text=text, bg=PANEL, fg=FG, justify="left", anchor="w", font=("Segoe UI", 10),
                 wraplength=int(620 * scale)).pack(fill="x", padx=pad, pady=(pad, int(8 * scale)))
        self.vars = []
        avail = [r for r in rows if r.get("where")]
        self.all_var = tk.BooleanVar(value=True)
        head = tk.Frame(top, bg=PANEL)
        head.pack(fill="x", padx=pad)
        ttk.Checkbutton(head, text=t("deps_all", n=len(avail)), variable=self.all_var,
                        command=self._toggle_all).pack(side="left")
        missing = len(rows) - len(avail)
        if missing:
            ttk.Label(head, text=t("deps_missing", n=missing), style="CardErr.TLabel").pack(side="right")

        box = ScrollFrame(top, bg=PANEL)
        box.pack(fill="both", expand=True, padx=pad, pady=(4, int(10 * scale)))
        for r in rows:
            line = ttk.Frame(box.inner, style="Card.TFrame")
            line.pack(fill="x", anchor="w")
            ok = bool(r.get("where"))
            v = tk.BooleanVar(value=ok)
            self.vars.append(v)
            cb = ttk.Checkbutton(line, text=r["rel"], variable=v, command=self._sync_all)
            cb.pack(side="left")
            if not ok:
                cb.state(["disabled"])
            ttk.Label(line, text="  " + (t("deps_for", f=r["parent"]) if ok else t("deps_not_found")),
                      style="CardMuted.TLabel" if ok else "CardErr.TLabel").pack(side="left")
        top.update_idletasks()
        need = box.inner.winfo_reqheight()
        box.configure(height=min(max(need, int(60 * scale)), int(360 * scale)))
        box.pack_propagate(False)
        box.configure(width=max(box.inner.winfo_reqwidth() + int(24 * scale), int(560 * scale)))

        bar = tk.Frame(top, bg=CARD)
        bar.pack(fill="x", side="bottom")
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(side="right", padx=int(14 * scale), pady=int(10 * scale))
        first = None
        for i, (key, value) in enumerate((("export_with", "with"), ("export_only", "only"), ("cancel", None))):
            b = ttk.Button(inner, text=t("dlg_" + key), style="Accent.TButton" if i == 0 else "TButton",
                           command=lambda v=value: self._close(v))
            b.pack(side="left", padx=(0 if i == 0 else int(8 * scale), 0))
            first = first or b
        top.bind("<Return>", lambda _e: self._close("with"))
        top.bind("<Escape>", lambda _e: self._close(None))
        top.protocol("WM_DELETE_WINDOW", lambda: self._close(None))

        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        dark_title_bar(top, dark)
        top.deiconify()
        top.lift()
        try:
            top.grab_set()
        except tk.TclError:
            pass
        first.focus_set()
        parent.wait_window(top)

    def _toggle_all(self):
        on = bool(self.all_var.get())
        for r, v in zip(self.rows, self.vars):
            if r.get("where"):
                v.set(on)

    def _sync_all(self):
        self.all_var.set(all(v.get() for r, v in zip(self.rows, self.vars) if r.get("where")))

    def _close(self, choice):
        if choice == "with":
            self.result = [r for r, v in zip(self.rows, self.vars) if v.get() and r.get("where")]
        elif choice == "only":
            self.result = []
        else:
            self.result = None
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()


class ScrollFrame(ttk.Frame):
    """Frame whose content scrolls when the window (or the log) leaves too little room.
    Build the content in .inner; it is at least as tall as the visible area."""

    def __init__(self, master, style="Card.TFrame", bg=None):
        super().__init__(master, style=style)
        self.canvas = tk.Canvas(self, bg=bg or PANEL, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas, style=style)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self._bar_shown = False
        self.inner.bind("<Configure>", lambda _e: self._update())
        self.canvas.bind("<Configure>", lambda _e: self._update())

    @staticmethod
    def route_wheel(e):
        """Global mouse wheel handler: scrolls the ScrollFrame under the pointer, if any."""
        try:
            w = e.widget.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, AttributeError, KeyError):
            return
        while w is not None:
            if isinstance(w, (tk.Text, ttk.Treeview, tk.Listbox)):
                return              # they scroll themselves
            if isinstance(w, ScrollFrame):
                w._wheel(e)
                return
            w = w.master

    def _watch(self):
        """The content's requested height does not cause an event while the item height is
        set, so it is checked now and then."""
        try:
            if not self.winfo_exists():
                return
            if self.inner.winfo_reqheight() != getattr(self, "_last_need", None):
                self._update()
            self.after(400, self._watch)
        except tk.TclError:
            pass

    def _update(self):
        try:
            cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
            need = self.inner.winfo_reqheight()
        except tk.TclError:
            return
        if not getattr(self, "_watching", False):
            self._watching = True
            self.after(400, self._watch)
        self._last_need = need
        self.canvas.itemconfigure(self._win, width=cw, height=max(ch, need))
        self.canvas.configure(scrollregion=(0, 0, cw, max(ch, need)))
        show = need > ch + 1
        if show != self._bar_shown:
            self._bar_shown = show
            if show:
                self.bar.pack(side="right", fill="y", before=self.canvas)
            else:
                self.bar.pack_forget()
                self.canvas.yview_moveto(0)

    def _wheel(self, e):
        if self._bar_shown:
            self.canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")


class ResultTable(ttk.Frame):
    """Conversion results table: search, Show boxes and clickable counts filter it."""

    # row status -> group of the Show boxes and the counts
    GROUPS = (("created", ("created",)), ("have", ("exists", "cs2", "tool", "skip")),
              ("missing", ("missing", "error")), ("unsupported", ("unsupported",)), ("found", ("found",)))
    COLS = ("kind", "path", "status", "detail", "source", "map")

    def __init__(self, master, export=None, find=None, with_map=False, with_game=False, in_folder=False):
        """export: called with [(path, source), ...] of the found files the user exports.
        find: called with a file name to search for it (Missing Assets tab).
        with_map: the rows carry the map they belong to (several maps ported at once).
        with_game: the rows carry the game a found file belongs to (same column and filter).
        in_folder: the files are written to a folder, not an addon (Tools)."""
        super().__init__(master, style="Card.TFrame")
        self.export = export
        self.find = find
        # the last column: the map of a row, or the game of a found file
        self.with_map = with_map or with_game
        self.col6 = "game" if with_game else "map"
        self.all_text = t("all_games") if with_game else t("all_maps")
        self.in_folder = in_folder
        self.group_of = {s: g for g, sts in self.GROUPS for s in sts}
        top = ttk.Frame(self, style="Card.TFrame")
        top.pack(fill="x", padx=8, pady=(6, 4))
        # Show: one box per kind of row; the counts below do the same when clicked
        ttk.Label(top, text=t("show"), style="Card.TLabel").pack(side="left")
        self.show_vars = {}
        for g, _sts in self.GROUPS:
            if g == "found":
                continue
            v = tk.BooleanVar(value=True)
            self.show_vars[g] = v
            ttk.Checkbutton(top, text=t("show_" + g), variable=v, command=self._show_changed,
                            takefocus=False).pack(side="left", padx=(6, 0))
        self.show_vars["found"] = tk.BooleanVar(value=True)
        self.map_filter = tk.StringVar(value=self.all_text)
        if self.with_map:
            ttk.Label(top, text=t("table_" + self.col6), style="Card.TLabel").pack(side="left", padx=(16, 0))
            self.cb_map = ttk.Combobox(top, textvariable=self.map_filter, values=[self.all_text],
                                       state="readonly", width=24 if with_game else 20)
            self.cb_map.pack(side="left", padx=(6, 0))
            self.cb_map.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        bar = ttk.Frame(self, style="Card.TFrame")
        bar.pack(fill="x", padx=8, pady=(0, 4))
        # search in the names (and details) of the rows
        ttk.Label(bar, text=t("table_search"), style="Card.TLabel").pack(side="left")
        self.query = tk.StringVar()
        ttk.Entry(bar, textvariable=self.query, width=28).pack(side="left", padx=(6, 12))
        self._search_job = None
        self.query.trace_add("write", lambda *a: self._search_later())
        # counts as buttons (click: only these rows), colored while there are any
        self.summary = ttk.Frame(bar, style="Card.TFrame")
        self.summary.pack(side="left")
        self._sum_labels = []
        ttk.Label(bar, text=t("dbl_click_hint"), style="CardMuted.TLabel").pack(side="right")

        wrap = ttk.Frame(self, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        cols = self.COLS[:5] + ((self.col6,) if self.with_map else ())
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=8, selectmode="extended")
        # a click on a heading sorts by that column (again: the other way); Type first
        self.sort_col, self.sort_desc = 0, False
        self._shown_keys = []
        widths = {"kind": (80, False), "path": (340, True), "status": (110, False), "detail": (250, True),
                  "source": (190, True), "map": (140, False), "game": (170, False)}
        self._widths = widths
        for i, c in enumerate(cols):
            w, stretch = widths[c]
            self.tree.heading(c, text=t("col_" + c), command=lambda i=i: self._sort_by(i))
            self.tree.column(c, width=w, stretch=stretch, anchor="w")
        self._heading_keys = tuple("col_" + c for c in cols)
        self._update_headings()
        ys = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        for st, color in STATUS_COLORS.items():
            self.tree.tag_configure(st, foreground=color)
        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<Button-3>", self._context_menu)
        self.rows = []
        self.content_dir = ""
        self.content_dirs = {}      # map name -> its addon content folder (several maps)
        self._update_summary()
        self._sync_game_col()

    def clear(self):
        self.rows = []
        self._shown_keys = []
        self.content_dirs = {}
        self.tree.delete(*self.tree.get_children())
        self._update_summary()
        if self.with_map:
            self.map_filter.set(self.all_text)
            self.cb_map["values"] = [self.all_text]
        self._sync_game_col()

    def _sync_game_col(self):
        """The Game column is only there while a row has a game (search results)."""
        if self.col6 != "game":
            return
        cols = list(self.COLS[:5]) + (["game"] if len(self.cb_map["values"]) > 1 else [])
        if list(self.tree["displaycolumns"]) != cols:
            self.tree["displaycolumns"] = cols
            self._fit_columns(cols)

    def _fit_columns(self, cols):
        """The shown columns fill the table again (the wide ones share what the others leave)."""
        width = self.tree.winfo_width()
        if width <= 1:
            return
        fixed = sum(self._widths[c][0] for c in cols if not self._widths[c][1])
        grow = [c for c in cols if self._widths[c][1]]
        total = sum(self._widths[c][0] for c in grow) or 1
        room = max(width - fixed - 4, 60 * len(grow))
        for c in cols:
            w, stretch = self._widths[c]
            self.tree.column(c, width=int(room * w / total) if stretch else w)

    def _set_summary(self, parts):
        """parts: [(text, tone ("ok", "warn", "err" or None), group or None)]. A count with a
        group is a button: only its rows are shown (click more of them for more kinds)."""
        state = [(p, r, g, self._chip_on(g)) for p, r, g in parts]
        if state == getattr(self, "_sum_parts", None):
            return
        self._sum_parts = state
        for lbl in self._sum_labels:
            lbl.destroy()
        self._sum_labels = []
        tones = {"ok": "ChipOk.TButton", "warn": "ChipWarn.TButton", "err": "ChipErr.TButton"}
        for text, tone, group, on in state:
            if group is None:
                w = ttk.Label(self.summary, text=text, style="CardMuted.TLabel")
                w.pack(side="left", padx=(6, 0))
            else:
                w = ttk.Button(self.summary, text=text, takefocus=False, cursor="hand2",
                               style="ChipOn.TButton" if on else tones.get(tone, "Chip.TButton"),
                               command=lambda g=group: self._chip_click(g))
                w.pack(side="left", padx=(0, 6))
                ToolTip(w, t("table_chip_tip"))
            self._sum_labels.append(w)

    def _shown_groups(self):
        return {g for g, v in self.show_vars.items() if v.get()}

    def _chip_on(self, group):
        """A count is lit while only some kinds are shown and its kind is one of them."""
        if group is None:
            return False
        shown = self._shown_groups() - {"found"}
        return group in shown and len(shown) < len(self.show_vars) - 1

    def _chip_click(self, group):
        groups = [g for g in self.show_vars if g != "found"]
        shown = self._shown_groups() - {"found"}
        if len(shown) == len(groups):
            shown = {group}
        elif group in shown:
            shown.discard(group)
            if not shown:
                shown = set(groups)
        else:
            shown.add(group)
        for g in groups:
            self.show_vars[g].set(g in shown)
        self.refresh()

    def _show_changed(self):
        self.refresh()

    def add(self, kind, name, status, detail, source, map_name=""):
        row = (kind, name, status, detail, source, map_name)
        self.rows.append(row)
        if self.with_map and map_name and map_name not in self.cb_map["values"]:
            self.cb_map["values"] = list(self.cb_map["values"]) + [map_name]
            self._sync_game_col()
        if self._visible(row):
            self._insert(row, len(self.rows) - 1)
        self._update_summary()

    def _display(self, row):
        kind, name, status, detail, source = row[:5]
        st = pipeline.status_text(status)
        if self.in_folder and status == "exists":
            st = t("st_in_folder")
            if detail == t("m_exists"):
                detail = t("m_in_folder")
        vals = (t("kind_" + kind), name, st, detail, source)
        return vals + ((row[5] if len(row) > 5 else "",) if self.with_map else ())

    def _sort_key(self, row, seq):
        vals = self._display(row)
        return str(vals[self.sort_col]).lower(), str(vals[1]).lower(), seq

    def _update_headings(self):
        for i, key in enumerate(self._heading_keys):
            arrow = (" ▼" if self.sort_desc else " ▲") if i == self.sort_col else ""
            self.tree.heading(key[4:], text=t(key) + arrow)

    def _sort_by(self, col):
        if col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, False
        self._update_headings()
        self.refresh()

    def _search_later(self):
        # typing fast does not rebuild the table for every key
        if self._search_job is not None:
            self.after_cancel(self._search_job)
        self._search_job = self.after(150, self._search_now)

    def _search_now(self):
        self._search_job = None
        self.refresh()

    def _visible(self, row):
        q = self.query.get().strip().lower().replace("\\", "/")
        if q and not any(q in str(v).lower().replace("\\", "/") for v in (row[1], row[3])):
            return False
        if self.with_map and self.map_filter.get() != self.all_text and \
                (row[5] if len(row) > 5 else "") != self.map_filter.get():
            return False
        return self.group_of.get(row[2], "have") in self._shown_groups()

    def _insert(self, row, seq):
        key = self._sort_key(row, seq)
        keys = self._shown_keys
        lo, hi = 0, len(keys)
        while lo < hi:
            mid = (lo + hi) // 2
            if (keys[mid] >= key) if self.sort_desc else (keys[mid] <= key):
                lo = mid + 1
            else:
                hi = mid
        keys.insert(lo, key)
        self.tree.insert("", lo, iid=f"r{seq}", values=self._display(row), tags=(row[2], row[0]))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        shown = [(self._sort_key(r, i), i, r) for i, r in enumerate(self.rows) if self._visible(r)]
        shown.sort(key=lambda x: x[0], reverse=self.sort_desc)
        self._shown_keys = [k for k, _i, _r in shown]
        for _k, i, r in shown:
            self.tree.insert("", "end", iid=f"r{i}", values=self._display(r), tags=(r[2], r[0]))
        self._update_summary()

    def _update_summary(self):
        rows = self.rows
        if self.with_map and self.map_filter.get() != self.all_text:
            rows = [r for r in rows if (r[5] if len(r) > 5 else "") == self.map_filter.get()]
        count = {g: 0 for g, _s in self.GROUPS}
        for r in rows:
            count[self.group_of.get(r[2], "have")] += 1
        if count["found"] and not (count["created"] or count["missing"] or count["unsupported"] or count["have"]):
            parts = [(t("table_found", n=count["found"]), None, None)]
        else:
            parts = [(t("table_created", n=count["created"]), "ok" if count["created"] else None, "created"),
                     (t("table_have", n=count["have"]), None, "have"),
                     (t("table_missing", n=count["missing"]), "err" if count["missing"] else None, "missing"),
                     (t("table_uns", n=count["unsupported"]), "warn" if count["unsupported"] else None,
                      "unsupported")]
        # how many rows the filters leave (always there, so the bar does not jump)
        if self.rows:
            parts.append((t("table_shown", n=len(self.tree.get_children())), None, None))
        self._set_summary(parts)

    def _row(self, iid):
        """The table row of a tree item (iid 'r<n>')."""
        try:
            return self.rows[int(str(iid)[1:])]
        except (ValueError, IndexError):
            return None

    def _content_dir_of(self, row):
        if row is not None and len(row) > 5 and row[5] in getattr(self, "content_dirs", {}):
            return self.content_dirs[row[5]]
        return self.content_dir

    def _content_file(self, kind, name, content_dir=None):
        """Path of the addon file a row stands for."""
        content_dir = content_dir or self.content_dir
        n = str(name).replace("\\", "/").lower().replace(" ", "_")
        if kind == "sound":
            rel = n[len("sound/"):] if n.startswith("sound/") else n
            base = os.path.join(content_dir, "sounds", *os.path.splitext(rel)[0].split("/"))
            for ext in (".wav", ".mp3"):
                if os.path.isfile(base + ext):
                    return base + ext
            return base + ".wav"
        if kind == "model":
            return os.path.join(content_dir, *(os.path.splitext(n)[0] + ".vmdl").split("/"))
        if n.endswith(".vmat"):
            n = n[:-5]
        if not n.startswith("materials/"):
            n = "materials/" + n
        return os.path.join(content_dir, *(n + ".vmat").split("/"))

    def _file_of(self, iid):
        """(the file a row stands for, it exists): the made file, or the found file."""
        row = self._row(iid)
        if row is None:
            return "", False
        if row[2] == "found":
            return row[4], bool(row[4]) and os.path.exists(row[4])
        cdir = self._content_dir_of(row)
        if not cdir:
            return "", False
        p = self._content_file(row[0], row[1], cdir)
        return p, os.path.isfile(p)

    def _double_click(self, e):
        # a double click on a heading only sorts
        if self.tree.identify_region(e.x, e.y) in ("cell", "tree"):
            self._open(e)

    def _open(self, _e):
        sel = self.tree.selection()
        if not sel:
            return
        p, ok = self._file_of(sel[0])
        if ok:
            open_in_explorer(p)
            return
        row = self._row(sel[0])
        cdir = self._content_dir_of(row)
        d = os.path.dirname(p) if p else ""
        while d and not os.path.isdir(d):
            d = os.path.dirname(d)
        if d or cdir:
            open_in_explorer(d or cdir)

    def _found_items(self, items):
        out = []
        for iid in items:
            row = self._row(iid)
            if row is not None and row[2] == "found":
                out.append((str(row[1]), str(row[4])))
        return out

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _context_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        # a right click on a row outside the selection selects that row only
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        sel = list(self.tree.selection())
        rows = [r for r in (self._row(i) for i in sel) if r is not None]
        row = self._row(iid)
        menu = popup_menu(self)
        found = self._found_items(sel)
        if self.export is not None and found:
            label = t("export_selected", n=len(found)) if len(found) > 1 else t("export_one")
            menu.add_command(label=label, command=lambda: self.export(found, False))
            # only the files, without their game folders
            menu.add_command(label=t("export_as_is", n=len(found)) if len(found) > 1 else t("export_as_is_one"),
                             command=lambda: self.export(found, True))
            shown = self._found_items(self.tree.get_children())
            if len(shown) > len(found):
                menu.add_command(label=t("export_shown", n=len(shown)), command=lambda: self.export(shown, False))
            menu.add_separator()
        menu.add_command(label=t("show_in_folder"), command=lambda: self._open(None))
        menu.add_separator()
        menu.add_command(label=t("menu_copy_name") if len(rows) == 1 else t("menu_copy_names", n=len(rows)),
                         command=lambda: self._copy("\n".join(str(r[1]) for r in rows)))
        paths = [p for p, _ok in (self._file_of(i) for i in sel) if p]
        menu.add_command(label=t("menu_copy_path") if len(paths) <= 1 else t("menu_copy_paths", n=len(paths)),
                         command=lambda: self._copy("\n".join(os.path.normpath(p) for p in paths)),
                         state="normal" if paths else "disabled")
        if row is not None and row[2] in ("missing", "error", "unsupported") and self.find is not None:
            menu.add_separator()
            stem = os.path.splitext(os.path.basename(str(row[1]).replace("\\", "/")))[0]
            menu.add_command(label=t("menu_find_file", name=stem), command=lambda: self.find(stem))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.cfg = config.load()
        i18n.set_lang(self.cfg.get("lang") or i18n.DEFAULT)
        if self.cfg.get("theme") not in THEMES:
            self.cfg["theme"] = "dark"
        set_theme(self.cfg["theme"])
        self.shell = None
        self.queue = queue.Queue()
        self.worker = None
        self.cancel = threading.Event()
        self.current_table = None
        self.last_report = ""
        self.indeterminate = False
        self._logo_img = None
        self._flag_imgs = {}
        self._scale = 1.0
        # log lines are kept here too, so a language switch (which rebuilds the UI) keeps them
        self.log_entries = []
        # the maps of the Port Map list: {path, addon, state, sec, report}
        # the map list starts empty every time
        self.port_items = []

        root.title("CS2 Porter")
        root.geometry("1180x820")
        root.minsize(1000, 700)
        if self.cfg.get("maximized"):
            # the last session was closed maximized
            try:
                root.state("zoomed")
            except tk.TclError:
                pass
        root.configure(bg=BG)
        ico = os.path.join(config.ASSETS_DIR, "icon.ico")
        try:
            if os.path.isfile(ico):
                root.iconbitmap(default=ico)
        except tk.TclError:
            pass
        # sharp taskbar / title bar icons: every size is drawn on its own (see assets/icon_*.png)
        self._icon_imgs = []
        for size in (256, 64, 48, 32, 24, 16):
            p = os.path.join(config.ASSETS_DIR, f"icon_{size}.png")
            try:
                if os.path.isfile(p):
                    self._icon_imgs.append(tk.PhotoImage(file=p))
            except tk.TclError:
                pass
        if self._icon_imgs:
            try:
                root.iconphoto(True, *self._icon_imgs)
            except tk.TclError:
                pass
        root.after(150, lambda: self._set_window_icons(ico))
        root.after(160, lambda: dark_title_bar(root, self.cfg["theme"] == "dark"))
        self._style()
        self._build()
        self._show_shell()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind_all("<MouseWheel>", ScrollFrame.route_wheel, add="+")
        root.bind_all("<Button-1>", self._click_focus, add="+")
        # right click on any text field: cut / copy / paste
        for cls in ("TEntry", "TCombobox"):
            root.bind_class(cls, "<Button-3>", entry_menu, add="+")
        # the lit taskbar button goes off as soon as the window is in front again
        self._lit = False
        root.bind("<FocusIn>", self._unlight, add="+")
        root.bind("<Activate>", self._unlight, add="+")
        root.after(60, self._poll)
        root.after(300, self._startup_checks)

    # widgets that keep the keyboard focus when clicked; a click anywhere else takes it away from
    # the text field, so its focus ring goes
    _KEEP_FOCUS = ("TEntry", "Entry", "TCombobox", "Text", "TSpinbox", "Spinbox", "Listbox", "Treeview")

    def _unlight(self, _e=None):
        if self._lit:
            self._lit = False
            flash_taskbar(self.root, stop=True)

    def _click_focus(self, e):
        w = e.widget
        try:
            if w.winfo_class() not in self._KEEP_FOCUS:
                w.winfo_toplevel().focus_set()
        except (AttributeError, tk.TclError):
            pass

    def _set_window_icons(self, ico):
        """The taskbar shows the window's small icon at 24 px (at 100 % scale); Tk gives it the
        16 px image, which Windows enlarges and blurs. The icon of the taskbar size (and a big
        one for Alt+Tab) is taken from the .ico instead."""
        if not os.path.isfile(ico):
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
            user32.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            self.root.update_idletasks()
            hwnd = int(self.root.wm_frame(), 16)
            scale = self.root.winfo_fpixels("1i") / 96.0
            # (WM_SETICON type, class icon index, size): ICON_SMALL / GCLP_HICONSM, ICON_BIG / GCLP_HICON
            for which, cls, size in ((0, -34, round(24 * scale)), (1, -14, round(48 * scale))):
                h = user32.LoadImageW(None, ico, 1, size, size, 0x10)              # IMAGE_ICON, LR_LOADFROMFILE
                if h:
                    user32.SendMessageW(hwnd, 0x80, which, h)                       # WM_SETICON
                    user32.SetClassLongPtrW(hwnd, cls, h)
        except (AttributeError, OSError, ValueError, tk.TclError):
            pass

    def _msg(self, title, text, kind="info"):
        Dialog(self.root, title, text, kind, ("ok",), self.cfg["theme"] == "dark")

    def _ask(self, title, text, kind="question"):
        return Dialog(self.root, title, text, kind, ("yes", "no"), self.cfg["theme"] == "dark").result == "yes"

    # --- theme ---------------------------------------------------------------
    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        self.root.configure(bg=BG)
        base_font = ("Segoe UI", 10)
        self.root.option_add("*TCombobox*Listbox.font", base_font)
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", ON_ACCENT)
        s.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER,
                    lightcolor=PANEL, darkcolor=PANEL, troughcolor=FIELD, focuscolor=ACCENT,
                    selectbackground=ACCENT, selectforeground=ON_ACCENT, font=base_font)
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=PANEL)
        s.configure("Inner.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Card.TLabel", background=PANEL, foreground=FG)
        s.configure("Inner.TLabel", background=CARD, foreground=FG)
        s.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("CardErr.TLabel", background=PANEL, foreground=ERR, font=("Segoe UI", 9))
        # Port Map list
        s.configure("Map.Treeview", rowheight=26, font=("Segoe UI", 10))
        s.configure("About.TFrame", background=CARD)
        s.configure("About.TLabel", background=CARD, foreground=FG)
        s.configure("AboutTitle.TLabel", background=CARD, foreground=FG, font=("Segoe UI Semibold", 12))
        s.configure("AboutMuted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("AboutLink.TLabel", background=CARD, foreground=ACCENT, font=("Segoe UI", 10, "underline"))
        s.configure("InnerMuted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("Footer.TLabel", background=BG, foreground=FOOTER, font=("Segoe UI", 9))
        s.configure("Brand.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI Semibold", 9, "underline"))
        s.configure("Link.TLabel", background=PANEL, foreground=ACCENT, font=("Segoe UI", 10, "underline"))
        s.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI Semibold", 18))
        s.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        s.configure("Section.TLabel", background=PANEL, foreground=ACCENT, font=("Segoe UI Semibold", 10))
        s.configure("InnerSection.TLabel", background=CARD, foreground=ACCENT, font=("Segoe UI Semibold", 12))
        s.configure("NavHead.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI Semibold", 8))
        s.configure("Chip.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9), padding=(8, 3))
        s.configure("ChipOk.TLabel", background=CHIP_OK, foreground=OK, font=("Segoe UI", 9), padding=(8, 3))
        s.configure("ChipBad.TLabel", background=CHIP_BAD, foreground=ERR, font=("Segoe UI", 9), padding=(8, 3))

        s.configure("TButton", background=CARD, foreground=FG, bordercolor=BORDER, padding=(12, 5),
                    lightcolor=CARD, darkcolor=CARD, focusthickness=0)
        s.map("TButton", background=[("disabled", PANEL), ("pressed", BORDER), ("active", HOVER)],
              foreground=[("disabled", DISABLED)])
        s.configure("Accent.TButton", background=ACCENT, foreground=ON_ACCENT, bordercolor=ACCENT_DARK,
                    lightcolor=ACCENT, darkcolor=ACCENT, font=("Segoe UI Semibold", 10), padding=(18, 7))
        s.map("Accent.TButton", background=[("disabled", ACCENT_OFF), ("pressed", ACCENT_DARK), ("active", ACCENT_HI)],
              foreground=[("disabled", ACCENT_OFF_FG)])
        s.configure("Danger.TButton", background=DANGER, foreground=DANGER_FG, bordercolor=DANGER_BORDER,
                    lightcolor=DANGER, darkcolor=DANGER, padding=(12, 7))
        s.map("Danger.TButton", background=[("disabled", PANEL), ("active", DANGER_HOVER)],
              foreground=[("disabled", DISABLED)])
        s.configure("Small.TButton", padding=(8, 3), font=("Segoe UI", 9))
        # up / down buttons of the log search
        s.configure("Arrow.TButton", padding=(6, 1), font=("Segoe UI", 8), width=2)
        # the count buttons of the result tables (colored while there are any; lit: only their
        # rows are shown)
        for name, fg in (("Chip", MUTED), ("ChipOk", OK), ("ChipWarn", WARN), ("ChipErr", ERR)):
            s.configure(name + ".TButton", background=CARD, foreground=fg, bordercolor=BORDER, lightcolor=CARD,
                        darkcolor=CARD, padding=(10, 2), font=("Segoe UI Semibold", 9))
            s.map(name + ".TButton", background=[("pressed", BORDER), ("active", HOVER)],
                  bordercolor=[("active", fg)])
        s.configure("ChipOn.TButton", background=ACCENT, foreground=ON_ACCENT, bordercolor=ACCENT_DARK,
                    lightcolor=ACCENT, darkcolor=ACCENT, padding=(10, 2), font=("Segoe UI Semibold", 9))
        s.map("ChipOn.TButton", background=[("pressed", ACCENT_DARK), ("active", ACCENT_HI)])

        # button-looking radio buttons for the tools menu and the language picker
        s.configure("Nav.Toolbutton", background=PANEL, foreground=FG, anchor="w", padding=(12, 6),
                    font=("Segoe UI", 10), bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL, relief="flat")
        s.map("Nav.Toolbutton", background=[("selected", CARD), ("active", NAV_HOVER)],
              foreground=[("selected", ACCENT)], bordercolor=[("selected", CARD)],
              lightcolor=[("selected", CARD)], darkcolor=[("selected", CARD)])
        s.configure("Lang.Toolbutton", background=CARD, foreground=MUTED, padding=(9, 3), anchor="center",
                    font=("Segoe UI Semibold", 9), bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
        s.map("Lang.Toolbutton", background=[("selected", ACCENT), ("active", HOVER)],
              foreground=[("selected", ON_ACCENT)])
        # no dotted focus ring: a tab click moves the focus to the first button of the tab, which made
        # the first tool look selected
        for name in ("Nav.Toolbutton", "Lang.Toolbutton"):
            s.layout(name, [("Toolbutton.border", {"sticky": "nswe", "children": [
                ("Toolbutton.padding", {"sticky": "nswe", "children": [("Toolbutton.label", {"sticky": "nswe"})]})]})])
        s.layout("TNotebook.Tab", [("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})])

        for name, bg in (("TCheckbutton", PANEL), ("Inner.TCheckbutton", CARD), ("TRadiobutton", PANEL),
                         ("Inner.TRadiobutton", CARD)):
            s.configure(name, background=bg, foreground=FG, indicatorbackground=FIELD,
                        indicatorforeground=ON_ACCENT, indicatormargin=(0, 0, 6, 0), padding=(2, 3),
                        upperbordercolor=BORDER, lowerbordercolor=BORDER)
            s.map(name, background=[("active", bg)],
                  indicatorbackground=[("selected", ACCENT), ("pressed", BORDER)],
                  upperbordercolor=[("selected", ACCENT_DARK)], lowerbordercolor=[("selected", ACCENT_DARK)],
                  foreground=[("disabled", DISABLED)])

        s.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG, bordercolor=BORDER,
                    lightcolor=FIELD, darkcolor=FIELD, padding=(6, 4))
        s.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        # log search: room on the right for the match count drawn over it
        s.configure("Find.TEntry", padding=(6, 4, 62, 4))
        s.map("Find.TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        s.configure("FindCount.TLabel", background=FIELD, foreground=MUTED, font=("Segoe UI", 8))
        s.configure("FindCountErr.TLabel", background=FIELD, foreground=ERR, font=("Segoe UI", 8))
        s.configure("TCombobox", fieldbackground=FIELD, foreground=FG, background=CARD, arrowcolor=FG,
                    bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD, padding=(6, 4), insertcolor=FG)
        s.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)],
              bordercolor=[("focus", ACCENT)], selectbackground=[("readonly", FIELD)],
              selectforeground=[("readonly", FG)])

        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(12, 6, 12, 0),
                    bordercolor=BORDER, lightcolor=PANEL, darkcolor=PANEL)
        s.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(16, 7), borderwidth=0,
                    font=("Segoe UI Semibold", 10), bordercolor=BORDER, lightcolor=BG, darkcolor=BG)
        s.map("TNotebook.Tab", background=[("selected", PANEL), ("active", TAB_HOVER)],
              foreground=[("selected", ACCENT), ("active", FG)],
              lightcolor=[("selected", PANEL)], darkcolor=[("selected", PANEL)])

        s.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=FG, bordercolor=BORDER,
                    rowheight=24, font=("Segoe UI", 9))
        s.map("Treeview", background=[("selected", ROW_SEL)], foreground=[("selected", FG)])
        s.configure("Treeview.Heading", background=CARD, foreground=MUTED, bordercolor=BORDER,
                    relief="flat", font=("Segoe UI Semibold", 9), padding=(6, 4))
        s.map("Treeview.Heading", background=[("active", HEAD_HOVER)])
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD, bordercolor=BORDER,
                    lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)
        s.configure("Vertical.TScrollbar", background=CARD, troughcolor=FIELD, bordercolor=FIELD,
                    arrowcolor=MUTED, lightcolor=CARD, darkcolor=CARD)
        s.map("Vertical.TScrollbar", background=[("active", BORDER)])
        s.configure("TPanedwindow", background=BG)
        s.configure("Sash", sashthickness=6, gripcount=0, background=BG)

    # --- layout ---------------------------------------------------------------
    def _logo(self, size):
        try:
            from PIL import Image, ImageTk
            img = Image.open(os.path.join(config.ASSETS_DIR, "logo.png")).resize((size, size), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _link(label, url):
        label.configure(cursor="hand2")
        label.bind("<Button-1>", lambda _e: webbrowser.open(url))
        return label

    def _flag(self, code, height=14):
        key = (code, height)
        if key not in self._flag_imgs:
            img = None
            try:
                from PIL import Image, ImageTk
                im = Image.open(os.path.join(config.ASSETS_DIR, "flags", f"{code}.png")).convert("RGBA")
                h = max(10, int(height * self._scale))
                img = ImageTk.PhotoImage(im.resize((round(h * im.width / im.height), h), Image.LANCZOS))
            except Exception:  # noqa: BLE001
                img = None
            self._flag_imgs[key] = img
        return self._flag_imgs[key] or ""

    def _theme_icon(self, height=14):
        """Moon (dark theme is on) or sun (light theme is on), drawn in the text color."""
        key = ("theme", self.cfg["theme"], height)
        if key not in self._flag_imgs:
            img = None
            try:
                from PIL import Image, ImageDraw, ImageTk
                h = max(10, int(height * self._scale))
                big = h * 4
                im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
                d = ImageDraw.Draw(im)
                rgb = tuple(int(FG[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
                c = big / 2
                if self.cfg["theme"] == "dark":
                    r = big * 0.42
                    d.ellipse((c - r, c - r, c + r, c + r), fill=rgb)
                    # the bite out of the moon
                    o = big * 0.36
                    d.ellipse((c - r + o, c - r - o * 0.45, c + r + o, c + r - o * 0.45), fill=(0, 0, 0, 0))
                else:
                    r = big * 0.2
                    d.ellipse((c - r, c - r, c + r, c + r), fill=rgb)
                    w = max(2, int(big * 0.08))
                    for i in range(8):
                        a = math.pi / 4 * i
                        x0, y0 = c + math.cos(a) * big * 0.31, c + math.sin(a) * big * 0.31
                        x1, y1 = c + math.cos(a) * big * 0.46, c + math.sin(a) * big * 0.46
                        d.line((x0, y0, x1, y1), fill=rgb, width=w)
                img = ImageTk.PhotoImage(im.resize((h, h), Image.LANCZOS))
            except Exception:  # noqa: BLE001
                img = None
            self._flag_imgs[key] = img
        return self._flag_imgs[key] or ""

    def _toggle_theme(self):
        self.v_theme.set("light" if self.cfg["theme"] == "dark" else "dark")
        self._change_theme()

    def _build(self):
        """Builds the whole window inside a new frame; the caller shows it with _show_shell()."""
        try:
            scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            scale = 1.0
        self._scale = scale
        self.shell = root = ttk.Frame(self.root)

        header = ttk.Frame(root)
        header.pack(fill="x", padx=18, pady=(12, 6))
        self._logo_img = self._logo(int(50 * scale))
        if self._logo_img is not None:
            ttk.Label(header, image=self._logo_img).pack(side="left", padx=(0, 12))
        left = ttk.Frame(header)
        left.pack(side="left")
        ttk.Label(left, text="CS2 Porter", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text=t("app_subtitle"), style="Sub.TLabel").pack(anchor="w")

        right = ttk.Frame(header)
        right.pack(side="right", anchor="e")
        langs = ttk.Frame(right)
        langs.pack(side="right", padx=(12, 0))
        self.v_lang = tk.StringVar(value=i18n.get_lang())
        for code, name in i18n.languages():
            rb = ttk.Radiobutton(langs, text=code.upper(), image=self._flag(code), compound="left", value=code,
                                 variable=self.v_lang, style="Lang.Toolbutton", command=self._change_lang)
            rb.pack(side="left", padx=1)
            ToolTip(rb, name)
        # dark / light switch next to the languages
        icon = self._theme_icon()
        tb = ttk.Button(right, image=icon, text="" if icon else "◐", style="Lang.Toolbutton",
                        command=self._toggle_theme, takefocus=False)
        tb.pack(side="right", padx=(12, 0))
        ToolTip(tb, t("theme_light") if self.cfg["theme"] == "dark" else t("theme_dark"))
        chips = ttk.Frame(right)
        chips.pack(side="right")
        self.chip_cs2 = ttk.Label(chips, text="CS2", style="Chip.TLabel")
        self.chip_cs2.pack(side="left", padx=3)
        self.chip_games = ttk.Label(chips, text="", style="Chip.TLabel")
        self.chip_games.pack(side="left", padx=3)

        footer = ttk.Frame(root)
        footer.pack(side="bottom", fill="x", padx=18, pady=(0, 6))
        ttk.Label(footer, text=f"v{__version__}", style="Footer.TLabel").pack(side="left")
        self._link(ttk.Label(footer, text="Made by tehlikeli91", style="Brand.TLabel"),
                   config.AUTHOR_PAGE).pack(side="right")

        # the panes are resized when the divider is let go (a live resize redraws every widget on
        # each mouse move and makes the log jump)
        self.paned = paned = tk.PanedWindow(root, orient="vertical", bg=BG, bd=0, sashwidth=int(7 * scale),
                                            sashrelief="flat", opaqueresize=False, showhandle=False)
        try:
            paned.configure(proxybackground=MUTED, proxyrelief="flat")
        except tk.TclError:
            pass
        paned.pack(fill="both", expand=True, padx=12)
        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, stretch="always", minsize=int(260 * scale))
        self.tab_port = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_missing = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_tools = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_settings = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_help = ttk.Frame(self.nb, style="Card.TFrame")
        for tab, key in ((self.tab_port, "tab_port"), (self.tab_missing, "tab_missing"),
                         (self.tab_tools, "tab_tools"), (self.tab_settings, "tab_settings"),
                         (self.tab_help, "tab_help")):
            self.nb.add(tab, text=f"  {t(key)}  ")

        bottom = ttk.Frame(paned)
        paned.add(bottom, stretch="always", minsize=int(110 * scale))
        self._build_status(bottom)
        self._build_log(bottom)

        self._build_port_tab()
        self._build_missing_tab()
        self._build_tools_tab()
        self._build_settings_tab()
        self._build_help_tab()
        self._refresh_addons()
        self._update_chips()

    def _bottom_height(self):
        try:
            return self.paned.winfo_height() - self.paned.sash_coord(0)[1]
        except (AttributeError, tk.TclError, IndexError):
            return 0

    def _show_shell(self, old=None, bottom=0):
        """Shows the frame _build() made (the old one is removed only now, so a rebuild does not
        show half built tabs) and gives the log its height."""
        if old is not None:
            old.pack_forget()
        self.shell.pack(fill="both", expand=True)
        if old is not None:
            old.destroy()
        self.root.update_idletasks()
        self._place_sash(bottom or int(225 * self._scale))

    def _place_sash(self, bottom, tries=0):
        paned = self.paned
        try:
            h = paned.winfo_height()
            if h <= 1 and tries < 40:
                # the window is not on screen yet
                self.root.after(25, lambda: self._place_sash(bottom, tries + 1))
                return
            paned.sash_place(0, 0, max(int(260 * self._scale), h - bottom))
        except tk.TclError:
            pass

    def _build_status(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(8, 4))
        # spinning arc and elapsed time while a job runs
        size = int(16 * self._scale)
        self.spinner = tk.Canvas(bar, width=size, height=size, bg=BG, highlightthickness=0, bd=0)
        self.spinner.pack(side="left", padx=(4, 6))
        pad = max(2, size // 8)
        self._spin_arc = self.spinner.create_arc(pad, pad, size - pad, size - pad, start=90, extent=0,
                                                 style="arc", outline=ACCENT, width=max(2, size // 7))
        self.lbl_timer = ttk.Label(bar, text="", style="Muted.TLabel", width=7, anchor="w")
        self.lbl_timer.pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="determinate", maximum=1000)
        self.progress.pack(side="left", fill="x", expand=True, padx=(4, 10))
        self.status = ttk.Label(bar, text=t("ready"), style="Muted.TLabel", width=46, anchor="w")
        self.status.pack(side="left")
        if getattr(self, "_job_t0", None) and self.worker and self.worker.is_alive():
            self._tick()

    @staticmethod
    def _clock(sec):
        sec = int(sec)
        return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"

    def _tick(self):
        """Turns the spinner and updates the elapsed time while a job runs."""
        if not (self.worker and self.worker.is_alive()) or not self.spinner.winfo_exists():
            return
        self._spin_angle = (getattr(self, "_spin_angle", 0) - 24) % 360
        self.spinner.itemconfigure(self._spin_arc, start=self._spin_angle, extent=270)
        self.lbl_timer.config(text=self._clock(time.time() - self._job_t0))
        self.root.after(60, self._tick)

    def _build_log(self, parent):
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.pack(fill="both", expand=True, pady=(0, 6))
        head = ttk.Frame(frame, style="Card.TFrame")
        head.pack(fill="x", padx=10, pady=(6, 2))
        ttk.Label(head, text=t("log_title"), style="Section.TLabel").pack(side="left")
        # a setting of the log itself: next to its title
        self.verbose = tk.BooleanVar(value=False)
        ttk.Checkbutton(head, text=t("log_verbose"), variable=self.verbose).pack(side="left", padx=(14, 0))
        ttk.Button(head, text=t("log_clear"), style="Small.TButton", command=self._clear_log).pack(side="right")
        ttk.Button(head, text=t("log_save"), style="Small.TButton", command=self._save_log).pack(side="right", padx=6)
        self._build_log_find(head)
        body = ttk.Frame(frame, style="Card.TFrame")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        # the selection stays visible when the log does not have the focus (Select all)
        self.log_text = tk.Text(body, bg=FIELD, fg=INFO, insertbackground=FG, relief="flat", wrap="word",
                                font=("Consolas", 9), padx=8, pady=6, height=4, borderwidth=0,
                                highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER,
                                selectbackground=ROW_SEL, selectforeground=FG, inactiveselectbackground=ROW_SEL,
                                exportselection=False)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("info", INFO), ("ok", OK), ("warn", WARN), ("err", ERR), ("dim", MUTED),
                           ("tool", TOOL), ("time", LOG_TIME)):
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.tag_configure("head", foreground=ACCENT, font=("Consolas", 9, "bold"))
        self.log_text.tag_configure("find", background=ACCENT_OFF, foreground=FG)
        self.log_text.tag_configure("find_cur", background=ACCENT, foreground=ON_ACCENT)
        self.log_text.tag_raise("find")
        self.log_text.tag_raise("find_cur")
        self.log_text.tag_raise("sel")
        self.log_text.bind("<Button-3>", self._log_menu)
        self.log_text.bind("<Button-1>", lambda e: self.log_text.focus_set())
        self.log_text.bind("<Control-a>", lambda e: self._log_select_all() or "break")
        self.log_text.bind("<Control-A>", lambda e: self._log_select_all() or "break")
        self.log_text.bind("<Control-c>", lambda e: self._log_copy() or "break")
        self.log_text.bind("<Control-C>", lambda e: self._log_copy() or "break")

    def _log_select_all(self):
        self.log_text.focus_set()
        self.log_text.tag_add("sel", "1.0", "end-1c")

    def _log_selection(self):
        try:
            return self.log_text.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def _log_copy(self):
        sel = self._log_selection()
        if sel:
            copy_text(self.root, sel)

    # --- log search (Ctrl+F) ---------------------------------------------------
    def _build_log_find(self, head):
        box = ttk.Frame(head, style="Card.TFrame")
        box.pack(side="right", padx=(0, 10))
        if getattr(self, "v_logfind", None) is None:
            self.v_logfind = tk.StringVar()
        self.find_entry = ttk.Entry(box, textvariable=self.v_logfind, width=30, style="Find.TEntry")
        self.find_entry.pack(side="left")
        ToolTip(self.find_entry, t("log_find_tip"))
        # the match count sits inside the box, at its right end (nothing there while empty)
        self.lbl_find = ttk.Label(self.find_entry, text="Ctrl+F", style="FindCount.TLabel", cursor="xterm")
        self.lbl_find.place(relx=1.0, x=-6, rely=0.5, anchor="e")
        self.lbl_find.bind("<Button-1>", lambda e: self._find_focus())
        ttk.Button(box, text="▲", style="Arrow.TButton", takefocus=False,
                   command=lambda: self._find_step(-1)).pack(side="left", padx=(4, 0))
        ttk.Button(box, text="▼", style="Arrow.TButton", takefocus=False,
                   command=lambda: self._find_step(1)).pack(side="left", padx=(2, 0))
        self._find_job = None
        if not getattr(self, "_find_traced", False):
            self._find_traced = True
            self.v_logfind.trace_add("write", lambda *a: self._find_later(new=True))
            # Ctrl+F anywhere in the window goes to the log search
            for seq in ("<Control-f>", "<Control-F>"):
                self.root.bind(seq, lambda e: self._find_focus())
        self.find_entry.bind("<Return>", lambda e: self._find_step(1))
        self.find_entry.bind("<Shift-Return>", lambda e: self._find_step(-1))
        self.find_entry.bind("<Down>", lambda e: self._find_step(1))
        self.find_entry.bind("<Up>", lambda e: self._find_step(-1))
        self.find_entry.bind("<Escape>", lambda e: self.v_logfind.set(""))

    def _find_focus(self):
        self.find_entry.focus_set()
        self.find_entry.select_range(0, "end")
        return "break"

    def _find_later(self, new=False):
        """Marks the matches again a moment later (typing, or new log lines while searching)."""
        if new:
            self._find_new = True
        if self._find_job is not None:
            self.root.after_cancel(self._find_job)
        self._find_job = self.root.after(200, self._find_all)

    def _find_ranges(self, tag):
        r = self.log_text.tag_ranges(tag)
        return [(str(r[i]), str(r[i + 1])) for i in range(0, len(r), 2)]

    def _find_all(self):
        """Every place the search text is in the log (any case) gets marked."""
        self._find_job = None
        txt = self.log_text
        if not txt.winfo_exists():
            return
        cur = self._find_ranges("find_cur")
        txt.tag_remove("find", "1.0", "end")
        txt.tag_remove("find_cur", "1.0", "end")
        q = self.v_logfind.get()
        hits = []
        if q:
            count = tk.IntVar()
            idx = "1.0"
            while len(hits) < 20000:
                idx = txt.search(q, idx, stopindex="end", nocase=True, count=count)
                if not idx or not count.get():
                    break
                end = f"{idx}+{count.get()}c"
                txt.tag_add("find", idx, end)
                hits.append((idx, end))
                idx = end
        if not hits:
            self._find_hits = []
            self.lbl_find.configure(text="0 / 0" if q else "Ctrl+F", style="FindCountErr.TLabel" if q else "FindCount.TLabel")
            self._find_new = False
            return
        # typing: the first match; new log lines: the same match as before
        pos = 0
        if cur and not getattr(self, "_find_new", False):
            pos = next((i for i, (a, _b) in enumerate(hits) if txt.compare(a, ">=", cur[0][0])), len(hits) - 1)
        self._find_new = False
        self._find_mark(hits, pos)

    def _find_mark(self, hits, pos):
        self._find_hits, self._find_pos = hits, pos
        a, b = hits[pos]
        self.log_text.tag_remove("find_cur", "1.0", "end")
        self.log_text.tag_add("find_cur", a, b)
        self.log_text.see(a)
        self.lbl_find.configure(text=f"{pos + 1} / {len(hits)}", style="FindCount.TLabel")

    def _find_step(self, step):
        if self._find_job is not None:
            self.root.after_cancel(self._find_job)
            self._find_all()
        hits = getattr(self, "_find_hits", None)
        if not hits or not self.v_logfind.get():
            return "break"
        self._find_mark(hits, (self._find_pos + step) % len(hits))
        return "break"

    def _log_menu(self, e):
        txt = self.log_text
        sel = self._log_selection()
        line = txt.get(f"@{e.x},{e.y} linestart", f"@{e.x},{e.y} lineend")
        menu = popup_menu(txt)
        menu.add_command(label=t("menu_copy"), command=lambda: copy_text(self.root, sel),
                         state="normal" if sel else "disabled")
        menu.add_command(label=t("menu_copy_line"), command=lambda: copy_text(self.root, line),
                         state="normal" if line.strip() else "disabled")
        menu.add_command(label=t("menu_copy_all"), command=lambda: copy_text(self.root, txt.get("1.0", "end-1c")))
        menu.add_command(label=t("menu_select_all"), command=self._log_select_all)
        # a path in the line (C:\... or a game path) can be opened
        m = re.search(r"([A-Za-z]:\\[^\"'<>|*?\n]+?)(?:\s{2}|\s*$|\s\()", line)
        if m and os.path.exists(m.group(1).strip()):
            p = m.group(1).strip()
            menu.add_separator()
            menu.add_command(label=t("menu_open_path"), command=lambda: open_in_explorer(p))
        menu.add_separator()
        menu.add_command(label=t("log_save"), command=self._save_log)
        menu.add_command(label=t("log_clear"), command=self._clear_log)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    # --- Port Map ----------------------------------------------------------
    def _build_port_tab(self):
        tab = self.tab_port
        top = ttk.Frame(tab, style="Card.TFrame")
        top.pack(fill="x", padx=16, pady=(12, 2))
        head = ttk.Frame(top, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text=t("port_maps_title"), style="Section.TLabel").pack(side="left")
        ttk.Label(head, text=t("port_hint"), style="CardMuted.TLabel").pack(side="left", padx=(12, 0))

        body = ttk.Frame(top, style="Card.TFrame")
        body.pack(fill="x", pady=(4, 0))
        lst = ttk.Frame(body, style="Card.TFrame")
        lst.pack(side="left", fill="x", expand=True)
        cols = ("map", "addon", "status", "path")
        self.map_list = ttk.Treeview(lst, columns=cols, show="headings", height=4, style="Map.Treeview",
                                     selectmode="extended")
        for c, w, stretch in (("map", 180, False), ("addon", 180, False), ("status", 150, False),
                              ("path", 360, True)):
            self.map_list.heading(c, text=t("mapcol_" + c), anchor="w")
            self.map_list.column(c, width=w, stretch=stretch, anchor="w")
        for tag, color in (("running", ACCENT), ("done", OK), ("failed", ERR), ("cancelled", MUTED),
                           ("waiting", FG), ("missing", ERR)):
            self.map_list.tag_configure(tag, foreground=color)
        self.map_list.tag_configure("done", background=CHIP_OK)
        ys = ttk.Scrollbar(lst, orient="vertical", command=self.map_list.yview)
        self.map_list.configure(yscrollcommand=ys.set)
        self.map_list.pack(side="left", fill="x", expand=True)
        ys.pack(side="left", fill="y")
        self.map_list.bind("<<TreeviewSelect>>", lambda e: self._map_selected())
        self.map_list.bind("<Button-3>", self._map_menu)
        self.map_list.bind("<Delete>", lambda e: self._remove_maps())
        self.map_list.bind("<Double-1>", lambda e: self.cb_addon.focus_set())
        btns = ttk.Frame(body, style="Card.TFrame")
        btns.pack(side="left", fill="y", padx=(10, 0))
        ttk.Button(btns, text=t("port_add"), command=self._pick_bsp).pack(fill="x")
        ttk.Button(btns, text=t("port_add_folder"), command=self._pick_bsp_folder).pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text=t("port_remove"), command=self._remove_maps).pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text=t("port_clear"), command=self._clear_maps).pack(fill="x", pady=(4, 0))

        row = ttk.Frame(top, style="Card.TFrame")
        row.pack(fill="x", pady=(6, 0))
        lab = ttk.Label(row, text=t("port_addon_label"), style="Card.TLabel")
        lab.pack(side="left")
        ToolTip(lab, t("port_addon_tip"))
        self.v_addon = tk.StringVar()
        self.cb_addon = ttk.Combobox(row, textvariable=self.v_addon, width=34)
        self.cb_addon.pack(side="left", padx=8)
        ToolTip(self.cb_addon, t("port_addon_tip"))
        self._addon_sync = False
        self.v_addon.trace_add("write", lambda *a: self._addon_changed())
        self.lbl_mapinfo = ttk.Label(row, text="", style="CardMuted.TLabel")
        self.lbl_mapinfo.pack(side="left", padx=12)

        actions = ttk.Frame(tab, style="Card.TFrame")
        actions.pack(fill="x", padx=16, pady=(10, 8))
        self.btn_port = ttk.Button(actions, text=t("port_start"), style="Accent.TButton", command=self._start_port)
        self.btn_port.pack(side="left")
        self.btn_stop = ttk.Button(actions, text=t("stop"), style="Danger.TButton", command=self._stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(actions, text=t("port_open_report"), command=self._open_report).pack(side="right")
        ttk.Button(actions, text=t("port_open_addon"), command=self._open_addon).pack(side="right", padx=8)

        self.table_port = ResultTable(tab, find=self._find_from_table, with_map=True)
        self.table_port.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self._fill_map_list()

    # --- Missing Assets ----------------------------------------------------
    def _build_missing_tab(self):
        sf = ScrollFrame(self.tab_missing)
        sf.pack(fill="both", expand=True)
        tab = sf.inner

        # 1) the missing assets of a ported map
        ttk.Label(tab, text=t("miss_title"), style="Section.TLabel").pack(anchor="w", padx=16, pady=(12, 0))
        ttk.Label(tab, style="CardMuted.TLabel", wraplength=1050, justify="left",
                  text=t("miss_info")).pack(fill="x", padx=16, pady=(2, 6))
        grid = ttk.Frame(tab, style="Card.TFrame")
        grid.pack(fill="x", padx=16)
        grid.columnconfigure(1, weight=1)
        ttk.Label(grid, text=t("miss_addon"), style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=3)
        row = ttk.Frame(grid, style="Card.TFrame")
        row.grid(row=0, column=1, columnspan=2, sticky="w", padx=8, pady=3)
        self.v_maddon = tk.StringVar()
        self.cb_maddon = ttk.Combobox(row, textvariable=self.v_maddon, width=30)
        self.cb_maddon.pack(side="left")
        self.cb_maddon.bind("<<ComboboxSelected>>", lambda e: self._maddon_selected())
        self.cb_maddon.bind("<FocusOut>", lambda e: self._maddon_selected())
        ttk.Label(row, text=t("miss_map"), style="Card.TLabel").pack(side="left", padx=(16, 6))
        self.v_mmap = tk.StringVar()
        self.cb_mmap = ttk.Combobox(row, textvariable=self.v_mmap, width=30, state="readonly")
        self.cb_mmap.pack(side="left")

        ttk.Label(grid, text=t("miss_extra"), style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.v_mextra = tk.StringVar()
        ttk.Entry(grid, textvariable=self.v_mextra).grid(row=1, column=1, sticky="ew", padx=8, pady=(6, 0))
        eb = ttk.Frame(grid, style="Card.TFrame")
        eb.grid(row=1, column=2, sticky="e", pady=(6, 0))
        ttk.Button(eb, text=t("miss_pick_bsp"), command=self._pick_extra_bsp).pack(side="left")
        ttk.Button(eb, text=t("miss_pick_folder"), command=lambda: self._pick_dir(self.v_mextra)).pack(side="left", padx=(6, 0))
        ttk.Label(grid, text=t("miss_extra_hint"), style="CardMuted.TLabel", wraplength=800,
                  justify="left").grid(row=2, column=1, sticky="w", padx=8)

        act = ttk.Frame(tab, style="Card.TFrame")
        act.pack(fill="x", padx=16, pady=(8, 4))
        self.btn_missing = ttk.Button(act, text=t("miss_start"), style="Accent.TButton", command=self._start_missing)
        self.btn_missing.pack(side="left")
        self.btn_stop2 = ttk.Button(act, text=t("stop"), style="Danger.TButton", command=self._stop, state="disabled")
        self.btn_stop2.pack(side="left", padx=8)
        ttk.Button(act, text=t("port_open_report"), command=self._open_report).pack(side="right")

        tk.Frame(tab, height=1, bg=BORDER).pack(fill="x", padx=16, pady=(8, 0))

        # 2) find files by name in every source (game files, resource folders, the extra source)
        ttk.Label(tab, text=t("find_label"), style="Section.TLabel").pack(anchor="w", padx=16, pady=(8, 0))
        ttk.Label(tab, text=t("find_hint"), style="CardMuted.TLabel", wraplength=1050,
                  justify="left").pack(fill="x", padx=16, pady=(2, 6))
        fr = ttk.Frame(tab, style="Card.TFrame")
        fr.pack(fill="x", padx=16, pady=(0, 8))
        self.v_find = tk.StringVar()
        ent = ttk.Entry(fr, textvariable=self.v_find, width=40)
        ent.pack(side="left", padx=(0, 10))
        ent.bind("<Return>", lambda e: self._start_find())
        self.v_find_mode = tk.StringVar(value="contains")
        for mode in ("contains", "exact"):
            ttk.Radiobutton(fr, text=t("find_" + mode), value=mode,
                            variable=self.v_find_mode).pack(side="left", padx=(0, 10))
        self.btn_find = ttk.Button(fr, text=t("find_button"), command=self._start_find)
        self.btn_find.pack(side="left", padx=(4, 0))

        self.table_missing = ResultTable(tab, export=self._export_found, find=self._find_from_table,
                                         with_game=True)
        self.table_missing.pack(fill="both", expand=True, padx=8, pady=(0, 6))

    # --- Tools -----------------------------------------------------------
    def _build_tools_tab(self):
        sf = self._tools_scroll = ScrollFrame(self.tab_tools)
        sf.pack(fill="both", expand=True)
        wrap = ttk.Frame(sf.inner, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=14, pady=12)
        nav = ttk.Frame(wrap, style="Card.TFrame", width=230)
        nav.pack(side="left", fill="y")
        self.v_tool = tk.StringVar(value=tools.tool_by_id(self.cfg.get("last_tool") or "convert")["id"])
        for group, key in (("port", "tools_group_port"), ("assets", "tools_group_assets")):
            ttk.Label(nav, text=t(key), style="NavHead.TLabel").pack(anchor="w", padx=6, pady=(8 if group == "port" else 16, 4))
            for td in tools.TOOLS:
                if td["group"] != group:
                    continue
                ttk.Radiobutton(nav, text=t(td["name"]), value=td["id"], variable=self.v_tool,
                                style="Nav.Toolbutton", command=self._tool_selected).pack(fill="x")
        self.tool_panel = ttk.Frame(wrap, style="Inner.TFrame")
        self.tool_panel.pack(side="left", fill="both", expand=True, padx=(14, 0))
        self.btn_tool = None
        self._tool_selected()

    def _tool_selected(self):
        for w in self.tool_panel.winfo_children():
            w.destroy()
        td = tools.tool_by_id(self.v_tool.get())
        self.cfg["last_tool"] = td["id"]
        saved = self.cfg.setdefault("tool_paths", {}).get(td["id"], {})
        p = self.tool_panel
        p.columnconfigure(1, weight=1)
        ttk.Label(p, text=t(td["name"]), style="InnerSection.TLabel").grid(row=0, column=0, columnspan=3,
                                                                          sticky="w", padx=16, pady=(14, 2))
        ttk.Label(p, text=t(td["desc"]), style="Inner.TLabel", wraplength=860,
                  justify="left").grid(row=1, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 2))
        r = 2
        if td.get("hint"):
            ttk.Label(p, text=t(td["hint"]), style="InnerMuted.TLabel", wraplength=860,
                      justify="left").grid(row=r, column=0, columnspan=3, sticky="w", padx=16)
            r += 1
        self.v_tin = tk.StringVar(value=saved.get("in", ""))
        self.v_tout = tk.StringVar(value=saved.get("out", ""))
        ttk.Label(p, text=t("tool_in"), style="Inner.TLabel").grid(row=r, column=0, sticky="w", padx=(16, 8), pady=(12, 3))
        ttk.Entry(p, textvariable=self.v_tin).grid(row=r, column=1, sticky="ew", pady=(12, 3))
        ttk.Button(p, text=t("browse"), command=lambda: self._pick_dir(self.v_tin)).grid(row=r, column=2, padx=(8, 16), pady=(12, 3))
        r += 1
        ttk.Label(p, text=t("tool_out"), style="Inner.TLabel").grid(row=r, column=0, sticky="w", padx=(16, 8), pady=3)
        ttk.Entry(p, textvariable=self.v_tout).grid(row=r, column=1, sticky="ew", pady=3)
        ttk.Button(p, text=t("browse"), command=lambda: self._pick_dir(self.v_tout)).grid(row=r, column=2, padx=(8, 16), pady=3)
        r += 1
        ttk.Label(p, text=t("tool_out_hint"), style="InnerMuted.TLabel").grid(row=r, column=1, sticky="w")
        r += 1

        opts = td["options"]
        sopts = saved.get("opts", {})
        self.tool_vars = {}
        self.v_mode = None
        if opts.get("modes"):
            # what is turned into what
            fr = ttk.Frame(p, style="Inner.TFrame")
            fr.grid(row=r, column=0, columnspan=3, sticky="w", padx=16, pady=(10, 0))
            ttk.Label(fr, text=t("tool_mode"), style="Inner.TLabel").pack(side="left")
            self._mode_labels = {m: t("cv_" + m) for m in opts["modes"]}
            mode = sopts.get("mode") if sopts.get("mode") in opts["modes"] else opts["modes"][0]
            self.v_mode = tk.StringVar(value=self._mode_labels[mode])
            cb = ttk.Combobox(fr, textvariable=self.v_mode, values=list(self._mode_labels.values()),
                              state="readonly", width=30)
            cb.pack(side="left", padx=(8, 12))
            self.lbl_mode = ttk.Label(fr, text="", style="InnerMuted.TLabel", wraplength=560, justify="left")
            self.lbl_mode.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda e: self._mode_changed())
            r += 1
        if opts.get("formats"):
            fr = ttk.Frame(p, style="Inner.TFrame")
            fr.grid(row=r, column=0, columnspan=3, sticky="w", padx=16, pady=(10, 0))
            all_label = t("tool_all_formats")
            self._fmt_from_values = [all_label] + list(tools.IMAGE_IN.keys())
            ttk.Label(fr, text=t("tool_from"), style="Inner.TLabel").pack(side="left")
            src = sopts.get("from", "TGA")
            self.v_from = tk.StringVar(value=all_label if src == "ALL" else src)
            ttk.Combobox(fr, textvariable=self.v_from, values=self._fmt_from_values, state="readonly",
                         width=8).pack(side="left", padx=(6, 10))
            ttk.Label(fr, text="→", style="Inner.TLabel").pack(side="left")
            ttk.Label(fr, text=t("tool_to"), style="Inner.TLabel").pack(side="left", padx=(10, 0))
            self.v_to = tk.StringVar(value=sopts.get("to", "PNG"))
            ttk.Combobox(fr, textvariable=self.v_to, values=list(tools.IMAGE_OUT), state="readonly",
                         width=8).pack(side="left", padx=6)
            r += 1
        checks = ttk.Frame(p, style="Inner.TFrame")
        checks.grid(row=r, column=0, columnspan=3, sticky="w", padx=14, pady=(8, 0))
        r += 1
        self._tool_checks = {}
        for key, label in (("recursive", "tool_recursive"), ("overwrite", "tool_overwrite"),
                           ("fix_rotation", "tool_fix_rotation")):
            if key in opts:
                v = tk.BooleanVar(value=bool(sopts.get(key, opts[key])))
                self.tool_vars[key] = v
                cbx = ttk.Checkbutton(checks, text=t(label), variable=v, style="Inner.TCheckbutton")
                cbx.pack(anchor="w")
                self._tool_checks[key] = cbx
        if self.v_mode is not None:
            self._mode_changed()

        act = ttk.Frame(p, style="Inner.TFrame")
        act.grid(row=r, column=0, columnspan=3, sticky="w", padx=16, pady=16)
        self.btn_tool = ttk.Button(act, text=t("tool_run"), style="Accent.TButton", command=self._run_tool)
        self.btn_tool.pack(side="left")
        if self.worker and self.worker.is_alive():
            self.btn_tool.config(state="disabled")
        ttk.Button(act, text=t("tool_open_out"), command=self._tool_open_out).pack(side="left", padx=8)
        if td.get("reset"):
            ttk.Button(act, text=t("tool_reset"), command=self._tool_reset).pack(side="left")
        # the converter lists what it made, like a map port (kept while other tools are open)
        old = getattr(self, "table_tool", None)
        if old is not None:
            self._tool_saved = (list(old.rows), old.content_dir)
        self.table_tool = None
        if td.get("full"):
            tb = self.table_tool = ResultTable(p, find=self._find_from_table, in_folder=True)
            tb.grid(row=r + 1, column=0, columnspan=3, sticky="nsew", padx=8, pady=(0, 8))
            rows, cdir = getattr(self, "_tool_saved", None) or ([], "")
            tb.content_dir = cdir
            for row in rows:
                tb.rows.append(row)
            tb.refresh()
        if old is not None and self.current_table is old:
            self.current_table = self.table_tool
        # the panel's height changed: the scroll area has to know
        self.root.after_idle(self._tools_scroll._update)

    def _tool_mode(self):
        if self.v_mode is None:
            return None
        label = self.v_mode.get()
        return next((m for m, lab in self._mode_labels.items() if lab == label), None)

    def _mode_changed(self):
        """The converter's options and hint follow what it turns into what."""
        mode = self._tool_mode()
        self.lbl_mode.config(text=t("cv_" + mode + "_hint"))
        uses = {"vmt_vmat": ("recursive", "overwrite"), "mdl_vmdl": ("recursive", "overwrite"),
                "qc_vmdl": ("recursive", "fix_rotation"), "mdl_qc": ("recursive",), "tex_vmat": ("recursive",)}
        for key, w in self._tool_checks.items():
            if key in uses.get(mode, ()):
                w.pack(anchor="w")
            else:
                w.pack_forget()
        self.root.after_idle(self._tools_scroll._update)

    def _tool_values(self):
        td = tools.tool_by_id(self.v_tool.get())
        opts = {k: bool(v.get()) for k, v in self.tool_vars.items()}
        mode = self._tool_mode()
        if mode:
            opts["mode"] = mode
        if td["options"].get("formats"):
            src = self.v_from.get()
            opts["from"] = "ALL" if src == t("tool_all_formats") else src
            opts["to"] = self.v_to.get()
        return td, self.v_tin.get().strip(), self.v_tout.get().strip(), opts

    def _run_tool(self):
        td, inp, out, opts = self._tool_values()
        if not inp or not os.path.isdir(inp):
            self._msg(t(td["name"]), t("tool_need_in"), "warning")
            return
        self.cfg.setdefault("tool_paths", {})[td["id"]] = {"in": inp, "out": out, "opts": opts}
        config.save(self.cfg)
        if out:
            os.makedirs(out, exist_ok=True)
        name = t(td["name"])

        if opts.get("mode"):
            name = f"{name} ({t('cv_' + opts['mode'])})"
        cfg = self.cfg
        table = self.table_tool if td.get("full") else None
        if table is not None:
            table.clear()
            top = "models" if opts.get("mode") in ("mdl_vmdl", "qc_vmdl", "mdl_qc") else "materials"
            table.content_dir = out or tools._game_root(inp, top) or inp

        def job(ctx):
            ctx.log(f"{name}:  {inp}" + (f"  ->  {out}" if out else ""), "head")
            if td.get("full"):
                td["run"](inp, out, opts, ctx.log, cfg=cfg, cancel=ctx.cancel, result=ctx.result)
            else:
                td["run"](inp, out, opts, ctx.log)
            ctx.log(t("tool_done", name=name), "ok")

        self._start_job(job, name, table, indeterminate=True)

    def _tool_open_out(self):
        _td, inp, out, _o = self._tool_values()
        open_in_explorer(out if out and os.path.isdir(out) else inp)

    def _tool_reset(self):
        _td, inp, _out, _o = self._tool_values()
        if not inp or not os.path.isdir(inp):
            self._msg(t("tool_reset"), t("tool_need_in"), "warning")
            return
        targets = tools.fbx_reset_targets(inp)
        if not targets:
            self._msg(t("tool_reset"), t("tool_reset_none"), "info")
            return
        if not self._ask(t("tool_reset"), t("tool_reset_confirm", list="  " + "\n  ".join(targets)), "warning"):
            return
        self._start_job(lambda ctx: tools.reset_fbx_folders(inp, ctx.log), t("tool_reset_job"), None,
                        indeterminate=True)

    # --- Settings -----------------------------------------------------------------
    def _build_settings_tab(self):
        sf = ScrollFrame(self.tab_settings)
        sf.pack(fill="both", expand=True)
        g = ttk.Frame(sf.inner, style="Card.TFrame")
        g.pack(fill="both", expand=True, padx=18, pady=12)
        g.columnconfigure(1, weight=1)
        r = 0
        # language and theme side by side
        top = ttk.Frame(g, style="Card.TFrame")
        top.grid(row=r, column=0, columnspan=3, sticky="w")
        lang_box = ttk.Frame(top, style="Card.TFrame")
        lang_box.pack(side="left", anchor="n")
        ttk.Label(lang_box, text=t("set_lang"), style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        lf = ttk.Frame(lang_box, style="Card.TFrame")
        lf.pack(anchor="w")
        for code, name in i18n.languages():
            ttk.Radiobutton(lf, text=" " + name, image=self._flag(code, 16), compound="left", value=code,
                            variable=self.v_lang, command=self._change_lang).pack(side="left", padx=(0, 20))
        theme_box = ttk.Frame(top, style="Card.TFrame")
        theme_box.pack(side="left", anchor="n", padx=(40, 0))
        ttk.Label(theme_box, text=t("set_theme"), style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        tf = ttk.Frame(theme_box, style="Card.TFrame")
        tf.pack(anchor="w")
        self.v_theme = tk.StringVar(value=self.cfg["theme"])
        for code in ("dark", "light"):
            ttk.Radiobutton(tf, text=t("theme_" + code), value=code, variable=self.v_theme,
                            command=self._change_theme).pack(side="left", padx=(0, 20))
        r += 1

        ttk.Label(g, text=t("set_folders"), style="Section.TLabel").grid(row=r, column=0, sticky="w", pady=(16, 4))
        r += 1
        self.v_cs2 = tk.StringVar(value=self.cfg.get("cs2_dir", ""))
        lab = ttk.Label(g, text=t("set_cs2"), style="Card.TLabel")
        lab.grid(row=r, column=0, sticky="w", pady=3)
        ToolTip(lab, t("set_cs2_tip"))
        ttk.Entry(g, textvariable=self.v_cs2).grid(row=r, column=1, sticky="ew", padx=8)
        ttk.Button(g, text=t("browse"), command=lambda: self._pick_dir(self.v_cs2)).grid(row=r, column=2, sticky="ew")
        r += 1
        self.lbl_cs2_info = ttk.Label(g, text="", style="CardMuted.TLabel")
        self.lbl_cs2_info.grid(row=r, column=1, sticky="w", padx=8)
        r += 1
        self.v_cs2.trace_add("write", lambda *a: self._update_settings_info())

        lab = ttk.Label(g, text=t("set_games"), style="Card.TLabel")
        lab.grid(row=r, column=0, sticky="nw", pady=(6, 3))
        self.lbl_games = ttk.Label(g, text="", style="CardMuted.TLabel", wraplength=760, justify="left")
        self.lbl_games.grid(row=r, column=1, columnspan=2, sticky="w", padx=8, pady=(6, 3))
        r += 1

        # extra folders with Source 1 files, searched before the installed games
        lab = ttk.Label(g, text=t("set_resources"), style="Card.TLabel")
        lab.grid(row=r, column=0, sticky="nw", pady=(8, 3))
        ToolTip(lab, t("set_resources_hint"))
        rf = ttk.Frame(g, style="Card.TFrame")
        rf.grid(row=r, column=1, columnspan=2, sticky="ew", padx=8, pady=(8, 3))
        rf.columnconfigure(0, weight=1)
        self.lst_res = tk.Listbox(rf, height=3, bg=FIELD, fg=FG, selectbackground=ACCENT,
                                  selectforeground=ON_ACCENT, relief="flat", borderwidth=0, activestyle="none",
                                  highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                                  font=("Segoe UI", 9))
        self.lst_res.grid(row=0, column=0, sticky="ew")
        for d in self.cfg.get("resource_dirs") or []:
            self.lst_res.insert("end", d)
        self.lst_res.bind("<Button-3>", self._res_menu)
        rb = ttk.Frame(rf, style="Card.TFrame")
        rb.grid(row=0, column=1, sticky="n", padx=(8, 0))
        ttk.Button(rb, text=t("set_res_add"), style="Small.TButton", command=self._res_add).pack(fill="x")
        ttk.Button(rb, text=t("set_res_remove"), style="Small.TButton",
                   command=self._res_remove).pack(fill="x", pady=(4, 0))
        self.lbl_res = ttk.Label(rf, text="", style="CardMuted.TLabel", wraplength=760, justify="left")
        self.lbl_res.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self._update_res_info()
        r += 1

        ttk.Label(g, text=t("set_options"), style="Section.TLabel").grid(row=r, column=0, sticky="w", pady=(16, 4))
        r += 1
        # two columns: what is written and how (left), what the map gets (right)
        of = ttk.Frame(g, style="Card.TFrame")
        of.grid(row=r, column=0, columnspan=3, sticky="ew")
        of.columnconfigure(0, weight=1, uniform="opts")
        of.columnconfigure(1, weight=1, uniform="opts")
        left = ttk.Frame(of, style="Card.TFrame")
        left.grid(row=0, column=0, sticky="nw")
        right = ttk.Frame(of, style="Card.TFrame")
        right.grid(row=0, column=1, sticky="nw", padx=(24, 0))
        self.opt_vars = {}
        tips = {"light_probes": "opt_light_probes_tip", "post_processing": "opt_post_processing_tip",
                "bhop_blocks": "opt_bhop_blocks_tip"}

        def add_opt(parent, key, label, pad=0):
            v = tk.BooleanVar(value=bool(self.cfg["opts"].get(key, config.DEFAULTS["opts"].get(key, False))))
            self.opt_vars[key] = v
            cb = ttk.Checkbutton(parent, text=t(label), variable=v, command=self._sync_opts)
            cb.pack(anchor="w", padx=(pad, 0))
            if key in tips:
                ToolTip(cb, t(tips[key]))
            return cb

        ttk.Label(left, text=t("set_opts_files"), style="CardMuted.TLabel").pack(anchor="w", pady=(0, 2))
        for key, label in (("overwrite", "opt_overwrite"), ("convert_cs2_existing", "opt_cs2_existing"),
                           ("compile_assets", "opt_compile_assets"), ("split_triggers", "opt_split_triggers"),
                           ("bhop_blocks", "opt_bhop_blocks")):
            add_opt(left, key, label)
        # "bhop script" in this option opens the script's workshop page
        v = tk.BooleanVar(value=bool(self.cfg["opts"].get("bhop_script", True)))
        self.opt_vars["bhop_script"] = v
        row = ttk.Frame(left, style="Card.TFrame")
        row.pack(anchor="w")
        ttk.Checkbutton(row, text=t("opt_bhop_pre"), variable=v, command=self._sync_opts).pack(side="left")
        self._link(ttk.Label(row, text=t("opt_bhop_link"), style="Link.TLabel"),
                   config.BHOP_SCRIPT_PAGE).pack(side="left")
        if t("opt_bhop_post"):
            ttk.Label(row, text=t("opt_bhop_post"), style="Card.TLabel").pack(side="left")
        cb = add_opt(left, "bhop_download", "opt_bhop_download", pad=24)
        ToolTip(cb, t("opt_bhop_download_tip"))

        ttk.Label(right, text=t("set_opts_look"), style="CardMuted.TLabel").pack(anchor="w", pady=(0, 2))
        for key, label in (("fix_light_brightness", "opt_light_brightness"), ("light_probes", "opt_light_probes"),
                           ("port_particles", "opt_port_particles"), ("post_processing", "opt_post_processing")):
            add_opt(right, key, label)
        r += 1
        # work files of the ports (decompiled map, files taken from the .bsp) pile up over time
        ttk.Label(g, text=t("set_work"), style="Section.TLabel").grid(row=r, column=0, sticky="w", pady=(16, 4))
        r += 1
        wf = ttk.Frame(g, style="Card.TFrame")
        wf.grid(row=r, column=0, columnspan=3, sticky="w")
        self.lbl_work = ttk.Label(wf, text="", style="Card.TLabel")
        self.lbl_work.pack(side="left")
        ttk.Button(wf, text=t("set_work_open"), command=self._open_work).pack(side="left", padx=(12, 6))
        ttk.Button(wf, text=t("set_work_clean"), command=self._clean_work).pack(side="left")
        r += 1
        ttk.Label(g, text=t("set_work_info"), style="CardMuted.TLabel", wraplength=760, justify="left").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        r += 1
        self._update_work_info()

        bot = ttk.Frame(g, style="Card.TFrame")
        bot.grid(row=r, column=0, columnspan=3, sticky="w", pady=(16, 0))
        ttk.Button(bot, text=t("set_save"), style="Accent.TButton", command=self._save_settings).pack(side="left")
        ttk.Button(bot, text=t("set_detect"), command=self._autodetect).pack(side="left", padx=8)
        self.lbl_settings = ttk.Label(bot, text="", style="CardMuted.TLabel")
        self.lbl_settings.pack(side="left", padx=12)
        self._update_settings_info()

    def _update_settings_info(self):
        cs2 = self.v_cs2.get().strip()
        ok = config.is_valid_cs2(cs2)
        self.lbl_cs2_info.config(text="✔ Counter-Strike 2" if ok else "✖ " + t("set_not_found"))
        games = config.s1_game_names(self._s1_dirs())
        text = t("set_games_info")
        text += "\n" + (t("set_found", names=", ".join(games)) if games else t("set_games_none"))
        self.lbl_games.config(text=text)

    # --- resource folders --------------------------------------------------------------
    def _res_add(self):
        d = filedialog.askdirectory(parent=self.root, initialdir=os.path.expanduser("~"), title=t("pick_folder"))
        if not d:
            return
        d = os.path.normpath(d)
        dirs = list(self.cfg.get("resource_dirs") or [])
        if os.path.normcase(d) not in {os.path.normcase(x) for x in dirs}:
            dirs.append(d)
            self.lst_res.insert("end", d)
        self.cfg["resource_dirs"] = dirs
        config.save(self.cfg)
        self._update_res_info()

    def _res_remove(self):
        sel = list(self.lst_res.curselection())
        if not sel:
            return
        dirs = list(self.cfg.get("resource_dirs") or [])
        for i in sorted(sel, reverse=True):
            self.lst_res.delete(i)
            if i < len(dirs):
                del dirs[i]
        self.cfg["resource_dirs"] = dirs
        config.save(self.cfg)
        self._update_res_info()

    def _res_menu(self, e):
        lst = self.lst_res
        i = lst.nearest(e.y)
        menu = popup_menu(lst)
        if 0 <= i < lst.size() and lst.bbox(i) and lst.bbox(i)[1] <= e.y <= lst.bbox(i)[1] + lst.bbox(i)[3]:
            lst.selection_clear(0, "end")
            lst.selection_set(i)
            d = lst.get(i)
            menu.add_command(label=t("menu_open_folder"), command=lambda: open_in_explorer(d),
                             state="normal" if os.path.isdir(d) else "disabled")
            menu.add_command(label=t("menu_copy_path"), command=lambda: copy_text(lst, d))
            menu.add_command(label=t("set_res_remove"), command=self._res_remove)
            menu.add_separator()
        menu.add_command(label=t("set_res_add"), command=self._res_add)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _update_res_info(self):
        dirs = self.cfg.get("resource_dirs") or []
        if not dirs:
            self.lbl_res.config(text=t("set_resources_hint"))
            return
        roots = config.resource_roots(dirs)
        names = ", ".join(os.path.basename(r) or r for r in roots[:8]) + (f" +{len(roots) - 8}" if len(roots) > 8 else "")
        self.lbl_res.config(text=t("set_res_found", n=len(roots), names=names) if roots else t("set_res_none"))

    # --- work files -----------------------------------------------------------------
    @staticmethod
    def _work_folders():
        root = config.work_root()
        try:
            return [os.path.join(root, d) for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
        except OSError:
            return []

    def _update_work_info(self):
        """Counts the work folders and their size in the background (they can be big)."""
        def run():
            folders = self._work_folders()
            size = 0
            for d in folders:
                for dp, _dn, fn in os.walk(d):
                    for f in fn:
                        try:
                            size += os.path.getsize(os.path.join(dp, f))
                        except OSError:
                            pass
            text = t("set_work_size", n=len(folders), size=_human_size(size))
            try:
                self.root.after(0, lambda: self.lbl_work.winfo_exists() and self.lbl_work.config(text=text))
            except (RuntimeError, tk.TclError):
                pass

        self.lbl_work.config(text=t("set_work_counting"))
        threading.Thread(target=run, daemon=True).start()

    def _open_work(self):
        root = config.work_root()
        os.makedirs(root, exist_ok=True)
        open_in_explorer(root)

    def _clean_work(self):
        if self.worker and self.worker.is_alive():
            self._msg(t("busy_title"), t("busy_msg"), "info")
            return
        folders = self._work_folders()
        if not folders:
            self._msg(t("set_work"), t("set_work_empty"), "info")
            return
        if not self._ask(t("set_work"), t("set_work_confirm", n=len(folders))):
            return
        failed = 0
        for d in folders:
            shutil.rmtree(d, ignore_errors=True)
            failed += os.path.isdir(d)
        if failed:
            self._msg(t("set_work"), t("set_work_failed", n=failed), "warning")
        self._update_work_info()

    def _s1_dirs(self):
        if not hasattr(self, "_s1_cache"):
            self._s1_cache = config.find_s1_gamedirs()
        return self._s1_cache

    # --- Help -------------------------------------------------------------------
    def _about_image(self, name, size, color=None, round_=False):
        """A picture of assets/about at size (scaled), tinted with color (icons), with rounded
        corners (round_)."""
        key = ("about", name, size, color, round_)
        if key not in self._flag_imgs:
            img = None
            try:
                from PIL import Image, ImageDraw, ImageTk
                px = max(12, int(size * self._scale))
                im = Image.open(os.path.join(config.ASSETS_DIR, "about", name + ".png")).convert("RGBA")
                im = im.resize((px, px), Image.LANCZOS)
                if color:
                    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
                    solid = Image.new("RGBA", im.size, rgb + (255,))
                    solid.putalpha(im.getchannel("A"))
                    im = solid
                if round_:
                    big = Image.new("L", (px * 4, px * 4), 0)
                    ImageDraw.Draw(big).rounded_rectangle((0, 0, px * 4 - 1, px * 4 - 1), radius=px, fill=255)
                    im.putalpha(big.resize((px, px), Image.LANCZOS))
                img = ImageTk.PhotoImage(im)
            except Exception:  # noqa: BLE001
                img = None
            self._flag_imgs[key] = img
        return self._flag_imgs[key] or ""

    def _build_about(self, parent):
        """Who made the program, a donation link and the maker's pages."""
        card = ttk.Frame(parent, style="About.TFrame", padding=(16, 14))
        top = ttk.Frame(card, style="About.TFrame")
        top.pack(anchor="w")
        av = self._about_image("avatar", 64, round_=True)
        lab = ttk.Label(top, image=av, style="About.TLabel", cursor="hand2")
        lab.pack(side="left")
        self._link(lab, config.AUTHOR_PAGE)
        ToolTip(lab, t("about_steam_tip"))
        names = ttk.Frame(top, style="About.TFrame")
        names.pack(side="left", padx=(12, 0))
        ttk.Label(names, text=t("about_by"), style="AboutMuted.TLabel").pack(anchor="w")
        self._link(ttk.Label(names, text="tehlikeli91", style="AboutTitle.TLabel", cursor="hand2"),
                   config.AUTHOR_PAGE).pack(anchor="w")
        steam = ttk.Frame(names, style="About.TFrame")
        steam.pack(anchor="w", pady=(2, 0))
        ttk.Label(steam, image=self._about_image("steam", 14, MUTED), style="About.TLabel").pack(side="left")
        self._link(ttk.Label(steam, text=t("about_steam"), style="AboutMuted.TLabel", cursor="hand2"),
                   config.AUTHOR_PAGE).pack(side="left", padx=(4, 0))

        ttk.Label(card, text=t("about_thanks"), style="AboutMuted.TLabel", wraplength=int(250 * self._scale),
                  justify="left").pack(anchor="w", pady=(12, 6))
        donate = ttk.Button(card, text=t("about_donate"), style="Accent.TButton",
                            command=lambda: webbrowser.open(config.DONATE_PAGE))
        donate.pack(anchor="w", fill="x")
        ToolTip(donate, t("about_donate_tip"))

        social = ttk.Frame(card, style="About.TFrame")
        social.pack(anchor="w", pady=(12, 0))
        for icon, color, text, url in (("youtube", None, "@burak.016", config.YOUTUBE_PAGE),
                                       ("github", FG, "bcanusar", config.GITHUB_PAGE)):
            row = ttk.Frame(social, style="About.TFrame")
            row.pack(anchor="w", pady=2)
            ic = ttk.Label(row, image=self._about_image(icon, 18, color), style="About.TLabel", cursor="hand2")
            ic.pack(side="left")
            self._link(ic, url)
            self._link(ttk.Label(row, text=text, style="AboutLink.TLabel", cursor="hand2"), url).pack(
                side="left", padx=(8, 0))
        return card

    def _build_help_tab(self):
        body = ttk.Frame(self.tab_help, style="Card.TFrame")
        body.pack(fill="both", expand=True, padx=4, pady=4)
        side = ttk.Frame(body, style="Card.TFrame")
        side.pack(side="right", fill="y", padx=(0, 12), pady=12)
        self._build_about(side).pack(anchor="n")
        txt = tk.Text(body, bg=PANEL, fg=FG, relief="flat", wrap="word", font=("Segoe UI", 10),
                      padx=20, pady=14, borderwidth=0, highlightthickness=0, height=4)
        # the text can be taller than the tab (a big log leaves little room): it gets a scrollbar
        sb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("h", foreground=ACCENT, font=("Segoe UI Semibold", 12), spacing1=10, spacing3=4)
        i = 1
        while t(f"help_h{i}") != f"help_h{i}":
            txt.insert("end", t(f"help_h{i}") + "\n", "h")
            txt.insert("end", t(f"help_p{i}").strip("\n") + "\n")
            i += 1
        txt.configure(state="disabled")

    # ---------------------------------------------------------------------------
    # Settings <-> UI
    # ---------------------------------------------------------------------------
    def _collect_settings(self):
        c = self.cfg
        c["cs2_dir"] = self.v_cs2.get().strip()
        self._sync_opts()

    def _sync_opts(self):
        for k, v in self.opt_vars.items():
            self.cfg["opts"][k] = bool(v.get())

    def _save_settings(self):
        self._collect_settings()
        config.save(self.cfg)
        self._refresh_addons()
        self._update_chips()
        self.lbl_settings.config(text=t("set_saved", time=time.strftime("%H:%M:%S")))

    def _autodetect(self):
        self._collect_settings()
        config.autodetect(self.cfg, only_missing=False)
        self.v_cs2.set(self.cfg.get("cs2_dir", ""))
        self._s1_cache = config.rescan_s1_games()
        self._update_settings_info()
        self._refresh_addons()
        self._update_chips()
        self.log(t("set_detected"), "ok")

    def _change_lang(self):
        lang = self.v_lang.get()
        if lang == i18n.get_lang():
            return
        if self.worker and self.worker.is_alive():
            self._msg(t("busy_title"), t("busy_msg"), "info")
            self.v_lang.set(i18n.get_lang())
            return
        self._collect_settings()
        self.cfg["lang"] = lang
        config.save(self.cfg)
        i18n.set_lang(lang)
        self._rebuild()

    def _change_theme(self):
        theme = self.v_theme.get()
        if theme == self.cfg["theme"] or theme not in THEMES:
            return
        if self.worker and self.worker.is_alive():
            self._msg(t("busy_title"), t("busy_msg"), "info")
            self.v_theme.set(self.cfg["theme"])
            return
        self._collect_settings()
        self.cfg["theme"] = theme
        config.save(self.cfg)
        set_theme(theme)
        self._style()
        dark_title_bar(self.root, theme == "dark")
        self._rebuild()

    def _rebuild(self):
        """Builds the UI again (new language or colors); entered values, tables and the log are kept."""
        state = {
            "tab": self.nb.index("current"), "map_sel": list(self.map_list.selection()),
            "mmap": self.v_mmap.get(), "mextra": self.v_mextra.get(), "maddon": self.v_maddon.get(),
            "find": self.v_find.get(), "find_mode": self.v_find_mode.get(),
            "verbose": bool(self.verbose.get()),
            "tables": [(list(tb.rows), tb.content_dir, tb.query.get(), (tb.sort_col, tb.sort_desc),
                        dict(tb.content_dirs), {g: v.get() for g, v in tb.show_vars.items()}, tb.map_filter.get())
                       for tb in (self.table_port, self.table_missing)],
            "current": (self.table_port, self.table_missing).index(self.current_table)
            if self.current_table in (self.table_port, self.table_missing) else None,
        }
        old, bottom = self.shell, self._bottom_height()
        self._build()
        keep = [s for s in state["map_sel"] if self.map_list.exists(s)]
        if keep:
            self.map_list.selection_set(keep)
        self.v_mextra.set(state["mextra"])
        self.v_maddon.set(state["maddon"])
        self._maddon_selected()
        self.v_mmap.set(state["mmap"])
        self.v_find.set(state["find"])
        self.v_find_mode.set(state["find_mode"])
        self.verbose.set(state["verbose"])
        tables = (self.table_port, self.table_missing)
        for tb, (rows, content_dir, query, sort, dirs, shown, map_filter) in zip(tables, state["tables"]):
            tb.content_dir = content_dir
            tb.content_dirs = dirs
            tb.rows = list(rows)
            tb.sort_col, tb.sort_desc = sort
            tb._update_headings()
            for g, on in shown.items():
                tb.show_vars[g].set(on)
            if tb.with_map:
                names = sorted({r[5] for r in rows if len(r) > 5 and r[5]})
                tb.cb_map["values"] = [tb.all_text] + names
                tb.map_filter.set(map_filter if map_filter in names else tb.all_text)
                tb._sync_game_col()
            tb.query.set(query)
            tb.refresh()
        if state["current"] is not None:
            self.current_table = tables[state["current"]]
        self._restore_log()
        self.nb.select(state["tab"])
        self._show_shell(old, bottom)

    def _restore_log(self):
        self.log_text.configure(state="normal")
        for stamp, msg, tag in self.log_entries:
            self.log_text.insert("end", stamp, "time")
            self.log_text.insert("end", msg + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        if self.v_logfind.get():
            self._find_later()

    def _update_chips(self):
        cs2_ok = config.is_valid_cs2(self.cfg.get("cs2_dir", ""))
        self.chip_cs2.configure(text=("✔ " + t("chip_cs2_ok")) if cs2_ok else ("✖ " + t("chip_cs2_bad")),
                                style="ChipOk.TLabel" if cs2_ok else "ChipBad.TLabel")
        games = config.s1_game_names(self._s1_dirs())
        short = {"Counter-Strike Source": "CSS", "Half-Life 2": "HL2", "GarrysMod": "GMod",
                 "Team Fortress 2": "TF2", "csgo legacy": "CS:GO", "Portal 2": "Portal 2"}
        names = []
        for g in games:
            s = short.get(g, g)
            if s not in names:
                names.append(s)
        shown = ", ".join(names[:5]) + (f" +{len(names) - 5}" if len(names) > 5 else "")
        self.chip_games.configure(text=("✔ " + t("chip_games", names=shown)) if games
                                  else ("✖ " + t("set_games_none")),
                                  style="ChipOk.TLabel" if games else "ChipBad.TLabel")

    def _refresh_addons(self):
        cs2 = self.cfg.get("cs2_dir", "")
        addons = config.list_addons(cs2) if config.is_valid_cs2(cs2) else []
        self.cb_addon["values"] = addons
        self.cb_maddon["values"] = addons

    # ---------------------------------------------------------------------------
    # Pickers
    # ---------------------------------------------------------------------------
    def _pick_bsp(self):
        init = self.cfg.get("last_bsp_dir") or os.path.expanduser("~")
        ps = filedialog.askopenfilenames(parent=self.root, initialdir=init, title=t("port_pick_title"),
                                         filetypes=[(t("ft_map"), "*.bsp *.vmf"), (t("all_files"), "*.*")])
        if ps:
            self.cfg["last_bsp_dir"] = os.path.dirname(ps[0])
            self._add_maps(ps)

    def _pick_bsp_folder(self):
        """Every .bsp of a folder (and its subfolders) goes into the list."""
        init = self.cfg.get("last_bsp_dir") or os.path.expanduser("~")
        d = filedialog.askdirectory(parent=self.root, initialdir=init if os.path.isdir(init) else None,
                                    title=t("pick_folder"))
        if not d:
            return
        found = []
        for dp, _dn, fn in os.walk(d):
            found += [os.path.join(dp, f) for f in sorted(fn) if f.lower().endswith(".bsp")]
        if not found:
            self._msg(t("tab_port"), t("port_folder_none"), "info")
            return
        self.cfg["last_bsp_dir"] = d
        self._add_maps(found)

    # --- the map list -------------------------------------------------------------
    @staticmethod
    def _default_addon(path):
        return pipeline.sanitize_name(os.path.splitext(os.path.basename(path))[0])

    def _add_maps(self, paths):
        have = {os.path.normcase(os.path.normpath(it["path"])) for it in self.port_items}
        new = []
        for p in paths:
            p = os.path.normpath(p)
            if os.path.normcase(p) in have:
                continue
            have.add(os.path.normcase(p))
            self.port_items.append({"path": p, "addon": self._default_addon(p), "state": "waiting", "sec": 0.0})
            new.append(len(self.port_items) - 1)
        self._fill_map_list()
        if new:
            self.map_list.selection_set([str(i) for i in new])
            self.map_list.see(str(new[-1]))

    def _status_of(self, it):
        if not os.path.isfile(it["path"]):
            return t("map_missing"), "missing"
        st = it.get("state", "waiting")
        if st == "done":
            return t("map_done", sec=self._clock(it.get("sec", 0))), "done"
        return t("map_" + st), st

    def _fill_map_list(self):
        if not hasattr(self, "map_list"):
            return
        sel = set(self.map_list.selection())
        self.map_list.delete(*self.map_list.get_children())
        for i, it in enumerate(self.port_items):
            text, tag = self._status_of(it)
            name = os.path.splitext(os.path.basename(it["path"]))[0]
            self.map_list.insert("", "end", iid=str(i), values=(name, it["addon"], text, it["path"]), tags=(tag,))
        keep = [s for s in sel if self.map_list.exists(s)]
        if keep:
            self.map_list.selection_set(keep)
        n = len(self.port_items)
        self.map_list.configure(height=max(3, min(8, n)))
        self._map_selected()

    def _update_map_row(self, i):
        if not self.map_list.exists(str(i)):
            return
        it = self.port_items[i]
        text, tag = self._status_of(it)
        self.map_list.item(str(i), values=(os.path.splitext(os.path.basename(it["path"]))[0], it["addon"], text,
                                           it["path"]), tags=(tag,))

    def _selected_items(self):
        return [int(s) for s in self.map_list.selection() if s.isdigit() and int(s) < len(self.port_items)]

    def _map_selected(self):
        """The addon field shows (and changes) the addon of the selected maps."""
        sel = self._selected_items()
        self._addon_sync = True
        try:
            if not sel:
                self.v_addon.set("")
            else:
                addons = {self.port_items[i]["addon"] for i in sel}
                self.v_addon.set(addons.pop() if len(addons) == 1 else "")
        finally:
            self._addon_sync = False
        self.cb_addon.config(state="normal" if sel else "disabled")
        self._show_map_info(sel)

    def _addon_changed(self):
        if self._addon_sync:
            return
        name = self.v_addon.get().strip()
        sel = self._selected_items()
        if not sel or not name:
            return
        for i in sel:
            self.port_items[i]["addon"] = name
            self._update_map_row(i)

    def _show_map_info(self, sel):
        if len(sel) != 1:
            self.lbl_mapinfo.config(text=t("port_selected", n=len(sel)) if sel else
                                    (t("port_count", n=len(self.port_items)) if self.port_items else ""))
            return
        p = self.port_items[sel[0]]["path"]
        if not os.path.isfile(p):
            self.lbl_mapinfo.config(text=t("map_missing"))
            return
        if p.lower().endswith(".bsp"):
            try:
                info = bspmod.BSPInfo(p)
                zf = info.pakfile()
                n = len([z for z in zf.infolist() if not z.is_dir()]) if zf else 0
                sky = info.worldspawn_keys().get("skyname", "-")
                size = os.path.getsize(p) / (1024 * 1024)
                self.lbl_mapinfo.config(text=t("port_mapinfo", ver=info.version, game=info.game_guess,
                                               size=size, n=n, sky=sky))
            except Exception as e:  # noqa: BLE001
                self.lbl_mapinfo.config(text=t("port_mapinfo_err", e=e))
        else:
            self.lbl_mapinfo.config(text=t("port_mapinfo_vmf"))

    def _remove_maps(self, indices=None):
        if self.worker and self.worker.is_alive():
            return
        drop = set(self._selected_items() if indices is None else indices)
        if not drop:
            return
        self.port_items = [it for i, it in enumerate(self.port_items) if i not in drop]
        self.map_list.selection_set(())
        self._fill_map_list()
        # the map that came after the removed one is selected
        if self.port_items:
            nxt = str(min(min(drop), len(self.port_items) - 1))
            self.map_list.selection_set(nxt)
            self.map_list.focus(nxt)
            self.map_list.see(nxt)

    def _clear_maps(self):
        if self.worker and self.worker.is_alive() or not self.port_items:
            return
        self.port_items = []
        self._fill_map_list()

    def _move_map(self, i, step):
        j = i + step
        if self.worker and self.worker.is_alive() or not (0 <= j < len(self.port_items)):
            return
        items = self.port_items
        items[i], items[j] = items[j], items[i]
        self._fill_map_list()
        self.map_list.selection_set(str(j))

    def _addon_dir(self, addon):
        return os.path.join(self.cfg.get("cs2_dir", ""), "content", "csgo_addons", pipeline.sanitize_name(addon))

    def _map_report(self, it):
        name = self._default_addon(it["path"])
        p = it.get("report") or os.path.join(config.work_root(), name, pipeline.REPORT_FILE)
        return p if os.path.isfile(p) else ""

    def _map_menu(self, e):
        iid = self.map_list.identify_row(e.y)
        busy = bool(self.worker and self.worker.is_alive())
        menu = popup_menu(self.map_list)
        if not iid:
            menu.add_command(label=t("port_add"), command=self._pick_bsp, state="disabled" if busy else "normal")
            menu.add_command(label=t("port_add_folder"), command=self._pick_bsp_folder,
                             state="disabled" if busy else "normal")
            if self.port_items:
                menu.add_command(label=t("port_clear"), command=self._clear_maps, state="disabled" if busy else "normal")
        else:
            if iid not in self.map_list.selection():
                self.map_list.selection_set(iid)
            sel = self._selected_items()
            i = int(iid)
            it = self.port_items[i]
            st = "disabled" if busy else "normal"
            menu.add_command(label=t("menu_port_these", n=len(sel)) if len(sel) > 1 else t("menu_port_this"),
                             command=lambda: self._start_port(sel), state=st)
            menu.add_separator()
            menu.add_command(label=t("menu_open_map_folder"), command=lambda: open_in_explorer(it["path"]),
                             state="normal" if os.path.exists(it["path"]) else "disabled")
            adir = self._addon_dir(it["addon"])
            menu.add_command(label=t("menu_open_addon"), command=lambda: open_in_explorer(adir),
                             state="normal" if os.path.isdir(adir) else "disabled")
            rep = self._map_report(it)
            menu.add_command(label=t("port_open_report"), command=lambda: os.startfile(rep),  # noqa: S606
                             state="normal" if rep else "disabled")
            menu.add_separator()
            menu.add_command(label=t("menu_copy_path") if len(sel) == 1 else t("menu_copy_paths", n=len(sel)),
                             command=lambda: copy_text(self.map_list, "\n".join(self.port_items[k]["path"] for k in sel)))
            menu.add_command(label=t("menu_copy_name"),
                             command=lambda: copy_text(self.map_list, "\n".join(
                                 os.path.splitext(os.path.basename(self.port_items[k]["path"]))[0] for k in sel)))
            menu.add_command(label=t("menu_copy_addon_path"), command=lambda: copy_text(self.map_list, adir))
            menu.add_separator()
            menu.add_command(label=t("menu_up"), command=lambda: self._move_map(i, -1),
                             state="normal" if i > 0 and not busy else "disabled")
            menu.add_command(label=t("menu_down"), command=lambda: self._move_map(i, 1),
                             state="normal" if i < len(self.port_items) - 1 and not busy else "disabled")
            menu.add_command(label=t("port_remove"), command=self._remove_maps, state=st)
            done = [k for k, x in enumerate(self.port_items) if x.get("state") == "done"]
            if done:
                menu.add_command(label=t("menu_remove_done", n=len(done)), command=lambda: self._remove_maps(done),
                                 state=st)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _pick_dir(self, var):
        cur = var.get().strip()
        init = cur if os.path.isdir(cur) else os.path.expanduser("~")
        d = filedialog.askdirectory(parent=self.root, initialdir=init, title=t("pick_folder"))
        if d:
            var.set(os.path.normpath(d))

    def _pick_extra_bsp(self):
        init = self.cfg.get("last_bsp_dir") or os.path.expanduser("~")
        p = filedialog.askopenfilename(parent=self.root, initialdir=init, title=t("port_pick_title"),
                                       filetypes=[(t("ft_map"), "*.bsp"), (t("all_files"), "*.*")])
        if p:
            self.v_mextra.set(os.path.normpath(p))

    def _addon_maps(self, addon):
        """Maps of an addon (maps/*.vmap, prefabs not included)."""
        d = os.path.join(self.cfg.get("cs2_dir", ""), "content", "csgo_addons", addon, "maps")
        if not addon or not os.path.isdir(d):
            return []
        return sorted(f[:-5] for f in os.listdir(d) if f.lower().endswith(".vmap"))

    def _maddon_selected(self):
        """The map list follows the addon; the map with the addon's name comes first."""
        addon = self.v_maddon.get().strip()
        maps = self._addon_maps(addon)
        self.cb_mmap["values"] = maps
        cur = self.v_mmap.get()
        if cur not in maps:
            self.v_mmap.set(addon if addon in maps else (maps[0] if maps else ""))

    def _open_addon(self):
        cs2 = self.cfg.get("cs2_dir", "")
        sel = self._selected_items()
        it = self.port_items[sel[0]] if sel else (self.port_items[0] if len(self.port_items) == 1 else None)
        base = os.path.join(cs2, "content", "csgo_addons")
        p = self._addon_dir(it["addon"]) if it else base
        open_in_explorer(p if os.path.isdir(p) else base)

    def _open_report(self):
        sel = self._selected_items()
        p = self._map_report(self.port_items[sel[0]]) if sel else ""
        if not p:
            p = self.last_report if self.last_report and os.path.isfile(self.last_report) else ""
        if p:
            os.startfile(p)  # noqa: S606
        else:
            self._msg(t("port_open_report"), t("no_report"), "info")

    # ---------------------------------------------------------------------------
    # Jobs
    # ---------------------------------------------------------------------------
    def _start_port(self, indices=None):
        """Ports the maps of the list (or only these indices), each into its addon."""
        if self.worker and self.worker.is_alive():
            self._msg(t("busy_title"), t("busy_msg"), "info")
            return
        idx = list(range(len(self.port_items))) if indices is None else list(indices)
        if not idx:
            self._msg(t("tab_port"), t("port_need_map"), "warning")
            return
        missing = [i for i in idx if not os.path.isfile(self.port_items[i]["path"])]
        if missing:
            self._msg(t("tab_port"), t("port_maps_missing", n=len(missing)), "warning")
            return
        self._collect_settings()
        config.save(self.cfg)
        self.table_port.clear()
        for i in idx:
            self.port_items[i].update(state="waiting", sec=0.0)
            self._update_map_row(i)
        jobs = [(self.port_items[i]["path"], self.port_items[i]["addon"]) for i in idx]
        q = self.queue

        def on_state(k, state, sec=0.0):
            q.put(("map_state", idx[k], state, sec))

        title = t("job_port_many", n=len(jobs)) if len(jobs) > 1 else t("job_port")
        self._start_job(lambda ctx: pipeline.port_maps(self.cfg, ctx, jobs, on_state=on_state), title,
                        self.table_port)

    def _set_map_state(self, i, state, sec):
        if 0 <= i < len(self.port_items):
            self.port_items[i].update(state=state, sec=sec)
            self._update_map_row(i)

    def _find_from_table(self, name):
        """Missing Assets tab: search the files with this name."""
        self.v_find.set(name)
        self.v_find_mode.set("contains")
        self.nb.select(self.tab_missing)
        self._start_find()

    def _start_missing(self):
        self._collect_settings()
        config.save(self.cfg)
        addon = self.v_maddon.get().strip()
        name = self.v_mmap.get().strip()
        vmap = os.path.join(self.cfg.get("cs2_dir", ""), "content", "csgo_addons", addon, "maps", name + ".vmap")
        if not config.is_valid_cs2(self.cfg["cs2_dir"]):
            self._msg(t("tab_missing"), t("miss_no_cs2"), "warning")
            return
        if not addon or not name or not os.path.isfile(vmap):
            self._msg(t("tab_missing"), t("miss_need_map"), "warning")
            return
        extra = self.v_mextra.get().strip() or None
        if extra and not (os.path.isdir(extra) or (os.path.isfile(extra) and extra.lower().endswith(".bsp"))):
            extra = None
        self.table_missing.clear()
        self._start_job(lambda ctx: pipeline.complete_missing(self.cfg, ctx, {"vmap": vmap}, addon, None, extra),
                        t("job_missing"), self.table_missing)

    def _start_find(self):
        query = self.v_find.get().strip()
        if not query:
            self._msg(t("tab_missing"), t("find_need_text"), "warning")
            return
        extra = self.v_mextra.get().strip() or None
        exact = self.v_find_mode.get() == "exact"
        self.table_missing.clear()
        self._start_job(lambda ctx: pipeline.find_files(self.cfg, ctx, query, exact, extra),
                        t("job_find"), self.table_missing)

    def _export_found(self, items, flat=False):
        """Copies found files (from folders, packages or a .bsp) into a folder the user picks,
        with their game paths (materials/..., models/...), or only the files when flat."""
        if self.worker and self.worker.is_alive():
            self._msg(t("busy_title"), t("busy_msg"), "info")
            return
        def pick():
            # the folder is asked for after the files that go along are picked
            init = self.cfg.get("last_export_dir") or os.path.expanduser("~")
            d = filedialog.askdirectory(parent=self.root, initialdir=init if os.path.isdir(init) else None,
                                        title=t("export_pick"))
            if not d:
                return None
            out = os.path.normpath(d)
            self.cfg["last_export_dir"] = out
            config.save(self.cfg)
            return out

        extra = self.v_mextra.get().strip() or None
        self._start_job(lambda ctx: pipeline.export_files(ctx, items, lambda: self._on_ui(pick), flat,
                                                          self.cfg, extra),
                        t("job_export"), None)

    def _on_ui(self, fn):
        """Runs fn on the window's thread (from a job) and returns what it returns."""
        ev = threading.Event()
        holder = [None]
        self.queue.put(("call", fn, ev, holder))
        ev.wait()
        return holder[0]

    def _start_job(self, fn, title, table, indeterminate=False):
        if self.worker and self.worker.is_alive():
            self._msg(t("busy_title"), t("busy_msg"), "info")
            return
        self.cancel = threading.Event()
        self.current_table = table
        self._set_busy(True)
        self.indeterminate = indeterminate
        if indeterminate:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.configure(mode="determinate")
            self.progress["value"] = 0
        self.status.config(text=t("status_running", title=title))
        q = self.queue
        verbose = bool(self.verbose.get())

        def log(msg, tag="info"):
            if tag == "tool" and not verbose:
                return
            q.put(("log", msg, tag))

        def progress(f, text=""):
            q.put(("progress", f, text))

        def result(kind, name, status, detail, source, map_name=""):
            q.put(("result", kind, name, status, detail, source, map_name))

        def ask(title_, msg):
            ev = threading.Event()
            holder = [False]
            q.put(("ask", title_, msg, ev, holder))
            ev.wait()
            return holder[0]

        def choose(title_, msg, rows):
            ev = threading.Event()
            holder = [None]
            q.put(("choose", title_, msg, rows, ev, holder))
            ev.wait()
            return holder[0]

        ctx = pipeline.JobContext(log, progress, result, ask, self.cancel, choose)

        def run():
            t0 = time.time()
            ctx.begin()
            try:
                rep = fn(ctx)
                q.put(("done", title, rep, None, time.time() - t0))
            except Cancelled:
                pipeline.write_failed_report(ctx, t("cancelled"))
                q.put(("done", title, None, t("cancelled"), time.time() - t0))
            except ToolError as e:
                pipeline.write_failed_report(ctx, str(e))
                q.put(("done", title, None, str(e), time.time() - t0))
            except Exception as e:  # noqa: BLE001
                tb = traceback.format_exc()
                self._write_crash(tb)
                q.put(("log", tb, "err"))
                pipeline.write_failed_report(ctx, t("unexpected_error", e=e), tb)
                q.put(("done", title, None, t("unexpected_error", e=e), time.time() - t0))
            finally:
                ctx.end()

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        self._job_t0 = time.time()
        self._tick()

    def _stop(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.status.config(text=t("stopping"))

    def _set_busy(self, busy):
        st = "disabled" if busy else "normal"
        for b in (self.btn_port, self.btn_missing, self.btn_tool, self.btn_find):
            if b is not None and b.winfo_exists():
                b.config(state=st)
        for b in (self.btn_stop, self.btn_stop2):
            b.config(state="normal" if busy else "disabled")

    # ---------------------------------------------------------------------------
    # Queue / log
    # ---------------------------------------------------------------------------
    def _poll(self):
        n = 0
        try:
            while n < 400:
                item = self.queue.get_nowait()
                n += 1
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1], item[2])
                elif kind == "progress":
                    if not self.indeterminate:
                        self.progress["value"] = int(max(0.0, min(1.0, item[1])) * 1000)
                    if item[2]:
                        self.status.config(text=item[2][:60])
                elif kind == "result":
                    if self.current_table is not None:
                        self.current_table.add(*item[1:])
                elif kind == "map_state":
                    self._set_map_state(*item[1:])
                elif kind == "ask":
                    _k, title, msg, ev, holder = item
                    holder[0] = self._ask(title, msg)
                    ev.set()
                elif kind == "choose":
                    _k, title, msg, rows, ev, holder = item
                    try:
                        holder[0] = ChecklistDialog(self.root, title, msg, rows,
                                                    self.cfg["theme"] == "dark").result
                    finally:
                        ev.set()
                elif kind == "call":
                    _k, fn, ev, holder = item
                    try:
                        holder[0] = fn()
                    finally:
                        ev.set()
                elif kind == "done":
                    self._job_done(*item[1:])
        except queue.Empty:
            pass
        self.root.after(40 if n else 80, self._poll)

    def _job_done(self, title, report, error, elapsed):
        self._set_busy(False)
        flash_taskbar(self.root)
        self._lit = True
        if getattr(self, "lbl_work", None) is not None and self.lbl_work.winfo_exists():
            self._update_work_info()
        if self.spinner.winfo_exists():
            self.spinner.itemconfigure(self._spin_arc, extent=0)
            self.lbl_timer.config(text=self._clock(elapsed))
        if self.indeterminate:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.indeterminate = False
        if error:
            self._append_log(error, "err")
            self.status.config(text=t("status_failed", title=title))
            self.progress["value"] = 0
            if error != t("cancelled"):
                self._msg(title, error, "error")
            return
        self.progress["value"] = 1000
        self.status.config(text=t("status_done", title=title, sec=elapsed))
        if report is not None:
            if getattr(report, "content_dir", "") and self.current_table is not None:
                self.current_table.content_dir = report.content_dir
            if getattr(report, "report_path", ""):
                self.last_report = report.report_path
        self._refresh_addons()
        if isinstance(report, pipeline.BatchReport) and self.current_table is self.table_port:
            # every map's rows open files in its own addon
            by_path = {os.path.normcase(it["path"]): it for it in self.port_items}
            for p, r, _e in report.reports:
                if r is None:
                    continue
                name = pipeline.sanitize_name(os.path.splitext(os.path.basename(p))[0])
                self.table_port.content_dirs[name] = r.content_dir
                it = by_path.get(os.path.normcase(os.path.normpath(p)))
                if it is not None and r.report_path:
                    it["report"] = r.report_path
                if r.report_path:
                    self.last_report = r.report_path
        if report is not None and getattr(report, "import_ok", None) is False:
            self._msg(title, t("port_import_failed"), "warning")
        notices = getattr(report, "notices", None) if report is not None else None
        if notices:
            self._msg(title, "\n\n".join(notices), "warning")

    def log(self, msg, tag="info"):
        self.queue.put(("log", msg, tag))

    def _append_log(self, msg, tag):
        stamp = time.strftime("%H:%M:%S ")
        self.log_entries.append((stamp, msg, tag))
        if len(self.log_entries) > 6000:
            del self.log_entries[:1000]
        self.log_text.configure(state="normal")
        self.log_text.insert("end", stamp, "time")
        self.log_text.insert("end", msg + "\n", tag)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 6000:
            self.log_text.delete("1.0", f"{lines - 5000}.0")
        if self.v_logfind.get():
            # while searching the view stays on the match; the new lines are searched too
            self._find_later()
        else:
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_entries.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._find_later()

    def _save_log(self):
        p = filedialog.asksaveasfilename(parent=self.root, defaultextension=".txt",
                                         initialfile=f"cs2porter_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
                                         filetypes=[(t("log_file"), "*.txt")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.log_text.get("1.0", "end"))

    def _write_crash(self, tb):
        try:
            with open(os.path.join(config.APP_DIR, "error_log.txt"), "a", encoding="utf-8") as f:
                f.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n{tb}\n")
        except OSError:
            pass

    # ---------------------------------------------------------------------------
    def _startup_checks(self):
        self.log(t("startup_ready", v=__version__), "head")
        cs2 = self.cfg.get("cs2_dir", "")
        if not config.is_valid_cs2(cs2):
            self.log(t("startup_no_cs2"), "warn")
        elif not os.path.isfile(os.path.join(cs2, "game", "bin", "win64", "source1import.exe")):
            self.log(t("startup_no_workshop_tools"), "warn")
        if cs2 and not cs2.isascii():
            self.log(t("startup_cs2_path"), "warn")
        if not config.find_bspsource():
            self.log(t("startup_no_bspsource", path=config.BSPSOURCE_DIR), "warn")
        if not self._s1_dirs():
            self.log(t("startup_no_games"), "warn")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not self._ask(t("exit_title"), t("exit_msg")):
                return
            self.cancel.set()
            self.worker.join(timeout=8)
        try:
            self._collect_settings()
            self.cfg["maximized"] = self.root.state() == "zoomed"
            config.save(self.cfg)
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()


def main():
    _enable_dpi()
    try:
        # own taskbar group, so Windows shows the program icon instead of the Python one
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("tehlikeli91.CS2Porter")
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
