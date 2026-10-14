"""Latency screening: cheap ranking of nodes before any throughput is measured.

A bad exit node is usually visible as high latency, jitter or handshake loss,
so the screen is used to (a) drop clear outliers and (b) decide which few nodes
deserve a throughput probe.  It does NOT pick a winner: latency alone is only
loosely correlated with throughput (0.39-0.97 across the saved runs).
"""

from __future__ import annotations

import statistics
from typing import List, Optional, Sequence, Tuple

from tsbench.models import Node, NodeResult

# A node is dropped when its latency exceeds OUTLIER_FACTOR x the median of the
# screened nodes plus OUTLIER_SLACK_MS.  Conservative on purpose: Manchester and
# Glasgow are legitimately a few ms further away than London.
OUTLIER_FACTOR = 2.0
OUTLIER_SLACK_MS = 10.0


def screen_key(r: NodeResult) -> float:
    """Lower is better.  Milliseconds-equivalent: latency, plus a penalty for
    jitter and for each percent of failed handshakes."""
    return (r.med_lat or 1e9) + 2.0 * (r.med_jit or 0.0) + 20.0 * (r.loss_pct or 0.0)


def rank_screen(results: Sequence[NodeResult]) -> List[NodeResult]:
    """Usable screened nodes, best first (the DIRECT baseline is excluded)."""
    ok = [r for r in results if r.status == "ok" and not r.node.is_baseline]
    return sorted(ok, key=lambda r: (screen_key(r), r.node.hostname))


def cut_outliers(ranked: Sequence[NodeResult]) -> Tuple[List[NodeResult], List[NodeResult]]:
    """Split a ranked list into (kept, dropped-as-outliers)."""
    lats = [r.med_lat for r in ranked if r.med_lat is not None]
    if len(lats) < 4:                      # too few to call anything an outlier
        return list(ranked), []
    limit = OUTLIER_FACTOR * statistics.median(lats) + OUTLIER_SLACK_MS
    kept = [r for r in ranked if (r.med_lat or 0) <= limit]
    dropped = [r for r in ranked if (r.med_lat or 0) > limit]
    return kept, dropped


def pick_candidates(ranked: Sequence[NodeResult], favourites: Sequence[str],
                    top_k: int) -> List[Node]:
    """The best `top_k` by screen, plus remembered favourites that passed the
    screen, in screen order (so the likeliest winners are measured first)."""
    chosen = list(ranked[:max(0, top_k)])
    have = {r.node.hostname for r in chosen}
    for host in favourites:
        extra = next((r for r in ranked if r.node.hostname == host), None)
        if extra is not None and host not in have:
            chosen.append(extra)
            have.add(host)
    chosen.sort(key=lambda r: (screen_key(r), r.node.hostname))
    return [r.node for r in chosen]


def failed_to_screen(results: Sequence[NodeResult]) -> List[NodeResult]:
    return [r for r in results if r.status != "ok" and not r.node.is_baseline]


def top_n_nodes(ranked: Sequence[NodeResult], n: Optional[int]) -> List[Node]:
    return [r.node for r in (ranked if n is None else ranked[:n])]
