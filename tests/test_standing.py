from datetime import datetime, timedelta, timezone

import pytest

from tsbench.standing import (
    TIE_MARGIN,
    compute_estimates,
    ranked,
    standing_score,
    top_group,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def obs(host, hours_ago=0.0, down=400.0, up=None, lat=12.0, jit=1.0, loss=0.0, status="ok",
        successes=1, attempted=1, tier="quick", base_down=800.0, base_up=None, base_lat=10.0,
        city="London"):
    ts = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return {"hostname": host, "status": status, "successes": successes, "attempted": attempted,
            "med_down": down, "med_up": up, "med_lat": lat, "med_jit": jit, "loss_pct": loss,
            "started_at": ts, "tier": tier, "network_tag": None, "baseline_down": base_down,
            "baseline_up": base_up, "baseline_lat": base_lat, "city": city}


def test_standing_compares_to_the_same_runs_baseline():
    fast = standing_score(obs("a", down=800.0, lat=10.0, jit=0.0))
    half = standing_score(obs("a", down=400.0, lat=10.0, jit=0.0))
    assert fast == pytest.approx(100.0)
    assert half < fast and half == pytest.approx(100 * (0.4 * 0.5 + 0.3 + 0.1) / 0.8)


def test_standing_is_comparable_across_baselines():
    # same fraction of direct speed on a slow and on a fast line
    slow_line = standing_score(obs("a", down=33.0, base_down=66.0))
    fast_line = standing_score(obs("a", down=400.0, base_down=800.0))
    assert slow_line == pytest.approx(fast_line, rel=0.02)


def test_latency_is_judged_as_latency_added_over_direct():
    near = standing_score(obs("a", lat=12.0, base_lat=10.0))
    far = standing_score(obs("a", lat=70.0, base_lat=10.0))
    assert near > far


def test_no_baseline_or_no_throughput_gives_no_standing():
    assert standing_score(obs("a", base_down=None)) is None
    assert standing_score(obs("a", down=None, tier="screen")) is None


def test_upload_term_is_used_only_when_both_sides_have_it():
    with_up = standing_score(obs("a", up=100.0, base_up=100.0))
    without = standing_score(obs("a", up=None, base_up=None))
    assert with_up != without


def test_partial_runs_and_packet_loss_reduce_standing():
    clean = standing_score(obs("a"))
    assert standing_score(obs("a", successes=1, attempted=2)) == pytest.approx(clean / 2)
    assert standing_score(obs("a", loss=5.0)) == pytest.approx(clean / 2)


def test_recent_observations_outweigh_old_ones():
    rows = [obs("a", hours_ago=100, down=800.0), obs("a", hours_ago=1, down=200.0)]
    est = compute_estimates(rows, NOW, halflife_h=24)["a"]
    old_only = compute_estimates([rows[0]], NOW)["a"].mean
    new_only = compute_estimates([rows[1]], NOW)["a"].mean
    assert abs(est.mean - new_only) < abs(est.mean - old_only)


def test_evidence_beats_a_single_lucky_observation():
    steady = [obs("steady", hours_ago=h, down=600.0) for h in range(0, 12, 2)]
    lucky = [obs("lucky", hours_ago=0, down=700.0)]
    est = compute_estimates(steady + lucky, NOW)
    assert est["lucky"].mean > est["steady"].mean       # better on the day...
    assert est["steady"].est > est["lucky"].est         # ...but less trusted


def test_failures_lower_the_estimate_and_are_counted():
    good = [obs("flaky", hours_ago=h) for h in (1, 2, 3)]
    bad = [obs("flaky", hours_ago=h, status="failed", down=None) for h in (1, 2, 3)]
    est = compute_estimates(good + bad, NOW)
    clean = compute_estimates(good, NOW)
    assert est["flaky"].fail_rate == pytest.approx(0.5, abs=0.05)
    assert est["flaky"].est < clean["flaky"].est


def test_a_node_with_only_failures_or_screens_has_no_estimate():
    rows = [obs("dead", status="failed", down=None),
            obs("screened", down=None, tier="screen", status="ok")]
    assert compute_estimates(rows, NOW) == {}


def test_median_probe_latency_comes_only_from_probe_tiers():
    rows = [obs("a", tier="quick", lat=12.0), obs("a", tier="screen", lat=14.0, down=None),
            obs("a", tier="full", lat=99.0)]
    assert compute_estimates(rows, NOW)["a"].med_lat in (12.0, 14.0)


def test_ranking_and_tie_group():
    rows = ([obs("best", down=600.0)] * 3 + [obs("tie", down=590.0)] * 3
            + [obs("weak", down=200.0)] * 3)
    order = ranked(compute_estimates(rows, NOW))
    assert [e.hostname for e in order][0] == "best"
    group = [e.hostname for e in top_group(order)]
    assert "best" in group and "weak" not in group
    assert all(e.est >= order[0].est * (1 - TIE_MARGIN) for e in top_group(order))


def test_ranked_can_be_limited_to_available_nodes():
    est = compute_estimates([obs("a"), obs("b", down=300.0)], NOW)
    assert [e.hostname for e in ranked(est, allowed=["b"])] == ["b"]


def test_city_travels_with_the_estimate():
    assert compute_estimates([obs("a", city="Glasgow")], NOW)["a"].city == "Glasgow"
