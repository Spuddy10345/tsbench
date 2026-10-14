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

import json
import urllib.error
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
