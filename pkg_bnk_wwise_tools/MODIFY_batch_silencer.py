"""
batch_silencer - read silence designations and modify .pck files in-place.

Workflow (default):
    1. Load silence_designations.json
    2. Group by pack
    3. For each pack:
         a. Locate original .pck in source_dir
         b. Backup to backup_YYYY-MM-DD/ beside source_dir
         c. Parse .pck to find WEM offsets
         d. Replace designated WEM data with silent audio
         e. Overwrite the original .pck in source_dir

Usage:
    python -m pkg_bnk_wwise_tools batch_silence <source_dir> <silence_json>

Produces (default):
    backup_YYYY-MM-DD/   original .pck files (unchanged)
    (source_dir)/        modified .pck files with silenced entries

Use --output to write modified files elsewhere instead.
"""

from __future__ import annotations

import struct
from datetime import date
from pathlib import Path
from typing import List, Optional

from .UTIL_logger import Level, Logger
from .PARSE_pck_bnk import PckBnkParser
from .DATA_silence_manager import SilenceManager
from .MODIFY_repackager import zero_fill_riff

__all__ = [
    "run_batch_silence",
    "main",
]


def _replace_wem_in_pck(
    pck_data: bytearray,
    wems: list,
    target_wem_ids: set,
    logger: Logger,
    data_start: int = 0,
) -> int:
    """Replace designated WEMs with silent data. Returns count replaced."""
    replaced = 0
    for wem in wems:
        wid = f"{wem.descriptor.wem_id:08x}"
        if wid not in target_wem_ids:
            continue

        # For BNK-embedded WEMs, offset is relative to DATA section start.
        # For loose WEMs, offset is absolute in the PCK file.
        offset = wem.descriptor.offset
        if not wem.is_loose:
            offset = data_start + offset

        length = wem.descriptor.length
        if offset + length > len(pck_data):
            logger.warn(f"WEM {wid} offset out of bounds, skipping")
            continue
        original = bytes(pck_data[offset:offset + length])
        silent = zero_fill_riff(original)
        # Pad or trim to exact original length
        if len(silent) < length:
            silent = silent + b"\x00" * (length - len(silent))
        elif len(silent) > length:
            silent = silent[:length]
        pck_data[offset:offset + length] = silent
        replaced += 1
        logger.ok(f"Silenced WEM {wid} at 0x{offset:x}")
    return replaced


def run_batch_silence(
    source_dir: Path,
    silence_json: Path,
    output_dir: Optional[Path] = None,
    logger: Optional[Logger] = None,
) -> Path:
    """
    Main entry point.  Reads silence designations and produces modified .pck files.
    """
    logger = logger or Logger(Level.INFO)
    source_dir = Path(source_dir)
    silence_json = Path(silence_json)

    if not silence_json.exists():
        raise FileNotFoundError(f"Silence designations not found: {silence_json}")

    mgr = SilenceManager(silence_json)
    if mgr.count == 0:
        logger.warn("No silence designations - nothing to do.")
        return Path()

    # Determine output / backup directories
    today = date.today().isoformat()
    if output_dir is None:
        # Default: in-place mode. Backup beside source_dir, overwrite in source_dir.
        backup_dir = source_dir.parent / f"backup_{today}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        write_dir = source_dir
        logger.info(f"Mode         : in-place (overwrite originals)")
    else:
        # Explicit output dir: legacy mode
        out_root = output_dir
        out_root.mkdir(parents=True, exist_ok=True)
        backup_dir = out_root / "backup"
        backup_dir.mkdir(exist_ok=True)
        write_dir = out_root
        logger.info(f"Output folder: {out_root}")
    logger.info(f"Backup folder: {backup_dir}")
    logger.info(f"Designations : {mgr.count}")

    by_pack = mgr.by_pack()
    total_packs = len(by_pack)
    total_replaced = 0

    for pack_name, items in by_pack.items():
        logger.info("=" * 50)
        logger.info(f"Pack: {pack_name}")

        # Find original .pck
        pck_path = source_dir / f"{pack_name}.pck"
        if not pck_path.exists():
            logger.warn(f"  Original .pck not found: {pck_path}")
            continue

        # Backup
        backup_path = backup_dir / f"{pack_name}.pck"
        if not backup_path.exists():
            backup_path.write_bytes(pck_path.read_bytes())
            logger.ok(f"  Backed up -> {backup_path}")

        # Parse to get WEM offsets
        parser = PckBnkParser.from_file(pck_path, logger=logger)
        parser.parse()

        if not parser.wems:
            logger.warn(f"  No WEMs found in {pck_path.name}")
            continue

        # Target WEM IDs from designations
        target_ids = {item.wem_id.lower() for item in items}

        # Compute DATA section start for BNK-embedded WEM offset correction
        data_start = 0
        for sec in parser.sections:
            if sec.identifier == "DATA":
                data_start = sec.offset + 8
                break

        # Build modified .pck
        pck_data = bytearray(pck_path.read_bytes())
        replaced = _replace_wem_in_pck(pck_data, parser.wems, target_ids, logger, data_start=data_start)
        total_replaced += replaced

        # Write
        out_pck = write_dir / f"{pack_name}.pck"
        out_pck.write_bytes(pck_data)
        logger.ok(f"  Written modified -> {out_pck.name} ({replaced} WEM(s) silenced)")

    logger.info("=" * 50)
    logger.ok(f"Done: {total_replaced} WEM(s) silenced across {total_packs} pack(s)")
    logger.info(f"Modified files: {write_dir}")
    logger.info(f"Backup files  : {backup_dir}")

    return write_dir


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="pkg_bnk_wwise_tools batch_silence",
        description="Batch silence designated WEMs in .pck files",
    )
    parser.add_argument("source_dir", help="Directory containing original .pck files")
    parser.add_argument("silence_json", help="Path to silence_designations.json")
    parser.add_argument(
        "-o", "--output",
        help="Output directory (default: in-place, backup to backup_YYYY-MM-DD)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logs")
    args = parser.parse_args(argv)

    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    try:
        run_batch_silence(
            Path(args.source_dir),
            Path(args.silence_json),
            output_dir=Path(args.output) if args.output else None,
            logger=logger,
        )
    except Exception as e:
        logger.error(str(e))
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
