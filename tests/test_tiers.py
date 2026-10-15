"""End-to-end runs of the screen / quick / full tiers with Tailscale and the
network faked out."""

import json
import sqlite3

import pytest

from conftest import FakeTailscale
from test_bench import OOKLA_JSON, StatefulTailscale, egress_follows_exit
from tsbench import bench as bench_mod
from tsbench.cli import main
from tsbench.preflight import Tools
from tsbench.probes import OoklaTester


class FakeProbe:
    """Stands in for NativeProbe: numbers depend on which node is active."""

    name = "probe"
    native = True
    units_note = "fake"

    def __init__(self, ts, slow_ips=(), dead_ips=()):
        self.ts, self.slow_ips, self.dead_ips = ts, set(slow_ips), set(dead_ips)
        self.calls = []                      # (active ip, throughput?)

    def measure(self, throughput=True):
        ip = self.ts.current
        self.calls.append((ip, throughput))
        last = int(ip.split(".")[-1]) if ip else 0
        if ip in self.dead_ips:
            raise ValueError("no TCP handshake succeeded")
        lat = 200.0 if ip in self.slow_ips else 10.0 + last * 0.5
        return {"latency_ms": lat, "jitter_ms": 1.0, "packet_loss_pct": 0.0,
                "upload_mbps": None, "server": "fake", "server_id": None,
                "download_mbps": (600.0 - last * 10) if throughput else None}

    def latency(self, samples=None):
        return self.measure(False)


@pytest.fixture
def env(status_uk, tmp_path, monkeypatch):
    ts = StatefulTailscale(status_uk)
    online = [n for n in ts.discover() if n.online and n.country_code == "GB"]
    ts.current = online[0].ip                        # the user starts on a UK node
    egress_follows_exit(monkeypatch, ts)
    monkeypatch.setattr(bench_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(bench_mod, "probe_network_tag", lambda _t: "home-isp")
    monkeypatch.setattr(bench_mod, "run", lambda cmd, timeout=None: (0, OOKLA_JSON, ""))
    db = str(tmp_path / "t.db")
    state = {"ts": ts, "db": db, "start": online[0].ip, "nodes": online, "tester": None}

    def install(probe, tester=None):
        from tsbench import cli
        state["probe"] = probe
        tools = Tools(ts=ts, tester=tester or probe, ts_version="t", tester_version="t")
        monkeypatch.setattr(cli, "preflight", lambda s, term, log: tools)
        monkeypatch.setattr(cli.NativeProbe, "from_settings", classmethod(lambda c, s: probe))

    state["install"] = install
    return state


FAST = ["--no-colour", "--switch-delay", "0", "--rep-delay", "0", "--ready-timeout", "0.3"]


def runs(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con.execute("SELECT * FROM runs ORDER BY id").fetchall()


def test_screen_only_measures_latency_only_and_restores(env, capsys):
    probe = FakeProbe(env["ts"])
    env["install"](probe)
    assert main(["--screen-only", "--db", env["db"], *FAST]) == 0
    out = capsys.readouterr().out
    assert "LATENCY SCREEN" in out
    assert all(throughput is False for _ip, throughput in probe.calls)
    assert env["ts"].current == env["start"]                      # restored
    r = runs(env["db"])
    assert [x["tier"] for x in r] == ["screen"] and r[0]["network_tag"] == "home-isp"


def test_screen_drops_a_latency_outlier_and_records_why(env, capsys):
    slow = env["nodes"][3].ip
    env["install"](FakeProbe(env["ts"], slow_ips=[slow]))
    assert main(["--screen-only", "--db", env["db"], *FAST]) == 0
    assert "dropped 0" not in capsys.readouterr().out
    con = sqlite3.connect(env["db"])
    n_failed = con.execute("SELECT COUNT(*) FROM node_runs WHERE status='failed'").fetchone()[0]
    assert n_failed == 1


def test_quick_probes_top_k_after_the_screen_and_saves_both(env, capsys):
    probe = FakeProbe(env["ts"])
    env["install"](probe)
    assert main(["--quick", "--top", "3", "--favourites", "0", "--db", env["db"], *FAST]) == 0
    out = capsys.readouterr().out
    assert "RECOMMENDED" in out
    throughput_ips = {ip for ip, t in probe.calls if t}
    assert len(throughput_ips - {None}) == 3                      # exactly top-K probed
    assert env["ts"].current == env["start"]
    tiers = [x["tier"] for x in runs(env["db"])]
    assert tiers == ["screen", "quick"]
    q = runs(env["db"])[1]
    assert q["baseline_down"] is not None                          # quick includes a baseline


def test_quick_adds_remembered_favourites(env):
    probe = FakeProbe(env["ts"])
    env["install"](probe)
    assert main(["--quick", "--top", "5", "--favourites", "0", "--db", env["db"], *FAST]) == 0
    first = {ip for ip, t in probe.calls if t and ip}
    assert len(first) == 5
    # history comes to love the node that screens slowest of the five
    slowest = max(first, key=lambda ip: int(ip.split(".")[-1]))
    con = sqlite3.connect(env["db"])
    con.execute("UPDATE node_runs SET med_down=900, med_lat=10, med_jit=0 "
                "WHERE ip=? AND med_down IS NOT NULL", (slowest,))
    con.commit()
    con.close()
    probe.calls.clear()
    assert main(["--quick", "--top", "1", "--favourites", "1", "--db", env["db"], *FAST]) == 0
    second = {ip for ip, t in probe.calls if t and ip}
    assert slowest in second and len(second) == 2                  # fastest + the favourite


def test_quick_uses_no_ookla_and_needs_no_licence(env, monkeypatch):
    env["install"](FakeProbe(env["ts"]))
    monkeypatch.setattr(bench_mod, "run", lambda *a, **k: pytest.fail("shelled out"))
    assert main(["--quick", "--top", "2", "--no-save", *FAST]) == 0


def test_no_save_leaves_no_database(env):
    env["install"](FakeProbe(env["ts"]))
    assert main(["--screen-only", "--no-save", "--db", env["db"], *FAST]) == 0
    import os
    assert not os.path.exists(env["db"])


def test_unreachable_probe_marks_nodes_failed_not_crash(env, capsys):
    dead = [n.ip for n in env["nodes"]]
    env["install"](FakeProbe(env["ts"], dead_ips=dead))
    assert main(["--screen-only", "--db", env["db"], *FAST]) == 0
    assert env["ts"].current == env["start"]


def test_full_run_screens_first_and_tests_only_survivors(env, capsys):
    slow = env["nodes"][2].ip
    probe = FakeProbe(env["ts"], slow_ips=[slow])
    env["install"](probe, tester=OoklaTester("speedtest", accept_license=True))
    assert main(["--max-nodes", "3", "--reps", "1", "--db", env["db"], *FAST]) == 0
    tiers = [x["tier"] for x in runs(env["db"])]
    assert tiers == ["screen", "full"]
    con = sqlite3.connect(env["db"])
    full_hosts = {r[0] for r in con.execute(
        "SELECT hostname FROM node_runs WHERE run_id=2")}
    assert len(full_hosts) == 3                                    # --max-nodes after the screen
    slow_host = next(n.hostname for n in env["nodes"] if n.ip == slow)
    assert slow_host not in full_hosts


def test_full_run_can_skip_the_screen(env):
    env["install"](FakeProbe(env["ts"]), tester=OoklaTester("speedtest", accept_license=True))
    assert main(["--no-screen", "--max-nodes", "2", "--reps", "1", "--db", env["db"],
                 *FAST]) == 0
    assert [x["tier"] for x in runs(env["db"])] == ["full"]


def test_quick_and_screen_only_are_exclusive(capsys):
    assert main(["--quick", "--screen-only"]) == 2


def test_dry_run_in_quick_mode_changes_nothing(env, capsys):
    env["install"](FakeProbe(env["ts"]))
    assert main(["--quick", "--dry-run", "--no-colour"]) == 0
    assert "screened, then the best few probed" in capsys.readouterr().out
    assert env["ts"].calls == []


# -- status and apply on top of a real history ---------------------------------------

def build_history(env):
    env["install"](FakeProbe(env["ts"]))
    for _ in range(2):
        assert main(["--quick", "--top", "4", "--favourites", "0", "--db", env["db"],
                     *FAST]) == 0


def test_status_reports_standings_and_current_node(env, capsys, monkeypatch):
    build_history(env)
    capsys.readouterr()
    from tsbench import status
    monkeypatch.setattr(status, "require_tailscale", lambda s, log: (env["ts"], "t"))
    assert main(["status", "--db", env["db"], "--no-colour"]) == 0
    out = capsys.readouterr().out
    assert "Remembered standing" in out and "Top group" in out and "Current exit node" in out


def test_status_json(env, capsys):
    build_history(env)
    capsys.readouterr()
    assert main(["status", "--db", env["db"], "--json", "--no-current"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["nodes"] and data["top_group"] and data["network"] == "home-isp"


def test_status_without_history(tmp_path, capsys):
    assert main(["status", "--db", str(tmp_path / "none.db")]) == 4


def test_apply_dry_run_from_history_does_not_switch(env, capsys, monkeypatch):
    build_history(env)
    capsys.readouterr()
    from tsbench import apply as apply_mod
    monkeypatch.setattr(apply_mod, "require_tailscale", lambda s, log: (env["ts"], "t"))
    env["ts"].calls.clear()
    assert main(["apply", "--dry-run", "--json", "--db", env["db"]]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["dry_run"] is True and d["action"] in ("switch", "stay")
    assert env["ts"].calls == []


def test_apply_switches_and_records_it(env, capsys, monkeypatch):
    build_history(env)
    capsys.readouterr()
    from tsbench import apply as apply_mod
    monkeypatch.setattr(apply_mod, "require_tailscale", lambda s, log: (env["ts"], "t"))
    monkeypatch.setattr(apply_mod.time, "sleep", lambda _s: None)
    # sit on a node that screens slowest (never probed), then plain `apply` moves to the best
    unseen = max(env["nodes"], key=lambda n: int(n.ip.split(".")[-1]))
    env["ts"].current = unseen.ip
    assert main(["apply", "--db", env["db"], "--no-colour"]) == 0
    assert env["ts"].current != unseen.ip
    last = sqlite3.connect(env["db"]).execute("SELECT hostname FROM applies").fetchall()
    assert len(last) == 1


def test_apply_if_better_never_hijacks_direct_connectivity(env, capsys, monkeypatch):
    build_history(env)
    capsys.readouterr()
    from tsbench import apply as apply_mod
    monkeypatch.setattr(apply_mod, "require_tailscale", lambda s, log: (env["ts"], "t"))
    env["ts"].current = None
    env["ts"].calls.clear()
    assert main(["apply", "--if-better", "--db", env["db"], "--no-colour"]) == 0
    assert env["ts"].calls == [] and env["ts"].current is None


def test_apply_without_history_exits_with_no_data(env, tmp_path, monkeypatch):
    from tsbench import apply as apply_mod
    monkeypatch.setattr(apply_mod, "require_tailscale", lambda s, log: (env["ts"], "t"))
    assert main(["apply", "--db", str(tmp_path / "empty.db")]) == 4


def test_run_apply_flag_decides_after_a_quick_run(env, capsys, monkeypatch):
    env["install"](FakeProbe(env["ts"]))
    from tsbench import apply as apply_mod
    monkeypatch.setattr(apply_mod.time, "sleep", lambda _s: None)
    assert main(["--quick", "--top", "3", "--favourites", "0", "--apply", "--db", env["db"],
                 *FAST]) == 0
    assert "Apply:" in capsys.readouterr().out


def test_unused_helpers_import_cleanly():
    assert FakeTailscale and StatefulTailscale


def test_apply_does_not_create_a_database_when_there_is_no_history(env, tmp_path, monkeypatch):
    import os

    from tsbench import apply as apply_mod
    monkeypatch.setattr(apply_mod, "require_tailscale", lambda s, log: (env["ts"], "t"))
    db = tmp_path / "absent.db"
    assert main(["apply", "--db", str(db)]) == 4
    assert not os.path.exists(db)


# -- remembered refresh, the run lock, and the one-line status ---------------------------

def test_remembered_probes_only_the_best_remembered_nodes_with_no_screen(env, capsys):
    build_history(env)
    before = len(runs(env["db"]))
    env["install"](FakeProbe(env["ts"]))
    probe = env["probe"]
    assert main(["--quick", "--remembered", "2", "--db", env["db"], *FAST]) == 0
    after = runs(env["db"])
    assert [x["tier"] for x in after[before:]] == ["quick"]        # no screen run was recorded
    assert all(throughput for _ip, throughput in probe.calls)       # nothing was latency-screened
    probed = {ip for ip, _t in probe.calls if ip}
    assert len(probed) == 2
    assert env["ts"].current == env["start"]                         # restored as usual


def test_remembered_falls_back_to_the_screen_without_history(env, capsys):
    probe = FakeProbe(env["ts"])
    env["install"](probe)
    assert main(["--quick", "--remembered", "2", "--top", "2", "--favourites", "0",
                 "--db", env["db"], *FAST]) == 0
    assert "no remembered nodes yet" in capsys.readouterr().out
    assert [x["tier"] for x in runs(env["db"])] == ["screen", "quick"]


def test_remembered_needs_quick(capsys):
    assert main(["--remembered", "3"]) == 2
    assert main(["--quick", "--remembered", "-1"]) == 2


def test_a_manual_run_refuses_while_another_run_holds_the_lock(env, capsys):
    from tsbench.errors import EXIT_LOCKED
    from tsbench.lock import RunLock
    env["install"](FakeProbe(env["ts"]))
    with RunLock("auto"):
        assert main(["--quick", "--db", env["db"], *FAST]) == EXIT_LOCKED
    assert "automatic run" in capsys.readouterr().err
    assert env["ts"].calls == []                                     # exit node never touched


def test_dry_run_does_not_need_the_lock(env, capsys):
    from tsbench.lock import RunLock
    env["install"](FakeProbe(env["ts"]))
    with RunLock("auto"):
        assert main(["--quick", "--dry-run", *FAST]) == 0


def test_apply_refuses_under_a_running_benchmark_but_dry_run_is_fine(env, capsys, monkeypatch):
    from tsbench import apply as apply_mod
    from tsbench.errors import EXIT_LOCKED
    from tsbench.lock import RunLock
    build_history(env)
    monkeypatch.setattr(apply_mod, "require_tailscale", lambda s, log: (env["ts"], "t"))
    env["ts"].calls.clear()
    with RunLock("run"):
        assert main(["apply", "--db", env["db"], "--no-colour"]) == EXIT_LOCKED
        assert env["ts"].calls == []
        assert main(["apply", "--dry-run", "--db", env["db"], "--no-colour"]) == 0


def test_the_lock_is_released_after_a_run(env):
    from tsbench.lock import RunLock
    env["install"](FakeProbe(env["ts"]))
    assert main(["--screen-only", "--db", env["db"], *FAST]) == 0
    with RunLock("next"):
        pass


def test_status_oneline_is_a_single_summary_line(env, capsys):
    build_history(env)
    capsys.readouterr()
    assert main(["status", "--oneline", "--no-current", "--db", env["db"]]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("Best: gb-") and "last full run" in out[0]
