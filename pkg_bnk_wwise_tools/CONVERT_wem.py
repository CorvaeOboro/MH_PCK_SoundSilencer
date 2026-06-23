"""
WEM -> OGG conversion pipeline using bundled ww2ogg + revorb.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from .UTIL_logger import Logger, log
from .CONFIG_tools import ToolPaths

__all__ = ["WemConverter"]


class WemConverter:
    """Converts extracted WEM files to Ogg Vorbis."""

    def __init__(self, tool_paths: Optional[ToolPaths] = None, logger: Optional[Logger] = None):
        self.log = logger or log
        self.tp = tool_paths or ToolPaths.load()
        self._validate_tools()

    def _validate_tools(self) -> None:
        with self.log.step("Validate conversion tools"):
            missing = []
            for name, path in [
                ("ww2ogg", self.tp.ww2ogg),
                ("revorb", self.tp.revorb),
                ("codebooks", self.tp.codebooks),
            ]:
                if not Path(path).exists():
                    missing.append(f"{name}: {path}")
                else:
                    self.log.ok(f"Found {name}: {path}")
            if missing:
                raise RuntimeError("Missing tool(s):\n  " + "\n  ".join(missing))

    def convert_single(self, wem_path: Path, out_dir: Path) -> Optional[Path]:
        """Convert one .wem to .ogg.  Returns path to .ogg or None on failure."""
        with self.log.step(f"Convert {wem_path.name}"):
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)

            # ww2ogg
            self.log.info(f"ww2ogg {wem_path.name}")
            result = subprocess.run(
                [self.tp.ww2ogg, str(wem_path), "--pcb", self.tp.codebooks],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.log.error(f"ww2ogg failed: {result.stderr.strip()}")
                return None
            # ww2ogg writes next to the source; use a temp copy to avoid
            # colliding with the final output directory.
            ogg_intermediate = wem_path.with_suffix(".ogg")
            if not ogg_intermediate.exists():
                self.log.error("ww2ogg did not produce .ogg output")
                return None
            self.log.ok(f"ww2ogg produced {ogg_intermediate.name} ({ogg_intermediate.stat().st_size:,} bytes)")

            # revorb
            self.log.info(f"revorb {ogg_intermediate.name}")
            rev_result = subprocess.run(
                [self.tp.revorb, str(ogg_intermediate)],
                capture_output=True,
                text=True,
            )
            if rev_result.returncode != 0:
                self.log.warn(f"revorb exited {rev_result.returncode}: {rev_result.stderr.strip()}")

            final = out_dir / (wem_path.stem + ".ogg")
            if final.resolve() != ogg_intermediate.resolve():
                final.write_bytes(ogg_intermediate.read_bytes())
                self.log.ok(f"Final OGG: {final.name} ({final.stat().st_size:,} bytes)")
                ogg_intermediate.unlink(missing_ok=True)
            else:
                self.log.ok(f"Final OGG: {final.name} ({final.stat().st_size:,} bytes)")
            return final

    def convert_batch(self, wem_paths: List[Path], out_dir: Path) -> List[Path]:
        """Convert a list of .wem files.  Returns successfully converted paths."""
        with self.log.step("Batch WEM -> OGG conversion"):
            written: List[Path] = []
            for wem_path in wem_paths:
                result = self.convert_single(wem_path, out_dir)
                if result:
                    written.append(result)
            self.log.ok(f"Converted {len(written)} / {len(wem_paths)} WEM(s)")
            return written
