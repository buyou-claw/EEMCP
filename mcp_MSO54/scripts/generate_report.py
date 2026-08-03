"""Generate a self-contained test report from a live MSO54 reading.

Outputs (under `reports/<label>/`):
  state.json              — full instrument state, machine-readable
  waveform_chN.csv        — per-channel (time_s, voltage_v) rows
  waveform.png            — 4-channel overlay plot (matplotlib)
  scope_screenshot.png    — operator drops the scope's PNG here after pulling
                             it off manually (SAVE:IMAGE cannot be read back
                             over USBTMC, see NOTES.md §1)
  report.md               — human-readable summary embedding all of the above

Usage:
  python -m mcp_MSO54.scripts.generate_report
  python -m mcp_MSO54.scripts.generate_report --label PCBA_001_run3
  python -m mcp_MSO54.scripts.generate_report --label smoke --channels 1 --stop 500

Stop mechanism: `touch reports/<label>/.stop` between captures to break out
cleanly. (Future: long-running log mode. For now: single capture per run.)
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "E:/other/ai_project/EEMCP")

from mcp_MSO54.instrument import MSO54, MSO54Error
from mcp_MSO54.config import CONFIG, MEAS_UNIT


# ---------------------------------------------------------------------------
def _dump_state(scope: MSO54) -> dict:
    """One-shot read of everything we know how to read."""
    state = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "idn": scope.idn(),
        "options": scope.options(),
        "stb": f"0x{scope.read_stb():02x}",
        "channels": {},
        "horizontal": {
            "mode": scope.q_horizontal_mode(),
            "scale_s_per_div": scope.q_horizontal_scale(),
            "position_pct": scope.q_horizontal_position(),
            "sample_rate_sa_per_s": scope.q_sample_rate(),
            "record_length": scope.q_record_length(),
        },
        "acquisition": {
            "mode": scope.q_acq_mode(),
            "numavg": scope.q_acq_numavg(),
            "state": scope.q_acq_state(),
            "stopafter": scope.q_acq_stopafter(),
            "numacq": scope.q_acq_numacq(),
        },
        "trigger": {
            "type": scope.q_trig_type(),
            "edge_source": scope.q_trig_edge_source(),
            "edge_slope": scope.q_trig_edge_slope(),
            "edge_coupling": scope.q_trig_edge_coupling(),
            "mode": scope.q_trig_mode(),
            "state": scope.q_trig_state(),
            "level_ch1_v": scope.q_trig_level(1),
        },
        "measurements": [],
        "dvm": {
            "mode": scope.q_dvm_mode(),
            "source": scope.q_dvm_source(),
            "value_v": scope.q_dvm_value(),
        },
    }
    for n in range(1, CONFIG.num_channels + 1):
        state["channels"][f"CH{n}"] = {
            "display_on": scope.q_channel_display(n),
            "scale_v_per_div": scope.q_channel_scale(n),
            "position_divs": scope.q_channel_position(n),
            "offset_volts": scope.q_channel_offset(n),
            "coupling": scope.q_channel_coupling(n),
            "bandwidth_hz": scope.q_channel_bandwidth(n),
            "termination_ohms": scope.q_channel_termination(n),
            "label": scope.q_channel_label(n),
        }
    for n in range(1, 9):
        try:
            t = scope.q_meas_type(n); s = scope.q_meas_source(n); v = scope.q_meas_value(n)
            state["measurements"].append(
                {"slot": n, "type": t, "source": s,
                 "value": v, "unit": MEAS_UNIT.get(t, "")})
        except MSO54Error:
            break
    return state


def _capture_waveforms(scope: MSO54, channels: list[int], stop: int) -> dict:
    """Capture CURVE? for each requested channel. Returns {chN: wfm_dict}."""
    out = {}
    for ch in channels:
        out[ch] = scope.read_curve(f"CH{ch}", start=1, stop=stop, width=1)
    return out


def _write_csv(path: Path, times: list[float], values: list[float],
               unit: str) -> None:
    """FSYNC per write — see BUILDING_AN_INSTRUMENT_MCP §18."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(f"time_s,{unit}\n")
        for t, v in zip(times, values):
            f.write(f"{t:.9e},{v:.9e}\n")
        f.flush()
        os.fsync(f.fileno())


def _plot_waveforms(path: Path, waveforms: dict, state: dict) -> None:
    """4-channel overlay plot. matplotlib must be installed."""
    import matplotlib
    matplotlib.use("Agg")  # headless
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5))
    colors = {1: "C0", 2: "C1", 3: "C2", 4: "C3"}
    for ch, wfm in waveforms.items():
        label = f"CH{ch} ({wfm['unit']})"
        # Times are seconds; convert to µs for readability
        t_us = [t * 1e6 for t in wfm["times"]]
        ax.plot(t_us, wfm["values"], color=colors.get(ch, "k"),
                linewidth=0.8, label=label)
    ax.set_xlabel("time (µs)")
    ax.set_ylabel("voltage (V)")
    title = f"MSO54 waveform capture — {state['idn']}"
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=9)
    h = state["horizontal"]
    ax.text(0.99, -0.18,
            f"horiz: {h['scale_s_per_div']:.2e} s/div  "
            f"sample_rate: {h['sample_rate_sa_per_s']:.2e} Sa/s  "
            f"rec_len: {h['record_length']}",
            transform=ax.transAxes, ha="right", va="top", fontsize=8,
            family="monospace")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _write_report_md(path: Path, label: str, state: dict,
                     waveforms: dict, scope_png_saved: str | None) -> None:
    lines = []
    lines.append(f"# MSO54 Test Report — `{label}`")
    lines.append("")
    lines.append(f"**Captured at:** {state['captured_at']}  ")
    lines.append(f"**Instrument:** `{state['idn']}`  ")
    lines.append(f"**Options:** {state['options']}  ")
    lines.append(f"**STB:** `{state['stb']}`")
    lines.append("")

    lines.append("## Horizontal")
    h = state["horizontal"]
    lines.append(f"- mode: `{h['mode']}`")
    lines.append(f"- scale: `{h['scale_s_per_div']:.3e}` s/div")
    lines.append(f"- position: `{h['position_pct']}%`")
    lines.append(f"- sample rate: `{h['sample_rate_sa_per_s']:.3e}` Sa/s")
    lines.append(f"- record length: `{h['record_length']}` points")
    lines.append("")

    lines.append("## Acquisition")
    a = state["acquisition"]
    lines.append(f"- mode: `{a['mode']}`")
    lines.append(f"- numavg: `{a['numavg']}`")
    lines.append(f"- state: `{a['state']}` (1=RUN, 0=STOP)")
    lines.append(f"- stopafter: `{a['stopafter']}`")
    lines.append(f"- numacq: `{a['numacq']}`")
    lines.append("")

    lines.append("## Trigger")
    t = state["trigger"]
    lines.append(f"- type: `{t['type']}`")
    lines.append(f"- edge source: `{t['edge_source']}`  slope: `{t['edge_slope']}`  "
                 f"coupling: `{t['edge_coupling']}`")
    lines.append(f"- mode: `{t['mode']}`  state: `{t['state']}`")
    lines.append(f"- level (CH1): `{t['level_ch1_v']} V`")
    lines.append("")

    lines.append("## Channels")
    lines.append("")
    lines.append("| Ch | Display | Scale (V/div) | Pos (div) | Offset (V) | Coupling | BW (Hz) | Term (Ω) | Label |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for ch_name, c in state["channels"].items():
        lines.append(
            f"| {ch_name} | `{c['display_on']}` | `{c['scale_v_per_div']}` | "
            f"`{c['position_divs']}` | `{c['offset_volts']}` | `{c['coupling']}` | "
            f"`{c['bandwidth_hz']:.0e}` | `{c['termination_ohms']:.0e}` | `{c['label']}` |"
        )
    lines.append("")

    lines.append("## Measurements")
    if state["measurements"]:
        lines.append("")
        lines.append("| Slot | Type | Source | Value | Unit |")
        lines.append("|---|---|---|---|---|")
        for m in state["measurements"]:
            lines.append(f"| MEAS{m['slot']} | `{m['type']}` | `{m['source']}` | "
                         f"`{m['value']:.6e}` | `{m['unit']}` |")
    else:
        lines.append("")
        lines.append("_(none installed)_")
    lines.append("")

    lines.append("## DVM")
    d = state["dvm"]
    lines.append(f"- mode: `{d['mode']}`")
    lines.append(f"- source: `{d['source']}`")
    lines.append(f"- value: **`{d['value_v']:.6f} V`**")
    lines.append("")

    lines.append("## Waveform")
    if waveforms:
        lines.append("")
        lines.append(f"Captured {len(waveforms)} channel(s) — see `waveform.png`.")
        lines.append("")
        lines.append("![waveform](waveform.png)")
        lines.append("")
        lines.append("Per-channel stats:")
        lines.append("")
        lines.append("| Channel | Points | vmin | vmax | vmean | vp-p |")
        lines.append("|---|---|---|---|---|---|")
        for ch, wfm in waveforms.items():
            v = wfm["values"]
            vmin, vmax = min(v), max(v)
            vmean = sum(v) / len(v)
            lines.append(
                f"| CH{ch} | {wfm['npoints']} | `{vmin:+.4f}` | `{vmax:+.4f}` | "
                f"`{vmean:+.4f}` | `{vmax - vmin:.4f}` |"
            )
    else:
        lines.append("")
        lines.append("_(no waveform captured — pass `--channels 1` etc. to enable)_")
    lines.append("")

    lines.append("## Scope screenshot (manual)")
    lines.append("")
    scope_png_path = path.parent / "scope_screenshot.png"
    if scope_png_path.exists():
        # Operator already pulled the file — embed it.
        lines.append(f"![scope screenshot](scope_screenshot.png)")
        lines.append("")
    elif scope_png_saved:
        lines.append(f"Sent `SAVE:IMAGE {scope_png_saved!r}` to the scope. The scope")
        lines.append("silently translated the path on this firmware to its Windows-style")
        lines.append("Temp directory (e.g. `C:/Users/<user>/AppData/Local/Temp/...`).")
        lines.append("")
        lines.append("**Pull the PNG off the scope manually** (touchscreen file manager")
        lines.append("or USB stick) and copy it to `scope_screenshot.png` in this")
        lines.append("directory to embed it below:")
        lines.append("")
        lines.append("```bash")
        lines.append(f"# After pulling the file off the scope:")
        lines.append(f"cp /path/to/pulled.png {scope_png_path}")
        lines.append("```")
        lines.append("")
        lines.append("Then re-run the report generator or just refresh this markdown view.")
    else:
        lines.append("_Not captured for this run. Use `--save-scope-screenshot` next time._")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"Generated by `mcp_MSO54/scripts/generate_report.py` at "
                 f"{datetime.now().isoformat(timespec='seconds')}.")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--label", default=datetime.now().strftime("run_%Y%m%d_%H%M%S"),
                    help="Subdirectory name under reports/ (default: timestamp)")
    ap.add_argument("--out-root", default="reports",
                    help="Parent directory for the report (default: ./reports)")
    ap.add_argument("--channels", default="1,2,3,4",
                    help="Comma-separated channel list to capture (default: 1,2,3,4)")
    ap.add_argument("--skip-off-channels", action="store_true", default=True,
                    help="If a requested channel has display=OFF, skip it with a "
                         "warning instead of failing (default: True). Passing "
                         "--channels that are OFF can wedge USBTMC bulk-IN.")
    ap.add_argument("--stop", type=int, default=1000,
                    help="CURVE? stop index (default: 1000 = 1000 points)")
    ap.add_argument("--save-scope-screenshot", action="store_true",
                    help="Send SAVE:IMAGE to the scope; operator must pull the PNG "
                         "off manually and drop it at scope_screenshot.png")
    args = ap.parse_args()

    out_dir = Path(args.out_root) / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing report to: {out_dir}")

    channels = [int(c) for c in args.channels.split(",") if c.strip()]
    if any(c < 1 or c > CONFIG.num_channels for c in channels):
        print(f"ERROR: channels must be in 1..{CONFIG.num_channels}", file=sys.stderr)
        return 1
    if args.stop - 0 + 1 > 10000:
        print("ERROR: --stop must be <= 10000 to keep transfer bounded",
              file=sys.stderr)
        return 1

    scope = MSO54()
    scope.connect()

    # Optional: send SAVE:IMAGE first so the operator has time to pull the
    # file off while the report generates.
    scope_png_target = None
    if args.save_scope_screenshot:
        scope_png_target = "/tmp/mcp_mso54_report.png"
        try:
            scope.fs_delete(scope_png_target)
        except MSO54Error:
            pass
        scope.save_image(scope_png_target)
        print(f"  -> SAVE:IMAGE sent; pull {scope_png_target} from scope manually")

    try:
        print("  -> reading full state...")
        state = _dump_state(scope)
        with open(out_dir / "state.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())

        print(f"  -> capturing CURVE? for CH{', CH'.join(str(c) for c in channels)} "
              f"(stop={args.stop})...")

        # Filter out display=OFF channels — querying their preamble wedges
        # USBTMC bulk-IN (the scope hangs instead of returning an error).
        # See NOTES.md §1 (trigger #2).
        active_channels = []
        for ch in channels:
            if scope.q_channel_display(ch):
                active_channels.append(ch)
            else:
                print(f"     [skip] CH{ch} display=OFF — would wedge bulk-IN")
        if not active_channels:
            print("ERROR: no requested channels have display=ON; nothing to capture",
                  file=sys.stderr)
            return 1
        waveforms = _capture_waveforms(scope, active_channels, args.stop)
        for ch, wfm in waveforms.items():
            _write_csv(out_dir / f"waveform_ch{ch}.csv",
                       wfm["times"], wfm["values"], wfm["unit"])
            print(f"     wrote waveform_ch{ch}.csv  ({wfm['npoints']} points)")

        print("  -> generating waveform plot...")
        _plot_waveforms(out_dir / "waveform.png", waveforms, state)
        print(f"     wrote waveform.png")

        print("  -> writing report.md...")
        _write_report_md(out_dir / "report.md", args.label, state,
                         waveforms, scope_png_target)
        print(f"     wrote report.md")

    finally:
        scope.disconnect()

    print()
    print(f"Done. Report at: {out_dir}")
    print(f"  state.json       — machine-readable full state")
    print(f"  waveform_ch*.csv — per-channel time/voltage")
    print(f"  waveform.png     — 4-channel overlay plot")
    if scope_png_target:
        print(f"  scope_screenshot.png — NOT YET present; pull {scope_png_target!r}")
        print(f"     off the scope manually (NOTES.md §1) and drop here.")
    print(f"  report.md        — open this in your editor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
