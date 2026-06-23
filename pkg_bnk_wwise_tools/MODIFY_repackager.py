"""
BNK / .pck modification toolkit.

Supports:
  - Replacing a WEM with new data (updates DIDX offsets automatically)
  - Silencing a WEM (injects a 0-byte PCM RIFF/WAVE)
  - Writing modified BNK back into the original .pck envelope

"""

from __future__ import annotations

import hashlib
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .PARSE_pck_bnk import BnkSectionHeader, ParseError, PckBnkParser, WemDescriptor
from .UTIL_logger import Logger, log

__all__ = [
    "make_silent_wem",
    "zero_fill_riff",
    "Repackager",
    "verify_round_trip",
]


# ---------------------------------------------------------------------------
# Silent WEM factory
# ---------------------------------------------------------------------------

_FMT_PCM = bytes([
    0x66, 0x6D, 0x74, 0x20, 0x10, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x01, 0x00, 0x44, 0xAC, 0x00, 0x00,
    0x88, 0x58, 0x01, 0x00, 0x02, 0x00, 0x10, 0x00,
])


def make_silent_wem() -> bytes:
    """Build a valid but silent RIFF/WAVE (0 samples)."""
    data_chunk = b"data" + struct.pack("<I", 0)
    body = _FMT_PCM + data_chunk
    return b"RIFF" + struct.pack("<I", len(body)) + b"WAVE" + body


def zero_fill_riff(data: bytes) -> bytes:
    """
    Zero out the audio data inside a RIFF/WAVE file while preserving
    the exact file size and all headers.  Finds the 'data' chunk
    and overwrites its payload with 0x00 bytes.
    """
    if len(data) < 36 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return b"\x00" * len(data)

    result = bytearray(data)
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        if chunk_id == b"data":
            start = pos + 8
            end = start + chunk_size
            if end <= len(data):
                result[start:end] = b"\x00" * (end - start)
            break
        pos += 8 + chunk_size
        if pos % 2 != 0:
            pos += 1
    return bytes(result)


# ---------------------------------------------------------------------------
# Repackager
# ---------------------------------------------------------------------------

class Repackager:
    """Modify WEMs inside a parsed BNK and write back to a new .pck."""

    def __init__(self, parser: PckBnkParser, logger: Optional[Logger] = None):
        self.parser = parser
        self.log = logger or log
        self._mutations: List[str] = []
        self._bnk_modified = False
        self._loose_modified = False

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def replace_wem(self, index: int, new_data: bytes, reason: str = "") -> None:
        """
        Replace a WEM's payload.  For BNK-embedded WEMs, shifts subsequent
        DIDX offsets.  For loose WEMs, preserves exact size to avoid
        corrupting the .pck envelope.
        """
        wems = self.parser.wems
        if not (0 <= index < len(wems)):
            raise ParseError(f"WEM index {index} out of range (0-{len(wems)-1})")

        wem = wems[index]
        old_len = len(wem.data)
        new_len = len(new_data)
        delta = new_len - old_len

        # Determine if this is a BNK-embedded WEM or a loose WEM
        is_loose = wem.is_loose
        if is_loose and delta != 0:
            raise ParseError(
                f"Cannot change size of loose WEM[{index}] - "
                f"would corrupt .pck envelope. Use zero-fill silence instead."
            )

        with self.log.step(f"Replace WEM[{index}] {reason}"):
            self.log.info(f"Before: length={old_len:,}  offset=0x{wem.descriptor.offset:x}")
            wem.data = bytes(new_data)
            wem.descriptor.length = new_len
            self.log.info(f"After:  length={new_len:,}  delta={delta:+,}")

            if not is_loose:
                if delta != 0:
                    self._bnk_modified = True
                    shifted = 0
                    for j in range(index + 1, len(wems)):
                        if not wems[j].is_loose:
                            wems[j].descriptor.offset += delta
                            shifted += 1
                    self.log.info(f"Shifted offsets for {shifted} subsequent BNK WEM(s) by {delta:+,}")
            else:
                self._loose_modified = True

            self._mutations.append(
                f"WEM[{index}] id=0x{wem.descriptor.wem_id:08x}  {old_len}->{new_len}  ({reason})"
            )

    def silence_wem(self, index: int) -> None:
        """
        Replace a WEM with silence.
        For BNK WEMs: injects a same-size zero-filled WEM.
        For loose WEMs: overwrites audio data bytes with zeros in-place.
        """
        wem = self.parser.wems[index]
        is_loose = wem.is_loose

        if is_loose:
            # For loose WEMs: zero-fill the existing data while preserving size
            with self.log.step(f"Silence loose WEM[{index}] (zero-fill)"):
                old_data = wem.data
                # Find the 'data' chunk in the RIFF and zero it
                new_data = zero_fill_riff(old_data)
                wem.data = new_data
                self._loose_modified = True
                self.log.info(f"Zero-filled {len(new_data):,} bytes (size preserved)")
                self._mutations.append(f"WEM[{index}] zero-filled  ({len(new_data)} bytes)")
        else:
            # For BNK WEMs: replace with a same-size silent WEM
            with self.log.step(f"Silence BNK WEM[{index}]"):
                old_data = wem.data
                silent = make_silent_wem()
                if len(silent) != len(old_data):
                    # Pad or truncate to match original size
                    if len(silent) < len(old_data):
                        silent = silent + b"\x00" * (len(old_data) - len(silent))
                    else:
                        silent = silent[:len(old_data)]
                self.replace_wem(index, silent, reason="silence")

    # ------------------------------------------------------------------
    # Write back
    # ------------------------------------------------------------------

    def write_pck(self, out_path: Path) -> Path:
        """
        Write modified .pck back to disk.

        Strategy:
          1. If only size-preserving changes were made (silence / zero-fill),
             patch WEMs in-place without rebuilding structures.
          2. If BNK WEM sizes changed, rebuild BNK and adjust offsets.
        """
        with self.log.step("Repack modified .pck"):
            out = bytearray(self.parser.data)

            # Check if any BNK WEM actually changed size
            bnk_size_changed = False
            for wem in self.parser.wems:
                if wem.descriptor.offset < self.parser.bnk_offset:
                    orig_len = len(self.parser.data)
                    # We can't easily check original size without re-parsing;
                    # rely on the _bnk_modified flag set during replace_wem
                    pass

            # For size-preserving silence: just patch all WEMs in-place
            if not self._bnk_modified:
                self.log.info("Size-preserving mode: patching WEMs in-place")
                patched = 0
                for wem in self.parser.wems:
                    abs_off = wem.descriptor.offset
                    # For BNK WEMs, offset is relative to DATA start.
                    # Convert to absolute offset in .pck.
                    if not wem.is_loose:
                        # Find DATA section start
                        data_start = next(
                            (s.offset + 8 for s in self.parser.sections if s.identifier == "DATA"),
                            0
                        )
                        abs_off = data_start + wem.descriptor.offset

                    if abs_off + len(wem.data) <= len(out):
                        out[abs_off:abs_off + len(wem.data)] = wem.data
                        patched += 1
                    else:
                        self.log.error(
                            f"WEM[{wem.index}] abs=0x{abs_off:x} out of range"
                        )
                self.log.ok(f"Patched {patched} WEM(s) in-place (no structural rebuild)")
            else:
                # BNK size changed: rebuild BNK region
                new_bnk = self._build_bnk()
                bnk_start = self.parser.bnk_offset
                old_bnk_end = bnk_start + self._original_bnk_length()
                out[bnk_start:old_bnk_end] = new_bnk
                delta_bnk = len(new_bnk) - (old_bnk_end - bnk_start)
                if delta_bnk != 0:
                    self.log.warn(
                        f"BNK size changed by {delta_bnk:+,} - "
                        f"loose WEM offsets shifted, file may be corrupt"
                    )
                else:
                    self.log.ok("BNK rebuilt with identical size")

                # Patch loose WEMs at their (possibly shifted) offsets
                patched = 0
                for wem in self.parser.wems:
                    if wem.is_loose:
                        abs_off = wem.descriptor.offset + delta_bnk
                        if abs_off + len(wem.data) <= len(out):
                            out[abs_off:abs_off + len(wem.data)] = wem.data
                            patched += 1
                if patched:
                    self.log.info(f"Patched {patched} loose WEM(s) after BNK resize")

            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(bytes(out))

            orig_size = len(self.parser.data)
            new_size = len(out)
            delta = new_size - orig_size
            if delta == 0:
                self.log.ok(f"Wrote {out_path.name}  size unchanged ({orig_size:,} bytes)")
            else:
                self.log.ok(
                    f"Wrote {out_path.name}  {orig_size:,} -> {new_size:,} bytes  "
                    f"delta={delta:+,}"
                )
            return out_path

    def write_bnk(self, out_path: Path) -> Path:
        """Write just the modified BNK (useful for testing)."""
        with self.log.step("Write BNK standalone"):
            data = self._build_bnk()
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            self.log.ok(f"Wrote {out_path.name} ({len(data):,} bytes)")
            return out_path

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _original_bnk_length(self) -> int:
        """Compute total length of the original BNK region."""
        # Find end of last known section
        end = self.parser.bnk_offset
        for sec in self.parser.sections:
            end = max(end, sec.offset + 8 + sec.length)
        return end - self.parser.bnk_offset

    def _build_bnk(self) -> bytes:
        """Serialize BNK with updated DIDX and DATA."""
        with self.log.step("Serialize BNK"):
            parts: List[bytes] = []

            # BKHD (unchanged)
            bh = self.parser.bnk_header
            if bh:
                parts.append(b"BKHD")
                bkhd_len = 8 + len(bh.remaining)
                parts.append(struct.pack("<I", bkhd_len))
                parts.append(struct.pack("<II", bh.version, bh.bank_id))
                parts.append(bh.remaining)
                self.log.debug(f"BKHD length={bkhd_len}")

            # DIDX (recalculated)
            didx_body = b"".join(d.to_bytes() for d in self.parser.didx_entries)
            didx_len = len(didx_body)
            parts.append(b"DIDX")
            parts.append(struct.pack("<I", didx_len))
            parts.append(didx_body)
            self.log.debug(f"DIDX entries={len(self.parser.didx_entries)}  length={didx_len}")

            # HIRC / other sections that came between DIDX and DATA (preserve)
            data_offset = None
            for sec in self.parser.sections:
                if sec.identifier == "DATA":
                    data_offset = sec.offset
                    break
                elif sec.identifier in ("HIRC", "STID", "INIT", "PLAT"):
                    # Copy verbatim from original data
                    raw = self.parser.data[sec.offset : sec.offset + 8 + sec.length]
                    parts.append(raw)
                    self.log.debug(f"Preserved {sec.identifier} section ({len(raw)} bytes)")

            # DATA (rebuilt) - only BNK-embedded WEMs (not loose)
            bnk_wems = [w for w in self.parser.wems if not w.is_loose]
            data_body = b"".join(w.data + w.padding for w in bnk_wems)
            parts.append(b"DATA")
            parts.append(struct.pack("<I", len(data_body)))
            parts.append(data_body)
            self.log.debug(f"DATA length={len(data_body)}  (from {len(bnk_wems)} BNK WEMs)")

            # Any trailing sections after DATA (preserve)
            if data_offset:
                data_end = data_offset + 8 + next(
                    (s.length for s in self.parser.sections if s.identifier == "DATA"), 0
                )
                for sec in self.parser.sections:
                    if sec.offset >= data_end:
                        raw = self.parser.data[sec.offset : sec.offset + 8 + sec.length]
                        parts.append(raw)
                        self.log.debug(f"Preserved trailing {sec.identifier} ({len(raw)} bytes)")

            bnk = b"".join(parts)
            self.log.info(f"Serialized BNK size: {len(bnk):,} bytes")
            return bnk

    def summary(self) -> str:
        return "\n".join(self._mutations)


# ---------------------------------------------------------------------------
# Round-trip verification 
# ---------------------------------------------------------------------------

def verify_round_trip(
    original_path: Path,
    modified_path: Path,
    expected_mutations: Optional[List[int]] = None,
    logger: Optional[Logger] = None,
) -> bool:
    """Parse both files, compare structures. Returns True if verification passes."""
    log = logger or log
    errors: List[str] = []
    warnings: List[str] = []

    with log.step("Round-trip verification"):
        orig = PckBnkParser.from_file(original_path, logger=log)
        orig.parse()
        mod = PckBnkParser.from_file(modified_path, logger=log)
        mod.parse()

        ok = True

        # 1. Compare counts
        if len(orig.wems) != len(mod.wems):
            _vr_err(errors, log, f"WEM count mismatch: {len(orig.wems)} vs {len(mod.wems)}")
            ok = False

        # 2. Per-WEM comparison
        for i in range(min(len(orig.wems), len(mod.wems))):
            o = orig.wems[i]
            m = mod.wems[i]
            changed = _compare_wem(i, o, m, expected_mutations, log)
            if changed and expected_mutations and i not in expected_mutations:
                _vr_warn(warnings, log, f"WEM[{i}] changed unexpectedly")
            elif not changed and expected_mutations and i in expected_mutations:
                _vr_err(errors, log, f"WEM[{i}] expected to change but did not")
                ok = False

        # 3. BNK structure
        if orig.bnk_header and mod.bnk_header:
            if orig.bnk_header.version != mod.bnk_header.version:
                _vr_warn(warnings, log, f"Bank version changed: {orig.bnk_header.version} -> {mod.bnk_header.version}")
            if orig.bnk_header.bank_id != mod.bnk_header.bank_id:
                _vr_err(errors, log, f"Bank ID mismatch")
                ok = False

        # 4. DIDX consistency
        if len(orig.didx_entries) == len(mod.didx_entries):
            for i, (od, md) in enumerate(zip(orig.didx_entries, mod.didx_entries)):
                if od.wem_id != md.wem_id:
                    _vr_err(errors, log, f"DIDX[{i}] WemId mismatch 0x{od.wem_id:08x} vs 0x{md.wem_id:08x}")
                    ok = False
                if od.length != md.length:
                    log.info(f"DIDX[{i}] length changed: {od.length:,} -> {md.length:,}")
                if od.offset != md.offset:
                    log.info(f"DIDX[{i}] offset changed: 0x{od.offset:x} -> 0x{md.offset:x}")

        if ok:
            log.ok("Round-trip verification PASSED")
        else:
            log.error("Round-trip verification FAILED")
            for e in errors:
                log.error(f"  * {e}")
        for w in warnings:
            log.warn(f"  * {w}")
        return ok


def _compare_wem(index: int, orig, mod, expected_mutations, log) -> bool:
    """Compare two WEM entries.  Returns True if any field changed."""
    o_hash = hashlib.sha256(orig.data).hexdigest()[:16]
    m_hash = hashlib.sha256(mod.data).hexdigest()[:16]
    changed = False

    if orig.descriptor.wem_id != mod.descriptor.wem_id:
        _vr_err([], log, f"WEM[{index}] WemId mismatch")
        changed = True
    if orig.descriptor.length != mod.descriptor.length:
        log.info(f"WEM[{index}] length: {orig.descriptor.length:,} -> {mod.descriptor.length:,}")
        changed = True
    if o_hash != m_hash:
        log.info(f"WEM[{index}] data hash: {o_hash} -> {m_hash}")
        changed = True
    if orig.descriptor.offset != mod.descriptor.offset:
        log.debug(f"WEM[{index}] offset: 0x{orig.descriptor.offset:x} -> 0x{mod.descriptor.offset:x}")

    return changed


def _vr_err(errors: List[str], log, msg: str) -> None:
    errors.append(msg)
    log.error(msg)


def _vr_warn(warnings: List[str], log, msg: str) -> None:
    warnings.append(msg)
    log.warn(msg)
