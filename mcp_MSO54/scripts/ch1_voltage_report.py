"""Single-shot CH1 voltage snapshot + minimal markdown report.

Strategy:
  1. Connect (will fail loudly if USBTMC bulk-IN still wedged)
  2. Use DVM (already validated, P8) for the headline voltage value —
     no binary transfers, lowest wedge risk
  3. Add a MEAN measurement on CH1 as a second opinion
  4. Capture a 500-pt CURVE? as a waveform backup (gate on CH1 display=ON)
  5. Write report.md to reports/ch1_voltage/

Run:
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python -m mcp_MSO54.scripts.ch1_voltage_report
"""
from __future__ import annotations
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "E:/other/ai_project/EEMCP")

from mcp_MSO54.instrument import MSO54, MSO54Error


def main() -> int:
    out_dir = Path("reports/ch1_voltage")
    out_dir.mkdir(parents=True, exist_ok=True)

    s = MSO54()
    try:
        s.connect()
    except Exception as e:
        print(f"CONNECT FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        print("Scope is likely still USBTMC-wedged. USB-port reset required.",
              file=sys.stderr)
        return 1

    try:
        idn = s.idn()
        stb = f"0x{s.read_stb():02x}"
        print(f"idn: {idn}")
        print(f"stb: {stb}")

        # --- DVM (cheapest, text-only path) ---
        dvm_mode = s.q_dvm_mode()
        dvm_src = s.q_dvm_source()
        print(f"DVM: mode={dvm_mode}  source={dvm_src}")
        if dvm_mode != "DC":
            print("  setting DVM mode=DC, source=CH1 ...")
            s.set_dvm_mode("DC")
            s.set_dvm_source("CH1")
            # Per Tek docs, DVM needs a moment to compute the first sample
            time.sleep(0.5)
            dvm_mode = s.q_dvm_mode()
            dvm_src = s.q_dvm_source()
        dvm_value = s.q_dvm_value()
        print(f"DVM value: {dvm_value:.6f} V")

        # --- Mean measurement (text-only) ---
        # meas_add() returns nothing — it appends to the next free slot.
        # Strategy: find the lowest empty slot via q_meas_type() then set type+source.
        meas_slot = None
        for slot in range(1, 9):
            existing = s.q_meas_type(slot)
            if not existing or existing.strip() == "":
                meas_slot = slot
                break
        if meas_slot is None:
            # All slots full — overwrite slot 1
            print("  all MEAS slots full — overwriting MEAS1")
            meas_slot = 1
        s.meas_set_type(meas_slot, "MEAN")
        s.meas_set_source(meas_slot, "CH1")
        time.sleep(0.3)
        meas_type_back = s.q_meas_type(meas_slot)
        meas_src_back = s.q_meas_source(meas_slot)
        meas_value = s.q_meas_value(meas_slot)
        print(f"MEAN[{meas_slot}] type={meas_type_back} src={meas_src_back} "
              f"value={meas_value:.6f} V")

        # --- CH1 state ---
        ch1 = {
            "display": s.q_channel_display(1),
            "scale_v_per_div": s.q_channel_scale(1),
            "position_divs": s.q_channel_position(1),
            "offset_volts": s.q_channel_offset(1),
            "coupling": s.q_channel_coupling(1),
            "termination_ohms": s.q_channel_termination(1),
            "label": s.q_channel_label(1),
        }
        trigger = {
            "type": s.q_trig_type(),
            "edge_source": s.q_trig_edge_source(),
            "edge_slope": s.q_trig_edge_slope(),
            "edge_coupling": s.q_trig_edge_coupling(),
            "mode": s.q_trig_mode(),
            "state": s.q_trig_state(),
            "level_ch1_v": s.q_trig_level(1),
        }
        horiz = {
            "mode": s.q_horizontal_mode(),
            "scale_s_per_div": s.q_horizontal_scale(),
            "position_pct": s.q_horizontal_position(),
            "sample_rate": s.q_sample_rate(),
            "record_length": s.q_record_length(),
        }
        acq = {
            "mode": s.q_acq_mode(),
            "state": s.q_acq_state(),
            "numavg": s.q_acq_numavg(),
            "numacq": s.q_acq_numacq(),
        }

        # --- CURVE? on CH1 (gated on display=ON; per NOTES §1 trigger #4) ---
        wfm_stats = None
        if ch1["display"]:
            try:
                wfm = s.read_curve("CH1", start=1, stop=500, width=1)
                v = wfm["values"]
                wfm_stats = {
                    "npoints": wfm["npoints"],
                    "vmin": min(v),
                    "vmax": max(v),
                    "vmean": sum(v) / len(v),
                    "vp_p": max(v) - min(v),
                    "unit": wfm["unit"],
                }
                # Per-channel CSV
                csv_path = out_dir / "waveform_ch1.csv"
                with open(csv_path, "w", encoding="utf-8", newline="") as f:
                    f.write(f"time_s,{wfm_stats['unit']}\n")
                    for t, vv in zip(wfm["times"], v):
                        f.write(f"{t:.9e},{vv:.9e}\n")
                    f.flush(); os.fsync(f.fileno())
                print(f"  CURVE? CH1: {wfm_stats['npoints']} pts  "
                      f"vmin={wfm_stats['vmin']:+.4f}  vmax={wfm_stats['vmax']:+.4f}  "
                      f"vmean={wfm_stats['vmean']:+.4f}")
            except Exception as e:
                print(f"  CURVE? FAIL: {type(e).__name__}: {e}", file=sys.stderr)
                wfm_stats = {"error": str(e)}
        else:
            print("  CH1 display=OFF — skipping CURVE? (would wedge bulk-IN)")

        # --- JSON dump ---
        state = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "idn": idn,
            "stb": stb,
            "ch1": ch1,
            "trigger": trigger,
            "horizontal": horiz,
            "acquisition": acq,
            "dvm": {"mode": dvm_mode, "source": dvm_src, "value_v": dvm_value},
            "meas_mean_ch1_v": meas_value,
            "meas_slot": meas_slot,
            "waveform_ch1": wfm_stats,
        }
        with open(out_dir / "state.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())

        # --- Markdown report ---
        lines = []
        lines.append("# CH1 Voltage Snapshot")
        lines.append("")
        lines.append(f"**Captured at:** {state['captured_at']}  ")
        lines.append(f"**Instrument:** `{idn}`  ")
        lines.append(f"**STB:** `{stb}`")
        lines.append("")
        lines.append("## Headline value")
        lines.append("")
        lines.append(f"- **DVM (DC, CH1):** `{dvm_value:+.6f} V`")
        lines.append(f"- **MEAN[{meas_slot}] on CH1:** `{meas_value:+.6f} V`")
        if wfm_stats and "vmean" in wfm_stats:
            lines.append(f"- **CURVE? waveform mean (500 pts):** "
                         f"`{wfm_stats['vmean']:+.6f} V`")
            lines.append(f"  (min `{wfm_stats['vmin']:+.4f}`, "
                         f"max `{wfm_stats['vmax']:+.4f}`, "
                         f"p-p `{wfm_stats['vp_p']:.4f}`)")
        lines.append("")
        lines.append("## Acquisition")
        lines.append("")
        lines.append(f"- mode: `{acq['mode']}`, state: `{acq['state']}`, "
                     f"numavg: `{acq['numavg']}`, numacq: `{acq['numacq']}`")
        lines.append(f"- horizontal: `{horiz['scale_s_per_div']:.3e}` s/div, "
                     f"`{horiz['sample_rate']:.3e}` Sa/s, "
                     f"reclen `{horiz['record_length']}`")
        lines.append("")
        lines.append("## Trigger")
        lines.append("")
        lines.append(f"- `{trigger['type']}` on `{trigger['edge_source']}`, "
                     f"slope `{trigger['edge_slope']}`, "
                     f"coupling `{trigger['edge_coupling']}`")
        lines.append(f"- mode `{trigger['mode']}`, state `{trigger['state']}`, "
                     f"level `{trigger['level_ch1_v']}` V")
        lines.append("")
        lines.append("## CH1 settings")
        lines.append("")
        lines.append(f"- display: `{ch1['display']}`, "
                     f"scale: `{ch1['scale_v_per_div']}` V/div, "
                     f"position: `{ch1['position_divs']}` div")
        lines.append(f"- coupling: `{ch1['coupling']}`, "
                     f"termination: `{ch1['termination_ohms']:.0e}` Ω, "
                     f"offset: `{ch1['offset_volts']}` V, "
                     f"label: `{ch1['label']}`")
        lines.append("")
        if wfm_stats and "vmean" in wfm_stats:
            lines.append("## Waveform (500 pts)")
            lines.append("")
            lines.append(f"CSV written to `waveform_ch1.csv` "
                         f"({wfm_stats['npoints']} points, "
                         f"time/voltage columns, unit `{wfm_stats['unit']}`).")
            lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"Generated by `mcp_MSO54/scripts/ch1_voltage_report.py` at "
                     f"{state['captured_at']}.")
        with open(out_dir / "report.md", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.flush(); os.fsync(f.fileno())

        print()
        print(f"Done. Report at: {out_dir}")
        print(f"  state.json   — full machine-readable state")
        if wfm_stats and "vmean" in wfm_stats:
            print(f"  waveform_ch1.csv — {wfm_stats['npoints']} pts")
        print(f"  report.md    — open this")
    finally:
        s.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
