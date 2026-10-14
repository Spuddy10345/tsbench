from conftest import build_results
from tsbench.models import Node, NodeResult, RunResult, ranked_ok
from tsbench.scoring import W_DOWN, W_JIT, W_LAT, W_UP, score_nodes


def _result(host, down, up, lat, jit, loss=0.0, reps=3, ok=3, baseline=False):
    node = Node(hostname=host, dns_name="", ip="100.64.0.1", city="London", city_code="LON",
                country="UK", country_code="GB", online=True, is_baseline=baseline)
    nr = NodeResult(node=node, connected=True)
    for i in range(reps):
        good = i < ok
        nr.runs.append(RunResult(
            rep=i + 1, ok=good, started_at="t", duration_s=1.0,
            download_mbps=down if good else None, upload_mbps=up if good else None,
            latency_ms=lat if good else None, jitter_ms=jit if good else None,
            packet_loss_pct=loss if good else None))
    nr.aggregate(min_success=2)
    return nr


def test_weights_sum_to_one():
    assert abs(W_DOWN + W_UP + W_LAT + W_JIT - 1.0) < 1e-9


def test_best_on_every_metric_scores_100():
    best = _result("a", 100, 50, 10, 1)
    worse = _result("b", 50, 25, 20, 2)
    score_nodes([best, worse])
    assert best.score == 100.0 and best.rank == 1
    assert worse.score < best.score and worse.rank == 2


def test_baseline_is_not_scored_or_ranked():
    base = _result("", 900, 100, 5, 0.5, baseline=True)
    node = _result("a", 100, 50, 10, 1)
    score_nodes([base, node])
    assert base.score is None and base.rank is None
    assert node.score == 100.0  # the baseline must not become the normalisation reference


def test_inconclusive_node_does_not_flatten_the_others():
    flaky = _result("flaky", 10_000, 10_000, 1, 0.1, ok=1)  # 1/3 ok -> inconclusive
    steady = _result("steady", 100, 50, 10, 1)
    score_nodes([flaky, steady])
    assert flaky.status == "inconclusive" and flaky.score is None
    assert steady.score == 100.0


def test_reliability_penalises_failed_reps():
    full = _result("full", 100, 50, 10, 1, ok=3)
    partial = _result("partial", 100, 50, 10, 1, ok=2)
    score_nodes([full, partial])
    assert partial.status == "ok"
    assert abs(partial.score - full.score * 2 / 3) < 0.01


def test_packet_loss_scales_the_score():
    clean = _result("clean", 100, 50, 10, 1, loss=0)
    lossy = _result("lossy", 100, 50, 10, 1, loss=5)
    score_nodes([clean, lossy])
    assert abs(lossy.score - clean.score * 0.5) < 0.01


def test_no_conclusive_nodes_is_a_noop():
    dead = _result("dead", 0, 0, 0, 0, ok=0)
    score_nodes([dead])
    assert dead.status == "failed" and dead.score is None and dead.rank is None


def test_ranked_ok_orders_by_rank():
    a, b, c = _result("a", 100, 50, 10, 1), _result("b", 80, 40, 12, 1), _result("c", 60, 30, 14, 1)
    score_nodes([c, a, b])
    assert [r.node.hostname for r in ranked_ok([c, a, b])] == ["a", "b", "c"]


def test_reproduces_the_original_script_on_real_data(run_fixture):
    """Parity check: re-scoring the saved 25 Sep run gives the same scores and
    ranks the 1.0.0 script produced."""
    results = build_results(run_fixture)
    score_nodes(results)
    by_host = {r.node.hostname: r for r in results if not r.node.is_baseline}
    for n in run_fixture["results"]:
        if n["is_baseline"]:
            continue
        r = by_host[n["hostname"]]
        assert r.rank == n["stored_rank"], n["hostname"]
        assert abs(r.score - n["stored_score"]) < 0.01, n["hostname"]
