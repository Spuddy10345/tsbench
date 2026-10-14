import pytest

from tsbench.matching import (
    city_matches,
    country_matches,
    host_city_code,
    host_country_code,
    normalise,
    split_list,
)
from tsbench.models import Node


def node(city="London", code="LON", host="gb-lon-wg-001", country="UK", cc="GB") -> Node:
    return Node(hostname=host, dns_name="", ip="100.64.0.1", city=city, city_code=code,
                country=country, country_code=cc, online=True)


@pytest.mark.parametrize("raw,expected", [
    ("São Paulo", "sao paulo"),
    ("SAO-PAULO", "sao paulo"),
    ("Denver, CO", "denver"),
    ("  Hong   Kong ", "hong kong"),
    ("", ""),
])
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_normalise_keeps_region_suffix_when_asked():
    assert normalise("Denver, CO", drop_suffix=False) == "denver co"


def test_split_list_ignores_blanks():
    assert split_list(" London, ,Glasgow ,") == ["London", "Glasgow"]
    assert split_list("") == []


def test_host_codes():
    assert host_city_code("gb-lon-wg-001") == "lon"
    assert host_country_code("gb-lon-wg-001") == "gb"
    assert host_city_code("laptop") == ""


@pytest.mark.parametrize("query", ["London", "london", "LON", "lon"])
def test_city_matches_name_and_codes(query):
    assert city_matches(node(), query)


def test_city_matches_uses_hostname_segment_when_metadata_missing():
    n = node(city="", code="", host="gb-glw-wg-001")
    assert city_matches(n, "glw")
    assert not city_matches(n, "lon")


def test_city_matches_is_anchored():
    new_york = node(city="New York, NY", code="NYC", host="us-nyc-wg-301", country="USA", cc="US")
    assert not city_matches(new_york, "York")
    assert city_matches(new_york, "New York")
    assert city_matches(new_york, "New York NY")


def test_city_matches_region_suffix_forms():
    denver = node(city="Denver, CO", code="DEN", host="us-den-wg-001", country="USA", cc="US")
    assert city_matches(denver, "Denver")
    assert city_matches(denver, "Denver CO")


def test_city_matches_empty_query_never_matches():
    assert not city_matches(node(), "")


@pytest.mark.parametrize("query", ["uk", "UK", "gb", "GB"])
def test_country_matches_display_code_and_hostname(query):
    assert country_matches(node(), query)


def test_country_matches_rejects_other_countries_and_allows_empty():
    assert not country_matches(node(), "de")
    assert country_matches(node(), "")
