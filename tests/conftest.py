import json
import os
from typing import Any, Dict, List

import pytest

from tsbench.console import Log, Term
from tsbench.models import Node, NodeResult, RunResult
from tsbench.tailscale import Tailscale

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name: str) -> Any:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path_factory, monkeypatch):
    """No test may touch the real ~/.local/state/tsbench (history, run lock, logs)."""
    monkeypatch.setenv("TSBENCH_HOME", str(tmp_path_factory.mktemp("tsbench-home")))
    monkeypatch.delenv("TSBENCH_ACCEPT_OOKLA_LICENSE", raising=False)


@pytest.fixture
def status_uk() -> Dict[str, Any]:
    return load_fixture("status_uk.json")


@pytest.fixture
def run_fixture() -> Dict[str, Any]:
    return load_fixture("run_sanitised.json")


@pytest.fixture
def quiet_log() -> Log:
    return Log(Term(force_colour=False), None, quiet=True)


class FakeTailscale(Tailscale):
    """A Tailscale whose `status --json` comes from a dict, so no binary is run."""

    def __init__(self, status: Dict[str, Any]) -> None:
        super().__init__(exe="/nonexistent/tailscale", log=None)
        self._status = status
        self.calls: List[str] = []

    def status(self) -> Dict[str, Any]:
        return self._status

    def exit_node_table(self, country: str):
        return 0, ""

    def set_exit_node(self, target: str):
        self.calls.append(target)
        return True, ""


@pytest.fixture
def fake_ts(status_uk) -> FakeTailscale:
    return FakeTailscale(status_uk)


def build_results(fixture: Dict[str, Any], min_success: int = 2) -> List[NodeResult]:
    """Rebuild aggregated NodeResults from the sanitised run fixture."""
    out: List[NodeResult] = []
    for n in fixture["results"]:
        node = Node(hostname=n["hostname"], dns_name="", ip=n["ip"], city=n["city"],
                    city_code="", country="UK", country_code="GB", online=True,
                    is_baseline=n["is_baseline"])
        nr = NodeResult(node=node, connected=True)
        for r in n["runs"]:
            nr.runs.append(RunResult(
                rep=r["rep"], ok=r["ok"], started_at="2026-01-01T00:00:00+00:00",
                duration_s=20.0, download_mbps=r["download_mbps"],
                upload_mbps=r["upload_mbps"], latency_ms=r["latency_ms"],
                jitter_ms=r["jitter_ms"], packet_loss_pct=r["packet_loss_pct"]))
        nr.aggregate(min_success)
        out.append(nr)
    return out
