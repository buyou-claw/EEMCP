"""plot_ble_report.py — Generate a visual HTML + PNG report from a BLE test folder.

Reads a ``reports/ble_full_<ts>/`` folder (``data.csv`` + ``summary.json``) and
emits, in the same folder:

* ``plot_<test>.png`` — one PNG per test (LEOP, LEICD, LEMI, LESS)
* ``dashboard.png``   — 2×2 combined view of all four
* ``report.html``     — self-contained, all PNGs embedded as base64, no external assets

The HTML can be opened in any browser, archived, or emailed as a single file.
Pass/fail bars are coloured (green / red / grey); Bluetooth-spec reference
lines are drawn on the LEICD / LEMI plots.

Usage::

    python -m mcp_8852b.scripts.plot_ble_report --report-dir reports/ble_full_20260618_143625
    python -m mcp_8852b.scripts.plot_ble_report --latest
    python -m mcp_8852b.scripts.plot_ble_report --report-dir <dir> --no-html
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# Bluetooth 5.x spec reference lines (illustrative; product spec wins).
SPEC_LIMITS: dict[str, dict] = {
    "LEICD": {"abs_max_drift_hz": 150_000, "label": "BLE spec: |drift| ≤ 150 kHz"},
    "LEMI":  {"f1avg_min_hz": 185_000,   "label": "BLE spec: f1avg ≥ 185 kHz"},
    "LEOP":  {"min_dbm": -20, "max_dbm": 20,
              "label": "Illustrative window: -20..+20 dBm (use product spec)"},
    "LESS":  {"max_per_pct": 30.8,
              "label": "BLE spec: PER ≤ 30.8 % @ 1500 pkts"},
}

COLOR_PASS = "#10b981"
COLOR_FAIL = "#ef4444"
COLOR_INVALID = "#6b7280"
COLOR_HEADER = "#1e40af"
COLOR_GRID = "#e5e7eb"
COLOR_SPEC = "#f59e0b"


def load_report(report_dir: Path) -> tuple[list[dict], dict]:
    data_csv = report_dir / "data.csv"
    summary_json = report_dir / "summary.json"
    if not data_csv.exists():
        raise FileNotFoundError(f"data.csv not found in {report_dir}")
    if not summary_json.exists():
        raise FileNotFoundError(f"summary.json not found in {report_dir}")
    with data_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with summary_json.open(encoding="utf-8") as f:
        summary = json.load(f)
    return rows, summary


def group_by_test(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        # LEMI: the 8852B puts its PASS/FAIL token in the n_packets CSV column
        # (it overloads the ORESULT layout), not in pass_fail. Recover it.
        if not r.get("pass_fail"):
            guess = r.get("n_packets", "")
            if guess in ("PASS", "FAIL"):
                r["pass_fail"] = guess
        groups.setdefault(r["test_id"], []).append(r)
    for rows_ in groups.values():
        rows_.sort(key=lambda r: int(r["channel"]))
    return groups


def _bar_color(pf: str) -> str:
    if pf == "PASS":
        return COLOR_PASS
    if pf == "FAIL":
        return COLOR_FAIL
    return COLOR_INVALID


def plot_single_test(test_id: str, rows: list[dict], ax) -> None:
    channels = [int(r["channel"]) for r in rows]
    values = [float(r["value_main"]) for r in rows]
    pass_fails = [r["pass_fail"] for r in rows]
    unit = rows[0]["unit"]
    test_name = rows[0]["test_name"]
    payload = rows[0]["payload"]

    colors = [_bar_color(pf) for pf in pass_fails]
    bars = ax.bar(channels, values, color=colors, edgecolor="white",
                  linewidth=1.5, alpha=0.9, width=6.0)

    span = (max(values) - min(values)) if max(values) != min(values) else max(abs(max(values)), 1.0) * 0.05
    for bar, val in zip(bars, values):
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2.0, h + span * 0.02,
                f"{val:.4g}", ha="center", va="bottom",
                fontsize=9, fontweight="bold", color="#111827")

    spec = SPEC_LIMITS.get(test_id, {})
    if test_id == "LEICD":
        ax.axhline(y=spec["abs_max_drift_hz"], color=COLOR_SPEC,
                   linestyle="--", linewidth=1.5, label=spec["label"])
        ax.axhline(y=-spec["abs_max_drift_hz"], color=COLOR_SPEC,
                   linestyle="--", linewidth=1.5)
    elif test_id == "LEMI":
        ax.axhline(y=spec["f1avg_min_hz"], color=COLOR_SPEC,
                   linestyle="--", linewidth=1.5, label=spec["label"])
    elif test_id == "LESS":
        ax.axhline(y=spec["max_per_pct"], color=COLOR_SPEC,
                   linestyle="--", linewidth=1.5, label=spec["label"])
    elif test_id == "LEOP":
        ax.axhline(y=spec["max_dbm"], color=COLOR_SPEC, linestyle=":",
                   linewidth=1.0, alpha=0.6, label=spec["label"])
        ax.axhline(y=spec["min_dbm"], color=COLOR_SPEC, linestyle=":",
                   linewidth=1.0, alpha=0.6)

    ax.set_xlabel("Channel", fontsize=10)
    ax.set_ylabel(unit, fontsize=10)
    ax.set_title(f"{test_id} — {test_name}\n(payload: {payload})",
                 fontsize=11, fontweight="bold", loc="left")
    ax.set_xticks(channels)
    ax.grid(axis="y", color=COLOR_GRID, linestyle="-", linewidth=0.5, alpha=0.7)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    if spec and test_id != "LEOP":
        ax.legend(loc="best", fontsize=8, framealpha=0.9)
    elif spec and test_id == "LEOP":
        ax.legend(loc="best", fontsize=8, framealpha=0.9)

    if values:
        vmin, vmax = min(values), max(values)
        span = (vmax - vmin) * 0.2 if vmax != vmin else abs(vmax) * 0.1 + 0.1
        if test_id == "LEICD":
            ax.set_ylim(bottom=min(0, vmin - span), top=vmax + span * 6)
        elif test_id == "LESS":
            ax.set_ylim(bottom=0, top=max(vmax * 1.3, spec["max_per_pct"] * 1.2))
        else:
            ax.set_ylim(bottom=vmin - span, top=vmax + span * 6)


def make_per_test_pngs(groups: dict[str, list[dict]], report_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for tid, rows in groups.items():
        if not rows:
            continue
        fig, ax = plt.subplots(figsize=(7.0, 4.2))
        plot_single_test(tid, rows, ax)
        fname = f"plot_{tid.lower()}.png"
        fig.savefig(report_dir / fname, dpi=110, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        out[tid] = fname
    return out


def make_dashboard_png(groups: dict[str, list[dict]], report_dir: Path) -> str:
    order = [t for t in ("LEOP", "LEICD", "LEMI", "LESS") if t in groups]
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), constrained_layout=True)
    for i, tid in enumerate(order):
        plot_single_test(tid, groups[tid], axes[i // 2][i % 2])
    for j in range(len(order), 4):
        axes[j // 2][j % 2].axis("off")
    fig.suptitle("BLE Test Dashboard", fontsize=14, fontweight="bold")
    fname = "dashboard.png"
    fig.savefig(report_dir / fname, dpi=110, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return fname


def make_html(summary: dict, groups: dict[str, list[dict]], report_dir: Path,
              dashboard_png: str) -> str:
    totals = summary.get("totals", {})
    n_pass = totals.get("pass", 0)
    n_fail = totals.get("fail", 0)
    n_inv = totals.get("invalid_or_unknown", 0)
    n_total = totals.get("tests_run", 0)

    started = summary.get("started", "")
    ended = summary.get("ended", "")
    duration = summary.get("duration_s", 0)
    instr = summary.get("instrument", "—")
    eut = summary.get("eut", {})
    plan = summary.get("plan", {})

    verdict_class = "pass" if (n_fail == 0 and n_inv == 0 and n_total > 0) else "fail"
    verdict_text = "ALL PASS" if verdict_class == "pass" else "HAS FAILURES"

    with (report_dir / dashboard_png).open("rb") as f:
        dashboard_b64 = base64.b64encode(f.read()).decode("ascii")

    table_rows = []
    for tid, rows in groups.items():
        for r in rows:
            pf = r["pass_fail"] or ""
            pf_class = pf.lower() if pf else "na"
            table_rows.append(
                f'<tr><td>{tid}</td><td>{r["test_name"]}</td>'
                f'<td style="text-align:right">{r["channel"]}</td>'
                f'<td>{r["payload"]}</td>'
                f'<td style="text-align:right">{r["value_main"]} {r["unit"]}</td>'
                f'<td class="pf-{pf_class}">{pf or "—"}</td>'
                f'<td style="text-align:right">{r["elapsed_s"]}s</td></tr>'
            )
    table_html = "\n".join(table_rows)

    plan_tests = ", ".join(plan.get("tests", []))
    plan_chs = ",".join(str(c) for c in plan.get("channels", []))
    plan_text = f"{plan_tests} × ch{plan_chs} @ {plan.get('capture_type', '—')} PHY, pkt_len={plan.get('packet_length', '—')}"

    eut_text = (f'{eut.get("interface", "?")} @ {eut.get("baud", "?")} baud, '
                f'handshake={eut.get("handshake", "?")}')

    verdict_tail = ""
    if n_fail:
        verdict_tail += f" · FAIL: {n_fail}"
    if n_inv:
        verdict_tail += f" · INVALID: {n_inv}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BLE Test Report — {summary.get('run_label', '?')}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #f3f4f6; color: #111827;
    line-height: 1.4;
  }}
  .wrap {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
  .verdict {{
    padding: 18px 24px; border-radius: 10px; color: white;
    font-size: 16px; font-weight: 600; margin-bottom: 18px;
    display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
  }}
  .verdict.pass {{ background: linear-gradient(90deg, #10b981, #059669); }}
  .verdict.fail {{ background: linear-gradient(90deg, #ef4444, #dc2626); }}
  .verdict .num {{ font-size: 26px; }}
  .meta {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px; margin-bottom: 18px;
  }}
  .card {{
    background: white; border-radius: 8px; padding: 12px 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.05);
  }}
  .card .label {{
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
    color: #6b7280; margin-bottom: 4px;
  }}
  .card .value {{ font-size: 14px; font-weight: 600; color: #111827;
                  word-break: break-all; }}
  .card .value.mono {{ font-family: "SF Mono", Consolas, "Liberation Mono", monospace; font-size: 12px; }}
  .section {{
    background: white; border-radius: 10px; padding: 18px 22px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 18px;
  }}
  .section h2 {{
    margin-top: 0; color: {COLOR_HEADER}; font-size: 15px;
    border-bottom: 1px solid #e5e7eb; padding-bottom: 8px;
  }}
  .dashboard {{ text-align: center; }}
  .dashboard img {{ max-width: 100%; height: auto; border-radius: 6px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid #e5e7eb; }}
  th {{
    background: #f9fafb; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.06em; color: #6b7280;
  }}
  td {{ font-family: "SF Mono", Consolas, "Liberation Mono", monospace; font-size: 12px; }}
  .pf-pass {{ color: #059669; font-weight: 600; }}
  .pf-fail {{ color: #dc2626; font-weight: 600; }}
  .pf-na {{ color: #6b7280; }}
  .footer {{ text-align: center; color: #9ca3af; font-size: 11px; margin-top: 20px; }}
</style>
</head>
<body>
<div class="wrap">

  <div class="verdict {verdict_class}">
    <span class="num">{n_pass}/{n_total} PASS</span>
    <span>{verdict_text} · {duration:.1f}s{verdict_tail}</span>
  </div>

  <div class="meta">
    <div class="card"><div class="label">Started</div><div class="value mono">{started}</div></div>
    <div class="card"><div class="label">Ended</div><div class="value mono">{ended}</div></div>
    <div class="card"><div class="label">Duration</div><div class="value">{duration:.1f} s</div></div>
    <div class="card"><div class="label">Instrument</div><div class="value mono">{instr}</div></div>
    <div class="card"><div class="label">EUT Interface</div><div class="value">{eut_text}</div></div>
    <div class="card"><div class="label">Plan</div><div class="value">{plan_text}</div></div>
  </div>

  <div class="section dashboard">
    <h2>Per-test results</h2>
    <img src="data:image/png;base64,{dashboard_b64}" alt="BLE test dashboard">
  </div>

  <div class="section">
    <h2>All measurements</h2>
    <table>
      <thead>
        <tr>
          <th>Test</th><th>Name</th><th>Ch</th><th>Payload</th>
          <th>Value</th><th>Result</th><th>Elapsed</th>
        </tr>
      </thead>
      <tbody>
        {table_html}
      </tbody>
    </table>
  </div>

  <div class="footer">
    Generated by mcp_8852b/scripts/plot_ble_report.py
  </div>

</div>
</body>
</html>
"""


def find_latest_report(reports_root: Path) -> Path | None:
    candidates = sorted(reports_root.glob("ble_full_*"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def main() -> int:
    p = argparse.ArgumentParser(
        description="Generate visual HTML + PNG report from a BLE test folder")
    p.add_argument("--report-dir", help="Path to a reports/ble_full_<ts>/ folder")
    p.add_argument("--latest", action="store_true",
                   help="Use the most recent reports/ble_full_*/ folder (overrides --report-dir)")
    p.add_argument("--reports-root", default="reports",
                   help="Root folder to scan when --latest is set (default: reports)")
    p.add_argument("--no-html", action="store_true", help="Skip HTML generation (PNG only)")
    args = p.parse_args()

    if args.latest:
        root = Path(args.reports_root).resolve()
        latest = find_latest_report(root)
        if latest is None:
            print(f"No ble_full_* folders found under {root}", file=sys.stderr)
            return 1
        report_dir = latest
    elif args.report_dir:
        report_dir = Path(args.report_dir).resolve()
    else:
        p.error("Either --report-dir or --latest is required")

    if not report_dir.is_dir():
        print(f"Report dir not found: {report_dir}", file=sys.stderr)
        return 1

    try:
        rows, summary = load_report(report_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    groups = group_by_test(rows)
    print(f"[plot_ble_report] {len(rows)} measurements / {len(groups)} tests: "
          f"{', '.join(groups.keys())}")

    png_files = make_per_test_pngs(groups, report_dir)
    print(f"[plot_ble_report] wrote {len(png_files)} per-test PNGs")
    dashboard_png = make_dashboard_png(groups, report_dir)
    print(f"[plot_ble_report] wrote {dashboard_png}")

    if not args.no_html:
        html = make_html(summary, groups, report_dir, dashboard_png)
        out_html = report_dir / "report.html"
        out_html.write_text(html, encoding="utf-8")
        print(f"[plot_ble_report] wrote {out_html}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
