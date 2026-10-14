# tsbench

Benchmark your Tailscale (Mullvad) exit nodes and get a ranked recommendation of
the best few — then optionally stay connected to the winner.

Defaults to **London**, excludes **Glasgow** and **Manchester**, measures a
**no-exit-node baseline** for reference, and **always restores your original
exit-node state** — including on Ctrl-C.

## Requirements

| Thing | Why | Install |
|---|---|---|
| Python 3.9+ | the script (standard library only, no pip packages) | preinstalled on macOS 13+ |
| `tailscale` CLI, logged in | discovery + switching | `brew install tailscale`, or App Store app |
| `speedtest` (Ookla) | the speed test | `brew tap teamookla/speedtest && brew install speedtest` |

Optional alternative tester: `librespeed-cli` (`brew install librespeed-cli`).

**No `sudo` needed** as long as your user is the Tailscale operator. Check with
`tailscale debug prefs | grep OperatorUser`; if it is not you, run once:

```sh
sudo tailscale set --operator=$USER
```

`speedtest` prompts for a licence on first run; tsbench passes
`--accept-license --accept-gdpr`, but running `speedtest` once by hand first is
harmless.

> ⚠️ The Ookla `speedtest` binary is **not** the same as the `speedtest-cli`
> Python package or Debian's `speedtest-cli`. They emit different JSON. tsbench
> refuses to substitute one for the other; it will tell you what to install.

## Install

```sh
chmod +x tsbench.py
./tsbench.py --help
```

## Usage examples

```sh
# Default: every London node, 3 reps each, plus a DIRECT baseline
./tsbench.py

# Discovery only — see what would be tested, change nothing
./tsbench.py --dry-run

# Custom city selection
./tsbench.py --cities London,Edinburgh
./tsbench.py --cities Manchester --exclude-cities ''      # override the defaults

# Limited run (quick sanity check)
./tsbench.py --max-nodes 5

# More repetitions for tighter medians, slower switching
./tsbench.py --reps 5 --switch-delay 5

# Finish connected to the best node instead of restoring the old one
./tsbench.py --apply-best

# Alternative tester, no baseline, keep raw JSON, log to a file
./tsbench.py --tester librespeed --no-baseline --keep-raw --log-file ~/tsbench.log

# Other countries work too (the city filter is generic)
./tsbench.py --country sweden --cities Stockholm --exclude-cities ''
```

A full default run is **14 London nodes × 3 reps ≈ 25–30 minutes**, because each
Ookla test takes ~25 s and they must run one at a time.
Start with `--max-nodes 4 --reps 2` if you want a result sooner.

## Key options

| Option | Default | Meaning |
|---|---|---|
| `--cities LIST` | `London` | cities to include |
| `--exclude-cities LIST` | `Glasgow,Manchester` | cities to exclude (wins over include) |
| `--country NAME` | `uk` | country filter |
| `--reps N` | `3` | speed tests per node |
| `--max-nodes N` | all | cap the number of nodes |
| `--tester` | `ookla` | `ookla` or `librespeed` |
| `--server-id ID` | auto | pin every test to one speed-test server |
| `--no-pin-server` | pinning on | let each test pick its own server |
| `--baseline` / `--no-baseline` | on | measure direct connectivity for reference |
| `--switch-delay SEC` | `3` | settle time between nodes |
| `--rep-delay SEC` | `1` | pause between reps |
| `--ready-timeout SEC` | `30` | how long to wait for a node to become usable |
| `--test-timeout SEC` | `180` | per-speed-test timeout |
| `--no-egress-check` | off | skip `am.i.mullvad.net` egress verification |
| `--dry-run` | off | discover and display only |
| `--fail-fast` | off | stop at the first unusable node instead of recording it failed |
| `--apply-best` | off | end connected to the winner |
| `--keep-raw` | off | save each raw speed-test JSON |
| `--out-dir DIR` | `./tsbench-results` | parent of the timestamped run directory |
| `--force` | off | allow overwriting an existing run directory |
| `--log-file PATH` | none | append a plain-text log |
| `--colour` / `--no-colour` | auto | colour is off automatically when redirected |
| `--parallel` | refused | see below |

### Why server pinning matters

Ookla picks the "nearest" server based on your **egress** IP. Different exit
nodes can therefore be measured against *different* servers, which makes the
numbers incomparable. By default tsbench locks onto the server chosen by the
first successful test and reuses it (`--server-id` to choose explicitly,
`--no-pin-server` to disable). `speedtest --servers` lists candidates.

### Why there is no parallel mode

Every test shares this machine's single uplink. Running nodes concurrently would
measure contention between the tests, not the quality of the nodes. `--parallel`
exits with an explanatory error rather than pretending to work.

## Scoring methodology

Per node, across its **successful** runs:

- median download (Mbps), median upload (Mbps)
- median latency (ms), median jitter (ms), median packet loss (%)

Medians — not means — so one stalled transfer cannot dominate a node's figures.

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
**relative to the field you tested**, not an absolute rating. Range 0–100.

Deliberate choices:

- **Not download-only.** Download is the largest single weight (0.40) but
  latency carries 0.30, because for interactive use a low-latency 80 Mbps node
  beats a jittery 95 Mbps one.
- **Failures are penalised, not hidden.** `reliability` scales the whole score,
  so 2 good runs out of 3 costs a third of the score.
- **Packet loss is punished hard.** 1 % loss ⇒ −10 %; 10 % or more ⇒ score 0.
- **Missing metrics score 0 for their term** rather than being quietly skipped.
  `librespeed` reports no packet loss, so `loss_factor` is 1 there — say so when
  comparing runs across testers.
- **Inconclusive ≠ bad.** A node needs at least `max(2, ceil(reps/2))`
  successful runs; otherwise it is marked *inconclusive* and left **unranked**
  rather than given a misleading position.
- **The baseline is excluded** from ranking *and* from score normalisation. It
  is a reference point (what your line does with no exit node), not a candidate.

## Output

Each run writes a timestamped directory (`run-YYYYmmdd-HHMMSS`) under
`--out-dir`, so previous runs are never overwritten (`--run-name` + `--force` to
override):

| File | Contents |
|---|---|
| `results.json` | everything: metadata, scoring definition, per-node aggregates, every run |
| `results.csv` | one row per node — the ranked summary |
| `runs.csv` | one row per individual speed test |
| `summary.md` | human-readable report: ranked table, recommendation, method |
| `raw/` | raw speed-test JSON (only with `--keep-raw`) |

On screen you get live progress (current node, `[n/total]`, connection status,
test status, each result as it lands), then the ranked table, the baseline
comparison, and up to three recommendations with the exact command to connect.

## Safety and state handling

- The original exit node is recorded **before** anything changes.
- It is restored on normal completion, on error, and on `SIGINT`/`SIGTERM`
  (restore is idempotent and runs from a `finally` block). Exit code is 130 when
  interrupted.
- If there was no exit node to begin with, tsbench returns you to direct
  connectivity (`tailscale set --exit-node=`).
- The final line always states the exit node you are left on.
- Only `--exit-node` is ever touched. Other preferences — including
  `--exit-node-allow-lan-access` — are left exactly as you had them.
- Dependency and discovery failures abort **before** any state change.
- No credentials, API keys, or secrets are read or written.

## Verified behaviour

Checked live against **tailscale 1.102.4** and **Speedtest by Ookla 1.2.0.84**
on macOS (Darwin 25.6.0) on 2026-09-15:

- `tailscale exit-node list --filter=uk` prints a **padded text table**
  (IP / HOSTNAME / COUNTRY / CITY / STATUS) with **no `--json` flag**, and emits
  a **duplicate row per country with `CITY="Any"`**. tsbench therefore uses
  `tailscale status --json` as its primary source — it carries a structured
  `.Location` object (`City`, `CityCode`, `Country`, `CountryCode`) and no
  duplicate rows — and keeps the table for display and as a fallback parser
  (parsed by header column offsets, since cities like `Denver, CO` and
  `Hong Kong` contain spaces).
- `tailscale set --exit-node=<short hostname>` is **rejected**:
  `invalid value "gb-lon-wg-004" for --exit-node; must be IP or peer hostname`.
  The **Tailscale IP** and the **full DNS name** (`gb-lon-wg-004.mullvad.ts.net`)
  both work. tsbench always passes the IP.
- `tailscale set --exit-node=` (empty) clears the exit node; `.ExitNodeStatus`
  then becomes `null`.
- `.ExitNodeStatus.TailscaleIPs` entries carry a CIDR suffix (`100.64.0.1/32`).
- Switching is fast: Tailscale reported the new node in ~0.25 s and verified
  egress followed within ~1 s. tsbench still polls rather than assuming.
- Ookla machine-readable output uses **bytes/s** for
  `.download.bandwidth` / `.upload.bandwidth` (the CLI help states this
  explicitly), so **Mbps = bandwidth × 8 ÷ 1e6**. `.ping.latency`/`.ping.jitter`
  are ms; `.packetLoss` is a percentage and **may be absent**.
- `librespeed-cli --json` returns a **one-element array**; `download`/`upload`
  are already **Mbps**, `ping`/`jitter` are ms, and there is **no packet-loss
  field**. Its default server choice is not distance-optimal (it picked
  Amsterdam from a London egress) — pin a server with `--server`/`--server-id`.

**Not verified locally** (flagged rather than assumed):

- Behaviour on Tailscale < 1.80, where `exit-node list` may differ. tsbench does
  not depend on that table unless `status --json` lacks `Location` data, and the
  fallback parser aborts loudly if the header is not what it expects.
- Windows and Linux paths. The logic is OS-neutral and uses no shell, but only
  macOS was exercised.
- Non-Mullvad exit nodes. Egress *verification* uses `am.i.mullvad.net`, which
  can only name Mullvad servers. For other nodes tsbench falls back to a plain
  reachability probe and labels the node `unverified egress` — it still
  benchmarks correctly.

## Troubleshooting

**`Tailscale backend state is 'Stopped'`** — run `tailscale up`.

**`access denied` on `tailscale set`** — run `sudo tailscale set --operator=$USER`
once, or run tsbench under `sudo`.

**`no exit nodes matched cities [...]`** — tsbench prints the cities actually
available plus the raw `exit-node list` output. Remember `--exclude-cities`
defaults to `Glasgow,Manchester`, so `--cities Manchester` alone matches nothing;
use `--exclude-cities ''`.

**Mullvad exit nodes missing entirely** — enable the Mullvad add-on for your
tailnet and make sure this device is allowed to use exit nodes (ACLs).

**`egress is gb-lon-wg-00X, expected gb-lon-wg-00Y`** — usually a transient
during a switch; tsbench retries until `--ready-timeout`. Persistent mismatch
suggests a stale DNS/route state — `tailscale down && tailscale up`.

**Every node looks the same** — your own line is the bottleneck. Check the
baseline row: if the best node is at ~100 % of baseline download, download is
saturated and latency/jitter are the only meaningful differentiators. That is
precisely why they carry 40 % of the score combined.

**Results vary between runs** — consumer broadband is noisy. Raise `--reps`,
pin a server, and avoid benchmarking while other devices are busy.

## Limitations

- Results describe *your* line, at *this* time of day, to *one* speed-test
  server. They are a snapshot, not a verdict on the nodes.
- Sequential by necessity, so a full run is slow and network conditions can
  drift across it. Interleaving repetitions across nodes would be more
  statistically robust but multiplies the number of switches; repeating the
  whole run and comparing is the practical alternative.
- Ookla measures throughput to a nearby server, not to whatever you actually
  use. For a specific workload, substitute a tester that exercises it.
- Medians of 3 are robust to one outlier, not statistically tight. Use
  `--reps 5+` if you intend to act on small differences.
- Packet loss comes from the speed-test tool and is not measured independently.
