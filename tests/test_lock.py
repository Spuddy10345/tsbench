import subprocess
import sys
import textwrap
import time

import pytest

from tsbench.lock import LockBusy, RunLock, describe_holder


def test_second_holder_is_refused_and_told_who_has_it(tmp_path):
    path = str(tmp_path / "run.lock")
    with RunLock("auto", path):
        with pytest.raises(LockBusy) as busy:
            RunLock("run", path).acquire()
    assert "auto" in busy.value.holder


def test_lock_is_released_on_exit_so_it_can_be_retaken(tmp_path):
    path = str(tmp_path / "run.lock")
    with RunLock("run", path):
        pass
    with RunLock("apply", path):
        pass


def test_a_crashed_holder_leaves_no_stale_lock(tmp_path):
    path = str(tmp_path / "run.lock")
    code = textwrap.dedent(f"""
        import sys, time
        from tsbench.lock import RunLock
        lock = RunLock("auto", {path!r})     # keep a reference: the lock lives as long as it
        lock.acquire()
        print("held", flush=True)
        time.sleep(60)
    """)
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(LockBusy):
            RunLock("run", path).acquire()
    finally:
        child.kill()
        child.wait()
    deadline = time.time() + 5                      # the kernel drops the lock with the process
    while True:
        try:
            RunLock("run", path).acquire()
            break
        except LockBusy:
            assert time.time() < deadline, "lock still held after the holder died"
            time.sleep(0.05)


def test_lock_directory_is_private(tmp_path):
    import os
    path = tmp_path / "state" / "run.lock"
    with RunLock("run", str(path)):
        assert (os.stat(path.parent).st_mode & 0o077) == 0


def test_holder_is_described_for_humans():
    assert describe_holder("4242 auto") == "an automatic run (pid 4242)"
    assert describe_holder("7 run") == "a benchmark run (pid 7)"
    assert describe_holder("garbage") == "garbage"
