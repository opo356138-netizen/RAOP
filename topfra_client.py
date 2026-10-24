#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topfra_client.py  -  Topfra Sovereign App v3.0

Needs enop.py in the same folder (protocol, crypto, networking, session, import).

Requirements
    Python 3.8+ with Tkinter.
    Recommended (vault encryption):      pip install cryptography
    Optional (PDF import):               pip install pypdf
    Optional (image OCR):                pip install pytesseract pillow  (+ Tesseract engine)

Architecture / memory notes
    * 100 tabs, selected from the dropdown on the far right.
    * Each tab has a mode:  "Text Workspace"  or  "Vault Table".
    * LAZY LOADING: at startup only tab names/modes are read. A tab's content is
      decrypted from SQLite when you open it and released (gc.collect()) when you
      leave it. Only ONE editor / table view exists at a time.
    * Data is stored compactly as JSON (encrypted when the vault has a password).
    * Autosave writes recovery DRAFTS to the vault. "Save Page" (or answering Yes in
      the Word-style prompt) commits the tab and exports it to Sovereign_Vault_Files.
"""

import base64
import gc
import html
import json
import os
import platform
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import font, messagebox, simpledialog, ttk, filedialog

try:
    from enop import (HAS_CRYPTO, VaultCrypto, HttpshClient, EnopSession, TextExtractor,
                      UnsupportedCustomization, ExtractionUnavailable, UNSUPPORTED_MSG,
                      COLOR_TAG_NAMES, STYLE_TAG_NAMES)
except ImportError as _e:                                     # pragma: no cover
    sys.exit(f"enop.py must be in the same folder as topfra_client.py ({_e})")

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
APP_TITLE = "Httpsh Protocol - TOPFRA Sovereign Core v3.0"
TAB_COUNT = 100
MAX_CHARS = 10_000
WARN_RATIO = 0.9
AUTOSAVE_MS = 30_000
MAX_TABLE_COLS = 12
MAX_TABLE_ROWS = 1000

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "topfra_vault.db"
VAULT_DIR = BASE_DIR / "Sovereign_Vault_Files"

MODE_TEXT, MODE_TABLE = "text", "table"
MODE_LABELS = {MODE_TEXT: "Text Workspace", MODE_TABLE: "Vault Table"}

STYLE_TAGS = STYLE_TAG_NAMES
# tag -> (colour on light themes, colour on dark theme)
COLOR_TAGS = {
    "c_red": ("#d00000", "#ff6666"),
    "c_blue": ("#0000e0", "#6699ff"),
    "c_green": ("#007000", "#33ff66"),
    "c_yellow": ("#b39700", "#ffee33"),
    "c_purple": ("#7b1fa2", "#d28cff"),
    "c_orange": ("#e65100", "#ffa040"),
}
assert set(COLOR_TAGS) == set(COLOR_TAG_NAMES)

THEMES = {
    "White & Grey": dict(bg="#f0f0f0", fg="#000000", text_bg="#ffffff", text_fg="#000000",
                         btn_bg="#e1e1e1", dark=False, hit="#fff176", sel="#3399ff"),
    "Pixel Black & White": dict(bg="#000000", fg="#00ff00", text_bg="#000000",
                                text_fg="#00ff00", btn_bg="#1a1a1a", dark=True,
                                hit="#665c00", sel="#1f5f1f"),
    "Gray, Black & White": dict(bg="#c8c8c8", fg="#000000", text_bg="#ffffff",
                                text_fg="#000000", btn_bg="#e6e6e6", dark=False,
                                hit="#b0b0b0", sel="#505050", sel_fg="#ffffff"),
}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def _empty_data() -> dict:
    return {"content": "", "tags": {}, "cursor": 0, "table": None, "notes": ""}


def _sanitize_data(d) -> dict:
    """Validate/normalise a tab's data dict (from DB, drafts or migration)."""
    out = _empty_data()
    if not isinstance(d, dict):
        return out
    out["content"] = str(d.get("content", ""))[:MAX_CHARS]
    n = len(out["content"])
    tags = d.get("tags", {})
    if isinstance(tags, dict):
        for name, vals in tags.items():
            if name in STYLE_TAGS and isinstance(vals, list):
                clean = []
                for s, e in zip(vals[0::2], vals[1::2]):
                    if isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= n:
                        clean += [s, e]
                if clean:
                    out["tags"][name] = clean
    cur = d.get("cursor", 0)
    out["cursor"] = cur if isinstance(cur, int) and 0 <= cur <= n else 0
    tbl = d.get("table")
    if isinstance(tbl, dict) and isinstance(tbl.get("cols"), list) and tbl["cols"]:
        cols = [str(c)[:30] for c in tbl["cols"][:MAX_TABLE_COLS]]
        rows = []
        raw_rows = tbl.get("rows", [])
        if isinstance(raw_rows, list):
            for r in raw_rows[:MAX_TABLE_ROWS]:
                if isinstance(r, list):
                    cells = [str(x).replace("\n", " ").replace("\t", " ")
                             for x in r[:len(cols)]]
                    cells += [""] * (len(cols) - len(cells))
                    rows.append(cells)
        nrows = tbl.get("nrows", len(rows))
        nrows = nrows if isinstance(nrows, int) else len(rows)
        out["table"] = {"cols": cols, "rows": rows,
                        "nrows": max(1, len(rows), min(nrows, MAX_TABLE_ROWS))}
    out["notes"] = str(d.get("notes", ""))[:MAX_CHARS]
    return out


def _line_starts(content: str):
    starts, pos = [], 0
    for line in content.split("\n"):
        starts.append(pos)
        pos += len(line) + 1
    return starts


def _pos_to_offset(starts, pos: str, n: int) -> int:
    """Convert a legacy 'line.col' Tk index to a character offset."""
    try:
        line, col = pos.split(".")
        off = starts[int(line) - 1] + int(col)
        return max(0, min(n, off))
    except (ValueError, IndexError):
        return 0


def _tk_count(widget, a, b) -> int:
    r = widget.count(a, b, "chars")
    if r is None:
        return 0
    return int(r[0]) if isinstance(r, (tuple, list)) else int(r)


def _recolor_tree(widget, th, color_btn_tags):
    for child in widget.winfo_children():
        try:
            if isinstance(child, tk.Button):
                tag = color_btn_tags.get(child)
                fg = COLOR_TAGS[tag][1 if th["dark"] else 0] if tag else th["fg"]
                child.configure(bg=th["btn_bg"], activebackground=th["btn_bg"],
                                fg=fg, activeforeground=fg)
            elif isinstance(child, tk.Label):
                child.configure(bg=th["bg"], fg=th["fg"])
            elif isinstance(child, tk.Entry):
                child.configure(bg=th["text_bg"], fg=th["text_fg"],
                                insertbackground=th["fg"])
            elif isinstance(child, tk.Frame):
                child.configure(bg=th["bg"])
        except tk.TclError:
            pass
        _recolor_tree(child, th, color_btn_tags)


def _style_text(ta, th):
    ta.configure(bg=th["text_bg"], fg=th["text_fg"], insertbackground=th["fg"],
                 selectbackground=th["sel"], inactiveselectbackground=th["sel"])
    if "sel_fg" in th:
        ta.configure(selectforeground=th["sel_fg"])


# ----------------------------------------------------------------------------
# Vault Table view: one Text widget per column, kept row-aligned and scroll-synced
# ----------------------------------------------------------------------------
class TableView(tk.Frame):
    """Lightweight editable table (N columns = N widgets, not N x M cells).

    Row r of every column is line r of that column's Text widget, so the whole
    table scrolls vertically as one while staying horizontally aligned.
    The notes box ("Write your notes here...") sits at the very bottom."""

    def __init__(self, master, theme, f_normal, f_bold, on_change, on_limit, max_chars):
        super().__init__(master)
        self.theme, self.f_normal, self.f_bold = theme, f_normal, f_bold
        self.on_change, self.on_limit, self.max_chars = on_change, on_limit, max_chars
        self.heads, self.cols = [], []
        self.nrows = 1
        self._loading = False
        self._syncing = False
        self._last_col = 0
        self._vcmd = (self.register(lambda P: len(P) <= 30), "%P")

        self.bar = tk.Frame(self)
        self.bar.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))
        for text, cmd in (("+ Row", self.add_row), ("- Row", self.delete_row),
                          ("+ Col", self.add_column), ("- Col", self.remove_column)):
            tk.Button(self.bar, text=f" {text} ", command=cmd,
                      font=("Consolas", 9, "bold")).pack(side=tk.LEFT, padx=2)
        self.info = tk.Label(self.bar, text="", font=("Consolas", 9), anchor="w")
        self.info.pack(side=tk.LEFT, padx=8)

        # notes box at the very bottom (packed before the body so the body takes the rest)
        self.notes_frame = tk.Frame(self)
        self.notes_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        self.notes_label = tk.Label(self.notes_frame, text="Write your notes here...",
                                    anchor="w", font=("Consolas", 10, "bold"))
        self.notes_label.pack(fill=tk.X)
        nsb = tk.Scrollbar(self.notes_frame, orient="vertical")
        nsb.pack(side=tk.RIGHT, fill="y")
        self.notes = tk.Text(self.notes_frame, height=8, wrap="word", undo=True, maxundo=100,
                             font=self.f_normal, exportselection=False, padx=6, pady=4,
                             yscrollcommand=nsb.set)
        self.notes.pack(side=tk.LEFT, fill=tk.X, expand=True)
        nsb.config(command=self.notes.yview)
        self.notes.bind("<<Modified>>", self._on_modified)
        self.notes.bind("<KeyPress>", self._on_notes_key)

        self.body = tk.Frame(self)
        self.body.pack(side=tk.TOP, fill="both", expand=True)
        self.vbar = tk.Scrollbar(self.body, orient="vertical", command=self._yview)
        self.vbar.pack(side=tk.RIGHT, fill="y")
        self.grid_frame = tk.Frame(self.body)
        self.grid_frame.pack(side=tk.LEFT, fill="both", expand=True)

    # ---- model <-> widgets --------------------------------------------------
    def set_model(self, model, notes):
        self._loading = True
        try:
            for w in self.grid_frame.winfo_children():
                w.destroy()
            self.heads, self.cols = [], []
            rows = model.get("rows", [])
            self.nrows = max(1, int(model.get("nrows", 1)), len(rows))
            self.grid_frame.grid_rowconfigure(1, weight=1)
            for c, name in enumerate(model["cols"]):
                self.grid_frame.grid_columnconfigure(c, weight=1, uniform="tcols")
                e = tk.Entry(self.grid_frame, font=self.f_bold, justify="center", width=8,
                             validate="key", validatecommand=self._vcmd)
                e.insert(0, name)
                e.grid(row=0, column=c, sticky="ew", padx=1, pady=(0, 2))
                e.bind("<KeyRelease>", lambda ev: self.on_change())
                t = tk.Text(self.grid_frame, font=self.f_normal, wrap="none", width=8,
                            height=10, undo=True, maxundo=100, exportselection=False,
                            padx=4, pady=2, yscrollcommand=self._on_yscroll)
                t.grid(row=1, column=c, sticky="nsew", padx=1)
                lines = [(rows[r][c] if r < len(rows) and c < len(rows[r]) else "")
                         for r in range(self.nrows)]
                t.insert("1.0", "\n".join(lines))
                t.bind("<KeyPress>", lambda ev, c=c: self._on_key(ev, c))
                t.bind("<<Paste>>", lambda ev, c=c: self._on_paste(ev, c))
                t.bind("<<Cut>>", self._on_cut)
                t.bind("<<Modified>>", self._on_modified)
                t.bind("<FocusIn>", lambda ev, c=c: setattr(self, "_last_col", c))
                self.heads.append(e)
                self.cols.append(t)
            self.notes.delete("1.0", "end")
            self.notes.insert("1.0", notes)
            for t in self.cols + [self.notes]:
                t.edit_reset()
                t.edit_modified(False)
            self._last_col = 0
            self.apply_theme(self.theme)
            self._update_info()
        finally:
            self._loading = False

    def get_model(self):
        names = [e.get() for e in self.heads]
        texts = [t.get("1.0", "end-1c").split("\n") for t in self.cols]
        rows = [[(ct[r] if r < len(ct) else "") for ct in texts] for r in range(self.nrows)]
        while rows and not any(rows[-1]):
            rows.pop()
        return {"cols": names, "rows": rows, "nrows": self.nrows}

    def get_notes(self):
        return self.notes.get("1.0", "end-1c")

    def total_chars(self):
        total = len(self.notes.get("1.0", "end-1c"))
        for t in self.cols:
            total += max(0, len(t.get("1.0", "end-1c")) - (self.nrows - 1))
        for e in self.heads:
            total += len(e.get())
        return total

    # ---- scrolling ----------------------------------------------------------
    def _yview(self, *args):
        self._syncing = True
        try:
            for t in self.cols:
                t.yview(*args)
        finally:
            self._syncing = False

    def _on_yscroll(self, first, last):
        self.vbar.set(first, last)
        if self._syncing:
            return
        self._syncing = True
        try:
            for t in self.cols:
                t.yview_moveto(first)
        finally:
            self._syncing = False

    # ---- keyboard / clipboard guards ---------------------------------------
    @staticmethod
    def _cur_row(t):
        return int(t.index("insert").split(".")[0])

    @staticmethod
    def _multiline_selection(t):
        try:
            a, b = t.index("sel.first"), t.index("sel.last")
        except tk.TclError:
            return False
        return a.split(".")[0] != b.split(".")[0]

    def _on_key(self, e, c):
        t, ks = e.widget, e.keysym
        if ks in ("Return", "KP_Enter"):
            self._goto(c, self._cur_row(t) + 1)
            return "break"
        if ks in ("Tab", "ISO_Left_Tab"):
            back = ks == "ISO_Left_Tab" or bool(e.state & 0x1)
            self._goto(c - 1 if back else c + 1, self._cur_row(t), to_end=True)
            return "break"
        multi = self._multiline_selection(t)
        if ks in ("BackSpace", "Delete"):
            if multi:
                return "break"
            if not t.tag_ranges("sel"):
                if ks == "BackSpace" and t.index("insert").split(".")[1] == "0":
                    return "break"
                if ks == "Delete" and t.compare("insert", "==", "insert lineend"):
                    return "break"
            return None
        if e.state & 0x4:                       # Ctrl combos: handled by virtual events
            return None
        if e.char and e.char.isprintable():
            if multi:
                return "break"
            if not t.tag_ranges("sel") and self.total_chars() >= self.max_chars:
                self.on_limit(False)
                return "break"
        return None

    def _on_notes_key(self, e):
        if e.state & 0x4 or not e.char or not e.char.isprintable():
            return None
        if not self.notes.tag_ranges("sel") and self.total_chars() >= self.max_chars:
            self.on_limit(False)
            return "break"
        return None

    def _on_cut(self, e):
        return "break" if self._multiline_selection(e.widget) else None

    def _goto(self, col, row, to_end=False):
        n = len(self.cols)
        if col >= n:
            col, row = 0, row + 1
        elif col < 0:
            col, row = n - 1, row - 1
        row = max(1, row)
        if row > self.nrows:
            if self.nrows >= MAX_TABLE_ROWS or not self.add_row():
                row = self.nrows
        t = self.cols[col]
        t.focus_set()
        t.mark_set("insert", f"{row}.end" if to_end else f"{row}.0")
        t.see("insert")

    def _on_paste(self, e, c):
        t = e.widget
        try:
            clip = self.clipboard_get()
        except tk.TclError:
            return "break"
        clip = clip.replace("\r\n", "\n").replace("\r", "\n")
        body = clip.rstrip("\n")
        if "\n" not in body and "\t" not in body:               # plain text into one cell
            if self._multiline_selection(t):
                return "break"
            sel = t.tag_ranges("sel")
            removed = _tk_count(t, "sel.first", "sel.last") if sel else 0
            room = max(0, self.max_chars - self.total_chars() + removed)
            text = body[:room]
            self._loading = True
            try:
                if sel:
                    t.delete("sel.first", "sel.last")
                t.insert("insert", text)
            finally:
                self._loading = False
                t.edit_modified(False)
            if len(text) < len(body):
                self.on_limit(True)
            self._changed()
            return "break"
        rows = [ln.split("\t") for ln in body.split("\n")]       # block paste (TSV / Excel)
        r0, truncated = self._cur_row(t), False
        self._loading = True
        try:
            for i, cells in enumerate(rows):
                r = r0 + i
                while r > self.nrows and self.nrows < MAX_TABLE_ROWS:
                    for tt in self.cols:
                        tt.insert("end-1c", "\n")
                    self.nrows += 1
                if r > self.nrows:
                    truncated = True
                    break
                for j, cell in enumerate(cells):
                    col = c + j
                    if col >= len(self.cols):
                        break
                    tt, cell = self.cols[col], cell.strip()
                    old = tt.get(f"{r}.0", f"{r}.end")
                    room = max(0, self.max_chars - self.total_chars() + len(old))
                    if len(cell) > room:
                        cell, truncated = cell[:room], True
                    tt.delete(f"{r}.0", f"{r}.end")
                    tt.insert(f"{r}.0", cell)
        finally:
            self._loading = False
            for tt in self.cols:
                tt.edit_modified(False)
        if truncated:
            self.on_limit(True)
        self._changed()
        return "break"

    # ---- change tracking / integrity ----------------------------------------
    def _on_modified(self, ev):
        t = ev.widget
        if not t.edit_modified():
            return
        t.edit_modified(False)
        if self._loading:
            return
        if t is not self.notes:
            self._normalize(t)
        self._enforce(t)
        t.edit_modified(False)
        self._changed()

    def _normalize(self, t):
        """Keep every column at exactly `nrows` lines (rows can never drift apart)."""
        n = int(t.index("end-1c").split(".")[0])
        if n == self.nrows:
            return
        self._loading = True
        try:
            if n < self.nrows:
                t.insert("end-1c", "\n" * (self.nrows - n))
            else:
                lines = t.get("1.0", "end-1c").split("\n")
                keep = lines[:self.nrows - 1] + [" ".join(lines[self.nrows - 1:])]
                pos = t.index("insert")
                t.delete("1.0", "end")
                t.insert("1.0", "\n".join(keep))
                try:
                    t.mark_set("insert", pos)
                except tk.TclError:
                    pass
        finally:
            self._loading = False
            t.edit_modified(False)

    def _enforce(self, t):
        total = self.total_chars()
        if total <= self.max_chars:
            return
        excess = total - self.max_chars
        self._loading = True
        try:
            if t is self.notes:
                t.delete(f"end-1c-{excess}c", "end-1c")
            else:
                before = _tk_count(t, t.index("insert linestart"), "insert")
                n = min(excess, before)
                if n:
                    t.delete(f"insert-{n}c", "insert")
        finally:
            self._loading = False
            t.edit_modified(False)
        self.on_limit(True)

    def _changed(self):
        self._update_info()
        self.on_change()

    def _update_info(self):
        self.info.config(text=f"{len(self.cols)} columns x {self.nrows} rows   "
                              "(Enter = next row, Tab = next cell)")

    # ---- structure ------------------------------------------------------------
    def add_row(self):
        if self.nrows >= MAX_TABLE_ROWS:
            messagebox.showwarning("Table", f"Maximum of {MAX_TABLE_ROWS} rows reached.")
            return False
        self._loading = True
        try:
            for t in self.cols:
                t.insert("end-1c", "\n")
            self.nrows += 1
            for t in self.cols:
                t.edit_modified(False)
        finally:
            self._loading = False
        self._changed()
        return True

    def delete_row(self):
        if not self.cols:
            return
        t = self.cols[min(self._last_col, len(self.cols) - 1)]
        r = min(self._cur_row(t), self.nrows)
        if not messagebox.askyesno("Delete Row", f"Delete row {r}?", parent=self):
            return
        self._loading = True
        try:
            for tt in self.cols:
                if self.nrows == 1:
                    tt.delete("1.0", "end-1c")
                elif r < self.nrows:
                    tt.delete(f"{r}.0", f"{r + 1}.0")
                else:
                    tt.delete(f"{r - 1}.end", f"{r}.end")
            if self.nrows > 1:
                self.nrows -= 1
            for tt in self.cols:
                tt.edit_modified(False)
        finally:
            self._loading = False
        self._changed()

    def add_column(self):
        if len(self.cols) >= MAX_TABLE_COLS:
            messagebox.showwarning("Table", f"Maximum of {MAX_TABLE_COLS} columns reached.")
            return False
        m = self.get_model()
        m["cols"].append(f"Col {len(m['cols']) + 1}")
        for r in m["rows"]:
            r.append("")
        self.set_model(m, self.get_notes())
        self._changed()
        return True

    def remove_column(self):
        if len(self.cols) <= 1:
            return False
        m = self.get_model()
        if (any(r[-1] for r in m["rows"]) or m["cols"][-1].strip()) and not messagebox.askyesno(
                "Remove Column", "The last column contains data. Remove it anyway?", parent=self):
            return False
        m["cols"].pop()
        for r in m["rows"]:
            r.pop()
        self.set_model(m, self.get_notes())
        self._changed()
        return True

    # ---- search highlight / theme ----------------------------------------------
    def highlight(self, query):
        """Bold + highlight every match; the whole table always stays visible."""
        total, count = 0, tk.IntVar()
        for t in self.cols + [self.notes]:
            t.tag_remove("hit", "1.0", "end")
            if not query:
                continue
            pos = "1.0"
            while True:
                pos = t.search(query, pos, stopindex="end", nocase=True, count=count)
                if not pos or count.get() == 0:
                    break
                end = f"{pos}+{count.get()}c"
                t.tag_add("hit", pos, end)
                total += 1
                pos = end
        return total

    def apply_theme(self, th):
        self.theme = th
        self.configure(bg=th["bg"])
        _recolor_tree(self, th, {})
        for t in self.cols + [self.notes]:
            _style_text(t, th)
            t.configure(highlightthickness=1, highlightbackground="#888888")
            t.tag_configure("hit", background=th["hit"], font=self.f_bold)
            t.tag_raise("sel")


# ----------------------------------------------------------------------------
# Main application
# ----------------------------------------------------------------------------
class TopfraSovereignApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.ready = False

        self.text = None
        self.text_frame = None
        self.table_view = None
        self.db = None
        self.crypto = None
        self.enop = None
        self.session = {}
        self.client = HttpshClient()
        self.tabs = [{"name": f"Tab_{i + 1:03d}", "mode": MODE_TEXT, "data": None,
                      "exists": False, "unsaved": False, "draft": False}
                     for i in range(TAB_COUNT)]
        self.current_index = 0
        self._loaded = False
        self._loading = False
        self._last_count = 0
        self._last_warn = 0.0
        self.filter_active = False
        self.search_info = ""
        self.status_extra = ""
        self._flash_id = None
        self._autosave_id = None
        self._poll_id = None
        self._busy = False
        self._queue = queue.Queue()
        self._color_btn_tags = {}
        self._print_files = []
        self.theme_name = "White & Grey"
        self.theme = THEMES[self.theme_name]

        self._init_fonts()
        self.root.configure(bg=self.theme["bg"])
        self._maximize()
        self.root.update()

        if not self._init_vault():
            self.root.destroy()
            raise SystemExit(0)

        self.enop = EnopSession(self.db, self.crypto)
        self.session = self.enop.restore_or_create()
        self._apply_theme(self._get_meta("theme", self.theme_name))

        self._load_tab_index()
        self._build_ui()
        self._refresh_dropdown()
        self._recover_drafts()
        last = self.enop.ui.get("last_tab", 0)
        self.switch_to_tab(last if isinstance(last, int) and 0 <= last < TAB_COUNT else 0)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close_app)
        self._autosave_id = self.root.after(AUTOSAVE_MS, self._autosave_tick)
        self._poll_id = self.root.after(100, self._poll_queue)
        self.ready = True

    # ------------------------------------------------------------------ setup
    def _maximize(self):
        try:
            self.root.state("zoomed")
        except tk.TclError:
            try:
                self.root.attributes("-zoomed", True)
            except tk.TclError:
                self.root.geometry(f"{self.root.winfo_screenwidth()}x"
                                   f"{self.root.winfo_screenheight()}")

    def _init_fonts(self):
        available = set(font.families(self.root))
        family = next((f for f in ("Consolas", "DejaVu Sans Mono", "Menlo", "Courier New")
                       if f in available), "Courier")
        self.f_normal = font.Font(family=family, size=11)
        self.f_bold = font.Font(family=family, size=11, weight="bold")
        self.f_italic = font.Font(family=family, size=11, slant="italic")
        self.f_bolditalic = font.Font(family=family, size=11, weight="bold", slant="italic")

    def _build_ui(self):
        ui = ("Consolas", 9)
        self.header = tk.Label(
            self.root, font=("Consolas", 11, "bold"),
            text="--- [ TOPFRA SOVEREIGN PROTOCOL: 100-TABS & ADVANCED MULTI-COLOR ENGINE ] ---")
        self.header.pack(pady=3)

        # row 1: formatting + theme, tab dropdown on the far right
        self.row1 = tk.Frame(self.root)
        self.row1.pack(fill=tk.X, padx=5, pady=2)
        self.tab_selector_var = tk.StringVar()
        self.tab_dropdown = ttk.Combobox(self.row1, textvariable=self.tab_selector_var,
                                         state="readonly", width=28, height=20)
        self.tab_dropdown.pack(side=tk.RIGHT, padx=5)
        self.tab_dropdown.bind("<<ComboboxSelected>>", self.on_tab_selected)
        tk.Label(self.row1, text="Select Tab:", font=("Consolas", 9, "bold")).pack(
            side=tk.RIGHT, padx=2)

        self._btn(self.row1, "Bold", lambda: self.toggle_style("bold"), bold=True)
        self._btn(self.row1, "Italic", lambda: self.toggle_style("italic"), italic=True)
        for label, tag in (("Red", "c_red"), ("Blue", "c_blue"), ("Green", "c_green"),
                           ("Yellow", "c_yellow"), ("Purple", "c_purple"),
                           ("Orange", "c_orange")):
            self._btn(self.row1, label, lambda t=tag: self.apply_color(t), bold=True, tag=tag)
        self._btn(self.row1, "Clear Fmt", self.clear_formatting)
        tk.Label(self.row1, text=" | ", font=ui).pack(side=tk.LEFT, padx=2)
        self.theme_var = tk.StringVar(value=self.theme_name)
        self.theme_dropdown = ttk.Combobox(self.row1, textvariable=self.theme_var,
                                           values=list(THEMES), state="readonly", width=20)
        self.theme_dropdown.pack(side=tk.LEFT, padx=2)
        self.theme_dropdown.bind("<<ComboboxSelected>>", self.change_theme_event)

        # row 2: tab mode, table generation, rename, filter
        self.row2 = tk.Frame(self.root)
        self.row2.pack(fill=tk.X, padx=5, pady=2)
        tk.Label(self.row2, text="Mode:", font=ui).pack(side=tk.LEFT, padx=2)
        self.mode_var = tk.StringVar(value=MODE_LABELS[MODE_TEXT])
        self.mode_dropdown = ttk.Combobox(self.row2, textvariable=self.mode_var,
                                          values=list(MODE_LABELS.values()),
                                          state="readonly", width=16)
        self.mode_dropdown.pack(side=tk.LEFT, padx=2)
        self.mode_dropdown.bind("<<ComboboxSelected>>", self.on_mode_selected)
        self._btn(self.row2, "Generate Table", self.generate_table, bold=True)
        tk.Label(self.row2, text=" | ", font=ui).pack(side=tk.LEFT, padx=2)
        self._btn(self.row2, "Rename Tab", self.rename_current_tab, bold=True)
        self.rename_entry = tk.Entry(self.row2, font=ui, width=18)
        self.rename_entry.pack(side=tk.LEFT, padx=2)
        self.rename_entry.bind("<Return>", lambda e: self.rename_current_tab())
        tk.Label(self.row2, text=" | ", font=ui).pack(side=tk.LEFT, padx=2)
        tk.Label(self.row2, text="Filter/Search:", font=ui).pack(side=tk.LEFT, padx=2)
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(self.row2, textvariable=self.search_var, font=ui, width=22)
        self.search_entry.pack(side=tk.LEFT, padx=2)
        self.search_entry.bind("<Escape>", lambda e: self.search_var.set(""))
        self._btn(self.row2, "X", lambda: self.search_var.set(""))

        # row 3: actions
        self.row3 = tk.Frame(self.root)
        self.row3.pack(fill=tk.X, padx=5, pady=2)
        self._btn(self.row3, "Save Page", self.save_current_page, bold=True)
        self._btn(self.row3, "Print", self.print_current)
        self._btn(self.row3, "Import File", lambda: self.import_file(True))
        self._btn(self.row3, "Import Plain", lambda: self.import_file(False))
        tk.Label(self.row3, text=" | ", font=ui).pack(side=tk.LEFT, padx=2)
        self._btn(self.row3, "Fetch Node", self.fetch_httpsh_node)
        self._btn(self.row3, "Send to Node", self.send_to_node)
        self._btn(self.row3, "Session", self.show_session_info)
        self._btn(self.row3, "Open Vault Folder", self.open_vault_folder)

        # status bar BEFORE the workspace, so it always exists and stays visible
        self.status_bar = tk.Label(self.root, text="", font=("Consolas", 10, "bold"),
                                   bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.main_container = tk.Frame(self.root)
        self.main_container.pack(fill="both", expand=True, padx=5, pady=5)
        self.text_frame = tk.Frame(self.main_container)
        scrollbar = tk.Scrollbar(self.text_frame, orient="vertical")
        scrollbar.pack(side=tk.RIGHT, fill="y")
        self.text = tk.Text(self.text_frame, font=self.f_normal, wrap="word", undo=True,
                            maxundo=200, autoseparators=True, exportselection=False,
                            padx=8, pady=6, yscrollcommand=scrollbar.set)
        self.text.pack(side=tk.LEFT, fill="both", expand=True)
        scrollbar.config(command=self.text.yview)

        self.text.bind("<KeyPress>", self.on_text_keypress)
        self.text.bind("<<Modified>>", self.on_text_modified)
        self.text.bind("<Control-s>", lambda e: (self.save_current_page(), "break")[1])
        self.text.bind("<Control-p>", lambda e: (self.print_current(), "break")[1])
        self.text.bind("<Control-f>", lambda e: (self.search_entry.focus_set(), "break")[1])
        self.text.bind("<Control-Next>", lambda e: (self.step_tab(+1), "break")[1])
        self.text.bind("<Control-Prior>", lambda e: (self.step_tab(-1), "break")[1])

        self._style_text_widget()
        self._recolor(self.root)
        self.search_var.trace_add("write", lambda *a: self.apply_filter())

    def _btn(self, parent, text, cmd, bold=False, italic=False, tag=None):
        style = " ".join(s for s, on in (("bold", bold), ("italic", italic)) if on) or "normal"
        b = tk.Button(parent, text=f" {text} ", command=cmd, font=("Consolas", 9, style))
        b.pack(side=tk.LEFT, padx=2)
        if tag:
            self._color_btn_tags[b] = tag
        return b

    # ----------------------------------------------------------------- theme
    def _apply_theme(self, name):
        if name not in THEMES:
            name = "White & Grey"
        self.theme_name, self.theme = name, THEMES[name]
        self.root.configure(bg=self.theme["bg"])
        if self.text is not None:
            self._recolor(self.root)
            self._style_text_widget()
            if self.table_view is not None:
                self.table_view.apply_theme(self.theme)
            self.update_status_bar()

    def _recolor(self, widget):
        _recolor_tree(widget, self.theme, self._color_btn_tags)

    def _style_text_widget(self):
        th, ta = self.theme, self.text
        _style_text(ta, th)
        ta.tag_configure("bold", font=self.f_bold)
        ta.tag_configure("italic", font=self.f_italic)
        ta.tag_configure("bolditalic", font=self.f_bolditalic)
        for name, (light, dark) in COLOR_TAGS.items():
            ta.tag_configure(name, foreground=dark if th["dark"] else light)
        ta.tag_configure("search_hit", background=th["hit"])
        ta.tag_configure("hidden", elide=True)
        ta.tag_raise("bolditalic")
        ta.tag_raise("sel")

    def change_theme_event(self, event=None):
        self._apply_theme(self.theme_var.get())
        self._set_meta("theme", self.theme_name)

    # --------------------------------------------------------------- database
    def _init_vault(self):
        try:
            self.db = sqlite3.connect(str(DB_PATH))
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS meta "
                                "(key TEXT PRIMARY KEY, value TEXT)")
        except sqlite3.Error as e:
            messagebox.showerror("Database Error", f"Cannot open the vault database:\n{e}")
            return False
        if not self._unlock_vault():
            return False
        try:
            self._ensure_schema()
        except sqlite3.Error as e:
            messagebox.showerror("Database Error", f"Cannot prepare the vault:\n{e}")
            return False
        return True

    def _get_meta(self, key, default=None):
        try:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return default
        return row[0] if row else default

    def _set_meta(self, key, value):
        try:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                (key, str(value)))
        except sqlite3.Error:
            pass

    def _unlock_vault(self):
        mode = self._get_meta("mode")
        if mode is None:
            return self._setup_new_vault()
        if mode == "plain":
            self.crypto = None
            return True
        if mode != "enc":
            messagebox.showerror("Vault Error", f"Unknown vault mode: {mode}")
            return False
        if not HAS_CRYPTO:
            messagebox.showerror("Encryption Unavailable",
                                 "This vault is encrypted but the 'cryptography' package is "
                                 "missing.\n\nInstall it with:  pip install cryptography")
            return False
        try:
            salt = base64.b64decode(self._get_meta("salt"))
            check = self._get_meta("check")
        except Exception:
            messagebox.showerror("Vault Error", "Vault metadata is damaged.")
            return False
        for attempt in range(1, 4):
            pw = simpledialog.askstring("Unlock Vault", "Vault password:", show="*",
                                        parent=self.root)
            if pw is None:
                return False
            try:
                crypto = VaultCrypto(VaultCrypto.derive_key(pw, salt))
                if crypto.decrypt(check, "check") == VaultCrypto.CHECK_VALUE:
                    self.crypto = crypto
                    return True
            except Exception:
                pass
            messagebox.showerror("Wrong Password", f"Wrong password (attempt {attempt} of 3).")
        return False

    def _setup_new_vault(self):
        if not HAS_CRYPTO:
            ok = messagebox.askyesno(
                "Encryption Unavailable",
                "The 'cryptography' package is not installed, so the vault cannot be "
                "encrypted.\n\nContinue WITHOUT encryption?\n\n"
                "(To enable it: pip install cryptography, delete topfra_vault.db "
                "if it is new, and restart.)", parent=self.root)
            if ok:
                self._set_meta("mode", "plain")
            return ok
        while True:
            pw = simpledialog.askstring(
                "Create Vault Password",
                "Choose a vault password (minimum 8 characters).\n"
                "There is NO recovery if you forget it.\n\n"
                "Leave empty to store everything as plain text.",
                show="*", parent=self.root)
            if pw is None:
                return False
            if pw == "":
                if messagebox.askyesno("No Encryption",
                                       "Tabs, session data and exported files will be stored as "
                                       "PLAIN TEXT.\nThis choice is permanent for this vault. "
                                       "Continue?", parent=self.root):
                    self._set_meta("mode", "plain")
                    return True
                continue
            if len(pw) < 8:
                messagebox.showwarning("Weak Password", "Use at least 8 characters.")
                continue
            pw2 = simpledialog.askstring("Confirm Password", "Re-enter the password:",
                                         show="*", parent=self.root)
            if pw2 is None:
                return False
            if pw != pw2:
                messagebox.showerror("Mismatch", "The passwords do not match.")
                continue
            salt = os.urandom(16)
            self.crypto = VaultCrypto(VaultCrypto.derive_key(pw, salt))
            self._set_meta("salt", base64.b64encode(salt).decode("ascii"))
            self._set_meta("check", self.crypto.encrypt(VaultCrypto.CHECK_VALUE, "check"))
            self._set_meta("mode", "enc")
            return True

    # compact (JSON) + optionally encrypted storage helpers
    def _seal(self, obj, aad):
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        return self.crypto.encrypt(s, aad) if self.crypto else s

    def _open(self, blob, aad):
        return json.loads(self.crypto.decrypt(blob, aad) if self.crypto else blob)

    def _ensure_schema(self):
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(tabs)")}
        legacy = []
        if cols and "blob" in cols and "meta" not in cols:        # v2.4 layout -> migrate
            with self.db:
                self.db.execute("ALTER TABLE tabs RENAME TO tabs_v1_backup")
            legacy = self.db.execute("SELECT tab_index, blob FROM tabs_v1_backup").fetchall()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS tabs (tab_index INTEGER PRIMARY KEY, "
                            "meta TEXT NOT NULL, data TEXT NOT NULL, updated TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS drafts (tab_index INTEGER PRIMARY KEY, "
                            "meta TEXT NOT NULL, data TEXT NOT NULL, updated TEXT)")
        failed = 0
        for idx, blob in legacy:
            try:
                raw = self.crypto.decrypt(blob, f"tab:{idx}") if self.crypto else blob
                rec = json.loads(raw)
                content = str(rec.get("content", ""))[:MAX_CHARS]
                starts, tags = _line_starts(content), {}
                for name, vals in (rec.get("tags") or {}).items():
                    if name in STYLE_TAGS and isinstance(vals, list):
                        tags[name] = [_pos_to_offset(starts, str(v), len(content))
                                      for v in vals]
                data = _sanitize_data({"content": content, "tags": tags, "cursor": 0})
                meta = {"name": self._clean_name(rec.get("name")) or f"Tab_{idx + 1:03d}",
                        "mode": MODE_TEXT}
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO tabs(tab_index, meta, data, updated)"
                                    " VALUES (?, ?, ?, ?)",
                                    (idx, self._seal(meta, f"meta:{idx}"),
                                     self._seal(data, f"data:{idx}"), self._stamp()))
            except Exception:
                failed += 1
        if failed:
            messagebox.showwarning("Migration", f"{failed} tab(s) from the previous version "
                                                "could not be converted (kept in tabs_v1_backup).")

    @staticmethod
    def _stamp():
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _clean_name(value):
        return re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()[:40]

    def _load_tab_index(self):
        """Lazy loading: read ONLY names and modes at startup."""
        bad = 0
        for idx, meta in self.db.execute("SELECT tab_index, meta FROM tabs").fetchall():
            if not (0 <= idx < TAB_COUNT):
                continue
            try:
                m = self._open(meta, f"meta:{idx}")
                t = self.tabs[idx]
                t["name"] = self._clean_name(m.get("name")) or t["name"]
                t["mode"] = m.get("mode") if m.get("mode") in MODE_LABELS else MODE_TEXT
                t["exists"] = True
            except Exception:
                bad += 1
        if bad:
            messagebox.showwarning("Load Warning",
                                   f"{bad} saved tab(s) could not be read and were skipped.")

    def _load_data(self, idx):
        row = self.db.execute("SELECT data FROM tabs WHERE tab_index=?", (idx,)).fetchone()
        if not row:
            return _empty_data()
        try:
            return _sanitize_data(self._open(row[0], f"data:{idx}"))
        except Exception as e:
            messagebox.showwarning("Load Warning",
                                   f"Tab {idx + 1} could not be read ({e}).\n"
                                   "Saving this tab will overwrite the unreadable copy.")
            return _empty_data()

    def _get_data(self, idx):
        t = self.tabs[idx]
        if t["data"] is None:
            t["data"] = self._load_data(idx)
        return t["data"]

    def _recover_drafts(self):
        rows = self.db.execute("SELECT tab_index, meta, data FROM drafts").fetchall()
        if not rows:
            return
        valid = []
        for idx, meta, data in rows:
            try:
                if 0 <= idx < TAB_COUNT:
                    valid.append((idx, self._open(meta, f"dmeta:{idx}"),
                                  _sanitize_data(self._open(data, f"ddata:{idx}"))))
            except Exception:
                pass
        if valid and messagebox.askyesno(
                "Recover Unsaved Changes",
                f"The app was not closed normally.\nRecover unsaved changes from "
                f"{len(valid)} tab(s)?", parent=self.root):
            for idx, m, data in valid:
                t = self.tabs[idx]
                t["name"] = self._clean_name(m.get("name")) or t["name"]
                t["mode"] = m.get("mode") if m.get("mode") in MODE_LABELS else MODE_TEXT
                t["data"], t["unsaved"], t["draft"] = data, True, False
            self._refresh_dropdown()
        else:
            with self.db:
                self.db.execute("DELETE FROM drafts")

    def write_draft(self, idx):
        """Autosave: recovery draft inside the vault database (not a commit)."""
        t = self.tabs[idx]
        if t["data"] is None:
            return
        meta = {"name": t["name"], "mode": t["mode"]}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO drafts(tab_index, meta, data, updated) "
                            "VALUES (?, ?, ?, ?)",
                            (idx, self._seal(meta, f"dmeta:{idx}"),
                             self._seal(t["data"], f"ddata:{idx}"), self._stamp()))
        t["draft"] = False

    def commit_tab(self, idx):
        """Save: commit to the vault, drop the draft, export to Sovereign_Vault_Files."""
        t = self.tabs[idx]
        data = self._get_data(idx)
        meta = {"name": t["name"], "mode": t["mode"]}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO tabs(tab_index, meta, data, updated) "
                            "VALUES (?, ?, ?, ?)",
                            (idx, self._seal(meta, f"meta:{idx}"),
                             self._seal(data, f"data:{idx}"), self._stamp()))
            self.db.execute("DELETE FROM drafts WHERE tab_index=?", (idx,))
        path = self._export_tab(idx, data)
        t["unsaved"], t["draft"], t["exists"] = False, False, True
        self._refresh_dropdown()
        return path

    def discard_tab_changes(self, idx):
        with self.db:
            self.db.execute("DELETE FROM drafts WHERE tab_index=?", (idx,))
        t = self.tabs[idx]
        t["name"], t["mode"] = f"Tab_{idx + 1:03d}", MODE_TEXT
        row = self.db.execute("SELECT meta FROM tabs WHERE tab_index=?", (idx,)).fetchone()
        if row:
            try:
                m = self._open(row[0], f"meta:{idx}")
                t["name"] = self._clean_name(m.get("name")) or t["name"]
                t["mode"] = m.get("mode") if m.get("mode") in MODE_LABELS else MODE_TEXT
            except Exception:
                pass
        t["data"], t["unsaved"], t["draft"] = None, False, False
        self._refresh_dropdown()

    @staticmethod
    def _render_export(data):
        content, tbl, notes = data.get("content", ""), data.get("table"), data.get("notes", "")
        sections = []
        if content.strip():
            sections.append(("TEXT", content))
        if tbl and any(c for r in tbl["rows"] for c in r):
            lines = ["\t".join(tbl["cols"])] + ["\t".join(r) for r in tbl["rows"]]
            sections.append(("TABLE", "\n".join(lines)))
        if notes.strip():
            sections.append(("NOTES", notes))
        if not sections:
            return ""
        if len(sections) == 1 and sections[0][0] == "TEXT":
            return content
        return "\n\n".join(f"=== {name} ===\n{body}" for name, body in sections)

    def _export_tab(self, idx, data):
        t, n = self.tabs[idx], idx + 1
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        stale = (list(VAULT_DIR.glob(f"Tab_{n:03d}.*")) +
                 list(VAULT_DIR.glob(f"Tab_{n:03d}__*")))
        rendered = self._render_export(data)
        if not rendered.strip():
            for p in stale:
                p.unlink(missing_ok=True)
            return None
        if self.crypto:
            path = VAULT_DIR / f"Tab_{n:03d}.httpsh"       # no title in the name (would leak it)
            body = ("HTTPSH-VAULT-EXPORT 1.1 | AES-256-GCM\n" +
                    self.crypto.encrypt(rendered, f"export:{idx}") + "\n")
        else:
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", t["name"]).strip("._") or "untitled"
            path = VAULT_DIR / f"Tab_{n:03d}__{safe}.txt"
            body = rendered
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
        for p in stale:
            if p != path:
                p.unlink(missing_ok=True)
        return path

    # ------------------------------------------------------------ tab handling
    def _refresh_dropdown(self):
        self.tab_dropdown["values"] = [
            f"{i + 1}. {t['name']}{' *' if t['unsaved'] else ''}"
            for i, t in enumerate(self.tabs)]
        self.tab_dropdown.current(self.current_index)

    def on_tab_selected(self, event=None):
        idx = self.tab_dropdown.current()
        if idx >= 0 and self.switch_to_tab(idx):
            self._focus_editor()

    def step_tab(self, delta):
        self.switch_to_tab((self.current_index + delta) % TAB_COUNT)

    def _focus_editor(self):
        if self.table_view is not None and self.table_view.cols:
            self.table_view.cols[0].focus_set()
        else:
            self.text.focus_set()

    def switch_to_tab(self, idx):
        if self._loaded:
            if idx == self.current_index:
                return True
            if not self._confirm_leave_tab():
                self.tab_dropdown.current(self.current_index)
                return False
            self._leave_current()
        self.current_index = idx
        self._show_current_tab()
        self._loaded = True
        self.tab_dropdown.current(idx)
        self.enop.set_last_tab(idx)
        return True

    def _confirm_leave_tab(self):
        """Word-style prompt when leaving a tab that has unsaved changes."""
        idx, t = self.current_index, self.tabs[self.current_index]
        if not t["unsaved"]:
            return True
        ans = messagebox.askyesnocancel(
            "Unsaved Changes",
            f"Do you want to save changes to '{t['name']}'?\n\n"
            "Choose No to discard your changes.", icon="warning", parent=self.root)
        if ans is None:
            return False
        if ans:
            try:
                self.snapshot_current()
                self.commit_tab(idx)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save:\n{e}")
                return False
            return True
        self.discard_tab_changes(idx)
        return True

    def _leave_current(self):
        t = self.tabs[self.current_index]
        if t["unsaved"]:
            self.snapshot_current()
        else:
            t["data"] = None                 # lazy loading: release the tab's content
        self._destroy_table_view()
        self._clear_text_widget()
        gc.collect()

    def snapshot_current(self):
        """Copy the live editor / table state into the in-memory tab data."""
        idx = self.current_index
        t, data = self.tabs[idx], self._get_data(idx)
        if t["mode"] == MODE_TABLE:
            if self.table_view is not None:
                data["table"] = self.table_view.get_model()
                data["notes"] = self.table_view.get_notes()
        else:
            ta = self.text
            data["content"] = ta.get("1.0", "end-1c")
            data["tags"] = self._serialize_tags()
            data["cursor"] = self._off("insert")

    def _off(self, index):
        return _tk_count(self.text, "1.0", str(index))

    def _serialize_tags(self):
        out = {}
        for name in STYLE_TAGS:
            rs = self.text.tag_ranges(name)
            flat = []
            for a, b in zip(rs[0::2], rs[1::2]):
                flat += [self._off(a), self._off(b)]
            if flat:
                out[name] = flat
        return out

    def _mark_modified(self):
        t = self.tabs[self.current_index]
        t["draft"] = True
        if not t["unsaved"]:
            t["unsaved"] = True
            self._refresh_dropdown()

    # ------------------------------------------------------------- view modes
    def _show_current_tab(self):
        idx = self.current_index
        t, data = self.tabs[idx], self._get_data(idx)
        self.mode_var.set(MODE_LABELS[t["mode"]])
        if t["mode"] == MODE_TABLE:
            self._enter_table_view(data)
        else:
            self._enter_text_view(data)
        self.apply_filter()
        self.update_status_bar()
        gc.collect()

    def _enter_text_view(self, data):
        self._destroy_table_view()
        self.text_frame.pack(fill="both", expand=True)
        self._load_text(data)

    def _enter_table_view(self, data):
        self.text_frame.pack_forget()
        self._clear_text_widget()
        self._destroy_table_view()
        tv = TableView(self.main_container, self.theme, self.f_normal, self.f_bold,
                       self._on_table_change, self.warn_limit, MAX_CHARS)
        tv.pack(fill="both", expand=True)
        tbl = data["table"] or {"cols": ["Col 1", "Col 2", "Col 3"], "rows": [], "nrows": 10}
        tv.set_model(tbl, data["notes"])
        self.table_view = tv

    def _destroy_table_view(self):
        if self.table_view is not None:
            self.table_view.destroy()
            self.table_view = None

    def _clear_text_widget(self):
        self._loading = True
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.edit_reset()
            self.text.edit_modified(False)
        finally:
            self._loading = False

    def _load_text(self, data):
        ta, content = self.text, data["content"]
        self._loading = True
        try:
            ta.configure(state="normal")
            ta.delete("1.0", "end")
            ta.insert("1.0", content, ())
            for name, vals in data["tags"].items():
                for s, e in zip(vals[0::2], vals[1::2]):
                    try:
                        ta.tag_add(name, f"1.0+{s}c", f"1.0+{e}c")
                    except tk.TclError:
                        pass
            self.refresh_bolditalic()
            ta.edit_reset()
            ta.edit_modified(False)
            try:
                ta.mark_set("insert", f"1.0+{data['cursor']}c")
            except tk.TclError:
                ta.mark_set("insert", "1.0")
            ta.see("insert")
            self._last_count = len(content)
        finally:
            self._loading = False

    def _on_table_change(self):
        self._mark_modified()
        self.update_status_bar()

    def _ask_table_size(self):
        cols = simpledialog.askinteger("Generate Table",
                                       f"Number of columns (1-{MAX_TABLE_COLS}):",
                                       parent=self.root, minvalue=1,
                                       maxvalue=MAX_TABLE_COLS, initialvalue=4)
        if cols is None:
            return None
        rows = simpledialog.askinteger("Generate Table",
                                       f"Number of rows (1-{MAX_TABLE_ROWS}):",
                                       parent=self.root, minvalue=1,
                                       maxvalue=MAX_TABLE_ROWS, initialvalue=10)
        if rows is None:
            return None
        return {"cols": [f"Col {i + 1}" for i in range(cols)], "rows": [], "nrows": rows}

    def on_mode_selected(self, event=None):
        new = MODE_TABLE if self.mode_var.get() == MODE_LABELS[MODE_TABLE] else MODE_TEXT
        idx, t = self.current_index, self.tabs[self.current_index]
        if new == t["mode"]:
            return
        self.snapshot_current()
        data = self._get_data(idx)
        if new == MODE_TABLE and not data["table"]:
            model = self._ask_table_size()
            if model is None:
                self.mode_var.set(MODE_LABELS[t["mode"]])
                return
            data["table"] = model
        t["mode"] = new
        self._mark_modified()
        self._refresh_dropdown()
        self._show_current_tab()

    def generate_table(self):
        idx, t = self.current_index, self.tabs[self.current_index]
        self.snapshot_current()
        data = self._get_data(idx)
        tbl = data.get("table")
        if tbl and any(c for r in tbl["rows"] for c in r) and not messagebox.askyesno(
                "Generate Table", "This tab already has table data.\nReplace it with a new table?",
                parent=self.root):
            return
        model = self._ask_table_size()
        if model is None:
            return
        data["table"] = model
        t["mode"] = MODE_TABLE
        self._mark_modified()
        self._refresh_dropdown()
        self._show_current_tab()

    def rename_current_tab(self):
        new_name = self._clean_name(self.rename_entry.get())
        if not new_name:
            messagebox.showwarning("Warning", "Please enter a valid custom name for the tab!")
            return
        self.tabs[self.current_index]["name"] = new_name
        self._mark_modified()
        self._refresh_dropdown()
        self.rename_entry.delete(0, tk.END)
        self.update_status_bar()
        messagebox.showinfo("Success", f"Tab renamed successfully to: {new_name}")

    # ----------------------------------------------------- limit / modification
    def _char_count(self):
        if self.table_view is not None:
            return self.table_view.total_chars()
        return len(self.text.get("1.0", "end-1c"))

    def on_text_keypress(self, event):
        if event.state & 0x4:
            return None
        ch = event.char
        if not ch or not (ch.isprintable() or ch in ("\r", "\n", "\t")):
            return None
        if not self.text.tag_ranges("sel") and self._char_count() >= MAX_CHARS:
            self.warn_limit()
            return "break"
        return None

    def on_text_modified(self, event=None):
        ta = self.text
        if not ta.edit_modified():
            return
        ta.edit_modified(False)
        if self._loading:
            return
        self._enforce_limit()
        ta.edit_modified(False)
        self._mark_modified()
        self.update_status_bar()

    def _enforce_limit(self):
        ta = self.text
        n = len(ta.get("1.0", "end-1c"))
        if n > MAX_CHARS:
            ta.delete(f"1.0+{MAX_CHARS}c", "end-1c")      # correct CHARACTER offset
            n = MAX_CHARS
            self.warn_limit(truncated=True)
        elif n == MAX_CHARS and self._last_count < MAX_CHARS:
            self.warn_limit()
        self._last_count = n

    def warn_limit(self, truncated=False):
        now = time.monotonic()
        if now - self._last_warn < 3:
            return
        self._last_warn = now
        msg = "Maximum limit of 10,000 characters reached for this tab!"
        if truncated:
            msg += "\n\nThe extra text was cut off."
        self.root.after(10, lambda: messagebox.showwarning("Character Limit", msg))

    def insert_text_limited(self, text, spans=None):
        """Append text (and optional (start, end, tag) spans) at the end of the editor."""
        ta = self.text
        room = MAX_CHARS - self._char_count()
        if room <= 0:
            self.warn_limit()
            return 0
        chunk = text[:room]
        base = self._char_count()
        ta.configure(state="normal")
        ta.insert("end-1c", chunk, ())
        for s, e, tag in (spans or []):
            e = min(e, len(chunk))
            if e > s:
                ta.tag_add(tag, f"1.0+{base + s}c", f"1.0+{base + e}c")
        self.refresh_bolditalic()
        ta.see("end")
        self._mark_modified()
        self.apply_filter()
        if len(chunk) < len(text):
            self.warn_limit(truncated=True)
        return len(chunk)

    # ------------------------------------------------------------ formatting
    def _text_mode_only(self):
        if self.table_view is not None:
            self.flash("Formatting is available in Text Workspace mode.")
            return False
        return True

    def _selection(self):
        try:
            return self.text.index("sel.first"), self.text.index("sel.last")
        except tk.TclError:
            return None

    def _fully_tagged(self, tag, s, e):
        ta = self.text
        total = _tk_count(ta, s, e)
        if total == 0:
            return False
        covered = 0
        ranges = [str(x) for x in ta.tag_ranges(tag)]
        for rs, re_ in zip(ranges[0::2], ranges[1::2]):
            lo = rs if ta.compare(rs, ">", s) else s
            hi = re_ if ta.compare(re_, "<", e) else e
            if ta.compare(lo, "<", hi):
                covered += _tk_count(ta, lo, hi)
        return covered >= total

    def toggle_style(self, tag):
        if not self._text_mode_only():
            return
        sel = self._selection()
        if not sel:
            messagebox.showwarning("Formatting", "Please select some text first!")
            return
        s, e = sel
        if self._fully_tagged(tag, s, e):
            self.text.tag_remove(tag, s, e)
        else:
            self.text.tag_add(tag, s, e)
        self.refresh_bolditalic()
        self._mark_modified()

    def apply_color(self, tag):
        if not self._text_mode_only():
            return
        sel = self._selection()
        if not sel:
            messagebox.showwarning("Color Error", "Please select text to apply color!")
            return
        s, e = sel
        if self._fully_tagged(tag, s, e):
            self.text.tag_remove(tag, s, e)
        else:
            for other in COLOR_TAGS:
                self.text.tag_remove(other, s, e)
            self.text.tag_add(tag, s, e)
        self._mark_modified()

    def clear_formatting(self):
        if not self._text_mode_only():
            return
        sel = self._selection()
        if not sel:
            messagebox.showwarning("Formatting", "Please select some text first!")
            return
        for name in STYLE_TAGS:
            self.text.tag_remove(name, *sel)
        self.refresh_bolditalic()
        self._mark_modified()

    def refresh_bolditalic(self):
        """'bolditalic' (derived tag) = wherever bold and italic overlap."""
        ta = self.text
        ta.tag_remove("bolditalic", "1.0", "end")
        wanted = {"bold", "italic"}
        active, start = set(), None
        for key, name, index in ta.dump("1.0", "end", tag=True):
            if name not in wanted:
                continue
            before = wanted <= active
            if key == "tagon":
                active.add(name)
            else:
                active.discard(name)
            after = wanted <= active
            if not before and after:
                start = index
            elif before and not after and start is not None:
                ta.tag_add("bolditalic", start, index)
                start = None
        if wanted <= active and start is not None:
            ta.tag_add("bolditalic", start, "end")

    # ------------------------------------------------------ filter / highlight
    def apply_filter(self):
        """Text mode: hide (elide) non-matching lines - never deletes text.
        Table mode: the whole table stays visible; matches are bolded + highlighted."""
        if self.text is None:
            return
        query = self.search_var.get().strip()
        if self.table_view is not None:
            self.filter_active = False
            n = self.table_view.highlight(query)
            self.search_info = f"Search: {n} match(es) highlighted" if query else ""
            self.update_status_bar()
            return
        ta = self.text
        ta.configure(state="normal")
        ta.tag_remove("hidden", "1.0", "end")
        ta.tag_remove("search_hit", "1.0", "end")
        if not query:
            self.filter_active, self.search_info = False, ""
            self.update_status_bar()
            return
        self.filter_active = True
        matches, count = 0, tk.IntVar()
        total = int(ta.index("end-1c").split(".")[0])
        for n in range(1, total + 1):
            pos = ta.search(query, f"{n}.0", stopindex=f"{n}.end", nocase=True, count=count)
            if not pos:
                ta.tag_add("hidden", f"{n}.0", f"{n}.end+1c")
                continue
            matches += 1
            while pos:
                end = f"{pos}+{count.get()}c"
                ta.tag_add("search_hit", pos, end)
                pos = ta.search(query, end, stopindex=f"{n}.end", nocase=True, count=count)
        self.search_info = f"Filter: {matches} matching line(s) - editing paused"
        ta.configure(state="disabled")
        self.update_status_bar()

    # ------------------------------------------------------------ status bar
    def flash(self, msg, ms=5000):
        self.status_extra = msg
        if self._flash_id:
            self.root.after_cancel(self._flash_id)
        self._flash_id = self.root.after(ms, self._clear_flash)
        self.update_status_bar()

    def _clear_flash(self):
        self._flash_id, self.status_extra = None, ""
        self.update_status_bar()

    def update_status_bar(self):
        if self.text is None or not hasattr(self, "status_bar"):
            return
        n = self._char_count()
        t = self.tabs[self.current_index]
        parts = [f"Tab: {self.current_index + 1} of {TAB_COUNT} ({t['name']})"
                 + (" [unsaved]" if t["unsaved"] else ""),
                 f"{MODE_LABELS[t['mode']]}",
                 f"Characters: {n:,} / {MAX_CHARS:,}",
                 "Vault: AES-256-GCM" if self.crypto else "Vault: plain text",
                 f"Session: {self.session.get('identity', '?')}"]
        if self.search_info:
            parts.append(self.search_info)
        if self.status_extra:
            parts.append(self.status_extra)
        dark = self.theme["dark"]
        if n >= MAX_CHARS:
            fg = "#ff5555" if dark else "#cc0000"
        elif n >= MAX_CHARS * WARN_RATIO:
            fg = "#ffaa33" if dark else "#cc7a00"
        else:
            fg = self.theme["fg"]
        self.status_bar.config(text=" | ".join(parts), fg=fg, bg=self.theme["bg"])

    # --------------------------------------------------------------- actions
    def save_current_page(self):
        idx = self.current_index
        try:
            self.snapshot_current()
            path = self.commit_tab(idx)
            self.enop.save()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save:\n{e}")
            return
        n = self._char_count()
        where = f"\nExported to:\n{path}" if path else "\n(Tab is empty - no file exported.)"
        if n >= MAX_CHARS:
            messagebox.showwarning("Character Limit",
                                   f"Saved, but this tab is FULL ({n:,} / {MAX_CHARS:,}). "
                                   f"No more text can be added.{where}")
        elif n >= MAX_CHARS * WARN_RATIO:
            messagebox.showwarning("Close to Limit",
                                   f"Saved. This tab is close to the limit "
                                   f"({n:,} / {MAX_CHARS:,}).{where}")
        else:
            messagebox.showinfo("Success", f"Saved to the vault database.{where}")
        self.update_status_bar()

    @staticmethod
    def _open_external(path):
        if sys.platform.startswith("win"):
            os.startfile(str(path))                                    # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def open_vault_folder(self):
        try:
            VAULT_DIR.mkdir(parents=True, exist_ok=True)
            self._open_external(VAULT_DIR)
        except Exception as e:
            messagebox.showerror("Error", f"Could not open the folder:\n{e}\n\n{VAULT_DIR}")

    def _autosave_tick(self):
        try:
            if self.tabs[self.current_index]["draft"]:
                self.snapshot_current()
            saved = 0
            for i, t in enumerate(self.tabs):
                if t["draft"] and t["data"] is not None:
                    self.write_draft(i)
                    saved += 1
            self.enop.save()
            if saved:
                self.flash(f"Draft autosaved ({saved} tab(s)) at {datetime.now():%H:%M:%S}")
        except Exception as e:
            self.flash(f"Autosave failed: {e}")
        self._autosave_id = self.root.after(AUTOSAVE_MS, self._autosave_tick)

    # ------------------------------------------------------------------ print
    @staticmethod
    def _text_html(content, tags):
        spans = []
        for name, vals in tags.items():
            for s, e in zip(vals[0::2], vals[1::2]):
                spans.append((s, e, name))
        cuts = sorted({0, len(content)} | {s for s, _, _ in spans} | {e for _, e, _ in spans})
        out = []
        for a, b in zip(cuts, cuts[1:]):
            if b <= a:
                continue
            seg = html.escape(content[a:b])
            active = [n for s, e, n in spans if s <= a and e >= b]
            css = []
            if "bold" in active:
                css.append("font-weight:bold")
            if "italic" in active:
                css.append("font-style:italic")
            for n in active:
                if n in COLOR_TAGS:
                    css.append("color:" + COLOR_TAGS[n][0])
                    break
            out.append(f'<span style="{";".join(css)}">{seg}</span>' if css else seg)
        return "".join(out)

    def _build_print_html(self, t, data):
        esc = html.escape
        if t["mode"] == MODE_TABLE:
            parts = []
            tbl = data.get("table")
            if tbl:
                rows = list(tbl["rows"])
                parts.append("<table><thead><tr>" +
                             "".join(f"<th>{esc(c)}</th>" for c in tbl["cols"]) +
                             "</tr></thead><tbody>")
                for r in rows or [[""] * len(tbl["cols"])]:
                    parts.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>")
                parts.append("</tbody></table>")
            if data.get("notes", "").strip():
                parts.append('<h3>Notes</h3><div class="text">' + esc(data["notes"]) + "</div>")
            body = "".join(parts)
        else:
            body = ('<div class="text">' +
                    self._text_html(data["content"], data["tags"]) + "</div>")
        css = ("body{font-family:Consolas,'DejaVu Sans Mono',monospace;margin:24px;color:#000}"
               ".meta{color:#555;font-size:12px;margin-bottom:16px}"
               ".text{white-space:pre-wrap;unicode-bidi:plaintext;text-align:start;"
               "line-height:1.5}"
               "table{border-collapse:collapse;width:100%;margin-bottom:16px}"
               "th,td{border:1px solid #444;padding:4px 8px;text-align:start;"
               "unicode-bidi:plaintext;vertical-align:top}"
               "th{background:#e8e8e8}")
        js = ("window.addEventListener('load',function(){setTimeout(function(){"
              "window.print();},300);});")
        return ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>" + esc(t["name"]) +
                "</title><style>" + css + "</style></head><body><h2>" + esc(t["name"]) +
                "</h2><div class=\"meta\">" + esc(MODE_LABELS[t["mode"]]) + " - " +
                datetime.now().strftime("%Y-%m-%d %H:%M") + "</div>" + body +
                "<script>" + js + "</script></body></html>")

    def print_current(self):
        """Safe printing: writes a clean HTML page and opens it with the system browser,
        which shows the standard print dialog. No Tk/threading involved."""
        try:
            self.snapshot_current()
            idx = self.current_index
            t, data = self.tabs[idx], self._get_data(idx)
            if not self._render_export(data).strip():
                messagebox.showwarning("Print", "This tab is empty - nothing to print.")
                return
            if self.crypto and not messagebox.askokcancel(
                    "Print", "Printing creates a temporary readable (unencrypted) copy of "
                             "this tab. It is deleted when the app closes.\n\nContinue?",
                    parent=self.root):
                return
            fd, path = tempfile.mkstemp(prefix="topfra_print_", suffix=".html")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self._build_print_html(t, data))
            self._print_files.append(path)
            self._open_external(path)
            self.flash("Opened in your browser - use its print dialog (Ctrl+P)")
        except Exception as e:
            messagebox.showerror("Print Error", f"Could not prepare the print page:\n{e}")

    # ------------------------------------------------------- import / extraction
    @staticmethod
    def _split_point(text, start, size):
        size = max(1, size)
        end = min(len(text), start + size)
        if end >= len(text):
            return len(text)
        lo = max(start, end - 500)
        cut = text.rfind("\n", lo, end)
        if cut == -1:
            cut = text.rfind(" ", lo, end)
        return cut + 1 if cut > start else end

    @staticmethod
    def _clip_spans(spans, a, b):
        return [(max(s, a) - a, min(e, b) - a, t) for s, e, t in spans if e > a and s < b]

    @staticmethod
    def _spans_to_tags(spans):
        tags = {}
        for s, e, t in spans:
            if e > s and t in STYLE_TAGS:
                tags.setdefault(t, []).extend([s, e])
        return tags

    def _free_tabs(self, count):
        out = []
        for i in range(self.current_index + 1, TAB_COUNT):
            t = self.tabs[i]
            if not t["exists"] and not t["unsaved"] and t["data"] is None:
                out.append(i)
                if len(out) >= count:
                    break
        return out

    def import_file(self, keep_format):
        if self.table_view is not None:
            self.flash("Switch this tab to Text Workspace mode to import a file.")
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="Import book / document / image",
            filetypes=[("Books, documents, images", TextExtractor.dialog_patterns()),
                       ("All files", "*.*")])
        if not path:
            return
        self.flash("Extracting text...")
        self._run_async(lambda: TextExtractor.extract(path, keep_format),
                        lambda res: self._import_done(path, res), self._import_failed)

    def _import_failed(self, e):
        if isinstance(e, UnsupportedCustomization):
            messagebox.showerror("خطأ", UNSUPPORTED_MSG)
        elif isinstance(e, ExtractionUnavailable):
            messagebox.showinfo("Extraction Unavailable", str(e))
        else:
            messagebox.showerror("Import Error", f"{type(e).__name__}: {e}")

    def _import_done(self, path, result):
        try:
            self._place_import(path, result)
        except (UnsupportedCustomization, tk.TclError):
            messagebox.showerror("خطأ", UNSUPPORTED_MSG)

    def _place_import(self, path, result):
        text, spans = result.text, result.spans
        if not text.strip():
            messagebox.showinfo("Import", "No text was found in the file.")
            return
        cur = self.current_index
        room = MAX_CHARS - self._char_count()
        chunks, pos = [], 0
        if room > 0:
            end = self._split_point(text, 0, room)
            chunks.append((0, end))
            pos = end
        while pos < len(text):
            end = self._split_point(text, pos, MAX_CHARS)
            chunks.append((pos, end))
            pos = end
        first_here = room > 0
        extra_needed = len(chunks) - (1 if first_here else 0)
        use_extra, free = False, []
        if extra_needed > 0:
            free = self._free_tabs(extra_needed)
            msg = (f"This text has {len(text):,} characters; one tab holds {MAX_CHARS:,}.\n"
                   f"{len(free)} empty tab(s) are available after this one"
                   + (" (the rest would be dropped)" if len(free) < extra_needed else "") +
                   ".\n\nYes = continue into the next empty tab(s)\n"
                   "No = import only what fits in this tab\nCancel = abort")
            ans = messagebox.askyesnocancel("Import Book", msg, parent=self.root)
            if ans is None:
                return
            use_extra = bool(ans) and bool(free)
        targets = ([cur] if first_here else []) + (free if use_extra else [])
        if not targets:
            messagebox.showinfo("Import", "This tab is full. Nothing was imported.")
            return
        stem = self._clean_name(Path(path).stem)[:32] or "Import"
        placed = 0
        for page, tab_idx in enumerate(targets):
            a, b = chunks[page]
            piece, pspans = text[a:b], self._clip_spans(spans, a, b)
            if tab_idx == cur:
                self.insert_text_limited(piece, pspans)
            else:
                t = self.tabs[tab_idx]
                d = _empty_data()
                d["content"], d["tags"] = piece, self._spans_to_tags(pspans)
                t["data"], t["name"], t["mode"] = d, f"{stem} p{page + 1}", MODE_TEXT
                t["unsaved"], t["draft"] = True, True
            placed += 1
        self._refresh_dropdown()
        notes = list(result.notes)
        if placed < len(chunks):
            notes.append(f"{len(chunks) - placed} page(s) did not fit and were not imported")
        notes.append("unsaved - press Save Page to keep it")
        self.flash("Imported: " + "; ".join(notes), ms=9000)

    # ---------------------------------------------------- session / networking
    def show_session_info(self):
        try:
            self.enop.save()
            block = self.enop.export_portable_account(platform.node() or "this-device")
            if messagebox.askyesno("Session",
                                   self.enop.summary() +
                                   "\n\nCopy the portable account block to the clipboard?",
                                   parent=self.root):
                self.root.clipboard_clear()
                self.root.clipboard_append(block)
                self.flash("Portable account block copied to the clipboard")
        except Exception as e:
            messagebox.showerror("Session", str(e))

    def _run_async(self, func, on_ok, on_err):
        if self._busy:
            self.flash("Another operation is still running...")
            return
        self._busy = True

        def worker():
            try:
                result = func()
            except Exception as e:
                self._queue.put((on_err, e))
            else:
                self._queue.put((on_ok, result))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                callback, arg = self._queue.get_nowait()
                self._busy = False
                try:
                    callback(arg)
                except Exception as e:                    # never let a callback kill the poller
                    messagebox.showerror("Error", f"{type(e).__name__}: {e}")
        except queue.Empty:
            pass
        self._poll_id = self.root.after(100, self._poll_queue)

    def _node_error(self, action, e):
        try:
            self.enop.record_node_event(action, False, detail=f"{type(e).__name__}: {e}")
        except Exception:
            pass
        messagebox.showerror("Connection Error",
                             "Failed to reach the local Httpsh node at 127.0.0.1:8000.\n"
                             "Make sure server.py is running.\n\n"
                             f"{type(e).__name__}: {e}")

    def fetch_httpsh_node(self):
        if self.table_view is not None:
            self.flash("Switch this tab to Text Workspace mode to insert the node response.")
            return

        def done(resp):
            ok = HttpshClient.is_verified(resp)
            self.enop.record_node_event("fetch", True, ok)
            header = ("=== [ Httpsh Local Node Verified Response ] ===" if ok
                      else "=== [ UNVERIFIED Node Response ] ===")
            lines = [f"{str(k).upper()}: {str(v)[:500]}" for k, v in resp.items()]
            if self.table_view is None:
                self.insert_text_limited("\n" + header + "\n" + "\n".join(lines) + "\n")
            self.flash("Node verified" if ok else "Node responded but is NOT verified")

        self._run_async(self.client.get, done, lambda e: self._node_error("fetch", e))

    def send_to_node(self):
        self.snapshot_current()
        idx = self.current_index
        content = self._render_export(self._get_data(idx))
        if not content.strip():
            messagebox.showwarning("Warning", "Active page content is empty!")
            return
        ok, reason = HttpshClient.validate_payload("text", content)
        if not ok:
            messagebox.showerror("Httpsh Violation", reason)
            return
        payload, rid = HttpshClient.sanitize(content), f"tab_{idx + 1:03d}"

        def done(resp):
            verified = resp.get("core") == "TOPFRA"
            self.enop.record_node_event("send", True, verified)
            messagebox.showinfo("Node Response",
                                f"Status: {resp.get('status', '?')}\n"
                                f"Payload: {resp.get('payload_status', '?')}\n"
                                f"Node: {'verified' if verified else 'UNVERIFIED'}")

        self._run_async(lambda: self.client.post(rid, "text", payload), done,
                        lambda e: self._node_error("send", e))

    # -------------------------------------------------------------- shutdown
    def on_close_app(self):
        try:
            if self.tabs[self.current_index]["unsaved"]:
                self.snapshot_current()
            unsaved = [i for i, t in enumerate(self.tabs) if t["unsaved"]]
            if unsaved:
                if len(unsaved) == 1:
                    msg = f"Do you want to save changes to '{self.tabs[unsaved[0]]['name']}'?"
                else:
                    msg = f"Do you want to save changes to {len(unsaved)} tabs?"
                ans = messagebox.askyesnocancel(
                    "Unsaved Changes", msg + "\n\nChoose No to discard your changes.",
                    icon="warning", parent=self.root)
                if ans is None:
                    return
                if ans:
                    for i in unsaved:
                        self.commit_tab(i)
                else:
                    with self.db:
                        self.db.execute("DELETE FROM drafts")
        except Exception as e:
            if not messagebox.askyesno("Save Failed",
                                       f"Could not save everything:\n{e}\n\n"
                                       "Quit anyway? (Autosaved drafts can be recovered "
                                       "next time.)"):
                return
        try:
            self.enop.set_last_tab(self.current_index)
            self.enop.save()
        except Exception:
            pass
        for after_id in (self._autosave_id, self._poll_id, self._flash_id):
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
        for p in self._print_files:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            self.db.close()
        except sqlite3.Error:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = TopfraSovereignApp()
    app.run()
