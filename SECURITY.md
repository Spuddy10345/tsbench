# Security policy

## Reporting a vulnerability

Please report security problems privately, using GitHub's **Report a
vulnerability** button on the repository's *Security* tab (private vulnerability
reporting), rather than a public issue. Include what you ran, what you expected,
and what happened. Expect an acknowledgement within a few days.

## What is in scope

tsbench runs locally with your own privileges, changes your Tailscale exit node,
runs `tailscale` and (optionally) `speedtest` as subprocesses, makes a few
outbound HTTPS/TCP requests, and stores results in a local SQLite database.
Examples of issues worth reporting:

- command or argument injection through data that comes from the tailnet
  (peer names, locations) or from speed-test output;
- writing files outside the intended directories;
- leaking private data (tailnet name, Tailscale IPs, public IP) into exports,
  logs or the repository;
- ways to leave the machine on an unintended exit node after a run.

## What tsbench does for safety

- Subprocesses run from argument lists; no shell is involved.
- Names reported by other tailnet nodes have control characters stripped before
  they are printed and are sanitised before use in file names.
- The history database and its directory are created owner-only (`0600`/`0700`).
- No telemetry, no update checks, and no credentials are read or stored.
- Ookla's licence is never accepted on your behalf.

See the *Privacy and security* section of the README for the full list of
hosts tsbench contacts.
