"""`tsbench auto`: one unattended step, meant to be fired by a scheduler.

Each invocation does exactly one of:

  * a **full** run (Ookla, screens first, then applies the best node) when the
    last full run is older than `--full-every-hours`;
  * a **refresh**: a quick probe of only the best remembered nodes, then
    `apply --if-better` -- about a minute, no 22-node screen;
  * nothing, when a guard says the machine is busy, or another run is active.

It only schedules and guards; the measuring and the switching are the same
code a manual `tsbench --quick --apply` runs, with the same hysteresis.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from tsbench import PROG
from tsbench.errors import EXIT_LOCKED, EXIT_SKIPPED
from tsbench.guards import DEFAULT_CALL_PROCESSES, DEFAULT_MIN_IDLE_S, check_guards
from tsbench.lock import LockBusy, RunLock, describe_holder
from tsbench.settings import default_db_path, state_dir
from tsbench.store import Store

DEFAULT_FULL_EVERY_H = 18.0     # inside the 12-24 h window; tune with --full-every-hours
DEFAULT_REFRESH_NODES = 3
DEFAULT_REFRESH_PROBE_S = 4.0   # shorter than a manual probe: this runs unattended
LOG_MAX_BYTES = 1_000_000


@dataclass
class Plan:
    kind: str                   # full | deep | refresh
    reason: str
    argv: List[str]             # arguments for run_main


def _age_hours(ts: Optional[str], now: datetime) -> Optional[float]:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def make_plan(last_full: Optional[str], last_screen: Optional[str], now: datetime,
              full_every_h: float, can_full: bool, refresh_nodes: int,
              extra: Optional[List[str]] = None,
              refresh_probe_s: float = DEFAULT_REFRESH_PROBE_S) -> Plan:
    """Choose what this step should do.

    A "deep" run is the full tier when Ookla's licence has been accepted, else a
    wide quick run (screen everything, probe the best eight plus favourites).
    Either way it refreshes the candidate set; a refresh only re-measures the
    nodes already known to be good.  Staleness of a deep run is judged from the
    last full run, or from the last latency screen when full runs are not
    permitted (every deep run records a screen; a refresh does not).
    """
    extra = list(extra or [])
    common = ["--apply", "--quiet", "--no-colour"]
    last = last_full if can_full else last_screen
    age = _age_hours(last, now)
    what = "full run" if can_full else "wide quick run (no Ookla licence accepted)"
    if age is None or age >= full_every_h:
        since = "has never run" if age is None else f"was {age:.1f} h ago"
        reason = f"the last {what} {since} (limit {full_every_h:g} h)"
        if can_full:
            return Plan("full", reason, ["--accept-ookla-license", *common, *extra])
        return Plan("deep", reason, ["--quick", "--top", "8", "--favourites", "3",
                                     *common, *extra])
    return Plan("refresh",
                f"the last {what} was {age:.1f} h ago (limit {full_every_h:g} h)",
                ["--quick", "--remembered", str(refresh_nodes),
                 "--probe-seconds", f"{refresh_probe_s:g}", *common, *extra])


def build_auto_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{PROG} auto",
        description="One scheduled step: skip if the machine is busy, otherwise refresh the "
                    "best remembered nodes (or run a full benchmark when it is due) and "
                    "apply the best node if it is clearly worth it.")
    p.add_argument("--full-every-hours", type=float, default=DEFAULT_FULL_EVERY_H, metavar="H",
                   help=f"run a full benchmark when the last one is older than this "
                        f"(default: {DEFAULT_FULL_EVERY_H:g})")
    p.add_argument("--refresh-nodes", type=int, default=DEFAULT_REFRESH_NODES, metavar="N",
                   help=f"nodes re-measured by a refresh (default: {DEFAULT_REFRESH_NODES})")
    p.add_argument("--probe-seconds", type=float, default=DEFAULT_REFRESH_PROBE_S, metavar="SEC",
                   help="time cap per throughput probe in a refresh; the data used grows with "
                        f"your line speed (default: {DEFAULT_REFRESH_PROBE_S:g})")
    g = p.add_argument_group("guards (a scheduled run skips when one of these says no)")
    g.add_argument("--no-guard", action="store_true", help="disable every guard")
    g.add_argument("--allow-battery", action="store_true",
                   help="do not skip when running on battery power")
    g.add_argument("--min-idle", type=float, default=DEFAULT_MIN_IDLE_S, metavar="SEC",
                   help=f"skip unless the keyboard/mouse have been idle this long "
                        f"(macOS; 0 disables; default: {DEFAULT_MIN_IDLE_S:g})")
    g.add_argument("--call-app", action="append", default=None, metavar="PROCESS",
                   help="skip while this process is running (repeatable; replaces the "
                        f"default list: {', '.join(DEFAULT_CALL_PROCESSES)})")
    p.add_argument("--accept-ookla-license", action="store_true",
                   help="confirm you accepted Ookla's licence so full runs may run "
                        "unattended (or set TSBENCH_ACCEPT_OOKLA_LICENSE=1); without it the "
                        "periodic deep run uses the built-in probe instead")
    p.add_argument("--dry-run", action="store_true",
                   help="show the guards and the plan, then exit without running anything")
    p.add_argument("--log-file", default=None, metavar="PATH",
                   help=f"append here (default: {os.path.join(state_dir(), 'auto.log')})")
    p.add_argument("--db", dest="db_path", default=None, metavar="PATH")
    p.add_argument("--tailscale-path", default=None, metavar="PATH")
    p.add_argument("--tester-path", default=None, metavar="PATH")
    p.add_argument("--country", default=None, metavar="NAME")
    p.add_argument("--cities", default=None, metavar="LIST")
    p.add_argument("--exclude-cities", default=None, metavar="LIST")
    return p


def rotate_log(path: str, max_bytes: int = LOG_MAX_BYTES) -> None:
    """Keep one previous log so an unattended job cannot fill the disk."""
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass


class _AutoLog:
    def __init__(self, path: str, echo: bool) -> None:
        self.path = path
        self.echo = echo

    def __call__(self, msg: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} auto: {msg}"
        if self.echo:
            print(line, file=sys.stderr)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def _passthrough(args: argparse.Namespace) -> List[str]:
    extra: List[str] = []
    for flag, val in (("--db", args.db_path), ("--tailscale-path", args.tailscale_path),
                      ("--tester-path", args.tester_path), ("--country", args.country),
                      ("--cities", args.cities), ("--exclude-cities", args.exclude_cities)):
        if val:
            extra += [flag, val]
    return extra


def auto_main(argv: List[str]) -> int:
    args = build_auto_parser().parse_args(argv)
    log_path = args.log_file or os.path.join(state_dir(), "auto.log")
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), mode=0o700, exist_ok=True)
    rotate_log(log_path)
    say = _AutoLog(log_path, echo=sys.stderr.isatty() or args.dry_run)

    can_full = args.accept_ookla_license or (
        os.environ.get("TSBENCH_ACCEPT_OOKLA_LICENSE", "") not in ("", "0"))
    db = args.db_path or default_db_path()
    now = datetime.now(timezone.utc)

    last_full = last_screen = None
    if os.path.exists(db):
        with Store(db) as store:
            last_full, last_screen = store.last_run_at("full"), store.last_run_at("screen")
    plan = make_plan(last_full, last_screen, now, args.full_every_hours, can_full,
                     args.refresh_nodes, _passthrough(args), args.probe_seconds)

    reason = None if args.no_guard else check_guards(
        min_idle_s=args.min_idle,
        call_apps=args.call_app if args.call_app is not None else DEFAULT_CALL_PROCESSES,
        battery=not args.allow_battery)

    if args.dry_run:
        say(f"guards: {'would skip -- ' + reason if reason else 'clear'}")
        say(f"plan: {plan.kind} -- {plan.reason}")
        say(f"would run: {PROG} {' '.join(plan.argv)}")
        return 0
    if reason:
        say(f"skipped: {reason}")
        return EXIT_SKIPPED

    lock = RunLock("auto")
    try:
        lock.acquire()
    except LockBusy as busy:
        say(f"skipped: {describe_holder(busy.holder)} is already running")
        return EXIT_LOCKED
    try:
        say(f"starting a {plan.kind} step: {plan.reason}")
        from tsbench.cli import run_main
        rc = run_main([*plan.argv, "--log-file", log_path], locked=True)
        say(f"finished the {plan.kind} step with exit code {rc}")
        return rc
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(auto_main(sys.argv[1:]))
