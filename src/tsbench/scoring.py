"""Composite scoring for one benchmark run."""

from __future__ import annotations

from typing import List, Optional

from tsbench.models import NodeResult

# Composite score weights (must sum to 1.0).  Documented in the README.
W_DOWN, W_UP, W_LAT, W_JIT = 0.40, 0.20, 0.30, 0.10

TOP_N = 3  # how many nodes a run recommends


def score_nodes(results: List[NodeResult]) -> None:
    """Composite score, 0-100, relative to the best node IN THIS RUN.

        raw   = 0.40*D + 0.20*U + 0.30*L + 0.10*J
          D = med_down / best_med_down          (higher is better)
          U = med_up   / best_med_up
          L = best_med_lat / med_lat            (lower is better)
          J = best_med_jit / med_jit            (lower is better)
        score = 100 * raw * reliability * loss_factor
          reliability = successful_runs / attempted_runs
          loss_factor = max(0, 1 - median_packet_loss_pct / 10)

    Normalisation pool excludes the DIRECT baseline (it is a reference, not a
    candidate) and excludes nodes whose status is not 'ok', so one dead node
    cannot flatten everyone else's numbers.  Missing metrics score 0 for their
    term rather than being silently dropped.

    Because each term is normalised to the run's best node, scores are only
    comparable WITHIN a run.  Cross-run comparison uses the raw medians.
    """
    pool = [r for r in results if r.status == "ok" and not r.node.is_baseline]
    if not pool:
        return

    def best(attr: str, lower_better: bool) -> Optional[float]:
        vals = [getattr(r, attr) for r in pool if getattr(r, attr) is not None]
        vals = [v for v in vals if v > 0]
        if not vals:
            return None
        return min(vals) if lower_better else max(vals)

    b_down = best("med_down", False)
    b_up = best("med_up", False)
    b_lat = best("med_lat", True)
    b_jit = best("med_jit", True)

    def frac(val: Optional[float], ref: Optional[float], lower_better: bool) -> float:
        if val is None or ref is None or val <= 0:
            return 0.0
        return min(1.0, ref / val) if lower_better else min(1.0, val / ref)

    for r in pool:
        raw = (W_DOWN * frac(r.med_down, b_down, False)
               + W_UP * frac(r.med_up, b_up, False)
               + W_LAT * frac(r.med_lat, b_lat, True)
               + W_JIT * frac(r.med_jit, b_jit, True))
        attempted = max(1, len(r.runs))
        reliability = r.successes / attempted
        loss = r.loss_pct or 0.0
        loss_factor = max(0.0, 1.0 - loss / 10.0)
        r.score = round(100.0 * raw * reliability * loss_factor, 2)

    ranked = sorted(pool, key=lambda r: (-(r.score or 0.0), r.node.hostname))
    for i, r in enumerate(ranked, 1):
        r.rank = i
