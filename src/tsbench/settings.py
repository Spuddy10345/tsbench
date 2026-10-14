"""Every tunable in one place.

CLI flags populate a `Settings`; nothing else reads `argparse.Namespace`.  A
config file can later be loaded into the same dataclass without touching any
call site.
"""

from __future__ import annotations

import argparse
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


@dataclass
class Settings:
    # selection - empty `cities` means every city in `country`
    cities: List[str] = field(default_factory=list)
    exclude_cities: List[str] = field(default_factory=list)
    country: str = DEFAULT_COUNTRY
    max_nodes: Optional[int] = None
    include_offline: bool = False

    # benchmark
    reps: int = 3
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
        return cls(**values)

    @property
    def min_success(self) -> int:
        import math
        return max(2, math.ceil(self.reps / 2)) if self.reps > 1 else 1

    @property
    def resolved_db_path(self) -> str:
        return self.db_path or default_db_path()
