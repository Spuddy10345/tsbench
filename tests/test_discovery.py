import pytest

from tsbench.console import Term
from tsbench.discovery import filter_nodes, select_nodes
from tsbench.errors import Fatal
from tsbench.settings import Settings


def test_default_selects_every_uk_city(fake_ts):
    chosen = filter_nodes(fake_ts.discover(), Settings())
    assert len(chosen) == 22
    assert {n.city for n in chosen} == {"London", "Manchester", "Glasgow"}


def test_country_filter_excludes_other_countries(fake_ts):
    chosen = filter_nodes(fake_ts.discover(), Settings())
    assert all(n.country_code == "GB" for n in chosen)


def test_include_single_city(fake_ts):
    chosen = filter_nodes(fake_ts.discover(), Settings(cities=["Manchester"]))
    assert len(chosen) == 6
    assert all(n.city == "Manchester" for n in chosen)


def test_include_by_mullvad_city_code(fake_ts):
    chosen = filter_nodes(fake_ts.discover(), Settings(cities=["glw"]))
    assert {n.city for n in chosen} == {"Glasgow"}


def test_exclude_wins_over_include(fake_ts):
    s = Settings(cities=["London", "Glasgow"], exclude_cities=["Glasgow"])
    assert {n.city for n in filter_nodes(fake_ts.discover(), s)} == {"London"}


def test_exclude_alone_removes_a_city(fake_ts):
    s = Settings(exclude_cities=["Glasgow", "Manchester"])
    chosen = filter_nodes(fake_ts.discover(), s)
    assert len(chosen) == 14


def test_results_are_sorted_by_city_then_hostname(fake_ts):
    chosen = filter_nodes(fake_ts.discover(), Settings())
    keys = [(n.city, n.hostname) for n in chosen]
    assert keys == sorted(keys)


def test_select_nodes_skips_offline_by_default(fake_ts, quiet_log):
    nodes = select_nodes(fake_ts, Settings(), Term(force_colour=False), quiet_log)
    assert len(nodes) == 21
    assert all(n.online for n in nodes)


def test_select_nodes_can_include_offline(fake_ts, quiet_log):
    nodes = select_nodes(fake_ts, Settings(include_offline=True), Term(force_colour=False),
                         quiet_log)
    assert len(nodes) == 22


def test_max_nodes_truncates(fake_ts, quiet_log):
    nodes = select_nodes(fake_ts, Settings(max_nodes=5), Term(force_colour=False), quiet_log)
    assert len(nodes) == 5


def test_max_nodes_must_be_positive(fake_ts, quiet_log):
    with pytest.raises(Fatal):
        select_nodes(fake_ts, Settings(max_nodes=0), Term(force_colour=False), quiet_log)


def test_no_match_is_fatal_and_changes_nothing(fake_ts, quiet_log):
    with pytest.raises(Fatal, match="no matching exit nodes"):
        select_nodes(fake_ts, Settings(cities=["Atlantis"]), Term(force_colour=False), quiet_log)
    assert fake_ts.calls == []  # never touched the exit node
