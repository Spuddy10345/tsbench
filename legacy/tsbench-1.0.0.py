#!/usr/bin/env python3
"""
tsbench - benchmark Tailscale (Mullvad) exit nodes and recommend the best few.

Discovers exit nodes dynamically from `tailscale status --json`, benchmarks each
one sequentially with a real speed test, aggregates with medians, ranks with a
documented composite score, and always restores your original exit-node state.

Python 3.9+, standard library only.  See README.md for the full guide.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = "1.0.0"
PROG = "tsbench"

# --------------------------------------------------------------------------- #
# Verified-behaviour notes (checked against the versions below on 2026-09-15)
#
#   tailscale 1.102.4 (macOS/darwin), Speedtest by Ookla 1.2.0.84
#
#   * `tailscale exit-node list --filter=<country>` prints a PADDED TEXT TABLE
#     (IP / HOSTNAME / COUNTRY / CITY / STATUS) and has no --json flag.  It also
#     emits a duplicate row per country with CITY="Any".  We therefore use
#     `tailscale status --json` as the primary, machine-readable source (it
#     carries a structured .Location object and no duplicate rows), and keep the
#     table only for display and as a documented fallback parser.
#   * `tailscale set --exit-node=` REJECTS a bare short hostname
#     ("gb-lon-wg-004" -> "invalid value ...; must be IP or peer hostname").
#     It accepts a Tailscale IP or the full DNS name ("gb-lon-wg-004.mullvad.ts.net").
#     We always pass the Tailscale IP, which is unambiguous.
#   * `tailscale set --exit-node=` with an EMPTY value clears the exit node;
#     .ExitNodeStatus then becomes null.  Verified.
#   * .ExitNodeStatus.TailscaleIPs entries carry a CIDR suffix ("100.64.0.1/32");
#     strip it before comparing.
#   * Ookla JSON reports .download.bandwidth / .upload.bandwidth in BYTES PER
#     SECOND ("Machine readable formats ... use bytes as the unit of measure"),
#     so Mbps = bandwidth * 8 / 1e6.  .ping.latency/.ping.jitter are ms,
#     .packetLoss is a percentage and may be absent on some runs.
#   * librespeed-cli --json reports .download/.upload already in Mbps and .ping
#     /.jitter in ms, and reports NO packet loss.  Its default server choice is
#     not distance-optimal (it picked Amsterdam from a London egress), so pin a
#     server when using it.
#   * No elevated privileges are needed while your user is the Tailscale
#     operator (`tailscale set --operator=$USER`); otherwise `tailscale set`
#     needs sudo and tsbench will say so.
# --------------------------------------------------------------------------- #

MULLVAD_CHECK_URL = "https://am.i.mullvad.net/json"
GENERIC_CHECK_URL = "https://www.gstatic.com/generate_204"

DEFAULT_INCLUDE = "London"
DEFAULT_EXCLUDE = "Glasgow,Manchester"
DEFAULT_COUNTRY = "uk"

# Composite score weights (must sum to 1.0).  Documented in README.md.
W_DOWN, W_UP, W_LAT, W_JIT = 0.40, 0.20, 0.30, 0.10


# --------------------------------------------------------------------------- #
# Terminal / logging
# --------------------------------------------------------------------------- #

class Term:
    """Colour + in-place line rendering, disabled when not interactive."""

    def __init__(self, force_colour: Optional[bool] = None) -> None:
        tty = sys.stdout.isatty()
        if force_colour is None:
            enabled = tty and os.environ.get("NO_COLOR") is None \
                and os.environ.get("TERM", "") != "dumb"
        else:
            enabled = force_colour
        self.colour = enabled
        self.interactive = tty
        self._live = False

    def c(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.colour else text

    def bold(self, t: str) -> str:   return self.c(t, "1")
    def dim(self, t: str) -> str:    return self.c(t, "2")
    def red(self, t: str) -> str:    return self.c(t, "31")
    def green(self, t: str) -> str:  return self.c(t, "32")
    def yellow(self, t: str) -> str: return self.c(t, "33")
    def blue(self, t: str) -> str:   return self.c(t, "36")
    def mag(self, t: str) -> str:    return self.c(t, "35")

    @property
    def width(self) -> int:
        try:
            return max(60, shutil.get_terminal_size((100, 24)).columns)
        except Exception:
            return 100

    def live(self, text: str) -> None:
        """Update the current line in place (interactive) or stay quiet."""
        if not self.interactive:
            return
        self._live = True
        sys.stdout.write("\r\033[2K" + text[: self.width - 1])
        sys.stdout.flush()

    def endlive(self, final: Optional[str] = None) -> None:
        if self.interactive and self._live:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
        self._live = False
        if final is not None:
            print(final)


class Log:
    def __init__(self, term: Term, path: Optional[str], quiet: bool) -> None:
        self.term = term
        self.quiet = quiet
        self.fh = open(path, "a", encoding="utf-8") if path else None
        if self.fh:
            self.fh.write(f"\n=== {PROG} {VERSION} run {datetime.now().isoformat()} ===\n")

    def _write(self, line: str) -> None:
        if self.fh:
            self.fh.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
            self.fh.flush()

    def say(self, line: str = "") -> None:
        self.term.endlive()
        if not self.quiet:
            print(line)
        self._write(strip_ansi(line))

    def info(self, line: str) -> None:
        self.say(f"  {line}")

    def ok(self, line: str) -> None:
        self.say(f"  {self.term.green('OK')}  {line}")

    def warn(self, line: str) -> None:
        self.term.endlive()
        print(f"  {self.term.yellow('WARN')}  {line}", file=sys.stderr)
        self._write("WARN " + strip_ansi(line))

    def err(self, line: str) -> None:
        self.term.endlive()
        print(f"  {self.term.red('ERROR')} {line}", file=sys.stderr)
        self._write("ERROR " + strip_ansi(line))

    def close(self) -> None:
        if self.fh:
            self.fh.close()


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


class Fatal(Exception):
    """Unrecoverable error; message is printed and we exit non-zero."""


# --------------------------------------------------------------------------- #
# Subprocess helpers - argv lists only, never a shell string
# --------------------------------------------------------------------------- #

def run(argv: Sequence[str], timeout: Optional[float] = 60) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True,
                           timeout=timeout, shell=False)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"


def run_json(argv: Sequence[str], timeout: Optional[float] = 60) -> Any:
    rc, out, errtxt = run(argv, timeout=timeout)
    if rc != 0:
        raise Fatal(f"`{' '.join(argv)}` failed (rc={rc}): {errtxt.strip() or out.strip()}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise Fatal(f"`{' '.join(argv)}` did not return valid JSON: {exc}")


# --------------------------------------------------------------------------- #
# City / country matching
# --------------------------------------------------------------------------- #

def normalise(text: str, drop_suffix: bool = True) -> str:
    """Fold to a comparable form.

    Strips accents, lowercases, and reduces punctuation to single spaces, so
    "Sao Paulo", "SAO-PAULO" and "São Paulo" all fold to "sao paulo".

    With drop_suffix=True (the default) any ", XX" region suffix is removed as
    well, because Mullvad reports "Denver, CO" / "Raleigh, NC" and people say
    "Denver".  We also keep the suffix-retaining form around so that
    "Denver CO" still matches - see city_matches().
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    if drop_suffix:
        text = text.split(",")[0]
    text = re.sub(r"[^A-Za-z0-9]+", " ", text).strip().lower()
    return re.sub(r"\s+", " ", text)


def host_city_code(hostname: str) -> str:
    """'gb-lon-wg-001' -> 'lon'.  Documented assumption about Mullvad naming:
    <country>-<city>-<proto>-<index>.  Returns '' if it does not fit."""
    parts = hostname.split("-")
    return parts[1].lower() if len(parts) >= 2 else ""


def host_country_code(hostname: str) -> str:
    parts = hostname.split("-")
    return parts[0].lower() if len(parts) >= 2 else ""


def city_matches(node: Node, query: str) -> bool:
    """True if `query` names this node's city.

    Matching strategy, most to least specific:
      1. exact normalised city name           ("london" == "London")
      2. normalised city starts with query    ("washington dc" for "washington")
      3. Mullvad CityCode                     ("lon" == "LON")
      4. city segment of the hostname         ("lon" from "gb-lon-wg-001")

    Both the suffix-stripped and suffix-retaining folds are tried, so
    "Denver" and "Denver CO" both match Mullvad's "Denver, CO". Matching is
    deliberately anchored (equality, or a whole-word prefix) so that "York"
    does NOT match "New York, NY".
    """
    q = normalise(query)
    if not q:
        return False
    # Two folded forms: "denver" (region suffix dropped) and "denver co".
    for city in {normalise(node.city), normalise(node.city, drop_suffix=False)}:
        if city and (q == city or city.startswith(q + " ")):
            return True
    if node.city_code and q == node.city_code.lower():
        return True
    return bool(host_city_code(node.hostname)) and q == host_city_code(node.hostname)


def country_matches(node: Node, query: str) -> bool:
    """Accepts the display country ('uk'), the ISO code ('gb') or the hostname
    prefix ('gb').  Tailscale reports Country='UK' with CountryCode='GB'."""
    q = normalise(query)
    if not q:
        return True
    for cand in (node.country, node.country_code, host_country_code(node.hostname)):
        if cand and q == normalise(cand):
            return True
    return False


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

@dataclass
class Node:
    hostname: str
    dns_name: str
    ip: str
    city: str
    city_code: str
    country: str
    country_code: str
    online: bool
    node_id: str = ""
    is_baseline: bool = False

    @property
    def label(self) -> str:
        return "DIRECT (no exit node)" if self.is_baseline else self.hostname

    @property
    def target(self) -> str:
        """What we hand to `tailscale set --exit-node=` (IP: unambiguous)."""
        return self.ip


@dataclass
class RunResult:
    rep: int
    ok: bool
    started_at: str
    duration_s: float
    download_mbps: Optional[float] = None
    upload_mbps: Optional[float] = None
    latency_ms: Optional[float] = None
    jitter_ms: Optional[float] = None
    packet_loss_pct: Optional[float] = None
    server: Optional[str] = None
    server_id: Optional[int] = None
    egress_ip: Optional[str] = None
    egress_host: Optional[str] = None
    error: Optional[str] = None
    raw_file: Optional[str] = None


@dataclass
class NodeResult:
    node: Node
    connected: bool = False
    verified: bool = False
    connect_error: Optional[str] = None
    runs: List[RunResult] = field(default_factory=list)
    # aggregates
    successes: int = 0
    failures: int = 0
    med_down: Optional[float] = None
    med_up: Optional[float] = None
    med_lat: Optional[float] = None
    med_jit: Optional[float] = None
    loss_pct: Optional[float] = None
    score: Optional[float] = None
    status: str = "pending"          # ok | inconclusive | failed
    rank: Optional[int] = None
    recommended: bool = False

    def aggregate(self, min_success: int) -> None:
        good = [r for r in self.runs if r.ok]
        self.successes = len(good)
        self.failures = len(self.runs) - len(good)

        def med(attr: str) -> Optional[float]:
            vals = [getattr(r, attr) for r in good if getattr(r, attr) is not None]
            return round(statistics.median(vals), 3) if vals else None

        self.med_down = med("download_mbps")
        self.med_up = med("upload_mbps")
        self.med_lat = med("latency_ms")
        self.med_jit = med("jitter_ms")
        self.loss_pct = med("packet_loss_pct")

        if not self.connected and not self.node.is_baseline:
            self.status = "failed"
        elif self.successes == 0:
            self.status = "failed"
        elif self.successes < min_success or self.med_down is None or self.med_lat is None:
            self.status = "inconclusive"
        else:
            self.status = "ok"


# --------------------------------------------------------------------------- #
# Tailscale interface
# --------------------------------------------------------------------------- #

class Tailscale:
    def __init__(self, exe: str, log: Log) -> None:
        self.exe = exe
        self.log = log

    def status(self) -> Dict[str, Any]:
        return run_json([self.exe, "status", "--json"], timeout=30)

    def backend_state(self) -> str:
        return str(self.status().get("BackendState", "Unknown"))

    # -- discovery ---------------------------------------------------------- #

    def discover(self) -> List[Node]:
        """Primary discovery: structured peers from `tailscale status --json`."""
        st = self.status()
        nodes: List[Node] = []
        for peer in (st.get("Peer") or {}).values():
            if not peer.get("ExitNodeOption"):
                continue
            loc = peer.get("Location") or {}
            ips = peer.get("TailscaleIPs") or []
            ip4 = next((i for i in ips if ":" not in i), ips[0] if ips else "")
            if not ip4:
                continue
            nodes.append(Node(
                hostname=peer.get("HostName") or "",
                dns_name=(peer.get("DNSName") or "").rstrip("."),
                ip=ip4.split("/")[0],
                city=loc.get("City") or "",
                city_code=loc.get("CityCode") or "",
                country=loc.get("Country") or "",
                country_code=loc.get("CountryCode") or "",
                online=bool(peer.get("Online")),
                node_id=peer.get("ID") or "",
            ))
        return nodes

    def exit_node_table(self, country: str) -> Tuple[int, str]:
        rc, out, errtxt = run(
            [self.exe, "exit-node", "list", f"--filter={country}"], timeout=45)
        return rc, out if rc == 0 else (errtxt or out)

    def discover_from_table(self, country: str) -> List[Node]:
        """Documented fallback: parse the padded text table by header column
        offsets (not by splitting on whitespace, because city names such as
        'Denver, CO' and 'Hong Kong' contain spaces).  Only used when
        `status --json` carried no Location data for any exit node."""
        rc, out = self.exit_node_table(country)
        if rc != 0:
            raise Fatal(f"`tailscale exit-node list --filter={country}` failed: {out.strip()}")
        lines = [ln for ln in out.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        hdr_i = next((i for i, ln in enumerate(lines)
                      if "HOSTNAME" in ln and "CITY" in ln), None)
        if hdr_i is None:
            raise Fatal("could not locate the header row of the exit-node table")
        hdr = lines[hdr_i]
        cols = ["IP", "HOSTNAME", "COUNTRY", "CITY", "STATUS"]
        starts = []
        for name in cols:
            at = hdr.find(name)
            if at < 0:
                raise Fatal(f"exit-node table has no {name!r} column; "
                            f"refusing to guess. Header was: {hdr.strip()!r}")
            starts.append(at)
        bounds = list(zip(starts, starts[1:] + [10 ** 6]))

        seen: Dict[str, Node] = {}
        for ln in lines[hdr_i + 1:]:
            cell = [ln[a:b].strip() for a, b in bounds]
            ip, host, ctry, city, _status = cell
            if not ip or not host:
                continue
            short = host.split(".")[0]
            # The table repeats one node per country with CITY="Any"; prefer the
            # row that names a real city.
            if short in seen and normalise(city) == "any":
                continue
            seen[short] = Node(
                hostname=short,
                dns_name=host,
                ip=ip,
                city="" if normalise(city) == "any" else city,
                city_code=host_city_code(short).upper(),
                country=ctry,
                country_code="",
                online=True,          # the table does not report liveness
                node_id="",
            )
        return list(seen.values())

    # -- current state ------------------------------------------------------ #

    def current_exit_ip(self) -> Optional[str]:
        """Active exit node's Tailscale IP, or None for direct connectivity."""
        st = self.status()
        ens = st.get("ExitNodeStatus")
        if isinstance(ens, dict):
            for raw in (ens.get("TailscaleIPs") or []):
                ip = raw.split("/")[0]
                if ":" not in ip:
                    return ip
        for peer in (st.get("Peer") or {}).values():
            if peer.get("ExitNode"):
                for ip in (peer.get("TailscaleIPs") or []):
                    if ":" not in ip:
                        return ip.split("/")[0]
        return None

    def hostname_for_ip(self, ip: str) -> str:
        for peer in (self.status().get("Peer") or {}).values():
            for pip in (peer.get("TailscaleIPs") or []):
                if pip.split("/")[0] == ip:
                    return peer.get("HostName") or ip
        return ip

    # -- mutation ----------------------------------------------------------- #

    def set_exit_node(self, target: str) -> Tuple[bool, str]:
        """target='' clears the exit node.  Only --exit-node is touched, so
        other preferences (including --exit-node-allow-lan-access) are left
        exactly as the user had them."""
        rc, out, errtxt = run([self.exe, "set", f"--exit-node={target}"], timeout=45)
        if rc == 0:
            return True, ""
        msg = (errtxt or out).strip() or f"rc={rc}"
        if "access denied" in msg.lower() or "permission" in msg.lower():
            msg += ("\n        Your user is not the Tailscale operator. Run "
                    "`sudo tailscale set --operator=$USER` once, or run tsbench under sudo.")
        return False, msg


# --------------------------------------------------------------------------- #
# Egress verification
# --------------------------------------------------------------------------- #

def http_json(url: str, timeout: float) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{PROG}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def http_reachable(url: str, timeout: float) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{PROG}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500
    except (urllib.error.URLError, OSError):
        return False


def probe_egress(timeout: float) -> Tuple[Optional[str], Optional[str], bool]:
    """(exit_hostname, egress_ip, internet_ok).

    Uses Mullvad's own JSON endpoint, which names the exact exit server, so we
    can verify we really left the tunnel where we intended.  Falls back to a
    plain reachability probe for non-Mullvad nodes or if the endpoint is down.
    """
    data = http_json(MULLVAD_CHECK_URL, timeout)
    if isinstance(data, dict):
        host = data.get("mullvad_exit_ip_hostname") or None
        return (str(host) if host else None), (data.get("ip") or None), True
    return None, None, http_reachable(GENERIC_CHECK_URL, timeout)


# --------------------------------------------------------------------------- #
# Speed-test back ends
# --------------------------------------------------------------------------- #

class SpeedTester:
    name = "abstract"
    units_note = ""

    def __init__(self, exe: str, extra: Sequence[str] = ()) -> None:
        self.exe = exe
        self.extra = list(extra)

    def command(self, server_id: Optional[int]) -> List[str]:
        raise NotImplementedError

    def parse(self, stdout: str) -> Dict[str, Any]:
        raise NotImplementedError


class OoklaTester(SpeedTester):
    name = "ookla"
    units_note = ("Ookla JSON reports bandwidth in bytes/s (converted here with "
                  "*8/1e6 to Mbps); latency/jitter in ms; packetLoss in %.")

    def command(self, server_id: Optional[int]) -> List[str]:
        cmd = [self.exe, "--format=json", "--accept-license", "--accept-gdpr",
               "--precision=4"]
        if server_id is not None:
            cmd.append(f"--server-id={server_id}")
        return cmd + self.extra

    def parse(self, stdout: str) -> Dict[str, Any]:
        d = json.loads(stdout)
        if d.get("type") == "log" or "download" not in d:
            raise ValueError(d.get("message") or "no download result in JSON")
        ping = d.get("ping") or {}
        srv = d.get("server") or {}
        loss = d.get("packetLoss")
        return {
            "download_mbps": round(float(d["download"]["bandwidth"]) * 8 / 1e6, 3),
            "upload_mbps": round(float(d["upload"]["bandwidth"]) * 8 / 1e6, 3),
            "latency_ms": _f(ping.get("latency")),
            "jitter_ms": _f(ping.get("jitter")),
            "packet_loss_pct": _f(loss),
            "server": f"{srv.get('name','?')} ({srv.get('location','?')})",
            "server_id": int(srv["id"]) if srv.get("id") is not None else None,
            "raw": d,
        }


class LibrespeedTester(SpeedTester):
    name = "librespeed"
    units_note = ("librespeed-cli --json reports download/upload already in "
                  "Mbps and ping/jitter in ms. It reports NO packet loss, so "
                  "the packet-loss column stays empty and contributes nothing "
                  "to the score.")

    def command(self, server_id: Optional[int]) -> List[str]:
        cmd = [self.exe, "--json"]
        if server_id is not None:
            cmd += ["--server", str(server_id)]
        return cmd + self.extra

    def parse(self, stdout: str) -> Dict[str, Any]:
        d = json.loads(stdout)
        if isinstance(d, list):
            if not d:
                raise ValueError("librespeed-cli returned an empty JSON array")
            d = d[0]
        srv = d.get("server") or {}
        return {
            "download_mbps": _f(d.get("download")),
            "upload_mbps": _f(d.get("upload")),
            "latency_ms": _f(d.get("ping")),
            "jitter_ms": _f(d.get("jitter")),
            "packet_loss_pct": None,
            "server": str(srv.get("name", "?")),
            "server_id": None,
            "raw": d,
        }


TESTERS = {"ookla": (OoklaTester, "speedtest"),
           "librespeed": (LibrespeedTester, "librespeed-cli")}


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else round(float(v), 3)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def score_nodes(results: List[NodeResult], reps: int) -> None:
    """Composite score, 0-100.  Documented in README.md.

        raw   = 0.40*D + 0.20*U + 0.30*L + 0.10*J
          D = med_down / best_med_down          (higher is better)
          U = med_up   / best_med_up
          L = best_med_lat / med_lat            (lower is better)
          J = best_med_jit / med_jit            (lower is better)
        score = 100 * raw * reliability * loss_factor
          reliability = successful_runs / attempted_runs
          loss_factor = max(0, 1 - median_packet_loss_pct / 10)

    Normalisation pool excludes the DIRECT baseline (it is a reference, not a
    candidate) and excludes nodes whose status is not 'ok', so one dead node
    cannot flatten everyone else's numbers.  Missing metrics score 0 for their
    term rather than being silently dropped.
    """
    pool = [r for r in results if r.status == "ok" and not r.node.is_baseline]
    if not pool:
        return

    def best(attr: str, lower_better: bool) -> Optional[float]:
        vals = [getattr(r, attr) for r in pool if getattr(r, attr) is not None]
        vals = [v for v in vals if v > 0]
        if not vals:
            return None
        return min(vals) if lower_better else max(vals)

    b_down = best("med_down", False)
    b_up = best("med_up", False)
    b_lat = best("med_lat", True)
    b_jit = best("med_jit", True)

    for r in pool:
        def frac(val: Optional[float], ref: Optional[float], lower_better: bool) -> float:
            if val is None or ref is None or val <= 0:
                return 0.0
            return min(1.0, ref / val) if lower_better else min(1.0, val / ref)

        raw = (W_DOWN * frac(r.med_down, b_down, False)
               + W_UP * frac(r.med_up, b_up, False)
               + W_LAT * frac(r.med_lat, b_lat, True)
               + W_JIT * frac(r.med_jit, b_jit, True))
        attempted = max(1, len(r.runs))
        reliability = r.successes / attempted
        loss = r.loss_pct or 0.0
        loss_factor = max(0.0, 1.0 - loss / 10.0)
        r.score = round(100.0 * raw * reliability * loss_factor, 2)

    ranked = sorted(pool, key=lambda r: (-(r.score or 0.0), r.node.hostname))
    for i, r in enumerate(ranked, 1):
        r.rank = i


# --------------------------------------------------------------------------- #
# Benchmark engine
# --------------------------------------------------------------------------- #

class Benchmark:
    def __init__(self, args: argparse.Namespace, ts: Tailscale, tester: SpeedTester,
                 term: Term, log: Log, run_dir: str) -> None:
        self.a = args
        self.ts = ts
        self.tester = tester
        self.term = term
        self.log = log
        self.run_dir = run_dir
        self.original_ip: Optional[str] = None
        self.original_label = "unknown"
        self.restored = False
        self.restored_to = "unknown"
        self.pinned_server: Optional[int] = args.server_id
        self.interrupted = False

    # -- state handling ----------------------------------------------------- #

    def capture_original(self) -> None:
        self.original_ip = self.ts.current_exit_ip()
        self.original_label = (f"{self.ts.hostname_for_ip(self.original_ip)} "
                               f"({self.original_ip})") if self.original_ip \
            else "none (direct connectivity)"
        self.log.info(f"Original exit node: {self.term.bold(self.original_label)}")

    def restore(self, to_ip: Optional[str] = None, why: str = "") -> None:
        """Idempotent.  Called from the finally block and from signal handlers."""
        if self.restored:
            return
        target_ip = self.original_ip if to_ip is None else to_ip
        target = target_ip or ""
        desc = (f"{self.ts.hostname_for_ip(target_ip)} ({target_ip})"
                if target_ip else "none (direct connectivity)")
        self.term.endlive()
        self.log.say(f"\n{self.term.blue('->')} Restoring exit node to {self.term.bold(desc)}"
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
                self.log.warn(f"After restore, active exit node is {now or 'none'}, "
                              f"expected {desc}. Check `tailscale status`.")
        else:
            self.restored_to = f"FAILED to restore to {desc}"
            self.log.err(f"Could not restore exit node: {err}")
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

    def speed_test(self, node: Node, rep: int, idx: int, total: int) -> RunResult:
        prefix = f"[{idx}/{total}] {node.label}  rep {rep}/{self.a.reps}"
        cmd = self.tester.command(self.pinned_server)
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        self.term.live(f"{prefix}  running {self.tester.name} speed test ...")
        rc, out, errtxt = run(cmd, timeout=self.a.test_timeout)
        dur = round(time.monotonic() - t0, 2)
        res = RunResult(rep=rep, ok=False, started_at=started.isoformat(timespec="seconds"),
                        duration_s=dur)

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
        res.ok = res.download_mbps is not None
        if not res.ok:
            res.error = "speed test returned no download figure"

        if res.ok and self.a.pin_server and self.pinned_server is None \
                and res.server_id is not None:
            self.pinned_server = res.server_id
            self.log.info(f"Pinned speed-test server for the rest of this run: "
                          f"{res.server} id={res.server_id}")

        if self.a.keep_raw and raw is not None:
            raw_dir = os.path.join(self.run_dir, "raw")
            os.makedirs(raw_dir, exist_ok=True)
            fname = f"{node.hostname or 'direct'}-rep{rep}.json"
            path = os.path.join(raw_dir, fname)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, indent=2)
            res.raw_file = os.path.join("raw", fname)
        return res

    # -- main loop ---------------------------------------------------------- #

    def run_all(self, nodes: List[Node]) -> List[NodeResult]:
        results: List[NodeResult] = []
        total = len(nodes)
        min_success = max(2, math.ceil(self.a.reps / 2)) if self.a.reps > 1 else 1

        for idx, node in enumerate(nodes, 1):
            if self.interrupted:
                break
            nr = NodeResult(node=node)
            results.append(nr)

            connected, verified, err = self.connect(node, idx, total)
            nr.connected, nr.verified, nr.connect_error = connected, verified, err

            if not connected:
                self.term.endlive()
                self.log.say(f"  {self.term.red('FAIL')} [{idx}/{total}] "
                             f"{self.term.bold(node.label)}: {err}")
                nr.aggregate(min_success)
                if self.a.fail_fast:
                    raise Fatal(f"--fail-fast: {node.label} could not be activated: {err}")
                continue

            tag = self.term.green("verified") if verified else self.term.yellow("unverified egress")
            self.log.say(f"  {self.term.green('UP')}   [{idx}/{total}] "
                         f"{self.term.bold(node.label)} active ({tag})")

            for rep in range(1, self.a.reps + 1):
                if self.interrupted:
                    break
                r = self.speed_test(node, rep, idx, total)
                if r.ok:
                    host, eip, _ = (None, None, True) if self.a.no_egress_check \
                        else probe_egress(4.0)
                    r.egress_host, r.egress_ip = host, eip
                nr.runs.append(r)
                self.term.endlive()
                if r.ok:
                    self.log.say(
                        f"       rep {rep}/{self.a.reps}: "
                        f"{self.term.bold(f'{r.download_mbps:.1f}')} Mbps down / "
                        f"{r.upload_mbps:.1f} Mbps up / "
                        f"{fmt(r.latency_ms)} ms lat / {fmt(r.jitter_ms)} ms jit"
                        + (f" / {r.packet_loss_pct:.1f}% loss" if r.packet_loss_pct else "")
                        + self.term.dim(f"  [{r.duration_s}s]"))
                else:
                    self.log.say(f"       rep {rep}/{self.a.reps}: "
                                 f"{self.term.red('failed')} - {r.error}")
                if rep < self.a.reps:
                    time.sleep(max(0.0, self.a.rep_delay))

            nr.aggregate(min_success)
            if nr.status == "ok":
                self.log.say(f"       {self.term.blue('median')}: "
                             f"{nr.med_down:.1f} / {nr.med_up:.1f} Mbps, "
                             f"{fmt(nr.med_lat)} ms, {nr.successes}/{len(nr.runs)} ok")
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


def fmt(v: Optional[float], nd: int = 1) -> str:
    return "-" if v is None else f"{v:.{nd}f}"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

TABLE_COLS = [
    ("Rank", 5, "r"), ("Exit node", 22, "l"), ("City", 12, "l"), ("OK", 5, "r"),
    ("Down", 9, "r"), ("Up", 9, "r"), ("Lat", 8, "r"), ("Jit", 7, "r"),
    ("Loss", 6, "r"), ("Score", 7, "r"), ("Status", 17, "l"),
]


def row_cells(r: NodeResult, attempted: int) -> List[str]:
    if r.node.is_baseline:
        rank, status = "--", "baseline (ref)"
    elif r.status == "ok":
        rank = str(r.rank or "-")
        status = "RECOMMENDED" if r.recommended else "tested"
    elif r.status == "inconclusive":
        rank, status = "--", "inconclusive"
    else:
        rank, status = "--", "failed"
    return [
        rank, r.node.label, (r.node.city or "-"),
        f"{r.successes}/{attempted}",
        fmt(r.med_down), fmt(r.med_up), fmt(r.med_lat), fmt(r.med_jit),
        "-" if r.loss_pct is None else f"{r.loss_pct:.1f}",
        "-" if r.score is None else f"{r.score:.1f}",
        status,
    ]


def render_table(results: List[NodeResult], attempted: int, term: Term) -> str:
    widths = []
    for i, (head, minw, _) in enumerate(TABLE_COLS):
        longest = max([len(head)] + [len(row_cells(r, attempted)[i]) for r in results])
        widths.append(max(minw, longest))

    def line(cells: List[str], colourise=None) -> str:
        out = []
        for (head, _, align), w, cell in zip(TABLE_COLS, widths, cells):
            txt = cell.rjust(w) if align == "r" else cell.ljust(w)
            out.append(colourise(txt, cell) if colourise else txt)
        return "  ".join(out).rstrip()

    heads = [h for h, _, _ in TABLE_COLS]
    sep = "  ".join("-" * w for w in widths)
    body = [term.bold(line(heads)), term.dim(sep)]

    def paint(txt: str, cell: str) -> str:
        if cell == "RECOMMENDED":
            return term.green(txt)
        if cell == "failed":
            return term.red(txt)
        if cell in ("inconclusive", "baseline (ref)"):
            return term.yellow(txt)
        return txt

    order = (sorted([r for r in results if r.status == "ok" and not r.node.is_baseline],
                    key=lambda r: r.rank or 999)
             + [r for r in results if r.node.is_baseline]
             + [r for r in results if r.status == "inconclusive"]
             + [r for r in results if r.status == "failed"])
    for r in order:
        body.append(line(row_cells(r, attempted), paint))
    return "\n".join(body)


def write_outputs(run_dir: str, results: List[NodeResult], meta: Dict[str, Any],
                  recommended: List[NodeResult], term: Term) -> Dict[str, str]:
    attempted = int(meta["reps"])
    paths = {}

    # JSON -------------------------------------------------------------------
    payload = {
        "tool": PROG, "version": VERSION, "meta": meta,
        "scoring": {
            "formula": ("score = 100 * (0.40*D + 0.20*U + 0.30*L + 0.10*J) "
                        "* reliability * loss_factor"),
            "terms": {
                "D": "median_download / best_median_download (higher better)",
                "U": "median_upload / best_median_upload (higher better)",
                "L": "best_median_latency / median_latency (lower better)",
                "J": "best_median_jitter / median_jitter (lower better)",
                "reliability": "successful_runs / attempted_runs",
                "loss_factor": "max(0, 1 - median_packet_loss_pct / 10)",
            },
            "units": {"download": "Mbps", "upload": "Mbps", "latency": "ms",
                      "jitter": "ms", "packet_loss": "percent", "score": "0-100"},
            "normalisation_pool": ("nodes with status 'ok'; the DIRECT baseline is "
                                   "excluded (reference only)"),
        },
        "results": [],
    }
    for r in results:
        payload["results"].append({
            "hostname": r.node.hostname, "label": r.node.label,
            "dns_name": r.node.dns_name, "ip": r.node.ip,
            "city": r.node.city, "city_code": r.node.city_code,
            "country": r.node.country, "is_baseline": r.node.is_baseline,
            "connected": r.connected, "egress_verified": r.verified,
            "connect_error": r.connect_error,
            "status": r.status, "rank": r.rank, "recommended": r.recommended,
            "successful_runs": r.successes, "failed_runs": r.failures,
            "attempted_runs": len(r.runs),
            "median_download_mbps": r.med_down, "median_upload_mbps": r.med_up,
            "median_latency_ms": r.med_lat, "median_jitter_ms": r.med_jit,
            "median_packet_loss_pct": r.loss_pct, "composite_score": r.score,
            "runs": [asdict(x) for x in r.runs],
        })
    p = os.path.join(run_dir, "results.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    paths["json"] = p

    # CSV --------------------------------------------------------------------
    p = os.path.join(run_dir, "results.csv")
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "hostname", "city", "ip", "is_baseline", "status",
                    "recommended", "successful_runs", "attempted_runs",
                    "median_download_mbps", "median_upload_mbps",
                    "median_latency_ms", "median_jitter_ms",
                    "median_packet_loss_pct", "composite_score",
                    "connect_error"])
        for r in results:
            w.writerow([r.rank or "", r.node.hostname, r.node.city, r.node.ip,
                        r.node.is_baseline, r.status, r.recommended,
                        r.successes, len(r.runs), r.med_down, r.med_up,
                        r.med_lat, r.med_jit, r.loss_pct, r.score,
                        r.connect_error or ""])
    paths["csv"] = p

    # per-run CSV ------------------------------------------------------------
    p = os.path.join(run_dir, "runs.csv")
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["hostname", "city", "rep", "ok", "started_at", "duration_s",
                    "download_mbps", "upload_mbps", "latency_ms", "jitter_ms",
                    "packet_loss_pct", "server", "server_id", "egress_host",
                    "egress_ip", "error"])
        for r in results:
            for x in r.runs:
                w.writerow([r.node.hostname or "direct", r.node.city, x.rep, x.ok,
                            x.started_at, x.duration_s, x.download_mbps,
                            x.upload_mbps, x.latency_ms, x.jitter_ms,
                            x.packet_loss_pct, x.server, x.server_id,
                            x.egress_host, x.egress_ip, x.error or ""])
    paths["runs_csv"] = p

    # Markdown ---------------------------------------------------------------
    plain = Term(force_colour=False)
    md = [f"# {PROG} report", ""]
    md.append(f"- **Run started:** {meta['started_at']}")
    md.append(f"- **Duration:** {meta['duration_s']}s")
    md.append(f"- **Cities included:** {', '.join(meta['include_cities'])}")
    md.append(f"- **Cities excluded:** {', '.join(meta['exclude_cities']) or '(none)'}")
    md.append(f"- **Speed test:** {meta['tester']} — {meta['units_note']}")
    md.append(f"- **Repetitions per node:** {meta['reps']}")
    md.append(f"- **Pinned server:** {meta.get('pinned_server') or 'auto (per test)'}")
    md.append(f"- **Nodes benchmarked:** {meta['nodes_benchmarked']}")
    md.append(f"- **Original exit node:** {meta['original_exit_node']}")
    md.append(f"- **Restored to:** {meta['restored_to']}")
    md += ["", "## Ranked results", "",
           "Download/upload in Mbps, latency/jitter in ms, loss in %, score 0-100.", ""]
    md.append("| " + " | ".join(h for h, _, _ in TABLE_COLS) + " |")
    md.append("|" + "|".join("---" for _ in TABLE_COLS) + "|")
    order = (sorted([r for r in results if r.status == "ok" and not r.node.is_baseline],
                    key=lambda r: r.rank or 999)
             + [r for r in results if r.node.is_baseline]
             + [r for r in results if r.status == "inconclusive"]
             + [r for r in results if r.status == "failed"])
    for r in order:
        md.append("| " + " | ".join(row_cells(r, attempted)) + " |")

    md += ["", "## Recommendation", ""]
    if recommended:
        base = next((r for r in results if r.node.is_baseline and r.status == "ok"), None)
        for i, r in enumerate(recommended, 1):
            extra = ""
            if base and base.med_down:
                extra = (f" — {100.0 * (r.med_down or 0) / base.med_down:.0f}% of the "
                         f"no-exit-node baseline download")
            md.append(f"{i}. **`{r.node.dns_name or r.node.hostname}`** "
                      f"(score {r.score:.1f}) — {fmt(r.med_down)} Mbps down, "
                      f"{fmt(r.med_up)} Mbps up, {fmt(r.med_lat)} ms latency{extra}")
        md += ["", "Connect to the top node with:", "",
               "```sh", f"tailscale set --exit-node={recommended[0].node.ip}  "
               f"# {recommended[0].node.hostname}", "```"]
    else:
        md.append("_No node produced enough valid data to be recommended._")

    incon = [r for r in results if r.status == "inconclusive"]
    failed = [r for r in results if r.status == "failed"]
    tested = [r for r in results if r.status == "ok" and not r.node.is_baseline
              and not r.recommended]
    if tested:
        md += ["", "## Tested but not recommended", ""]
        md += [f"- `{r.node.hostname}` — rank {r.rank}, score {r.score:.1f}" for r in tested]
    if incon:
        md += ["", "## Inconclusive (insufficient valid data)", ""]
        md += [f"- `{r.node.hostname}` — {r.successes}/{len(r.runs)} successful runs"
               for r in incon]
    if failed:
        md += ["", "## Failed", ""]
        md += [f"- `{r.node.hostname or 'DIRECT'}` — "
               f"{r.connect_error or 'all speed tests failed'}" for r in failed]

    md += ["", "## Scoring method", "",
           "```", payload["scoring"]["formula"], "```", ""]
    for k, v in payload["scoring"]["terms"].items():
        md.append(f"- `{k}` = {v}")
    md += ["",
           "Medians are used rather than means so a single stalled transfer cannot "
           "dominate a node's figures. Nodes with fewer than "
           f"{meta['min_success']} successful runs are marked *inconclusive* and are "
           "left unranked rather than being given a misleading position. The DIRECT "
           "baseline is measured for reference and is deliberately excluded from "
           "both ranking and score normalisation.", ""]
    p = os.path.join(run_dir, "summary.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    paths["markdown"] = p
    return paths


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Benchmark Tailscale (Mullvad) exit nodes and recommend the best few.",
        epilog="""\
examples:
  tsbench.py                                 # London only, 3 reps, + DIRECT baseline
  tsbench.py --dry-run                       # just show which nodes would be tested
  tsbench.py --cities London,Edinburgh       # custom city selection
  tsbench.py --max-nodes 5                   # limit the run
  tsbench.py --reps 5 --switch-delay 5       # more repetitions, slower switching
  tsbench.py --apply-best                    # stay connected to the winner
  tsbench.py --no-baseline --tester librespeed
""")
    g = p.add_argument_group("selection")
    g.add_argument("--cities", default=DEFAULT_INCLUDE, metavar="LIST",
                   help=f"comma-separated cities to include (default: {DEFAULT_INCLUDE})")
    g.add_argument("--exclude-cities", default=DEFAULT_EXCLUDE, metavar="LIST",
                   help=f"comma-separated cities to exclude (default: {DEFAULT_EXCLUDE})")
    g.add_argument("--country", default=DEFAULT_COUNTRY, metavar="NAME",
                   help=f"country filter passed to `tailscale exit-node list` and matched "
                        f"against node metadata (default: {DEFAULT_COUNTRY})")
    g.add_argument("--max-nodes", type=int, default=None, metavar="N",
                   help="test at most N nodes (default: all matching)")
    g.add_argument("--include-offline", action="store_true",
                   help="also test nodes Tailscale reports as offline")

    g = p.add_argument_group("benchmark")
    g.add_argument("--reps", type=int, default=3, metavar="N",
                   help="speed-test repetitions per node (default: 3)")
    g.add_argument("--tester", choices=sorted(TESTERS), default="ookla",
                   help="speed-test implementation (default: ookla)")
    g.add_argument("--tester-path", default=None, metavar="PATH",
                   help="explicit path to the speed-test binary")
    g.add_argument("--server-id", type=int, default=None, metavar="ID",
                   help="pin every test to this speed-test server id")
    g.add_argument("--no-pin-server", dest="pin_server", action="store_false",
                   help="let each test pick its own server (default: pin to the first "
                        "server used, so nodes are compared against the same target)")
    g.add_argument("--baseline", dest="baseline", action="store_true", default=True,
                   help="measure a no-exit-node DIRECT reference first (default: on)")
    g.add_argument("--no-baseline", dest="baseline", action="store_false",
                   help="skip the DIRECT reference measurement")
    g.add_argument("--switch-delay", type=float, default=3.0, metavar="SEC",
                   help="settle time after finishing a node (default: 3)")
    g.add_argument("--rep-delay", type=float, default=1.0, metavar="SEC",
                   help="pause between repetitions on one node (default: 1)")
    g.add_argument("--ready-timeout", type=float, default=30.0, metavar="SEC",
                   help="how long to wait for a node to become usable (default: 30)")
    g.add_argument("--test-timeout", type=float, default=180.0, metavar="SEC",
                   help="per-speed-test timeout (default: 180)")
    g.add_argument("--no-egress-check", action="store_true",
                   help="do not verify real egress via am.i.mullvad.net")
    g.add_argument("--sequential-only", action="store_true", default=True,
                   help="run one node at a time (always enforced; see --parallel)")
    g.add_argument("--parallel", action="store_true",
                   help="refused: every test shares one local link, so parallel runs "
                        "would measure contention, not node quality")

    g = p.add_argument_group("behaviour")
    g.add_argument("--dry-run", action="store_true",
                   help="discover and display matching nodes, then exit")
    g.add_argument("--fail-fast", action="store_true",
                   help="stop on the first node that cannot be activated "
                        "(default: record it as failed and continue)")
    g.add_argument("--apply-best", action="store_true",
                   help="finish connected to the top-ranked node instead of restoring "
                        "the original exit node")
    g.add_argument("--keep-raw", action="store_true",
                   help="save each raw speed-test JSON under <run-dir>/raw/")

    g = p.add_argument_group("output")
    g.add_argument("--out-dir", default="./tsbench-results", metavar="DIR",
                   help="parent directory for run directories (default: ./tsbench-results)")
    g.add_argument("--run-name", default=None, metavar="NAME",
                   help="run directory name (default: run-YYYYmmdd-HHMMSS)")
    g.add_argument("--force", action="store_true",
                   help="allow overwriting an existing run directory")
    g.add_argument("--log-file", default=None, metavar="PATH",
                   help="append a plain-text log here")
    g.add_argument("--colour", dest="colour", action="store_true", default=None,
                   help="force colour on")
    g.add_argument("--no-colour", dest="colour", action="store_false",
                   help="force colour off")
    g.add_argument("--quiet", action="store_true", help="only print the final report")
    g.add_argument("--version", action="version", version=f"{PROG} {VERSION}")
    return p


def split_list(s: str) -> List[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def preflight(args: argparse.Namespace, term: Term, log: Log) -> Tuple[Tailscale, SpeedTester]:
    if sys.version_info < (3, 9):
        raise Fatal(f"Python 3.9+ required, running {sys.version.split()[0]}")

    ts_exe = shutil.which("tailscale")
    if not ts_exe:
        raise Fatal("`tailscale` not found on PATH.\n"
                    "        macOS (Homebrew):  brew install tailscale\n"
                    "        macOS (App Store): ln -s /Applications/Tailscale.app/Contents/"
                    "MacOS/Tailscale /usr/local/bin/tailscale")
    ts = Tailscale(ts_exe, log)

    rc, out, _ = run([ts_exe, "version"], timeout=15)
    ts_ver = out.splitlines()[0].strip() if rc == 0 and out.strip() else "unknown"

    try:
        state = ts.backend_state()
    except Fatal as exc:
        raise Fatal(f"could not query Tailscale: {exc}\n"
                    "        Is the daemon running and are you logged in? Try `tailscale status`.")
    if state != "Running":
        raise Fatal(f"Tailscale backend state is {state!r}, expected 'Running'.\n"
                    "        Run `tailscale up` and make sure this machine is authenticated.")

    cls, default_exe = TESTERS[args.tester]
    st_exe = args.tester_path or shutil.which(default_exe)
    if not st_exe or not os.access(st_exe, os.X_OK):
        hint = {
            "ookla": ("brew tap teamookla/speedtest && brew install speedtest\n"
                      "        (Linux/other: https://www.speedtest.net/apps/cli)\n"
                      "        Note: the `speedtest-cli` Python package and Debian's\n"
                      "        `speedtest-cli` are DIFFERENT tools with different JSON;\n"
                      "        they are not accepted here."),
            "librespeed": "brew install librespeed-cli",
        }[args.tester]
        raise Fatal(f"selected speed test {args.tester!r} needs {default_exe!r}, "
                    f"which was not found.\n        Install it with:\n        {hint}\n"
                    "        tsbench will not silently substitute a different tool.")
    tester = cls(st_exe)

    rc, out, errtxt = run([st_exe, "--version"], timeout=20)
    st_ver = (out or errtxt).splitlines()[0].strip() if (out or errtxt) else "unknown"

    log.info(f"tailscale: {term.dim(ts_ver)}")
    log.info(f"speed test: {term.dim(st_ver)}  [{tester.name}]")
    log.info(f"units: {term.dim(tester.units_note)}")
    args._ts_version, args._st_version = ts_ver, st_ver
    return ts, tester


def select_nodes(ts: Tailscale, args: argparse.Namespace, term: Term,
                 log: Log) -> List[Node]:
    include = split_list(args.cities)
    exclude = split_list(args.exclude_cities)
    if not include:
        raise Fatal("--cities is empty; nothing to benchmark")

    all_nodes = ts.discover()
    source = "tailscale status --json"
    if all_nodes and not any(n.city or n.city_code for n in all_nodes):
        log.warn("status --json carried no Location data; falling back to parsing "
                 "`tailscale exit-node list`")
        all_nodes = ts.discover_from_table(args.country)
        source = f"tailscale exit-node list --filter={args.country}"
    if not all_nodes:
        rc, raw = ts.exit_node_table(args.country)
        raise Fatal("no exit nodes are advertised to this tailnet.\n"
                    f"        Raw `tailscale exit-node list --filter={args.country}` output:\n"
                    + "\n".join("        " + l for l in raw.splitlines()[:25]))

    in_country = [n for n in all_nodes if country_matches(n, args.country)]
    chosen: List[Node] = []
    for n in in_country:
        if any(city_matches(n, c) for c in exclude):
            continue
        if any(city_matches(n, c) for c in include):
            chosen.append(n)
    chosen.sort(key=lambda n: (normalise(n.city), n.hostname))

    log.say()
    log.say(f"{term.bold('Discovery')} {term.dim('(' + source + ')')}")
    log.info(f"exit nodes advertised: {len(all_nodes)}   "
             f"in country {args.country!r}: {len(in_country)}")
    log.info(f"include cities: {', '.join(include)}")
    log.info(f"exclude cities: {', '.join(exclude) or '(none)'}")

    if not chosen:
        cities = sorted({n.city for n in in_country if n.city}) or ["(none reported)"]
        rc, raw = ts.exit_node_table(args.country)
        log.say()
        log.err(f"no exit nodes matched cities {include!r} in country {args.country!r}.")
        log.say(f"  Cities actually available in {args.country!r}: {', '.join(cities)}")
        usable = [c for c in cities
                  if c != "(none reported)" and not any(normalise(c) == normalise(x) for x in exclude)]
        if usable:
            log.say(f"  Adjust the filter, e.g.:  {PROG} --cities {usable[0]}")
        elif cities[0] != "(none reported)":
            log.say("  Every available city is in --exclude-cities. To test one anyway:")
            log.say(f"      {PROG} --cities {cities[0]} --exclude-cities ''")
        log.say(f"  Raw `tailscale exit-node list --filter={args.country}`:")
        for ln in raw.splitlines()[:30]:
            log.say("    " + ln)
        raise Fatal("no matching exit nodes; nothing was changed")

    if not args.include_offline:
        offline = [n for n in chosen if not n.online]
        if offline:
            log.warn(f"skipping {len(offline)} offline node(s): "
                     f"{', '.join(n.hostname for n in offline)}  (--include-offline to test)")
            chosen = [n for n in chosen if n.online]
    if not chosen:
        raise Fatal("every matching node is offline; nothing was changed")

    if args.max_nodes is not None:
        if args.max_nodes < 1:
            raise Fatal("--max-nodes must be >= 1")
        if len(chosen) > args.max_nodes:
            log.info(f"limiting to the first {args.max_nodes} of {len(chosen)} nodes "
                     f"(--max-nodes)")
            chosen = chosen[: args.max_nodes]

    log.say()
    log.say(f"{term.bold('Nodes to benchmark')} ({len(chosen)})")
    w = max(len(n.hostname) for n in chosen)
    for i, n in enumerate(chosen, 1):
        log.say(f"  {i:>3}. {n.hostname.ljust(w)}  {n.ip:<16} "
                f"{(n.city or '?'):<14} {term.dim(n.dns_name)}")
    return chosen


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    term = Term(force_colour=args.colour)
    log = Log(term, args.log_file, args.quiet)

    if args.parallel:
        log.err("--parallel is deliberately not implemented: every test shares this "
                "machine's single uplink, so concurrent runs would measure contention "
                "between themselves rather than node quality. Runs are always sequential.")
        return 2
    if args.reps < 1:
        log.err("--reps must be >= 1")
        return 2

    bench: Optional[Benchmark] = None
    t_start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def on_signal(signum, _frame):
        name = signal.Signals(signum).name
        term.endlive()
        print(f"\n  {term.yellow('!')} {name} received - stopping after the current "
              f"step and restoring Tailscale state.", file=sys.stderr)
        if bench is not None:
            bench.interrupted = True
        raise KeyboardInterrupt(name)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        log.say(term.bold(f"{PROG} {VERSION}") + term.dim("  - Tailscale exit-node benchmark"))
        log.say()
        ts, tester = preflight(args, term, log)
        nodes = select_nodes(ts, args, term, log)

        if args.dry_run:
            log.say()
            log.ok(f"--dry-run: {len(nodes)} node(s) would be benchmarked "
                   f"({args.reps} rep(s) each"
                   + (", plus a DIRECT baseline" if args.baseline else "")
                   + "). Nothing was changed.")
            return 0

        run_root = os.path.abspath(args.out_dir)
        name = args.run_name or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        run_dir = os.path.join(run_root, name)
        if os.path.exists(run_dir) and not args.force:
            raise Fatal(f"run directory already exists: {run_dir}\n"
                        "        Pass --run-name for a different name, or --force to overwrite.")
        os.makedirs(run_dir, exist_ok=True)

        bench = Benchmark(args, ts, tester, term, log, run_dir)
        log.say()
        log.say(term.bold("Tailscale state"))
        bench.capture_original()

        queue = list(nodes)
        if args.baseline:
            queue.insert(0, Node(hostname="", dns_name="", ip="", city="direct",
                                 city_code="", country="", country_code="",
                                 online=True, is_baseline=True))

        est = len(queue) * args.reps * 25 + len(queue) * args.switch_delay
        log.say()
        log.say(term.bold("Benchmark") + term.dim(
            f"  {len(queue)} target(s) x {args.reps} rep(s), sequential, "
            f"rough estimate {est/60:.0f} min"))
        log.say(term.dim(f"  results -> {run_dir}"))
        log.say()

        results = bench.run_all(queue)
        min_success = max(2, math.ceil(args.reps / 2)) if args.reps > 1 else 1
        score_nodes(results, args.reps)

        ranked = sorted([r for r in results if r.status == "ok" and not r.node.is_baseline],
                        key=lambda r: r.rank or 999)
        recommended = ranked[:3]
        for r in recommended:
            r.recommended = True

        # -- restore or apply ------------------------------------------------
        if args.apply_best and recommended:
            bench.restore(to_ip=recommended[0].node.ip,
                          why="--apply-best: connecting to the top-ranked node")
        else:
            if args.apply_best:
                log.warn("--apply-best requested but no node was conclusive; "
                         "restoring the original state instead.")
            bench.restore()

        # -- report ----------------------------------------------------------
        duration = round(time.monotonic() - t_start, 1)
        meta = {
            "started_at": started_at, "duration_s": duration,
            "include_cities": split_list(args.cities),
            "exclude_cities": split_list(args.exclude_cities),
            "country": args.country, "reps": args.reps,
            "min_success": min_success,
            "tester": tester.name, "units_note": tester.units_note,
            "pinned_server": bench.pinned_server,
            "tailscale_version": getattr(args, "_ts_version", "unknown"),
            "speedtest_version": getattr(args, "_st_version", "unknown"),
            "nodes_benchmarked": len([r for r in results if not r.node.is_baseline]),
            "baseline_included": bool(args.baseline),
            "egress_verification": not args.no_egress_check,
            "original_exit_node": bench.original_label,
            "restored_to": bench.restored_to,
            "interrupted": bench.interrupted,
            "host": os.uname().nodename if hasattr(os, "uname") else "",
        }
        paths = write_outputs(run_dir, results, meta, recommended, term)

        log.say()
        log.say(term.bold("=" * min(term.width - 1, 108)))
        log.say(term.bold("RANKED RESULTS") + term.dim(
            "   down/up Mbps, lat/jit ms, loss %, score 0-100"))
        log.say(term.bold("=" * min(term.width - 1, 108)))
        log.say()
        print(render_table(results, args.reps, term))
        log.say()

        base = next((r for r in results if r.node.is_baseline and r.status == "ok"), None)
        if base:
            log.say(f"{term.bold('Baseline (no exit node)')}: "
                    f"{fmt(base.med_down)} Mbps down / {fmt(base.med_up)} Mbps up / "
                    f"{fmt(base.med_lat)} ms latency")
            if recommended and base.med_down and recommended[0].med_down:
                pct = 100.0 * recommended[0].med_down / base.med_down
                dl = (recommended[0].med_lat or 0) - (base.med_lat or 0)
                log.say(term.dim(f"  best exit node keeps {pct:.0f}% of baseline download "
                                 f"and adds {dl:+.0f} ms latency"))
            log.say()

        log.say(term.bold(f"RECOMMENDED (top {len(recommended)}, best first)"))
        if recommended:
            for i, r in enumerate(recommended, 1):
                log.say(f"  {term.green(str(i) + '.')} "
                        f"{term.bold(r.node.dns_name or r.node.hostname)}   "
                        f"score {term.bold(f'{r.score:.1f}')}   "
                        f"{fmt(r.med_down)}/{fmt(r.med_up)} Mbps   "
                        f"{fmt(r.med_lat)} ms   {r.successes}/{args.reps} runs ok")
                log.say(term.dim(f"      tailscale set --exit-node={r.node.ip}"))
        else:
            log.say(f"  {term.yellow('none')} - no node produced enough valid data "
                    f"(need {min_success} successful runs). Marked inconclusive rather "
                    f"than ranked.")
        log.say()

        counts = {
            "recommended": len(recommended),
            "tested, not recommended": len(ranked) - len(recommended),
            "inconclusive": len([r for r in results if r.status == "inconclusive"]),
            "failed": len([r for r in results if r.status == "failed"]),
        }
        log.say(term.bold("Outcome") + "  " + "   ".join(
            f"{k}: {term.bold(str(v))}" for k, v in counts.items()))
        log.say()
        log.say(term.bold("Files"))
        for k in ("json", "csv", "runs_csv", "markdown"):
            log.say(f"  {k:<9} {paths[k]}")
        if args.keep_raw:
            log.say(f"  {'raw':<9} {os.path.join(run_dir, 'raw')}/")
        log.say()
        log.say(term.bold("Tailscale state: ") + f"exit node is now {bench.restored_to}")
        if bench.interrupted:
            log.warn("run was interrupted; results cover only the nodes completed above.")
        return 0

    except KeyboardInterrupt:
        if bench is not None:
            bench.restore(why="interrupted")
            log.say()
            log.say(term.bold("Tailscale state: ") + f"exit node is now {bench.restored_to}")
        else:
            log.say("\n  Interrupted before any change was made.")
        return 130
    except Fatal as exc:
        log.err(str(exc))
        if bench is not None and not bench.restored:
            bench.restore(why="aborting after an error")
            log.say(term.bold("Tailscale state: ") + f"exit node is now {bench.restored_to}")
        return 1
    finally:
        if bench is not None and not bench.restored:
            bench.restore(why="cleanup")
        log.close()


if __name__ == "__main__":
    sys.exit(main())
