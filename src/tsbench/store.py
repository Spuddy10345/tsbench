"""Persistent run history (SQLite, standard library).

Raw measurements are stored, never per-run normalised scores alone: those are
only comparable within a run, so cross-run reasoning has to start from medians
and individual samples.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from tsbench.models import NodeResult

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    tier          TEXT NOT NULL,
    duration_s    REAL,
    tester        TEXT,
    reps          INTEGER,
    pinned_server INTEGER,
    tool_version  TEXT,
    network_tag   TEXT,
    baseline_down REAL,
    baseline_up   REAL,
    baseline_lat  REAL,
    original_exit TEXT,
    applied_node  TEXT,
    interrupted   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS node_runs (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    hostname   TEXT NOT NULL,
    ip         TEXT,
    city       TEXT,
    status     TEXT,
    verified   INTEGER,
    rank       INTEGER,
    score      REAL,
    med_down   REAL,
    med_up     REAL,
    med_lat    REAL,
    med_jit    REAL,
    loss_pct   REAL,
    successes  INTEGER,
    attempted  INTEGER
);
CREATE TABLE IF NOT EXISTS samples (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    hostname   TEXT NOT NULL,
    ts         TEXT,
    rep        INTEGER,
    ok         INTEGER NOT NULL,
    down_mbps  REAL,
    up_mbps    REAL,
    lat_ms     REAL,
    jit_ms     REAL,
    loss_pct   REAL,
    server_id  INTEGER,
    error      TEXT
);
CREATE TABLE IF NOT EXISTS applies (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    hostname  TEXT,
    ip        TEXT,
    previous  TEXT,
    reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_node_runs_host ON node_runs(hostname, run_id);
CREATE INDEX IF NOT EXISTS idx_samples_host ON samples(hostname, run_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} was written by a newer tsbench (schema {version}); "
                f"this version understands up to {SCHEMA_VERSION}.")
        with self.conn:
            self.conn.executescript(_SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- writes ------------------------------------------------------------- #

    def save_run(self, meta: Dict[str, Any], results: List[NodeResult],
                 tier: str = "full") -> int:
        """Persist one run atomically and return its id."""
        base = next((r for r in results if r.node.is_baseline and r.status == "ok"), None)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (started_at, tier, duration_s, tester, reps, pinned_server,"
                " tool_version, network_tag, baseline_down, baseline_up, baseline_lat,"
                " original_exit, applied_node, interrupted)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (meta.get("started_at") or _utcnow(), tier, meta.get("duration_s"),
                 meta.get("tester"), meta.get("reps"), meta.get("pinned_server"),
                 meta.get("tool_version"), meta.get("network_tag"),
                 base.med_down if base else None, base.med_up if base else None,
                 base.med_lat if base else None, meta.get("original_exit_node"),
                 meta.get("restored_to"), int(bool(meta.get("interrupted")))))
            run_id = int(cur.lastrowid or 0)
            for r in results:
                if r.node.is_baseline:
                    continue
                host = r.node.hostname
                self.conn.execute(
                    "INSERT INTO node_runs (run_id, hostname, ip, city, status, verified, rank,"
                    " score, med_down, med_up, med_lat, med_jit, loss_pct, successes, attempted)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, host, r.node.ip, r.node.city, r.status, int(r.verified), r.rank,
                     r.score, r.med_down, r.med_up, r.med_lat, r.med_jit, r.loss_pct,
                     r.successes, len(r.runs)))
                if not r.runs:
                    # Could not even connect: record it so reliability is learnable.
                    self.conn.execute(
                        "INSERT INTO samples (run_id, hostname, ts, rep, ok, error)"
                        " VALUES (?,?,?,?,?,?)",
                        (run_id, host, meta.get("started_at"), 0, 0, r.connect_error))
                for x in r.runs:
                    self.conn.execute(
                        "INSERT INTO samples (run_id, hostname, ts, rep, ok, down_mbps, up_mbps,"
                        " lat_ms, jit_ms, loss_pct, server_id, error)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, host, x.started_at, x.rep, int(x.ok), x.download_mbps,
                         x.upload_mbps, x.latency_ms, x.jitter_ms, x.packet_loss_pct,
                         x.server_id, x.error))
        return run_id

    def prune(self, older_than_days: float) -> int:
        """Delete runs (and their samples) older than N days; returns runs deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(
            timespec="seconds")
        with self.conn:
            cur = self.conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        return cur.rowcount

    # -- reads -------------------------------------------------------------- #

    def list_runs(self, limit: int = 20) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM node_runs n WHERE n.run_id = r.id) AS nodes"
            " FROM runs r ORDER BY r.started_at DESC, r.id DESC LIMIT ?", (limit,)).fetchall()

    def node_runs(self, run_id: int) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM node_runs WHERE run_id = ? ORDER BY rank IS NULL, rank, hostname",
            (run_id,)).fetchall()

    def samples(self, run_id: int) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM samples WHERE run_id = ? ORDER BY hostname, rep", (run_id,)).fetchall()

    def observations(self, since: str, network_tag: Optional[str] = None) -> List[sqlite3.Row]:
        """Per-node results since `since` joined with their run, oldest first.
        With `network_tag`, runs recorded on a different network are left out
        (runs with no tag are kept)."""
        sql = ("SELECT n.hostname, n.status, n.successes, n.attempted, n.med_down, n.med_up,"
               " n.med_lat, n.med_jit, n.loss_pct, n.city, r.started_at, r.tier, r.network_tag,"
               " r.baseline_down, r.baseline_up, r.baseline_lat"
               " FROM node_runs n JOIN runs r ON r.id = n.run_id WHERE r.started_at >= ?")
        args: List[Any] = [since]
        if network_tag:
            sql += " AND (r.network_tag IS NULL OR r.network_tag = ?)"
            args.append(network_tag)
        return self.conn.execute(sql + " ORDER BY r.started_at, n.id", args).fetchall()

    def latest_network_tag(self) -> Optional[str]:
        row = self.conn.execute("SELECT network_tag FROM runs WHERE network_tag IS NOT NULL"
                                " ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def latency_history(self, hostname: str, limit: int = 20) -> List[float]:
        """Recent probe-measured latencies (screen and quick tiers only: the
        full tier uses Ookla's ping, which is a different measurement)."""
        rows = self.conn.execute(
            "SELECT n.med_lat FROM node_runs n JOIN runs r ON r.id = n.run_id"
            " WHERE n.hostname = ? AND r.tier IN ('screen', 'quick') AND n.status = 'ok'"
            " AND n.med_lat IS NOT NULL ORDER BY r.started_at DESC, n.id DESC LIMIT ?",
            (hostname, limit)).fetchall()
        return [float(r[0]) for r in rows]

    def record_apply(self, hostname: Optional[str], ip: Optional[str], previous: Optional[str],
                     reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO applies (ts, hostname, ip, previous, reason) VALUES (?,?,?,?,?)",
                (_utcnow(), hostname, ip, previous, reason))

    def last_apply(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM applies ORDER BY ts DESC, id DESC LIMIT 1").fetchone()

    def last_run_at(self, tier: Optional[str] = None) -> Optional[str]:
        if tier:
            row = self.conn.execute(
                "SELECT MAX(started_at) FROM runs WHERE tier = ? AND interrupted = 0",
                (tier,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT MAX(started_at) FROM runs WHERE interrupted = 0").fetchone()
        return row[0] if row else None
