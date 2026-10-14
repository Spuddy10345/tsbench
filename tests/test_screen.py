from tsbench.models import Node, NodeResult, RunResult
from tsbench.screen import cut_outliers, failed_to_screen, pick_candidates, rank_screen, screen_key


def result(host, lat, jit=1.0, loss=0.0, status="ok", baseline=False):
    node = Node(hostname=host, dns_name="", ip=f"100.64.0.{abs(hash(host)) % 200}", city="London",
                city_code="lon", country="UK", country_code="GB", online=True,
                is_baseline=baseline)
    nr = NodeResult(node=node, connected=status != "failed")
    nr.med_lat, nr.med_jit, nr.loss_pct, nr.status = lat, jit, loss, status
    nr.runs = [RunResult(rep=1, ok=status == "ok", started_at="t", duration_s=1.0)]
    return nr


def test_screen_key_penalises_jitter_and_loss():
    assert screen_key(result("a", 10, jit=0, loss=0)) == 10
    assert screen_key(result("a", 10, jit=3, loss=0)) == 16
    assert screen_key(result("a", 10, jit=0, loss=20)) == 410


def test_rank_orders_by_key_and_excludes_baseline_and_failures():
    rs = [result("slow", 30), result("fast", 8), result("direct", 5, baseline=True),
          result("dead", None, status="failed"), result("jittery", 7, jit=10)]
    assert [r.node.hostname for r in rank_screen(rs)] == ["fast", "jittery", "slow"]


def test_ties_break_by_hostname_for_stable_output():
    rs = [result("b", 10), result("a", 10)]
    assert [r.node.hostname for r in rank_screen(rs)] == ["a", "b"]


def test_cut_outliers_drops_only_clear_outliers():
    rs = rank_screen([result(f"n{i}", 10 + i) for i in range(6)] + [result("far", 90)])
    kept, dropped = cut_outliers(rs)
    assert [r.node.hostname for r in dropped] == ["far"]
    assert len(kept) == 6


def test_a_farther_city_is_not_an_outlier():
    # Glasgow-style: a few ms further than London must survive the cut
    rs = rank_screen([result(f"lon{i}", 11 + i * 0.5) for i in range(5)] + [result("glw", 24)])
    assert cut_outliers(rs)[1] == []


def test_too_few_nodes_to_call_an_outlier():
    rs = rank_screen([result("a", 5), result("b", 400)])
    assert cut_outliers(rs) == (rs, [])


def test_pick_candidates_takes_top_k_plus_favourites_in_screen_order():
    rs = rank_screen([result(f"n{i}", 10 + i) for i in range(8)])
    picked = pick_candidates(rs, ["n7", "n1"], top_k=3)
    assert [n.hostname for n in picked] == ["n0", "n1", "n2", "n7"]   # n1 already in top 3


def test_favourites_that_failed_the_screen_are_not_resurrected():
    rs = rank_screen([result(f"n{i}", 10 + i) for i in range(4)])
    assert [n.hostname for n in pick_candidates(rs, ["ghost"], top_k=2)] == ["n0", "n1"]


def test_failed_to_screen_lists_non_baseline_failures():
    rs = [result("ok", 5), result("dead", None, status="failed"),
          result("direct", None, status="failed", baseline=True)]
    assert [r.node.hostname for r in failed_to_screen(rs)] == ["dead"]
