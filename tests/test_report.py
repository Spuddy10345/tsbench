import csv
import json
import os

from conftest import build_results
from tsbench.console import Term
from tsbench.models import ranked_ok
from tsbench.report import display_order, render_table, write_outputs
from tsbench.scoring import TOP_N, score_nodes


def scored(run_fixture):
    results = build_results(run_fixture)
    score_nodes(results)
    recommended = ranked_ok(results)[:TOP_N]
    for r in recommended:
        r.recommended = True
    return results, recommended


META = {"started_at": "2026-01-01T00:00:00+00:00", "duration_s": 99.0, "include_cities": [],
        "exclude_cities": [], "tester": "ookla", "units_note": "units", "reps": 3,
        "pinned_server": 1, "nodes_benchmarked": 14, "original_exit_node": "none",
        "restored_to": "none", "min_success": 2}


def test_display_order_is_ranked_then_baseline(run_fixture):
    results, _ = scored(run_fixture)
    order = display_order(results)
    assert order[0].rank == 1
    assert order[14].node.is_baseline          # 14 ranked nodes first, then the baseline
    assert [r.rank for r in order[:14]] == list(range(1, 15))


def test_table_marks_the_recommended_nodes(run_fixture):
    results, _ = scored(run_fixture)
    text = render_table(results, 3, Term(force_colour=False))
    assert text.count("RECOMMENDED") == TOP_N
    assert "baseline (ref)" in text


def test_write_outputs_creates_all_files(tmp_path, run_fixture):
    results, recommended = scored(run_fixture)
    run_dir = str(tmp_path / "run-x")
    paths = write_outputs(run_dir, results, dict(META), recommended)
    assert set(paths) == {"json", "csv", "runs_csv", "markdown"}
    assert all(os.path.exists(p) for p in paths.values())

    data = json.load(open(paths["json"]))
    assert data["tool"] == "tsbench" and len(data["results"]) == 15
    assert "host" not in data["meta"]          # no machine identity in exports

    rows = list(csv.DictReader(open(paths["csv"])))
    assert len(rows) == 15 and rows[0]["recommended"] in ("True", "False")
    md = open(paths["markdown"]).read()
    assert "## Recommendation" in md and "tailscale set --exit-node=" in md


def test_markdown_handles_a_run_with_no_recommendation(tmp_path):
    paths = write_outputs(str(tmp_path / "r"), [], dict(META), [])
    assert "No node produced enough valid data" in open(paths["markdown"]).read()
