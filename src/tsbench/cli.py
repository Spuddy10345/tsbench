"""Command-line interface.

    tsbench [run] [flags]      benchmark exit nodes (bare `tsbench` is `run`)
    tsbench status             show remembered standings from past runs
    tsbench apply              switch to the best remembered node (no benchmarking)
    tsbench history list|prune inspect or trim the saved history
    tsbench auto               one scheduled step: guards, then a refresh or a full run
    tsbench schedule ...       install/uninstall/status of the macOS launchd job
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tsbench import PROG, __version__
from tsbench.bench import Benchmark
from tsbench.console import Log, Term, fmt
from tsbench.discovery import select_nodes
from tsbench.errors import EXIT_LOCKED, Fatal
from tsbench.lock import LockBusy, RunLock, describe_holder
from tsbench.models import Node, NodeResult, baseline_node, ranked_ok
from tsbench.preflight import Tools, preflight
from tsbench.probes import TESTERS, NativeProbe
from tsbench.report import render_table, write_outputs
from tsbench.scoring import TOP_N, score_nodes
from tsbench.screen import cut_outliers, failed_to_screen, pick_candidates, rank_screen
from tsbench.settings import DEFAULT_COUNTRY, Settings, default_db_path, min_success_for
from tsbench.standing import compute_estimates, ranked, window_start
from tsbench.store import Store

# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Benchmark Tailscale (Mullvad) exit nodes and recommend the best few.",
        epilog="""\
examples:
  tsbench                                    # every UK node, 3 reps, + DIRECT baseline
  tsbench --quick                            # ~3 min: latency screen + short probe of the best
  tsbench --screen-only                      # ~2 min: latency screen only
  tsbench status                             # what the history says is best
  tsbench apply --if-better                  # switch only if clearly worth it
  tsbench auto                               # one scheduled step (see `tsbench schedule`)
  tsbench --dry-run                          # just show which nodes would be tested
  tsbench --cities London,Manchester         # custom city selection
  tsbench --max-nodes 5                      # limit the run
  tsbench --reps 5 --switch-delay 5          # more repetitions, slower switching
  tsbench --apply-best                       # stay connected to the winner
  tsbench --no-save --out-dir ./report       # don't touch history; export files instead
  tsbench history list                       # past runs

Results are saved to a local history database (see `tsbench history list`).
Pass --out-dir to also export JSON/CSV/Markdown for a run.
""")
    g = p.add_argument_group("selection")
    g.add_argument("--cities", default="", metavar="LIST",
                   help="comma-separated cities to include (default: every city in --country)")
    g.add_argument("--exclude-cities", default="", metavar="LIST",
                   help="comma-separated cities to exclude (default: none)")
    g.add_argument("--country", default=DEFAULT_COUNTRY, metavar="NAME",
                   help=f"country filter passed to `tailscale exit-node list` and matched "
                        f"against node metadata (default: {DEFAULT_COUNTRY})")
    g.add_argument("--max-nodes", type=int, default=None, metavar="N",
                   help="test at most N nodes (default: all matching)")
    g.add_argument("--include-offline", action="store_true",
                   help="also test nodes Tailscale reports as offline")

    g = p.add_argument_group("tiers")
    g.add_argument("--quick", action="store_true",
                   help="latency-screen every node, then run one short built-in throughput "
                        "probe on the best few plus remembered favourites (no Ookla)")
    g.add_argument("--screen-only", action="store_true",
                   help="latency screen only: connect to each node, take a few TCP "
                        "handshake samples, measure no throughput")
    g.add_argument("--no-screen", dest="screen", action="store_false",
                   help="full run: skip the latency screen and test every selected node")
    g.add_argument("--top", dest="top_k", type=int, default=5, metavar="K",
                   help="--quick: probe the K best-screened nodes (default: 5)")
    g.add_argument("--favourites", type=int, default=2, metavar="N",
                   help="--quick: also probe the N best remembered nodes (default: 2)")
    g.add_argument("--remembered", type=int, default=0, metavar="N",
                   help="--quick: skip the latency screen and probe only the N best remembered "
                        "nodes (about a minute; falls back to the screen when there is no "
                        "history). This is what scheduled refreshes use")
    g.add_argument("--probe-url", default=None, metavar="URL",
                   help="download used by the built-in throughput probe "
                        "(default: Cloudflare's speed-test endpoint)")
    g.add_argument("--probe-host", default="1.1.1.1", metavar="HOST",
                   help="host whose TCP handshake time is the latency sample (default: 1.1.1.1)")
    g.add_argument("--probe-seconds", dest="probe_cap_s", type=float, default=6.0, metavar="SEC",
                   help="time cap for one throughput probe (default: 6)")
    g.add_argument("--apply", action="store_true",
                   help="after the run, switch to the best remembered node if it is clearly "
                        "better than the current one (same rules as `apply --if-better`)")

    g = p.add_argument_group("benchmark")
    g.add_argument("--reps", type=int, default=None, metavar="N",
                   help="repetitions per node (default: 3 for a full run, 1 for --quick)")
    g.add_argument("--tester", choices=sorted(TESTERS), default="ookla",
                   help="speed-test implementation (default: ookla)")
    g.add_argument("--tester-path", default=None, metavar="PATH",
                   help="explicit path to the speed-test binary")
    g.add_argument("--tailscale-path", default=None, metavar="PATH",
                   help="explicit path to the tailscale binary")
    g.add_argument("--accept-ookla-license", action="store_true",
                   help="confirm you have accepted Ookla's licence/privacy terms, so the "
                        "CLI can run non-interactively (or set TSBENCH_ACCEPT_OOKLA_LICENSE=1)")
    g.add_argument("--server-id", type=int, default=None, metavar="ID",
                   help="pin every test to this speed-test server id")
    g.add_argument("--no-pin-server", dest="pin_server", action="store_false",
                   help="let each test pick its own server (default: pin to the first "
                        "server used, so nodes are compared against the same target)")
    g.add_argument("--baseline", dest="baseline", action="store_true", default=True,
                   help="measure a no-exit-node DIRECT reference first (default: on)")
    g.add_argument("--no-baseline", dest="baseline", action="store_false",
                   help="skip the DIRECT reference measurement")
    g.add_argument("--switch-delay", type=float, default=None, metavar="SEC",
                   help="settle time after finishing a node (default: 3, or 1 for "
                        "--quick/--screen-only)")
    g.add_argument("--rep-delay", type=float, default=1.0, metavar="SEC",
                   help="pause between repetitions on one node (default: 1)")
    g.add_argument("--ready-timeout", type=float, default=30.0, metavar="SEC",
                   help="how long to wait for a node to become usable (default: 30)")
    g.add_argument("--test-timeout", type=float, default=180.0, metavar="SEC",
                   help="per-speed-test timeout (default: 180)")
    g.add_argument("--no-egress-check", action="store_true",
                   help="do not verify real egress via am.i.mullvad.net")

    g = p.add_argument_group("behaviour")
    g.add_argument("--dry-run", action="store_true",
                   help="discover and display matching nodes, then exit")
    g.add_argument("--fail-fast", action="store_true",
                   help="stop on the first node that cannot be activated "
                        "(default: record it as failed and continue)")
    g.add_argument("--apply-best", action="store_true",
                   help="finish connected to the top-ranked node instead of restoring "
                        "the original exit node")
    g.add_argument("--keep-raw", action="store_true",
                   help="save each raw speed-test JSON under <out-dir>/<run>/raw/ "
                        "(requires --out-dir)")

    g = p.add_argument_group("persistence and output")
    g.add_argument("--save", dest="save", action="store_true", default=True,
                   help="record this run in the history database (default)")
    g.add_argument("--no-save", dest="save", action="store_false",
                   help="do not record this run in the history database")
    g.add_argument("--db", dest="db_path", default=None, metavar="PATH",
                   help=f"history database (default: {default_db_path()}; "
                        f"honours $TSBENCH_HOME)")
    g.add_argument("--out-dir", default=None, metavar="DIR",
                   help="also export JSON/CSV/Markdown for this run under DIR/<run-name>/")
    g.add_argument("--run-name", default=None, metavar="NAME",
                   help="export directory name (default: run-YYYYmmdd-HHMMSS)")
    g.add_argument("--force", action="store_true",
                   help="allow overwriting an existing export directory")
    g.add_argument("--log-file", default=None, metavar="PATH",
                   help="append a plain-text log here")
    g.add_argument("--colour", dest="colour", action="store_true", default=None,
                   help="force colour on")
    g.add_argument("--no-colour", dest="colour", action="store_false",
                   help="force colour off")
    g.add_argument("--quiet", action="store_true", help="only print the final report")
    g.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    return p


def build_history_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{PROG} history",
                                description="Inspect or trim the saved run history.")
    # --db lives on each subcommand so `history list --db X` works as people expect.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", dest="db_path", default=None, metavar="PATH",
                        help=f"history database (default: {default_db_path()})")
    sub = p.add_subparsers(dest="action", required=True)
    ls = sub.add_parser("list", parents=[common], help="list recent runs")
    ls.add_argument("-n", "--limit", type=int, default=20, help="how many runs (default: 20)")
    pr = sub.add_parser("prune", parents=[common], help="delete runs older than a number of days")
    pr.add_argument("--older-than", type=float, required=True, metavar="DAYS")
    return p


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def build_meta(s: Settings, tools: Tools, bench: Benchmark, results: List[NodeResult],
               started_at: str, duration: float, tier: str = "full",
               tester: Any = None, reps: Optional[int] = None) -> Dict[str, Any]:
    tester = tester or tools.tester
    return {
        "started_at": started_at, "duration_s": duration, "tier": tier,
        "include_cities": s.cities, "exclude_cities": s.exclude_cities,
        "country": s.country, "reps": reps or s.reps_n,
        "min_success": min_success_for(reps or s.reps_n),
        "tester": tester.name, "units_note": tester.units_note,
        "pinned_server": bench.pinned_server,
        "tool_version": __version__,
        "tailscale_version": tools.ts_version,
        "speedtest_version": tools.tester_version,
        "nodes_benchmarked": len([r for r in results if not r.node.is_baseline]),
        "baseline_included": bool(s.baseline),
        "egress_verification": not s.no_egress_check,
        "network_tag": bench.network_tag,
        "original_exit_node": bench.original_label,
        "restored_to": bench.restored_to,
        "interrupted": bench.interrupted,
    }


def print_report(results: List[NodeResult], recommended: List[NodeResult], s: Settings,
                 term: Term, log: Log) -> None:
    ranked = ranked_ok(results)
    log.say()
    log.say(term.bold("=" * min(term.width - 1, 108)))
    log.say(term.bold("RANKED RESULTS") + term.dim(
        "   down/up Mbps, lat/jit ms, loss %, score 0-100"))
    log.say(term.bold("=" * min(term.width - 1, 108)))
    log.say()
    print(render_table(results, s.reps_n, term))
    log.say()

    base = next((r for r in results if r.node.is_baseline and r.status == "ok"), None)
    if base:
        log.say(f"{term.bold('Baseline (no exit node)')}: "
                f"{fmt(base.med_down)} Mbps down / {fmt(base.med_up)} Mbps up / "
                f"{fmt(base.med_lat)} ms latency")
        if recommended and base.med_down and recommended[0].med_down:
            pct = 100.0 * recommended[0].med_down / base.med_down
            dl = (recommended[0].med_lat or 0) - (base.med_lat or 0)
            log.say(term.dim(f"  best exit node keeps {pct:.0f}% of baseline download "
                             f"and adds {dl:+.0f} ms latency"))
        log.say()

    log.say(term.bold(f"RECOMMENDED (top {len(recommended)}, best first)"))
    if recommended:
        for i, r in enumerate(recommended, 1):
            log.say(f"  {term.green(str(i) + '.')} "
                    f"{term.bold(r.node.dns_name or r.node.hostname)}   "
                    f"score {term.bold(fmt(r.score))}   "
                    f"{fmt(r.med_down)}/{fmt(r.med_up)} Mbps   "
                    f"{fmt(r.med_lat)} ms   {r.successes}/{s.reps_n} runs ok")
            log.say(term.dim(f"      tailscale set --exit-node={r.node.ip}"))
    else:
        log.say(f"  {term.yellow('none')} - no node produced enough valid data "
                f"(need {s.min_success} successful runs). Marked inconclusive rather "
                f"than ranked.")
    log.say()

    counts = {
        "recommended": len(recommended),
        "tested, not recommended": len(ranked) - len(recommended),
        "inconclusive": len([r for r in results if r.status == "inconclusive"]),
        "failed": len([r for r in results if r.status == "failed"]),
    }
    log.say(term.bold("Outcome") + "  " + "   ".join(
        f"{k}: {term.bold(str(v))}" for k, v in counts.items()))
    log.say()


def print_screen_report(results: List[NodeResult], ranked_screen: List[NodeResult],
                        dropped: List[NodeResult], s: Settings, term: Term, log: Log) -> None:
    log.say()
    log.say(term.bold("=" * min(term.width - 1, 108)))
    log.say(term.bold("LATENCY SCREEN") + term.dim(
        "   lat/jit ms, loss % (failed TCP handshakes); no throughput measured"))
    log.say(term.bold("=" * min(term.width - 1, 108)))
    log.say()
    print(render_table(results, s.screen_samples, term))
    log.say()
    log.say(term.bold("Outcome") + f"  responsive: {term.bold(str(len(ranked_screen)))}   "
            f"dropped as latency outliers: {term.bold(str(len(dropped)))}   "
            f"failed: {term.bold(str(len(failed_to_screen(results))))}")
    log.say(term.dim("  Latency only loosely predicts throughput; this is a filter, "
                     "not a ranking of winners."))
    log.say()


def load_favourites(s: Settings, nodes: List[Node]) -> List[str]:
    """Hostnames of the best remembered nodes among `nodes` (best first)."""
    if s.favourites <= 0 or not os.path.exists(s.resolved_db_path):
        return []
    try:
        with Store(s.resolved_db_path) as store:
            rows = store.observations(window_start(14), store.latest_network_tag())
    except (sqlite3.Error, RuntimeError, OSError):
        return []
    est = compute_estimates(rows)
    return [e.hostname for e in ranked(est, allowed=[n.hostname for n in nodes])[:s.favourites]]


def save_run(s: Settings, log: Log, meta: Dict[str, Any], results: List[NodeResult],
             tier: str) -> Optional[int]:
    if not s.save:
        return None
    try:
        with Store(s.resolved_db_path) as store:
            return store.save_run(meta, results, tier=tier)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        log.warn(f"could not save run to {s.resolved_db_path}: {exc}")
        return None


def estimate_minutes(s: Settings, targets: int, tier: Optional[str] = None) -> float:
    tier = tier or s.tier
    if tier == "full":
        return (targets * s.reps_n * 25 + targets * s.switch_delay) / 60.0
    per_node = 4.0 + s.switch_delay + (0 if tier == "screen" else s.probe_cap_s * s.reps_n)
    return targets * per_node / 60.0


def run_main(argv: List[str], locked: bool = False) -> int:
    """Run the benchmark.  `locked` means the caller already holds the run lock."""
    args = build_parser().parse_args(argv)
    s = Settings.from_args(args)
    term = Term(force_colour=s.colour)
    log = Log(term, s.log_file, s.quiet)

    if s.reps_n < 1:
        log.err("--reps must be >= 1")
        return 2
    if s.keep_raw and not s.out_dir:
        log.err("--keep-raw needs --out-dir (raw files are written next to the export)")
        return 2
    if s.quick and s.screen_only:
        log.err("--quick and --screen-only are different tiers; pick one")
        return 2
    if s.top_k < 1:
        log.err("--top must be >= 1")
        return 2
    if s.remembered < 0 or (s.remembered and not s.quick):
        log.err("--remembered needs --quick and a number >= 1")
        return 2

    lock: Optional[RunLock] = None
    if not s.dry_run and not locked:
        lock = RunLock("run")
        try:
            lock.acquire()
        except LockBusy as busy:
            log.err(f"{describe_holder(busy.holder)} is already changing the exit node; "
                    f"try again when it has finished.")
            log.close()
            return EXIT_LOCKED

    bench: Optional[Benchmark] = None
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tier = s.tier
    use_screen = tier != "full" or s.screen

    def on_signal(signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name
        term.endlive()
        print(f"\n  {term.yellow('!')} {name} received - stopping after the current "
              f"step and restoring Tailscale state.", file=sys.stderr)
        if bench is not None:
            bench.interrupted = True
        raise KeyboardInterrupt(name)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        log.say(term.bold(f"{PROG} {__version__}") + term.dim(
            f"  - Tailscale exit-node benchmark  [{tier}]"))
        log.say()
        tools = preflight(s, term, log)
        nodes = select_nodes(tools.ts, s, term, log, limit=not use_screen)
        remembered: Optional[List[Node]] = None
        if tier == "quick" and s.remembered:
            favs = load_favourites(replace(s, favourites=s.remembered), nodes)
            if favs:
                by_host = {n.hostname: n for n in nodes}
                remembered = [by_host[h] for h in favs]
                use_screen = False
            else:
                log.info("no remembered nodes yet; screening instead")

        if s.dry_run:
            log.say()
            what = {"screen": "screened (latency only)",
                    "quick": "screened, then the best few probed",
                    "full": "benchmarked" + (" after a latency screen" if use_screen else "")}[tier]
            log.ok(f"--dry-run: {len(nodes)} node(s) would be {what}"
                   + (", plus a DIRECT baseline" if s.baseline else "")
                   + ". Nothing was changed.")
            return 0

        run_dir: Optional[str] = None
        if s.out_dir:
            name = s.run_name or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            run_dir = os.path.join(os.path.abspath(s.out_dir), name)
            if os.path.exists(run_dir) and not s.force:
                raise Fatal(f"export directory already exists: {run_dir}\n"
                            "        Pass --run-name for a different name, or --force "
                            "to overwrite.")

        bench = Benchmark(s, tools.ts, tools.tester, term, log, run_dir)
        probe = tools.tester if getattr(tools.tester, "native", False) \
            else NativeProbe.from_settings(s)
        log.say()
        log.say(term.bold("Tailscale state"))
        bench.capture_original()

        where = s.resolved_db_path if s.save else "(not saving: --no-save)"
        log.say(term.dim(f"  history -> {where}"))
        if run_dir:
            log.say(term.dim(f"  export  -> {run_dir}"))

        # -- 1. latency screen -------------------------------------------------
        screened: List[NodeResult] = []
        kept: List[NodeResult] = []
        if use_screen:
            queue = ([baseline_node()] if s.baseline else []) + list(nodes)
            log.say()
            log.say(term.bold("Latency screen") + term.dim(
                f"  {len(queue)} target(s) x {s.screen_samples} TCP handshakes, "
                f"rough estimate {estimate_minutes(s, len(queue), 'screen'):.0f} min"))
            log.say()
            s_start = time.monotonic()
            with bench.using(probe):
                screened = bench.run_all(queue, reps=1, throughput=False)
            ranked_screen = rank_screen(screened)
            for i, r in enumerate(ranked_screen, 1):
                r.rank = i
            kept, dropped = cut_outliers(ranked_screen)
            for r in dropped:
                r.status = "failed"
                r.connect_error = (f"dropped by the screen: {r.med_lat:.0f} ms is far above "
                                   f"the other nodes")
                r.rank = None
            sid = save_run(s, log, build_meta(
                s, tools, bench, screened, started_at, round(time.monotonic() - s_start, 1),
                tier="screen", tester=probe, reps=1), screened, "screen") \
                if not bench.interrupted else None
            if tier == "screen":
                bench.restore()
                print_screen_report(screened, kept, dropped, s, term, log)
                if sid is not None:
                    log.say(term.bold("Saved") + f"  history run #{sid} in {s.resolved_db_path}")
                log.say(term.bold("Tailscale state: ") + f"exit node is now {bench.restored_to}")
                return 0
            if not kept and not bench.interrupted:
                raise Fatal("no node answered the latency screen; nothing to measure further.\n"
                            "        Check your connection, or run with --no-screen.")
            if dropped:
                log.info(f"dropped {len(dropped)} latency outlier(s): "
                         + ", ".join(r.node.hostname for r in dropped))

        # -- 2. throughput -----------------------------------------------------
        favs = []
        if tier == "quick":
            if remembered is not None:
                favs = [n.hostname for n in remembered]
                targets = list(remembered)
            else:
                favs = load_favourites(s, [r.node for r in kept])
                targets = pick_candidates(kept, favs, s.top_k)
            tester: Any = probe
        else:
            targets = [r.node for r in kept] if use_screen else list(nodes)
            if use_screen and s.max_nodes is not None and len(targets) > s.max_nodes:
                log.info(f"keeping the {s.max_nodes} best-screened of {len(targets)} nodes "
                         f"(--max-nodes)")
                targets = targets[:s.max_nodes]
            tester = tools.tester
        queue = ([baseline_node()] if s.baseline else []) + targets
        reps = s.reps_n
        log.say()
        log.say(term.bold("Benchmark") + term.dim(
            f"  {len(queue)} target(s) x {reps} rep(s), sequential, rough estimate "
            f"{estimate_minutes(s, len(queue)):.0f} min"))
        if tier == "quick":
            log.say(term.dim("  probing: " + ", ".join(n.hostname for n in targets)
                             + (f"  (favourites: {', '.join(favs)})" if favs else "")))
        log.say()

        p_start = time.monotonic()
        with bench.using(tester):
            results = bench.run_all(queue, reps=reps, throughput=True)
        score_nodes(results)

        ranked_ok_ = ranked_ok(results)
        recommended = ranked_ok_[:TOP_N]
        for r in recommended:
            r.recommended = True

        # -- restore or apply ----------------------------------------------------
        if s.apply_best and recommended:
            bench.restore(to_ip=recommended[0].node.ip, applying=True,
                          why="--apply-best: connecting to the top-ranked node")
        else:
            if s.apply_best:
                log.warn("--apply-best requested but no node was conclusive; "
                         "restoring the original state instead.")
            bench.restore()

        # -- persist ---------------------------------------------------------
        duration = round(time.monotonic() - p_start, 1)
        meta = build_meta(s, tools, bench, results, started_at, duration, tier=tier,
                          tester=tester, reps=reps)
        paths: Dict[str, str] = {}
        if run_dir:
            paths = write_outputs(run_dir, results, meta, recommended)
        run_id = save_run(s, log, meta, results, tier)

        # -- report ----------------------------------------------------------
        print_report(results, recommended, s, term, log)
        if paths or run_id is not None:
            log.say(term.bold("Saved"))
            if run_id is not None:
                log.say(f"  history   run #{run_id} in {s.resolved_db_path}")
            for k in ("json", "csv", "runs_csv", "markdown"):
                if k in paths:
                    log.say(f"  {k:<9} {paths[k]}")
            if s.keep_raw and run_dir:
                log.say(f"  {'raw':<9} {os.path.join(run_dir, 'raw')}/")
            log.say()
        log.say(term.bold("Tailscale state: ") + f"exit node is now {bench.restored_to}")
        if bench.interrupted:
            log.warn("run was interrupted; results cover only the nodes completed above.")
        elif s.apply and s.save:
            from tsbench.apply import run_apply
            d, _ = run_apply(tools.ts, s, if_better=True)
            log.say(term.bold("Apply: ") + f"{d.action} - {d.reason}")
        return 0

    except KeyboardInterrupt:
        if bench is not None:
            bench.restore(why="interrupted")
            log.say()
            log.say(term.bold("Tailscale state: ") + f"exit node is now {bench.restored_to}")
        else:
            log.say("\n  Interrupted before any change was made.")
        return 130
    except Fatal as exc:
        log.err(str(exc))
        if bench is not None and not bench.restored:
            bench.restore(why="aborting after an error")
            log.say(term.bold("Tailscale state: ") + f"exit node is now {bench.restored_to}")
        return 1
    finally:
        if bench is not None and not bench.restored:
            bench.restore(why="cleanup")
        log.close()
        if lock is not None:
            lock.release()


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #


def history_main(argv: List[str]) -> int:
    args = build_history_parser().parse_args(argv)
    path = args.db_path or Settings().resolved_db_path
    if args.action == "list" and not os.path.exists(path):
        print(f"No history yet ({path}). Run `{PROG}` to create some.")
        return 0
    with Store(path) as store:
        if args.action == "prune":
            n = store.prune(args.older_than)
            print(f"Deleted {n} run(s) older than {args.older_than:g} day(s).")
            return 0
        rows = store.list_runs(args.limit)
    if not rows:
        print("No runs recorded.")
        return 0
    print(f"{'ID':>4}  {'Started (UTC)':<20}  {'Tier':<6}  {'Nodes':>5}  {'Dur':>6}  Applied node")
    for r in rows:
        dur = f"{r['duration_s']:.0f}s" if r["duration_s"] is not None else "-"
        print(f"{r['id']:>4}  {r['started_at'][:19].replace('T', ' '):<20}  {r['tier']:<6}  "
              f"{r['nodes']:>5}  {dur:>6}  {r['applied_node'] or '-'}")
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "history":
        return history_main(args[1:])
    if args and args[0] == "status":
        from tsbench.status import status_main
        return status_main(args[1:])
    if args and args[0] == "apply":
        from tsbench.apply import apply_main
        return apply_main(args[1:])
    if args and args[0] == "auto":
        from tsbench.auto import auto_main
        return auto_main(args[1:])
    if args and args[0] == "schedule":
        from tsbench.schedule import schedule_main
        return schedule_main(args[1:])
    if args and args[0] == "run":
        args = args[1:]
    return run_main(args)


if __name__ == "__main__":
    sys.exit(main())
