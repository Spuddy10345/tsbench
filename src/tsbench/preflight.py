"""Checks that must pass before anything touches the exit node."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from tsbench.console import Log, Term
from tsbench.errors import Fatal
from tsbench.probes import KNOWN_TESTER_PATHS, TESTERS, OoklaTester, SpeedTester
from tsbench.proc import find_executable, run
from tsbench.settings import Settings
from tsbench.tailscale import KNOWN_TAILSCALE_PATHS, Tailscale

OOKLA_LICENSE_HELP = (
    "The Ookla CLI requires you to accept its licence and privacy terms\n"
    "        (https://www.speedtest.net/about/eula, https://www.speedtest.net/about/privacy).\n"
    "        tsbench will not accept them on your behalf. Either run `speedtest` once\n"
    "        yourself and accept, then pass --accept-ookla-license so tsbench can run it\n"
    "        non-interactively, or set TSBENCH_ACCEPT_OOKLA_LICENSE=1.\n"
    "        (Quick mode does not use Ookla; `--tester librespeed` is another option.)"
)

INSTALL_HINTS = {
    "ookla": ("brew tap teamookla/speedtest && brew install speedtest\n"
              "        (Linux/other: https://www.speedtest.net/apps/cli)\n"
              "        Note: the `speedtest-cli` Python package and Debian's\n"
              "        `speedtest-cli` are DIFFERENT tools with different JSON;\n"
              "        they are not accepted here."),
    "librespeed": "brew install librespeed-cli",
}


@dataclass
class Tools:
    ts: Tailscale
    tester: SpeedTester
    ts_version: str
    tester_version: str


def preflight(s: Settings, term: Term, log: Log) -> Tools:
    ts_exe = find_executable("tailscale", KNOWN_TAILSCALE_PATHS,
                             s.tailscale_path or os.environ.get("TSBENCH_TAILSCALE"))
    if not ts_exe:
        raise Fatal("`tailscale` not found on PATH or in the usual install locations.\n"
                    "        macOS (Homebrew):  brew install tailscale\n"
                    "        macOS (App Store): ln -s /Applications/Tailscale.app/Contents/"
                    "MacOS/Tailscale /usr/local/bin/tailscale\n"
                    "        Or point tsbench at it with --tailscale-path.")
    ts = Tailscale(ts_exe, log)

    rc, out, _ = run([ts_exe, "version"], timeout=15)
    ts_ver = out.splitlines()[0].strip() if rc == 0 and out.strip() else "unknown"

    try:
        state = ts.backend_state()
    except Fatal as exc:
        raise Fatal(
            f"could not query Tailscale: {exc}\n"
            "        Is the daemon running and are you logged in? "
            "Try `tailscale status`.") from exc
    if state != "Running":
        raise Fatal(f"Tailscale backend state is {state!r}, expected 'Running'.\n"
                    "        Run `tailscale up` and make sure this machine is authenticated.")

    cls = TESTERS[s.tester]
    st_exe = find_executable(cls.binary, KNOWN_TESTER_PATHS.get(cls.binary, ()), s.tester_path)
    if not st_exe:
        raise Fatal(f"selected speed test {s.tester!r} needs {cls.binary!r}, "
                    f"which was not found.\n        Install it with:\n"
                    f"        {INSTALL_HINTS[s.tester]}\n"
                    "        tsbench will not silently substitute a different tool.")
    if cls is OoklaTester:
        if not s.dry_run and not s.accept_ookla_license:
            raise Fatal(OOKLA_LICENSE_HELP)
        tester: SpeedTester = OoklaTester(st_exe, accept_license=s.accept_ookla_license)
    else:
        tester = cls(st_exe)

    rc, out, errtxt = run([st_exe, "--version"], timeout=20)
    text: Optional[str] = out or errtxt
    st_ver = text.splitlines()[0].strip() if text else "unknown"

    log.info(f"tailscale: {term.dim(ts_ver)}")
    log.info(f"speed test: {term.dim(st_ver)}  [{tester.name}]")
    log.info(f"units: {term.dim(tester.units_note)}")
    return Tools(ts=ts, tester=tester, ts_version=ts_ver, tester_version=st_ver)
