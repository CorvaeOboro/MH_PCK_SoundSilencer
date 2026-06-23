"""
Project PCK Index Builder - aggregate transcription results across multiple .pck files.

Scans a directory tree for transcription.json files and merges them into
a single project_pck_index.json, preserving which .pck each entry came from.

Example:
    python -m pkg_bnk_wwise_tools index ./extracted -o project_pck_index.json
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .UTIL_logger import Logger, log

__all__ = ["build_project_pck_index"]


def build_project_pck_index(
    root_dir: Path,
    output_path: Path,
    logger: Optional[Logger] = None,
) -> Path:
    """Scan for transcription.json files and build a merged project PCK index."""
    logger = logger or log
    root_dir = Path(root_dir)
    output_path = Path(output_path)

    # Find all transcription.json files
    transcription_files = sorted(root_dir.rglob("transcription.json"))
    if not transcription_files:
        logger.warn(f"No transcription.json files found under {root_dir}")
        return output_path

    logger.info(f"Found {len(transcription_files)} transcription file(s)")

    all_entries: List[Dict[str, Any]] = []
    pck_summaries: List[Dict[str, Any]] = []

    for tfile in transcription_files:
        try:
            with open(tfile, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warn(f"Could not read {tfile}: {e}")
            continue

        # Determine which .pck this belongs to from the parent folder
        pck_basename = tfile.parent.name
        rel_dir = str(tfile.parent.relative_to(root_dir))

        # Load classification data if available
        clf_file = tfile.parent / "classification.json"
        clf_lookup: Dict[str, Dict[str, Any]] = {}
        if clf_file.exists():
            try:
                with open(clf_file, "r", encoding="utf-8") as f:
                    clf_data = json.load(f)
                for item in clf_data.get("files", []):
                    clf_lookup[item["filename"]] = item
            except Exception as e:
                logger.warn(f"Could not read classification {clf_file}: {e}")

        entries = data.get("entries", [])
        speech_count = sum(1 for e in entries if e.get("transcription"))
        silent_count = sum(1 for e in entries if not e.get("transcription") and not e.get("error"))
        error_count = sum(1 for e in entries if e.get("error"))

        # Count dialogue from classification if available
        dialogue_count = sum(
            1 for e in entries
            if clf_lookup.get(e.get("filename", ""), {}).get("is_dialogue")
        )

        pck_summaries.append({
            "pck_basename": pck_basename,
            "rel_dir": rel_dir,
            "transcription_json": str(tfile.resolve()),
            "classification_json": str(clf_file.resolve()) if clf_file.exists() else None,
            "entry_count": len(entries),
            "speech_count": speech_count,
            "silent_count": silent_count,
            "error_count": error_count,
            "dialogue_count": dialogue_count,
        })

        for e in entries:
            entry = dict(e)
            entry["_pck"] = pck_basename
            entry["_rel_dir"] = rel_dir
            # Merge classification data
            clf = clf_lookup.get(entry.get("filename", ""))
            if clf:
                entry["_is_dialogue"] = clf.get("is_dialogue", False)
                entry["_dialogue_confidence"] = clf.get("dialogue_confidence", 0.0)
                entry["_classifications"] = clf.get("classifications", [])
                entry["_models_used"] = clf.get("models_used", [])
            all_entries.append(entry)

        logger.ok(
            f"{pck_basename}: {len(entries)} entries "
            f"({speech_count} speech, {silent_count} silent, {error_count} errors)"
        )

    master: Dict[str, Any] = {
        "is_project_pck_index": True,
        "generated_at": datetime.now().isoformat(),
        "root_dir": str(root_dir.resolve()),
        "total_pcks": len(pck_summaries),
        "total_entries": len(all_entries),
        "pcks": pck_summaries,
        "entries": all_entries,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(master, f, indent=2, ensure_ascii=False)
    logger.ok(f"Project PCK index written: {output_path.name} ({len(all_entries)} entries from {len(pck_summaries)} .pck file(s))")

    return output_path
