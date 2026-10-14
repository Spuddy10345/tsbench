"""The benchmark engine: switch to each node, measure, restore."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, List, Optional, Tuple

from tsbench.console import Log, Term, fmt
from tsbench.errors import Fatal
from tsbench.models import Node, NodeResult, RunResult
from tsbench.probes import SpeedTester, probe_egress, probe_network_tag
from tsbench.proc import run
from tsbench.settings import Settings, min_success_for
from tsbench.tailscale import Tailscale


class Benchmark:
    def __init__(self, settings: Settings, ts: Tailscale, tester: SpeedTester,
                 term: Term, log: Log, run_dir: Optional[str]) -> None:
        self.a = settings
        self.ts = ts
        self.tester = tester
        self.term = term
        self.log = log
        self.run_dir = run_dir
        self.original_ip: Optional[str] = None
        self.original_label = "unknown"
        self.restored = False
        self.restored_to = "unknown"
        self.pinned_server: Optional[int] = settings.server_id
        self.interrupted = False
        self.network_tag: Optional[str] = None

    @contextmanager
    def using(self, tester: Any) -> Iterator[None]:
        """Temporarily measure with a different tester (the screen and quick tiers
        use the built-in probe even when a full run would use Ookla)."""
        previous = self.tester
        self.tester = tester
        try:
            yield
        finally:
            self.tester = previous

    # -- state handling ----------------------------------------------------- #

    def capture_original(self) -> None:
        self.original_ip = self.ts.current_exit_ip()
        self.original_label = (f"{self.ts.hostname_for_ip(self.original_ip)} "
                               f"({self.original_ip})") if self.original_ip \
            else "none (direct connectivity)"
        self.log.info(f"Original exit node: {self.term.bold(self.original_label)}")

    def restore(self, to_ip: Optional[str] = None, why: str = "",
                applying: bool = False) -> None:
        """Idempotent.  Called from the finally block and from signal handlers.

        With `applying=True` the target is a deliberately chosen node rather
        than the original state, and the messages say so."""
        if self.restored:
            return
        target_ip = self.original_ip if to_ip is None else to_ip
        target = target_ip or ""
        desc = (f"{self.ts.hostname_for_ip(target_ip)} ({target_ip})"
                if target_ip else "none (direct connectivity)")
        verb = "Applying" if applying else "Restoring"
        self.term.endlive()
        self.log.say(f"\n{self.term.blue('->')} {verb} exit node {self.term.bold(desc)}"
                     + (f" {self.term.dim('(' + why + ')')}" if why else ""))
        ok, err = self.ts.set_exit_node(target)
        self.restored = True
        if ok:
            time.sleep(0.5)
            now = self.ts.current_exit_ip()
            if (now or None) == (target_ip or None):
                self.restored_to = desc
                self.log.ok(f"Tailscale exit node is now: {desc}")
            else:
                self.restored_to = f"UNEXPECTED: {now or 'none'} (wanted {desc})"
                self.log.warn(f"After {verb.lower()}, active exit node is {now or 'none'}, "
                              f"expected {desc}. Check `tailscale status`.")
        else:
            self.restored_to = f"FAILED to restore to {desc}"
            self.log.err(f"Could not set exit node: {err}")
            self.log.err(f"Fix manually with: tailscale set --exit-node={target or ''}")

    # -- connection --------------------------------------------------------- #

    def connect(self, node: Node, idx: int, total: int) -> Tuple[bool, bool, Optional[str]]:
        """Returns (connected, egress_verified, error)."""
        prefix = f"[{idx}/{total}] {node.label}"
        if node.is_baseline:
            self.term.live(f"{prefix}  connecting: clearing exit node ...")
            ok, err = self.ts.set_exit_node("")
            if not ok:
                return False, False, f"could not clear exit node: {err}"
        else:
            self.term.live(f"{prefix}  connecting: tailscale set --exit-node={node.ip} ...")
            ok, err = self.ts.set_exit_node(node.ip)
            if not ok:
                return False, False, f"tailscale set failed: {err}"

        # 1) wait for Tailscale itself to report the intended state
        deadline = time.monotonic() + self.a.ready_timeout
        want = None if node.is_baseline else node.ip
        state_ok = False
        while time.monotonic() < deadline:
            try:
                cur = self.ts.current_exit_ip()
            except Fatal as exc:
                return False, False, str(exc)
            if (cur or None) == (want or None):
                state_ok = True
                break
            left = deadline - time.monotonic()
            self.term.live(f"{prefix}  waiting for Tailscale to confirm "
                           f"(now: {cur or 'none'}, {left:.0f}s left) ...")
            time.sleep(0.4)
        if not state_ok:
            return False, False, (f"Tailscale did not report the requested exit node "
                                  f"within {self.a.ready_timeout}s")

        if self.a.no_egress_check:
            return True, False, None

        # 2) wait until traffic actually flows, and confirm where it leaves
        verified = False
        last = "no response"
        while time.monotonic() < deadline:
            host, ip, internet = probe_egress(min(6.0, self.a.ready_timeout))
            if host:
                if node.is_baseline:
                    last = f"egress via Mullvad node {host} while expecting DIRECT"
                    self.term.live(f"{prefix}  {last} ...")
                    time.sleep(0.6)
                    continue
                if host == node.hostname:
                    return True, True, None
                last = f"egress is {host}, expected {node.hostname}"
                self.term.live(f"{prefix}  {last}, retrying ...")
                time.sleep(0.6)
                continue
            if internet:
                # Reachable but unattributable (non-Mullvad node, or the Mullvad
                # endpoint is down).  Usable, just not positively verified.
                return True, verified, None
            left = deadline - time.monotonic()
            self.term.live(f"{prefix}  waiting for usable connectivity ({left:.0f}s left) ...")
            time.sleep(0.8)
        return False, False, f"no usable connectivity within {self.a.ready_timeout}s ({last})"

    # -- speed test --------------------------------------------------------- #

    def speed_test(self, node: Node, rep: int, idx: int, total: int,
                   reps: Optional[int] = None, throughput: bool = True) -> RunResult:
        prefix = f"[{idx}/{total}] {node.label}  rep {rep}/{reps or self.a.reps_n}"
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        what = "latency screen" if not throughput else f"{self.tester.name} speed test"
        self.term.live(f"{prefix}  running {what} ...")
        res = RunResult(rep=rep, ok=False, started_at=started.isoformat(timespec="seconds"),
                        duration_s=0.0)

        raw = None
        if getattr(self.tester, "native", False):
            try:
                parsed = self.tester.measure(throughput=throughput)
            except ValueError as exc:
                res.duration_s = round(time.monotonic() - t0, 2)
                res.error = str(exc)
                return res
            res.duration_s = round(time.monotonic() - t0, 2)
        else:
            rc, out, errtxt = run(self.tester.command(self.pinned_server),
                                  timeout=self.a.test_timeout)
            res.duration_s = round(time.monotonic() - t0, 2)
            if rc != 0 and not out.strip():
                res.error = f"{self.tester.name} exited rc={rc}: {(errtxt or '').strip()[:300]}"
                return res
            try:
                parsed = self.tester.parse(out)
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                res.error = f"could not parse {self.tester.name} output: {exc}"
                return res
            raw = parsed.pop("raw", None)

        for k, v in parsed.items():
            setattr(res, k, v)
        if throughput:
            res.ok = res.download_mbps is not None
            if not res.ok:
                res.error = "speed test returned no download figure"
        else:
            res.ok = res.latency_ms is not None
            if not res.ok:
                res.error = "latency probe returned no figure"

        if res.ok and self.a.pin_server and self.pinned_server is None \
                and res.server_id is not None:
            self.pinned_server = res.server_id
            self.log.info(f"Pinned speed-test server for the rest of this run: "
                          f"{res.server} id={res.server_id}")

        if self.a.keep_raw and raw is not None and self.run_dir:
            raw_dir = os.path.join(self.run_dir, "raw")
            os.makedirs(raw_dir, exist_ok=True)
            fname = f"{node.hostname or 'direct'}-rep{rep}.json"
            path = os.path.join(raw_dir, fname)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, indent=2)
            res.raw_file = os.path.join("raw", fname)
        return res

    # -- main loop ---------------------------------------------------------- #

    def run_all(self, nodes: List[Node], reps: Optional[int] = None,
                throughput: bool = True) -> List[NodeResult]:
        """Measure each node in turn.  `reps` overrides the setting (screening
        and quick runs take one sample); `throughput=False` is the latency screen."""
        results: List[NodeResult] = []
        total = len(nodes)
        reps = reps or self.a.reps_n
        min_success = min_success_for(reps)

        for idx, node in enumerate(nodes, 1):
            if self.interrupted:
                break
            nr = NodeResult(node=node)
            results.append(nr)

            connected, verified, err = self.connect(node, idx, total)
            nr.connected, nr.verified, nr.connect_error = connected, verified, err
            if connected and node.is_baseline and self.network_tag is None \
                    and not self.a.no_egress_check:
                self.network_tag = probe_network_tag(5.0)

            if not connected:
                self.term.endlive()
                self.log.say(f"  {self.term.red('FAIL')} [{idx}/{total}] "
                             f"{self.term.bold(node.label)}: {err}")
                nr.aggregate(min_success, need_down=throughput)
                if self.a.fail_fast:
                    raise Fatal(f"--fail-fast: {node.label} could not be activated: {err}")
                continue

            tag = self.term.green("verified") if verified else self.term.yellow("unverified egress")
            self.log.say(f"  {self.term.green('UP')}   [{idx}/{total}] "
                         f"{self.term.bold(node.label)} active ({tag})")

            native = getattr(self.tester, "native", False)
            for rep in range(1, reps + 1):
                if self.interrupted:
                    break
                r = self.speed_test(node, rep, idx, total, reps, throughput)
                if r.ok and throughput and not native:
                    host, eip, _ = (None, None, True) if self.a.no_egress_check \
                        else probe_egress(4.0)
                    r.egress_host, r.egress_ip = host, eip
                nr.runs.append(r)
                self.term.endlive()
                if r.ok:
                    speeds = (f"{self.term.bold(fmt(r.download_mbps))} Mbps down / "
                              f"{fmt(r.upload_mbps)} Mbps up / ") if throughput else ""
                    self.log.say(
                        f"       rep {rep}/{reps}: {speeds}"
                        f"{fmt(r.latency_ms)} ms lat / {fmt(r.jitter_ms)} ms jit"
                        + (f" / {r.packet_loss_pct:.1f}% loss" if r.packet_loss_pct else "")
                        + self.term.dim(f"  [{r.duration_s}s]"))
                else:
                    self.log.say(f"       rep {rep}/{reps}: "
                                 f"{self.term.red('failed')} - {r.error}")
                if rep < reps:
                    time.sleep(max(0.0, self.a.rep_delay))

            nr.aggregate(min_success, need_down=throughput)
            if nr.status == "ok":
                speeds = (f"{fmt(nr.med_down)} / {fmt(nr.med_up)} Mbps, "
                          if throughput else "")
                self.log.say(f"       {self.term.blue('median')}: "
                             f"{speeds}{fmt(nr.med_lat)} ms, {nr.successes}/{len(nr.runs)} ok")
            else:
                self.log.say(f"       {self.term.yellow(nr.status)}: "
                             f"{nr.successes}/{len(nr.runs)} successful runs "
                             f"(need {min_success})")
            self.log.say()

            if idx < total and not self.interrupted:
                self.term.live(f"  settling {self.a.switch_delay}s before next node ...")
                time.sleep(max(0.0, self.a.switch_delay))
                self.term.endlive()

        return results
