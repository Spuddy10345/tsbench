"""Data model shared by every layer."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import List, Optional


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


def baseline_node() -> Node:
    """The synthetic 'no exit node' reference target."""
    return Node(hostname="", dns_name="", ip="", city="direct", city_code="",
                country="", country_code="", online=True, is_baseline=True)


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

    def aggregate(self, min_success: int, need_down: bool = True) -> None:
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
        elif (self.successes < min_success or self.med_lat is None
              or (need_down and self.med_down is None)):
            self.status = "inconclusive"
        else:
            self.status = "ok"


def ranked_ok(results: List[NodeResult]) -> List[NodeResult]:
    """Conclusive, non-baseline results, best rank first."""
    return sorted([r for r in results if r.status == "ok" and not r.node.is_baseline],
                  key=lambda r: r.rank or 999)
