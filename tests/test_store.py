import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from conftest import build_results
from tsbench.models import Node, NodeResult
from tsbench.scoring import score_nodes
from tsbench.store import SCHEMA_VERSION, Store


def _meta(started_at=None, **extra):
    m = {"started_at": started_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "duration_s": 120.0, "tester": "ookla", "reps": 3, "pinned_server": 42760,
         "tool_version": "test", "original_exit_node": "gb-lon-wg-001 (100.64.0.1)",
         "restored_to": "gb-lon-wg-001 (100.64.0.1)", "interrupted": False}
    m.update(extra)
    return m


@pytest.fixture
def store(tmp_path):
    with Store(str(tmp_path / "state" / "t.db")) as s:   # parent dir is created on demand
        yield s


def test_creates_parent_directory_and_sets_schema_version(tmp_path):
    path = tmp_path / "a" / "b" / "t.db"
    Store(str(path)).close()
    assert path.exists()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_refuses_a_database_from_a_newer_version(tmp_path):
    path = tmp_path / "t.db"
    sqlite3.connect(path).execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}").connection.close()
    with pytest.raises(RuntimeError, match="newer tsbench"):
        Store(str(path))


def test_round_trip_of_a_real_run(store, run_fixture):
    results = build_results(run_fixture)
    score_nodes(results)
    run_id = store.save_run(_meta(), results, tier="full")

    runs = store.list_runs()
    assert len(runs) == 1 and runs[0]["id"] == run_id and runs[0]["nodes"] == 14
    assert runs[0]["baseline_down"] and runs[0]["tier"] == "full"

    node_rows = store.node_runs(run_id)
    assert len(node_rows) == 14                       # baseline is not a node row
    assert node_rows[0]["rank"] == 1                  # ordered best first
    assert all(r["hostname"] for r in node_rows)

    samples = store.samples(run_id)
    assert len(samples) == 14 * 3
    assert {s["ok"] for s in samples} == {1}


def test_unreachable_node_is_recorded_as_a_failed_sample(store):
    node = Node(hostname="gb-lon-wg-999", dns_name="", ip="100.64.0.99", city="London",
                city_code="LON", country="UK", country_code="GB", online=True)
    dead = NodeResult(node=node, connected=False, connect_error="timed out")
    dead.aggregate(min_success=2)
    run_id = store.save_run(_meta(), [dead])
    (sample,) = store.samples(run_id)
    assert sample["ok"] == 0 and sample["error"] == "timed out"
    assert store.node_runs(run_id)[0]["status"] == "failed"


def test_prune_deletes_old_runs_and_their_children(store, run_fixture):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    results = build_results(run_fixture)
    old_id = store.save_run(_meta(started_at=old), results)
    new_id = store.save_run(_meta(), results)

    assert store.prune(older_than_days=30) == 1
    assert [r["id"] for r in store.list_runs()] == [new_id]
    assert store.samples(old_id) == [] and store.node_runs(old_id) == []   # cascade
    assert store.samples(new_id)


def test_last_run_at_by_tier_ignores_interrupted_runs(store, run_fixture):
    results = build_results(run_fixture)
    assert store.last_run_at("full") is None
    store.save_run(_meta(started_at="2026-01-01T00:00:00+00:00"), results, tier="full")
    store.save_run(_meta(started_at="2026-01-02T00:00:00+00:00"), results, tier="quick")
    store.save_run(_meta(started_at="2026-01-03T00:00:00+00:00", interrupted=True), results,
                   tier="full")
    assert store.last_run_at("full") == "2026-01-01T00:00:00+00:00"
    assert store.last_run_at("quick") == "2026-01-02T00:00:00+00:00"
    assert store.last_run_at() == "2026-01-02T00:00:00+00:00"


# -- schema v2 / reads used by status and apply -------------------------------------

def test_observations_join_runs_and_filter_by_time_and_network(store, run_fixture):
    store.save_run(_meta("2026-01-01T00:00:00+00:00", network_tag="home"),
                   build_results(run_fixture), tier="full")
    store.save_run(_meta("2026-02-01T00:00:00+00:00", network_tag="cafe"),
                   build_results(run_fixture), tier="quick")
    everything = store.observations("2025-01-01")
    assert {r["tier"] for r in everything} == {"full", "quick"}
    assert all(r["baseline_down"] for r in everything)
    assert {r["tier"] for r in store.observations("2026-01-15")} == {"quick"}
    assert {r["tier"] for r in store.observations("2025-01-01", "home")} == {"full"}


def test_runs_without_a_network_tag_are_kept_when_filtering(store, run_fixture):
    store.save_run(_meta("2026-01-01T00:00:00+00:00"), build_results(run_fixture))
    assert store.observations("2025-01-01", "home")


def test_latest_network_tag(store, run_fixture):
    assert store.latest_network_tag() is None
    store.save_run(_meta("2026-01-01T00:00:00+00:00", network_tag="old"),
                   build_results(run_fixture))
    store.save_run(_meta("2026-02-01T00:00:00+00:00", network_tag="new"),
                   build_results(run_fixture))
    store.save_run(_meta("2026-03-01T00:00:00+00:00"), build_results(run_fixture))
    assert store.latest_network_tag() == "new"


def test_latency_history_uses_only_probe_tiers(store, run_fixture):
    results = build_results(run_fixture)
    host = next(r.node.hostname for r in results if not r.node.is_baseline)
    store.save_run(_meta("2026-01-01T00:00:00+00:00"), results, tier="full")
    assert store.latency_history(host) == []
    store.save_run(_meta("2026-01-02T00:00:00+00:00"), results, tier="quick")
    store.save_run(_meta("2026-01-03T00:00:00+00:00"), results, tier="screen")
    assert len(store.latency_history(host)) == 2


def test_applies_round_trip(store):
    assert store.last_apply() is None
    store.record_apply("a", "100.64.0.1", "b", "first")
    store.record_apply("c", "100.64.0.3", "a", "second")
    last = store.last_apply()
    assert last["hostname"] == "c" and last["previous"] == "a" and last["reason"] == "second"


def test_a_version_1_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, tier TEXT NOT NULL,
            duration_s REAL, tester TEXT, reps INTEGER, pinned_server INTEGER, tool_version TEXT,
            network_tag TEXT, baseline_down REAL, baseline_up REAL, baseline_lat REAL,
            original_exit TEXT, applied_node TEXT, interrupted INTEGER NOT NULL DEFAULT 0);
        INSERT INTO runs (started_at, tier) VALUES ('2026-01-01T00:00:00+00:00', 'full');
        PRAGMA user_version = 1;""")
    conn.commit()
    conn.close()
    with Store(str(path)) as s:
        assert len(s.list_runs()) == 1               # old data survives
        s.record_apply("a", "100.64.0.1", None, "after upgrade")
        assert s.last_apply()["hostname"] == "a"
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_database_and_state_dir_are_owner_only(tmp_path):
    import os
    import stat
    path = tmp_path / "private" / "t.db"
    Store(str(path)).close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
