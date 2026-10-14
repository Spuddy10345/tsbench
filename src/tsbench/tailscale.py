"""Thin wrapper around the `tailscale` CLI.

Verified-behaviour notes (checked against tailscale 1.102.4 on macOS, 2026-09-15):

  * `tailscale exit-node list --filter=<country>` prints a PADDED TEXT TABLE
    (IP / HOSTNAME / COUNTRY / CITY / STATUS) and has no --json flag.  It also
    emits a duplicate row per country with CITY="Any".  We therefore use
    `tailscale status --json` as the primary, machine-readable source (it
    carries a structured .Location object and no duplicate rows), and keep the
    table only for display and as a documented fallback parser.
  * `tailscale set --exit-node=` REJECTS a bare short hostname
    ("gb-lon-wg-004" -> "invalid value ...; must be IP or peer hostname").
    It accepts a Tailscale IP or the full DNS name.  We always pass the IP.
  * `tailscale set --exit-node=` with an EMPTY value clears the exit node;
    .ExitNodeStatus then becomes null.
  * .ExitNodeStatus.TailscaleIPs entries carry a CIDR suffix ("100.64.0.1/32");
    strip it before comparing.
  * No elevated privileges are needed while your user is the Tailscale operator
    (`tailscale set --operator=$USER`); otherwise `tailscale set` needs sudo.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from tsbench.console import Log
from tsbench.errors import Fatal
from tsbench.matching import host_city_code, normalise
from tsbench.models import Node
from tsbench.proc import run, run_json

# Where Tailscale usually lives; launchd/Raycast PATHs rarely include these.
KNOWN_TAILSCALE_PATHS = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/bin/tailscale",
)


class Tailscale:
    def __init__(self, exe: str, log: Optional[Log] = None) -> None:
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
        return parse_exit_node_table(out)

    # -- current state ------------------------------------------------------ #

    def current_exit_ip(self) -> Optional[str]:
        """Active exit node's Tailscale IP, or None for direct connectivity."""
        return exit_ip_from_status(self.status())

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


def exit_ip_from_status(st: Dict[str, Any]) -> Optional[str]:
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


def parse_exit_node_table(out: str) -> List[Node]:
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
