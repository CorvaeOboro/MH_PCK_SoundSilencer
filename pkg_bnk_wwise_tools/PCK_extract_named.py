"""
Named audio extraction pipeline.

Attempts to discover human-readable names for WEM audio assets by parsing
HIRC (audio object hierarchy) and STID (string table) sections from the
embedded BNK.  Falls back to WEM-ID-based names if metadata is unavailable.

Writes a JSON manifest for later repackaging reference.

Usage (via CLI):
    python -m pkg_bnk_wwise_tools extract-named SFX_AntMan_INT.pck -o ./named_out
"""

from __future__ import annotations

import json
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .UTIL_logger import Logger, log
from .PARSE_pck_bnk import PckBnkParser, WemEntry
from .CONVERT_wem import WemConverter
from .CONFIG_tools import ToolPaths

__all__ = [
    "HircSoundInfo",
    "StidEntry",
    "AudioAsset",
    "HircParser",
    "StidParser",
    "NameResolver",
    "NamedExtractor",
]


# ---------------------------------------------------------------------------
# HIRC / STID parsing data structures
# ---------------------------------------------------------------------------

@dataclass
class HircSoundInfo:
    """Parsed HIRC Sound object metadata."""
    object_id: int
    audio_id: int          # WEM ID in DIDX
    storage_type: int      # 0=Embedded, 1=Streamed, 2=StreamedPrefetched
    source_id: int         # external source ID
    sound_type: int        # 0=SFX, 1=Voice
    raw_length: int


@dataclass
class StidEntry:
    """Parsed STID name entry."""
    id: int
    name: str


@dataclass
class AudioAsset:
    """A single audio asset with all discovered metadata."""
    index: int
    wem_id: int
    offset: int
    length: int
    is_loose: bool
    file_name: str = ""
    display_name: str = ""
    sound_type: Optional[str] = None      # "SFX" or "Voice"
    storage_type: Optional[str] = None    # "Embedded", "Streamed"
    source_id: Optional[int] = None


# ---------------------------------------------------------------------------
# HIRC parser
# ---------------------------------------------------------------------------

class HircParser:
    """
    Parse HIRC (Hierarchy) section from a BNK.

    Based on bnk.bt (010 Editor template) from eXpl0it3r/bnkextr.
    """

    # Object type IDs from bnk.bt
    TYPE_SETTINGS = 1
    TYPE_SOUND = 2
    TYPE_EVENT_ACTION = 3
    TYPE_EVENT = 4
    TYPE_RANDOM_OR_SEQUENCE = 5
    TYPE_SWITCH_CONTAINER = 6
    TYPE_ACTOR_MIXER = 7
    TYPE_AUDIO_BUS = 8
    TYPE_BLEND_CONTAINER = 9
    TYPE_MUSIC_SEGMENT = 10
    TYPE_MUSIC_TRACK = 11
    TYPE_MUSIC_SWITCH = 12
    TYPE_MUSIC_PLAYLIST = 13
    TYPE_ATTENUATION = 14
    TYPE_DIALOGUE_EVENT = 15
    TYPE_MOTION_BUS = 16
    TYPE_MOTION_FX = 17
    TYPE_EFFECT = 18
    TYPE_UNKNOWN = 19
    TYPE_AUXILIARY_BUS = 20

    SOUND_TYPE_LABELS = {0: "SFX", 1: "Voice"}
    STORAGE_TYPE_LABELS = {0: "Embedded", 1: "Streamed", 2: "StreamedPrefetched"}

    def __init__(self, data: bytes, offset: int, bank_version: int, logger: Optional[Logger] = None):
        self.data = data
        self.offset = offset
        self.bank_version = bank_version
        self.log = logger or log
        self.sounds: Dict[int, HircSoundInfo] = {}   # audio_id -> info
        self.events: Dict[int, Tuple[int, ...]] = {}  # event_id -> action_ids
        self.event_actions: Dict[int, dict] = {}

    def parse(self) -> bool:
        """Parse HIRC section. Returns True if any Sound objects found."""
        with self.log.step("Parse HIRC section"):
            pos = self.offset
            if pos + 4 > len(self.data):
                self.log.warn("HIRC section truncated")
                return False

            count = struct.unpack_from("<I", self.data, pos)[0]
            pos += 4
            self.log.info(f"HIRC object count: {count}")

            found_sounds = 0
            for i in range(count):
                if pos + 9 > len(self.data):
                    self.log.warn(f"HIRC object {i} truncated")
                    break

                obj_type = self.data[pos]
                obj_length = struct.unpack_from("<I", self.data, pos + 1)[0]
                obj_id = struct.unpack_from("<I", self.data, pos + 5)[0]
                pos += 9

                if obj_type == self.TYPE_SOUND:
                    info = self._parse_sound(pos, obj_length, obj_id)
                    if info:
                        self.sounds[info.audio_id] = info
                        found_sounds += 1
                elif obj_type == self.TYPE_EVENT:
                    self._parse_event(pos, obj_length, obj_id)
                elif obj_type == self.TYPE_EVENT_ACTION:
                    self._parse_event_action(pos, obj_length, obj_id)

                pos += obj_length

            self.log.ok(f"Parsed {found_sounds} Sound object(s), {len(self.events)} Event(s)")
            return found_sounds > 0

    def _parse_sound(self, pos: int, length: int, obj_id: int) -> Optional[HircSoundInfo]:
        """Parse a HIRC Sound object (type 2)."""
        # Layout: unknown(4) + state(1) + audioId(4) + sourceId(4) + soundType(1) + rest
        if length < 14:
            return None
        unknown = struct.unpack_from("<I", self.data, pos)[0]
        state = self.data[pos + 4]
        audio_id = struct.unpack_from("<I", self.data, pos + 5)[0]
        source_id = struct.unpack_from("<I", self.data, pos + 9)[0]
        sound_type = self.data[pos + 13]

        return HircSoundInfo(
            object_id=obj_id,
            audio_id=audio_id,
            storage_type=state,
            source_id=source_id,
            sound_type=sound_type,
            raw_length=length,
        )

    def _parse_event(self, pos: int, length: int, obj_id: int) -> None:
        """Parse a HIRC Event object (type 4)."""
        # Version-aware action count
        if self.bank_version >= 134:
            if length < 1:
                return
            action_count = self.data[pos]
            action_pos = pos + 1
        else:
            if length < 4:
                return
            action_count = struct.unpack_from("<I", self.data, pos)[0]
            action_pos = pos + 4

        action_ids = []
        for _ in range(action_count):
            if action_pos + 4 > len(self.data):
                break
            action_ids.append(struct.unpack_from("<I", self.data, action_pos)[0])
            action_pos += 4

        self.events[obj_id] = tuple(action_ids)

    def _parse_event_action(self, pos: int, length: int, obj_id: int) -> None:
        """Parse a HIRC EventAction object (type 3)."""
        if length < 13:
            return
        scope = self.data[pos]
        action_type = self.data[pos + 1]
        game_object_id = struct.unpack_from("<I", self.data, pos + 2)[0]
        parameter_count = self.data[pos + 7]

        self.event_actions[obj_id] = {
            "scope": scope,
            "action_type": action_type,
            "game_object_id": game_object_id,
            "parameter_count": parameter_count,
        }


# ---------------------------------------------------------------------------
# STID parser
# ---------------------------------------------------------------------------

class StidParser:
    """
    Parse STID (Sound Type ID / string table) section.

    Tries two formats:
      - New: one(uint32), count(uint32), entries[{id, len_byte, name}]
      - Old: skip 12 bytes, read single length_byte + name
    """

    def __init__(self, data: bytes, offset: int, length: int, logger: Optional[Logger] = None):
        self.data = data
        self.offset = offset
        self.length = length
        self.log = logger or log
        self.entries: Dict[int, str] = {}   # id -> name
        self.bank_name: str = ""

    def parse(self) -> bool:
        with self.log.step("Parse STID section"):
            pos = self.offset
            end = pos + self.length
            if end > len(self.data):
                self.log.warn("STID section truncated")
                return False

            # Try new format first
            if self._try_new_format(pos, end):
                return True

            # Fall back to old format (single string)
            return self._try_old_format(pos, end)

    def _try_new_format(self, pos: int, end: int) -> bool:
        """Try new multi-entry format."""
        if pos + 8 > end:
            return False
        one = struct.unpack_from("<I", self.data, pos)[0]
        count = struct.unpack_from("<I", self.data, pos + 4)[0]

        if count > 10000 or count == 0:
            return False

        pos += 8
        entries_read = 0
        for i in range(count):
            if pos + 5 > end:
                break
            entry_id = struct.unpack_from("<I", self.data, pos)[0]
            name_len = self.data[pos + 4]
            if pos + 5 + name_len > end:
                break
            name = self.data[pos + 5 : pos + 5 + name_len].decode("utf-8", errors="replace")
            self.entries[entry_id] = name
            pos += 5 + name_len
            entries_read += 1

        if entries_read > 0:
            self.log.ok(f"STID new format: {entries_read} name(s)")
            return True
        return False

    def _try_old_format(self, pos: int, end: int) -> bool:
        """Try old single-string format (from bnkextr.dpr)."""
        # Skip 12 bytes (3 × uint32), then read 1-byte length + string
        if pos + 12 > end:
            return False
        pos += 12
        if pos + 1 > end:
            return False
        name_len = self.data[pos]
        if pos + 1 + name_len > end:
            return False
        name = self.data[pos + 1 : pos + 1 + name_len].decode("utf-8", errors="replace")
        self.bank_name = name
        self.log.ok(f"STID old format: bank name = '{name}'")
        return True


# ---------------------------------------------------------------------------
# Name resolver
# ---------------------------------------------------------------------------

class NameResolver:
    """
    Combine HIRC Sound info + STID names to produce human-readable
    filenames for extracted WEMs.
    """

    def __init__(
        self,
        hirc: HircParser,
        stid: StidParser,
        logger: Optional[Logger] = None,
    ):
        self.hirc = hirc
        self.stid = stid
        self.log = logger or log

    def resolve(self, wem_id: int) -> Tuple[str, dict]:
        """
        Return (display_name, metadata_dict) for a given WEM ID.
        """
        meta: dict = {}
        name = f"wem_{wem_id:08x}"

        sound = self.hirc.sounds.get(wem_id)
        if sound:
            meta["object_id"] = f"0x{sound.object_id:08x}"
            meta["sound_type"] = HircParser.SOUND_TYPE_LABELS.get(sound.sound_type, f"unknown_{sound.sound_type}")
            meta["storage_type"] = HircParser.STORAGE_TYPE_LABELS.get(sound.storage_type, f"unknown_{sound.storage_type}")
            meta["source_id"] = f"0x{sound.source_id:08x}"

            # Try STID name lookup by object_id
            stid_name = self.stid.entries.get(sound.object_id)
            if stid_name:
                name = self._sanitize(stid_name)
            else:
                # Fallback: use sound type + audio ID
                type_label = HircParser.SOUND_TYPE_LABELS.get(sound.sound_type, "unknown")
                name = f"{type_label.lower()}_{wem_id:08x}"
        else:
            # No HIRC info - try STID by WEM id directly
            stid_name = self.stid.entries.get(wem_id)
            if stid_name:
                name = self._sanitize(stid_name)
                meta["from_stid"] = True

        return name, meta

    @staticmethod
    def _sanitize(name: str) -> str:
        """Sanitize a name for use as a filename."""
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return safe.strip("_") or "unnamed"


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------

class NamedExtractor:
    """
    Extract audio from a .pck with human-readable names.

    Pipeline:
      1. Parse .pck -> BNK + loose WEMs
      2. Parse HIRC + STID from embedded BNK
      3. Build name map (WEM ID -> display name)
      4. Extract all WEMs
      5. Convert to OGG
      6. Write manifest.json
    """

    def __init__(self, tool_paths: Optional[ToolPaths] = None, logger: Optional[Logger] = None):
        self.log = logger or log
        self.tp = tool_paths or ToolPaths.load()
        self.assets: List[AudioAsset] = []
        self.manifest: dict = {}
        self._parser: Optional[PckBnkParser] = None

    def run(self, pck_path: Path | str, out_dir: Path | str, to_ogg: bool = True) -> Path:
        pck_path = Path(pck_path)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        with self.log.step("Named extraction pipeline"):
            # 1. Parse .pck
            self.log.info(f"Input: {pck_path}")
            self._parser = PckBnkParser.from_file(pck_path, logger=self.log)
            self._parser.parse()
            self.log.ok(f"Parsed {len(self._parser.wems)} WEM(s)")

            # 2. Parse HIRC + STID from embedded BNK
            hirc = self._parse_hirc(self._parser)
            stid = self._parse_stid(self._parser)
            resolver = NameResolver(hirc, stid, logger=self.log)

            # 3. Build asset list with resolved names
            self.assets = self._build_assets(self._parser, resolver)
            self.log.ok(f"Resolved {len(self.assets)} asset name(s)")

            # 4. Extract WEMs
            wem_paths = self._extract_wems(out_dir)

            # 5. Convert to OGG
            if to_ogg:
                self._convert_to_ogg(wem_paths, out_dir)

            # 6. Write manifest
            self._write_manifest(out_dir, pck_path, stid)

            return out_dir / "manifest.json"

    def _parse_hirc(self, parser: PckBnkParser) -> HircParser:
        """Find and parse HIRC section from the embedded BNK."""
        hirc = HircParser(parser.data, 0, 0, logger=self.log)

        for sec in parser.sections:
            if sec.identifier == "HIRC":
                hirc = HircParser(
                    parser.data,
                    sec.offset + 8,  # skip section header
                    parser.bnk_header.version if parser.bnk_header else 0,
                    logger=self.log,
                )
                hirc.parse()
                return hirc

        self.log.warn("No HIRC section found in BNK")
        return hirc

    def _parse_stid(self, parser: PckBnkParser) -> StidParser:
        """Find and parse STID section from the embedded BNK."""
        stid = StidParser(parser.data, 0, 0, logger=self.log)

        for sec in parser.sections:
            if sec.identifier == "STID":
                stid = StidParser(
                    parser.data,
                    sec.offset + 8,
                    sec.length,
                    logger=self.log,
                )
                stid.parse()
                return stid

        self.log.warn("No STID section found in BNK")
        return stid

    def _build_assets(self, parser: PckBnkParser, resolver: NameResolver) -> List[AudioAsset]:
        """Create AudioAsset entries for all WEMs with resolved names."""
        assets: List[AudioAsset] = []
        used_names: Dict[str, int] = {}  # name -> count for dedup

        for wem in parser.wems:
            if wem.is_loose:
                # Loose WEMs have no WemId from DIDX; use synthetic offset-based ID
                wem_id = wem.descriptor.wem_id
                display_name = f"loose_0x{wem.descriptor.offset:08x}"
                meta = {"loose": True}
            else:
                wem_id = wem.descriptor.wem_id
                display_name, meta = resolver.resolve(wem_id)

            # Deduplicate names
            base_name = display_name
            count = used_names.get(base_name, 0)
            used_names[base_name] = count + 1
            if count > 0:
                display_name = f"{base_name}_{count}"

            assets.append(AudioAsset(
                index=wem.index,
                wem_id=wem_id,
                offset=wem.descriptor.offset,
                length=wem.descriptor.length,
                is_loose=wem.is_loose,
                file_name=display_name,
                display_name=display_name,
                sound_type=meta.get("sound_type"),
                storage_type=meta.get("storage_type"),
                source_id=meta.get("source_id"),
            ))

        return assets

    def _extract_wems(self, out_dir: Path) -> List[Path]:
        """Write all WEM files to disk."""
        wem_paths: List[Path] = []
        with self.log.step("Extract WEM files"):
            for asset in self.assets:
                path = out_dir / f"{asset.file_name}.wem"
                # Get data from parser via index
                # We need the original parser data; store reference
                path.write_bytes(self._get_wem_data(asset.index))
                wem_paths.append(path)
                self.log.debug(f"  {asset.file_name}.wem ({asset.length:,} bytes)")
            self.log.ok(f"Wrote {len(wem_paths)} .wem file(s)")
        return wem_paths

    def _get_wem_data(self, index: int) -> bytes:
        """Retrieve WEM data from the stored parser."""
        if self._parser is None or not (0 <= index < len(self._parser.wems)):
            return b""
        return self._parser.wems[index].data

    def _convert_to_ogg(self, wem_paths: List[Path], out_dir: Path) -> None:
        """Convert extracted WEMs to OGG."""
        with self.log.step("Convert to OGG"):
            converter = WemConverter(tool_paths=self.tp, logger=self.log)
            converted = 0
            for wem_path in wem_paths:
                result = converter.convert_single(wem_path, out_dir)
                if result:
                    converted += 1
            self.log.ok(f"Converted {converted} / {len(wem_paths)} to OGG")

    def _write_manifest(self, out_dir: Path, pck_path: Path, stid: StidParser) -> None:
        """Build and write the JSON manifest."""
        self.manifest = {
            "source_file": str(pck_path),
            "asset_count": len(self.assets),
            "bank_name": stid.bank_name,
            "stid_entries": stid.entries,
            "assets": [
                {
                    "index": a.index,
                    "wem_id": f"0x{a.wem_id:08x}",
                    "name": a.display_name,
                    "file_name": a.file_name,
                    "offset": a.offset,
                    "length": a.length,
                    "is_loose": a.is_loose,
                    "sound_type": a.sound_type,
                    "storage_type": a.storage_type,
                    "source_id": a.source_id,
                }
                for a in self.assets
            ],
        }
        path = out_dir / "manifest.json"
        path.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        self.log.ok(f"Wrote manifest: {path}")
