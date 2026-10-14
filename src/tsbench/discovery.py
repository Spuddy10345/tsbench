"""Choose which exit nodes to benchmark."""

from __future__ import annotations

from typing import List

from tsbench import PROG
from tsbench.console import Log, Term
from tsbench.errors import Fatal
from tsbench.matching import city_matches, country_matches, normalise
from tsbench.models import Node
from tsbench.settings import Settings
from tsbench.tailscale import Tailscale


def filter_nodes(all_nodes: List[Node], s: Settings) -> List[Node]:
    """Country filter, then city include/exclude.  An empty include list means
    every city; exclude always wins over include."""
    chosen: List[Node] = []
    for n in all_nodes:
        if not country_matches(n, s.country):
            continue
        if any(city_matches(n, c) for c in s.exclude_cities):
            continue
        if not s.cities or any(city_matches(n, c) for c in s.cities):
            chosen.append(n)
    chosen.sort(key=lambda n: (normalise(n.city), n.hostname))
    return chosen


def select_nodes(ts: Tailscale, s: Settings, term: Term, log: Log,
                 limit: bool = True) -> List[Node]:
    """`limit=False` leaves --max-nodes to the caller (used when a screen decides
    which nodes to keep)."""
    all_nodes = ts.discover()
    source = "tailscale status --json"
    if all_nodes and not any(n.city or n.city_code for n in all_nodes):
        log.warn("status --json carried no Location data; falling back to parsing "
                 "`tailscale exit-node list`")
        all_nodes = ts.discover_from_table(s.country)
        source = f"tailscale exit-node list --filter={s.country}"
    if not all_nodes:
        rc, raw = ts.exit_node_table(s.country)
        raise Fatal("no exit nodes are advertised to this tailnet.\n"
                    f"        Raw `tailscale exit-node list --filter={s.country}` output:\n"
                    + "\n".join("        " + ln for ln in raw.splitlines()[:25]))

    in_country = [n for n in all_nodes if country_matches(n, s.country)]
    chosen = filter_nodes(all_nodes, s)

    log.say()
    log.say(f"{term.bold('Discovery')} {term.dim('(' + source + ')')}")
    log.info(f"exit nodes advertised: {len(all_nodes)}   "
             f"in country {s.country!r}: {len(in_country)}")
    log.info(f"include cities: {', '.join(s.cities) or 'all'}")
    log.info(f"exclude cities: {', '.join(s.exclude_cities) or '(none)'}")

    if not chosen:
        cities = sorted({n.city for n in in_country if n.city}) or ["(none reported)"]
        rc, raw = ts.exit_node_table(s.country)
        log.say()
        log.err(f"no exit nodes matched cities {s.cities!r} in country {s.country!r}.")
        log.say(f"  Cities actually available in {s.country!r}: {', '.join(cities)}")
        usable = [c for c in cities if c != "(none reported)"
                  and not any(normalise(c) == normalise(x) for x in s.exclude_cities)]
        if usable:
            log.say(f"  Adjust the filter, e.g.:  {PROG} --cities {usable[0]}")
        elif cities[0] != "(none reported)":
            log.say("  Every available city is in --exclude-cities. To test one anyway:")
            log.say(f"      {PROG} --cities {cities[0]} --exclude-cities ''")
        log.say(f"  Raw `tailscale exit-node list --filter={s.country}`:")
        for ln in raw.splitlines()[:30]:
            log.say("    " + ln)
        raise Fatal("no matching exit nodes; nothing was changed")

    if not s.include_offline:
        offline = [n for n in chosen if not n.online]
        if offline:
            log.warn(f"skipping {len(offline)} offline node(s): "
                     f"{', '.join(n.hostname for n in offline)}  (--include-offline to test)")
            chosen = [n for n in chosen if n.online]
    if not chosen:
        raise Fatal("every matching node is offline; nothing was changed")

    if s.max_nodes is not None and s.max_nodes < 1:
        raise Fatal("--max-nodes must be >= 1")
    if limit and s.max_nodes is not None:
        if len(chosen) > s.max_nodes:
            log.info(f"limiting to the first {s.max_nodes} of {len(chosen)} nodes "
                     f"(--max-nodes)")
            chosen = chosen[: s.max_nodes]

    log.say()
    log.say(f"{term.bold('Nodes to benchmark')} ({len(chosen)})")
    w = max(len(n.hostname) for n in chosen)
    for i, n in enumerate(chosen, 1):
        log.say(f"  {i:>3}. {n.hostname.ljust(w)}  {n.ip:<16} "
                f"{(n.city or '?'):<14} {term.dim(n.dns_name)}")
    return chosen
