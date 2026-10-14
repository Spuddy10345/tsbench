"""Terminal rendering and logging."""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime
from typing import Optional

from tsbench import PROG, __version__

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def fmt(v: Optional[float], nd: int = 1) -> str:
    return "-" if v is None else f"{v:.{nd}f}"


class Term:
    """Colour + in-place line rendering, disabled when not interactive."""

    def __init__(self, force_colour: Optional[bool] = None) -> None:
        tty = sys.stdout.isatty()
        if force_colour is None:
            enabled = tty and os.environ.get("NO_COLOR") is None \
                and os.environ.get("TERM", "") != "dumb"
        else:
            enabled = force_colour
        self.colour = enabled
        self.interactive = tty
        self._live = False

    def c(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.colour else text

    def bold(self, t: str) -> str:   return self.c(t, "1")
    def dim(self, t: str) -> str:    return self.c(t, "2")
    def red(self, t: str) -> str:    return self.c(t, "31")
    def green(self, t: str) -> str:  return self.c(t, "32")
    def yellow(self, t: str) -> str: return self.c(t, "33")
    def blue(self, t: str) -> str:   return self.c(t, "36")
    def mag(self, t: str) -> str:    return self.c(t, "35")

    @property
    def width(self) -> int:
        try:
            return max(60, shutil.get_terminal_size((100, 24)).columns)
        except Exception:
            return 100

    def live(self, text: str) -> None:
        """Update the current line in place (interactive) or stay quiet."""
        if not self.interactive:
            return
        self._live = True
        sys.stdout.write("\r\033[2K" + text[: self.width - 1])
        sys.stdout.flush()

    def endlive(self, final: Optional[str] = None) -> None:
        if self.interactive and self._live:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
        self._live = False
        if final is not None:
            print(final)


class Log:
    def __init__(self, term: Term, path: Optional[str], quiet: bool) -> None:
        self.term = term
        self.quiet = quiet
        self.fh = open(path, "a", encoding="utf-8") if path else None  # noqa: SIM115
        if self.fh:
            self.fh.write(f"\n=== {PROG} {__version__} run {datetime.now().isoformat()} ===\n")

    def _write(self, line: str) -> None:
        if self.fh:
            self.fh.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
            self.fh.flush()

    def say(self, line: str = "") -> None:
        self.term.endlive()
        if not self.quiet:
            print(line)
        self._write(strip_ansi(line))

    def info(self, line: str) -> None:
        self.say(f"  {line}")

    def ok(self, line: str) -> None:
        self.say(f"  {self.term.green('OK')}  {line}")

    def warn(self, line: str) -> None:
        self.term.endlive()
        print(f"  {self.term.yellow('WARN')}  {line}", file=sys.stderr)
        self._write("WARN " + strip_ansi(line))

    def err(self, line: str) -> None:
        self.term.endlive()
        print(f"  {self.term.red('ERROR')} {line}", file=sys.stderr)
        self._write("ERROR " + strip_ansi(line))

    def close(self) -> None:
        if self.fh:
            self.fh.close()
