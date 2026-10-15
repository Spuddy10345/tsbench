"""The privacy guard must catch what it claims to, and allow synthetic data."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_privacy.py"
spec = importlib.util.spec_from_file_location("check_privacy", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def scan(tmp_path, name, text):
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return guard.scan(tmp_path, [name])


def test_synthetic_fixture_data_is_allowed(tmp_path):
    text = "node 100.64.0.7 host gb-lon-wg-001.example.ts.net tail1.example"
    assert scan(tmp_path, "ok.txt", text) == []


def test_real_looking_tailnet_name_is_flagged(tmp_path):
    # Built at runtime so the repository never contains a literal match.
    name = "mac.tail" + "1a2b3c" + ".ts.net"
    problems = scan(tmp_path, "doc.md", f"see {name} for details")
    assert len(problems) == 1 and "tailnet DNS name" in problems[0]


def test_non_fixture_tailscale_ip_is_flagged(tmp_path):
    ip = ".".join(["100", "101", "102", "103"])
    problems = scan(tmp_path, "doc.md", f"exit node is {ip} today")
    assert len(problems) == 1 and "Tailscale IP" in problems[0]


def test_results_directory_and_databases_are_flagged(tmp_path):
    assert scan(tmp_path, "tsbench-results/run.json", "{}")
    assert scan(tmp_path, "state/tsbench.db", "x")


def test_binary_files_are_skipped(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\0\0" + ".".join(["100", "101", "102", "103"]).encode())
    assert guard.scan(tmp_path, ["blob.bin"]) == []


def test_repository_itself_is_clean():
    root = SCRIPT.parents[1]
    assert guard.scan(root, guard.tracked_files(root)) == []
