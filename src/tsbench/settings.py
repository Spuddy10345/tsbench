"""Every tunable in one place.

CLI flags populate a `Settings`; nothing else reads `argparse.Namespace`.  A
config file can later be loaded into the same dataclass without touching any
call site.
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass, field, fields
from typing import List, Optional

from tsbench.matching import split_list

DEFAULT_COUNTRY = "uk"


def state_dir() -> str:
    """Where tsbench keeps its history: $TSBENCH_HOME, else the XDG state dir."""
    override = os.environ.get("TSBENCH_HOME")
    if override:
        return override
    base = (os.environ.get("XDG_STATE_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "state"))
    return os.path.join(base, "tsbench")


def default_db_path() -> str:
    return os.path.join(state_dir(), "tsbench.db")


def min_success_for(reps: int) -> int:
    """Successful repetitions a node needs before its numbers count."""
    return max(2, math.ceil(reps / 2)) if reps > 1 else 1


@dataclass
class Settings:
    # selection - empty `cities` means every city in `country`
    cities: List[str] = field(default_factory=list)
    exclude_cities: List[str] = field(default_factory=list)
    country: str = DEFAULT_COUNTRY
    max_nodes: Optional[int] = None
    include_offline: bool = False

    # benchmark
    reps: Optional[int] = None       # None = 3 (full) or 1 (screen/quick); see `reps_n`
    tester: str = "ookla"
    tester_path: Optional[str] = None
    tailscale_path: Optional[str] = None
    accept_ookla_license: bool = False
    server_id: Optional[int] = None
    pin_server: bool = True
    baseline: bool = True
    switch_delay: float = 3.0
    rep_delay: float = 1.0
    ready_timeout: float = 30.0
    test_timeout: float = 180.0
    no_egress_check: bool = False

    # tiers: screen = latency only, quick = screen + short throughput probe
    quick: bool = False
    screen_only: bool = False
    screen: bool = True              # full tier: screen first, test only the survivors
    top_k: int = 5                   # quick: probe this many of the best-screened nodes
    favourites: int = 2              # quick: ...plus this many top remembered nodes
    remembered: int = 0              # quick: no screen, probe only the N best remembered nodes
    screen_samples: int = 5
    probe_host: str = "1.1.1.1"
    probe_url: str = "https://speed.cloudflare.com/__down?bytes=25000000"
    probe_cap_s: float = 6.0
    apply: bool = False              # after the run, apply the best node (with hysteresis)

    # behaviour
    dry_run: bool = False
    fail_fast: bool = False
    apply_best: bool = False
    keep_raw: bool = False

    # persistence / output
    save: bool = True
    db_path: Optional[str] = None
    out_dir: Optional[str] = None
    run_name: Optional[str] = None
    force: bool = False
    log_file: Optional[str] = None
    colour: Optional[bool] = None
    quiet: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> Settings:
        values = {}
        names = {f.name for f in fields(cls)}
        for name in names:
            if hasattr(ns, name):
                values[name] = getattr(ns, name)
        for lst in ("cities", "exclude_cities"):
            if isinstance(values.get(lst), str):
                values[lst] = split_list(values[lst])
        if not values.get("accept_ookla_license"):
            values["accept_ookla_license"] = (
                os.environ.get("TSBENCH_ACCEPT_OOKLA_LICENSE", "") not in ("", "0"))
        explicit_delay = values.get("switch_delay") is not None
        for k in ("probe_url", "switch_delay"):
            if values.get(k) is None:
                values.pop(k, None)
        s = cls(**values)
        if not explicit_delay and s.tier != "full":
            s.switch_delay = 1.0         # short tiers: don't idle between nodes
        return s

    @property
    def reps_n(self) -> int:
        """Repetitions per node: explicit --reps, else 3 for a full run and 1 for the
        screen and quick tiers (which are meant to be short)."""
        return self.reps if self.reps is not None else (3 if self.tier == "full" else 1)

    @property
    def tier(self) -> str:
        return "screen" if self.screen_only else "quick" if self.quick else "full"

    @property
    def min_success(self) -> int:
        return min_success_for(self.reps_n)

    @property
    def resolved_db_path(self) -> str:
        return self.db_path or default_db_path()
