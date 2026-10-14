"""City / country matching."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from tsbench.models import Node


def split_list(s: str) -> List[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def normalise(text: str, drop_suffix: bool = True) -> str:
    """Fold to a comparable form.

    Strips accents, lowercases, and reduces punctuation to single spaces, so
    "Sao Paulo", "SAO-PAULO" and "São Paulo" all fold to "sao paulo".

    With drop_suffix=True (the default) any ", XX" region suffix is removed as
    well, because Mullvad reports "Denver, CO" / "Raleigh, NC" and people say
    "Denver".  We also keep the suffix-retaining form around so that
    "Denver CO" still matches - see city_matches().
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    if drop_suffix:
        text = text.split(",")[0]
    text = re.sub(r"[^A-Za-z0-9]+", " ", text).strip().lower()
    return re.sub(r"\s+", " ", text)


def host_city_code(hostname: str) -> str:
    """'gb-lon-wg-001' -> 'lon'.  Documented assumption about Mullvad naming:
    <country>-<city>-<proto>-<index>.  Returns '' if it does not fit."""
    parts = hostname.split("-")
    return parts[1].lower() if len(parts) >= 2 else ""


def host_country_code(hostname: str) -> str:
    parts = hostname.split("-")
    return parts[0].lower() if len(parts) >= 2 else ""


def city_matches(node: Node, query: str) -> bool:
    """True if `query` names this node's city.

    Matching strategy, most to least specific:
      1. exact normalised city name           ("london" == "London")
      2. normalised city starts with query    ("washington dc" for "washington")
      3. Mullvad CityCode                     ("lon" == "LON")
      4. city segment of the hostname         ("lon" from "gb-lon-wg-001")

    Both the suffix-stripped and suffix-retaining folds are tried, so
    "Denver" and "Denver CO" both match Mullvad's "Denver, CO". Matching is
    deliberately anchored (equality, or a whole-word prefix) so that "York"
    does NOT match "New York, NY".
    """
    q = normalise(query)
    if not q:
        return False
    # Two folded forms: "denver" (region suffix dropped) and "denver co".
    for city in {normalise(node.city), normalise(node.city, drop_suffix=False)}:
        if city and (q == city or city.startswith(q + " ")):
            return True
    if node.city_code and q == node.city_code.lower():
        return True
    return bool(host_city_code(node.hostname)) and q == host_city_code(node.hostname)


def country_matches(node: Node, query: str) -> bool:
    """Accepts the display country ('uk'), the ISO code ('gb') or the hostname
    prefix ('gb').  Tailscale reports Country='UK' with CountryCode='GB'."""
    q = normalise(query)
    if not q:
        return True
    for cand in (node.country, node.country_code, host_country_code(node.hostname)):
        if cand and q == normalise(cand):
            return True
    return False


def clean_name(value: str) -> str:
    """Drop control characters (including terminal escapes) from a name that came
    from the tailnet before it is printed or stored.  Peers choose their own
    hostnames, so they are not trusted."""
    return "".join(ch for ch in str(value) if ch.isprintable()).strip()


def safe_filename(value: str) -> str:
    """A name usable as a single path component, whatever the input."""
    out = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(value))
    return out.strip(".") or "node"
