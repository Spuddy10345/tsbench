"""The launcher scripts under integrations/ run against a stand-in `tsbench`."""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RAYCAST = ROOT / "integrations" / "raycast"
LINUX = ROOT / "integrations" / "linux"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash scripts")


def fake_tsbench(tmp_path, body):
    exe = tmp_path / "tsbench"
    exe.write_text("#!/bin/bash\n" + body)
    exe.chmod(0o755)
    return exe


def run_script(name, tmp_path, fake_body):
    env = dict(os.environ, TSBENCH_BIN=str(fake_tsbench(tmp_path, fake_body)),
               TSBENCH_HOME=str(tmp_path / "state"))
    return subprocess.run(["bash", str(RAYCAST / name)], capture_output=True, text=True,
                          env=env, timeout=30)


def test_every_script_declares_valid_raycast_metadata_and_is_executable():
    scripts = sorted(RAYCAST.glob("*.sh"))
    assert len(scripts) == 3
    for s in scripts:
        text = s.read_text()
        assert text.startswith("#!/bin/bash")
        assert re.search(r"^# @raycast\.schemaVersion 1$", text, re.M)
        assert re.search(r"^# @raycast\.title .+", text, re.M)
        assert re.search(r"^# @raycast\.mode (silent|compact)$", text, re.M)
        assert s.stat().st_mode & stat.S_IXUSR, f"{s.name} is not executable"


def test_scripts_set_their_own_path_because_raycast_has_a_minimal_one():
    for s in RAYCAST.glob("*.sh"):
        assert 'export PATH="' in s.read_text()


def test_apply_best_shows_only_the_last_line_and_keeps_the_exit_code(tmp_path):
    r = run_script("tsbench-apply-best.sh", tmp_path,
                   'echo chatter; echo "switched a -> b: best remembered node"; exit 0\n')
    assert r.returncode == 0 and r.stdout.strip() == "switched a -> b: best remembered node"
    r = run_script("tsbench-apply-best.sh", tmp_path, 'echo "No history yet"; exit 4\n')
    assert r.returncode == 4 and r.stdout.strip() == "No history yet"


def test_apply_best_is_a_plain_apply_not_a_hysteresis_one(tmp_path):
    r = run_script("tsbench-apply-best.sh", tmp_path, 'echo "$*"\n')
    assert r.stdout.split() == ["apply", "--no-colour"]


def test_show_best_is_one_instant_status_line(tmp_path):
    r = run_script("tsbench-show-best.sh", tmp_path, 'echo "args: $*"\n')
    assert r.stdout.strip() == "args: status --oneline --no-colour"


def test_quick_retest_returns_at_once_and_runs_detached(tmp_path):
    r = run_script("tsbench-quick-retest.sh", tmp_path, 'sleep 3\n')
    assert r.returncode == 0 and "started" in r.stdout


def test_quick_retest_reports_a_run_that_dies_immediately(tmp_path):
    r = run_script("tsbench-quick-retest.sh", tmp_path,
                   'echo "an automatic run (pid 7) is already changing the exit node" >&2; '
                   'exit 11\n')
    assert r.returncode == 11 and "already changing the exit node" in r.stdout
    assert "started" not in r.stdout


def test_quick_retest_state_directory_is_private(tmp_path):
    run_script("tsbench-quick-retest.sh", tmp_path, 'exit 0\n')
    assert ((tmp_path / "state").stat().st_mode & 0o077) == 0


def test_quick_retest_uses_the_documented_flags(tmp_path):
    run_script("tsbench-quick-retest.sh", tmp_path, 'echo "$*"; exit 0\n')
    log = (tmp_path / "state" / "raycast.log").read_text()
    assert "--quick --apply --quiet --no-colour" in log


def test_systemd_units_are_complete_and_tolerate_the_skip_exit_codes():
    service = (LINUX / "tsbench-auto.service").read_text()
    assert "ExecStart=" in service and "Type=oneshot" in service
    assert "SuccessExitStatus=10 11" in service
    assert "ACCEPT_OOKLA" in service and "# Environment=TSBENCH_ACCEPT_OOKLA" in service
    timer = (LINUX / "tsbench-auto.timer").read_text()
    assert "OnCalendar=hourly" in timer and "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_crontab_example_uses_full_paths():
    lines = [ln for ln in (LINUX / "crontab.example").read_text().splitlines()
             if ln and not ln.startswith("#")]
    job = [ln for ln in lines if "tsbench" in ln]
    assert job and "$HOME/.local/bin/tsbench" in job[0] and " auto" in job[0]
