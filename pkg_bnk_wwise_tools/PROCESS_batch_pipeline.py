"""
Incremental, resumable batch processor for all .pck files in a directory.

Runs in stages:
    Stage 1:   extract + ogg + mapping + playlist       (fast)
    Stage 1.5: audio classification (YAMNet/PANNs)    (fast)
    Stage 2:   transcribe (optionally filtered by 1.5)   (slow)
    Stage 3:   build master index

Usage:
    python -m pkg_bnk_wwise_tools.process_all <source_dir> [--stage N] [--output DIR]

Resumes : skips folders where the stage artifacts already exist.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .UTIL_logger import Level, Logger
from .DATA_project_pck_index import build_project_pck_index
from .TRANSCRIBE_dialogue import OggTranscriber
from .PARSE_pck_bnk import PckBnkParser
from .CONVERT_wem import WemConverter
from .TRANSCRIBE_sfx_classifier import AudioClassifier
from .CONFIG_tools import ToolPaths

__all__ = ["PlaylistMaker", "run", "main"]


# -- PlaylistMaker (merged from playlist_maker.py) ------------------

class PlaylistMaker:
    """Concatenate a folder of .ogg files into one review track."""

    def __init__(self, tool_paths: Optional[ToolPaths] = None, logger: Optional[Logger] = None):
        self.log = logger or Logger(Level.INFO)
        self.tp = tool_paths or ToolPaths.load()
        if not Path(self.tp.ffmpeg).exists():
            raise RuntimeError(f"ffmpeg not found at: {self.tp.ffmpeg}")

    def make_playlist(
        self,
        ogg_dir: Path,
        output_wav: Path,
        pattern: str = "*.ogg",
        include_index_announcements: bool = False,
    ) -> Path:
        """Concatenate all matching .ogg files into a single WAV."""
        ogg_dir = Path(ogg_dir)
        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)

        ogg_files = sorted(ogg_dir.glob(pattern))
        if not ogg_files:
            self.log.warn(f"No .ogg files found in {ogg_dir}")
            return output_wav

        self.log.info(f"Building playlist from {len(ogg_files)} file(s)")

        # Build ffmpeg concat demuxer file list
        with tempfile.TemporaryDirectory() as tmpdir:
            list_path = Path(tmpdir) / "concat_list.txt"
            lines: List[str] = []
            for i, ogg in enumerate(ogg_files):
                lines.append(f"file '{ogg.resolve()}'")
                if include_index_announcements:
                    # Placeholder: could generate a short TTS beep here
                    pass
            list_path.write_text("\n".join(lines), encoding="utf-8")

            with self.log.step("Concatenate with ffmpeg"):
                result = subprocess.run(
                    [
                        self.tp.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "concat", "-safe", "0",
                        "-i", str(list_path),
                        "-c", "pcm_s16le", "-ar", "44100", "-ac", "2",
                        str(output_wav),
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    self.log.error(f"ffmpeg concat failed: {result.stderr.strip()}")
                    raise RuntimeError("Playlist creation failed")

        size = output_wav.stat().st_size
        self.log.ok(f"Playlist written: {output_wav.name} ({size:,} bytes)")
        return output_wav


def _is_done(out_dir: Path, stage: int) -> bool:
    mapping = out_dir / "mapping.json"
    transcription = out_dir / "transcription.json"
    playlist = out_dir / "review_playlist.wav"
    classification = out_dir / "classification.json"
    if stage == 1:
        return mapping.exists() and playlist.exists()
    if stage == 15:
        return classification.exists()
    if stage == 2:
        return transcription.exists()
    return False


def _lang_suffix(stem: str) -> str | None:
    """Return the trailing language suffix (e.g. '_INT', '_DEU', '_FRA') or None."""
    if "_" not in stem:
        return None
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1]:
        return parts[1]
    return None


def _matches_language(stem: str, allowed: set[str]) -> bool:
    """Check if a .pck stem matches the requested language filter.

    Rules:
      - If the stem ends with an allowed suffix (e.g. _INT), include it.
      - If no recognized suffix is found and 'INT' is allowed, treat as English/INT.
    """
    suffix = _lang_suffix(stem)
    if suffix in allowed:
        return True
    if suffix is None and "INT" in allowed:
        return True
    return False


def _auto_detect_stage(pck_files: List[Path], out_root: Path, use_classifier: bool = True) -> int:
    needs_s1 = [p for p in pck_files if not _is_done(out_root / p.stem, 1)]
    if needs_s1:
        return 1
    if use_classifier:
        needs_s15 = [p for p in pck_files if not _is_done(out_root / p.stem, 15)]
        if needs_s15:
            return 15
    needs_s2 = [p for p in pck_files if not _is_done(out_root / p.stem, 2)]
    if needs_s2:
        return 2
    return 3


def _write_progress(progress_path: Path, basename: str, stage: int, status: str, duration: float) -> None:
    fieldnames = ["timestamp", "basename", "stage", "status", "duration_sec"]
    exists = progress_path.exists()
    with open(progress_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().isoformat(),
            "basename": basename,
            "stage": stage,
            "status": status,
            "duration_sec": round(duration, 2),
        })


def _write_progress_json(out_root: Path, current_basename: str = "", current_stage: int = 0) -> None:
    """Write a JSON snapshot of overall progress for external consumers (e.g. GUI)."""
    log_dir = out_root / "_logs"
    log_dir.mkdir(exist_ok=True)
    json_path = log_dir / "progress.json"

    packs: list[dict] = []
    total_ogg = 0
    total_transcribed = 0

    for folder in sorted(out_root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("_"):
            continue
        mapping = folder / "mapping.json"
        classification = folder / "classification.json"
        transcription = folder / "transcription.json"

        # Count .ogg files
        ogg_count = len(list(folder.glob("*.ogg")))
        total_ogg += ogg_count

        # Count transcribed entries
        trans_count = 0
        if transcription.exists():
            try:
                with open(transcription, "r", encoding="utf-8") as f:
                    tdata = json.load(f)
                trans_count = len(tdata.get("entries", []))
            except Exception:
                pass
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
            "is_current": folder.name == current_basename,
        })

    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "total_packs": len(packs),
        "total_ogg": total_ogg,
        "total_transcribed": total_transcribed,
        "current_basename": current_basename,
        "current_stage": current_stage,
        "packs": packs,
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)


def _stage1(pck_path: Path, out_dir: Path, model_path: str, logger: Logger, tool_paths: Optional[ToolPaths] = None) -> bool:
    """Extract + OGG + mapping + playlist."""
    out_dir.mkdir(parents=True, exist_ok=True)
    basename = pck_path.stem

    # 1. Parse & extract
    parser = PckBnkParser.from_file(str(pck_path), logger=logger)
    parser.parse()
    wem_paths: list[Path] = []
    with logger.step("Extract WEMs"):
        for wem in parser.wems:
            name = f"{wem.descriptor.wem_id:08x}_{wem.index}.wem"
            path = out_dir / name
            path.write_bytes(wem.data)
            wem_paths.append(path)
        logger.ok(f"Extracted {len(wem_paths)} .wem file(s)")

    # 2. Convert to OGG
    converter = WemConverter(tool_paths=tool_paths, logger=logger)
    converter.convert_batch(wem_paths, out_dir)

    # 3. Save mapping
    mapping = {
        "source_pck": basename,
        "extracted_at": datetime.now().isoformat(),
        "wems": [
            {
                "index": wem.index,
                "wem_id": f"{wem.descriptor.wem_id:08x}",
                "filename_wem": f"{wem.descriptor.wem_id:08x}_{wem.index}.wem",
                "filename_ogg": f"{wem.descriptor.wem_id:08x}_{wem.index}.ogg",
                "offset": wem.descriptor.offset,
                "length": wem.descriptor.length,
                "is_loose": wem.is_loose,
            }
            for wem in parser.wems
        ],
    }
    map_path = out_dir / "mapping.json"
    map_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    logger.ok(f"Saved mapping to {map_path.name}")

    # 4. Playlist
    try:
        maker = PlaylistMaker(tool_paths=tool_paths, logger=logger)
        maker.make_playlist(out_dir, out_dir / "review_playlist.wav")
    except RuntimeError as e:
        logger.error(f"Playlist failed: {e}")
        return False

    return True


def _stage1_5(out_dir: Path, logger: Logger) -> bool:
    """Audio classification (YAMNet / PANNs)."""
    try:
        classifier = AudioClassifier(logger=logger, use_yamnet=True, use_panns=True)
        results = classifier.classify_folder(out_dir, pattern="*.ogg", top_k=5)
        classifier.save(results, out_dir / "classification.json")
    except RuntimeError as e:
        logger.error(f"Classification failed: {e}")
        return False
    except Exception as e:
        logger.error(f"Classification exception: {e}")
        return False
    return True


def _stage2(out_dir: Path, model_path: str, logger: Logger, use_classifier: bool = True, force: bool = False, tool_paths: Optional[ToolPaths] = None) -> bool:
    """Transcribe only. If classification.json exists, filter to dialogue files."""
    clf_path = out_dir / "classification.json"
    try:
        transcriber = OggTranscriber(model_path, tool_paths=tool_paths, logger=logger)
        transcriber.transcribe_folder(
            out_dir,
            out_dir / "transcription.json",
            classification_json=clf_path if use_classifier and clf_path.exists() else None,
            force=force,
        )
    except RuntimeError as e:
        logger.error(f"Transcription failed: {e}")
        return False
    return True


def run(
    source_dir: Path,
    out_root: Path,
    model_path: str,
    stage: int = 0,
    force: bool = False,
    dry_run: bool = False,
    languages: list[str] | None = None,
    use_classifier: bool = True,
    packs: list[str] | None = None,
    tool_paths: Optional[ToolPaths] = None,
    logger: Optional[Logger] = None,
) -> int:
    logger = logger or Logger(Level.INFO)
    source_dir = Path(source_dir)
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    log_dir = out_root / "_logs"
    log_dir.mkdir(exist_ok=True)
    progress_path = log_dir / "progress.csv"

    pck_files = sorted(source_dir.glob("*.pck"))
    if not pck_files:
        logger.error(f"No .pck files found in {source_dir}")
        return 1

    # Target pack filtering
    if packs:
        allowed = {p.lower() for p in packs}
        pck_files = [p for p in pck_files if p.stem.lower() in allowed]
        if not pck_files:
            logger.error(f"None of the specified packs found in {source_dir}")
            return 1
        logger.info(f"Target filter: processing {len(pck_files)} specific pack(s)")

    # Language filtering applies to Stage 2 (transcribe) only
    allowed_langs = set((languages or ["INT"]))
    if stage == 2:
        filtered = [p for p in pck_files if _matches_language(p.stem, allowed_langs)]
        skipped_lang = len(pck_files) - len(filtered)
        if skipped_lang:
            logger.info(f"Language filter ({', '.join(sorted(allowed_langs))}): {skipped_lang} .pck file(s) excluded")
        pck_files = filtered

    logger.info(f"Found {len(pck_files)} .pck file(s)")

    if stage == 0:
        stage = _auto_detect_stage(pck_files, out_root, use_classifier=use_classifier)
        logger.info(f"Auto-detected Stage {stage}")

    if dry_run:
        logger.info("=== DRY RUN ===")
        for pck in pck_files:
            done = _is_done(out_root / pck.stem, stage) and not force
            action = "SKIP" if done else "PROCESS"
            if stage == 2:
                suffix = _lang_suffix(pck.stem) or "INT"
                action += f"  [{suffix}]"
            logger.info(f"{action} : {pck.name}")
        logger.info("=== END DRY RUN ===")
        return 0

    to_process = [p for p in pck_files if force or not _is_done(out_root / p.stem, stage)]

    skipped = len(pck_files) - len(to_process)
    stage_label = "1.5" if stage == 15 else str(stage)
    logger.info(f"Processing {len(to_process)} file(s) in Stage {stage_label} ({skipped} skipped)")

    # Initial progress snapshot
    _write_progress_json(out_root)

    processed = 0
    failed = 0

    for pck in to_process:
        basename = pck.stem
        out_dir = out_root / basename
        logger.info("=" * 50)
        logger.info(f"Stage {stage} : {basename}")
        logger.info("=" * 50)

        # Mark current pack in progress snapshot
        _write_progress_json(out_root, current_basename=basename, current_stage=stage)

        t0 = time.time()
        ok = False
        try:
            if stage == 1:
                ok = _stage1(pck, out_dir, model_path, logger, tool_paths)
            elif stage == 15:
                ok = _stage1_5(out_dir, logger)
            elif stage == 2:
                ok = _stage2(out_dir, model_path, logger, use_classifier=use_classifier, force=force, tool_paths=tool_paths)
            elif stage == 3:
                ok = True
        except Exception as e:
            logger.error(f"EXCEPTION: {e}")
            ok = False

        duration = time.time() - t0
        status = "OK" if ok else "FAIL"
        _write_progress(progress_path, basename, stage, status, duration)

        if ok:
            processed += 1
            logger.ok(f"OK : {basename} ({duration:.1f}s)")
        else:
            failed += 1
            logger.error(f"FAILED : {basename} ({duration:.1f}s)")

    # Final snapshot - clear current
    _write_progress_json(out_root)

    logger.info("=" * 50)
    color = "ok" if failed == 0 else "warn"
    stage_label = "1.5" if stage == 15 else str(stage)
    getattr(logger, color)(f"Stage {stage_label} complete: {processed} processed, {failed} failed, {skipped} skipped")
    logger.info("=" * 50)

    # Stage 3: Build project PCK index
    if stage in (2, 3):
        index_path = out_root / "project_pck_index.json"
        logger.info(f"Building project PCK index: {index_path}")
        try:
            build_project_pck_index(out_root, index_path, logger=logger)
            logger.ok("Project PCK index OK")
        except Exception as e:
            logger.error(f"Project PCK index failed: {e}")

    # Next step hint
    if stage == 1:
        logger.info("Next: run Stage 2 (transcribe) with:")
        logger.info(f"  python -m pkg_bnk_wwise_tools process_all {source_dir} --stage 2")
    elif stage == 2:
        logger.info("Next: open dashboard with:")
        logger.info(f"  python -m pkg_bnk_wwise_tools dashboard {out_root / 'project_pck_index.json'}")

    return 0 if failed == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pkg_bnk_wwise_tools process_all",
        description="Incremental, resumable batch processor for all .pck files",
    )
    parser.add_argument("source_dir", help="Directory containing .pck files")
    parser.add_argument("-o", "--output", default="extracted", help="Root output directory")
    parser.add_argument("--model", default="./vosk-model-en-us-0.42-gigaspeech", help="Path to Vosk model")
    parser.add_argument("--stage", type=int, default=0, choices=[0, 1, 15, 2, 3], help="0=auto, 1=extract, 15=classify, 2=transcribe, 3=index")
    parser.add_argument("--languages", default="INT", help="Comma-separated language suffixes for Stage 2 (e.g. INT,DEU,FRA)")
    parser.add_argument("--no-classifier", action="store_true", help="Skip Stage 1.5 audio classification and do not filter Stage 2 by it")
    parser.add_argument("--packs", default="", help="Comma-separated pack name(s) to process (e.g. SFX_Angela_INT,SFX_ScarletWitch_INT). Default = all.")
    parser.add_argument("--force", action="store_true", help="Reprocess existing folders")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed without running")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logs")
    args = parser.parse_args(argv)

    lang_list = [s.strip().upper() for s in args.languages.split(",")]
    pack_list = [s.strip() for s in args.packs.split(",")] if args.packs else None

    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    return run(
        Path(args.source_dir),
        Path(args.output),
        args.model,
        stage=args.stage,
        force=args.force,
        dry_run=args.dry_run,
        languages=lang_list,
        use_classifier=not args.no_classifier,
        packs=pack_list,
        logger=logger,
    )


if __name__ == "__main__":
    sys.exit(main())
