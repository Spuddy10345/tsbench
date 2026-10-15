"""`tsbench schedule install|uninstall|status`: a launchd user agent on macOS.

The agent runs `tsbench auto` every two hours (configurable).  It is a *user* agent
(`~/Library/LaunchAgents`, loaded in the `gui/<uid>` domain), so it needs no
root and stops when you log out.

launchd starts jobs with a minimal PATH, so everything it needs is recorded as
an absolute path at install time: the Python that runs tsbench, and the
`tailscale` / `speedtest` binaries if they can be found.  Re-run `install`
after moving or upgrading the Python environment tsbench lives in.

On Linux there is no installer; see integrations/linux for systemd and cron
examples.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from tsbench import PROG
from tsbench.errors import EXIT_LOCKED, EXIT_SKIPPED
from tsbench.probes import KNOWN_TESTER_PATHS
from tsbench.proc import find_executable, run
from tsbench.settings import default_db_path, state_dir
from tsbench.store import Store
from tsbench.tailscale import KNOWN_TAILSCALE_PATHS

LABEL = "io.github.spuddy10345.tsbench"
LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

Runner = Callable[..., Tuple[int, str, str]]


def plist_path(home: Optional[str] = None) -> str:
    return os.path.join(home or os.path.expanduser("~"), "Library", "LaunchAgents",
                        f"{LABEL}.plist")


def build_plist(python: str, interval_h: float, auto_args: List[str],
                state: str, tsbench_home: Optional[str] = None) -> Dict[str, Any]:
    env = {"PATH": LAUNCHD_PATH}
    if tsbench_home:
        env["TSBENCH_HOME"] = tsbench_home
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "tsbench", "auto", *auto_args],
        "StartInterval": max(60, int(interval_h * 3600)),
        "RunAtLoad": False,                     # never switch exit node at login
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "WorkingDirectory": os.path.expanduser("~"),
        "EnvironmentVariables": env,
        # `auto` keeps its own rotated log; launchd's copy is only for crashes.
        "StandardOutPath": os.devnull,
        "StandardErrorPath": os.path.join(state, "launchd-error.log"),
    }


def discover_paths(tailscale: Optional[str], tester: Optional[str]) -> List[str]:
    """Absolute tool paths to bake into the job, as `auto` pass-through flags."""
    args: List[str] = []
    ts = find_executable("tailscale", KNOWN_TAILSCALE_PATHS, tailscale)
    if ts:
        args += ["--tailscale-path", ts]
    st = find_executable("speedtest", KNOWN_TESTER_PATHS.get("speedtest", ()), tester)
    if st:
        args += ["--tester-path", st]
    return args


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{PROG} schedule",
        description="Install, remove or inspect the scheduled launchd job (macOS).")
    sub = p.add_subparsers(dest="action", required=True)
    ins = sub.add_parser("install", help="write and load the launchd agent")
    ins.add_argument("--interval-hours", type=float, default=2.0, metavar="H",
                     help="how often launchd starts `tsbench auto` (default: 2; each step "
                          "downloads data, see the README's data-use section)")
    ins.add_argument("--full-every-hours", type=float, default=None, metavar="H",
                     help="forwarded to `tsbench auto` (default there: 18)")
    ins.add_argument("--accept-ookla-license", action="store_true",
                     help="confirm you accepted Ookla's licence so scheduled full runs can "
                          "use it; without this the periodic deep run uses the built-in probe")
    ins.add_argument("--allow-battery", action="store_true")
    ins.add_argument("--min-idle", type=float, default=None, metavar="SEC")
    ins.add_argument("--tailscale-path", default=None, metavar="PATH")
    ins.add_argument("--tester-path", default=None, metavar="PATH")
    ins.add_argument("--dry-run", action="store_true",
                     help="print the agent definition and change nothing")
    sub.add_parser("uninstall", help="unload and delete the launchd agent")
    sub.add_parser("status", help="is it installed, loaded and what did it last do")
    return p


def _install_args(a: argparse.Namespace) -> List[str]:
    args = discover_paths(a.tailscale_path, a.tester_path)
    if a.full_every_hours is not None:
        args += ["--full-every-hours", f"{a.full_every_hours:g}"]
    if a.accept_ookla_license:
        args.append("--accept-ookla-license")
    if a.allow_battery:
        args.append("--allow-battery")
    if a.min_idle is not None:
        args += ["--min-idle", f"{a.min_idle:g}"]
    return args


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(a: argparse.Namespace, runner: Runner = run, home: Optional[str] = None,
            python: Optional[str] = None) -> int:
    state = state_dir()
    data = build_plist(python or sys.executable, a.interval_hours, _install_args(a), state,
                       os.environ.get("TSBENCH_HOME"))
    if a.dry_run:
        print(plistlib.dumps(data).decode("utf-8"))
        print(f"(dry run: would write {plist_path(home)} and load it into {_domain()})")
        return 0
    path = plist_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(state, mode=0o700, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(data, fh)
    os.chmod(path, 0o644)                       # launchd rejects group/world-writable agents
    runner(["launchctl", "bootout", f"{_domain()}/{LABEL}"], timeout=20)  # reinstall is fine
    rc, out, err = runner(["launchctl", "bootstrap", _domain(), path], timeout=20)
    if rc != 0:
        print(f"wrote {path} but launchctl could not load it: {(err or out).strip()}",
              file=sys.stderr)
        return 1
    hours = data["StartInterval"] / 3600
    print(f"installed {path}\nrunning `{PROG} auto` every {hours:g} h "
          f"(first run in {hours:g} h; it never runs at login).\n"
          f"log: {os.path.join(state, 'auto.log')}")
    if not a.accept_ookla_license:
        print("note: Ookla's licence was not accepted here, so the periodic deep run uses the "
              "built-in probe. Re-run with --accept-ookla-license to allow full runs.")
    return 0


def uninstall(runner: Runner = run, home: Optional[str] = None) -> int:
    path = plist_path(home)
    runner(["launchctl", "bootout", f"{_domain()}/{LABEL}"], timeout=20)
    if os.path.exists(path):
        os.remove(path)
        print(f"removed {path}")
    else:
        print("not installed")
    return 0


_STATE = re.compile(r"^\s*state = (\S+)", re.M)
_LAST_EXIT = re.compile(r"^\s*last exit code = (\S+)", re.M)
_INTERVAL = re.compile(r"^\s*run interval = (\d+) seconds", re.M)


def parse_launchctl_print(text: str) -> Dict[str, Optional[str]]:
    def first(rx: re.Pattern[str]) -> Optional[str]:
        m = rx.search(text)
        return m.group(1) if m else None
    return {"state": first(_STATE), "last_exit": first(_LAST_EXIT),
            "interval": first(_INTERVAL)}


def explain_exit(code: Optional[str]) -> str:
    if code in (None, "(never exited)"):
        return "has not run yet"
    return {"0": "ok", str(EXIT_SKIPPED): "skipped by a guard (machine busy)",
            str(EXIT_LOCKED): "skipped, another run was active"}.get(
                code, f"exit code {code} (see the log)")


def status(runner: Runner = run, home: Optional[str] = None) -> int:
    path = plist_path(home)
    if not os.path.exists(path):
        print(f"not installed. Install it with `{PROG} schedule install`.")
        return 0
    print(f"installed: {path}")
    rc, out, _ = runner(["launchctl", "print", f"{_domain()}/{LABEL}"], timeout=20)
    if rc != 0:
        print("loaded:    no (run `tsbench schedule install` to load it)")
    else:
        info = parse_launchctl_print(out)
        every = f"{int(info['interval']) / 3600:g} h" if info["interval"] else "?"
        print(f"loaded:    yes, every {every}, state {info['state'] or '?'}")
        print(f"last run:  {explain_exit(info['last_exit'])}")
    db = default_db_path()
    if os.path.exists(db):
        with Store(db) as store:
            print(f"history:   last full run {store.last_run_at('full') or 'never'}, "
                  f"last run {store.last_run_at() or 'never'}")
    log = os.path.join(state_dir(), "auto.log")
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            tail = [ln.rstrip() for ln in fh if " auto: " in ln][-3:]
        if tail:
            print("recent:")
            for ln in tail:
                print(f"  {ln}")
    return 0


def schedule_main(argv: List[str]) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "darwin":
        print(f"`{PROG} schedule` installs a launchd job and only works on macOS.\n"
              "On Linux use the systemd timer or cron example in integrations/linux/.",
              file=sys.stderr)
        return 2
    if args.action == "install":
        return install(args)
    if args.action == "uninstall":
        return uninstall()
    return status()


if __name__ == "__main__":
    sys.exit(schedule_main(sys.argv[1:]))
