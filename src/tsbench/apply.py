"""Pick a node from memory and switch to it, without flapping.

Switching exit node drops live connections, and healthy nodes are close to a
statistical tie, so the automatic path (`--if-better`) only switches when:

  1. the current node is unhealthy (a live probe fails, or its latency is far
     above its own recent median), or
  2. the best remembered node beats the current one by at least `margin`,
     and the last switch was more than `cooldown_h` ago.

It also refuses to touch an exit node it does not manage: direct connectivity,
or a node that tsbench has never seen and did not apply itself.  `--force`
overrides that, and the cooldown.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from tsbench import PROG
from tsbench.console import Log, Term
from tsbench.discovery import filter_nodes
from tsbench.errors import Fatal
from tsbench.models import Node
from tsbench.preflight import require_tailscale
from tsbench.probes import NativeProbe
from tsbench.settings import Settings, default_db_path
from tsbench.standing import Estimate, compute_estimates, ranked, window_start
from tsbench.store import Store
from tsbench.tailscale import Tailscale

DEFAULT_MARGIN = 0.10
DEFAULT_COOLDOWN_H = 2.0
HEALTH_FACTOR = 1.5          # unhealthy when latency > 1.5x its own median...
HEALTH_SLACK_MS = 10.0       # ...and at least this much worse in absolute terms
HISTORY_DAYS = 14.0

EXIT_OK, EXIT_ERROR, EXIT_NO_DATA = 0, 1, 4


@dataclass
class Health:
    ok: bool
    reason: str
    latency_ms: Optional[float] = None
    median_ms: Optional[float] = None


@dataclass
class Decision:
    action: str                       # switch | stay | skip
    reason: str
    current: Optional[str] = None     # hostname of the active exit node, if any
    target: Optional[str] = None      # hostname to switch to
    target_ip: Optional[str] = None
    current_est: Optional[float] = None
    target_est: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def assess_health(latency_ms: Optional[float], history: Sequence[float]) -> Health:
    """Judge a live latency reading against the node's own recent history."""
    if latency_ms is None:
        return Health(False, "live probe failed: no connection through this exit node")
    if len(history) < 3:
        return Health(True, "no latency history to compare with", latency_ms)
    med = statistics.median(history)
    if latency_ms > HEALTH_FACTOR * med and latency_ms > med + HEALTH_SLACK_MS:
        return Health(False, f"latency {latency_ms:.0f} ms is well above its usual "
                             f"{med:.0f} ms", latency_ms, med)
    return Health(True, f"latency {latency_ms:.0f} ms (usual {med:.0f} ms)", latency_ms, med)


def _age_hours(ts: str, now: datetime) -> float:
    dt = datetime.fromisoformat(ts)
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def decide(candidates: Sequence[Node], estimates: Mapping[str, Estimate],
           current: Optional[Node], current_ip: Optional[str],
           health: Optional[Health], last_apply: Optional[Mapping[str, Any]],
           now: Optional[datetime] = None, if_better: bool = True, force: bool = False,
           margin: float = DEFAULT_MARGIN, cooldown_h: float = DEFAULT_COOLDOWN_H
           ) -> Decision:
    now = now or datetime.now(timezone.utc)
    by_host = {n.hostname: n for n in candidates}
    order = ranked(estimates, allowed=list(by_host))
    cur_host = current.hostname if current else None
    d = Decision("skip", "", current=cur_host or current_ip)
    if not order:
        d.reason = "no remembered data for the available nodes; run `tsbench run --quick`"
        return d

    def target_for(e: Estimate) -> Decision:
        n = by_host[e.hostname]
        cur_est = estimates[cur_host].est if cur_host in estimates else None
        return Decision("switch", "", current=d.current, target=n.hostname, target_ip=n.ip,
                        current_est=cur_est, target_est=e.est)

    best = order[0]
    cur_est = estimates[cur_host].est if cur_host in estimates else None
    d.current_est = cur_est

    if not if_better:
        if cur_host == best.hostname:
            d.action, d.reason = "stay", "already on the best remembered node"
            return d
        t = target_for(best)
        t.reason = "best remembered node"
        return t

    applied_host = last_apply["hostname"] if last_apply else None
    managed = current_ip is not None and (cur_host in estimates or cur_host == applied_host)
    if not managed and not force:
        d.reason = ("exit node is off (direct connectivity)" if current_ip is None
                    else "current exit node was not chosen by tsbench") \
            + "; leaving it alone (use --force to override)"
        return d

    others = [e for e in order if e.hostname != cur_host]
    if health is not None and not health.ok and others:
        t = target_for(others[0])
        t.reason = f"current node unhealthy: {health.reason}"
        return t

    if cur_host == best.hostname:
        d.action, d.reason = "stay", "already on the best remembered node"
        return d
    if last_apply and not force and _age_hours(last_apply["ts"], now) < cooldown_h:
        d.action = "stay"
        d.reason = (f"last switch was {_age_hours(last_apply['ts'], now):.1f} h ago "
                    f"(cooldown {cooldown_h:g} h)")
        return d
    base = cur_est or 0.0
    if best.est >= base * (1.0 + margin) and best.est > base:
        t = target_for(best)
        t.reason = (f"{best.hostname} ({best.est:.0f}) beats {cur_host or 'current'} "
                    f"({base:.0f}) by {100 * (best.est - base) / base:.0f}%" if base
                    else f"{best.hostname} is remembered; the current node is not")
        return t
    d.action = "stay"
    d.reason = (f"best remembered node ({best.hostname}, {best.est:.0f}) is within "
                f"{100 * margin:.0f}% of the current one ({base:.0f}): a tie, not worth a switch")
    return d


def switch_exit(ts: Tailscale, node: Node, wait_s: float = 12.0) -> str:
    """Set the exit node and wait for Tailscale to report it.  Returns '' on
    success, else an error message."""
    ok, err = ts.set_exit_node(node.ip)
    if not ok:
        return err
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if ts.current_exit_ip() == node.ip:
            return ""
        time.sleep(0.4)
    return f"Tailscale did not report {node.hostname} as the exit node within {wait_s:g}s"


# --------------------------------------------------------------------------- #
# command
# --------------------------------------------------------------------------- #


def build_apply_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{PROG} apply",
        description="Switch to the best remembered exit node, using the history database "
                    "(no benchmarking).")
    p.add_argument("--if-better", action="store_true",
                   help="only switch when the current node is unhealthy or clearly worse "
                        "(hysteresis, cooldown, never override a manual choice)")
    p.add_argument("--force", action="store_true",
                   help="with --if-better: ignore the cooldown and the manual-choice guard")
    p.add_argument("--dry-run", action="store_true", help="print the decision, change nothing")
    p.add_argument("--json", action="store_true", help="print the decision as JSON")
    p.add_argument("--margin", type=float, default=DEFAULT_MARGIN, metavar="FRAC",
                   help=f"required improvement to switch (default: {DEFAULT_MARGIN})")
    p.add_argument("--cooldown-hours", type=float, default=DEFAULT_COOLDOWN_H, metavar="H",
                   help="minimum time between automatic switches "
                        f"(default: {DEFAULT_COOLDOWN_H:g})")
    p.add_argument("--no-health-check", action="store_true",
                   help="do not probe the current node before deciding")
    p.add_argument("--country", default="uk", metavar="NAME")
    p.add_argument("--cities", default="", metavar="LIST")
    p.add_argument("--exclude-cities", default="", metavar="LIST")
    p.add_argument("--tailscale-path", default=None, metavar="PATH")
    p.add_argument("--probe-host", default="1.1.1.1", metavar="HOST")
    p.add_argument("--db", dest="db_path", default=None, metavar="PATH",
                   help=f"history database (default: {default_db_path()})")
    p.add_argument("--no-colour", dest="colour", action="store_false", default=None)
    return p


def run_apply(ts: Tailscale, s: Settings, *, if_better: bool, force: bool = False,
              dry_run: bool = False, margin: float = DEFAULT_MARGIN,
              cooldown_h: float = DEFAULT_COOLDOWN_H, health_check: bool = True
              ) -> Tuple[Decision, bool]:
    """Decide from memory and (unless `dry_run`) act.  Returns (decision, had_data)."""
    all_nodes = ts.discover()
    candidates = [n for n in filter_nodes(all_nodes, s) if n.online]
    current_ip = ts.current_exit_ip()
    current = next((n for n in all_nodes if n.ip == current_ip), None)

    if not os.path.exists(s.resolved_db_path):       # don't create an empty database
        return Decision("skip", "no history yet; run `tsbench run --quick` first",
                        current=current.hostname if current else current_ip), False

    with Store(s.resolved_db_path) as store:
        tag = store.latest_network_tag()
        estimates = compute_estimates(store.observations(window_start(HISTORY_DAYS), tag))
        last = store.last_apply()
        last_apply = dict(last) if last else None

        health: Optional[Health] = None
        if if_better and health_check and current is not None \
                and current.hostname in estimates:
            lat = NativeProbe(host=s.probe_host).latency(3)["latency_ms"]
            health = assess_health(lat, store.latency_history(current.hostname))

        d = decide(candidates, estimates, current, current_ip, health, last_apply,
                   if_better=if_better, force=force, margin=margin, cooldown_h=cooldown_h)

        if d.action == "switch" and not dry_run:
            target = next(n for n in candidates if n.hostname == d.target)
            err = switch_exit(ts, target)
            if err:
                raise Fatal(f"could not switch to {d.target}: {err}")
            store.record_apply(d.target, d.target_ip, d.current, d.reason)
    return d, bool(estimates)


def apply_main(argv: List[str]) -> int:
    args = build_apply_parser().parse_args(argv)
    s = Settings.from_args(args)
    s.probe_host = args.probe_host
    term = Term(force_colour=s.colour)
    log = Log(term, None, quiet=args.json)
    try:
        ts, _ = require_tailscale(s, log)
        d, had_data = run_apply(ts, s, if_better=args.if_better, force=args.force,
                                dry_run=args.dry_run, margin=args.margin,
                                cooldown_h=args.cooldown_hours,
                                health_check=not args.no_health_check)
        _print_decision(d, args, term, log)
        return EXIT_NO_DATA if (d.action == "skip" and not had_data) else EXIT_OK
    except Fatal as exc:
        log.err(str(exc))
        return EXIT_ERROR


def _print_decision(d: Decision, args: argparse.Namespace, term: Term, log: Log) -> None:
    if args.json:
        out = d.as_dict()
        out["dry_run"] = bool(args.dry_run)
        print(json.dumps(out, indent=2))
        return
    if d.action == "switch":
        verb = "would switch" if args.dry_run else "switched"
        log.ok(f"{verb} {d.current or 'direct'} -> {term.bold(d.target or '?')}: {d.reason}")
    elif d.action == "stay":
        log.info(f"staying on {term.bold(d.current or 'direct')}: {d.reason}")
    else:
        log.warn(f"nothing done: {d.reason}")


if __name__ == "__main__":
    sys.exit(apply_main(sys.argv[1:]))
