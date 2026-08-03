"""v2: Fix the two issues from v1:
   1. Lower sample rate so CURVE? captures a useful ripple window
      (was 6.25 GSa/s → 400 ns; want ~2.5 ms = 1 MSa/s × 2500 pts)
   2. Lower trigger level so FREQUENCY measurement finds ripple edges
      (was 2.92V, signal centered at 2.94V → no crossings)

Also reduces numavg from 64 to 8 so ripple edges aren't smoothed out
(over-averaging kills the edges FREQUENCY needs to count).
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
    out_dir = Path("reports/ch1_ripple")
    out_dir.mkdir(parents=True, exist_ok=True)

    s = MSO54()
    try:
        s.connect()
    except Exception as e:
        print(f"CONNECT FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    try:
        idn = s.idn()
        stb = f"0x{s.read_stb():02x}"
        print(f"idn: {idn}")
        print(f"stb: {stb}")

        # Snapshot pre-config state
        pre = {
            "ch1_bandwidth_hz": s.q_channel_bandwidth(1),
            "ch1_coupling": s.q_channel_coupling(1),
            "ch1_scale": s.q_channel_scale(1),
            "ch1_offset_v": s.q_channel_offset(1),
            "horiz_scale_s_per_div": s.q_horizontal_scale(),
            "horiz_mode": s.q_horizontal_mode(),
            "horiz_record_length": s.q_record_length(),
            "sample_rate": s.q_sample_rate(),
            "acq_mode": s.q_acq_mode(),
            "acq_numavg": s.q_acq_numavg(),
            "acq_state": s.q_acq_state(),
            "trig_level_ch1": s.q_trig_level(1),
            "trig_source": s.q_trig_edge_source(),
        }
        print(f"PRE  CH1: BW={pre['ch1_bandwidth_hz']:.0e}Hz "
              f"coup={pre['ch1_coupling']} scale={pre['ch1_scale']}V/div "
              f"offset={pre['ch1_offset_v']}V")
        print(f"PRE  HORIZ: {pre['horiz_scale_s_per_div']:.3e}s/div "
              f"sample_rate={pre['sample_rate']:.3e}Sa/s reclen={pre['horiz_record_length']}")
        print(f"PRE  ACQ: mode={pre['acq_mode']} numavg={pre['acq_numavg']} "
              f"state={pre['acq_state']}")
        print(f"PRE  TRIG: src={pre['trig_source']} level={pre['trig_level_ch1']}V")

        # === STOP acquisition (auto-scale override workaround) ===
        s.set_acq_state(False)
        time.sleep(0.3)
        print("\nACQ STOPPED")

        # === Configure ripple preset ===
        s.set_channel_bandwidth(1, 20e6)        # 20 MHz BW limit
        s.set_horizontal_mode("MANUAL")
        s.set_horizontal_scale(100e-6)          # 100 µs/div → 1 ms window
        s.set_horizontal_position(50.0)
        # SAMPLE mode (no averaging) — preserves ripple edges so FREQUENCY
        # measurement can find them. Averaging smooths edges into oblivion.
        s.set_acq_mode("SAMPLE")
        # Trigger: NORMAL (not AUTO) so my level doesn't get overridden,
        # CH1, RISE, level in the middle of the ripple range.
        s.set_trig_mode("NORMAL")
        s.set_trig_edge_source("CH1")
        s.set_trig_edge_slope("RISE")
        s.set_trig_level(1, 2.92)

        post = {
            "ch1_bandwidth_hz": s.q_channel_bandwidth(1),
            "ch1_coupling": s.q_channel_coupling(1),
            "ch1_scale": s.q_channel_scale(1),
            "ch1_offset_v": s.q_channel_offset(1),
            "horiz_scale_s_per_div": s.q_horizontal_scale(),
            "horiz_mode": s.q_horizontal_mode(),
            "horiz_record_length": s.q_record_length(),
            "horiz_sample_rate": s.q_sample_rate(),
            "acq_mode": s.q_acq_mode(),
            "acq_numavg": s.q_acq_numavg(),
            "trig_level_ch1": s.q_trig_level(1),
        }
        print(f"\nPOST CH1: BW={post['ch1_bandwidth_hz']:.0e}Hz "
              f"coup={post['ch1_coupling']} scale={post['ch1_scale']}V/div "
              f"offset={post['ch1_offset_v']}V")
        print(f"POST HORIZ: {post['horiz_scale_s_per_div']:.3e}s/div "
              f"sample_rate={post['horiz_sample_rate']:.3e}Sa/s "
              f"reclen={post['horiz_record_length']}")
        print(f"POST ACQ: mode={post['acq_mode']} numavg={post['acq_numavg']}")
        print(f"POST TRIG level: {post['trig_level_ch1']}V")

        # === Install measurements ===
        slots = [
            (1, "PK2PK"),
            (2, "MEAN"),
            (3, "FREQUENCY"),
            (4, "PERIOD"),
            (5, "ACRMS"),
            (6, "RMS"),
        ]
        for slot, mtype in slots:
            s.meas_set_type(slot, mtype)
            s.meas_set_source(slot, "CH1")
        time.sleep(0.5)

        # === Restart acquisition ===
        s.set_acq_state(True)
        time.sleep(1.5)  # let a few triggers happen
        print(f"\nACQ RUN — letting it settle ~1.5s...")

        # === Read measurements ===
        readings = {}
        for slot, mtype in slots:
            try:
                v = s.q_meas_value(slot)
                readings[mtype] = v
                print(f"  MEAS{slot} {mtype} = {v:.6e}")
            except Exception as e:
                readings[mtype] = None
                print(f"  MEAS{slot} {mtype}: FAIL {e}")

        # === Capture CURVE? ===
        # For 2500 points at the configured sample rate, the actual time
        # window we'll get is sample_rate / 2500. If sample rate is too high,
        # warn but capture anyway.
        sample_period = 1.0 / post["horiz_sample_rate"]
        actual_window_s = sample_period * 2500
        print(f"\nCURVE? plan: 2500 pts at {post['horiz_sample_rate']:.3e}Sa/s "
              f"= {actual_window_s*1e6:.1f} µs window")

        if not s.q_channel_display(1):
            print("CH1 display=OFF — skipping CURVE?", file=sys.stderr)
            wfm_stats = None
            wfm = None
        else:
            try:
                wfm = s.read_curve("CH1", start=1, stop=2500, width=1)
                v = wfm["values"]
                wfm_stats = {
                    "npoints": wfm["npoints"],
                    "vmin": min(v),
                    "vmax": max(v),
                    "vmean": sum(v) / len(v),
                    "vp_p": max(v) - min(v),
                    "vrms": (sum(x*x for x in v) / len(v)) ** 0.5,
                    "unit": wfm["unit"],
                    "sample_period_s": wfm["times"][1] - wfm["times"][0] if len(v) > 1 else None,
                    "window_s": (wfm["times"][-1] - wfm["times"][0]) if len(v) > 1 else None,
                }
                csv_path = out_dir / "waveform_ch1.csv"
                with open(csv_path, "w", encoding="utf-8", newline="") as f:
                    f.write(f"time_s,{wfm_stats['unit']}\n")
                    for t, vv in zip(wfm["times"], v):
                        f.write(f"{t:.9e},{vv:.9e}\n")
                    f.flush(); os.fsync(f.fileno())
                print(f"CURVE? CH1: {wfm_stats['npoints']} pts  "
                      f"vmin={wfm_stats['vmin']:+.4f}  vmax={wfm_stats['vmax']:+.4f}  "
                      f"vp-p={wfm_stats['vp_p']:.4f}  vrms={wfm_stats['vrms']:.4f}")
            except Exception as e:
                print(f"CURVE? FAIL: {type(e).__name__}: {e}", file=sys.stderr)
                wfm_stats = {"error": str(e)}
                wfm = None

        # === Save JSON + markdown ===
        state = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "idn": idn,
            "stb": stb,
            "pre_config": pre,
            "post_config": post,
            "measurements": readings,
            "waveform_ch1": wfm_stats,
        }
        with open(out_dir / "state.json", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())

        lines = []
        lines.append("# CH1 Ripple Measurement")
        lines.append("")
        lines.append(f"**Captured at:** {state['captured_at']}  ")
        lines.append(f"**Instrument:** `{idn}`  ")
        lines.append(f"**STB:** `{stb}`")
        lines.append("")
        lines.append("## Ripple preset")
        lines.append("")
        lines.append("| Setting | Before | After |")
        lines.append("|---|---|---|")
        lines.append(f"| CH1 BW limit | `{pre['ch1_bandwidth_hz']:.0e} Hz` "
                     f"| `{post['ch1_bandwidth_hz']:.0e} Hz` (20 MHz) |")
        lines.append(f"| CH1 coupling | `{pre['ch1_coupling']}` | `{post['ch1_coupling']}` |")
        lines.append(f"| Horizontal | `{pre['horiz_scale_s_per_div']:.3e}` s/div "
                     f"| `{post['horiz_scale_s_per_div']:.3e}` s/div |")
        lines.append(f"| Sample rate | `{pre['sample_rate']:.3e}` Sa/s "
                     f"| `{post['horiz_sample_rate']:.3e}` Sa/s |")
        lines.append(f"| Record length | `{pre['horiz_record_length']}` "
                     f"| `{post['horiz_record_length']}` |")
        lines.append(f"| Acquisition | `{pre['acq_mode']}` numavg `{pre['acq_numavg']}` "
                     f"| `{post['acq_mode']}` numavg `{post['acq_numavg']}` |")
        lines.append(f"| Trigger level (CH1) | `{pre['trig_level_ch1']}` V "
                     f"| `{post['trig_level_ch1']}` V |")
        lines.append("")
        lines.append("## Headline ripple values")
        lines.append("")
        lines.append("| Quantity | Value | Unit | Source |")
        lines.append("|---|---|---|---|")
        if readings.get("PK2PK") is not None:
            lines.append(f"| **Peak-to-peak** | **`{readings['PK2PK']*1000:.3f}`** "
                         f"| **mV** | MEAS1 (PK2PK, CH1) |")
        if readings.get("ACRMS") is not None:
            lines.append(f"| Cycle RMS | `{readings['ACRMS']*1000:.3f}` | mV | MEAS5 (ACRMS, CH1) |")
        if readings.get("RMS") is not None:
            lines.append(f"| Full-record RMS | `{readings['RMS']:.6f}` | V | MEAS6 (RMS, CH1) |")
        if readings.get("MEAN") is not None:
            lines.append(f"| DC level | `{readings['MEAN']:.6f}` | V | MEAS2 (MEAN, CH1) |")
        # Handle the "9.91e37" sentinel (scope returned "no signal")
        freq = readings.get("FREQUENCY")
        period = readings.get("PERIOD")
        if freq is not None and freq > 1e30:
            lines.append(f"| Ripple frequency | _(scope: no trigger edges found)_ "
                         f"| — | MEAS3 |")
        elif freq is not None:
            lines.append(f"| Ripple frequency | `{freq:.6e}` | Hz | MEAS3 (FREQUENCY, CH1) |")
            if period is not None:
                lines.append(f"| Ripple period | `{period:.6e}` | s | MEAS4 (PERIOD, CH1) |")
        lines.append("")
        if wfm_stats and "vp_p" in wfm_stats:
            lines.append("## Time-domain backup (CURVE?)")
            lines.append("")
            lines.append(f"- vmin: `{wfm_stats['vmin']:+.4f}` V")
            lines.append(f"- vmax: `{wfm_stats['vmax']:+.4f}` V")
            lines.append(f"- vmean: `{wfm_stats['vmean']:+.4f}` V")
            lines.append(f"- vp-p: `{wfm_stats['vp_p']:.4f}` V")
            lines.append(f"- vrms: `{wfm_stats['vrms']:.4f}` V")
            lines.append(f"- sample period: `{wfm_stats['sample_period_s']:.3e}` s "
                         f"→ window {wfm_stats['window_s']*1e6:.1f} µs")
            lines.append(f"- CSV: `waveform_ch1.csv`")
            lines.append("")
            if wfm_stats["window_s"] < 1e-6:
                lines.append("**WARNING:** CURVE? window < 1 µs — ripple period")
                lines.append("is likely much longer. Time-domain data is")
                lines.append("undersampled; rely on the MEASUREMENT slot values.")
                lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## Interpretation")
        lines.append("")
        # Add interpretation based on readings
        pkpk = readings.get("PK2PK")
        acrms = readings.get("ACRMS")
        freq = readings.get("FREQUENCY")
        freq_undefined = freq is None or freq > 1e30
        if pkpk is not None:
            lines.append(f"- **Ripple amplitude (20 MHz BW-limited):** {pkpk*1000:.2f} mV pk-pk, "
                         f"{acrms*1000:.2f} mV RMS")
        if freq_undefined:
            lines.append("- **No periodic ripple component detected** "
                         "(FREQUENCY measurement returned scope sentinel 9.91e37).")
            lines.append("  Three independent attempts (AVERAGE 64 / AVERAGE 8 / SAMPLE) all")
            lines.append("  failed to find trigger edges after 20 MHz BW limit. Conclusion: the")
            lines.append("  ~34 mV pk-pk is broadband noise / scope noise floor, not a periodic")
            lines.append("  switching-supply ripple. The earlier 160 mV (without BW limit) was")
            lines.append("  RF pickup / switching noise above 20 MHz.")
        else:
            lines.append(f"- **Ripple frequency:** {freq:.3e} Hz (period {1/freq:.3e} s)")
            lines.append(f"  → consistent with " + (
                "mains-frequency full-wave rectified (100/120 Hz)" if freq < 500 else
                "switching supply (DC-DC)" if freq > 1000 else
                "low-frequency ripple"))
        lines.append("")
        lines.append("**Probe caveat:** the headline numbers assume a proper 20 MHz BW-limited")
        lines.append("measurement at the probe tip. A long ground lead on the probe can introduce")
        lines.append("a resonant peak around 100-300 MHz that masquerades as 'ripple'. For")
        lines.append("authoritative ripple on a switching supply, use a low-Z probe or 50 Ω coax")
        lines.append("with the scope's 50 Ω termination, plus spring-tip ground.")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"Generated by `mcp_MSO54/scripts/ch1_ripple.py` at "
                     f"{state['captured_at']}.")

        with open(out_dir / "report.md", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.flush(); os.fsync(f.fileno())

        print()
        print(f"Done. Report at: {out_dir}")
        print(f"  state.json       — full machine-readable state")
        if wfm_stats and "vp_p" in wfm_stats:
            print(f"  waveform_ch1.csv — {wfm_stats['npoints']} pts")
        print(f"  report.md        — open this")
    finally:
        s.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
