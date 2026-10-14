"""Remembered per-node standing, built from raw history.

Scores in a single run are normalised to that run's best node, so they cannot
be compared across runs.  A node's *standing* instead compares its numbers to
fixed references and to the same run's DIRECT baseline, then smooths over time.

Per observation (one node in one run), 0-100:

    D = down / baseline_down          (capped at 1)
    U = up   / baseline_up            (capped at 1; absent in quick runs)
    L = 1 - added_latency / 80 ms     (added = node latency - baseline latency)
    J = 1 - jitter / 20 ms
    standing = 100 * weighted mean of the available terms (same weights as the
               run score, renormalised) * reliability * (1 - loss/10)

Observations are weighted by recency (half-life `halflife_h`).  The estimate is
a conservative one: the weighted mean, scaled down by the failure rate, minus an
uncertainty penalty that shrinks as evidence accumulates.  A node seen once
does not outrank one that has been consistently good.

Rankings between healthy nodes are close to a statistical tie (see README), so
callers should treat nodes within `TIE_MARGIN` of the best as a group.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from tsbench.scoring import W_DOWN, W_JIT, W_LAT, W_UP

ADDED_LAT_ZERO_MS = 80.0
JITTER_ZERO_MS = 20.0
PRIOR_SD = 15.0          # assumed spread of a node we know little about
PRIOR_WEIGHT = 2.0
TIE_MARGIN = 0.10        # nodes within 10% of the best are treated as a tie


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def standing_score(row: Mapping[str, Any]) -> Optional[float]:
    """Standing of one observation, or None when it carries no throughput or no
    baseline to compare against (such observations only inform reliability)."""
    down, base_down = row["med_down"], row["baseline_down"]
    if down is None or not base_down:
        return None
    terms = [(W_DOWN, _clamp01(down / base_down))]
    up, base_up = row["med_up"], row["baseline_up"]
    if up is not None and base_up:
        terms.append((W_UP, _clamp01(up / base_up)))
    if row["med_lat"] is not None:
        added = row["med_lat"] - (row["baseline_lat"] or 0.0)
        terms.append((W_LAT, _clamp01(1.0 - max(0.0, added) / ADDED_LAT_ZERO_MS)))
    if row["med_jit"] is not None:
        terms.append((W_JIT, _clamp01(1.0 - row["med_jit"] / JITTER_ZERO_MS)))
    weight = sum(w for w, _ in terms)
    raw = sum(w * v for w, v in terms) / weight
    attempted = max(1, row["attempted"] or 1)
    reliability = (row["successes"] or 0) / attempted
    loss_factor = max(0.0, 1.0 - (row["loss_pct"] or 0.0) / 10.0)
    return 100.0 * raw * reliability * loss_factor


@dataclass
class Estimate:
    hostname: str
    est: float                 # conservative standing, 0-100
    mean: float                # weighted mean standing of successful observations
    n: int                     # observations carrying a standing
    n_eff: float               # recency-weighted effective sample size
    fail_rate: float           # recency-weighted share of failed observations
    last_seen: str             # ISO timestamp of the newest observation
    med_lat: Optional[float]   # median probe latency over recent screen/quick runs
    city: str = ""


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_estimates(rows: Sequence[Mapping[str, Any]], now: Optional[datetime] = None,
                      halflife_h: float = 24.0) -> Dict[str, Estimate]:
    """Fold observation rows (see Store.observations) into per-node estimates."""
    now = now or datetime.now(timezone.utc)
    by_host: Dict[str, List[Mapping[str, Any]]] = {}
    for r in rows:
        by_host.setdefault(r["hostname"], []).append(r)

    out: Dict[str, Estimate] = {}
    for host, obs in by_host.items():
        w_all = w_fail = 0.0
        pts: List[tuple] = []        # (weight, standing)
        lats: List[float] = []
        for r in obs:
            age_h = max(0.0, (now - _parse(r["started_at"])).total_seconds() / 3600.0)
            w = 0.5 ** (age_h / halflife_h)
            w_all += w
            if r["status"] == "failed":
                w_fail += w
                continue
            s = standing_score(r)
            if s is not None:
                pts.append((w, s))
            if r["tier"] in ("screen", "quick") and r["med_lat"] is not None:
                lats.append(float(r["med_lat"]))
        if not pts:
            continue
        sw = sum(w for w, _ in pts)
        mean = sum(w * s for w, s in pts) / sw
        n_eff = sw * sw / sum(w * w for w, _ in pts)
        var = sum(w * (s - mean) ** 2 for w, s in pts) / sw
        sd = math.sqrt((n_eff * var + PRIOR_WEIGHT * PRIOR_SD ** 2) / (n_eff + PRIOR_WEIGHT))
        fail_rate = w_fail / w_all if w_all else 0.0
        est = mean * (1.0 - fail_rate) - sd / math.sqrt(n_eff)
        lats.sort()
        med_lat = lats[len(lats) // 2] if lats else None
        out[host] = Estimate(host, round(max(0.0, est), 2), round(mean, 2), len(pts),
                             round(n_eff, 2), round(fail_rate, 3), obs[-1]["started_at"],
                             med_lat, (obs[-1]["city"] if "city" in obs[-1].keys() else "") or "")
    return out


def ranked(estimates: Mapping[str, Estimate], allowed: Optional[Sequence[str]] = None
           ) -> List[Estimate]:
    """Estimates, best first; optionally limited to the `allowed` hostnames."""
    pool = [e for h, e in estimates.items() if allowed is None or h in allowed]
    return sorted(pool, key=lambda e: (-e.est, e.hostname))


def top_group(ordered: Sequence[Estimate]) -> List[Estimate]:
    """Estimates within TIE_MARGIN of the best: statistically a tie."""
    if not ordered:
        return []
    cut = ordered[0].est * (1.0 - TIE_MARGIN)
    return [e for e in ordered if e.est >= cut]


def window_start(days: float, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).isoformat(timespec="seconds")
