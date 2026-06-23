#!/usr/bin/env python3
"""
MH_PCK_SoundSilencer 
Marvel Heroes Omega PCK sound silencer

this tool enables silencing specific sounds such as repetitve voice lines .
the default preset targets known "i cant do that" voice lines , and you may mute more .

this is a minimal standalone tool created after the dev workflow tools
if your interested in the full PCK audio extraction and transcription , launch the dev_workflow .

NOTES ==========================================================================
A `.pck` is an AKPK container holding an embedded BNK (BKHD/DIDX/DATA sections) followed
by loose RIFF/WAVE (WEM) streams. Each target line is identified by its 8-hex-digit
`wem_id`:
  * BNK-embedded WEM -> the DIDX entry id
  * loose WEM        -> its absolute byte offset in the file 

For each matched WEM we locate its RIFF `data` chunk and overwrite the samples with
0x00, keeping headers and total size intact. The result is a silent sound that the game loads.

"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Built-in preset of silence designations: { pack_name: [wem_id, ...] }
# wem_id is the lowercase 8-hex-digit id 
# "I can't do that" voice lines .
# ---------------------------------------------------------------------------
PRESET: Dict[str, List[str]] = {
    "SFX_Cable_INT":                ["002b709d"],
    "SFX_CaptainAmerica_INT":       ["00198b6b", "00378af0"],
    "SFX_BlackWidow_INT":           ["001b1b71"],
    "SFX_Colossus_INT":             ["00236a36"],
    "SFX_Cyclops_INT":              ["0011a887"],
    "SFX_Daredevil_INT":            ["00563107"],
    "SFX_InitialDownloadChunk_INT": [
        "04dd9f05", "06c7aebb", "0b71c2ba", "0c8c5d32",
        "0d0cd477", "1275d4a2", "114e23ff",
    ],
    "SFX_LukeCage_INT":             ["002cbe60"],
    "SFX_Psylocke_INT":             ["002e4143"],
    "SFX_Rogue_INT":                ["0026680a"],
    "SFX_SquirrelGirl_INT":         ["002acae5"],
}

# ---------------------------------------------------------------------------
#  logging 
# ---------------------------------------------------------------------------
class Log:
    def __init__(self, quiet: bool = False):
        self.quiet = quiet

    def info(self, msg: str) -> None:
        print(f"[ ] {msg}")

    def ok(self, msg: str) -> None:
        print(f"[+] {msg}")

    def warn(self, msg: str) -> None:
        print(f"[!] {msg}")

    def err(self, msg: str) -> None:
        print(f"[x] {msg}", file=sys.stderr)

    def debug(self, msg: str) -> None:
        if not self.quiet:
            print(f"    {msg}")


# ---------------------------------------------------------------------------
# WEM descriptor discovered while parsing a .pck
# ---------------------------------------------------------------------------
class Wem:
    __slots__ = ("wem_id", "abs_offset", "length", "is_loose")

    def __init__(self, wem_id: int, abs_offset: int, length: int, is_loose: bool):
        self.wem_id = wem_id          # numeric id
        self.abs_offset = abs_offset  # absolute byte offset of the payload start in the file
        self.length = length          # payload byte length
        self.is_loose = is_loose

    @property
    def id_hex(self) -> str:
        return f"{self.wem_id:08x}"


# ---------------------------------------------------------------------------
# PCK parser 
# Returns absolute file offsets so silencing is uniform for embedded + loose WEMs.
# ---------------------------------------------------------------------------
def parse_pck(data: bytes, log: Log) -> List[Wem]:
    wems: List[Wem] = []

    if data[0:4] != b"AKPK":
        raise ValueError("Not an AKPK .pck file (bad magic)")

    header_len = struct.unpack_from("<I", data, 4)[0]

    # Locate the embedded BNK by its BKHD signature.
    bnk_off = data.find(b"BKHD", header_len)
    if bnk_off == -1:
        bnk_off = data.find(b"BKHD")
    if bnk_off == -1:
        raise ValueError("No BKHD section found")

    # Walk BNK sections to find DIDX (index) and DATA (payloads).
    didx: List[Tuple[int, int, int]] = []  # (wem_id, rel_offset, length)
    data_start = 0
    data_len = 0
    pos = bnk_off
    while pos + 8 <= len(data):
        ident = data[pos:pos + 4]
        seclen = struct.unpack_from("<I", data, pos + 8 - 4)[0]
        if ident == b"DIDX":
            count = seclen // 12
            p = pos + 8
            for _ in range(count):
                wid, woff, wlen = struct.unpack_from("<III", data, p)
                didx.append((wid, woff, wlen))
                p += 12
        elif ident == b"DATA":
            data_start = pos + 8
            data_len = seclen
            break
        pos += 8 + seclen
        if pos % 4 != 0:
            pos += 4 - (pos % 4)

    # BNK-embedded WEMs: offsets are relative to DATA section start.
    for wid, woff, wlen in didx:
        wems.append(Wem(wid, data_start + woff, wlen, is_loose=False))

    # Loose WEMs: signature-scan for RIFF/WAVE after the DATA section ends.
    scan_start = data_start + data_len if data_start else header_len
    p = scan_start
    while p + 12 <= len(data):
        if data[p:p + 4] == b"RIFF" and data[p + 8:p + 12] == b"WAVE":
            riff_len = struct.unpack_from("<I", data, p + 4)[0]
            total = 8 + riff_len
            # Loose WEM id is its absolute offset .
            wems.append(Wem(p & 0xFFFFFFFF, p, total, is_loose=True))
            p += total
        else:
            p += 1

    log.debug(f"parsed {len(wems)} WEM(s): "
              f"{sum(1 for w in wems if not w.is_loose)} embedded + "
              f"{sum(1 for w in wems if w.is_loose)} loose")
    return wems


# ---------------------------------------------------------------------------
# Silence one RIFF/WAVE buffer by zeroing its 'data' chunk payload in place.
# Preserves total length . Falls back to full zero-fill if not a RIFF.
# ---------------------------------------------------------------------------
def zero_fill_riff(buf: bytearray, start: int, length: int) -> bool:
    if length < 36 or buf[start:start + 4] != b"RIFF" or buf[start + 8:start + 12] != b"WAVE":
        buf[start:start + length] = b"\x00" * length
        return True

    pos = start + 12
    end_of_wem = start + length
    while pos + 8 <= end_of_wem:
        chunk_id = buf[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", buf, pos + 4)[0]
        if chunk_id == b"data":
            ds = pos + 8
            de = min(ds + chunk_size, end_of_wem)
            buf[ds:de] = b"\x00" * (de - ds)
            return True
        pos += 8 + chunk_size
        if pos % 2 != 0:
            pos += 1
    # No data chunk found: zero everything but the 12-byte RIFF/WAVE header.
    buf[start + 12:start + length] = b"\x00" * (length - 12)
    return True


# ---------------------------------------------------------------------------
# Preset loading: built-in dict or external silence_designations.json
# ---------------------------------------------------------------------------
def load_external_preset(path: Path) -> Dict[str, List[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    groups: Dict[str, List[str]] = {}
    for item in raw.get("designations", []):
        pack = item.get("pack")
        wem_id = item.get("wem_id")
        if pack and wem_id:
            groups.setdefault(pack, []).append(str(wem_id).lower())
    return groups


# ---------------------------------------------------------------------------
# Core: apply the preset to a directory of .pck files
# ---------------------------------------------------------------------------
def run(source_dir: Path, preset: Dict[str, List[str]], log: Log,
        dry_run: bool = False) -> int:
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        log.err(f"Not a directory: {source_dir}")
        return 1

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_dir = source_dir / f"backup_{stamp}"

    log.info(f"Source folder : {source_dir}")
    log.info(f"Backup folder : {backup_dir}{' (dry-run, not created)' if dry_run else ''}")
    log.info(f"Preset packs  : {len(preset)}")
    print("-" * 60)

    total_silenced = 0
    packs_done = 0

    for pack_name, wem_ids in preset.items():
        pck_path = source_dir / f"{pack_name}.pck"
        if not pck_path.exists():
            log.debug(f"skip (not present): {pack_name}.pck")
            continue

        log.info(f"Pack: {pack_name}  (targets: {len(wem_ids)})")
        target_ids = {w.lower() for w in wem_ids}

        original = pck_path.read_bytes()
        try:
            wems = parse_pck(original, log)
        except Exception as e:
            log.warn(f"  could not parse {pck_path.name}: {e}")
            continue

        buf = bytearray(original)
        silenced_here = 0
        matched_ids = set()
        for w in wems:
            if w.id_hex in target_ids and w.id_hex not in matched_ids:
                if w.abs_offset + w.length > len(buf):
                    log.warn(f"  WEM {w.id_hex} out of bounds, skipping")
                    continue
                zero_fill_riff(buf, w.abs_offset, w.length)
                matched_ids.add(w.id_hex)
                silenced_here += 1
                log.ok(f"  silenced {w.id_hex} @ 0x{w.abs_offset:x} ({w.length:,} bytes)")

        missing = target_ids - matched_ids
        for mid in sorted(missing):
            log.warn(f"  target {mid} not found in {pack_name}.pck")

        if silenced_here == 0:
            log.warn(f"  nothing silenced in {pack_name}.pck")
            continue

        if dry_run:
            log.info(f"  [dry-run] would back up and silence {silenced_here} WEM(s)")
        else:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / pck_path.name
            if not backup_path.exists():
                backup_path.write_bytes(original)
                log.ok(f"  backed up -> {backup_path.relative_to(source_dir)}")
            pck_path.write_bytes(buf)
            log.ok(f"  wrote modified {pck_path.name} ({silenced_here} WEM(s) silenced)")

        total_silenced += silenced_here
        packs_done += 1

    print("-" * 60)
    verb = "would silence" if dry_run else "silenced"
    log.ok(f"Done: {verb} {total_silenced} WEM(s) across {packs_done} pack(s)")
    if not dry_run and packs_done:
        log.info(f"Originals preserved in: {backup_dir}")
    return 0


# ---------------------------------------------------------------------------
#  tkinter GUI 
# ---------------------------------------------------------------------------
def find_project_pck_index(start: Optional[Path] = None) -> Optional[Path]:
    """Auto-discover project_pck_index.json near the script / current folder."""
    here = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / "project_pck_index.json",
        Path.cwd() / "extracted" / "project_pck_index.json",
        here / "project_pck_index.json",
        here / "extracted" / "project_pck_index.json",
    ]
    if start:
        candidates.insert(0, Path(start))
    for c in candidates:
        if c.exists():
            return c.resolve()
    return None


def wem_id_from_filename(filename: str) -> str:
    """'002b709d_318.ogg' -> '002b709d'."""
    stem = Path(filename).stem
    return stem.split("_")[0].lower() if stem else ""


def launch_gui() -> int:
    # Dark palette 
    BG = "#222222"
    FG = "#c0c0c0"
    ENTRY_BG = "#111111"
    OK_GREEN = "#5cb85c"
    TREE_BG = "#1a1a1a"

    class SilencerGUI:
        def __init__(self, master: "tk.Tk"):
            self.master = master
            self.entries: List[dict] = []        # rows from project_pck_index.json
            self.filtered: List[dict] = []       # currently shown rows
            self.marked: Dict[str, bool] = {}    # key "pck::filename" -> will silence

            master.title("MH PCK Sound Silencer")
            master.geometry("1100x720")
            master.configure(bg=BG)

            style = ttk.Style()
            style.theme_use("clam")
            style.configure("Treeview", background=TREE_BG, fieldbackground=TREE_BG,
                            foreground=FG, rowheight=26, font=("Arial", 10))
            style.configure("Treeview.Heading", background="#2a2a2a", foreground=FG,
                            font=("Arial", 10, "bold"))
            style.map("Treeview", background=[("selected", "#333333")])
            style.configure("TCombobox", fieldbackground="#000000", foreground=FG,
                            background="#000000")
            style.map("TCombobox", fieldbackground=[("readonly", "#000000")])
            master.option_add("*TCombobox*Listbox.background", "#000000")
            master.option_add("*TCombobox*Listbox.foreground", FG)
            master.option_add("*TCombobox*Listbox.selectBackground", "#333333")
            master.option_add("*TCombobox*Listbox.selectForeground", FG)

            # -- Source folder row ----------------------------------
            src_frame = tk.Frame(master, bg=BG)
            src_frame.pack(fill=tk.X, padx=12, pady=(10, 4))
            tk.Label(src_frame, text="PCK folder:", bg=BG, fg=FG,
                     font=("Arial", 11)).pack(side=tk.LEFT)
            self.source_var = tk.StringVar(
                value=r"C:\Steam\steamapps\common\Marvel Heroes\UnrealEngine3\MarvelGame\CookedPCConsole"
            )
            tk.Entry(src_frame, textvariable=self.source_var, bg=ENTRY_BG, fg=FG,
                     insertbackground=FG, font=("Consolas", 10)).pack(
                side=tk.LEFT, padx=(6, 4), fill=tk.X, expand=True)
            tk.Button(src_frame, text="Browse", command=self._browse_source,
                      bg="#337ab7", fg="white", relief=tk.FLAT,
                      font=("Arial", 10)).pack(side=tk.LEFT)

            # -- Index row ------------------------------------------
            idx_frame = tk.Frame(master, bg=BG)
            idx_frame.pack(fill=tk.X, padx=12, pady=(0, 4))
            tk.Label(idx_frame, text="Index:", bg=BG, fg=FG,
                     font=("Arial", 11)).pack(side=tk.LEFT)
            default_idx = Path.cwd() / "project_pck_index.json"
            self.index_var = tk.StringVar(value=str(default_idx))
            tk.Entry(idx_frame, textvariable=self.index_var, bg=ENTRY_BG, fg=FG,
                     insertbackground=FG, font=("Consolas", 10)).pack(
                side=tk.LEFT, padx=(6, 4), fill=tk.X, expand=True)
            tk.Button(idx_frame, text="Load Index", command=self._browse_index,
                      bg="#337ab7", fg="white", relief=tk.FLAT,
                      font=("Arial", 10)).pack(side=tk.LEFT)

            # -- Search + filter row --------------------------------
            search_frame = tk.Frame(master, bg=BG)
            search_frame.pack(fill=tk.X, padx=12, pady=(0, 4))
            tk.Label(search_frame, text="Search:", bg=BG, fg=FG,
                     font=("Arial", 11)).pack(side=tk.LEFT)
            self.search_var = tk.StringVar()
            ent = tk.Entry(search_frame, textvariable=self.search_var, bg=ENTRY_BG,
                           fg=FG, insertbackground=FG, font=("Arial", 11), width=36)
            ent.pack(side=tk.LEFT, padx=(6, 6))
            ent.bind("<Return>", lambda _e: self._do_search())
            tk.Button(search_frame, text="Go", command=self._do_search, bg="#337ab7",
                      fg="white", relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT)
            self.case_var = tk.BooleanVar(value=False)
            tk.Checkbutton(search_frame, text="Case sensitive", variable=self.case_var,
                           bg=BG, fg=FG, selectcolor=ENTRY_BG, activebackground=BG,
                           activeforeground=FG, font=("Arial", 10)).pack(
                side=tk.LEFT, padx=(12, 0))
            self.marked_only_var = tk.BooleanVar(value=False)
            tk.Checkbutton(search_frame, text="Marked only", variable=self.marked_only_var,
                           bg=BG, fg=FG, selectcolor=ENTRY_BG, activebackground=BG,
                           activeforeground=FG, font=("Arial", 10),
                           command=self._do_search).pack(side=tk.LEFT, padx=(12, 0))
            self.status_var = tk.StringVar(value="Ready")
            tk.Label(search_frame, textvariable=self.status_var, bg=BG, fg=OK_GREEN,
                     font=("Arial", 10)).pack(side=tk.RIGHT)

            # -- Filters row ------------------------------------------
            filters_frame = tk.Frame(master, bg=BG)
            filters_frame.pack(fill=tk.X, padx=12, pady=(0, 4))
            tk.Label(filters_frame, text="Filter:", bg=BG, fg=FG,
                     font=("Arial", 11, "bold")).pack(side=tk.LEFT)
            self.filter_var = tk.StringVar(value="all")
            self.filter_combo = ttk.Combobox(
                filters_frame, textvariable=self.filter_var,
                values=("all", "dialog", "sfx"),
                state="readonly", width=10,
            )
            self.filter_combo.pack(side=tk.LEFT, padx=(6, 0))
            self.filter_combo.bind("<<ComboboxSelected>>", lambda _e: self._do_search())
            tk.Label(filters_frame, text="PCK:", bg=BG, fg=FG,
                     font=("Arial", 11)).pack(side=tk.LEFT, padx=(12, 0))
            self.pck_filter_var = tk.StringVar()
            self.pck_filter_entry = tk.Entry(
                filters_frame, textvariable=self.pck_filter_var,
                bg=ENTRY_BG, fg=FG, insertbackground=FG,
                font=("Arial", 11), width=14,
            )
            self.pck_filter_entry.pack(side=tk.LEFT, padx=(6, 0))
            self.pck_filter_entry.bind("<Return>", lambda _e: self._do_search())

            # -- Action buttons row ---------------------------------
            act_frame = tk.Frame(master, bg=BG)
            act_frame.pack(fill=tk.X, padx=12, pady=(0, 4))
            tk.Button(act_frame, text="Apply Built-in Preset Marks",
                      command=self._mark_preset, bg="#5a8a6a", fg="white",
                      relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT)
            tk.Button(act_frame, text="Clear Marks", command=self._clear_marks,
                      bg="#6a6a8a", fg="white", relief=tk.FLAT,
                      font=("Arial", 10)).pack(side=tk.LEFT, padx=(6, 0))
            tk.Button(act_frame, text="Save Designations",
                      command=self._save_designations, bg="#5a7a9a", fg="white",
                      relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=(6, 0))
            tk.Button(act_frame, text="Load Designations",
                      command=self._load_designations, bg="#8a7a5a", fg="white",
                      relief=tk.FLAT, font=("Arial", 10)).pack(side=tk.LEFT, padx=(6, 0))
            tk.Button(act_frame, text="Dry Run", command=lambda: self._apply(True),
                      bg="#7a6a9a", fg="white", relief=tk.FLAT,
                      font=("Arial", 10)).pack(side=tk.RIGHT)
            tk.Button(act_frame, text="Silence (In-Place + Backup)",
                      command=lambda: self._apply(False), bg="#a05050", fg="white",
                      relief=tk.FLAT, font=("Arial", 10, "bold")).pack(
                side=tk.RIGHT, padx=(0, 6))

            tk.Label(master,
                     text="Space or double-click the Silence column to toggle a row. "
                          "If no rows are marked, the built-in preset is used.",
                     bg=BG, fg="#888888", font=("Arial", 9)).pack(
                anchor="w", padx=12, pady=(0, 2))

            # -- Results tree ---------------------------------------
            list_frame = tk.Frame(master, bg=BG)
            list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))
            cols = ("silence", "pck", "filename", "transcription", "status")
            self.tree = ttk.Treeview(list_frame, columns=cols, show="headings",
                                     selectmode="browse")
            self.tree.heading("silence", text="Silence?")
            self.tree.column("silence", width=80, anchor="center")
            self.tree.heading("pck", text="PCK")
            self.tree.column("pck", width=180, anchor="w")
            self.tree.heading("filename", text="Filename")
            self.tree.column("filename", width=160, anchor="w")
            self.tree.heading("transcription", text="Transcription")
            self.tree.column("transcription", width=440, anchor="w")
            self.tree.heading("status", text="Category")
            self.tree.column("status", width=80, anchor="center")
            self.tree.tag_configure("on", foreground="#ff69b4")
            self.tree.tag_configure("off", foreground="#6aaa6a")
            vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=vsb.set)
            self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            vsb.pack(side=tk.RIGHT, fill=tk.Y)
            self.tree.bind("<space>", self._toggle_selected)
            self.tree.bind("<Double-1>", self._on_double_click)

            # -- Log output -----------------------------------------
            self.log_widget = scrolledtext.ScrolledText(
                master, height=9, bg="#000000", fg=FG, insertbackground=FG,
                font=("Consolas", 9), relief=tk.FLAT)
            self.log_widget.pack(fill=tk.X, padx=12, pady=(0, 12))

            # Auto-load index: default first, then common paths
            default_idx = Path(self.index_var.get())
            if default_idx.exists():
                self._load_index(default_idx)
            else:
                found = find_project_pck_index()
                if found:
                    self._load_index(found)
                else:
                    self._log("[ ] No project_pck_index.json found - you can still apply "
                              "the built-in preset by choosing a PCK folder.")
                    self._log(f"[ ] Built-in preset covers {len(PRESET)} pack(s).")

        # -- helpers ------------------------------------------------
        def _log(self, msg: str) -> None:
            self.log_widget.insert(tk.END, msg + "\n")
            self.log_widget.see(tk.END)
            self.master.update_idletasks()

        def _key(self, entry: dict) -> str:
            return f"{entry.get('_pck', '')}::{entry.get('filename', '')}"

        def _entry_status(self, e: dict) -> str:
            if e.get("error"):
                return "ERROR"
            if e.get("_is_dialogue") or e.get("transcription"):
                return "DIALOG"
            return "SFX"

        # -- index loading ------------------------------------------
        def _browse_source(self) -> None:
            path = filedialog.askdirectory(title="Select folder containing .pck files",
                                           initialdir=self.source_var.get())
            if path:
                self.source_var.set(path)

        def _browse_index(self) -> None:
            path = filedialog.askopenfilename(
                title="Select project_pck_index.json",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")])
            if path:
                self._load_index(Path(path))

        def _load_index(self, path: Path) -> None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                self._log(f"[x] Could not load index: {e}")
                return
            self.entries = data.get("entries", [])
            self.index_var.set(str(path))
            # Pre-mark entries that match the built-in preset (keep existing marks)
            for e in self.entries:
                pck = e.get("_pck", "")
                wid = wem_id_from_filename(e.get("filename", ""))
                if wid and wid in {w.lower() for w in PRESET.get(pck, [])}:
                    self.marked[self._key(e)] = True
            self._log(f"[+] Loaded index: {len(self.entries)} entries "
                      f"({sum(self.marked.values())} pre-marked from preset)")
            self._populate(self.entries)

        # -- tree population / search -------------------------------
        def _populate(self, entries: List[dict]) -> None:
            for item in self.tree.get_children():
                self.tree.delete(item)
            for e in entries:
                marked = self.marked.get(self._key(e), False)
                self.tree.insert("", tk.END, tags=("on" if marked else "off",), values=(
                    "YES" if marked else "-",
                    e.get("_pck", ""),
                    e.get("filename", ""),
                    e.get("transcription", ""),
                    self._entry_status(e),
                ))
            self.filtered = entries

        def _do_search(self) -> None:
            q = self.search_var.get().strip()
            flt = self.filter_var.get()
            base = self.entries

            # PCK name pre-filter
            pck_q = self.pck_filter_var.get().strip()
            if pck_q:
                pql = pck_q.lower()
                base = [e for e in base if pql in e.get("_pck", "").lower()]

            # Category filter
            if flt == "dialog":
                base = [e for e in base if e.get("_is_dialogue") or e.get("transcription")]
            elif flt == "sfx":
                base = [e for e in base if not e.get("_is_dialogue") and not e.get("transcription")]

            if self.marked_only_var.get():
                base = [e for e in base if self.marked.get(self._key(e), False)]
            if q:
                if self.case_var.get():
                    base = [e for e in base if q in e.get("transcription", "")
                            or q in e.get("filename", "") or q in e.get("_pck", "")]
                else:
                    ql = q.lower()
                    base = [e for e in base if ql in e.get("transcription", "").lower()
                            or ql in e.get("filename", "").lower()
                            or ql in e.get("_pck", "").lower()]
            self._populate(base)
            self.status_var.set(f"Showing {len(base)} / {len(self.entries)}  |  "
                                f"marked: {sum(self.marked.values())}")

        # -- marking ------------------------------------------------
        def _on_double_click(self, event) -> None:
            if self.tree.identify_region(event.x, event.y) == "cell":
                if self.tree.identify_column(event.x) == "#1":
                    self._toggle_selected()

        def _toggle_selected(self, _event=None) -> None:
            sel = self.tree.selection()
            if not sel:
                return
            idx = self.tree.index(sel[0])
            if idx < 0 or idx >= len(self.filtered):
                return
            entry = self.filtered[idx]
            key = self._key(entry)
            now = not self.marked.get(key, False)
            self.marked[key] = now
            vals = list(self.tree.item(sel[0], "values"))
            vals[0] = "YES" if now else "-"
            self.tree.item(sel[0], values=vals, tags=("on" if now else "off",))
            self.status_var.set(f"marked: {sum(self.marked.values())}")

        def _mark_preset(self) -> None:
            count = 0
            for e in self.entries:
                pck = e.get("_pck", "")
                wid = wem_id_from_filename(e.get("filename", ""))
                if wid and wid in {w.lower() for w in PRESET.get(pck, [])}:
                    self.marked[self._key(e)] = True
                    count += 1
            self._populate(self.filtered)
            self._log(f"[+] Marked {count} preset entries.")
            self.status_var.set(f"marked: {sum(self.marked.values())}")

        def _clear_marks(self) -> None:
            self.marked.clear()
            self._populate(self.filtered)
            self.status_var.set("marked: 0")

        def _save_designations(self) -> None:
            path = filedialog.asksaveasfilename(
                title="Save silence designations",
                defaultextension=".json",
                filetypes=[("JSON", "*.json")],
                initialfile="silence_designations.json")
            if not path:
                return
            output: Dict[str, Dict[str, str]] = {}
            for e in self.entries:
                if not self.marked.get(self._key(e), False):
                    continue
                pck = e.get("_pck", "")
                wid = wem_id_from_filename(e.get("filename", ""))
                if pck and wid:
                    output.setdefault(pck, {})[wid] = "ON"
            try:
                Path(path).write_text(
                    json.dumps(output, indent=2, ensure_ascii=False),
                    encoding="utf-8")
                total = sum(len(v) for v in output.values())
                self._log(f"[+] Saved {total} designation(s) to {path}")
            except Exception as e:
                self._log(f"[x] Could not save: {e}")

        def _load_designations(self) -> None:
            path = filedialog.askopenfilename(
                title="Load silence designations",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")])
            if not path:
                return
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception as e:
                self._log(f"[x] Could not load: {e}")
                return
            count = 0
            for e in self.entries:
                pck = e.get("_pck", "")
                wid = wem_id_from_filename(e.get("filename", ""))
                if pck and wid:
                    pack_data = data.get(pck, {})
                    if isinstance(pack_data, dict) and pack_data.get(wid) == "ON":
                        self.marked[self._key(e)] = True
                        count += 1
            self._populate(self.filtered)
            self._log(f"[+] Loaded {count} designation(s) from {path}")
            self.status_var.set(f"marked: {sum(self.marked.values())}")

        # -- build preset from marked rows --------------------------
        def _build_marked_preset(self) -> Dict[str, List[str]]:
            preset: Dict[str, List[str]] = {}
            for e in self.entries:
                if not self.marked.get(self._key(e), False):
                    continue
                pck = e.get("_pck", "")
                wid = wem_id_from_filename(e.get("filename", ""))
                if pck and wid:
                    preset.setdefault(pck, [])
                    if wid not in preset[pck]:
                        preset[pck].append(wid)
            return preset

        # -- apply --------------------------------------------------
        def _apply(self, dry_run: bool) -> None:
            src = self.source_var.get().strip()
            if not src or not Path(src).is_dir():
                messagebox.showerror("Invalid folder",
                                     "Please choose a valid folder containing .pck files.")
                return

            marked_preset = self._build_marked_preset()
            if marked_preset:
                preset = marked_preset
                source = f"{sum(len(v) for v in preset.values())} marked entries"
            else:
                preset = PRESET
                source = "built-in preset"

            if not dry_run:
                if not messagebox.askyesno(
                    "Confirm Silence",
                    f"This will BACK UP originals into a backup_<datetime> folder and "
                    f"OVERWRITE matching .pck files in:\n{src}\n\nUsing: {source}\n\nContinue?"):
                    return

            self._log("=" * 60)
            self._log(f"[ ] {'DRY RUN' if dry_run else 'APPLYING'} using {source}")
            gui_log = _GuiLog(self._log)
            try:
                run(Path(src), preset, gui_log, dry_run=dry_run)
            except Exception as e:
                self._log(f"[x] Failed: {e}")
                return
            self.status_var.set("Dry run complete" if dry_run else "Silence applied")

    class _GuiLog(Log):
        """Log subclass that routes output to the GUI text widget."""
        def __init__(self, sink):
            super().__init__(quiet=False)
            self._sink = sink

        def info(self, msg: str) -> None:  self._sink(f"[ ] {msg}")
        def ok(self, msg: str) -> None:    self._sink(f"[+] {msg}")
        def warn(self, msg: str) -> None:  self._sink(f"[!] {msg}")
        def err(self, msg: str) -> None:   self._sink(f"[x] {msg}")
        def debug(self, msg: str) -> None: self._sink(f"    {msg}")

    root = tk.Tk()
    SilencerGUI(root)
    root.mainloop()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="MH_PCK_SoundSilencer",
        description="Apply a preset of silence designations to Marvel Heroes .pck files "
                    "in place (backs up originals first). No third-party dependencies.",
    )
    p.add_argument(
        "source_dir", nargs="?", default=".",
        help="Folder containing the .pck files (default: current folder)",
    )
    p.add_argument(
        "--preset", metavar="JSON",
        help="External designations JSON (same format as silence_designations.json). "
             "If omitted, the built-in preset is used.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing or backing up anything.",
    )
    p.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress verbose per-file debug output.",
    )
    p.add_argument(
        "--gui", action="store_true",
        help="Launch the graphical interface (also the default when run with no args).",
    )
    args = p.parse_args(argv)

    if args.gui:
        return launch_gui()

    log = Log(quiet=args.quiet)

    if args.preset:
        preset_path = Path(args.preset)
        if not preset_path.exists():
            log.err(f"Preset file not found: {preset_path}")
            return 1
        try:
            preset = load_external_preset(preset_path)
        except Exception as e:
            log.err(f"Could not read preset: {e}")
            return 1
        if not preset:
            log.err("Preset file contained no usable designations.")
            return 1
        log.info(f"Using external preset: {preset_path}")
    else:
        preset = PRESET
        log.info("Using built-in preset")

    return run(Path(args.source_dir), preset, log, dry_run=args.dry_run)


if __name__ == "__main__":
    # No arguments -> launch the GUI; otherwise run the CLI.
    if len(sys.argv) == 1:
        raise SystemExit(launch_gui())
    raise SystemExit(main())
