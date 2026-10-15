# tsbench

[![CI](https://github.com/Spuddy10345/tsbench/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Spuddy10345/tsbench/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

Find the fastest Mullvad exit node on your Tailscale network, remember what it
learned, and switch to a good one without running a 30-minute benchmark every
time.

- **Short runs.** `--screen-only` and `--quick` replace a long sequential
  speed-test marathon with a latency screen and one short throughput probe on
  the few nodes that matter.
- **Memory.** Every run is saved. `tsbench status` shows what the history says
  is best, and `tsbench apply` switches to it without benchmarking anything.
- **No flapping.** `apply --if-better` only switches when the current node is
  unhealthy or clearly worse, never during a cooldown, and never overrides an
  exit node you chose yourself.
- **Safe.** Your original exit node is recorded first and restored on
  completion, on error, and on Ctrl-C.
- **Standard library only.** No pip dependencies.

> **Status: 2.0, pre-release.** Everything described here is implemented and
> covered by tests. Two things have **not** been measured on a real tailnet yet:
> how long the short tiers take end to end, and how closely a quick run agrees
> with a full run (see [Performance](#performance)). The scheduled job has been
> checked against the real launchd, but not left running for days.
>
> tsbench is an independent project. It is not affiliated with or endorsed by
> Mullvad, Tailscale or Ookla.

## Requirements

| Thing | Why | Install |
|---|---|---|
| Python 3.9+ | the tool | preinstalled on recent macOS; `apt install python3` on Linux |
| `tailscale` CLI, logged in | discovery and switching | `brew install tailscale`, or the App Store app |
| Mullvad exit nodes on your tailnet | what is being compared | Tailscale admin console, Mullvad add-on |
| `speedtest` (Ookla) | **full runs only** | `brew tap teamookla/speedtest && brew install speedtest` |

`--screen-only` and `--quick` need nothing beyond Tailscale. `librespeed-cli` is
an alternative full-run tester (`--tester librespeed`).

**No `sudo` is needed** if your user is the Tailscale operator. Check with
`tailscale debug prefs | grep OperatorUser`; if it is not you, run once:

```sh
sudo tailscale set --operator=$USER
```

> The Ookla `speedtest` binary is **not** the `speedtest-cli` Python package or
> Debian's `speedtest-cli`. They emit different JSON, and tsbench refuses to
> substitute one for the other.

### Ookla licence

tsbench does **not** accept Ookla's licence and privacy terms for you. Read
them at <https://www.speedtest.net/about/eula> and
<https://www.speedtest.net/about/privacy>, run `speedtest` once yourself, then
let tsbench run it non-interactively with `--accept-ookla-license` or
`TSBENCH_ACCEPT_OOKLA_LICENSE=1`. The quick tiers do not use Ookla at all.

## Install

From a checkout (tsbench is not published to PyPI):

```sh
git clone https://github.com/Spuddy10345/tsbench.git && cd tsbench
pip install .          # or: pipx install .
tsbench --version
```

For development: `pip install -e '.[dev]'` then `pytest` and `ruff check .`.

## Quick start

```sh
tsbench --dry-run              # see which nodes would be used; changes nothing
tsbench --screen-only          # latency screen of every UK node
tsbench --quick                # screen, then a short probe of the best few
tsbench status                 # what the history says is best
tsbench apply --if-better      # switch only if clearly worth it
tsbench --accept-ookla-license # full run: Ookla, 3 reps per surviving node
```

Every run connects through each node in turn, so **your traffic is rerouted
while it runs** and your original exit node is restored at the end. Don't run it
in the middle of something that can't tolerate a few reconnects.

## The three tiers

| Tier | Command | What it does |
|---|---|---|
| screen | `--screen-only` | Connects to each node, takes 5 TCP-handshake latency samples, measures no throughput. Reports latency, jitter and handshake loss. |
| quick | `--quick` | Screens, then runs **one** time-capped download probe on the `--top K` best-screened nodes (default 5) plus the `--favourites N` best remembered nodes (default 2), plus a direct baseline. No Ookla. With `--remembered N` it skips the screen and probes only the N best remembered nodes (about a minute): this is what scheduled refreshes use. |
| full | *(default)* | Screens first, drops clear latency outliers, then runs the Ookla test `--reps` times (default 3) on the survivors plus a direct baseline. `--max-nodes N` keeps the best N *by screen*. `--no-screen` tests every node as before. |

The screen is a **filter, not a ranking**. Latency only loosely predicts
throughput (see [Why a top group](#why-a-top-group-not-a-winner)), so it removes
the nodes that are clearly bad and decides which few deserve a throughput probe.
A node is dropped only if its latency exceeds twice the median of the screened
nodes plus 10 ms, so a city that is a few milliseconds further away (Glasgow,
say) is not mistaken for a bad node.

By default **every UK city is included**. Use `--cities` / `--exclude-cities` to
narrow it, and `--country` for other countries.

### What the quick probe measures

- **Latency**: median TCP handshake time to `--probe-host` (default `1.1.1.1`
  port 443), through the active exit node. **Jitter** is the mean change between
  consecutive handshakes, and **loss** is the share that failed.
- **Throughput**: a single-stream HTTPS download, capped at `--probe-seconds`
  (default 6). It starts timing at the first byte and discards the first second
  so TCP slow start doesn't dominate. The default endpoint is Cloudflare's
  speed-test download (`--probe-url` to change it). That endpoint refuses large
  single requests, so the probe repeats a 25 MB request over one connection
  until the time cap is reached.
- Upload is not measured.

Single-stream numbers read **lower than Ookla's multi-stream numbers**. Compare
probe results with other probe results, never with a full run's Mbps.

Each probe moves a few hundred MB on a fast line, so don't run it on a metered
connection.

## History, `status` and `apply`

Every run is saved to a SQLite database (`~/.local/state/tsbench/tsbench.db`,
or `$TSBENCH_HOME`). The screen and the throughput phase of a run are saved as
separate records. `--no-save` skips saving, `--db PATH` uses another file, and
`tsbench history list|prune --older-than DAYS` inspects and trims it. The
database is created readable by your user only.

### Standing

Scores inside one run are normalised to that run's best node, so they can't be
compared across runs. tsbench keeps the raw measurements and derives a
**standing** per node, 0 to 100:

- download is compared with the **same run's direct baseline**, latency as the
  *added* latency over that baseline, jitter against a fixed reference, with the
  run score's weights renormalised over the terms that were measured;
- observations are weighted by recency (24 h half-life);
- the estimate is the weighted mean, scaled down by the node's failure rate and
  reduced by an uncertainty penalty that shrinks as evidence accumulates.

So a node that was good once does not outrank one that has been consistently
good, and a node that keeps failing sinks. Runs recorded on a different network
(recorded from your direct connection's provider name) are ignored, so moving
from home to a cafe doesn't poison the estimates.

### `tsbench status`

Example output:

```
Remembered standing  last 14 days, network: <your ISP>
  est = recency-weighted standing, discounted for failures and uncertainty (0-100)

  #  Exit node          City         Est  Mean  Obs  Fail    Lat  Last seen
  1  gb-lon-wg-004      London      71.2  78.4    6    0%     12  2.1 h ago
  ...
Top group (within 10% of the best, a statistical tie): gb-lon-wg-004, gb-mnc-wg-002
Current exit node: gb-lon-wg-004 (in the top group)
```

`--json` gives the same data for scripts. It exits with code 4 if there is no
usable history.

### `tsbench apply`

Picks from memory and switches. It does no benchmarking.

| Command | Behaviour |
|---|---|
| `tsbench apply` | Switch to the best remembered node (a no-op if already on it). |
| `tsbench apply --if-better` | The cautious, automation-friendly mode below. |
| `--dry-run` | Print the decision; change nothing. |
| `--json` | Machine-readable decision. |
| `--force` | With `--if-better`: ignore the cooldown and the manual-choice guard. |

`--if-better` switches only when **one** of these holds:

1. The current node is **unhealthy**: a live latency probe fails, or latency is
   more than 1.5x (and at least 10 ms above) its own recent median. This
   bypasses the cooldown, so you are never stuck on a broken node.
2. The best remembered node's standing beats the current node's by at least
   `--margin` (default 10%) **and** the last switch was more than
   `--cooldown-hours` ago (default 2).

It also **leaves alone** any exit node it does not manage: direct connectivity
(you turned the exit node off on purpose), or a node tsbench has neither seen in
its history nor applied itself.

Exit codes: `0` success (including "stayed"), `1` error, `4` no history.

`tsbench --quick --apply` runs a quick refresh and then makes the same
`--if-better` decision.

### Why a top group, not a winner

Across five full runs of 14 London nodes, per-node rank correlation between
pairs of runs ranged from **-0.35 to 0.65**, and the top three overlapped by at
most one node in 9 of 10 pairs. Two runs 11 minutes apart shared **no** top-3
node. Healthy nodes are close to a statistical tie, and switching exit node
drops live connections, so changing node for a 3% edge costs more than it
gains. What *was* consistent: a few nodes were reliably bad (visible in latency),
and a few were occasionally broken. That is why tsbench screens out bad nodes,
reports a near-tie group, and applies hysteresis.

Absolute throughput also moved a lot between runs (the direct line measured
63-66 Mbps in two early runs and 735-851 Mbps later), and the Ookla server
picked changed every run, so only same-run comparisons are meaningful.

## Running it automatically

Three ways to trigger it without typing, all built on one command.

### `tsbench auto`

One unattended step, meant for a scheduler. Each call does exactly one thing:

| Situation | What it does |
|---|---|
| The last **full** run is older than `--full-every-hours` (default 18), or there is none | A full run (screen, then Ookla on the survivors), then `apply --if-better` |
| Otherwise | A **refresh**: probe only the `--refresh-nodes` (default 3) best remembered nodes plus a baseline, then `apply --if-better` |
| A guard says the machine is busy | Nothing (exit code 10) |
| Another tsbench run is active | Nothing (exit code 11) |

A full run needs Ookla's licence to have been accepted by you
(`--accept-ookla-license`). Without that, the periodic "deep" run is a wide
quick run with the built-in probe instead. tsbench never accepts it for you.

`apply --if-better` is the same rule as before: switch only if the current node
is unhealthy or clearly worse, never inside the cooldown, never over a node you
chose yourself. `tsbench auto --dry-run` prints the guard verdict and the plan
without doing anything. Output goes to `~/.local/state/tsbench/auto.log`
(rotated at 1 MB, one previous file kept).

**Guards** (`--no-guard` disables all of them):

- **Battery**: skip on battery power (`--allow-battery` to permit it).
- **Activity**: skip unless the keyboard and mouse have been idle for
  `--min-idle` seconds (default 60, macOS only; `0` disables). A benchmark
  reroutes your traffic, so it waits until you are away.
- **Calls**: skip while a process named in `--call-app` is running (repeatable).
  The default list is `CptHost` (Zoom's in-meeting helper) and `FaceTime`. This
  is a **heuristic**: apps such as Teams or Zoom's main window run all day
  without a call in progress, so matching them would disable scheduled runs
  for most people. Add your own with `--call-app`.

Where a platform cannot answer (idle time off macOS), the guard passes.

**One run at a time.** Anything that switches exit node takes a lock
(`run.lock` in the state directory). A manual `tsbench`, `tsbench apply` or
`auto` that finds it held says who has it and exits with code 11 instead of
fighting over the exit node. `--dry-run` does not need it. The lock is released
by the kernel if a run is killed, so there is nothing stale to clean up.

Exit codes: `0` ok, `1` error, `10` skipped by a guard, `11` another run active.

### macOS: `tsbench schedule`

```sh
tsbench schedule install            # every 2 hours; add --accept-ookla-license for full runs
tsbench schedule status             # installed? loaded? what did it last do?
tsbench schedule uninstall
```

This writes a launchd **user agent** (`~/Library/LaunchAgents/io.github.spuddy10345.tsbench.plist`)
and loads it into your login session. No root. It never runs at login, only on
its interval. launchd starts jobs with a minimal `PATH`, so `install` records the
absolute paths of the Python that runs tsbench and of `tailscale` / `speedtest`.
Re-run `install` after moving or upgrading the Python environment tsbench lives
in. `tsbench schedule install --dry-run` prints the agent without writing it.

### Raycast

`integrations/raycast/` has three [Script Commands](https://github.com/raycast/script-commands).
In Raycast: *Settings > Extensions > Script Commands > Add Directories*, and
choose that folder.

| Command | Mode | Does |
|---|---|---|
| tsbench: apply best exit node | silent | `tsbench apply`: switch to the best remembered node now |
| tsbench: quick re-test | silent | starts `tsbench --quick --apply` in the background and returns at once |
| tsbench: show best exit nodes | compact | `tsbench status --oneline`: top group, current node, age of the last full run |

Raycast starts scripts with a minimal `PATH`; each script sets its own and finds
`tsbench` on it. If yours lives elsewhere set `TSBENCH_BIN` to its full path.

### Linux

There is no installer. `integrations/linux/` has a systemd user timer
(`tsbench-auto.service` and `.timer`) and a `crontab.example`. Battery and call
guards work; the idle guard has no Linux source and passes. To switch exit node
without sudo your user must be the Tailscale operator
(`sudo tailscale set --operator=$USER`). The Linux examples have **not** been run
on a real Linux machine; CI exercises the test suite there.

### Data use

The quick probe is time-capped, not size-capped, so the data it moves grows with
your line speed. As arithmetic (line speed x seconds, not a measurement): on an
800 Mbps line a 4 second probe is about 400 MB, and a refresh of three nodes plus
a baseline is four probes, so **roughly 1.5 GB per step**; at 100 Mbps it is
about a tenth of that. A full run adds Ookla's own transfers. That is why the
installer defaults to every 2 hours and a refresh probe is 4 seconds. Lower it
with `--probe-seconds`, `--refresh-nodes` or `--interval-hours`, and **do not
schedule this on a metered connection**.

## Full-run options

| Option | Default | Meaning |
|---|---|---|
| `--cities LIST` | all | cities to include |
| `--exclude-cities LIST` | none | cities to exclude (wins over include) |
| `--country NAME` | `uk` | country filter |
| `--max-nodes N` | all | cap the number of nodes (by screen, or alphabetical with `--no-screen`) |
| `--include-offline` | off | also test nodes Tailscale reports offline |
| `--reps N` | `3` (full), `1` (quick) | measurements per node |
| `--tester` | `ookla` | `ookla` or `librespeed` |
| `--server-id ID` | auto | pin every test to one speed-test server |
| `--no-pin-server` | pinning on | let each test pick its own server |
| `--baseline` / `--no-baseline` | on | measure direct connectivity for reference |
| `--switch-delay SEC` | `3` (`1` for short tiers) | settle time between nodes |
| `--rep-delay SEC` | `1` | pause between reps |
| `--ready-timeout SEC` | `30` | how long to wait for a node to become usable |
| `--test-timeout SEC` | `180` | per-speed-test timeout |
| `--no-egress-check` | off | skip `am.i.mullvad.net` egress verification |
| `--dry-run` | off | discover and display only |
| `--fail-fast` | off | stop at the first unusable node instead of recording it failed |
| `--apply-best` | off | end connected to this run's top-ranked node |
| `--apply` | off | after the run, `apply --if-better` from the history |
| `--keep-raw` | off | save each raw speed-test JSON (needs `--out-dir`) |
| `--save` / `--no-save` | save | record the run in the history database |
| `--out-dir DIR` | none | also export JSON/CSV/Markdown under `DIR/<run-name>/` |
| `--log-file PATH` | none | append a plain-text log |
| `--colour` / `--no-colour` | auto | colour is off automatically when redirected |

Environment: `TSBENCH_HOME` (state directory), `XDG_STATE_HOME`,
`TSBENCH_TAILSCALE` (path to the binary), `TSBENCH_ACCEPT_OOKLA_LICENSE`.

### Why server pinning matters

Ookla picks the "nearest" server from your **egress** IP, so different exit
nodes can be measured against *different* servers and the numbers become
incomparable. By default tsbench locks onto the server chosen by the first
successful test (`--server-id` to choose, `--no-pin-server` to disable). The
quick probe always uses one fixed endpoint, so it has no such confound.

### Why there is no parallel mode

Every test shares this machine's single uplink. Running nodes concurrently
would measure contention between the tests, not the quality of the nodes.

## Scoring (within one run)

Per node, across its **successful** measurements, medians of download, upload,
latency, jitter and packet loss. Medians, not means, so one stalled transfer
can't dominate.

```
raw   = 0.40*D + 0.20*U + 0.30*L + 0.10*J

  D = median_download / best_median_download      (higher is better)
  U = median_upload   / best_median_upload        (higher is better)
  L = best_median_latency / median_latency        (lower is better)
  J = best_median_jitter  / median_jitter         (lower is better)

score = 100 * raw * reliability * loss_factor

  reliability = successful_runs / attempted_runs
  loss_factor = max(0, 1 - median_packet_loss_pct / 10)
```

Each term is normalised against the best node in the run, so the score is
**relative to the field you tested**. When no node measured upload (the quick
tier), that term is dropped and the others rescaled, so scores still span 0-100.

- Download is the largest weight (0.40), but latency carries 0.30: for
  interactive use a low-latency 80 Mbps node beats a jittery 95 Mbps one.
- Failures are penalised, not hidden. Packet loss is punished hard: 1% loss
  costs 10%, and 10% or more scores zero.
- A node needs at least `max(2, ceil(reps/2))` successful runs, otherwise it is
  marked *inconclusive* and left unranked rather than given a misleading place.
- The direct baseline is excluded from ranking and from normalisation.

## Output

By default a run prints its report and saves to the history database. With
`--out-dir`, it also writes `DIR/run-YYYYmmdd-HHMMSS/`:

| File | Contents |
|---|---|
| `results.json` | metadata, scoring definition, per-node aggregates, every run |
| `results.csv` | one row per node, the ranked summary |
| `runs.csv` | one row per individual measurement |
| `summary.md` | human-readable report |
| `raw/` | raw speed-test JSON (only with `--keep-raw`) |

## Safety and state handling

- The original exit node is recorded **before** anything changes, and restored
  on normal completion, on error, and on `SIGINT`/`SIGTERM` (idempotent, from a
  `finally` block). Exit code 130 when interrupted.
- If there was no exit node to begin with, you are returned to direct
  connectivity.
- Only `--exit-node` is touched. Other preferences, including
  `--exit-node-allow-lan-access`, are left as they were.
- Dependency and discovery failures abort **before** any state change.
- `tsbench apply` and `--dry-run` never run a benchmark. `--dry-run` never
  changes anything.

## Privacy and security

- **No telemetry, no update checks, no accounts.** tsbench reads no credentials
  and writes none.
- **Third-party hosts contacted.** Egress verification calls
  `am.i.mullvad.net` (it reports the exit server you are leaving from, and so
  sees your current egress IP). The quick tiers contact `1.1.1.1:443` and
  Cloudflare's speed-test endpoint. Full runs contact whichever servers Ookla
  chooses. `--no-egress-check` skips the first; `--probe-host` / `--probe-url`
  change the others.
- **Cloudflare's endpoint.** Cloudflare publishes the speed-test download
  endpoint for its own tester and open-source library. I could not find a
  published policy covering third-party tools, so the default is a few seconds
  of download per probe and the URL is yours to change.
- **Local data is private-ish.** The history database holds your exit nodes'
  Tailscale IPs and hostnames and the name of your direct connection's
  provider. It is created owner-readable only. Exports under `--out-dir` also
  contain node DNS names (which include your tailnet name) and Tailscale IPs.
  **Don't commit or share them**; the repository's `.gitignore` excludes
  `tsbench-results/`, `results/`, `*.db` and `*.log`.
- Subprocesses run from argument lists, never through a shell. Names reported by
  other tailnet nodes are stripped of control characters before they are
  printed, and sanitised before being used in a file name.
- Found a security problem? See [SECURITY.md](SECURITY.md).

## Performance

Measured on the original script (15 targets x 3 Ookla tests, all sequential):
**21-23 minutes** for a full run. Ookla tests take roughly 25 s each and must
run one at a time.

The short tiers are designed to cost, per node, one connection switch plus
about 1 s (screen) or about 7 s (quick probe), which suggests roughly **2
minutes for a screen and 3-4 for a quick run on ~22 nodes**. That is an
estimate from the design, **not yet measured end to end**; this section will be
replaced with measured numbers and a comparison of quick against full rankings.
A scheduled refresh (`--remembered 3`, no screen) is three probes plus a baseline,
about a minute by the same arithmetic.

## Verified behaviour

Checked live against **tailscale 1.102.4** and **Speedtest by Ookla 1.2.0.84**
on macOS on 2026-09-15. Node discovery via `status --json` was re-checked on
tailscale 1.104.1 with a dry run (22 UK nodes found):

- `tailscale exit-node list --filter=uk` prints a **padded text table** with
  **no `--json` flag**, and emits a duplicate row per country with
  `CITY="Any"`. tsbench therefore uses `tailscale status --json` as its primary
  source (structured `.Location`, no duplicates) and keeps the table as a
  fallback parser (by header column offsets, since cities such as `Denver, CO`
  and `Hong Kong` contain spaces).
- `tailscale set --exit-node=<short hostname>` is **rejected**. The Tailscale IP
  and the full DNS name work; tsbench always passes the IP.
- `tailscale set --exit-node=` (empty) clears the exit node.
- `.ExitNodeStatus.TailscaleIPs` entries carry a CIDR suffix
  (`100.64.0.1/32`).
- Switching is fast: Tailscale reported the new node in about 0.25 s and egress
  followed within about 1 s. tsbench still polls rather than assuming.
- Ookla's JSON uses **bytes/s** for bandwidth, so **Mbps = bandwidth x 8 / 1e6**.
  `.packetLoss` is a percentage and may be absent.
- `librespeed-cli --json` returns a one-element array, in Mbps, with no
  packet-loss field, and its default server choice is not distance-optimal; pin
  one.
- The Cloudflare download endpoint served 25 MB requests and returned `403` for
  100 MB and larger (checked October 2026), hence the repeated 25 MB requests.

**Not verified:** Tailscale before 1.80; Windows; non-Mullvad exit nodes (egress
verification can only name Mullvad servers; others are benchmarked correctly
and labelled `unverified egress`). Linux is intended to work (no
macOS-specific calls so far) but has not been exercised.

## Troubleshooting

**`Tailscale backend state is 'Stopped'`**: run `tailscale up`.

**`access denied` on `tailscale set`**: run `sudo tailscale set --operator=$USER`
once.

**`no exit nodes matched cities [...]`**: tsbench prints the cities actually
available and the raw `exit-node list` output.

**Mullvad exit nodes missing entirely**: enable the Mullvad add-on for your
tailnet and make sure this device may use exit nodes (ACLs).

**`egress is gb-lon-wg-00X, expected gb-lon-wg-00Y`**: usually transient during
a switch; tsbench retries until `--ready-timeout`. A persistent mismatch
suggests stale routing: `tailscale down && tailscale up`.

**`tsbench apply` says "no remembered data"**: run `tsbench --quick` first.

**Every node looks the same**: your own line is the bottleneck. Check the
baseline row; if the best node is near 100% of baseline download, only latency
and jitter differentiate.

## Limitations

- Results describe *your* line, at *this* time, to *one* server. They are a
  snapshot, not a verdict on the nodes.
- Sequential by necessity, so conditions can drift during a run.
- Single-stream probe numbers are not Ookla numbers.
- Medians of three are robust to one outlier, not statistically tight.
- Packet loss comes from the test tool (or handshake failures in the probe), not
  from an independent measurement.

## Roadmap and known gaps

- **Validation run.** Run `--quick` and a full run back to back several times on
  a real tailnet to measure real timings, check that the screen drops genuinely
  bad nodes, and tune the thresholds (10% margin, 2 h cooldown, 18 h full-run
  interval). Until then those numbers are starting values.
- **Mid-run activity.** The idle guard is checked when a scheduled step starts,
  not during it; a full run can take 10-15 minutes.
- **Windows** is not supported.

## Contributing, security and licence

- Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) and the
  [code of conduct](CODE_OF_CONDUCT.md). `main` is protected, so changes land
  through a pull request that passes CI.
- Report security problems privately, as described in
  [SECURITY.md](SECURITY.md).
- Changes are listed in [CHANGELOG.md](CHANGELOG.md).
- Released under the [MIT licence](LICENSE).
