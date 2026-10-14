import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tsbench import probes
from tsbench.probes import NativeProbe, http_download_mbps, summarise_latency, tcp_connect_ms


class _Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_a):
        pass

    def do_GET(self):
        _Handler.requests.append(self.path)
        if self.path == "/forbidden":
            self.send_response(403)
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"x")
            return
        if self.path == "/bounded":                  # a small file, like the real endpoint
            body = b"y" * 1_000_000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/empty":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        chunk = b"x" * 65536
        try:
            for _ in range(2000):            # ~131 MB, far more than a short probe reads
                self.wfile.write(chunk)
        except OSError:
            pass                             # the client hung up when its cap expired


class _QuietServer(HTTPServer):
    def handle_error(self, request, client_address):     # clients hang up when their cap expires
        pass


@pytest.fixture
def http_server():
    srv = _QuietServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    yield srv.getsockname()[1]
    srv.close()


def test_summarise_latency_median_jitter_and_loss():
    out = summarise_latency([10.0, 14.0, None, 12.0, 10.0])
    assert out["latency_ms"] == 11.0                  # median of 10, 10, 12, 14
    assert out["jitter_ms"] == pytest.approx((4 + 2 + 2) / 3, abs=0.01)
    assert out["packet_loss_pct"] == 20.0


def test_summarise_latency_all_failed():
    out = summarise_latency([None, None])
    assert out == {"latency_ms": None, "jitter_ms": None, "packet_loss_pct": 100.0}


def test_summarise_latency_single_sample_has_zero_jitter():
    assert summarise_latency([8.0])["jitter_ms"] == 0.0


def test_tcp_connect_ms_measures_a_real_handshake(listener):
    ms = tcp_connect_ms("127.0.0.1", listener, 2.0)
    assert ms is not None and 0 <= ms < 1000


def test_tcp_connect_ms_returns_none_when_nothing_listens():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                         # now nothing is listening there
    assert tcp_connect_ms("127.0.0.1", port, 1.0) is None


def test_download_is_time_capped_and_reports_plausible_mbps(http_server):
    t0 = time.perf_counter()
    mbps = http_download_mbps(http_server + "/big", cap_s=1.0, warmup_s=0.2)
    assert time.perf_counter() - t0 < 5.0             # stopped by the cap, not the file end
    assert mbps > 10                                  # loopback is fast; just sanity-check


def test_a_small_bounded_file_is_fetched_repeatedly_until_the_cap(http_server):
    _Handler.requests.clear()
    mbps = http_download_mbps(http_server + "/bounded", cap_s=1.0, warmup_s=0.2)
    assert len(_Handler.requests) >= 3               # 1 MB each: one request cannot fill 1 s
    assert mbps > 10


def test_an_http_error_is_reported_not_measured(http_server):
    with pytest.raises(ValueError, match="HTTP 403"):
        http_download_mbps(http_server + "/forbidden", cap_s=1.0)


def test_unsupported_urls_are_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        http_download_mbps("ftp://example.com/x", cap_s=1.0)


def test_empty_download_is_an_error(http_server):
    with pytest.raises(ValueError, match="no data"):
        http_download_mbps(http_server + "/empty", cap_s=1.0)


def test_measure_without_throughput_skips_the_download(listener, monkeypatch):
    monkeypatch.setattr(probes, "http_download_mbps",
                        lambda *a, **k: pytest.fail("downloaded despite throughput=False"))
    p = NativeProbe(host="127.0.0.1", port=listener, url="unused", connect_timeout=1.0)
    out = p.measure(throughput=False)
    assert out["download_mbps"] is None and out["latency_ms"] is not None
    assert out["packet_loss_pct"] == 0.0 and out["upload_mbps"] is None


def test_measure_fails_when_the_host_is_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = NativeProbe(host="127.0.0.1", port=port, samples=2)
    with pytest.raises(ValueError, match="no TCP handshake"):
        p.measure(throughput=False)


def test_measure_with_throughput_returns_a_download(listener, http_server):
    p = NativeProbe(host="127.0.0.1", port=listener, url=http_server + "/big", cap_s=1.0,
                    samples=2)
    out = p.measure(throughput=True)
    assert out["download_mbps"] > 10 and out["latency_ms"] is not None


def test_measure_reports_a_failed_download_as_value_error(listener):
    p = NativeProbe(host="127.0.0.1", port=listener, url="http://127.0.0.1:9/never",
                    cap_s=1.0, samples=2)
    with pytest.raises(ValueError, match="download probe failed"):
        p.measure(throughput=True)
