"""Measurement back ends: egress verification and speed testers.

Units, verified against Ookla CLI 1.2.0.84 and librespeed-cli:
  * Ookla JSON reports .download.bandwidth / .upload.bandwidth in BYTES PER
    SECOND, so Mbps = bandwidth * 8 / 1e6.  .ping.latency/.ping.jitter are ms,
    .packetLoss is a percentage and may be absent on some runs.
  * librespeed-cli --json reports .download/.upload already in Mbps and
    .ping/.jitter in ms, and reports NO packet loss.  Its default server choice
    is not distance-optimal, so pin a server when using it.
"""

from __future__ import annotations

import http.client
import json
import socket
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tsbench import PROG, __version__

MULLVAD_CHECK_URL = "https://am.i.mullvad.net/json"
GENERIC_CHECK_URL = "https://www.gstatic.com/generate_204"

KNOWN_TESTER_PATHS = {
    "speedtest": ("/opt/homebrew/bin/speedtest", "/usr/local/bin/speedtest", "/usr/bin/speedtest"),
    "librespeed-cli": ("/opt/homebrew/bin/librespeed-cli", "/usr/local/bin/librespeed-cli",
                       "/usr/bin/librespeed-cli"),
}


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else round(float(v), 3)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Egress verification
# --------------------------------------------------------------------------- #

def http_json(url: str, timeout: float) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{PROG}/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def http_reachable(url: str, timeout: float) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{PROG}/{__version__}"})
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
    binary = ""
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
    binary = "speedtest"
    units_note = ("Ookla JSON reports bandwidth in bytes/s (converted here with "
                  "*8/1e6 to Mbps); latency/jitter in ms; packetLoss in %.")

    def __init__(self, exe: str, extra: Sequence[str] = (),
                 accept_license: bool = False) -> None:
        super().__init__(exe, extra)
        self.accept_license = accept_license

    def command(self, server_id: Optional[int]) -> List[str]:
        cmd = [self.exe, "--format=json", "--precision=4"]
        if self.accept_license:
            cmd += ["--accept-license", "--accept-gdpr"]
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
            "server": f"{srv.get('name', '?')} ({srv.get('location', '?')})",
            "server_id": int(srv["id"]) if srv.get("id") is not None else None,
            "raw": d,
        }


class LibrespeedTester(SpeedTester):
    name = "librespeed"
    binary = "librespeed-cli"
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


TESTERS = {"ookla": OoklaTester, "librespeed": LibrespeedTester}


# --------------------------------------------------------------------------- #
# Built-in probe (screen and quick tiers)
# --------------------------------------------------------------------------- #

DEFAULT_PROBE_HOST = "1.1.1.1"
DEFAULT_PROBE_PORT = 443
DEFAULT_PROBE_URL = "https://speed.cloudflare.com/__down?bytes=25000000"


def tcp_connect_ms(host: str, port: int, timeout: float) -> Optional[float]:
    """Time for one TCP handshake, in ms; None if it failed.  Through an exit
    node this is one round trip client -> node -> host, which is what we want."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    return (time.perf_counter() - t0) * 1000.0


def summarise_latency(samples: Sequence[Optional[float]]) -> Dict[str, Optional[float]]:
    """Median latency, jitter (mean absolute difference of consecutive samples)
    and packet-loss % (failed handshakes) from raw connect times."""
    good = [s for s in samples if s is not None]
    loss = 100.0 * (len(samples) - len(good)) / len(samples) if samples else None
    if not good:
        return {"latency_ms": None, "jitter_ms": None, "packet_loss_pct": loss}
    jitter = (statistics.mean(abs(a - b) for a, b in zip(good, good[1:]))
              if len(good) > 1 else 0.0)
    return {"latency_ms": round(statistics.median(good), 3),
            "jitter_ms": round(jitter, 3),
            "packet_loss_pct": round(loss or 0.0, 3)}


def http_download_mbps(url: str, cap_s: float, warmup_s: float = 1.0,
                       io_timeout: float = 8.0) -> float:
    """Stream `url` for at most `cap_s` seconds and return Mbps.

    The endpoint serves a bounded file (Cloudflare refuses large sizes), and a
    fast link finishes one in a fraction of a second, so requests are repeated
    over one keep-alive connection until the time cap is reached.  Timing starts
    at the first byte (connection set-up is excluded) and the first `warmup_s`
    seconds are discarded when the transfer lasts that long, so TCP slow start
    does not dominate a short probe.  Raises OSError or ValueError if nothing
    could be downloaded."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"unsupported probe URL: {url!r}")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                else http.client.HTTPConnection)
    headers = {"User-Agent": f"{PROG}/{__version__}"}

    total = 0
    t_first = mark_t = None
    mark_bytes = 0
    conn = None
    try:
        while True:
            if conn is None:
                conn = conn_cls(parts.hostname, parts.port, timeout=io_timeout)
            conn.request("GET", target, headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status} from {parts.hostname}")
            got = 0
            while True:
                chunk = resp.read(65536)
                now = time.perf_counter()
                if not chunk:
                    break
                got += len(chunk)
                total += len(chunk)
                if t_first is None:
                    t_first = now
                elif mark_t is None and now - t_first >= warmup_s:
                    mark_t, mark_bytes = now, total
                if now - t_first >= cap_s:
                    break
            if got == 0:
                raise ValueError("download returned no data")
            if t_first is not None and time.perf_counter() - t_first >= cap_s:
                break
            if resp.will_close:
                conn.close()
                conn = None
    finally:
        if conn is not None:
            conn.close()
    end = time.perf_counter()
    assert t_first is not None
    if mark_t is not None and end - mark_t >= 0.5:
        start_t, got_bytes = mark_t, total - mark_bytes
    else:
        start_t, got_bytes = t_first, total
    return got_bytes * 8 / max(end - start_t, 1e-6) / 1e6


class NativeProbe:
    """Short, dependency-free probe used by the screen and quick tiers.

    Latency is TCP-handshake time to a fixed host; throughput is a time-capped
    single-stream HTTP download.  It needs no external binary and no licence.
    Single-stream numbers read lower than Ookla's multi-stream ones, so they
    are only comparable with other probe runs, never with full-tier numbers."""

    name = "probe"
    binary = ""
    native = True
    units_note = ("Built-in probe: latency = median TCP handshake time to the probe host, "
                  "jitter = mean change between consecutive handshakes, loss = failed "
                  "handshakes; download = single-stream HTTP, time-capped, in Mbps. "
                  "Upload is not measured.")

    def __init__(self, host: str = DEFAULT_PROBE_HOST, port: int = DEFAULT_PROBE_PORT,
                 url: str = DEFAULT_PROBE_URL, cap_s: float = 6.0, samples: int = 5,
                 connect_timeout: float = 3.0) -> None:
        self.host, self.port, self.url = host, port, url
        self.cap_s, self.samples, self.connect_timeout = cap_s, samples, connect_timeout

    def latency(self, samples: Optional[int] = None) -> Dict[str, Optional[float]]:
        n = samples or self.samples
        raw = []
        for i in range(n):
            raw.append(tcp_connect_ms(self.host, self.port, self.connect_timeout))
            if i + 1 < n:
                time.sleep(0.05)
        return summarise_latency(raw)

    def measure(self, throughput: bool = True) -> Dict[str, Any]:
        """One probe: latency always, throughput when asked.  Raises ValueError
        when the host is unreachable or the download yields nothing."""
        out: Dict[str, Any] = dict(self.latency())
        if out["latency_ms"] is None:
            raise ValueError(f"no TCP handshake to {self.host}:{self.port} succeeded")
        out.update(upload_mbps=None, server=f"{self.host}:{self.port}", server_id=None,
                   download_mbps=None)
        if throughput:
            try:
                out["download_mbps"] = round(http_download_mbps(self.url, self.cap_s), 3)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise ValueError(f"download probe failed: {exc}") from exc
        return out

    @classmethod
    def from_settings(cls, s: Any) -> NativeProbe:
        return cls(host=s.probe_host, url=s.probe_url, cap_s=s.probe_cap_s,
                   samples=s.screen_samples)


def probe_network_tag(timeout: float = 5.0) -> Optional[str]:
    """A coarse label for the network we are on, from Mullvad's own check
    endpoint.  Only meaningful while connected DIRECTLY (no exit node)."""
    data = http_json(MULLVAD_CHECK_URL, timeout)
    if not isinstance(data, dict) or data.get("mullvad_exit_ip"):
        return None
    org = str(data.get("organization") or "").strip()
    return org[:80] or None
