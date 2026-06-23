"""
SilenceManager - persistent storage for audio entries marked for silencing.

Provides:
    - Load / save silence designations to a JSON file
    - Add / remove / query designations by unique key
    - Export designations grouped by PCK for batch repackaging

"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["SilenceDesignation", "SilenceManager"]


@dataclass
class SilenceDesignation:
    """A single human-reviewed entry marked for silencing."""

    pack: str                      # e.g. SFX_Rogue_INT
    filename: str                  # e.g. 0001a4b2_5.ogg
    wem_id: str                    # e.g. 0001a4b2
    ogg_path: str                  # absolute path to extracted .ogg
    transcription: str = ""         # transcription text (for context)
    reason: str = ""               # optional reviewer note
    designated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    reviewer_id: str = ""        # could be hostname or manual id
    _id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def key(self) -> str:
        return f"{self.pack}::{self.filename}"


class SilenceManager:
    """Manages the silence_designations.json file."""

    def __init__(self, json_path: Path | str):
        self.json_path = Path(json_path)
        self._designations: Dict[str, SilenceDesignation] = {}
        self._load()

    # -- Persistence -----------------------------------------------

    def _load(self) -> None:
        if not self.json_path.exists():
            return
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            self.log.warn(f"Could not load silence designations: {e}")
            return
        valid = {f.name for f in fields(SilenceDesignation)}
        for item in raw.get("designations", []):
            try:
                sd = SilenceDesignation(**{k: v for k, v in item.items() if k in valid})
                self._designations[sd.key] = sd
            except Exception as e:
                self.log.warn(f"Skipping invalid designation: {e}")

    def save(self) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now().isoformat(),
            "designations": [asdict(d) for d in self._designations.values()],
        }
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    # -- CRUD ------------------------------------------------------

    def add(self, sd: SilenceDesignation) -> None:
        self._designations[sd.key] = sd

    def remove(self, key: str) -> bool:
        if key in self._designations:
            del self._designations[key]
            return True
        return False

    def toggle(self, pack: str, filename: str, wem_id: str, ogg_path: str,
               transcription: str = "", reason: str = "") -> bool:
        """Toggle designation. Returns True if now designated, False if removed."""
        key = f"{pack}::{filename}"
        if key in self._designations:
            del self._designations[key]
            return False
        self._designations[key] = SilenceDesignation(
            pack=pack, filename=filename, wem_id=wem_id,
            ogg_path=ogg_path, transcription=transcription, reason=reason,
        )
        return True

    def is_designated(self, pack: str, filename: str) -> bool:
        return f"{pack}::{filename}" in self._designations

    def get(self, pack: str, filename: str) -> Optional[SilenceDesignation]:
        return self._designations.get(f"{pack}::{filename}")

    def clear_all(self) -> None:
        self._designations.clear()

    # -- Queries ----------------------------------------------------

    @property
    def count(self) -> int:
        return len(self._designations)

    def all(self) -> List[SilenceDesignation]:
        return list(self._designations.values())

    def by_pack(self) -> Dict[str, List[SilenceDesignation]]:
        groups: Dict[str, List[SilenceDesignation]] = {}
        for sd in self._designations.values():
            groups.setdefault(sd.pack, []).append(sd)
        return groups

    # -- Export for batch repackage -------------------------------

    def export_for_repackage(self, output_path: Optional[Path] = None) -> Path:
        """Write a summary JSON for the batch_silencer pipeline."""
        output_path = output_path or self.json_path.with_name("repackage_plan.json")
        plan = {
            "generated_at": datetime.now().isoformat(),
            "total_designations": self.count,
            "packs": {},
        }
        for pack, items in self.by_pack().items():
            plan["packs"][pack] = {
                "count": len(items),
                "files": [
                    {
                        "filename": i.filename,
                        "wem_id": i.wem_id,
                        "ogg_path": i.ogg_path,
                        "transcription": i.transcription,
                        "reason": i.reason,
                    }
                    for i in items
                ],
            }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2, ensure_ascii=False)
        return output_path
