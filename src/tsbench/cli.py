"""Command-line interface.

    tsbench [run] [flags]      benchmark exit nodes (bare `tsbench` is `run`)
    tsbench history list|prune inspect or trim the saved history
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tsbench import PROG, __version__
from tsbench.bench import Benchmark
from tsbench.console import Log, Term, fmt
from tsbench.discovery import select_nodes
from tsbench.errors import Fatal
from tsbench.models import NodeResult, baseline_node, ranked_ok
from tsbench.preflight import Tools, preflight
from tsbench.probes import TESTERS
from tsbench.report import render_table, write_outputs
from tsbench.scoring import TOP_N, score_nodes
from tsbench.settings import DEFAULT_COUNTRY, Settings, default_db_path
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

    g = p.add_argument_group("benchmark")
    g.add_argument("--reps", type=int, default=3, metavar="N",
                   help="speed-test repetitions per node (default: 3)")
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
    g.add_argument("--switch-delay", type=float, default=3.0, metavar="SEC",
                   help="settle time after finishing a node (default: 3)")
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
               started_at: str, duration: float) -> Dict[str, Any]:
    return {
        "started_at": started_at, "duration_s": duration,
        "include_cities": s.cities, "exclude_cities": s.exclude_cities,
        "country": s.country, "reps": s.reps, "min_success": s.min_success,
        "tester": tools.tester.name, "units_note": tools.tester.units_note,
        "pinned_server": bench.pinned_server,
        "tool_version": __version__,
        "tailscale_version": tools.ts_version,
        "speedtest_version": tools.tester_version,
        "nodes_benchmarked": len([r for r in results if not r.node.is_baseline]),
        "baseline_included": bool(s.baseline),
        "egress_verification": not s.no_egress_check,
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
    print(render_table(results, s.reps, term))
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
                    f"score {term.bold(f'{r.score:.1f}')}   "
                    f"{fmt(r.med_down)}/{fmt(r.med_up)} Mbps   "
                    f"{fmt(r.med_lat)} ms   {r.successes}/{s.reps} runs ok")
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


def run_main(argv: List[str]) -> int:
    args = build_parser().parse_args(argv)
    s = Settings.from_args(args)
    term = Term(force_colour=s.colour)
    log = Log(term, s.log_file, s.quiet)

    if s.reps < 1:
        log.err("--reps must be >= 1")
        return 2
    if s.keep_raw and not s.out_dir:
        log.err("--keep-raw needs --out-dir (raw files are written next to the export)")
        return 2

    bench: Optional[Benchmark] = None
    t_start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

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
        log.say(term.bold(f"{PROG} {__version__}") + term.dim("  - Tailscale exit-node benchmark"))
        log.say()
        tools = preflight(s, term, log)
        nodes = select_nodes(tools.ts, s, term, log)

        if s.dry_run:
            log.say()
            log.ok(f"--dry-run: {len(nodes)} node(s) would be benchmarked "
                   f"({s.reps} rep(s) each"
                   + (", plus a DIRECT baseline" if s.baseline else "")
                   + "). Nothing was changed.")
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
        log.say()
        log.say(term.bold("Tailscale state"))
        bench.capture_original()

        queue = list(nodes)
        if s.baseline:
            queue.insert(0, baseline_node())

        est = len(queue) * s.reps * 25 + len(queue) * s.switch_delay
        log.say()
        log.say(term.bold("Benchmark") + term.dim(
            f"  {len(queue)} target(s) x {s.reps} rep(s), sequential, "
            f"rough estimate {est / 60:.0f} min"))
        where = s.resolved_db_path if s.save else "(not saving: --no-save)"
        log.say(term.dim(f"  history -> {where}"))
        if run_dir:
            log.say(term.dim(f"  export  -> {run_dir}"))
        log.say()

        results = bench.run_all(queue)
        score_nodes(results)

        ranked = ranked_ok(results)
        recommended = ranked[:TOP_N]
        for r in recommended:
            r.recommended = True

        # -- restore or apply ------------------------------------------------
        if s.apply_best and recommended:
            bench.restore(to_ip=recommended[0].node.ip, applying=True,
                          why="--apply-best: connecting to the top-ranked node")
        else:
            if s.apply_best:
                log.warn("--apply-best requested but no node was conclusive; "
                         "restoring the original state instead.")
            bench.restore()

        # -- persist ---------------------------------------------------------
        duration = round(time.monotonic() - t_start, 1)
        meta = build_meta(s, tools, bench, results, started_at, duration)
        paths: Dict[str, str] = {}
        if run_dir:
            paths = write_outputs(run_dir, results, meta, recommended)
        run_id: Optional[int] = None
        if s.save:
            try:
                with Store(s.resolved_db_path) as store:
                    run_id = store.save_run(meta, results, tier="full")
            except (sqlite3.Error, OSError, RuntimeError) as exc:
                log.warn(f"could not save run to {s.resolved_db_path}: {exc}")

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
    if args and args[0] == "run":
        args = args[1:]
    return run_main(args)


if __name__ == "__main__":
    sys.exit(main())
