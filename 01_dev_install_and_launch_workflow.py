"""
Marvel Heroes Omega PCK Editor - Installer & Launcher

Creates a virtual environment, installs all dependencies,
checks GPU availability, downloads audio classification models,
and provides a command reference.

Usage:
    python 01_dev_install_and_launch_workflow.py
    python 01_dev_install_and_launch_workflow.py --skip-venv
    python 01_dev_install_and_launch_workflow.py --gpu-check-only
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import tkinter as tk
    from tkinter import filedialog, scrolledtext, ttk
    _HAS_TK = True
except ImportError:
    _HAS_TK = False

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
VENV_DIR = Path(__file__).parent / "venv"
REQUIREMENTS = Path(__file__).parent / "requirements.txt"
LOG_DIR = Path(__file__).parent / "logs"

# Enable ANSI colors on Windows
if sys.platform == "win32":
    os.system("")

# ANSI colors
C_INFO = "\033[36m"
C_OK = "\033[32m"
C_WARN = "\033[33m"
C_ERR = "\033[31m"
C_RESET = "\033[0m"

# GUI dark palette (matches UI_transcription_dashboard.py)
_GUI_BG = "#222222"
_GUI_FG = "#c0c0c0"
_GUI_ENTRY_BG = "#111111"
_GUI_ENTRY_FG = "#c0c0c0"
_GUI_SELECT_BG = "#2a2a2a"
_GUI_DETAIL_BG = "#000000"
_GUI_BTN_BLUE = "#337ab7"
_GUI_BTN_GREEN = "#28a745"
_GUI_BTN_PURPLE = "#6f42c1"
_GUI_BTN_ORANGE = "#8b4513"
_GUI_BTN_RED = "#8b0000"
_GUI_OK = "#5cb85c"

_DEFAULT_SOURCE = r"C:\Steam\steamapps\common\Marvel Heroes\UnrealEngine3\MarvelGame\CookedPCConsole"


def _load_font(name: str, size: int, bold: bool = True) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
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


def _make_browse_image(text: str = "^", bg: str = "#337ab7",
                       fg: str = "white", font_size: int = 24,
                       width: int = 32, height: int = 28) -> "ImageTk.PhotoImage | None":
    if not _HAS_PIL:
        return None
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


def _make_button_image(text: str, bg: str, fg: str = "white",
                       font_name: str = "Arial", font_size: int = 28,
                       pad_x: int = 28, pad_y: int = 14) -> "ImageTk.PhotoImage | None":
    if not _HAS_PIL:
        return None
    font = _load_font(font_name, font_size, bold=True)
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


def _tool_help_text(name: str) -> str:
    """Return a helpful message telling the user where to obtain a missing external tool."""
    helps = {
        "ww2ogg": (
            "ww2ogg is required to convert WEM files to OGG.\n"
            "  Download: https://github.com/hcs64/ww2ogg/releases\n"
        ),
        "revorb": (
            "revorb is required to fix OGG headers after conversion.\n"
            "  Download: bundled with ww2ogg at https://github.com/hcs64/ww2ogg\n"
        ),
        "codebooks": (
            "packed_codebooks_aoTuV_603.bin is required by ww2ogg for decoding.\n"
            "  Download: bundled with ww2ogg at https://github.com/hcs64/ww2ogg\n"
        ),
        "ffmpeg": (
            "ffmpeg is required for audio format conversion.\n"
            "  Download: https://ffmpeg.org/download.html (Windows builds)\n"
        ),
    }
    return helps.get(name, f"{name}: no additional info available.")


try:
    from pkg_bnk_wwise_tools.CONFIG_tools import ToolPaths
    _HAS_TOOLPATHS = True
except Exception:
    _HAS_TOOLPATHS = False


def _print(level: str, msg: str) -> None:
    color = {"INFO": C_INFO, "OK": C_OK, "WARN": C_WARN, "ERR": C_ERR}.get(level, "")
    label = {"INFO": "[INFO]", "OK": "[OK ]", "WARN": "[WRN]", "ERR": "[ERR]"}.get(level, "[?? ]")
    print(f"{color}{label}{C_RESET} {msg}")


def _run(cmd: list[str], cwd: Optional[Path] = None, capture: bool = True) -> tuple[int, str, str]:
    _print("INFO", f"Running: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout, result.stderr


def _python_exe(venv: bool = True) -> str:
    if sys.platform == "win32":
        return str((VENV_DIR / "Scripts" / "python.exe") if venv else shutil.which("python"))
    return str((VENV_DIR / "bin" / "python") if venv else shutil.which("python"))


def _pip_exe(venv: bool = True) -> str:
    return _python_exe(venv) + " -m pip"


# ------------------------------------------------------------------
# Checks
# ------------------------------------------------------------------

def check_python_version() -> bool:
    major, minor = sys.version_info.major, sys.version_info.minor
    _print("INFO", f"Python {major}.{minor}.{sys.version_info.micro}")
    if major < 3 or (major == 3 and minor < 10):
        _print("ERR", "Python 3.10+ is required.")
        return False
    _print("OK", "Python version OK.")
    return True


def check_gpu_tensorflow(python: str) -> dict:
    """Check TensorFlow GPU availability."""
    _print("INFO", "Checking TensorFlow + GPU...")
    script = (
        "import tensorflow as tf; "
        "print('TF_VERSION:', tf.__version__); "
        "gpus = tf.config.list_physical_devices('GPU'); "
        "print('TF_GPU_COUNT:', len(gpus)); "
        "print('TF_GPU_NAMES:', [g.name for g in gpus])"
    )
    rc, out, err = _run([python, "-c", script], capture=True)
    info = {"available": False, "version": None, "gpu_count": 0, "gpu_names": []}
    for line in (out + err).splitlines():
        if line.startswith("TF_VERSION:"):
            info["version"] = line.split(":", 1)[1].strip()
        elif line.startswith("TF_GPU_COUNT:"):
            info["gpu_count"] = int(line.split(":", 1)[1].strip())
            info["available"] = info["gpu_count"] > 0
        elif line.startswith("TF_GPU_NAMES:"):
            try:
                info["gpu_names"] = json.loads(line.split(":", 1)[1].strip())
            except Exception:
                pass
    return info


def check_gpu_torch(python: str) -> dict:
    """Check PyTorch GPU availability."""
    _print("INFO", "Checking PyTorch + GPU...")
    script = (
        "import torch; "
        "print('TORCH_VERSION:', torch.__version__); "
        "print('TORCH_CUDA:', torch.cuda.is_available()); "
        "print('TORCH_CUDA_VERSION:', torch.version.cuda if torch.cuda.is_available() else 'None'); "
        "print('TORCH_GPU_COUNT:', torch.cuda.device_count() if torch.cuda.is_available() else 0)"
    )
    rc, out, err = _run([python, "-c", script], capture=True)
    info = {"available": False, "version": None, "cuda_version": None, "gpu_count": 0}
    for line in (out + err).splitlines():
        if line.startswith("TORCH_VERSION:"):
            info["version"] = line.split(":", 1)[1].strip()
        elif line.startswith("TORCH_CUDA:"):
            info["available"] = line.split(":", 1)[1].strip().lower() == "true"
        elif line.startswith("TORCH_CUDA_VERSION:"):
            info["cuda_version"] = line.split(":", 1)[1].strip()
        elif line.startswith("TORCH_GPU_COUNT:"):
            try:
                info["gpu_count"] = int(line.split(":", 1)[1].strip())
            except Exception:
                pass
    return info


# ------------------------------------------------------------------
# Venv & Deps
# ------------------------------------------------------------------

def create_venv() -> bool:
    if VENV_DIR.exists():
        _print("INFO", f"Virtual environment exists: {VENV_DIR}")
        # Validate
        py = Path(_python_exe())
        if not py.exists():
            _print("WARN", "Venv appears broken. Recreating...")
            shutil.rmtree(VENV_DIR)
        else:
            _print("OK", "Venv OK.")
            return True

    _print("INFO", "Creating virtual environment...")
    rc, _, err = _run([sys.executable, "-m", "venv", str(VENV_DIR)])
    if rc != 0:
        _print("ERR", f"Failed to create venv: {err}")
        return False
    _print("OK", "Virtual environment created.")
    return True


def install_requirements() -> bool:
    if not REQUIREMENTS.exists():
        _print("ERR", f"{REQUIREMENTS} not found.")
        return False

    # Check freshness
    venv_marker = VENV_DIR / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    req_mtime = REQUIREMENTS.stat().st_mtime
    venv_mtime = venv_marker.stat().st_mtime if venv_marker.exists() else 0

    if venv_mtime >= req_mtime:
        _print("OK", "Requirements already up-to-date.")
        return True

    _print("INFO", f"Installing requirements from {REQUIREMENTS.name}...")
    rc, out, err = _run([_python_exe(), "-m", "pip", "install", "--upgrade", "pip"])
    rc, out, err = _run(
        [_python_exe(), "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    )
    if rc != 0:
        _print("ERR", f"Pip install failed:\n{err}")
        return False
    _print("OK", "Requirements installed.")
    return True


# ------------------------------------------------------------------
# Model setup
# ------------------------------------------------------------------

def setup_audio_models() -> bool:
    _print("INFO", "Setting up audio classification models...")
    rc, out, err = _run(
        [_python_exe(), "-m", "pkg_bnk_wwise_tools", "setup_models", "--setup-all"]
    )
    if rc != 0:
        if out:
            print(out)
        if err:
            print(err, file=sys.stderr)
        _print("WARN", "Audio model setup had issues (see above).")
        return False
    _print("OK", "Audio model setup complete.")
    return True


def _launch_dashboard() -> bool:
    """Auto-discover project_pck_index.json and launch the dashboard."""
    root = Path(__file__).parent
    candidates = [
        root / "extracted" / "project_pck_index.json",
        root / "project_pck_index.json",
        Path("extracted") / "project_pck_index.json",
    ]
    for c in candidates:
        if c.exists():
            _print("INFO", f"Launching dashboard with: {c}")
            subprocess.run(
                [_python_exe(), "-m", "pkg_bnk_wwise_tools", "dashboard", str(c)]
            )
            return True
    return False


# ------------------------------------------------------------------
# Launcher GUI
# ------------------------------------------------------------------

def _scan_progress(out_root: Path) -> dict:
    """Scan extracted directory or progress.json for current pipeline state."""
    progress_json = out_root / "_logs" / "progress.json"
    if progress_json.exists():
        try:
            with open(progress_json, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Fallback: scan folders directly
    packs: list[dict] = []
    total_ogg = 0
    total_transcribed = 0
    for folder in sorted(out_root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("_"):
            continue
        mapping = folder / "mapping.json"
        classification = folder / "classification.json"
        transcription = folder / "transcription.json"

        ogg_count = len(list(folder.glob("*.ogg")))
        total_ogg += ogg_count
        trans_count = 0
        if transcription.exists():
            try:
                with open(transcription, "r", encoding="utf-8") as f:
                    tdata = json.load(f)
                trans_count = len(tdata.get("entries", []))
            except Exception:
                pass
        else:
            # Fallback: count per-OGG sidecars for incremental resume accuracy
            sidecars = list(folder.glob("*_transcribe_vosk.txt"))
            trans_count = len(sidecars)
        total_transcribed += trans_count

        if mapping.exists():
            stage = 1
            if classification.exists():
                stage = 15
            if transcription.exists():
                stage = 2
        else:
            stage = 0

        packs.append({
            "basename": folder.name,
            "stage": stage,
            "ogg_count": ogg_count,
            "transcription_count": trans_count,
            "is_current": False,
        })

    return {
        "timestamp": "",
        "total_packs": len(packs),
        "total_ogg": total_ogg,
        "total_transcribed": total_transcribed,
        "current_basename": "",
        "current_stage": 0,
        "packs": packs,
    }


def _load_historic_averages(out_root: Path) -> dict[int, float]:
    """Parse progress.csv and return average OK duration per stage."""
    csv_path = out_root / "_logs" / "progress.csv"
    if not csv_path.exists():
        return {}
    stage_durations: dict[int, list[float]] = {}
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status") != "OK":
                    continue
                try:
                    stage = int(row["stage"])
                    dur = float(row["duration_sec"])
                except (ValueError, KeyError):
                    continue
                stage_durations.setdefault(stage, []).append(dur)
    except Exception:
        return {}
    return {
        stage: statistics.mean(durs) if len(durs) > 1 else durs[0]
        for stage, durs in stage_durations.items()
        if durs
    }


def _fmt_eta(seconds: float) -> str:
    """Format seconds as a compact human-readable ETA."""
    if seconds <= 0 or not seconds:
        return "-"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


_STAGE_COLORS = {
    0: "#333333",   # not extracted
    1: "#666666",   # extracted
    15: "#999999",  # classified
    2: "#2e7d32",   # transcribed
}

_STAGE_LABELS = {
    0: "Not started",
    1: "Extracted",
    15: "Classified",
    2: "Transcribed",
}


def _show_launcher_gui() -> bool:
    """Open a tkinter launcher window with buttons, progress grid, and live console."""
    if not _HAS_TK:
        _print("WARN", "tkinter not available - falling back to text mode.")
        return False

    root = tk.Tk()
    root.title("Marvel Heroes PCK Editor - Launcher")
    root.geometry("1000x820")
    root.configure(bg=_GUI_BG)

    style = ttk.Style()
    style.theme_use("clam")
    style.configure("TCombobox", fieldbackground=_GUI_ENTRY_BG, foreground=_GUI_FG, background=_GUI_ENTRY_BG)
    style.map("TCombobox", fieldbackground=[("readonly", _GUI_ENTRY_BG), ("active", _GUI_ENTRY_BG)])
    style.map("TCombobox", selectbackground=[("readonly", _GUI_ENTRY_BG)])
    style.map("TCombobox", selectforeground=[("readonly", _GUI_FG)])

    # -- Source dir ------------------------------------------------
    top = tk.Frame(root, bg=_GUI_BG)
    top.pack(fill=tk.X, padx=12, pady=(10, 4))
    tk.Label(top, text="Source:", bg=_GUI_BG, fg=_GUI_FG, font=("Arial", 12)).pack(side=tk.LEFT)
    source_var = tk.StringVar(value=_DEFAULT_SOURCE)
    source_entry = tk.Entry(top, textvariable=source_var, bg=_GUI_ENTRY_BG, fg=_GUI_ENTRY_FG,
                            insertbackground=_GUI_FG, font=("Consolas", 11), width=80)
    source_entry.pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)

    # -- Tool paths row --------------------------------------------
    tools_frame = tk.Frame(root, bg=_GUI_BG)
    tools_frame.pack(fill=tk.X, padx=12, pady=(0, 4))
    tk.Label(tools_frame, text="Tools:", bg=_GUI_BG, fg=_GUI_FG, font=("Arial", 10, "bold")).pack(side=tk.LEFT)

    if _HAS_TOOLPATHS:
        _tp = ToolPaths.load()
        _tool_defaults = {
            "ww2ogg": _tp.ww2ogg,
            "revorb": _tp.revorb,
            "codebooks": _tp.codebooks,
            "ffmpeg": _tp.ffmpeg,
        }
    else:
        _ref_tools = Path(__file__).parent / "ref" / "Wwise-Unpacker" / "Tools"
        _tool_defaults = {
            "ww2ogg": str(_ref_tools / "ww2ogg.exe"),
            "revorb": str(_ref_tools / "revorb.exe"),
            "codebooks": str(_ref_tools / "packed_codebooks_aoTuV_603.bin"),
            "ffmpeg": str(_ref_tools / "ffmpeg.exe"),
        }

    _tool_entries: dict[str, tk.Entry] = {}
    _tool_browse_images: list = []

    def _browse_tool(name: str) -> None:
        ent = _tool_entries[name]
        filetypes = [("Executables", "*.exe")] if name in ("ww2ogg", "revorb", "ffmpeg") else [("All files", "*.*")]
        path = filedialog.askopenfilename(title=f"Select {name}", initialdir=Path(ent.get()).parent, filetypes=filetypes)
        if path:
            ent.delete(0, tk.END)
            ent.insert(0, path)

    for name, val in _tool_defaults.items():
        tk.Label(tools_frame, text=f"{name}:", bg=_GUI_BG, fg=_GUI_FG, font=("Arial", 9)).pack(side=tk.LEFT)
        ent = tk.Entry(tools_frame, bg=_GUI_ENTRY_BG, fg=_GUI_ENTRY_FG, insertbackground=_GUI_FG,
                       font=("Consolas", 9), width=22)
        ent.insert(0, val)
        ent.pack(side=tk.LEFT, padx=(2, 2))
        _tool_entries[name] = ent
        browse_img = _make_browse_image("^", "#337ab7")
        if browse_img:
            browse = tk.Button(tools_frame, image=browse_img, bd=1,
                               highlightthickness=0, relief=tk.RIDGE,
                               bg="#337ab7", activebackground="#337ab7",
                               command=lambda n=name: _browse_tool(n))
            browse.image = browse_img
            _tool_browse_images.append(browse_img)
        else:
            browse = tk.Button(tools_frame, text="...", command=lambda n=name: _browse_tool(n),
                               bg=_GUI_BTN_BLUE, fg="white", font=("Arial", 8), relief=tk.FLAT)
        browse.pack(side=tk.LEFT, padx=(0, 6))

    def _save_tool_paths() -> None:
        if not _HAS_TOOLPATHS:
            return
        tp = ToolPaths(
            ww2ogg=_tool_entries["ww2ogg"].get(),
            revorb=_tool_entries["revorb"].get(),
            codebooks=_tool_entries["codebooks"].get(),
            ffmpeg=_tool_entries["ffmpeg"].get(),
        )
        tp.save()

    def _validate_tools() -> None:
        missing: list[str] = []
        for name, ent in _tool_entries.items():
            if not Path(ent.get()).exists():
                missing.append(name)
        if missing:
            _append("[WARN] Missing external tools detected:\n")
            for name in missing:
                _append(f"  - {name}: NOT FOUND\n")
                _append(f"    {_tool_help_text(name)}\n")
            _append("\n")
            status_var.set(f"Missing tools: {', '.join(missing)}")
        else:
            _append("[OK] All external tools found.\n")

    # -- Buttons ---------------------------------------------------
    btn_frame = tk.Frame(root, bg=_GUI_BG)
    btn_frame.pack(fill=tk.X, padx=12, pady=(4, 4))

    # -- Stats + Progress ------------------------------------------
    prog_frame = tk.LabelFrame(root, text="Pipeline Progress", bg=_GUI_BG, fg=_GUI_FG,
                               font=("Arial", 10, "bold"), padx=4, pady=2)
    prog_frame.pack(fill=tk.X, padx=12, pady=(0, 4))

    # Stats row
    stats_frame = tk.Frame(prog_frame, bg=_GUI_BG)
    stats_frame.pack(fill=tk.X)
    total_var = tk.StringVar(value="Packs: 0  |  .ogg: 0  |  Transcribed: 0")
    current_var = tk.StringVar(value="Current: -")
    tk.Label(stats_frame, textvariable=total_var, bg=_GUI_BG, fg=_GUI_OK,
             font=("Consolas", 11)).pack(side=tk.LEFT)
    tk.Label(stats_frame, textvariable=current_var, bg=_GUI_BG, fg="#ff9800",
             font=("Consolas", 11, "bold")).pack(side=tk.RIGHT)

    _out_root = Path(__file__).parent / "extracted"

    def _open_pack_folder(basename: str) -> None:
        folder = _out_root / basename
        if folder.exists():
            subprocess.Popen(["explorer", str(folder)])

    # Toggle + progress canvases
    show_deu = tk.BooleanVar(value=False)
    show_fra = tk.BooleanVar(value=False)

    # Top row: compact toggle strip on the left, Main canvas filling the rest
    top_row = tk.Frame(prog_frame, bg=_GUI_BG)
    top_row.pack(fill=tk.X, pady=(2, 0))

    toggle_col = tk.Frame(top_row, bg=_GUI_BG)
    toggle_col.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4))
    fra_chk = tk.Checkbutton(toggle_col, text="FRA", variable=show_fra, bg=_GUI_BG, fg=_GUI_FG,
                             selectcolor=_GUI_ENTRY_BG, activebackground=_GUI_BG, activeforeground=_GUI_FG,
                             font=("Arial", 10), anchor=tk.W)
    fra_chk.pack(anchor=tk.W)
    deu_chk = tk.Checkbutton(toggle_col, text="DEU", variable=show_deu, bg=_GUI_BG, fg=_GUI_FG,
                             selectcolor=_GUI_ENTRY_BG, activebackground=_GUI_BG, activeforeground=_GUI_FG,
                             font=("Arial", 10), anchor=tk.W)
    deu_chk.pack(anchor=tk.W)

    main_col = tk.Frame(top_row, bg=_GUI_BG)
    main_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    tk.Label(main_col, text="Main", bg=_GUI_BG, fg=_GUI_FG, font=("Arial", 10, "bold"),
             anchor=tk.W).pack(fill=tk.X)
    main_canvas = tk.Canvas(main_col, bg=_GUI_BG, highlightthickness=0, height=60)
    main_canvas.pack(fill=tk.BOTH, expand=True)

    # Full-width rows below Main (shown only when toggled on)
    fra_row = tk.Frame(prog_frame, bg=_GUI_BG)
    tk.Label(fra_row, text="FRA", bg=_GUI_BG, fg=_GUI_FG, font=("Arial", 10, "bold"),
             anchor=tk.W).pack(fill=tk.X)
    fra_canvas = tk.Canvas(fra_row, bg=_GUI_BG, highlightthickness=0, height=60)
    fra_canvas.pack(fill=tk.BOTH, expand=True)

    deu_row = tk.Frame(prog_frame, bg=_GUI_BG)
    tk.Label(deu_row, text="DEU", bg=_GUI_BG, fg=_GUI_FG, font=("Arial", 10, "bold"),
             anchor=tk.W).pack(fill=tk.X)
    deu_canvas = tk.Canvas(deu_row, bg=_GUI_BG, highlightthickness=0, height=60)
    deu_canvas.pack(fill=tk.BOTH, expand=True)

    deu_blocks: list[int] = []
    fra_blocks: list[int] = []
    main_blocks: list[int] = []
    deu_labels: list[int] = []
    fra_labels: list[int] = []
    main_labels: list[int] = []

    def _draw_col(canvas: tk.Canvas, packs: list[dict], blocks: list[int], labels: list[int], cw: int) -> None:
        for bid in blocks:
            canvas.delete(bid)
        for lid in labels:
            canvas.delete(lid)
        blocks.clear()
        labels.clear()
        if not packs:
            canvas.configure(height=4)
            return
        pad = 2
        cell_w = 40
        cell_h = 36
        sq_w = 32
        sq_h = 18
        cols = max(1, cw // (cell_w + pad))
        for i, p in enumerate(packs):
            col = i % cols
            row = i // cols
            x = pad + col * (cell_w + pad)
            y = pad + row * (cell_h + pad)
            stage = p.get("stage", 0)
            color = _STAGE_COLORS.get(stage, "#333333")
            is_current = p.get("is_current", False)
            outline = "#ff9800" if is_current else "black"
            width = 2 if is_current else 1

            basename = p.get("basename", "")

            # Background cell rect (dark grey with black/orange outline)
            bg_id = canvas.create_rectangle(x, y, x + cell_w, y + cell_h,
                                            fill="#1a1a1a", outline=outline, width=width)
            blocks.append(bg_id)
            canvas.tag_bind(bg_id, "<Double-Button-1>",
                            lambda event, b=basename: _open_pack_folder(b))  # type: ignore[arg-type]

            # Inner colored stage rect (16:9)
            cx = x + (cell_w - sq_w) // 2
            cy = y + 3
            color_id = canvas.create_rectangle(cx, cy, cx + sq_w, cy + sq_h,
                                               fill=color, outline=color)
            blocks.append(color_id)
            canvas.tag_bind(color_id, "<Double-Button-1>",
                             lambda event, b=basename: _open_pack_folder(b))  # type: ignore[arg-type]

            # Short clipped label
            short = basename.replace("SFX_", "").replace("DownloadChunk", "DC")[:7]
            text_id = canvas.create_text(x + cell_w // 2, y + cell_h - 3, text=short,
                                         fill=_GUI_FG, font=("Consolas", 7), anchor=tk.S)
            labels.append(text_id)
            canvas.tag_bind(text_id, "<Double-Button-1>",
                            lambda event, b=basename: _open_pack_folder(b))  # type: ignore[arg-type]

        rows = (len(packs) + cols - 1) // cols if packs else 1
        canvas.configure(height=pad + rows * (cell_h + pad))

    def _draw_progress(data: dict) -> None:
        packs = data.get("packs", [])
        deu_packs = [p for p in packs if p.get("basename", "").endswith("_DEU")]
        fra_packs = [p for p in packs if p.get("basename", "").endswith("_FRA")]
        main_packs = [p for p in packs if not p.get("basename", "").endswith(("_DEU", "_FRA"))]

        if show_deu.get():
            if not deu_row.winfo_manager():
                deu_row.pack(fill=tk.X, pady=(4, 0))
            _draw_col(deu_canvas, deu_packs, deu_blocks, deu_labels, deu_canvas.winfo_width() or 900)
        else:
            if deu_row.winfo_manager():
                deu_row.pack_forget()

        if show_fra.get():
            if not fra_row.winfo_manager():
                fra_row.pack(fill=tk.X, pady=(4, 0))
            _draw_col(fra_canvas, fra_packs, fra_blocks, fra_labels, fra_canvas.winfo_width() or 900)
        else:
            if fra_row.winfo_manager():
                fra_row.pack_forget()

        _draw_col(main_canvas, main_packs, main_blocks, main_labels, main_canvas.winfo_width() or 600)

    def _refresh_progress() -> None:
        out_root = Path(__file__).parent / "extracted"
        data = _scan_progress(out_root)
        packs = data.get("packs", [])

        # Filter by current visibility toggles
        visible = [
            p for p in packs
            if (not p.get("basename", "").endswith("_DEU") or show_deu.get())
            and (not p.get("basename", "").endswith("_FRA") or show_fra.get())
        ]
        total = len(visible)
        ogg = sum(p.get("ogg_count", 0) for p in visible)
        trans = sum(p.get("transcription_count", 0) for p in visible)

        # Percentage + ETA for the active stage (default to transcribe when idle)
        current_stage = data.get("current_stage", 0)
        target_stage = current_stage if current_stage > 0 else 2
        done = sum(1 for p in visible if p.get("stage", 0) >= target_stage)
        pct = (done / total * 100) if total else 0

        averages = _load_historic_averages(out_root)
        avg = averages.get(target_stage, 0)
        remaining = max(0, total - done)
        eta_sec = remaining * avg
        eta_str = _fmt_eta(eta_sec)
        stage_label = {1: "extract", 15: "classify", 2: "transcribe"}.get(target_stage, f"stage {target_stage}")

        total_var.set(
            f"Packs: {done}/{total} ({pct:.0f}%)  |  .ogg: {ogg:,}  |  Transcribed: {trans:,}"
        )
        current_var.set(
            f"ETA: {eta_str}  [{stage_label}]" if remaining else f"Done  [{stage_label}]"
        )
        _draw_progress(data)
        root.after(3000, _refresh_progress)

    # -- Console ---------------------------------------------------
    console = scrolledtext.ScrolledText(root, bg=_GUI_ENTRY_BG, fg=_GUI_FG,
                                        font=("Consolas", 11), insertbackground=_GUI_FG,
                                        wrap=tk.WORD, state=tk.NORMAL, height=12)
    console.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))

    # -- Status ------------------------------------------------------
    status_var = tk.StringVar(value="Ready")
    status = tk.Label(root, textvariable=status_var, bg=_GUI_BG, fg=_GUI_OK,
                      font=("Arial", 11), anchor=tk.W)
    status.pack(fill=tk.X, padx=12, pady=(0, 4))

    def _append(text: str) -> None:
        console.insert(tk.END, text)
        console.see(tk.END)

    def _run_cmd(cmd: list[str], label: str) -> None:
        _save_tool_paths()
        status_var.set(f"Running: {label}...")
        _append(f"\n{'='*60}\n>>> {' '.join(cmd)}\n{'='*60}\n")

        def target():
            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(cmd, **popen_kwargs)
            for line in proc.stdout:
                root.after(0, lambda l=line: _append(l))
            proc.wait()
            rc = proc.returncode
            root.after(0, lambda: status_var.set(
                f"Finished: {label} (exit {rc})" if rc != 0 else f"Finished: {label}"
            ))

        threading.Thread(target=target, daemon=True).start()

    def resume_pipeline():
        src = source_var.get()
        if not Path(src).exists():
            _append(f"[ERROR] Source directory not found: {src}\n")
            status_var.set("Source directory not found")
            return
        _run_cmd([
            _python_exe(), "-m", "pkg_bnk_wwise_tools", "process_all",
            src, "--stage", "2", "-o", "extracted"
        ], "Resume Full Pipeline (Stage 2)")

    def extract_only():
        src = source_var.get()
        if not Path(src).exists():
            _append(f"[ERROR] Source directory not found: {src}\n")
            status_var.set("Source directory not found")
            return
        _run_cmd([
            _python_exe(), "-m", "pkg_bnk_wwise_tools", "process_all",
            src, "--stage", "1", "-o", "extracted"
        ], "Extract Only (Stage 1)")

    def rebuild_index():
        _run_cmd([
            _python_exe(), "-m", "pkg_bnk_wwise_tools", "process_all",
            source_var.get(), "--stage", "3", "-o", "extracted"
        ], "Rebuild Index (Stage 3)")

    def launch_dashboard():
        candidates = [
            Path("extracted") / "project_pck_index.json",
            Path(__file__).parent / "extracted" / "project_pck_index.json",
        ]
        for c in candidates:
            if c.exists():
                _run_cmd([
                    _python_exe(), "-m", "pkg_bnk_wwise_tools", "dashboard", str(c)
                ], "Launch Dashboard")
                return
        _append("[WARN] No project_pck_index.json found. Run pipeline first.\n")
        status_var.set("No project_pck_index.json found")

    def gpu_check():
        _run_cmd([_python_exe(), "-m", "pkg_bnk_wwise_tools", "setup_models", "--gpu-check"], "GPU Check")

    # Image-based action buttons (fills width evenly, like the dashboard)
    btn_frame.columnconfigure(0, weight=1)
    btn_frame.columnconfigure(1, weight=1)
    btn_frame.columnconfigure(2, weight=1)
    btn_frame.columnconfigure(3, weight=1)
    btn_frame.columnconfigure(4, weight=1)
    _action_images: list = []
    _action_btns = [
        ("Resume Pipeline", resume_pipeline, "#5a8a6a"),
        ("Extract Only", extract_only, "#5a7a9a"),
        ("Rebuild Index", rebuild_index, "#8a7a5a"),
        ("EDITOR", launch_dashboard, "#7a6a9a"),
        ("GPU Check", gpu_check, "#5a7a9a"),
    ]
    for i, (text, cmd, bg) in enumerate(_action_btns):
        img = _make_button_image(text, bg, font_size=26, pad_x=20, pad_y=10)
        if img:
            btn = tk.Button(btn_frame, image=img, command=cmd, bd=1,
                            highlightthickness=0, relief=tk.RIDGE,
                            bg=bg, activebackground=bg)
            btn.image = img
            _action_images.append(img)
            btn.grid(row=0, column=i, sticky="nsew", padx=(3 if i else 0, 3))
        else:
            btn = tk.Button(btn_frame, text=text, command=cmd,
                            bg=bg, fg="white", font=("Arial", 11, "bold"),
                            relief=tk.FLAT)
            btn.grid(row=0, column=i, sticky="nsew", padx=(3 if i else 0, 3))

    # Validate tools once after UI is ready, then start polling
    root.after(500, _validate_tools)
    root.after(1000, _refresh_progress)
    root.mainloop()
    return True


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="01_dev_install_and_launch_workflow.py",
        description="MHOPCK Editor installer & launcher",
    )
    parser.add_argument("--skip-venv", action="store_true", help="Use system Python instead of venv")
    parser.add_argument("--gpu-check-only", action="store_true", help="Only check GPU availability and exit")
    parser.add_argument("--no-setup-models", action="store_true", help="Skip audio model download/setup")
    parser.add_argument("--no-dashboard", action="store_true", help="Skip auto-launching the dashboard after setup")
    parser.add_argument("--no-gui", action="store_true", help="Skip launcher GUI and print text commands only")
    args = parser.parse_args(argv)

    LOG_DIR.mkdir(exist_ok=True)

    print("=" * 60)
    print("  MARVEL HEROES OMEGA PCK EDITOR - Setup & Launcher")
    print("=" * 60)
    print()

    if not check_python_version():
        return 1

    use_venv = not args.skip_venv
    python = _python_exe(use_venv)

    if use_venv:
        if not create_venv():
            return 1
        if not install_requirements():
            return 1
    else:
        _print("INFO", "Using system Python (venv skipped).")

    # GPU check
    print()
    print("-" * 60)
    _print("INFO", "GPU / ML Backend Report")
    print("-" * 60)

    tf_info = check_gpu_tensorflow(python)
    if tf_info["version"]:
        _print("OK", f"TensorFlow {tf_info['version']}")
        if tf_info["available"]:
            _print("OK", f"  TF GPU: {tf_info['gpu_count']} device(s) - {', '.join(tf_info['gpu_names'])}")
        else:
            _print("WARN", "  TF GPU: not available (will use CPU)")
    else:
        _print("WARN", "TensorFlow not installed or failed to import.")

    torch_info = check_gpu_torch(python)
    if torch_info["version"]:
        _print("OK", f"PyTorch {torch_info['version']}")
        if torch_info["available"]:
            _print("OK", f"  Torch CUDA: {torch_info['cuda_version']} - {torch_info['gpu_count']} device(s)")
        else:
            _print("WARN", "  Torch CUDA: not available (will use CPU)")
    else:
        _print("WARN", "PyTorch not installed or failed to import.")

    if args.gpu_check_only:
        return 0

    print()
    print("-" * 60)
    if not args.no_setup_models:
        setup_audio_models()
    else:
        _print("INFO", "Skipping audio model setup (--no-setup-models).")

    print()
    print("=" * 60)
    _print("OK", "Setup complete!")
    print("=" * 60)
    print()

    # Show launcher GUI by default; fall back to text commands if --no-gui
    if not args.no_gui:
        gui_ok = _show_launcher_gui()
        if gui_ok:
            return 0
        _print("INFO", "GUI unavailable - falling back to text commands.")
        print()

    # Fallback: old text-mode behaviour
    if not args.no_dashboard:
        launched = _launch_dashboard()
        if launched:
            return 0
        _print("WARN", "No project_pck_index.json found - dashboard not launched.")
        _print("INFO", "Run the full pipeline first to generate transcription data.")
        print()

    print("Available commands (activate venv first):")
    print()
    print("  1. Extract single .pck")
    print("     python -m pkg_bnk_wwise_tools extract <file.pck> -o extracted")
    print()
    print("  2. Batch process all .pck files (full pipeline)")
    print("     python -m pkg_bnk_wwise_tools process_all <dir> --stage 2")
    print()
    print("  3. Rebuild project PCK index only")
    print("     python -m pkg_bnk_wwise_tools process_all <dir> --stage 3")
    print()
    print("  4. Open search dashboard")
    print("     python -m pkg_bnk_wwise_tools dashboard extracted\\project_pck_index.json")
    print()
    print("  5. Run audio classification only (Stage 1.5)")
    print("     python -m pkg_bnk_wwise_tools process_all <dir> --stage 15")
    print()
    print("  6. Dry-run preview")
    print("     python -m pkg_bnk_wwise_tools process_all <dir> --dry-run")
    print()

    if sys.platform == "win32" and use_venv:
        print("To activate the virtual environment in this terminal:")
        print(f"    {VENV_DIR}\\Scripts\\activate.bat")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
