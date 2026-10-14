import json

import pytest

from conftest import FakeTailscale
from tsbench import bench as bench_mod
from tsbench.bench import Benchmark
from tsbench.console import Log, Term
from tsbench.errors import Fatal
from tsbench.models import baseline_node
from tsbench.probes import OoklaTester
from tsbench.settings import Settings

OOKLA_JSON = json.dumps({
    "type": "result", "ping": {"latency": 20.0, "jitter": 1.0},
    "download": {"bandwidth": 12_500_000}, "upload": {"bandwidth": 6_250_000},
    "server": {"id": 777, "name": "Example", "location": "London"},
})


class StatefulTailscale(FakeTailscale):
    """Tracks the active exit node like the real thing: set_exit_node changes what
    current_exit_ip reports."""

    def __init__(self, status, fail_for=()):
        super().__init__(status)
        self.current = None
        self.fail_for = set(fail_for)

    def current_exit_ip(self):
        return self.current

    def set_exit_node(self, target):
        self.calls.append(target)
        if target in self.fail_for:
            return False, "boom"
        self.current = target or None
        return True, ""


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(bench_mod.time, "sleep", lambda _s: None)


@pytest.fixture
def speed(monkeypatch):
    """Replace the speed-test subprocess; records how it was called."""
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return 0, OOKLA_JSON, ""

    monkeypatch.setattr(bench_mod, "run", fake_run)
    return calls


def make(ts, **settings):
    s = Settings(reps=2, switch_delay=0, rep_delay=0, ready_timeout=0.2, **settings)
    log = Log(Term(force_colour=False), None, quiet=True)
    return Benchmark(s, ts, OoklaTester("speedtest", accept_license=True),
                     Term(force_colour=False), log, run_dir=None)


def three_nodes(ts):
    return [n for n in ts.discover() if n.city == "London" and n.online][:3]


def egress_follows_exit(monkeypatch, ts):
    """Make the egress probe report whichever node is currently active."""
    def probe(_timeout):
        host = next((n.hostname for n in ts.discover() if n.ip == ts.current), None)
        return host, "198.51.100.1", True
    monkeypatch.setattr(bench_mod, "probe_egress", probe)


def test_happy_path_measures_every_node_and_the_baseline(status_uk, speed, monkeypatch):
    ts = StatefulTailscale(status_uk)
    egress_follows_exit(monkeypatch, ts)
    b = make(ts)
    results = b.run_all([baseline_node(), *three_nodes(ts)])

    assert [r.status for r in results] == ["ok"] * 4
    assert results[0].node.is_baseline and "" in ts.calls       # baseline cleared the exit node
    assert all(r.verified for r in results[1:])                 # egress matched the node
    assert all(len(r.runs) == 2 for r in results)
    assert results[1].med_down == 100.0 and results[1].med_lat == 20.0


def test_server_is_pinned_after_the_first_success(status_uk, speed, monkeypatch):
    ts = StatefulTailscale(status_uk)
    egress_follows_exit(monkeypatch, ts)
    b = make(ts)
    b.run_all(three_nodes(ts)[:2])
    assert not any(a.startswith("--server-id") for a in speed[0])   # first test: free choice
    assert all("--server-id=777" in cmd for cmd in speed[1:])       # then pinned
    assert b.pinned_server == 777


def test_pinning_can_be_disabled(status_uk, speed, monkeypatch):
    ts = StatefulTailscale(status_uk)
    egress_follows_exit(monkeypatch, ts)
    b = make(ts, pin_server=False)
    b.run_all(three_nodes(ts)[:2])
    assert not any(a.startswith("--server-id") for cmd in speed for a in cmd)


def test_unactivatable_node_is_recorded_and_the_run_continues(status_uk, speed, monkeypatch):
    ts = StatefulTailscale(status_uk)
    nodes = three_nodes(ts)
    ts.fail_for = {nodes[1].ip}
    egress_follows_exit(monkeypatch, ts)
    results = make(ts).run_all(nodes)

    assert [r.status for r in results] == ["ok", "failed", "ok"]
    assert "tailscale set failed" in results[1].connect_error
    assert results[1].runs == []


def test_fail_fast_aborts_on_the_first_unactivatable_node(status_uk, speed, monkeypatch):
    ts = StatefulTailscale(status_uk)
    nodes = three_nodes(ts)
    ts.fail_for = {nodes[0].ip}
    with pytest.raises(Fatal, match="--fail-fast"):
        make(ts, fail_fast=True).run_all(nodes)


def test_wrong_egress_is_not_accepted_as_connected(status_uk, speed, monkeypatch):
    ts = StatefulTailscale(status_uk)
    monkeypatch.setattr(bench_mod, "probe_egress",
                        lambda _t: ("gb-lon-wg-999", "198.51.100.1", True))
    results = make(ts).run_all(three_nodes(ts)[:1])
    assert results[0].status == "failed"
    assert "expected" in results[0].connect_error and "gb-lon-wg-999" in results[0].connect_error


def test_unattributable_egress_is_usable_but_unverified(status_uk, speed, monkeypatch):
    ts = StatefulTailscale(status_uk)
    monkeypatch.setattr(bench_mod, "probe_egress", lambda _t: (None, None, True))
    results = make(ts).run_all(three_nodes(ts)[:1])
    assert results[0].status == "ok" and results[0].verified is False


def test_failing_speed_tests_make_the_node_failed(status_uk, monkeypatch):
    ts = StatefulTailscale(status_uk)
    egress_follows_exit(monkeypatch, ts)
    monkeypatch.setattr(bench_mod, "run", lambda cmd, timeout=None: (1, "", "network down"))
    results = make(ts).run_all(three_nodes(ts)[:1])
    assert results[0].status == "failed"
    assert all(not r.ok and "network down" in r.error for r in results[0].runs)


def test_unparseable_output_is_a_failed_run_not_a_crash(status_uk, monkeypatch):
    ts = StatefulTailscale(status_uk)
    egress_follows_exit(monkeypatch, ts)
    monkeypatch.setattr(bench_mod, "run", lambda cmd, timeout=None: (0, "<html>", ""))
    results = make(ts).run_all(three_nodes(ts)[:1])
    assert results[0].status == "failed"
    assert "could not parse" in results[0].runs[0].error


def test_interrupt_flag_stops_the_loop(status_uk, speed, monkeypatch):
    ts = StatefulTailscale(status_uk)
    egress_follows_exit(monkeypatch, ts)
    b = make(ts)
    b.interrupted = True
    assert b.run_all(three_nodes(ts)) == []


def test_restore_returns_to_the_original_node_and_is_idempotent(status_uk):
    ts = StatefulTailscale(status_uk)
    original = three_nodes(ts)[0]
    ts.current = original.ip
    b = make(ts)
    b.capture_original()
    ts.current = three_nodes(ts)[1].ip          # a benchmark left us on another node

    b.restore()
    assert ts.current == original.ip and original.hostname in b.restored_to
    calls = len(ts.calls)
    b.restore()                                  # second call must do nothing
    assert len(ts.calls) == calls


def test_restore_to_direct_when_there_was_no_exit_node(status_uk):
    ts = StatefulTailscale(status_uk)            # starts with no exit node
    b = make(ts)
    b.capture_original()
    ts.current = three_nodes(ts)[0].ip
    b.restore()
    assert ts.current is None and "direct" in b.restored_to


def test_apply_connects_to_the_chosen_node_and_says_so(status_uk, capsys):
    ts = StatefulTailscale(status_uk)
    b = Benchmark(Settings(), ts, OoklaTester("speedtest"), Term(force_colour=False),
                  Log(Term(force_colour=False), None, quiet=False), run_dir=None)
    target = three_nodes(ts)[2]
    b.capture_original()
    b.restore(to_ip=target.ip, applying=True, why="test")
    out = capsys.readouterr().out
    assert ts.current == target.ip and target.hostname in b.restored_to
    assert "Applying" in out and "Restoring" not in out


def test_failed_restore_is_reported_not_swallowed(status_uk):
    ts = StatefulTailscale(status_uk)
    b = make(ts)
    b.capture_original()
    ts.fail_for = {""}                           # clearing the exit node will fail
    b.restore()
    assert b.restored_to.startswith("FAILED")
