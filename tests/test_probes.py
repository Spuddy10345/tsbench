import json

import pytest

from tsbench.probes import LibrespeedTester, OoklaTester

OOKLA_OK = {
    "type": "result",
    "ping": {"latency": 24.5, "jitter": 1.25},
    "download": {"bandwidth": 12_500_000},   # bytes/s -> 100 Mbps
    "upload": {"bandwidth": 6_250_000},      # bytes/s -> 50 Mbps
    "packetLoss": 0.5,
    "server": {"id": 42760, "name": "Example ISP", "location": "London"},
}


def test_ookla_converts_bytes_per_second_to_mbps():
    out = OoklaTester("speedtest").parse(json.dumps(OOKLA_OK))
    assert out["download_mbps"] == 100.0
    assert out["upload_mbps"] == 50.0
    assert out["latency_ms"] == 24.5 and out["jitter_ms"] == 1.25
    assert out["packet_loss_pct"] == 0.5
    assert out["server_id"] == 42760 and out["server"] == "Example ISP (London)"


def test_ookla_packet_loss_may_be_absent():
    d = {k: v for k, v in OOKLA_OK.items() if k != "packetLoss"}
    assert OoklaTester("speedtest").parse(json.dumps(d))["packet_loss_pct"] is None


def test_ookla_log_message_is_an_error():
    msg = {"type": "log", "level": "error", "message": "Configuration - Could not connect"}
    with pytest.raises(ValueError, match="Could not connect"):
        OoklaTester("speedtest").parse(json.dumps(msg))


def test_ookla_does_not_accept_the_licence_unless_told_to():
    cmd = OoklaTester("speedtest").command(None)
    assert "--accept-license" not in cmd and "--accept-gdpr" not in cmd


def test_ookla_licence_flags_added_on_explicit_opt_in():
    cmd = OoklaTester("speedtest", accept_license=True).command(None)
    assert "--accept-license" in cmd and "--accept-gdpr" in cmd


def test_ookla_pins_server():
    assert "--server-id=123" in OoklaTester("speedtest").command(123)
    assert not any(a.startswith("--server-id") for a in OoklaTester("speedtest").command(None))


def test_librespeed_reports_mbps_directly_and_no_loss():
    d = [{"download": 88.8, "upload": 44.4, "ping": 20, "jitter": 2, "server": {"name": "X"}}]
    out = LibrespeedTester("librespeed-cli").parse(json.dumps(d))
    assert out["download_mbps"] == 88.8 and out["upload_mbps"] == 44.4
    assert out["packet_loss_pct"] is None and out["server_id"] is None


def test_librespeed_empty_array_is_an_error():
    with pytest.raises(ValueError):
        LibrespeedTester("librespeed-cli").parse("[]")


def test_librespeed_pins_server():
    assert LibrespeedTester("librespeed-cli").command(7) == ["librespeed-cli", "--json",
                                                              "--server", "7"]
