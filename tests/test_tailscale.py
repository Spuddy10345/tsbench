import pytest

from tsbench.errors import Fatal
from tsbench.tailscale import exit_ip_from_status, parse_exit_node_table


def test_discover_returns_only_exit_nodes(fake_ts):
    nodes = fake_ts.discover()
    assert all(n.hostname != "laptop" for n in nodes)
    gb = [n for n in nodes if n.country_code == "GB"]
    assert len(gb) == 22


def test_discover_uses_ipv4_without_cidr(fake_ts):
    for n in fake_ts.discover():
        assert ":" not in n.ip and "/" not in n.ip


def test_discover_city_breakdown_matches_the_tailnet(fake_ts):
    cities = {}
    for n in fake_ts.discover():
        if n.country_code == "GB":
            cities[n.city] = cities.get(n.city, 0) + 1
    assert cities == {"London": 14, "Manchester": 6, "Glasgow": 2}


def test_discover_strips_trailing_dot_from_dns_name(fake_ts):
    assert all(not n.dns_name.endswith(".") for n in fake_ts.discover())


def test_exit_ip_none_when_direct(status_uk):
    assert exit_ip_from_status(status_uk) is None


def test_exit_ip_from_exit_node_status_strips_cidr():
    st = {"ExitNodeStatus": {"TailscaleIPs": ["100.64.0.5/32", "fd7a::5/128"]}}
    assert exit_ip_from_status(st) == "100.64.0.5"


def test_exit_ip_falls_back_to_peer_flag():
    st = {"ExitNodeStatus": None,
          "Peer": {"k": {"ExitNode": True, "TailscaleIPs": ["fd7a::1", "100.64.0.9"]}}}
    assert exit_ip_from_status(st) == "100.64.0.9"


def _table():
    # Real output has column headers IP / HOSTNAME / COUNTRY / CITY / STATUS aligned
    # to the data columns; build one with exact offsets.
    rows = [
        ("IP", "HOSTNAME", "COUNTRY", "CITY", "STATUS"),
        ("100.64.0.1", "gb-lon-wg-001.mullvad.ts.net", "UK", "London", "-"),
        ("100.64.0.2", "gb-glw-wg-001.mullvad.ts.net", "UK", "Glasgow", "-"),
        ("100.64.0.1", "gb-lon-wg-001.mullvad.ts.net", "UK", "Any", "-"),
        ("100.64.0.3", "us-den-wg-001.mullvad.ts.net", "USA", "Denver, CO", "-"),
    ]
    widths = [max(len(r[i]) for r in rows) + 2 for i in range(5)]
    return "\n".join("".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def test_parse_exit_node_table_prefers_real_city_over_any():
    nodes = {n.hostname: n for n in parse_exit_node_table(_table())}
    assert nodes["gb-lon-wg-001"].city == "London"
    assert set(nodes) == {"gb-lon-wg-001", "gb-glw-wg-001", "us-den-wg-001"}


def test_parse_exit_node_table_keeps_cities_with_spaces_and_commas():
    nodes = {n.hostname: n for n in parse_exit_node_table(_table())}
    assert nodes["us-den-wg-001"].city == "Denver, CO"
    assert nodes["us-den-wg-001"].city_code == "DEN"


def test_parse_exit_node_table_refuses_to_guess_without_header():
    with pytest.raises(Fatal):
        parse_exit_node_table("nothing useful here\n")


def test_discovery_cleans_hostile_peer_names(status_uk):
    from conftest import FakeTailscale
    peer = next(iter(status_uk["Peer"].values()))
    peer["HostName"] = "gb-lon-wg-999\x1b]0;pwned\x07"
    names = [n.hostname for n in FakeTailscale(status_uk).discover()]
    assert "gb-lon-wg-999]0;pwned" in names
    assert not any("\x1b" in n or "\x07" in n for n in names)
