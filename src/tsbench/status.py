"""`tsbench status`: what the history says, with no benchmarking."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tsbench import PROG
from tsbench.console import Log, Term
from tsbench.errors import Fatal
from tsbench.preflight import require_tailscale
from tsbench.settings import Settings, default_db_path
from tsbench.standing import compute_estimates, ranked, top_group, window_start
from tsbench.store import Store

WINDOW_DAYS = 14.0


def build_status_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{PROG} status",
                                description="Show remembered node standings from past runs.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("-n", "--limit", type=int, default=10, metavar="N",
                   help="rows to show (default: 10)")
    p.add_argument("--oneline", action="store_true",
                   help="print a single summary line (for status bars and launchers)")
    p.add_argument("--no-current", action="store_true",
                   help="do not ask Tailscale which node is active")
    p.add_argument("--tailscale-path", default=None, metavar="PATH")
    p.add_argument("--db", dest="db_path", default=None, metavar="PATH",
                   help=f"history database (default: {default_db_path()})")
    p.add_argument("--no-colour", dest="colour", action="store_false", default=None)
    return p


def _ago(ts: Optional[str], now: datetime) -> str:
    if not ts:
        return "never"
    dt = datetime.fromisoformat(ts)
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    h = (now - dt).total_seconds() / 3600.0
    if h < 1:
        return f"{h * 60:.0f} min ago"
    return f"{h:.1f} h ago" if h < 48 else f"{h / 24:.0f} d ago"


def status_main(argv: List[str]) -> int:
    args = build_status_parser().parse_args(argv)
    s = Settings.from_args(args)
    term = Term(force_colour=s.colour)
    log = Log(term, None, quiet=False)
    path = s.resolved_db_path
    if not os.path.exists(path):
        print(f"No history yet ({path}). Run `{PROG} --quick` to create some.")
        return 4

    now = datetime.now(timezone.utc)
    with Store(path) as store:
        tag = store.latest_network_tag()
        est = compute_estimates(store.observations(window_start(WINDOW_DAYS), tag), now)
        last_full = store.last_run_at("full")
        last_any = store.last_run_at()
        last = store.last_apply()
        last_apply = dict(last) if last else None
    order = ranked(est)
    group = top_group(order)

    current: Optional[str] = None
    if not args.no_current:
        try:
            ts, _ = require_tailscale(s, log)
            ip = ts.current_exit_ip()
            current = next((n.hostname for n in ts.discover() if n.ip == ip), ip) if ip else ""
        except Fatal:
            current = None            # Tailscale not available: status still works

    if args.json:
        payload: Dict[str, Any] = {
            "network": tag, "window_days": WINDOW_DAYS, "last_full_run": last_full,
            "last_run": last_any, "last_apply": last_apply, "current_exit_node": current,
            "top_group": [e.hostname for e in group],
            "nodes": [e.__dict__ for e in order],
        }
        print(json.dumps(payload, indent=2))
        return 0

    if not order:
        print("History exists but holds no throughput measurements yet. "
              f"Run `{PROG} --quick`.")
        return 4
    if args.oneline:
        names = ", ".join(e.hostname for e in group[:3]) + (" +more" if len(group) > 3 else "")
        now_on = ("" if current is None else f" | now: {current or 'direct'}")
        print(f"Best: {names}{now_on} | last full run {_ago(last_full, now)}")
        return 0
    print(term.bold("Remembered standing") + term.dim(
        f"  last {WINDOW_DAYS:g} days" + (f", network: {tag}" if tag else "")))
    print(term.dim("  est = recency-weighted standing, discounted for failures and "
                   "uncertainty (0-100)"))
    print()
    head = f"{'#':>3}  {'Exit node':<18} {'City':<11} {'Est':>5} {'Mean':>5} {'Obs':>4} " \
           f"{'Fail':>5} {'Lat':>6}  Last seen"
    print(term.bold(head))
    in_group = {e.hostname for e in group}
    for i, e in enumerate(order[:args.limit], 1):
        mark = "*" if e.hostname == current else " "
        line = (f"{i:>3}{mark} {e.hostname:<18} {e.city or '-':<11} {e.est:>5.1f} "
                f"{e.mean:>5.1f} {e.n:>4} {100 * e.fail_rate:>4.0f}% "
                f"{'-' if e.med_lat is None else format(e.med_lat, '.0f'):>6}  "
                f"{_ago(e.last_seen, now)}")
        print(term.green(line) if e.hostname in in_group else line)
    if len(order) > args.limit:
        print(term.dim(f"  ... {len(order) - args.limit} more (-n to show them)"))
    print()
    print(f"Top group (within 10% of the best, a statistical tie): "
          f"{term.bold(', '.join(e.hostname for e in group))}")
    if current is not None:
        where = current or "none (direct)"
        pos = next((i for i, e in enumerate(order, 1) if e.hostname == current), None)
        note = ("in the top group" if current in in_group
                else f"rank {pos} of {len(order)}" if pos else "not in the history")
        print(f"Current exit node: {term.bold(where)} ({note})")
    print(f"Last full run: {_ago(last_full, now)}   last run: {_ago(last_any, now)}"
          + (f"   last switch: {last_apply['hostname']} {_ago(last_apply['ts'], now)}"
             if last_apply else ""))
    return 0


if __name__ == "__main__":
    sys.exit(status_main(sys.argv[1:]))
