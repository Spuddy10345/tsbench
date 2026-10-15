# Contributing

Thanks for taking an interest. tsbench is a small, standard-library-only tool,
and I want to keep it that way: **no runtime dependencies**.

## Before you start

- For anything bigger than a bug fix, open an issue first so we can agree on the
  approach before you spend time on it.
- Security problems go through private vulnerability reporting, not a public
  issue. See [SECURITY.md](SECURITY.md).

## Development setup

```sh
git clone https://github.com/Spuddy10345/tsbench.git && cd tsbench
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest            # the whole suite, no Tailscale or network needed
ruff check .
```

The suite uses fakes for Tailscale and the network, so it never changes your
exit node and runs in a few seconds. Python 3.9 is the floor; CI runs 3.9 to
3.14 on Linux and macOS.

## Pull requests

- Keep each pull request to one concern, with imperative commit subjects
  ("Add X", "Fix Y").
- Add or update tests for behaviour changes. Decision logic (`apply`,
  `standing`, `screen`) is covered by tests that fail if the rule is removed;
  please keep it that way.
- Run `pytest` and `ruff check .` before pushing. Both must pass in CI, and the
  `ci` check is required to merge.
- `main` is protected: changes land through a pull request.
- Update `CHANGELOG.md` under *Unreleased* for anything a user would notice.

## Privacy rules for contributions

Benchmark output contains tailnet names, Tailscale IPs and your public IP.
Never paste it into an issue, a pull request or a test fixture. Fixtures use
synthetic `100.64.x.x` addresses and made-up host names; please do the same.
`tsbench-results/` and `*.db` are gitignored for this reason.

## Code style

Match the surrounding code. `ruff` enforces formatting-adjacent rules and import
order; there is no other style gate. Comments explain *why*, and behaviours that
were checked against a real tool say which version they were checked against.
