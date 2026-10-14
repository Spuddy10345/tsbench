"""Console table and exported files (JSON / CSV / Markdown)."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict
from typing import Any, Dict, List

from tsbench import PROG, __version__
from tsbench.console import Term, fmt
from tsbench.models import NodeResult

TABLE_COLS = [
    ("Rank", 5, "r"), ("Exit node", 22, "l"), ("City", 12, "l"), ("OK", 5, "r"),
    ("Down", 9, "r"), ("Up", 9, "r"), ("Lat", 8, "r"), ("Jit", 7, "r"),
    ("Loss", 6, "r"), ("Score", 7, "r"), ("Status", 17, "l"),
]

SCORING_DOC = {
    "formula": ("score = 100 * (0.40*D + 0.20*U + 0.30*L + 0.10*J) "
                "* reliability * loss_factor"),
    "terms": {
        "D": "median_download / best_median_download (higher better)",
        "U": "median_upload / best_median_upload (higher better)",
        "L": "best_median_latency / median_latency (lower better)",
        "J": "best_median_jitter / median_jitter (lower better)",
        "reliability": "successful_runs / attempted_runs",
        "loss_factor": "max(0, 1 - median_packet_loss_pct / 10)",
    },
    "units": {"download": "Mbps", "upload": "Mbps", "latency": "ms",
              "jitter": "ms", "packet_loss": "percent", "score": "0-100"},
    "normalisation_pool": ("nodes with status 'ok'; the DIRECT baseline is "
                           "excluded (reference only)"),
}


def display_order(results: List[NodeResult]) -> List[NodeResult]:
    """Ranked nodes, then the baseline, then inconclusive, then failed."""
    return (sorted([r for r in results if r.status == "ok" and not r.node.is_baseline],
                   key=lambda r: r.rank or 999)
            + [r for r in results if r.node.is_baseline]
            + [r for r in results if r.status == "inconclusive"]
            + [r for r in results if r.status == "failed"])


def row_cells(r: NodeResult, attempted: int) -> List[str]:
    if r.node.is_baseline:
        rank, status = "--", "baseline (ref)"
    elif r.status == "ok":
        rank = str(r.rank or "-")
        status = "RECOMMENDED" if r.recommended else "tested"
    elif r.status == "inconclusive":
        rank, status = "--", "inconclusive"
    else:
        rank, status = "--", "failed"
    return [
        rank, r.node.label, (r.node.city or "-"),
        f"{r.successes}/{attempted}",
        fmt(r.med_down), fmt(r.med_up), fmt(r.med_lat), fmt(r.med_jit),
        "-" if r.loss_pct is None else f"{r.loss_pct:.1f}",
        "-" if r.score is None else f"{r.score:.1f}",
        status,
    ]


def render_table(results: List[NodeResult], attempted: int, term: Term) -> str:
    widths = []
    for i, (head, minw, _) in enumerate(TABLE_COLS):
        longest = max([len(head)] + [len(row_cells(r, attempted)[i]) for r in results])
        widths.append(max(minw, longest))

    def line(cells: List[str], colourise=None) -> str:
        out = []
        for (_head, _, align), w, cell in zip(TABLE_COLS, widths, cells):
            txt = cell.rjust(w) if align == "r" else cell.ljust(w)
            out.append(colourise(txt, cell) if colourise else txt)
        return "  ".join(out).rstrip()

    heads = [h for h, _, _ in TABLE_COLS]
    sep = "  ".join("-" * w for w in widths)
    body = [term.bold(line(heads)), term.dim(sep)]

    def paint(txt: str, cell: str) -> str:
        if cell == "RECOMMENDED":
            return term.green(txt)
        if cell == "failed":
            return term.red(txt)
        if cell in ("inconclusive", "baseline (ref)"):
            return term.yellow(txt)
        return txt

    for r in display_order(results):
        body.append(line(row_cells(r, attempted), paint))
    return "\n".join(body)


def write_outputs(run_dir: str, results: List[NodeResult], meta: Dict[str, Any],
                  recommended: List[NodeResult]) -> Dict[str, str]:
    """Export a run as JSON, CSV, per-run CSV and a Markdown summary."""
    os.makedirs(run_dir, exist_ok=True)
    attempted = int(meta["reps"])
    paths: Dict[str, str] = {}

    # JSON -------------------------------------------------------------------
    payload: Dict[str, Any] = {
        "tool": PROG, "version": __version__, "meta": meta,
        "scoring": SCORING_DOC, "results": [],
    }
    for r in results:
        payload["results"].append({
            "hostname": r.node.hostname, "label": r.node.label,
            "dns_name": r.node.dns_name, "ip": r.node.ip,
            "city": r.node.city, "city_code": r.node.city_code,
            "country": r.node.country, "is_baseline": r.node.is_baseline,
            "connected": r.connected, "egress_verified": r.verified,
            "connect_error": r.connect_error,
            "status": r.status, "rank": r.rank, "recommended": r.recommended,
            "successful_runs": r.successes, "failed_runs": r.failures,
            "attempted_runs": len(r.runs),
            "median_download_mbps": r.med_down, "median_upload_mbps": r.med_up,
            "median_latency_ms": r.med_lat, "median_jitter_ms": r.med_jit,
            "median_packet_loss_pct": r.loss_pct, "composite_score": r.score,
            "runs": [asdict(x) for x in r.runs],
        })
    p = os.path.join(run_dir, "results.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    paths["json"] = p

    # CSV --------------------------------------------------------------------
    p = os.path.join(run_dir, "results.csv")
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "hostname", "city", "ip", "is_baseline", "status",
                    "recommended", "successful_runs", "attempted_runs",
                    "median_download_mbps", "median_upload_mbps",
                    "median_latency_ms", "median_jitter_ms",
                    "median_packet_loss_pct", "composite_score",
                    "connect_error"])
        for r in results:
            w.writerow([r.rank or "", r.node.hostname, r.node.city, r.node.ip,
                        r.node.is_baseline, r.status, r.recommended,
                        r.successes, len(r.runs), r.med_down, r.med_up,
                        r.med_lat, r.med_jit, r.loss_pct, r.score,
                        r.connect_error or ""])
    paths["csv"] = p

    # per-run CSV ------------------------------------------------------------
    p = os.path.join(run_dir, "runs.csv")
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["hostname", "city", "rep", "ok", "started_at", "duration_s",
                    "download_mbps", "upload_mbps", "latency_ms", "jitter_ms",
                    "packet_loss_pct", "server", "server_id", "egress_host",
                    "egress_ip", "error"])
        for r in results:
            for x in r.runs:
                w.writerow([r.node.hostname or "direct", r.node.city, x.rep, x.ok,
                            x.started_at, x.duration_s, x.download_mbps,
                            x.upload_mbps, x.latency_ms, x.jitter_ms,
                            x.packet_loss_pct, x.server, x.server_id,
                            x.egress_host, x.egress_ip, x.error or ""])
    paths["runs_csv"] = p

    # Markdown ---------------------------------------------------------------
    md = [f"# {PROG} report", ""]
    md.append(f"- **Run started:** {meta['started_at']}")
    md.append(f"- **Duration:** {meta['duration_s']}s")
    md.append(f"- **Cities included:** {', '.join(meta['include_cities']) or 'all'}")
    md.append(f"- **Cities excluded:** {', '.join(meta['exclude_cities']) or '(none)'}")
    md.append(f"- **Speed test:** {meta['tester']} — {meta['units_note']}")
    md.append(f"- **Repetitions per node:** {meta['reps']}")
    md.append(f"- **Pinned server:** {meta.get('pinned_server') or 'auto (per test)'}")
    md.append(f"- **Nodes benchmarked:** {meta['nodes_benchmarked']}")
    md.append(f"- **Original exit node:** {meta['original_exit_node']}")
    md.append(f"- **Restored to:** {meta['restored_to']}")
    md += ["", "## Ranked results", "",
           "Download/upload in Mbps, latency/jitter in ms, loss in %, score 0-100.", ""]
    md.append("| " + " | ".join(h for h, _, _ in TABLE_COLS) + " |")
    md.append("|" + "|".join("---" for _ in TABLE_COLS) + "|")
    for r in display_order(results):
        md.append("| " + " | ".join(row_cells(r, attempted)) + " |")

    md += ["", "## Recommendation", ""]
    if recommended:
        base = next((r for r in results if r.node.is_baseline and r.status == "ok"), None)
        for i, r in enumerate(recommended, 1):
            extra = ""
            if base and base.med_down:
                extra = (f" — {100.0 * (r.med_down or 0) / base.med_down:.0f}% of the "
                         f"no-exit-node baseline download")
            md.append(f"{i}. **`{r.node.dns_name or r.node.hostname}`** "
                      f"(score {r.score:.1f}) — {fmt(r.med_down)} Mbps down, "
                      f"{fmt(r.med_up)} Mbps up, {fmt(r.med_lat)} ms latency{extra}")
        md += ["", "Connect to the top node with:", "",
               "```sh", f"tailscale set --exit-node={recommended[0].node.ip}  "
               f"# {recommended[0].node.hostname}", "```"]
    else:
        md.append("_No node produced enough valid data to be recommended._")

    incon = [r for r in results if r.status == "inconclusive"]
    failed = [r for r in results if r.status == "failed"]
    tested = [r for r in results if r.status == "ok" and not r.node.is_baseline
              and not r.recommended]
    if tested:
        md += ["", "## Tested but not recommended", ""]
        md += [f"- `{r.node.hostname}` — rank {r.rank}, score {r.score:.1f}" for r in tested]
    if incon:
        md += ["", "## Inconclusive (insufficient valid data)", ""]
        md += [f"- `{r.node.hostname}` — {r.successes}/{len(r.runs)} successful runs"
               for r in incon]
    if failed:
        md += ["", "## Failed", ""]
        md += [f"- `{r.node.hostname or 'DIRECT'}` — "
               f"{r.connect_error or 'all speed tests failed'}" for r in failed]

    md += ["", "## Scoring method", "", "```", SCORING_DOC["formula"], "```", ""]
    for k, v in SCORING_DOC["terms"].items():
        md.append(f"- `{k}` = {v}")
    md += ["",
           "Medians are used rather than means so a single stalled transfer cannot "
           "dominate a node's figures. Nodes with fewer than "
           f"{meta['min_success']} successful runs are marked *inconclusive* and are "
           "left unranked rather than being given a misleading position. The DIRECT "
           "baseline is measured for reference and is deliberately excluded from "
           "both ranking and score normalisation.", ""]
    p = os.path.join(run_dir, "summary.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    paths["markdown"] = p
    return paths
