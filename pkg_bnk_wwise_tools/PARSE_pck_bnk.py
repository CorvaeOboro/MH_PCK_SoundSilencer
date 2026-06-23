"""
Parser for Marvel Heroes .pck -> embedded BNK -> DIDX -> WEM.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional

from .UTIL_logger import Logger, log

__all__ = [
    "RawField",
    "PckHeader",
    "BnkSectionHeader",
    "BnkBankHeader",
    "WemDescriptor",
    "WemEntry",
    "ParseError",
    "PckBnkParser",
]

class ParseError(Exception):
    pass

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RawField:
    name: str
    offset: int
    size: int
    raw: bytes
    value: object

    def __str__(self) -> str:
        if self.size <= 16:
            return f"{self.name:<30} @{self.offset:06x}  {self.raw.hex():<34}  = {self.value!r}"
        return f"{self.name:<30} @{self.offset:06x}  ({self.size} bytes)  = {self.value!r}"


@dataclass
class PckHeader:
    magic: str
    length: int
    unknown: bytes
    wem_count: int
    raw_fields: List[RawField] = field(default_factory=list, repr=False)


@dataclass
class BnkSectionHeader:
    identifier: str
    length: int
    offset: int
    raw_fields: List[RawField] = field(default_factory=list, repr=False)

    def to_bytes(self) -> bytes:
        return self.identifier.encode("ascii") + struct.pack("<I", self.length)


@dataclass
class BnkBankHeader:
    version: int
    bank_id: int
    remaining: bytes
    raw_fields: List[RawField] = field(default_factory=list, repr=False)


@dataclass
class WemDescriptor:
    wem_id: int
    offset: int
    length: int
    raw_fields: List[RawField] = field(default_factory=list, repr=False)

    def to_bytes(self) -> bytes:
        return struct.pack("<III", self.wem_id, self.offset, self.length)


@dataclass
class WemEntry:
    index: int
    descriptor: WemDescriptor
    data: bytes
    padding: bytes = b""
    is_loose: bool = False


# ---------------------------------------------------------------------------
# Byte reader with logging
# ---------------------------------------------------------------------------

class _Reader:
    """Cursor over a byte buffer that logs every read."""

    def __init__(self, data: bytes, name: str = "file", logger: Optional[Logger] = None):
        self.data = data
        self.name = name
        self.pos = 0
        self.log = logger or log
        self.fields: List[RawField] = []

    def _need(self, n: int) -> None:
        if self.pos + n > len(self.data):
            raise ParseError(
                f"{self.name}: read past EOF at 0x{self.pos:x} "
                f"(need {n}, have {len(self.data) - self.pos})"
            )

    def seek(self, offset: int) -> None:
        if offset < 0 or offset > len(self.data):
            raise ParseError(f"{self.name}: seek out of range 0x{offset:x}")
        self.pos = offset

    def tell(self) -> int:
        return self.pos

    def _read_raw(self, n: int) -> bytes:
        self._need(n)
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def read_uint32(self, label: str = "uint32") -> int:
        raw = self._read_raw(4)
        val = struct.unpack("<I", raw)[0]
        self.fields.append(RawField(label, self.pos - 4, 4, raw, val))
        self.log.field(label, val, "0x{:08X}")
        return val

    def read_uint16(self, label: str = "uint16") -> int:
        raw = self._read_raw(2)
        val = struct.unpack("<H", raw)[0]
        self.fields.append(RawField(label, self.pos - 2, 2, raw, val))
        self.log.field(label, val, "0x{:04X}")
        return val

    def read_int32(self, label: str = "int32") -> int:
        raw = self._read_raw(4)
        val = struct.unpack("<i", raw)[0]
        self.fields.append(RawField(label, self.pos - 4, 4, raw, val))
        self.log.field(label, val)
        return val

    def read_bytes(self, n: int, label: str = "bytes") -> bytes:
        raw = self._read_raw(n)
        self.fields.append(RawField(label, self.pos - n, n, raw, f"({n} bytes)"))
        if n <= 8:
            self.log.field(label, raw.hex())
        else:
            self.log.field(label, f"{n} bytes")
        return raw

    def read_string(self, n: int, label: str = "string") -> str:
        raw = self._read_raw(n)
        text = raw.decode("ascii", errors="replace").rstrip("\x00")
        self.fields.append(RawField(label, self.pos - n, n, raw, text))
        self.log.field(label, text)
        return text

    def read_wstring(self, n: int, label: str = "wstring") -> str:
        raw = self._read_raw(n)
        text = raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        self.fields.append(RawField(label, self.pos - n, n, raw, text))
        self.log.field(label, text)
        return text

    def slice(self, offset: int, length: int) -> bytes:
        if offset + length > len(self.data):
            raise ParseError(f"slice out of range: 0x{offset:x} + {length}")
        return self.data[offset:offset + length]


# ---------------------------------------------------------------------------
# PCK -> BNK parser
# ---------------------------------------------------------------------------

class PckBnkParser:
    """
    Parse Marvel Heroes .pck (AKPK outer header + embedded BNK).

    Usage:
        parser = PckBnkParser.from_file(path)
        parser.parse()
        for wem in parser.wems:
            print(wem.descriptor.wem_id, wem.descriptor.length)
    """

    def __init__(self, data: bytes, path: Optional[Path] = None, logger: Optional[Logger] = None):
        self.data = data
        self.path = path
        self.log = logger or log
        self.pck_header: Optional[PckHeader] = None
        self.bnk_offset: int = 0
        self.bnk_header: Optional[BnkBankHeader] = None
        self.sections: List[BnkSectionHeader] = []
        self.didx_entries: List[WemDescriptor] = []
        self.wems: List[WemEntry] = []

    @classmethod
    def from_file(cls, path: Path | str, **kwargs) -> "PckBnkParser":
        path = Path(path)
        return cls(path.read_bytes(), path=path, **kwargs)

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def parse(self) -> None:
        with self.log.step("Parse .pck file"):
            self._parse_pck_header()
            self._find_bnk()
            self._parse_bnk_sections()
            self._scan_loose_wems()

    def _scan_loose_wems(self) -> None:
        """
        Marvel Heroes .pck stores most WEMs as loose RIFF files
        after the embedded BNK.  Signature-scan for them.
        """
        with self.log.step("Scan for loose WEMs"):
            # Determine end of BNK DATA section
            data_sec_end = 0
            for sec in self.sections:
                if sec.identifier == "DATA":
                    data_sec_end = sec.offset + 8 + sec.length
                    break

            if data_sec_end == 0:
                self.log.warn("No DATA section found - scanning from header end")
                data_sec_end = self.pck_header.length if self.pck_header else 0

            self.log.info(f"Scanning from 0x{data_sec_end:x} to end of file")

            pos = data_sec_end
            found = 0
            while pos + 12 <= len(self.data):
                if self.data[pos:pos + 4] == b"RIFF" and self.data[pos + 8:pos + 12] == b"WAVE":
                    riff_len = struct.unpack_from("<I", self.data, pos + 4)[0]
                    total = 8 + riff_len
                    # Use the offset as synthetic WemId
                    synthetic_id = pos & 0xFFFFFFFF
                    desc = WemDescriptor(
                        wem_id=synthetic_id,
                        offset=pos,  # absolute offset in .pck
                        length=total,
                    )
                    wem_data = self.data[pos:pos + total]
                    self.wems.append(
                        WemEntry(
                            index=len(self.wems),
                            descriptor=desc,
                            data=wem_data,
                            padding=b"",
                            is_loose=True,
                        )
                    )
                    self.log.debug(
                        f"  Loose WEM[{len(self.wems)-1}]  abs=0x{pos:x}  len={total:,}"
                    )
                    found += 1
                    pos += total
                else:
                    pos += 1

            self.log.ok(f"Found {found} loose WEM(s)  (total={len(self.wems)})")

    def _parse_pck_header(self) -> None:
        with self.log.step("Read AKPK outer header"):
            r = _Reader(self.data, name="pck", logger=self.log)
            magic = r.read_string(4, "magic")
            if magic != "AKPK":
                raise ParseError(f"Bad magic: {magic!r} (expected 'AKPK')")
            header_len = r.read_uint32("header_length")
            self.log.info(f"AKPK header length = {header_len} (0x{header_len:x})")

            # The remaining header bytes are still unclear; dump them for analysis
            remaining = header_len - 8  # already read 8 bytes (magic + length)
            if remaining > 0:
                r.read_bytes(min(remaining, 44), "header_unknown")
            # Read the rest as raw for hex inspection
            if r.tell() < header_len:
                leftover = self.data[r.tell():header_len]
                self.log.hex_dump(leftover, offset=r.tell(), length=min(len(leftover), 128), title="Remaining .pck header")
                r.seek(header_len)

            self.pck_header = PckHeader(
                magic=magic,
                length=header_len,
                unknown=b"",
                wem_count=0,
                raw_fields=r.fields,
            )

    def _find_bnk(self) -> None:
        with self.log.step("Locate embedded BNK"):
            # Search for BKHD signature within first 1KB after header
            search_start = self.pck_header.length
            window = self.data[search_start:search_start + 1024]
            bnk_rel = window.find(b"BKHD")
            if bnk_rel == -1:
                # Fallback: search entire file
                bnk_abs = self.data.find(b"BKHD")
                if bnk_abs == -1:
                    raise ParseError("No BKHD section found")
                self.bnk_offset = bnk_abs
            else:
                self.bnk_offset = search_start + bnk_rel

            self.log.ok(f"BKHD found at offset 0x{self.bnk_offset:x}")
            self.log.hex_dump(self.data, offset=self.bnk_offset, length=64, title="BNK start")

    def _parse_bnk_sections(self) -> None:
        with self.log.step("Parse BNK sections"):
            pos = self.bnk_offset
            while pos + 8 <= len(self.data):
                ident_bytes = self.data[pos:pos + 4]
                ident = ident_bytes.decode("ascii", errors="replace")
                ident_safe = ident.replace("\ufffd", "?")
                length = struct.unpack_from("<I", self.data, pos + 4)[0]
                sec_hdr = BnkSectionHeader(identifier=ident, length=length, offset=pos)
                self.sections.append(sec_hdr)
                self.log.info(f"Section @{pos:06x}: {ident_safe}  length={length:,}")

                if ident == "BKHD":
                    self._parse_bkhd(pos, length)
                elif ident == "DIDX":
                    self._parse_didx(pos, length)
                elif ident == "DATA":
                    self._parse_data(pos, length)
                    break  # DATA is last section we care about
                elif ident in ("HIRC", "STID", "INIT", "PLAT"):
                    pass  # skip for now
                else:
                    self.log.warn(f"Unknown section: {ident}")

                pos += 8 + length
                # Align to 4 if needed
                if pos % 4 != 0:
                    pos += 4 - (pos % 4)

    def _parse_bkhd(self, offset: int, length: int) -> None:
        with self.log.step("Parse BKHD (Bank Header)"):
            r = _Reader(self.data, name="bkhd", logger=self.log)
            r.seek(offset + 8)  # skip section header already read
            version = r.read_uint32("bank_version")
            bank_id = r.read_uint32("bank_id")
            remaining_len = length - 8
            if remaining_len > 0:
                remaining = r.read_bytes(remaining_len, "bkhd_remaining")
            else:
                remaining = b""
            self.bnk_header = BnkBankHeader(version, bank_id, remaining, raw_fields=r.fields)
            self.log.ok(f"Bank version={version}  bank_id=0x{bank_id:08x}")

    def _parse_didx(self, offset: int, length: int) -> None:
        with self.log.step("Parse DIDX (Data Index)"):
            entry_count = length // 12
            self.log.info(f"DIDX entries: {entry_count}")
            pos = offset + 8
            entries: List[WemDescriptor] = []
            for i in range(entry_count):
                wem_id = struct.unpack_from("<I", self.data, pos)[0]
                wem_offset = struct.unpack_from("<I", self.data, pos + 4)[0]
                wem_length = struct.unpack_from("<I", self.data, pos + 8)[0]
                self.log.debug(
                    f"  DIDX[{i}]  wem_id=0x{wem_id:08x}  offset=0x{wem_offset:x}  length={wem_length:,}"
                )
                entries.append(WemDescriptor(wem_id, wem_offset, wem_length))
                pos += 12
            self.didx_entries = entries

    def _parse_data(self, offset: int, length: int) -> None:
        with self.log.step("Parse DATA section"):
            data_start = offset + 8
            self.log.info(f"DATA starts at 0x{data_start:x}, declared length={length:,}")
            for i, desc in enumerate(self.didx_entries):
                abs_off = data_start + desc.offset
                if abs_off + desc.length > len(self.data):
                    raise ParseError(
                        f"WEM {i} out of bounds: offset=0x{abs_off:x} length={desc.length}"
                    )
                wem_data = self.data[abs_off:abs_off + desc.length]

                # Compute padding to next WEM or end of DATA
                if i + 1 < len(self.didx_entries):
                    next_off = data_start + self.didx_entries[i + 1].offset
                else:
                    next_off = data_start + length
                pad_len = next_off - (abs_off + desc.length)
                if pad_len < 0:
                    pad_len = 0
                padding = self.data[abs_off + desc.length : abs_off + desc.length + pad_len]

                self.wems.append(WemEntry(index=i, descriptor=desc, data=wem_data, padding=padding, is_loose=False))
                self.log.debug(
                    f"  WEM[{i}]  id=0x{desc.wem_id:08x}  abs=0x{abs_off:x}  "
                    f"len={desc.length:,}  pad={pad_len}"
                )

            self.log.ok(f"Loaded {len(self.wems)} WEM(s)")

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def wem_by_index(self, index: int) -> WemEntry:
        if not (0 <= index < len(self.wems)):
            raise ParseError(f"WEM index {index} out of range (0-{len(self.wems)-1})")
        return self.wems[index]

    def wem_by_id(self, wem_id: int) -> Optional[WemEntry]:
        for w in self.wems:
            if w.descriptor.wem_id == wem_id:
                return w
        return None

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "path": str(self.path) if self.path else None,
            "file_size": len(self.data),
            "bnk_offset": self.bnk_offset,
            "bank_version": self.bnk_header.version if self.bnk_header else None,
            "bank_id": f"0x{self.bnk_header.bank_id:08x}" if self.bnk_header else None,
            "wem_count": len(self.wems),
            "wems": [
                {
                    "index": w.index,
                    "wem_id": f"0x{w.descriptor.wem_id:08x}",
                    "offset": w.descriptor.offset,
                    "length": w.descriptor.length,
                }
                for w in self.wems
            ],
        }
