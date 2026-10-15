# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.0.0] - unreleased

A rewrite of the 1.0 single-file script into a tested, stdlib-only package.

### Added

- `--screen-only`: rank every exit node by connect latency, jitter and failed
  handshakes in about two minutes, with no throughput test.
- `--quick`: screen, then one short download probe on the best candidates plus
  the best remembered nodes and a direct baseline. Needs no Ookla binary.
- Full runs now screen first and benchmark only the survivors (`--no-screen`
  restores the old behaviour); `--max-nodes` keeps the best N by screen.
- A local SQLite history of every run (`~/.local/state/tsbench`, override with
  `TSBENCH_HOME`), with `--save` / `--no-save`.
- `tsbench status`: each node's remembered standing and the group of nodes that
  are effectively tied.
- `tsbench apply`: switch exit node from memory without benchmarking, with
  hysteresis (`--if-better`, a 10% margin, a 2 hour cooldown), a health check,
  and a guard that never touches a node tsbench does not manage.
- `tsbench auto`: one unattended step for a scheduler. It runs a full benchmark
  when the last one is stale, otherwise a one-minute refresh of the best
  remembered nodes (`--quick --remembered N`), then `apply --if-better`.
- Guards for scheduled runs: battery, recent keyboard/mouse activity, and a call
  in progress (a configurable, deliberately narrow list of process names).
- A run lock so a manual run, `apply` and `auto` never fight over the exit node.
- `tsbench schedule install|uninstall|status`: a macOS launchd user agent with
  absolute tool paths, a rotated log, and no run at login.
- Raycast Script Commands (apply best, quick re-test, show best), a systemd
  timer and a cron example.
- `tsbench status --oneline`.
- UK-wide coverage by default (London, Manchester, Glasgow).
- `SECURITY.md`, a test suite, CI and a rewritten README.

### Changed

- Ookla's licence is no longer accepted silently: full runs need
  `--accept-ookla-license` or `TSBENCH_ACCEPT_OOKLA_LICENSE=1`.
- File export needs an explicit `--out-dir`.
- Rankings are presented as a top group rather than a strict order, because
  repeated runs showed the healthy nodes are close to a statistical tie.

### Security

- The history database and its directory are created owner-only.
- Control characters and terminal escapes in peer names are stripped, and names
  are sanitised before they are used in file names (`--keep-raw`).

### Removed

- The never-implemented `--sequential-only` and `--parallel` flags.

The original script is kept at `legacy/tsbench-1.0.0.py`.
