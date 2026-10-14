import os

import pytest

from tsbench import cli
from tsbench.cli import build_parser, main
from tsbench.preflight import Tools
from tsbench.probes import LibrespeedTester
from tsbench.settings import Settings, default_db_path, state_dir
from tsbench.store import Store


def parse(*argv) -> Settings:
    return Settings.from_args(build_parser().parse_args(list(argv)))


def test_defaults_cover_every_uk_city_and_save_history():
    s = parse()
    assert s.cities == [] and s.exclude_cities == []
    assert s.country == "uk" and s.save is True and s.out_dir is None
    assert s.reps == 3 and s.baseline is True


def test_flags_populate_settings():
    s = parse("--cities", "London, Glasgow", "--exclude-cities", "Manchester", "--no-save",
              "--reps", "5", "--no-baseline", "--out-dir", "x", "--tester", "librespeed")
    assert s.cities == ["London", "Glasgow"] and s.exclude_cities == ["Manchester"]
    assert s.save is False and s.reps == 5 and s.baseline is False
    assert s.out_dir == "x" and s.tester == "librespeed"


def test_min_success():
    assert Settings(reps=1).min_success == 1
    assert Settings(reps=3).min_success == 2
    assert Settings(reps=5).min_success == 3


def test_ookla_licence_needs_flag_or_env(monkeypatch):
    monkeypatch.delenv("TSBENCH_ACCEPT_OOKLA_LICENSE", raising=False)
    assert parse().accept_ookla_license is False
    assert parse("--accept-ookla-license").accept_ookla_license is True
    monkeypatch.setenv("TSBENCH_ACCEPT_OOKLA_LICENSE", "1")
    assert parse().accept_ookla_license is True
    monkeypatch.setenv("TSBENCH_ACCEPT_OOKLA_LICENSE", "0")
    assert parse().accept_ookla_license is False


def test_state_dir_honours_tsbench_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TSBENCH_HOME", str(tmp_path))
    assert state_dir() == str(tmp_path)
    assert default_db_path() == str(tmp_path / "tsbench.db")


def test_state_dir_falls_back_to_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("TSBENCH_HOME", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state_dir() == os.path.join(str(tmp_path), "tsbench")


def test_keep_raw_requires_out_dir(capsys):
    assert main(["--keep-raw"]) == 2
    assert "--out-dir" in capsys.readouterr().err


def test_reps_must_be_positive(capsys):
    assert main(["--reps", "0"]) == 2


def test_bare_invocation_and_run_subcommand_are_equivalent(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "run_main", lambda argv: seen.append(argv) or 0)
    main(["--dry-run"])
    main(["run", "--dry-run"])
    assert seen == [["--dry-run"], ["--dry-run"]]


def test_dry_run_selects_nodes_and_never_touches_the_exit_node(monkeypatch, fake_ts, capsys):
    tools = Tools(ts=fake_ts, tester=LibrespeedTester("librespeed-cli"), ts_version="test",
                  tester_version="test")
    monkeypatch.setattr(cli, "preflight", lambda s, term, log: tools)
    assert main(["--dry-run", "--no-colour"]) == 0
    out = capsys.readouterr().out
    assert "21 node(s) would be benchmarked" in out        # 22 UK nodes, one offline
    assert "Manchester" in out and "Glasgow" in out        # UK-wide by default
    assert fake_ts.calls == []


def test_history_list_with_no_database(tmp_path, capsys):
    assert main(["history", "list", "--db", str(tmp_path / "none.db")]) == 0
    assert "No history yet" in capsys.readouterr().out


def test_history_list_and_prune(tmp_path, capsys, run_fixture):
    from conftest import build_results

    db = str(tmp_path / "h.db")
    with Store(db) as s:
        s.save_run({"started_at": "2020-01-01T00:00:00+00:00", "duration_s": 60.0,
                    "restored_to": "gb-lon-wg-001 (100.64.0.1)"}, build_results(run_fixture))
    assert main(["history", "list", "--db", db]) == 0
    listing = capsys.readouterr().out
    assert "2020-01-01 00:00:00" in listing and "gb-lon-wg-001" in listing

    assert main(["history", "prune", "--older-than", "30", "--db", db]) == 0
    assert "Deleted 1 run" in capsys.readouterr().out
    assert main(["history", "list", "--db", db]) == 0
    assert "No runs recorded" in capsys.readouterr().out


def test_prune_requires_a_threshold():
    with pytest.raises(SystemExit):
        main(["history", "prune"])
