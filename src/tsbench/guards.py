"""Conditions under which a scheduled run should not start.

A benchmark briefly reroutes all traffic and can pull a few hundred MB, so an
unattended run should stay out of the way: no battery drain, no interrupting a
call, no switching exit node under someone who is actively using the machine.

Every check answers "unknown" (does not block) when the platform cannot tell,
and each guard can be adjusted or disabled from the command line.  Detecting a
call from process names is a heuristic: by default only processes that exist
for the duration of a call are matched, because matching an app that is merely
open (Teams, Zoom's main window) would disable scheduled runs for most people.
"""

from __future__ import annotations

import glob
import os
import re
import sys
from typing import Callable, List, Optional, Sequence, Tuple

from tsbench.proc import run

Runner = Callable[..., Tuple[int, str, str]]

# Lower-case process names that exist (almost) only while a call is happening.
# Zoom's `CptHost` is its in-meeting helper; FaceTime has no background process.
DEFAULT_CALL_PROCESSES = ("cpthost", "facetime")
DEFAULT_MIN_IDLE_S = 60.0

_HID_IDLE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


def on_battery(runner: Runner = run, platform: Optional[str] = None,
               power_dir: str = "/sys/class/power_supply") -> Optional[bool]:
    """True on battery power, False on mains, None when unknown."""
    platform = platform or sys.platform
    if platform == "darwin":
        rc, out, _ = runner(["pmset", "-g", "batt"], timeout=10)
        if rc != 0:
            return None
        first = out.splitlines()[0] if out.strip() else ""
        if "Battery Power" in first:
            return True
        if "AC Power" in first:
            return False
        return None
    status_files = glob.glob(os.path.join(power_dir, "BAT*", "status"))
    if not status_files:
        return None                      # no battery: a desktop, treated as mains
    try:
        states = [open(p, encoding="utf-8").read().strip() for p in status_files]  # noqa: SIM115
    except OSError:
        return None
    return any(s == "Discharging" for s in states)


def idle_seconds(runner: Runner = run, platform: Optional[str] = None) -> Optional[float]:
    """Seconds since the last keyboard/mouse input, or None when unknown."""
    platform = platform or sys.platform
    if platform != "darwin":
        return None
    rc, out, _ = runner(["ioreg", "-c", "IOHIDSystem"], timeout=10)
    if rc != 0:
        return None
    m = _HID_IDLE.search(out)
    return int(m.group(1)) / 1e9 if m else None


def running_call_apps(names: Sequence[str], runner: Runner = run,
                      platform: Optional[str] = None) -> List[str]:
    """Which of `names` (matched case-insensitively against the process name)
    are running right now."""
    wanted = {n.strip().lower() for n in names if n.strip()}
    if not wanted:
        return []
    platform = platform or sys.platform
    argv = ["ps", "-axco", "comm="] if platform == "darwin" else ["ps", "-eo", "comm="]
    rc, out, _ = runner(argv, timeout=10)
    if rc != 0:
        return []
    running = {os.path.basename(line.strip()).lower() for line in out.splitlines()}
    return sorted(wanted & running)


def check_guards(min_idle_s: float = DEFAULT_MIN_IDLE_S,
                 call_apps: Sequence[str] = DEFAULT_CALL_PROCESSES,
                 battery: bool = True, runner: Runner = run,
                 platform: Optional[str] = None) -> Optional[str]:
    """The reason a scheduled run should skip, or None when it is fine to go."""
    if battery and on_battery(runner, platform):
        return "the machine is on battery power"
    if min_idle_s > 0:
        idle = idle_seconds(runner, platform)
        if idle is not None and idle < min_idle_s:
            return (f"the machine was in use {idle:.0f}s ago "
                    f"(waiting for {min_idle_s:g}s of idle time)")
    busy = running_call_apps(call_apps, runner, platform)
    if busy:
        return f"a call appears to be in progress ({', '.join(busy)})"
    return None
