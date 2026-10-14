"""Subprocess helpers - argv lists only, never a shell string."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Optional, Sequence, Tuple

from tsbench.errors import Fatal


def run(argv: Sequence[str], timeout: Optional[float] = 60) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True,
                           timeout=timeout, shell=False)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"


def run_json(argv: Sequence[str], timeout: Optional[float] = 60) -> Any:
    rc, out, errtxt = run(argv, timeout=timeout)
    if rc != 0:
        raise Fatal(f"`{' '.join(argv)}` failed (rc={rc}): {errtxt.strip() or out.strip()}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise Fatal(f"`{' '.join(argv)}` did not return valid JSON: {exc}") from exc


def find_executable(name: str, extra_paths: Sequence[str] = (),
                    override: Optional[str] = None) -> Optional[str]:
    """Locate a tool.  Order: explicit override, PATH, then well-known install
    locations.  The last step matters under launchd/Raycast, whose PATH is minimal."""
    if override:
        return override if os.access(override, os.X_OK) else None
    found = shutil.which(name)
    if found:
        return found
    for cand in extra_paths:
        if os.access(cand, os.X_OK):
            return cand
    return None
