"""One run at a time.

Benchmarking switches the machine's exit node, so a second run (or an `apply`)
starting in the middle would corrupt the measurements and could leave the
machine on the wrong node.  Everything that changes the exit node takes this
lock first; a scheduled run that finds it held simply skips.

It is an advisory `flock`, so it is released by the kernel if the process dies:
there is no stale lock to clean up.
"""

from __future__ import annotations

import os
from typing import IO, Any, Optional

from tsbench.settings import state_dir


class LockBusy(Exception):
    """Another tsbench run holds the lock."""

    def __init__(self, holder: str) -> None:
        super().__init__(holder)
        self.holder = holder


def default_lock_path() -> str:
    return os.path.join(state_dir(), "run.lock")


class RunLock:
    def __init__(self, purpose: str, path: Optional[str] = None) -> None:
        self.purpose = purpose
        self.path = path or default_lock_path()
        self._fh: Optional[IO[str]] = None

    def acquire(self) -> None:
        import fcntl  # POSIX only; imported lazily

        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")        # noqa: SIM115
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = self._read_holder(fh)
            fh.close()
            raise LockBusy(holder) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()} {self.purpose}\n")
        fh.flush()
        self._fh = fh

    @staticmethod
    def _read_holder(fh: IO[str]) -> str:
        try:
            fh.seek(0)
            text = fh.read().strip()
            return text or "another tsbench run"
        except OSError:
            return "another tsbench run"

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()           # closing drops the flock
            finally:
                self._fh = None

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def describe_holder(holder: str) -> str:
    """'1234 auto' -> 'an automatic run (pid 1234)'."""
    parts = holder.split(None, 1)
    if len(parts) == 2 and parts[0].isdigit():
        what = {"auto": "an automatic run", "run": "a benchmark run",
                "apply": "an apply"}.get(parts[1], parts[1])
        return f"{what} (pid {parts[0]})"
    return holder
