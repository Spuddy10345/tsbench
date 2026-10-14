from datetime import timedelta

import pytest

from test_standing import NOW, obs
from tsbench import apply as apply_mod
from tsbench.apply import Health, assess_health, decide
from tsbench.models import Node
from tsbench.standing import compute_estimates


def node(host, last):
    return Node(hostname=host, dns_name="", ip=f"100.64.0.{last}", city="London", city_code="lon",
                country="UK", country_code="GB", online=True)


A, B, C, OTHER = node("a", 1), node("b", 2), node("c", 3), node("not-ours", 9)


def estimates(**downs):
    rows = []
    for host, down in downs.items():
        rows += [obs(host, hours_ago=h, down=down) for h in (1, 2, 3, 4)]
    return compute_estimates(rows, NOW)


def run(current, est, health=None, last=None, **kw):
    cur = {"a": A, "b": B, "c": C, "x": OTHER}.get(current)
    return decide([A, B, C], est, cur, cur.ip if cur else None, health, last, now=NOW, **kw)


def ago(hours, host="a"):
    return {"hostname": host, "ts": (NOW - timedelta(hours=hours)).isoformat()}


# -- health ------------------------------------------------------------------

def test_failed_probe_is_unhealthy():
    h = assess_health(None, [10, 11, 12])
    assert not h.ok and "failed" in h.reason


def test_latency_far_above_its_own_median_is_unhealthy():
    assert not assess_health(30.0, [10, 11, 12]).ok


def test_small_absolute_blips_are_not_unhealthy():
    assert assess_health(16.0, [10, 10, 10]).ok          # 1.6x but only +6 ms


def test_no_history_means_nothing_to_compare_with():
    assert assess_health(500.0, [10, 11]).ok


# -- decide: plain apply -------------------------------------------------------

def test_plain_apply_goes_to_the_best_remembered_node():
    d = run("b", estimates(a=700.0, b=300.0, c=500.0), if_better=False)
    assert d.action == "switch" and d.target == "a"


def test_plain_apply_stays_when_already_on_the_best():
    d = run("a", estimates(a=700.0, b=300.0), if_better=False)
    assert d.action == "stay"


def test_no_data_is_a_skip_with_a_hint():
    d = run("a", {}, if_better=False)
    assert d.action == "skip" and "run --quick" in d.reason


def test_only_currently_available_nodes_are_candidates():
    est = estimates(a=700.0, ghost=900.0)
    assert run("b", est, if_better=False).target == "a"


# -- decide: --if-better ---------------------------------------------------------

def test_clearly_better_node_triggers_a_switch():
    d = run("b", estimates(a=700.0, b=300.0), if_better=True)
    assert d.action == "switch" and d.target == "a" and "beats" in d.reason


def test_a_tie_is_not_worth_switching_for():
    d = run("b", estimates(a=620.0, b=600.0), if_better=True)
    assert d.action == "stay" and "tie" in d.reason


def test_cooldown_blocks_a_margin_based_switch():
    d = run("b", estimates(a=700.0, b=300.0), last=ago(1.0, "b"), if_better=True)
    assert d.action == "stay" and "cooldown" in d.reason


def test_cooldown_expires():
    d = run("b", estimates(a=700.0, b=300.0), last=ago(3.0, "b"), if_better=True)
    assert d.action == "switch"


def test_unhealthy_current_node_bypasses_the_cooldown():
    d = run("b", estimates(a=700.0, b=690.0), health=Health(False, "dead"),
            last=ago(0.1, "b"), if_better=True)
    assert d.action == "switch" and d.target == "a" and "unhealthy" in d.reason


def test_unhealthy_best_node_switches_to_the_next_best():
    d = run("a", estimates(a=700.0, b=600.0, c=300.0), health=Health(False, "dead"),
            if_better=True)
    assert d.action == "switch" and d.target == "b"


def test_direct_connectivity_is_never_hijacked():
    d = run(None, estimates(a=700.0), if_better=True)
    assert d.action == "skip" and "direct" in d.reason


def test_a_manual_choice_outside_the_history_is_left_alone():
    d = run("x", estimates(a=700.0), if_better=True)
    assert d.action == "skip" and "not chosen by tsbench" in d.reason


def test_force_overrides_the_manual_choice_guard_and_cooldown():
    d = run("x", estimates(a=700.0), last=ago(0.1, "a"), if_better=True, force=True)
    assert d.action == "switch" and d.target == "a"


def test_a_node_applied_by_tsbench_counts_as_managed_even_without_estimates():
    d = run("b", estimates(a=700.0), last=ago(5.0, "b"), if_better=True)
    assert d.action == "switch" and d.target == "a"


def test_margin_is_configurable():
    est = estimates(a=640.0, b=600.0)
    assert run("b", est, if_better=True, margin=0.01).action == "switch"
    assert run("b", est, if_better=True, margin=0.50).action == "stay"


# -- switching -------------------------------------------------------------------

class FakeTS:
    def __init__(self, ok=True, reports=True):
        self.ok, self.reports, self.current = ok, reports, None

    def set_exit_node(self, target):
        if self.ok and self.reports:
            self.current = target
        return (True, "") if self.ok else (False, "denied")

    def current_exit_ip(self):
        return self.current


def test_switch_exit_succeeds_when_tailscale_confirms():
    assert apply_mod.switch_exit(FakeTS(), A) == ""


def test_switch_exit_reports_a_rejected_set():
    assert "denied" in apply_mod.switch_exit(FakeTS(ok=False), A)


def test_switch_exit_reports_an_unconfirmed_switch(monkeypatch):
    monkeypatch.setattr(apply_mod.time, "sleep", lambda _s: None)
    ts = FakeTS(reports=False)
    t = iter(range(100))
    monkeypatch.setattr(apply_mod.time, "monotonic", lambda: next(t))
    assert "did not report" in apply_mod.switch_exit(ts, A, wait_s=3)


def test_cooldown_uses_utc_timestamps_without_offsets():
    last = {"hostname": "b", "ts": (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()}
    assert run("b", estimates(a=700.0, b=300.0), last=last, if_better=True).action == "stay"


@pytest.mark.parametrize("cur", ["a", "b"])
def test_decisions_serialise(cur):
    d = run(cur, estimates(a=700.0, b=300.0), if_better=False).as_dict()
    assert set(d) >= {"action", "reason", "current", "target", "target_ip"}
