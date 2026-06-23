"""
LOGGER
Colored terminal logger for step-by-step pipeline inspection.
"""

import sys
from enum import IntEnum
from typing import Optional

__all__ = ["Level", "Logger", "log"]


class Level(IntEnum):
    DEBUG = 0
    INFO = 1
    STEP = 2
    OK = 3
    WARN = 4
    ERROR = 5


_COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
}


class Logger:
    """Context-aware logger that prefixes every line with a step breadcrumb."""

    def __init__(self, level: Level = Level.INFO):
        self.level = level
        self._indent = 0
        self._step_stack: list[str] = []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _prefix(self, label: str, color: str) -> str:
        indent = "  " * self._indent
        breadcrumb = " > ".join(self._step_stack)
        if breadcrumb:
            breadcrumb = f"[{breadcrumb}] "
        return f"{indent}{_COLORS[color]}{_COLORS['bold']}{label}{_COLORS['reset']} {breadcrumb}"

    def _write(self, prefix: str, msg: str) -> None:
        for line in msg.splitlines():
            text = f"{prefix}{line}\n"
            try:
                sys.stdout.write(text)
            except UnicodeEncodeError:
                # Windows console fallback: encode safely
                sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def step(self, name: str):
        """Enter a named step (context manager)."""
        return _StepContext(self, name)

    def debug(self, msg: str) -> None:
        if self.level <= Level.DEBUG:
            self._write(self._prefix("[DBG]", "blue"), msg)

    def info(self, msg: str) -> None:
        if self.level <= Level.INFO:
            self._write(self._prefix("[INF]", "cyan"), msg)

    def ok(self, msg: str) -> None:
        if self.level <= Level.OK:
            self._write(self._prefix("[OK ]", "green"), msg)

    def warn(self, msg: str) -> None:
        if self.level <= Level.WARN:
            self._write(self._prefix("[WRN]", "yellow"), msg)

    def error(self, msg: str) -> None:
        if self.level <= Level.ERROR:
            self._write(self._prefix("[ERR]", "red"), msg)

    def hex_dump(self, data: bytes, offset: int = 0, length: int = 64, title: str = "Hex dump") -> None:
        """Print a formatted hex dump."""
        if self.level > Level.DEBUG:
            return
        slice_ = data[offset:offset + length]
        lines: list[str] = []
        for i in range(0, len(slice_), 16):
            chunk = slice_[i:i + 16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            hex_part = f"{hex_part:<48}"
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"  {offset + i:08x}  {hex_part}  |{ascii_part}|")
        self._write(self._prefix("[HEX]", "magenta"), f"{title} ({len(slice_)} bytes):")
        for line in lines:
            self._write("", line)

    def field(self, name: str, value: object, fmt: str = "") -> None:
        """Log a single parsed field."""
        if fmt:
            val = fmt.format(value)
        else:
            val = repr(value)
        self.debug(f"  {name:<30} = {val}")

    def table(self, headers: tuple[str, ...], rows: list[tuple]) -> None:
        """Print an aligned ASCII table."""
        if self.level > Level.INFO:
            return
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell)))
        sep = " | ".join("-" * w for w in widths)
        header_line = " | ".join(h.ljust(w) for h, w in zip(headers, widths))
        self._write("", header_line)
        self._write("", sep)
        for row in rows:
            line = " | ".join(str(c).ljust(w) for c, w in zip(row, widths))
            self._write("", line)


class _StepContext:
    def __init__(self, logger: Logger, name: str):
        self.logger = logger
        self.name = name

    def __enter__(self):
        self.logger._step_stack.append(self.name)
        self.logger._indent = len(self.logger._step_stack) - 1
        self.logger.info(f">>> START: {self.name}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.logger.error(f"<<< FAILED: {self.name} - {exc_val}")
        else:
            self.logger.ok(f"<<< DONE: {self.name}")
        self.logger._step_stack.pop()
        self.logger._indent = max(0, len(self.logger._step_stack) - 1)
        return False


# Module-level default logger
log = Logger(Level.DEBUG)
