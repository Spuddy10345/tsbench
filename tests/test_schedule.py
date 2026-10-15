import argparse
import os
import plistlib
import subprocess
import sys

import pytest

from tsbench import schedule
from tsbench.errors import EXIT_LOCKED, EXIT_SKIPPED
from tsbench.schedule import (
    LABEL,
    build_parser,
    build_plist,
    explain_exit,
    install,
    parse_launchctl_print,
    plist_path,
    status,
    uninstall,
)


class FakeLaunchctl:
    def __init__(self, bootstrap_rc=0, print_out=""):
        self.calls, self.bootstrap_rc, self.print_out = [], bootstrap_rc, print_out

    def __call__(self, argv, timeout=None):
        self.calls.append(argv)
        if argv[1] == "bootstrap":
            return self.bootstrap_rc, "", "Bootstrap failed: 5" if self.bootstrap_rc else ""
        if argv[1] == "print":
            return (0, self.print_out, "") if self.print_out else (113, "", "not found")
        return 0, "", ""


def args(*extra):
    return build_parser().parse_args(["install", *extra])


def test_plist_runs_auto_with_absolute_paths_and_never_at_login():
    d = build_plist("/venv/bin/python", 1.0, ["--tailscale-path", "/usr/local/bin/tailscale"],
                    "/state")
    assert d["Label"] == LABEL
    assert d["ProgramArguments"][:4] == ["/venv/bin/python", "-m", "tsbench", "auto"]
    assert d["ProgramArguments"][4:] == ["--tailscale-path", "/usr/local/bin/tailscale"]
    assert d["StartInterval"] == 3600 and d["RunAtLoad"] is False
    assert all(os.path.isabs(p) for p in d["ProgramArguments"] if p.startswith("/"))
    assert "/opt/homebrew/bin" in d["EnvironmentVariables"]["PATH"]


def test_interval_has_a_floor_and_home_override_is_recorded():
    d = build_plist("/p", 0.0001, [], "/state", tsbench_home="/custom")
    assert d["StartInterval"] == 60
    assert d["EnvironmentVariables"]["TSBENCH_HOME"] == "/custom"


def test_plist_serialises_to_a_valid_property_list():
    d = build_plist("/p", 2, ["--min-idle", "30"], "/state")
    assert plistlib.loads(plistlib.dumps(d)) == d


@pytest.mark.skipif(sys.platform != "darwin", reason="plutil is macOS only")
def test_plutil_accepts_the_generated_plist(tmp_path):
    f = tmp_path / "a.plist"
    f.write_bytes(plistlib.dumps(build_plist(sys.executable, 1, [], str(tmp_path))))
    assert subprocess.run(["plutil", "-lint", str(f)], capture_output=True).returncode == 0


def test_install_writes_the_agent_then_loads_it(tmp_path, monkeypatch):
    monkeypatch.setenv("TSBENCH_HOME", str(tmp_path / "state"))
    lc = FakeLaunchctl()
    rc = install(args("--interval-hours", "2", "--full-every-hours", "12", "--accept-ookla-license",
                      "--tailscale-path", sys.executable), runner=lc, home=str(tmp_path),
                 python="/venv/bin/python")
    assert rc == 0
    path = plist_path(str(tmp_path))
    data = plistlib.loads(open(path, "rb").read())
    pa = data["ProgramArguments"]
    assert pa[:4] == ["/venv/bin/python", "-m", "tsbench", "auto"]
    assert "--accept-ookla-license" in pa and pa[pa.index("--full-every-hours") + 1] == "12"
    assert data["StartInterval"] == 7200
    assert (os.stat(path).st_mode & 0o022) == 0                 # launchd refuses writable agents
    verbs = [c[1] for c in lc.calls]
    assert verbs == ["bootout", "bootstrap"]                    # reinstall is idempotent
    assert lc.calls[1][-1] == path


def test_install_does_not_accept_the_ookla_licence_for_you(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TSBENCH_HOME", str(tmp_path / "state"))
    install(args(), runner=FakeLaunchctl(), home=str(tmp_path), python="/p")
    data = plistlib.loads(open(plist_path(str(tmp_path)), "rb").read())
    assert "--accept-ookla-license" not in data["ProgramArguments"]
    assert "built-in probe" in capsys.readouterr().out


def test_install_dry_run_prints_and_changes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TSBENCH_HOME", str(tmp_path / "state"))
    lc = FakeLaunchctl()
    assert install(args("--dry-run"), runner=lc, home=str(tmp_path), python="/p") == 0
    assert "<key>Label</key>" in capsys.readouterr().out
    assert lc.calls == [] and not os.path.exists(plist_path(str(tmp_path)))


def test_install_reports_a_load_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TSBENCH_HOME", str(tmp_path / "state"))
    rc = install(args(), runner=FakeLaunchctl(bootstrap_rc=5), home=str(tmp_path), python="/p")
    assert rc == 1 and "could not load" in capsys.readouterr().err


def test_uninstall_unloads_then_removes_the_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TSBENCH_HOME", str(tmp_path / "state"))
    install(args(), runner=FakeLaunchctl(), home=str(tmp_path), python="/p")
    lc = FakeLaunchctl()
    assert uninstall(runner=lc, home=str(tmp_path)) == 0
    assert lc.calls[0][1] == "bootout" and not os.path.exists(plist_path(str(tmp_path)))
    assert uninstall(runner=FakeLaunchctl(), home=str(tmp_path)) == 0
    assert "not installed" in capsys.readouterr().out


PRINT = """gui/501/io.github.spuddy10345.tsbench = {
\tstate = not running
\trun interval = 3600 seconds
\tlast exit code = 10
}"""


def test_launchctl_print_is_parsed():
    assert parse_launchctl_print(PRINT) == {"state": "not", "last_exit": "10", "interval": "3600"}


def test_exit_codes_are_explained_in_plain_words():
    assert "guard" in explain_exit(str(EXIT_SKIPPED))
    assert "another run" in explain_exit(str(EXIT_LOCKED))
    assert explain_exit("0") == "ok" and "not run" in explain_exit(None)
    assert "log" in explain_exit("1")


def test_status_when_not_installed(tmp_path, capsys):
    assert status(runner=FakeLaunchctl(), home=str(tmp_path)) == 0
    assert "not installed" in capsys.readouterr().out


def test_status_when_installed_and_loaded(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TSBENCH_HOME", str(tmp_path / "state"))
    install(args(), runner=FakeLaunchctl(), home=str(tmp_path), python="/p")
    capsys.readouterr()
    status(runner=FakeLaunchctl(print_out=PRINT), home=str(tmp_path))
    out = capsys.readouterr().out
    assert "loaded:    yes, every 1 h" in out and "skipped by a guard" in out


def test_non_macos_gets_a_pointer_to_the_linux_examples(monkeypatch, capsys):
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    assert schedule.schedule_main(["status"]) == 2
    assert "integrations/linux" in capsys.readouterr().err


def test_namespace_helper_covers_every_flag():
    ns = argparse.Namespace(tailscale_path=None, tester_path=None, full_every_hours=None,
                            accept_ookla_license=False, allow_battery=True, min_idle=0.0)
    assert "--allow-battery" in schedule._install_args(ns)
    assert schedule._install_args(ns)[-2:] == ["--min-idle", "0"]


def test_default_interval_is_two_hours_because_each_step_downloads_data():
    assert build_parser().parse_args(["install"]).interval_hours == 2.0
