"""
Transcription Dashboard - tkinter GUI for searching transcribed .ogg files.

Usage:
    python -m pkg_bnk_wwise_tools dashboard transcription.json

Features:
    - Full-text search across filenames and transcriptions
    - Case-sensitive toggle
    - Click-to-play audio (opens default OS player)
    - Shows transcription status: DIALOG / SFX / ERROR
    - Enabled toggle (ON / OFF) per row - OFF marks for silence
    - Persistent silence designations stored to JSON
"""

from __future__ import annotations

import json
import os
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk

from datetime import date
from PIL import Image, ImageDraw, ImageFont, ImageTk
from .DATA_silence_manager import SilenceManager
from .DATA_project_pck_index import build_project_pck_index
from .UTIL_logger import Level, Logger
from .MODIFY_batch_silencer import run_batch_silence
from .CONFIG_tools import ToolPaths

__all__ = ["TranscriptionDashboard", "run_dashboard", "_find_project_pck_index"]

try:
    import pygame
    _HAS_PYGAME = True
except ImportError:
    _HAS_PYGAME = False

# -- Dark colour palette -----------------------------------------
_BG = "#222222"          # dark grey for UI background
_FG = "#c0c0c0"
_ENTRY_BG = "#111111"    # near-black for entry fields
_ENTRY_FG = "#c0c0c0"
_SELECT_BG = "#2a2a2a"   # headings / selection base
_DETAIL_BG = "#000000"   # pure black for transcription detail
_OK_GREEN = "#5cb85c"
_BTN_BLUE = "#337ab7"
_BTN_PURPLE = "#6f42c1"
_BTN_GREEN = "#28a745"


def _style_treeview(style: ttk.Style) -> None:
    style.theme_use("clam")
    _TREE_BG = "#1a1a1a"
    style.configure("Treeview", background=_TREE_BG, fieldbackground=_TREE_BG, foreground=_FG, rowheight=30, font=("Arial", 11))
    style.configure("Treeview.Heading", background=_SELECT_BG, foreground=_FG, font=("Arial", 11, "bold"))
    style.map("Treeview", background=[("selected", "#333333")])
    # Scrollbar: black track, bright blue thumb, muted blue arrows, wider
    style.configure(
        "Vertical.TScrollbar",
        background="#1e90ff",
        troughcolor="#000000",
        bordercolor="#000000",
        arrowcolor="#4682b4",
        width=22,
    )
    # Combobox: black background, light grey text
    style.configure("TCombobox", fieldbackground=_ENTRY_BG, foreground=_FG, background=_ENTRY_BG)
    style.map("TCombobox", fieldbackground=[("readonly", _ENTRY_BG), ("active", _ENTRY_BG)])
    style.map("TCombobox", selectbackground=[("readonly", _ENTRY_BG)])
    style.map("TCombobox", selectforeground=[("readonly", _FG)])


def _make_browse_image(text: str = "^", bg: str = "#337ab7",
                       fg: str = "white", font_size: int = 24,
                       width: int = 32, height: int = 28) -> ImageTk.PhotoImage:
    img = Image.new("RGBA", (width, height), bg)
    draw = ImageDraw.Draw(img)
    font = _load_font("Arial", font_size, bold=False)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - tw) / 2
    y = (height - th) / 2
    for dx, dy in [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]:
        draw.text((x + dx, y + dy), text, font=font, fill="black")
    draw.text((x, y), text, font=font, fill=fg)
    return ImageTk.PhotoImage(img)


def _load_font(name: str, size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Load a TrueType font, trying explicit Windows paths as fallbacks."""
    candidates = []
    if bold:
        candidates.append(f"{name} Bold")
    candidates.append(name)
    if sys.platform == "win32":
        win_fonts = Path("C:/Windows/Fonts")
        fn = name.lower().replace(" ", "")
        if bold:
            candidates.insert(0, str(win_fonts / f"{fn}bd.ttf"))
        candidates.insert(1 if bold else 0, str(win_fonts / f"{fn}.ttf"))
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _make_button_image(text: str, bg: str, fg: str = "white",
                       font_name: str = "Arial", font_size: int = 21,
                       pad_x: int = 20, pad_y: int = 10) -> ImageTk.PhotoImage:
    font = _load_font(font_name, font_size, bold=True)
    # Use a temporary draw to measure text
    tmp = Image.new("RGBA", (1, 1))
    tmp_draw = ImageDraw.Draw(tmp)
    bbox = tmp_draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    width = int(tw + pad_x * 2)
    height = int(th + pad_y * 2)
    img = Image.new("RGBA", (width, height), bg)
    draw = ImageDraw.Draw(img)
    x = (width - tw) / 2
    y = (height - th) / 2
    for dx, dy in [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]:
        draw.text((x + dx, y + dy), text, font=font, fill="black")
    draw.text((x, y), text, font=font, fill=fg)
    return ImageTk.PhotoImage(img)


class TranscriptionDashboard:
    def __init__(self, master: tk.Tk, json_path: Path):
        self.master = master
        self.json_path = Path(json_path)
        self.data: dict = {}
        self.entries: list = []
        self.filtered: list = []
        self.is_master: bool = False

        # Silence manager lives beside the master index
        silence_path = self.json_path.parent / "silence_designations.json"
        self.silence_mgr = SilenceManager(silence_path)

        self._load_data()

        master.title("PCK Transcription Dashboard")
        master.geometry("1100x750")
        master.configure(bg=_BG)

        _style_treeview(ttk.Style())

        # -- Project path (very top) ------------------------------
        project_frame = tk.Frame(master, bg=_BG)
        project_frame.pack(fill=tk.X, padx=12, pady=(10, 4))
        tk.Label(project_frame, text="Project:", bg=_BG, fg=_FG, font=("Arial", 12)).pack(side=tk.LEFT)
        self.project_var = tk.StringVar(value=str(self.json_path.parent))
        tk.Entry(
            project_frame, textvariable=self.project_var,
            bg=_ENTRY_BG, fg=_ENTRY_FG, insertbackground=_FG,
            font=("Consolas", 11), width=80,
        ).pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)
        proj_browse_img = _make_browse_image("^", "#337ab7")
        proj_browse = tk.Button(project_frame, image=proj_browse_img, bd=1,
                                highlightthickness=0, relief=tk.RIDGE,
                                bg="#337ab7", activebackground="#337ab7",
                                command=self._browse_project)
        proj_browse.image = proj_browse_img
        proj_browse.pack(side=tk.LEFT, padx=(2, 0))

        # -- Tool paths row (compact) -----------------------------
        tools_frame = tk.Frame(master, bg=_BG)
        tools_frame.pack(fill=tk.X, padx=12, pady=(0, 4))
        tk.Label(tools_frame, text="Tools:", bg=_BG, fg=_FG, font=("Arial", 10, "bold")).pack(side=tk.LEFT)
        self._tool_entries: dict[str, tk.Entry] = {}
        self._tool_browse_images: list[ImageTk.PhotoImage] = []
        tp = ToolPaths.load()
        for name, val in [("ww2ogg", tp.ww2ogg), ("revorb", tp.revorb),
                          ("codebooks", tp.codebooks), ("ffmpeg", tp.ffmpeg)]:
            tk.Label(tools_frame, text=f"{name}:", bg=_BG, fg=_FG, font=("Arial", 9)).pack(side=tk.LEFT)
            ent = tk.Entry(tools_frame, bg=_ENTRY_BG, fg=_ENTRY_FG, insertbackground=_FG,
                          font=("Consolas", 9), width=22)
            ent.insert(0, val)
            ent.pack(side=tk.LEFT, padx=(2, 2))
            self._tool_entries[name] = ent
            browse_img = _make_browse_image("^", "#337ab7")
            browse = tk.Button(tools_frame, image=browse_img, bd=1,
                               highlightthickness=0, relief=tk.RIDGE,
                               bg="#337ab7", activebackground="#337ab7",
                               command=lambda n=name: self._browse_tool(n))
            browse.image = browse_img
            browse.pack(side=tk.LEFT, padx=(0, 6))
            self._tool_browse_images.append(browse_img)

        # -- Search row ----------------------------------------------
        search_frame = tk.Frame(master, bg=_BG)
        search_frame.pack(fill=tk.X, padx=12, pady=(4, 4))

        tk.Label(search_frame, text="Search:", bg=_BG, fg=_FG, font=("Arial", 12)).pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            search_frame, textvariable=self.search_var,
            bg=_ENTRY_BG, fg=_ENTRY_FG, insertbackground=_FG,
            font=("Arial", 12), width=40,
        )
        self.search_entry.pack(side=tk.LEFT, padx=(6, 6))
        self.search_entry.bind("<Return>", lambda _e: self._do_search())
        self.search_entry.focus_set()

        tk.Button(search_frame, text="Search", command=self._do_search,
                bg=_BTN_BLUE, fg="white", font=("Arial", 11), relief=tk.FLAT).pack(side=tk.LEFT)

        self.case_sensitive = tk.BooleanVar(value=False)
        tk.Checkbutton(
            search_frame, text="Case sensitive", variable=self.case_sensitive,
            bg=_BG, fg=_FG, selectcolor=_ENTRY_BG,
            activebackground=_BG, activeforeground=_FG, font=("Arial", 11),
        ).pack(side=tk.LEFT, padx=(12, 0))

        # -- Filters row ---------------------------------------------
        filters_frame = tk.Frame(master, bg=_BG)
        filters_frame.pack(fill=tk.X, padx=12, pady=(0, 4))

        tk.Label(filters_frame, text="Filters:", bg=_BG, fg=_FG, font=("Arial", 12, "bold")).pack(side=tk.LEFT)

        self.filter_var = tk.StringVar(value="all")
        self.filter_combo = ttk.Combobox(
            filters_frame, textvariable=self.filter_var,
            values=("all", "dialogue", "sfx"),
            state="readonly", width=10,
        )
        self.filter_combo.pack(side=tk.LEFT, padx=(6, 0))
        self.filter_combo.bind("<<ComboboxSelected>>", lambda _e: self._do_search())

        tk.Label(filters_frame, text="PCK:", bg=_BG, fg=_FG, font=("Arial", 12)).pack(side=tk.LEFT, padx=(12, 0))
        self.pck_filter_var = tk.StringVar()
        self.pck_filter_entry = tk.Entry(
            filters_frame, textvariable=self.pck_filter_var,
            bg=_ENTRY_BG, fg=_ENTRY_FG, insertbackground=_FG,
            font=("Arial", 12), width=14,
        )
        self.pck_filter_entry.pack(side=tk.LEFT, padx=(6, 0))
        self.pck_filter_entry.bind("<Return>", lambda _e: self._do_search())

        self.only_disabled_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            filters_frame, text="Silenced Only", variable=self.only_disabled_var,
            bg=_BG, fg=_FG, selectcolor=_ENTRY_BG,
            activebackground=_BG, activeforeground=_FG, font=("Arial", 11),
            command=self._do_search,
        ).pack(side=tk.LEFT, padx=(12, 0))

        self.status_var = tk.StringVar(value=f"Loaded {len(self.entries)} entries")
        tk.Label(filters_frame, textvariable=self.status_var,
                bg=_BG, fg=_OK_GREEN, font=("Arial", 11)).pack(side=tk.RIGHT)

        # -- Action buttons row (fills width evenly) ----------------
        btn_frame = tk.Frame(master, bg=_BG)
        btn_frame.pack(fill=tk.X, padx=12, pady=(4, 4))
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)
        btn_frame.columnconfigure(2, weight=1)

        btns = [
            ("Save & Export", self._save_and_export, "#5a8a6a"),
            ("Rebuild Index", self._rebuild_index, "#6a8a8a"),
            ("Clear All", self._clear_silences, "#6a6a8a"),
        ]
        for i, (text, cmd, bg) in enumerate(btns):
            img = _make_button_image(text, bg, font_size=21)
            btn = tk.Button(btn_frame, image=img, command=cmd, bd=1,
                            highlightthickness=0, relief=tk.RIDGE,
                            bg=bg, activebackground=bg)
            btn.image = img
            btn.grid(row=0, column=i, sticky="nsew", padx=(3 if i else 0, 3))

        # -- Source + Run Batch row ------------------------------------
        src_frame = tk.Frame(master, bg=_BG)
        src_frame.pack(fill=tk.X, padx=12, pady=(0, 4))
        tk.Label(src_frame, text="Source:", bg=_BG, fg=_FG, font=("Arial", 12)).pack(side=tk.LEFT)
        self.source_var = tk.StringVar(value=str(self.json_path.parent))
        tk.Entry(
            src_frame, textvariable=self.source_var,
            bg=_ENTRY_BG, fg=_ENTRY_FG, insertbackground=_FG,
            font=("Consolas", 11), width=60,
        ).pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)
        src_browse_img = _make_browse_image("^", "#337ab7")
        src_browse = tk.Button(src_frame, image=src_browse_img, bd=1,
                               highlightthickness=0, relief=tk.RIDGE,
                               bg="#337ab7", activebackground="#337ab7",
                               command=self._browse_source)
        src_browse.image = src_browse_img
        src_browse.pack(side=tk.LEFT, padx=(2, 12))
        run_bg = "#7a5a9a"
        run_img = _make_button_image("Run Batch Silence", run_bg, font_size=21)
        run_btn = tk.Button(src_frame, image=run_img, command=self._run_batch_silence, bd=1,
                            highlightthickness=0, relief=tk.RIDGE,
                            bg=run_bg, activebackground=run_bg)
        run_btn.image = run_img
        run_btn.pack(side=tk.LEFT, padx=(0, 12))
        self.disabled_count_var = tk.StringVar(value=f"Silenced: {self.silence_mgr.count}")
        tk.Label(src_frame, textvariable=self.disabled_count_var,
                bg=_BG, fg=_OK_GREEN, font=("Arial", 11)).pack(side=tk.LEFT)

        # -- Play button (upper left, above tree) ------------------
        play_frame = tk.Frame(master, bg=_BG)
        play_frame.pack(fill=tk.X, padx=12, pady=(0, 4), anchor="w")
        tk.Button(
            play_frame, text="Play Audio", command=self._play_selected,
            bg=_BTN_BLUE, fg="white", font=("Arial", 12, "bold"),
            width=12, relief=tk.FLAT,
        ).pack(side=tk.LEFT)
        tk.Label(play_frame, text="Double click, or use arrow keys to play sound entries",
                bg=_BG, fg="#888888", font=("Arial", 10)).pack(side=tk.LEFT, padx=(8, 0))

        # -- Middle: results tree -----------------------------------
        list_frame = tk.Frame(master, bg=_BG)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))

        cols = ["enabled"]
        if self.is_master:
            cols.append("pck")
        cols += ["filename", "transcription", "status"]

        self.tree = ttk.Treeview(
            list_frame, columns=cols, show="headings", selectmode="browse",
        )
        self.tree.heading("enabled", text="Enabled")
        self.tree.column("enabled", width=80, anchor="center")
        if self.is_master:
            self.tree.heading("pck", text="PCK")
            self.tree.column("pck", width=140, anchor="w")
        self.tree.heading("filename", text="Filename")
        self.tree.heading("transcription", text="Transcription")
        self.tree.heading("status", text="Category")
        # Dynamic filename width based on longest entry
        fname_font = tkfont.Font(family="Arial", size=11)
        max_fname = max((fname_font.measure(e.get("filename", "")) for e in self.entries), default=220)
        self.tree.column("filename", width=min(max_fname + 30, 360), anchor="w")
        self.tree.column("transcription", width=480 if self.is_master else 520, anchor="w")
        self.tree.column("status", width=90, anchor="center")

        # Tags: enabled state colors (muted green ON, bright pink OFF)
        self.tree.tag_configure("enabled_on", foreground="#6aaa6a")
        self.tree.tag_configure("enabled_off", foreground="#ff69b4")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<space>", self._toggle_selected_silence)
        self.tree.bind("<Up>", self._on_tree_arrow)
        self.tree.bind("<Down>", self._on_tree_arrow)

        # -- Bottom: details + play ---------------------------------
        detail_frame = tk.Frame(master, bg=_BG)
        detail_frame.pack(fill=tk.X, padx=12, pady=(0, 12))

        self.detail_var = tk.StringVar(value="Select an entry to see details. Arrow keys auto-play. Double-click a row to play. Press Space to toggle Enabled / Disabled.")
        tk.Label(
            detail_frame, textvariable=self.detail_var,
            bg=_DETAIL_BG, fg=_FG, font=("Consolas", 11),
            wraplength=1000, justify=tk.LEFT, anchor="nw",
        ).pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        master.bind("<Control-p>", lambda _e: self._play_selected())

        self._populate(self.entries)

    # -- Data loading ----------------------------------------------

    def _load_data(self) -> None:
        with open(self.json_path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        self.is_master = self.data.get("is_project_pck_index", False)
        self.entries = self.data.get("entries", [])

    # -- Populate tree ---------------------------------------------

    def _populate(self, entries: list) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for e in entries:
            filename = e.get("filename", "")
            trans = e.get("transcription", "")
            pck = e.get("_pck", "") if self.is_master else ""

            # Status + tag based on classification
            if e.get("error"):
                status = "ERROR"
                status_tag = "error"
            elif e.get("_is_dialogue") or trans:
                status = "DIALOG"
                status_tag = "dialog"
            else:
                status = "SFX"
                status_tag = "sfx"

            is_silenced = self.silence_mgr.is_designated(pck or "", filename)
            enabled_text = "OFF" if is_silenced else "ON"
            enabled_tag = "enabled_off" if is_silenced else "enabled_on"

            if self.is_master:
                self.tree.insert("", tk.END, values=(enabled_text, pck, filename, trans, status), tags=(enabled_tag,))
            else:
                self.tree.insert("", tk.END, values=(enabled_text, filename, trans, status), tags=(enabled_tag,))
        self.filtered = entries

    # -- Search ----------------------------------------------------

    def _do_search(self) -> None:
        query = self.search_var.get().strip()
        flt = self.filter_var.get()

        base = self.entries

        # PCK name pre-filter
        pck_query = self.pck_filter_var.get().strip()
        if pck_query and self.is_master:
            pq = pck_query.lower()
            base = [e for e in base if pq in e.get("_pck", "").lower()]

        if flt == "dialogue":
            base = [e for e in base if e.get("_is_dialogue") or e.get("transcription")]
        elif flt == "sfx":
            base = [e for e in base if not e.get("_is_dialogue") and not e.get("transcription")]

        if self.only_disabled_var.get():
            base = [e for e in base if self.silence_mgr.is_designated(
                e.get("_pck", "") if self.is_master else "", e.get("filename", ""))]

        def _status_msg(count: int) -> str:
            clauses: list[str] = []
            if pck_query:
                clauses.append(f"in PCKs with '{pck_query}'")
            if flt != "all":
                clauses.append(f"classified as '{flt}'")
            if self.only_disabled_var.get():
                clauses.append("silenced only")
            if query:
                msg = f"Found {count} match(es) for '{query}'"
            else:
                msg = f"Showing {count} entries"
            if clauses:
                msg += " " + " and ".join(clauses)
            return msg

        if not query:
            self._populate(base)
            self.status_var.set(_status_msg(len(base)))
            return

        filtered = []
        if self.case_sensitive.get():
            for e in base:
                if (query in e.get("transcription", "")
                        or query in e.get("filename", "")
                        or (self.is_master and query in e.get("_pck", ""))):
                    filtered.append(e)
        else:
            q = query.lower()
            for e in base:
                if (q in e.get("transcription", "").lower()
                        or q in e.get("filename", "").lower()
                        or (self.is_master and q in e.get("_pck", "").lower())):
                    filtered.append(e)

        self._populate(filtered)
        self.status_var.set(_status_msg(len(filtered)))

    # -- Selection & details -------------------------------------

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        idx = self.tree.index(selection[0])
        if idx < 0 or idx >= len(self.filtered):
            return
        entry = self.filtered[idx]
        fp = entry.get("filepath", "")
        trans = entry.get("transcription", "")
        err = entry.get("error", "")
        words = entry.get("word_timings", [])
        pck = entry.get("_pck", "") if self.is_master else ""
        is_silenced = self.silence_mgr.is_designated(pck, entry.get("filename", ""))

        lines = [f"File: {fp}", f'Transcription: {trans or "(none)"}']
        if self.is_master:
            lines.insert(0, f"PCK Filter : {pck}")
        if entry.get("_classifications"):
            tags = [f"{c['label']} ({c['model']}, {c['score']})"
                    for c in entry["_classifications"][:3]]
            lines.append(f"Tags: {', '.join(tags)}")
            lines.append(f"Dialogue: {'YES' if entry.get('_is_dialogue') else 'NO'} "
                         f"(conf={entry.get('_dialogue_confidence', 0)})")
        if is_silenced:
            lines.append("[DISABLED - will be silenced]")
        if err:
            lines.append(f"Error: {err}")
        if words:
            lines.append(f"Word count: {len(words)}")
        self.detail_var.set("\n".join(lines))

    # -- Double-click handling -----------------------------------

    def _on_double_click(self, event=None) -> None:
        region = self.tree.identify_region(event.x, event.y) if event else ""
        if region == "cell":
            col = self.tree.identify_column(event.x)
            if col == "#1":
                self._toggle_selected_silence()
                return
        self._play_selected()

    # -- Arrow-key auto-play -------------------------------------

    def _on_tree_arrow(self, _event=None) -> None:
        """Auto-play after Up/Down arrow navigation."""
        self.master.after_idle(self._play_selected)

    # -- Silence toggle ------------------------------------------

    def _toggle_selected_silence(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        idx = self.tree.index(selection[0])
        if idx < 0 or idx >= len(self.filtered):
            return
        entry = self.filtered[idx]
        pck = entry.get("_pck", "") if self.is_master else ""
        filename = entry.get("filename", "")
        trans = entry.get("transcription", "")
        fp = entry.get("filepath", "")
        wem_id = Path(filename).stem.split("_")[0] if filename else ""

        now_silenced = self.silence_mgr.toggle(
            pack=pck, filename=filename, wem_id=wem_id,
            ogg_path=fp, transcription=trans, reason="",
        )

        # Update the tree row
        values = list(self.tree.item(selection[0], "values"))
        values[0] = "OFF" if now_silenced else "ON"
        enabled_tag = "enabled_off" if now_silenced else "enabled_on"
        self.tree.item(selection[0], values=values, tags=(enabled_tag,))
        self.disabled_count_var.set(f"Silenced: {self.silence_mgr.count}")
        self.status_var.set(f"Silenced: {self.silence_mgr.count}")
        self._on_select()

    # -- Silence buttons -------------------------------------------

    def _save_silences(self) -> None:
        self.silence_mgr.save()
        # Also persist any edited tool paths
        tp = ToolPaths(
            ww2ogg=self._tool_entries["ww2ogg"].get(),
            revorb=self._tool_entries["revorb"].get(),
            codebooks=self._tool_entries["codebooks"].get(),
            ffmpeg=self._tool_entries["ffmpeg"].get(),
        )
        tp.save()
        self.status_var.set(f"Saved {self.silence_mgr.count} designations + tool paths  |  File: {self.silence_mgr.json_path.name}")

    def _save_and_export(self) -> None:
        self._save_silences()
        plan_path = self.silence_mgr.export_for_repackage()
        self.status_var.set(
            f"Saved {self.silence_mgr.count} designations + tool paths  |  "
            f"Repackage plan exported: {plan_path.name}"
        )

    def _clear_silences(self) -> None:
        if self.silence_mgr.count == 0:
            return
        self.silence_mgr.clear_all()
        self.silence_mgr.save()
        self._populate(self.filtered)
        self.disabled_count_var.set(f"Silenced: {self.silence_mgr.count}")
        self.status_var.set("All entries re-enabled (silence designations cleared).")

    def _export_plan(self) -> None:
        path = self.silence_mgr.export_for_repackage()
        self.status_var.set(f"Repackage plan exported: {path.name}")

    def _run_batch_silence(self) -> None:
        if self.silence_mgr.count == 0:
            self.status_var.set("No disabled entries. Set entries to OFF first.")
            return

        from tkinter import filedialog, messagebox
        src = self.source_var.get().strip()
        if not src:
            src = filedialog.askdirectory(
                title="Select directory containing original .pck files",
                initialdir=str(self.json_path.parent),
            )
            if not src:
                return
            self.source_var.set(src)

        if not messagebox.askyesno(
            "Confirm Batch Silence",
            f"This will BACKUP originals to backup_{date.today().isoformat()}\n"
            f"and OVERWRITE {self.silence_mgr.count} designated .pck(s) in:\n{src}\n\n"
            "Continue?",
        ):
            return

        self.status_var.set("Running batch silence...")
        self.master.update_idletasks()

        try:
            logger = Logger(Level.INFO)
            run_batch_silence(
                source_dir=Path(src),
                silence_json=self.silence_mgr.json_path,
                logger=logger,
            )
            self.disabled_count_var.set(f"Silenced: {self.silence_mgr.count}")
            self.status_var.set(
                f"Batch silence complete. Backups in backup_{date.today().isoformat()} "
                f"| Silenced: {self.silence_mgr.count}"
            )
        except Exception as e:
            self.status_var.set(f"Batch silence failed: {e}")

    def _rebuild_index(self) -> None:
        root_dir = self.json_path.parent
        self.status_var.set("Rebuilding project PCK index...")
        self.master.update_idletasks()
        try:
            build_project_pck_index(root_dir, self.json_path)
            self._load_data()
            self._populate(self.entries)
            self.disabled_count_var.set(f"Silenced: {self.silence_mgr.count}")
            self.status_var.set(f"Index rebuilt: {len(self.entries)} entries  |  Silenced: {self.silence_mgr.count}")
        except Exception as e:
            self.status_var.set(f"Index rebuild failed: {e}")

    # -- Browse helpers ----------------------------------------------

    def _browse_project(self) -> None:
        from tkinter import filedialog
        path = filedialog.askdirectory(title="Select project folder", initialdir=self.project_var.get())
        if path:
            self.project_var.set(path)

    def _browse_source(self) -> None:
        from tkinter import filedialog
        path = filedialog.askdirectory(title="Select source folder containing .pck files", initialdir=self.source_var.get())
        if path:
            self.source_var.set(path)

    def _browse_tool(self, name: str) -> None:
        from tkinter import filedialog
        ent = self._tool_entries[name]
        filetypes = [("Executables", "*.exe")] if name in ("ww2ogg", "revorb", "ffmpeg") else [("All files", "*.*")]
        path = filedialog.askopenfilename(title=f"Select {name}", initialdir=Path(ent.get()).parent, filetypes=filetypes)
        if path:
            ent.delete(0, tk.END)
            ent.insert(0, path)

    # -- Audio playback --------------------------------------------

    def _play_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        idx = self.tree.index(selection[0])
        if idx < 0 or idx >= len(self.filtered):
            return
        entry = self.filtered[idx]
        fp = entry.get("filepath", "")
        if not fp or not Path(fp).exists():
            self.detail_var.set("ERROR: audio file not found on disk")
            return

        if _HAS_PYGAME:
            try:
                if not pygame.mixer.get_init():
                    pygame.mixer.init()
                pygame.mixer.music.load(fp)
                pygame.mixer.music.play()
                self.detail_var.set(f"Now playing: {Path(fp).name}")
                return
            except Exception:
                pass

        os.startfile(fp)
        self.detail_var.set(f"Opened in default player: {Path(fp).name}")


def _find_project_pck_index() -> Path | None:
    """Auto-discover project_pck_index.json in common locations."""
    candidates = [
        Path("extracted") / "project_pck_index.json",
        Path(__file__).parent.parent / "extracted" / "project_pck_index.json",
        Path(__file__).parent.parent.parent / "extracted" / "project_pck_index.json",
    ]
    # Also accept first CLI arg
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if p.exists():
            return p
    # Try known locations
    for c in candidates:
        if c.exists():
            return c.resolve()
    return None


def run_dashboard(json_path: Path) -> None:
    root = tk.Tk()
    TranscriptionDashboard(root, json_path)
    root.mainloop()


if __name__ == "__main__":
    index_path = _find_project_pck_index()
    if index_path is None:
        print("Usage: python transcription_dashboard.py <project_pck_index.json>")
        print()
        print("Searched common paths but could not find project_pck_index.json.")
        print("Run the full pipeline first:")
        print("  python -m pkg_bnk_wwise_tools process_all <source_dir> --stage 2")
        sys.exit(1)
    print(f"Launching dashboard with: {index_path}")
    run_dashboard(index_path)
