"""
External tool path configuration. 
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

__all__ = ["ToolPaths"]


PACKAGE_DIR = Path(__file__).parent.resolve()
REF_DIR = PACKAGE_DIR.parent / "ref"
_DEFAULT_TOOLS = REF_DIR / "Wwise-Unpacker" / "Tools"
_DEFAULT_CONFIG = PACKAGE_DIR / "tools_config.json"


@dataclass
class ToolPaths:
    """Paths to external audio toolkit binaries."""

    ww2ogg: str
    revorb: str
    codebooks: str
    ffmpeg: str

    # -- Factory methods -----------------------------------------

    @classmethod
    def defaults(cls) -> "ToolPaths":
        """Use the bundled ref/Wwise-Unpacker/Tools/ paths."""
        return cls(
            ww2ogg=str(_DEFAULT_TOOLS / "ww2ogg.exe"),
            revorb=str(_DEFAULT_TOOLS / "revorb.exe"),
            codebooks=str(_DEFAULT_TOOLS / "packed_codebooks_aoTuV_603.bin"),
            ffmpeg=str(_DEFAULT_TOOLS / "ffmpeg.exe"),
        )

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "ToolPaths":
        """Load from JSON or fall back to defaults."""
        path = path or _DEFAULT_CONFIG
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return cls._from_dict(data)
            except Exception:
                pass
        return cls.defaults()

    # -- Persistence ---------------------------------------------

    def save(self, path: Optional[Path] = None) -> Path:
        """Write current paths to JSON."""
        path = path or _DEFAULT_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path

    # -- Validation ----------------------------------------------

    def validate(self) -> dict[str, bool]:
        """Check whether each configured path exists."""
        return {
            "ww2ogg": Path(self.ww2ogg).exists(),
            "revorb": Path(self.revorb).exists(),
            "codebooks": Path(self.codebooks).exists(),
            "ffmpeg": Path(self.ffmpeg).exists(),
        }

    def missing(self) -> list[str]:
        """Return a list of tool names whose paths do not exist."""
        return [name for name, ok in self.validate().items() if not ok]

    # -- Helpers -------------------------------------------------

    @classmethod
    def _from_dict(cls, d: dict) -> "ToolPaths":
        defaults = cls.defaults()
        return cls(
            ww2ogg=d.get("ww2ogg", defaults.ww2ogg),
            revorb=d.get("revorb", defaults.revorb),
            codebooks=d.get("codebooks", defaults.codebooks),
            ffmpeg=d.get("ffmpeg", defaults.ffmpeg),
        )
