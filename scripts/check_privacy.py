#!/usr/bin/env python3
"""Fail if tracked files look like they contain private network data.

tsbench output holds the tailnet name, Tailscale IPs and the public IP, so this
guards the repository against someone (including me) committing a result file
or pasting real output into docs, tests or fixtures.  Synthetic fixtures use
100.64.x.x addresses and made-up host names, which are allowed.

Run from anywhere inside the repository:  python scripts/check_privacy.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List

FORBIDDEN_PATHS = (
    re.compile(r"(^|/)tsbench-results(/|$)"),
    re.compile(r"\.(db|sqlite3?)(-journal|-wal|-shm)?$"),
)

CONTENT_RULES = (
    # A real tailnet DNS name: <host>.tail<hex>.ts.net
    ("tailnet DNS name", re.compile(r"\btail[0-9a-f]{4,}\.ts\.net\b", re.I)),
    # Tailscale hands out 100.64.0.0/10.  Only 100.64.x.x is used for fixtures.
    ("non-synthetic Tailscale IP",
     re.compile(r"\b100\.(?:6[5-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
)

MAX_BYTES = 2_000_000


def tracked_files(root: Path) -> List[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True,
                         capture_output=True).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def scan(root: Path, files: Iterable[str]) -> List[str]:
    problems: List[str] = []
    for rel in files:
        for pat in FORBIDDEN_PATHS:
            if pat.search(rel):
                problems.append(f"{rel}: tracked file must not be committed")
        path = root / rel
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            continue
        data = path.read_bytes()
        if b"\0" in data[:4096]:
            continue
        text = data.decode("utf-8", "replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pat in CONTENT_RULES:
                m = pat.search(line)
                if m:
                    problems.append(f"{rel}:{lineno}: {label}: {m.group(0)}")
    return problems


def main() -> int:
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True,
                               capture_output=True, text=True).stdout.strip())
    problems = scan(root, tracked_files(root))
    if problems:
        print("Privacy guard failed:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print("privacy guard: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
