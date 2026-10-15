import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tsbench import auto
from tsbench.auto import auto_main, make_plan, rotate_log
from tsbench.errors import EXIT_LOCKED, EXIT_SKIPPED
from tsbench.lock import RunLock

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")


# -- the planner ---------------------------------------------------------------------------

def test_never_run_means_a_deep_run_now():
    p = make_plan(None, None, NOW, 18, can_full=True, refresh_nodes=4)
    assert p.kind == "full" and "never" in p.reason
    assert "--accept-ookla-license" in p.argv and "--quick" not in p.argv


def test_stale_full_run_triggers_a_full_run():
    assert make_plan(ago(19), ago(1), NOW, 18, True, 4).kind == "full"


def test_fresh_full_run_means_a_cheap_refresh_of_the_remembered_nodes():
    p = make_plan(ago(5), ago(5), NOW, 18, True, 4)
    assert p.kind == "refresh"
    assert p.argv[p.argv.index("--remembered") + 1] == "4"
    assert "--accept-ookla-license" not in p.argv


def test_refresh_uses_a_short_probe_because_it_runs_unattended():
    p = make_plan(ago(5), ago(5), NOW, 18, True, 3)
    assert p.argv[p.argv.index("--probe-seconds") + 1] == "4"
    p = make_plan(ago(5), ago(5), NOW, 18, True, 3, refresh_probe_s=2.5)
    assert p.argv[p.argv.index("--probe-seconds") + 1] == "2.5"
    full = make_plan(None, None, NOW, 18, True, 3)
    assert "--probe-seconds" not in full.argv


def test_boundary_is_inclusive_so_a_run_is_never_skipped_forever():
    assert make_plan(ago(18), ago(18), NOW, 18, True, 4).kind == "full"
    assert make_plan(ago(17.9), ago(17.9), NOW, 18, True, 4).kind == "refresh"


def test_without_the_licence_the_deep_run_is_a_wide_quick_run_never_ookla():
    p = make_plan(None, None, NOW, 18, can_full=False, refresh_nodes=4)
    assert p.kind == "deep" and "--quick" in p.argv and "--accept-ookla-license" not in p.argv
    assert "--remembered" not in p.argv


def test_without_the_licence_staleness_follows_the_last_screen_not_the_last_full_run():
    # no full run ever recorded, but a screen 2 h ago: a refresh is right, not a deep run
    assert make_plan(None, ago(2), NOW, 18, can_full=False, refresh_nodes=4).kind == "refresh"
    assert make_plan(None, ago(30), NOW, 18, can_full=False, refresh_nodes=4).kind == "deep"


def test_every_plan_applies_with_hysteresis_and_stays_quiet():
    for p in (make_plan(None, None, NOW, 18, True, 4), make_plan(ago(1), ago(1), NOW, 18, True, 4)):
        assert "--apply" in p.argv and "--quiet" in p.argv and "--no-colour" in p.argv


def test_extra_arguments_are_forwarded():
    p = make_plan(ago(1), ago(1), NOW, 18, True, 4, ["--tailscale-path", "/x/tailscale"])
    assert p.argv[-2:] == ["--tailscale-path", "/x/tailscale"]


# -- the command ---------------------------------------------------------------------------

@pytest.fixture
def paths(tmp_path):
    return {"log": str(tmp_path / "auto.log"), "db": str(tmp_path / "t.db")}


def base_args(paths, *extra):
    return ["--log-file", paths["log"], "--db", paths["db"], *extra]


def test_a_guard_skips_without_running_anything(paths, monkeypatch):
    monkeypatch.setattr(auto, "check_guards", lambda **k: "the machine is on battery power")
    monkeypatch.setattr("tsbench.cli.run_main", lambda *a, **k: pytest.fail("ran while guarded"))
    assert auto_main(base_args(paths)) == EXIT_SKIPPED
    assert "skipped: the machine is on battery power" in open(paths["log"]).read()


def test_no_guard_runs_even_when_a_guard_would_say_no(paths, monkeypatch):
    monkeypatch.setattr(auto, "check_guards", lambda **k: "busy")
    seen = {}
    monkeypatch.setattr("tsbench.cli.run_main", lambda argv, locked=False: seen.update(
        argv=argv, locked=locked) or 0)
    assert auto_main(base_args(paths, "--no-guard")) == 0
    assert seen["locked"] is True


def test_a_held_lock_skips_with_its_own_exit_code(paths, monkeypatch):
    monkeypatch.setattr(auto, "check_guards", lambda **k: None)
    monkeypatch.setattr("tsbench.cli.run_main", lambda *a, **k: pytest.fail("ran while locked"))
    with RunLock("run"):
        assert auto_main(base_args(paths)) == EXIT_LOCKED
    assert "already running" in open(paths["log"]).read()


def test_it_holds_the_lock_while_the_run_is_in_progress(paths, monkeypatch):
    monkeypatch.setattr(auto, "check_guards", lambda **k: None)
    held = {}

    def fake_run(argv, locked=False):
        try:
            RunLock("manual").acquire()
            held["busy"] = False
        except Exception:
            held["busy"] = True
        return 0
    monkeypatch.setattr("tsbench.cli.run_main", fake_run)
    assert auto_main(base_args(paths)) == 0
    assert held["busy"] is True
    RunLock("after").acquire()                       # released afterwards


def test_the_runs_exit_code_is_passed_through_and_logged(paths, monkeypatch):
    monkeypatch.setattr(auto, "check_guards", lambda **k: None)
    monkeypatch.setattr("tsbench.cli.run_main", lambda argv, locked=False: 1)
    assert auto_main(base_args(paths)) == 1
    assert "exit code 1" in open(paths["log"]).read()


def test_dry_run_shows_guards_and_plan_and_does_nothing(paths, monkeypatch, capsys):
    monkeypatch.setattr(auto, "check_guards", lambda **k: "a call appears to be in progress")
    monkeypatch.setattr("tsbench.cli.run_main", lambda *a, **k: pytest.fail("ran"))
    assert auto_main(base_args(paths, "--dry-run")) == 0
    err = capsys.readouterr().err
    assert "would skip" in err and "plan:" in err and "would run:" in err


def test_plan_is_chosen_from_the_stored_history(paths, monkeypatch, capsys):
    con = sqlite3.connect(paths["db"])
    from tsbench.store import Store
    Store(paths["db"]).close()
    con = sqlite3.connect(paths["db"])
    con.execute("INSERT INTO runs (started_at, tier, interrupted) VALUES (?, 'full', 0)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    con.commit()
    con.close()
    monkeypatch.setattr(auto, "check_guards", lambda **k: None)
    assert auto_main(base_args(paths, "--dry-run", "--accept-ookla-license")) == 0
    assert "plan: refresh" in capsys.readouterr().err


def test_guard_options_reach_the_guard_check(paths, monkeypatch):
    seen = {}
    monkeypatch.setattr(auto, "check_guards", lambda **k: seen.update(k))
    monkeypatch.setattr("tsbench.cli.run_main", lambda *a, **k: 0)
    auto_main(base_args(paths, "--min-idle", "5", "--allow-battery", "--call-app", "obs"))
    assert seen["min_idle_s"] == 5 and seen["battery"] is False and seen["call_apps"] == ["obs"]


def test_env_var_alone_allows_full_runs(paths, monkeypatch, capsys):
    monkeypatch.setenv("TSBENCH_ACCEPT_OOKLA_LICENSE", "1")
    monkeypatch.setattr(auto, "check_guards", lambda **k: None)
    auto_main(base_args(paths, "--dry-run"))
    assert "plan: full" in capsys.readouterr().err


def test_log_rotation_keeps_one_previous_file(tmp_path):
    p = tmp_path / "auto.log"
    p.write_text("x" * 50)
    rotate_log(str(p), max_bytes=10)
    assert not p.exists() and (tmp_path / "auto.log.1").exists()
    p.write_text("small")
    rotate_log(str(p), max_bytes=10)
    assert p.exists()


def test_json_is_not_involved(paths):
    assert json  # keep the import honest for future structured output
