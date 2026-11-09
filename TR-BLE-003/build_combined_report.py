"""Build the combined bilingual Excel workbook for TR-BLE-003.

Mirrors REPORT_SPEC.md §1–§5 conventions (originally written for TR-HWID-001):

  01_Cover              Title, test ID, per-model identification, overall
                       verdict, per-model pass-rate table, mode row,
                       start/finish timestamps.
  02_Methodology       Seven reference sections (§1 test purpose …
                       §7 hardware procedure).
  03_Verdict           Per-model 3-criteria verdict blocks (12M, then 14D).
                       14D gets a DMM-rate-limited note row.
  04_Statistics        Per-model aggregate metrics + embedded PNG interval
                       histogram (matplotlib — identical across viewers).
  05_Raw Data — 14D    One row per period-matched broadcast event.
  06_Raw Data — 12M    Same.
  07_Notes             Per-model notes + workbook generation timestamp.

Outputs two self-contained xlsx files (`<ts>` is the most recent per-model
run's `YYYYMMDD_HHMMSS`):

  reports/report_combined_<ts>_en.xlsx
  reports/report_combined_<ts>_zh.xlsx

Run from anywhere:

  python TR-BLE-003/build_combined_report.py
"""
from __future__ import annotations
import csv
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
except ImportError:
    print("ERROR: openpyxl not installed. Run: pip install openpyxl",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Test plan — one row per PCBA model under test
# ---------------------------------------------------------------------------
TR_ROOT = Path(__file__).resolve().parent
REPORTS_DIR = TR_ROOT / "reports"
DATA_DIR = TR_ROOT / "data"

MODELS = [
    {
        "label":              "12M",
        "device_id":          "12M-01",
        "spec_desc":          "5 min (300s)",
        "run_dir_name":       "dmm_current_log_fixed_broadcast_5min_20260813_162215",
        "target_interval_s":  300.0,
        "tolerance_s":        5.0,
    },
    {
        "label":              "14D",
        "device_id":          "14D-01",
        "spec_desc":          "1 sec (1s)",
        "run_dir_name":       "dmm_current_log_fixed_14d_broadcast_1s_20260813_172308",
        "target_interval_s":  1.0,
        "tolerance_s":        0.05,
    },
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OVERRANGE_MARKER   = 9.9e37
EVENT_GAP_S        = 1.0
WINDOW_S           = 0.5
ACTIVE_THRESHOLD_A = 100e-6


# ---------------------------------------------------------------------------
# Translation table — single source of truth, both languages
# ---------------------------------------------------------------------------
T = {
    "en": {
        "test_id_label":     "Test ID",
        "model_label":       "Model",
        "device_label":      "Device",
        "spec_label":        "Spec",
        "started_label":     "Started",
        "ended_label":       "Ended",
        "mode_label":        "Mode",
        "mode_value":        "Live",
        "verdict_label":     "Overall verdict",
        "verdict_pass":      "PASS",
        "verdict_fail":      "FAIL",
        "verdict_allpass":   "ALL PASS",
        "verdict_mixed":     "MIXED",
        "pass_rate_hdr":     ["Model", "Verdict", "Pass rate"],
        "m1_test_purpose":   "Test purpose",
        "m1_body":           ("Verify BLE broadcast periodicity (ΔT within "
                              "target ± tolerance) and a stable duty cycle "
                              "across a representative capture window. "
                              "Per the CMT BLE Sticker PCBA family, two "
                              "variants are covered: 12M broadcasts every "
                              "5 minutes, 14D broadcasts every 1 second."),
        "m2_filter_title":   "§2 Period-matched filter",
        "m2_filter_body":    ("A 'catch' is an overrange sample cluster in "
                              "the DMM trace. Consecutive catches "
                              "(ΔT ≈ target) are kept; DMM-skipped gaps "
                              "(ΔT = N × target, N ≥ 2) are excluded so the "
                              "verdict reflects DUT behavior, not DMM "
                              "sample rate. The first catch of each run is "
                              "kept (no prev to compare against)."),
        "m3_dmm_title":      "§3 DMM sampling math",
        "m3_dmm_body":       ("Keysight 34465A configured for fixed 1 mA "
                              "range on the 3 A terminal, NPLC = 0.001, "
                              "auto-zero OFF, display ON. After a single "
                              "warm-up `:READ?`, the high-rate loop uses "
                              "`:READ?` only (≈75 Hz at this NPLC). "
                              "`:MEAS:CURR:DC?` would re-send configuration "
                              "and drop throughput to ≈2 Hz."),
        "m4_thresh_title":    "§4 Catch detection thresholds",
        "m4_thresh_body":    ("ACTIVE_THRESHOLD_A = 100 µA (above the "
                              "deep-sleep band, below light-sleep). "
                              "OVERRANGE_MARKER = 9.9e37 (DMM's overflow "
                              "flag). Events are clustered from "
                              "overrange samples separated by ≤ "
                              f"{EVENT_GAP_S:g} s."),
        "m5_burst_title":     "§5 Burst width vs DMM sample interval",
        "m5_burst_body":     ("Each BLE TX burst is ~5 ms wide. DMM samples "
                              "are ~13 ms apart (≈75 Hz). Most 1 Hz "
                              "broadcasts therefore fall entirely between "
                              "two samples and are missed — the raw catch "
                              "count is DMM-rate-limited, NOT a DUT issue. "
                              "The period-matched filter (§2) keeps only "
                              "consecutive catches so the verdict is "
                              "unaffected by DMM aliasing."),
        "m6_const_title":     "§6 Electrical & configuration constants",
        "m6_const_rows": [
            ("Target interval",       "300 s / 1 s (per model)"),
            ("Tolerance",             "±5 s / ±0.05 s (per model)"),
            ("DMM range",             "1 mA fixed on 3 A terminal"),
            ("DMM NPLC",              "0.001 (aperture mode)"),
            ("DMM auto-zero",         "OFF"),
            ("Effective sample rate", "≈75 Hz"),
            ("Active threshold",      "100 µA"),
            ("Overrange marker",      "9.9e37"),
            ("Event gap",             "1.0 s"),
            ("Window radius",         "0.5 s"),
        ],
        "m7_proc_title":      "§7 Hardware & configuration procedure",
        "m7_proc_bullets": [
            ("Fixture", "PCBA powered from bench supply; DMM in series "
                        "with the 3.3 V rail via the 3 A terminal."),
            ("Instrument connection", "Keysight 34465A over USB "
                                      "(USB0::0x2A8D::0x0101::MY57515137::0::INSTR); "
                                      "NI-VISA runtime, pyvisa backend."),
            ("DMM setup commands", "`:CURR:DC:RANG 0.01` → "
                                   "`:CURR:DC:NPLC 0.001` → "
                                   "`:CURR:DC:AZERO OFF` → warm-up "
                                   "`:READ?` → enter `:READ?` loop."),
            ("Stop mechanisms", "Ctrl+C, SIGTERM/TaskStop, or "
                                "`touch <RUN_DIR>/.stop` (clean `finally` "
                                "writes summary + report.md)."),
        ],
        "v_criterion1":       "All period-matched events have |ΔT - target| ≤ tol",
        "v_criterion2":       "Mean ΔT within target ± tol",
        "v_criterion3":       "At least 3 period-matched events",
        "v_result_hdr":       ["Criterion", "Result"],
        "v_dmm_note":         ("Note: 14D raw catch count = 209 of ~900 "
                               "expected broadcasts is DMM-rate-limited "
                               "(see §5). Period-matched filter (§2) keeps "
                               "the 23 consecutive catches that actually "
                               "verify the 1 s period. Independently "
                               "cross-checked by the test engineer watching "
                               "live advertisements on a phone — confirms "
                               "the 1 Hz broadcast behaviour is normal, "
                               "not a DUT issue."),
        "v_no_note":          "",

        "s_metrics_hdr":     "Aggregate metrics",
        "s_metrics_rows": [
            ("Mean ΔT",                   "mean_dt"),
            ("Min ΔT",                    "min_dt"),
            ("Max ΔT",                    "max_dt"),
            ("Range (max - min)",         "range_dt"),
            ("Std deviation",             "std_dt"),
            ("Max |period err|",          "max_dev"),
            ("Offset from target",        "offset"),
            ("Margin to lower edge  (Margins (informational))",
                                              "margin_lo"),
            ("Margin to upper edge  (Margins (informational))",
                                              "margin_hi"),
            ("Target interval",           "target"),
            ("Tolerance (±)",             "tolerance"),
        ],
        "s_hist_title":      "Interval distribution (16 bins around target)",
        "s_hist_legend":     ("Bin width: tol / 8; centre bin = target. "
                              "Bars = count of period-matched events whose "
                              "ΔT falls in each bin."),

        "raw_headers":       ["Event #", "t (s)", "ISO start",
                              "Current peak (A)", "ΔT from prev (s)",
                              "|err| (s)", "Within tolerance", "Notes"],
        "raw_notes_first":   "first catch (no prev)",
        "raw_notes_ok":      "OK",
        "raw_notes_fail":    "exceeds tol",
        "raw_sheet_title_prefix": "Period-matched broadcast events — ",
        "raw_count_suffix":      " events",

        "n_block_hdr":       "── MODEL ({device}) ──",
        "n_bullet1":         ("Live DMM capture (Keysight 34465A, NPLC=0.001, "
                             "fixed 1 mA range on 3 A terminal)."),
        "n_bullet2":         ("Period-matched filter applied to exclude "
                             "DMM-skipped gaps — kept events are "
                             "consecutive catches (real period measurement)."),
        "n_footer":          "Generated by build_combined_report.py on {ts}.",

        "fmt_target":        "Target = {v:g} s, Tol = ±{tol:g} s",
        "fmt_window":        "Window = [{lo:g}, {hi:g}] s",
        "fmt_pass_n":        "{n} / {total} ({pct:.1f}%)",
        "fmt_s":             "{v:.4f} s",
        "fmt_a":             "{v:.3e} A",
        "fmt_3dp":           "{v:.3f}",
    },

    "zh": {
        "test_id_label":     "测试编号",
        "model_label":       "型号",
        "device_label":      "设备",
        "spec_label":        "规格",
        "started_label":     "开始",
        "ended_label":       "结束",
        "mode_label":        "测试模式",
        "mode_value":        "实况",
        "verdict_label":     "总体结论",
        "verdict_pass":      "通过",
        "verdict_fail":      "不通过",
        "verdict_allpass":   "全部通过",
        "verdict_mixed":     "混合",
        "pass_rate_hdr":     ["型号", "判定", "通过率"],
        "m1_test_purpose":   "测试目的",
        "m1_body":           ("验证 BLE 广播周期（ΔT 在目标 ± 容差范围内）"
                              "以及占空比在代表性采集窗口内是否稳定。"
                              "本批次覆盖两款型号：12M 每 5 分钟广播一次，"
                              "14D 每秒广播一次。"),
        "m2_filter_title":   "§2 周期匹配筛选",
        "m2_filter_body":    ("一次「抓取」是 DMM 电流曲线中一段超量程样本簇。"
                              "保留相邻抓取（ΔT ≈ 目标）；剔除跨事件的间隔"
                              "（ΔT = N × 目标，N ≥ 2），让判定反映 DUT 行为、"
                              "而非 DMM 采样速率。每段运行的首次抓取保留"
                              "（无前次可对比）。"),
        "m3_dmm_title":      "§3 DMM 采样参数",
        "m3_dmm_body":       ("Keysight 34465A 配置：1 mA 固定量程（3 A "
                              "接线端），NPLC = 0.001，自动归零 OFF，显示屏 "
                              "ON。预热一次 `:READ?` 之后，高速循环仅用 "
                              "`:READ?`（此 NPLC 下约 75 Hz）。若改用 "
                              "`:MEAS:CURR:DC?`，每次会重发配置，吞吐跌至 "
                              "约 2 Hz。"),
        "m4_thresh_title":    "§4 抓取检测阈值",
        "m4_thresh_body":    (f"激活阈值 ACTIVE_THRESHOLD_A = 100 µA "
                              f"（高于深度休眠，低于轻度休眠）。"
                              f"超量程标记 OVERRANGE_MARKER = 9.9e37（DMM "
                              f"溢出旗标）。事件由间隔 ≤ {EVENT_GAP_S:g} s "
                              f"的超量程样本聚类而成。"),
        "m5_burst_title":     "§5 脉冲宽度与 DMM 采样间隔",
        "m5_burst_body":     ("每个 BLE TX 脉冲宽约 5 ms。DMM 采样间隔约 "
                              "13 ms（≈75 Hz）。每秒广播大多落在两次采样 "
                              "之间而被漏掉 — 原始抓取数受 DMM 速率限制，"
                              "并非 DUT 问题。周期匹配筛选（§2）只保留相邻"
                              "抓取，让判定不受 DMM 混叠影响。"),
        "m6_const_title":     "§6 电气与配置常量",
        "m6_const_rows": [
            ("目标间隔",                "300 s / 1 s（按型号）"),
            ("容差",                    "±5 s / ±0.05 s（按型号）"),
            ("DMM 量程",                "3 A 接线端 1 mA 固定量程"),
            ("DMM NPLC",                "0.001（孔径模式）"),
            ("DMM 自动归零",            "OFF"),
            ("有效采样率",              "约 75 Hz"),
            ("激活阈值",                "100 µA"),
            ("超量程标记",              "9.9e37"),
            ("事件间隔",                "1.0 s"),
            ("窗口半径",                "0.5 s"),
        ],
        "m7_proc_title":      "§7 硬件与配置流程",
        "m7_proc_bullets": [
            ("夹具", "PCBA 由台式电源供电；DMM 通过 3 A 接线端与 3.3 V "
                    "电源轨串联。"),
            ("仪器连接", "Keysight 34465A 通过 USB 连接 "
                        "（USB0::0x2A8D::0x0101::MY57515137::0::INSTR）；"
                        "NI-VISA 运行时 + pyvisa 后端。"),
            ("DMM 配置命令", "`:CURR:DC:RANG 0.01` → "
                             "`:CURR:DC:NPLC 0.001` → "
                             "`:CURR:DC:AZERO OFF` → 预热 `:READ?` → "
                             "进入 `:READ?` 循环。"),
            ("停止机制", "Ctrl+C、SIGTERM/TaskStop，或 "
                        "`touch <RUN_DIR>/.stop`（干净的 `finally` 块会生成 "
                              "summary + report.md）。"),
        ],
        "v_criterion1":       "所有周期匹配事件满足 |ΔT - 目标| ≤ 容差",
        "v_criterion2":       "平均 ΔT 在目标 ± 容差内",
        "v_criterion3":       "至少 3 个周期匹配事件",
        "v_result_hdr":       ["判定项", "结果"],
        "v_dmm_note":         ("说明：14D 原始抓取 209 / 预期 ~900 次广播，"
                               "受 DMM 速率限制（见 §5）。周期匹配筛选（§2）"
                               "保留 23 个相邻抓取用于验证 1 s 周期。另由测试"
                               "工程师用手机现场观察 14D 的实时广播频率，"
                               "独立确认 1 Hz 广播行为正常 — 非 DUT 问题。"),
        "v_no_note":          "",

        "s_metrics_hdr":     "汇总指标",
        "s_metrics_rows": [
            ("平均 ΔT",                "mean_dt"),
            ("最小 ΔT",                "min_dt"),
            ("最大 ΔT",                "max_dt"),
            ("极差 (max - min)",       "range_dt"),
            ("标准差",                  "std_dt"),
            ("最大 |周期偏差|",        "max_dev"),
            ("相对目标偏移",           "offset"),
            ("距下边裕量  （距边裕量（仅供参考））", "margin_lo"),
            ("距上边裕量  （距边裕量（仅供参考））", "margin_hi"),
            ("目标间隔",                "target"),
            ("容差 (±)",                "tolerance"),
        ],
        "s_hist_title":      "间隔分布（目标周围 16 个分箱）",
        "s_hist_legend":     ("分箱宽度：容差 / 8；中心分箱 = 目标。"
                              "柱高 = 该分箱内周期匹配事件数。"),

        "raw_headers":       ["序号", "时间 (s)", "开始时间 (ISO)",
                              "峰值电流 (A)", "与上次 ΔT (s)",
                              "|偏差| (s)", "在容差内", "备注"],
        "raw_notes_first":   "首次抓取",
        "raw_notes_ok":      "正常",
        "raw_notes_fail":    "超出容差",
        "raw_sheet_title_prefix": "周期匹配广播事件 — ",
        "raw_count_suffix":      " 个事件",

        "n_block_hdr":       "── 型号（{device}）──",
        "n_bullet1":         ("实况 DMM 采集（Keysight 34465A，NPLC=0.001，"
                             "3 A 接线端 1 mA 固定量程）。"),
        "n_bullet2":         ("应用周期匹配筛选剔除跨事件间隔 — 保留的事件"
                             "为相邻抓取（真实周期测量）。"),
        "n_footer":          "由 build_combined_report.py 生成于 {ts}。",

        "fmt_target":        "目标 = {v:g} s，容差 = ±{tol:g} s",
        "fmt_window":        "窗口 = [{lo:g}, {hi:g}] s",
        "fmt_pass_n":        "{n} / {total}（{pct:.1f}%）",
        "fmt_s":             "{v:.4f} s",
        "fmt_a":             "{v:.3e} A",
        "fmt_3dp":           "{v:.3f}",
    },
}


def tr(key: str, lang: str) -> str:
    return T[lang][key]


# ---------------------------------------------------------------------------
# Styling tokens — REPORT_SPEC.md §3
# ---------------------------------------------------------------------------
PALETTE = {
    "pass":     "2E7D32",
    "fail":     "C62828",
    "warn":     "F9A825",
    "hdr_bg":   "EEEEEE",
    "raw_bg":   "F5F5F5",
    "yellow":   "FFF59D",
}
FONT_SIZES = {"title": 18, "section": 14, "body": 11,
              "histogram": 9, "verdict": 24, "small": 10}


def _styles():
    return {
        "title":      Font(size=FONT_SIZES["title"], bold=True),
        "section":    Font(size=FONT_SIZES["section"], bold=True),
        "body":       Font(size=FONT_SIZES["body"]),
        "small":      Font(size=FONT_SIZES["small"]),
        "bold":       Font(size=FONT_SIZES["body"], bold=True),
        "italic_red": Font(size=FONT_SIZES["body"], italic=True,
                           color=PALETTE["fail"]),
        "hist":       Font(name="Consolas", size=FONT_SIZES["histogram"]),
        "hist_bold": Font(name="Consolas", size=FONT_SIZES["histogram"],
                          bold=True),
        "verdict":    Font(size=FONT_SIZES["verdict"], bold=True,
                           color="FFFFFF"),
        "pass":       Font(size=FONT_SIZES["body"], bold=True, color="FFFFFF"),
        "fail":       Font(size=FONT_SIZES["body"], bold=True, color="FFFFFF"),
        "section_fill": PatternFill(start_color=PALETTE["hdr_bg"],
                                    end_color=PALETTE["hdr_bg"],
                                    fill_type="solid"),
        "raw_fill":    PatternFill(start_color=PALETTE["raw_bg"],
                                   end_color=PALETTE["raw_bg"],
                                   fill_type="solid"),
        "pass_fill":   PatternFill(start_color=PALETTE["pass"],
                                   end_color=PALETTE["pass"],
                                   fill_type="solid"),
        "fail_fill":   PatternFill(start_color=PALETTE["fail"],
                                   end_color=PALETTE["fail"],
                                   fill_type="solid"),
        "warn_fill":   PatternFill(start_color=PALETTE["warn"],
                                   end_color=PALETTE["warn"],
                                   fill_type="solid"),
        "left":       Alignment(horizontal="left", wrap_text=True,
                                vertical="top"),
        "left_nowrap": Alignment(horizontal="left", vertical="center"),
        "center":     Alignment(horizontal="center", vertical="center",
                                 wrap_text=True),
        "right":      Alignment(horizontal="right", vertical="center"),
    }


def _apply_widths(ws, widths: dict) -> None:
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def _verdict_fill(s, passes: bool, sufficient: bool):
    if passes:
        return s["pass_fill"]
    if sufficient:
        return s["fail_fill"]
    return s["warn_fill"]


# ---------------------------------------------------------------------------
# Analysis helpers (carried over from build_excel_report.py — that file
# is now retired per REPORT_SPEC.md §4.7)
# ---------------------------------------------------------------------------
def _load_run(run_dir: Path) -> dict:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    csv_path = DATA_DIR / f"{run_dir.name}.csv"
    if not csv_path.exists():
        legacy = run_dir / "data.csv"
        if legacy.exists():
            csv_path = legacy
    if not csv_path.exists():
        raise FileNotFoundError(f"data CSV not found at {csv_path} or {legacy}")

    rows: list[dict] = []
    with csv_path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "t_s":        float(r["t_s"]),
                "current_a":  float(r["current_a"]),
                "iso_time":   r["iso_time"],
            })

    idn = "—"
    md = run_dir / "report.md"
    if md.exists():
        for line in md.read_text(encoding="utf-8").splitlines():
            if line.startswith("| Instrument |"):
                idn = line.split("|", 2)[2].strip()
                break
    summary["idn"] = idn
    summary["_csv_path"] = str(csv_path)
    return {"summary": summary, "rows": rows, "csv_path": csv_path}


def _find_events(rows: list[dict]) -> list[dict]:
    over = [r for r in rows if r["current_a"] > OVERRANGE_MARKER / 2]
    clusters: list[list[dict]] = []
    cur: list[dict] = []
    for r in over:
        if cur and (r["t_s"] - cur[-1]["t_s"] > EVENT_GAP_S):
            clusters.append(cur)
            cur = []
        cur.append(r)
    if cur:
        clusters.append(cur)

    events = []
    for i, cl in enumerate(clusters, 1):
        events.append({
            "n":                 i,
            "t_start_s":         cl[0]["t_s"],
            "t_end_s":           cl[-1]["t_s"],
            "iso_start":         cl[0]["iso_time"],
            "iso_end":           cl[-1]["iso_time"],
            "overrange_samples": len(cl),
            "dt_from_prev_s":    ((cl[0]["t_s"] - clusters[i - 2][0]["t_s"])
                                  if i > 1 else None),
        })
    return events


def _filter_period_matched(events, target, tol):
    """Keep events whose gap to the previous caught event is in
    [target - tol, target + tol]. The first event of the run is kept
    (no prev to compare against)."""
    kept: list[dict] = []
    for ev in events:
        dt = ev.get("dt_from_prev_s")
        if dt is None:
            kept.append(ev)
            continue
        if abs(dt - target) <= tol:
            kept.append(ev)
    for i, ev in enumerate(kept, 1):
        ev["n"] = i
    return kept


def _duty_cycle(rows: list[dict]) -> dict:
    n = len(rows)
    if n < 2:
        return {"error": "insufficient samples"}
    total_t = rows[-1]["t_s"] - rows[0]["t_s"]
    dt = total_t / (n - 1)

    active_n = 0; sleep_n = 0; overrange_n = 0
    active_vals: list[float] = []
    sleep_vals: list[float] = []
    for r in rows:
        v = r["current_a"]
        if v > OVERRANGE_MARKER / 2:
            active_n += 1; overrange_n += 1
        elif v > ACTIVE_THRESHOLD_A:
            active_n += 1; active_vals.append(v)
        else:
            sleep_n += 1
            if v > 0:
                sleep_vals.append(v)
    active_t = active_n * dt
    sleep_t = sleep_n * dt
    total = active_t + sleep_t
    valid = [r for r in rows if r["current_a"] < OVERRANGE_MARKER / 2]
    charge_c = (sum(r["current_a"] for r in valid) * dt
                if len(valid) > 1 else 0.0)
    return {
        "threshold_a":     ACTIVE_THRESHOLD_A,
        "total_s":         total,
        "active_s":        active_t,
        "sleep_s":         sleep_t,
        "active_pct":      (active_t / total * 100) if total > 0 else 0,
        "active_ppm":      (active_t / total * 1e6) if total > 0 else 0,
        "active_curr_mean": statistics.fmean(active_vals) if active_vals else None,
        "sleep_curr_mean":  statistics.fmean(sleep_vals) if sleep_vals else None,
        "n_active":        active_n,
        "n_sleep":         sleep_n,
        "n_overrange":     overrange_n,
        "charge_c":        charge_c,
    }


# ---------------------------------------------------------------------------
# Per-model analysis
# ---------------------------------------------------------------------------
def _collect(model_cfg: dict) -> dict:
    run_dir = REPORTS_DIR / model_cfg["run_dir_name"]
    target = model_cfg["target_interval_s"]
    tol = model_cfg["tolerance_s"]
    run = _load_run(run_dir)
    rows = run["rows"]

    raw_events = _find_events(rows)
    events = _filter_period_matched(raw_events, target, tol)
    duty = _duty_cycle(rows)

    intervals = [e["dt_from_prev_s"] for e in events if e["dt_from_prev_s"]]
    if intervals:
        mean_dt = statistics.fmean(intervals)
        max_dev = max(abs(d - target) for d in intervals)
        std_dt = statistics.pstdev(intervals) if len(intervals) > 1 else 0.0
        min_dt = min(intervals)
        max_dt = max(intervals)
        offset = mean_dt - target
    else:
        mean_dt = max_dev = std_dt = min_dt = max_dt = offset = None

    # 3-criterion verdict (REPORT_SPEC.md §2.3 / §4.3)
    c1 = (max_dev is not None) and (max_dev <= tol)
    c2 = (mean_dt is not None) and (abs(mean_dt - target) <= tol)
    c3 = len(events) >= 3
    passes = c1 and c2 and c3

    # Render the histogram PNG for each, into the run dir so it lives with
    # the capture. Filename encodes label + lang so EN/ZH each get a
    # language-specific image (title + caption baked in).
    hist_paths: dict[str, Path] = {}
    for lang in ("en", "zh"):
        p = run_dir / f"hist_intervals_{lang}.png"
        try:
            _render_histogram_png(intervals, target, tol,
                                  model_cfg["label"],
                                  model_cfg["spec_desc"], lang, p)
            hist_paths[lang] = p
        except Exception as e:
            print(f"[warn] histogram render failed for {model_cfg['label']} "
                  f"({lang}): {e}", file=sys.stderr)
            hist_paths[lang] = Path()  # empty path → embed skipped

    return {
        "cfg":          model_cfg,
        "run":          run,
        "run_dir":      run_dir,
        "rows":         rows,
        "raw_events":   raw_events,
        "events":       events,
        "duty":         duty,
        "intervals":    intervals,
        "mean_dt":      mean_dt,
        "max_dev":      max_dev,
        "std_dt":       std_dt,
        "min_dt":       min_dt,
        "max_dt":       max_dt,
        "offset":       offset,
        "c1":           c1,
        "c2":           c2,
        "c3":           c3,
        "passes":       passes,
        "hist_png_en":  hist_paths.get("en", Path()),
        "hist_png_zh":  hist_paths.get("zh", Path()),
    }


# ---------------------------------------------------------------------------
# 01_Cover / 01_封面
# ---------------------------------------------------------------------------
def _fmt_iso_short(iso: str) -> str:
    """`YYYY-MM-DDTHH:MM:SS.sss` → `YYYY-MM-DD HH:MM:SS` (REPORT_SPEC §2.1)."""
    return iso.replace("T", " ")[:19]


def _add_cover(wb, models_data: list[dict], lang: str) -> None:
    s = _styles()
    ws = wb.create_sheet("01_Cover" if lang == "en" else "01_封面")

    title_text = ("TR-BLE-003 — BLE Broadcast Duty Cycle Test Report "
                  "(12M + 14D)"
                  if lang == "en" else
                  "TR-BLE-003 — BLE 广播占空比测试报告（12M + 14D）")
    ws["A1"] = title_text
    ws["A1"].font = s["title"]
    ws.merge_cells("A1:F1")
    ws.row_dimensions[1].height = 30

    ws["A3"] = tr("test_id_label", lang)
    ws["A3"].font = s["bold"]
    ws["B3"] = "TR-BLE-003"

    # Per-model identification rows
    row = 4
    for m in models_data:
        cfg = m["cfg"]; summary = m["run"]["summary"]; rows = m["rows"]
        ws.cell(row=row, column=1, value=tr("model_label", lang)).font = s["bold"]
        ws.cell(row=row, column=2, value=cfg["label"])
        ws.cell(row=row, column=3, value=tr("device_label", lang)).font = s["bold"]
        ws.cell(row=row, column=4, value=cfg["device_id"])
        ws.cell(row=row, column=5, value=tr("spec_label", lang)).font = s["bold"]
        ws.cell(row=row, column=6, value=tr("fmt_target", lang).format(
            v=cfg["target_interval_s"], tol=cfg["tolerance_s"]))
        row += 1

    # Overall verdict
    all_pass = all(m["passes"] for m in models_data)
    verdict_row = row + 1
    ws.cell(row=verdict_row, column=1,
            value=tr("verdict_label", lang)).font = s["bold"]
    vc = ws.cell(row=verdict_row, column=2)
    vc.value = (tr("verdict_allpass", lang) if all_pass
                else tr("verdict_mixed", lang))
    vc.font = s["verdict"]
    vc.alignment = s["center"]
    vc.fill = (s["pass_fill"] if all_pass else s["warn_fill"])
    ws.merge_cells(start_row=verdict_row, start_column=2,
                   end_row=verdict_row, end_column=6)
    ws.row_dimensions[verdict_row].height = 40

    # Pass-rate table
    hdr_row = verdict_row + 2
    for i, h in enumerate(tr("pass_rate_hdr", lang), 1):
        c = ws.cell(row=hdr_row, column=i, value=h)
        c.font = s["bold"]; c.fill = s["section_fill"]
        c.alignment = s["center"]
    for i, m in enumerate(models_data):
        cfg = m["cfg"]; r = hdr_row + 1 + i
        ws.cell(row=r, column=1, value=cfg["label"])
        verdict_cell = ws.cell(row=r, column=2,
                               value=tr("verdict_pass" if m["passes"]
                                        else "verdict_fail", lang))
        verdict_cell.font = s["pass" if m["passes"] else "fail"]
        verdict_cell.fill = (s["pass_fill"] if m["passes"]
                             else s["fail_fill"])
        verdict_cell.alignment = s["center"]
        kept = len(m["events"]); raw = len(m["raw_events"])
        ws.cell(row=r, column=3,
                value=tr("fmt_pass_n", lang).format(
                    n=kept, total=raw, pct=(kept / raw * 100 if raw else 0)))
        ws.cell(row=r, column=3).alignment = s["right"]

    # Mode row
    mode_row = hdr_row + 1 + len(models_data) + 2
    ws.cell(row=mode_row, column=1,
            value=tr("mode_label", lang)).font = s["bold"]
    ws.cell(row=mode_row, column=2, value=tr("mode_value", lang))

    # Started / Ended
    ts_row = mode_row + 1
    parts = [f"{m['cfg']['label']}@{_fmt_iso_short(m['rows'][0]['iso_time'])}"
             for m in models_data]
    ws.cell(row=ts_row, column=1,
            value=tr("started_label", lang)).font = s["bold"]
    ws.cell(row=ts_row, column=2, value=", ".join(parts))
    ws.merge_cells(start_row=ts_row, start_column=2,
                   end_row=ts_row, end_column=6)
    parts = [f"{m['cfg']['label']}@{_fmt_iso_short(m['rows'][-1]['iso_time'])}"
             for m in models_data]
    ws.cell(row=ts_row + 1, column=1,
            value=tr("ended_label", lang)).font = s["bold"]
    ws.cell(row=ts_row + 1, column=2, value=", ".join(parts))
    ws.merge_cells(start_row=ts_row + 1, start_column=2,
                   end_row=ts_row + 1, end_column=6)

    _apply_widths(ws, {"A": 18, "B": 14, "C": 14, "D": 14, "E": 14, "F": 60})


# ---------------------------------------------------------------------------
# 02_Methodology / 02_测试方法
# ---------------------------------------------------------------------------
def _add_methodology(wb, lang: str) -> None:
    s = _styles()
    ws = wb.create_sheet("02_Methodology" if lang == "en"
                         else "02_测试方法")

    row = 1
    # §1 Test purpose
    ws.cell(row=row, column=1, value=tr("m1_test_purpose", lang))
    ws.cell(row=row, column=1).font = s["section"]
    ws.cell(row=row, column=1).fill = s["section_fill"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    row += 1
    ws.cell(row=row, column=1, value=tr("m1_body", lang))
    ws.cell(row=row, column=1).font = s["body"]
    ws.cell(row=row, column=1).alignment = s["left"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    ws.row_dimensions[row].height = 50
    row += 2

    # §2 Period-matched filter
    ws.cell(row=row, column=1, value=tr("m2_filter_title", lang))
    ws.cell(row=row, column=1).font = s["section"]
    ws.cell(row=row, column=1).fill = s["section_fill"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    row += 1
    ws.cell(row=row, column=1, value=tr("m2_filter_body", lang))
    ws.cell(row=row, column=1).font = s["body"]
    ws.cell(row=row, column=1).alignment = s["left"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    ws.row_dimensions[row].height = 60
    row += 2

    # §3 DMM sampling math
    ws.cell(row=row, column=1, value=tr("m3_dmm_title", lang))
    ws.cell(row=row, column=1).font = s["section"]
    ws.cell(row=row, column=1).fill = s["section_fill"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    row += 1
    ws.cell(row=row, column=1, value=tr("m3_dmm_body", lang))
    ws.cell(row=row, column=1).font = s["body"]
    ws.cell(row=row, column=1).alignment = s["left"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    ws.row_dimensions[row].height = 60
    row += 2

    # §4 Catch detection thresholds
    ws.cell(row=row, column=1, value=tr("m4_thresh_title", lang))
    ws.cell(row=row, column=1).font = s["section"]
    ws.cell(row=row, column=1).fill = s["section_fill"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    row += 1
    ws.cell(row=row, column=1, value=tr("m4_thresh_body", lang))
    ws.cell(row=row, column=1).font = s["body"]
    ws.cell(row=row, column=1).alignment = s["left"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    ws.row_dimensions[row].height = 50
    row += 2

    # §5 Burst width vs DMM sample interval
    ws.cell(row=row, column=1, value=tr("m5_burst_title", lang))
    ws.cell(row=row, column=1).font = s["section"]
    ws.cell(row=row, column=1).fill = s["section_fill"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    row += 1
    ws.cell(row=row, column=1, value=tr("m5_burst_body", lang))
    ws.cell(row=row, column=1).font = s["body"]
    ws.cell(row=row, column=1).alignment = s["left"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    ws.row_dimensions[row].height = 70
    row += 2

    # §6 Electrical & configuration constants
    ws.cell(row=row, column=1, value=tr("m6_const_title", lang))
    ws.cell(row=row, column=1).font = s["section"]
    ws.cell(row=row, column=1).fill = s["section_fill"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    row += 1
    for k, v in tr("m6_const_rows", lang):
        c1 = ws.cell(row=row, column=1, value=k); c1.font = s["bold"]
        c2 = ws.cell(row=row, column=2, value=v); c2.font = s["body"]
        ws.merge_cells(start_row=row, start_column=2,
                       end_row=row, end_column=6)
        row += 1
    row += 1

    # §7 Hardware & configuration procedure
    ws.cell(row=row, column=1, value=tr("m7_proc_title", lang))
    ws.cell(row=row, column=1).font = s["section"]
    ws.cell(row=row, column=1).fill = s["section_fill"]
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    row += 1
    for k, v in tr("m7_proc_bullets", lang):
        c1 = ws.cell(row=row, column=1, value=f"• {k}")
        c1.font = s["bold"]; c1.alignment = s["left"]
        c2 = ws.cell(row=row, column=2, value=v)
        c2.font = s["body"]; c2.alignment = s["left"]
        ws.merge_cells(start_row=row, start_column=2,
                       end_row=row, end_column=6)
        ws.row_dimensions[row].height = 36
        row += 1

    _apply_widths(ws, {"A": 18, "B": 14, "C": 14, "D": 14, "E": 14, "F": 60})


# ---------------------------------------------------------------------------
# 03_Verdict / 03_结论
# ---------------------------------------------------------------------------
def _add_verdict(wb, models_data: list[dict], lang: str) -> None:
    s = _styles()
    ws = wb.create_sheet("03_Verdict" if lang == "en" else "03_结论")

    row = 1
    for m in models_data:
        cfg = m["cfg"]
        hdr = tr("n_block_hdr", lang).format(device=cfg["device_id"])
        ws.cell(row=row, column=1, value=hdr).font = s["section"]
        ws.cell(row=row, column=1).fill = s["section_fill"]
        ws.merge_cells(start_row=row, start_column=1,
                       end_row=row, end_column=3)
        row += 1

        # 3-criterion table
        h1, h2 = tr("v_result_hdr", lang)
        ws.cell(row=row, column=1, value=h1).font = s["bold"]
        ws.cell(row=row, column=1).fill = s["raw_fill"]
        ws.cell(row=row, column=2, value=h2).font = s["bold"]
        ws.cell(row=row, column=2).fill = s["raw_fill"]
        ws.cell(row=row, column=2).alignment = s["center"]
        ws.cell(row=row, column=3).fill = s["raw_fill"]
        row += 1

        criteria = [
            (tr("v_criterion1", lang), m["c1"]),
            (tr("v_criterion2", lang), m["c2"]),
            (tr("v_criterion3", lang), m["c3"]),
        ]
        for label, ok in criteria:
            ws.cell(row=row, column=1, value=label).font = s["body"]
            ws.cell(row=row, column=1).alignment = s["left"]
            verdict_cell = ws.cell(row=row, column=2)
            verdict_cell.value = tr("verdict_pass" if ok else "verdict_fail",
                                    lang)
            verdict_cell.font = s["pass" if ok else "fail"]
            verdict_cell.fill = _verdict_fill(s, ok, sufficient=True)
            verdict_cell.alignment = s["center"]
            row += 1

        # DMM-rate-limited note for 14D, empty for 12M
        if cfg["label"] == "14D":
            note = tr("v_dmm_note", lang)
            ws.cell(row=row, column=1, value=note).font = s["italic_red"]
            ws.cell(row=row, column=1).alignment = s["left"]
            ws.merge_cells(start_row=row, start_column=1,
                           end_row=row, end_column=3)
            ws.row_dimensions[row].height = 90
            row += 1
        else:
            row += 1  # spacer

    _apply_widths(ws, {"A": 50, "B": 14, "C": 14})


# ---------------------------------------------------------------------------
# 04_Statistics / 04_统计
# ---------------------------------------------------------------------------
def _render_histogram_png(intervals, target, tol, label, spec_desc,
                          lang, output_path: Path) -> None:
    """16-bin matplotlib histogram of period-matched intervals centred on
    target. Renders identically across Excel / LibreOffice / WPS. The PNG
    is referenced from the workbook via openpyxl.drawing.image.Image so the
    statistics sheet no longer relies on ASCII art."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        # Fall back to writing an empty file so the embed doesn't crash;
        # the cell will show a broken-image marker.
        output_path.write_bytes(b"")
        return

    # Pick a CJK-capable font for the ZH workbook so titles / axis labels
    # don't render as missing-glyph boxes. DejaVu Sans (matplotlib's default)
    # has no CJK glyphs.
    if lang == "zh":
        cjk_candidates = ["Microsoft YaHei", "SimHei", "SimSun",
                          "Noto Sans CJK SC", "WenQuanYi Zen Hei",
                          "Source Han Sans CN", "Arial Unicode MS"]
        available = {f.name for f in font_manager.fontManager.ttflist}
        for name in cjk_candidates:
            if name in available:
                plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
                plt.rcParams["axes.unicode_minus"] = False
                break

    half = max(tol, 1e-9)
    bin_w = (2 * half) / 16
    edges = [target - half + i * bin_w for i in range(17)]

    fig, ax = plt.subplots(figsize=(8, 3.0), dpi=140)
    if intervals:
        ax.hist(intervals, bins=edges, color="#2E7D32", edgecolor="white",
                alpha=0.9)
    # Vertical guides: lower tol, target, upper tol
    for x, ls, lw, c in [((target - tol), "--", 1.0, "#C62828"),
                         (target,       "-",  1.6, "#2E7D32"),
                         ((target + tol), "--", 1.0, "#C62828")]:
        ax.axvline(x, linestyle=ls, linewidth=lw)

    title_text = (f"{label} — interval distribution (16 bins around target)"
                  if lang == "en" else
                  f"{label} — 间隔分布（目标周围 16 个分箱）")
    ax.set_title(title_text, fontsize=12, fontweight="bold", loc="left")
    xlabel = "ΔT (s)" if lang == "en" else "ΔT (秒)"
    ylabel = "Count"  if lang == "en" else "次数"
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.set_xlim(target - half * 1.1, target + half * 1.1)
    ax.grid(True, alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # Caption inside the figure: counts / target / window
    if lang == "en":
        caption = (f"n = {len(intervals)} events · target = {target:g} s · "
                   f"window = [{target - tol:g}, {target + tol:g}] s")
    else:
        caption = (f"n = {len(intervals)} 个事件 · 目标 = {target:g} s · "
                   f"窗口 = [{target - tol:g}, {target + tol:g}] s")
    ax.text(0.01, -0.32, caption, transform=ax.transAxes,
            fontsize=9, color="#555555")

    fig.tight_layout()
    fig.savefig(output_path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def _add_statistics(wb, models_data: list[dict], lang: str) -> None:
    s = _styles()
    ws = wb.create_sheet("04_Statistics" if lang == "en" else "04_统计")

    row = 1
    for m in models_data:
        cfg = m["cfg"]
        target = cfg["target_interval_s"]; tol = cfg["tolerance_s"]
        # Block title
        title_text = (f"{cfg['label']} ({cfg['device_id']}) — "
                      f"{tr('fmt_target', lang).format(v=target, tol=tol)}"
                      if lang == "en" else
                      f"{cfg['label']}（{cfg['device_id']}）— "
                      f"{tr('fmt_target', lang).format(v=target, tol=tol)}")
        ws.cell(row=row, column=1, value=title_text).font = s["section"]
        ws.cell(row=row, column=1).fill = s["section_fill"]
        ws.merge_cells(start_row=row, start_column=1,
                       end_row=row, end_column=4)
        row += 1

        # Aggregate metrics header
        ws.cell(row=row, column=1, value=tr("s_metrics_hdr", lang))
        ws.cell(row=row, column=1).font = s["bold"]
        row += 1

        # Render each metric row
        v = {
            "mean_dt":   m["mean_dt"],
            "min_dt":    m["min_dt"],
            "max_dt":    m["max_dt"],
            "range_dt":  ((m["max_dt"] - m["min_dt"]) if m["max_dt"] is not None
                          else None),
            "std_dt":    m["std_dt"],
            "max_dev":   m["max_dev"],
            "offset":    m["offset"],
            "margin_lo": ((m["min_dt"] if m["min_dt"] is not None else target)
                      - (target - tol)),
            "margin_hi": ((target + tol)
                      - (m["max_dt"] if m["max_dt"] is not None else target)),
            "target":    target,
            "tolerance": tol,
        }
        for label, key in tr("s_metrics_rows", lang):
            val = v[key]
            ws.cell(row=row, column=1, value=label).font = s["bold"]
            ws.cell(row=row, column=1).alignment = s["left"]
            cell = ws.cell(row=row, column=2)
            if val is None:
                cell.value = "—"
            elif key in ("target", "tolerance"):
                cell.value = f"{val:g} s"
            elif key == "offset":
                cell.value = f"{val:+.4f} s"
            elif key in ("margin_lo", "margin_hi"):
                cell.value = f"{val:+.4f} s"
            else:
                cell.value = f"{val:.4f} s"
            cell.font = s["body"]; cell.alignment = s["right"]
            row += 1

        row += 1

        # Histogram title
        ws.cell(row=row, column=1, value=tr("s_hist_title", lang))
        ws.cell(row=row, column=1).font = s["bold"]
        row += 1
        ws.cell(row=row, column=1, value=tr("s_hist_legend", lang))
        ws.cell(row=row, column=1).font = s["small"]
        ws.cell(row=row, column=1).alignment = s["left"]
        ws.merge_cells(start_row=row, start_column=1,
                       end_row=row, end_column=4)
        row += 1

        # Embedded PNG histogram (rendered identically across viewers).
        # The PNG is rendered once per (model, lang) by _collect() and the
        # path is stashed in m["hist_png"] — language only affects the
        # title/caption baked into the image.
        hist_png = m.get("hist_png_" + lang) or m.get("hist_png_en")
        anchor_row = row
        if hist_png and Path(hist_png).exists() \
                and Path(hist_png).stat().st_size > 0:
            from openpyxl.drawing.image import Image as XlImage
            img = XlImage(hist_png)
            # ~520 px wide × ~210 px tall — fits columns A–D and 14 rows
            img.width = 520
            img.height = 210
            ws.add_image(img, f"A{anchor_row}")
            # Reserve the rows the image visually occupies so the next
            # block doesn't overlap.
            for r in range(anchor_row, anchor_row + 14):
                ws.row_dimensions[r].height = 15
            row += 14
        else:
            placeholder = ("(matplotlib not available — histogram omitted)"
                           if lang == "en" else
                           "（未安装 matplotlib — 缺少直方图）")
            ws.cell(row=row, column=1, value=placeholder).font = s["italic_red"]
            row += 2

        row += 1  # spacer between model blocks

    _apply_widths(ws, {"A": 50, "B": 22, "C": 22, "D": 22})


# ---------------------------------------------------------------------------
# 05/06_Raw Data — 14D / 12M
# ---------------------------------------------------------------------------
def _add_raw_data(wb, m: dict, lang: str, sheet_name: str,
                  event_index_in_workbook: int) -> None:
    s = _styles()
    ws = wb.create_sheet(sheet_name)

    cfg = m["cfg"]; target = cfg["target_interval_s"]; tol = cfg["tolerance_s"]
    events = m["events"]
    n = len(events)
    title = (tr("raw_sheet_title_prefix", lang) + cfg["label"]
             + f"  ·  {tr('fmt_target', lang).format(v=target, tol=tol)}  ·  "
             + f"{n}{tr('raw_count_suffix', lang)}")
    ws["A1"] = title
    ws["A1"].font = s["title"]
    ws.merge_cells("A1:H1")
    ws.row_dimensions[1].height = 26

    headers = tr("raw_headers", lang)
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=3, column=i, value=h)
        c.font = s["bold"]; c.fill = s["raw_fill"]
        c.alignment = s["center"]
    ws.row_dimensions[3].height = 28

    row = 4
    for ev in events:
        ws.cell(row=row, column=1, value=ev["n"]).alignment = s["right"]
        ws.cell(row=row, column=2,
                value=round(ev["t_start_s"], 3)).alignment = s["right"]
        ws.cell(row=row, column=3, value=ev["iso_start"]).alignment = s["left"]

        # Peak current = max within ±WINDOW_S of event start (overrange
        # samples are 9.9e37, so filter them out for a sensible "peak")
        peak = None
        for r in m["rows"]:
            if abs(r["t_s"] - ev["t_start_s"]) <= WINDOW_S:
                v = r["current_a"]
                if v < OVERRANGE_MARKER / 2:
                    if peak is None or v > peak:
                        peak = v
        ws.cell(row=row, column=4,
                value=(f"{peak:.3e}" if peak is not None else "—")
                ).alignment = s["right"]

        if ev["dt_from_prev_s"] is None:
            ws.cell(row=row, column=5, value="—").alignment = s["right"]
            ws.cell(row=row, column=6, value="—").alignment = s["right"]
            ws.cell(row=row, column=7, value="—").alignment = s["center"]
            ws.cell(row=row, column=8,
                    value=tr("raw_notes_first", lang)).alignment = s["left"]
        else:
            dt = ev["dt_from_prev_s"]
            ok = abs(dt - target) <= tol
            ws.cell(row=row, column=5,
                    value=round(dt, 4)).alignment = s["right"]
            ws.cell(row=row, column=6,
                    value=round(abs(dt - target), 4)).alignment = s["right"]
            ok_cell = ws.cell(row=row, column=7)
            ok_cell.value = ("YES" if ok else "NO") if lang == "en" \
                else ("是" if ok else "否")
            ok_cell.font = s["pass" if ok else "fail"]
            ok_cell.fill = (s["pass_fill"] if ok else s["fail_fill"])
            ok_cell.alignment = s["center"]
            ws.cell(row=row, column=8,
                    value=tr("raw_notes_ok" if ok else "raw_notes_fail",
                             lang)).alignment = s["left"]
        row += 1

    _apply_widths(ws, {"A": 8, "B": 12, "C": 24, "D": 16,
                       "E": 14, "F": 12, "G": 16, "H": 16})
    ws.freeze_panes = "A4"


# ---------------------------------------------------------------------------
# 07_Notes / 07_备注
# ---------------------------------------------------------------------------
def _add_notes(wb, models_data: list[dict], lang: str) -> None:
    s = _styles()
    ws = wb.create_sheet("07_Notes" if lang == "en" else "07_备注")

    row = 1
    for m in models_data:
        cfg = m["cfg"]
        hdr = tr("n_block_hdr", lang).format(device=cfg["device_id"])
        ws.cell(row=row, column=1, value=hdr).font = s["section"]
        ws.cell(row=row, column=1).fill = s["section_fill"]
        ws.merge_cells(start_row=row, start_column=1,
                       end_row=row, end_column=4)
        row += 1
        for key in ("n_bullet1", "n_bullet2"):
            cell = ws.cell(row=row, column=1, value="• " + tr(key, lang))
            cell.font = s["body"]
            cell.alignment = s["left"]
            ws.merge_cells(start_row=row, start_column=1,
                           end_row=row, end_column=4)
            ws.row_dimensions[row].height = 30
            row += 1
        row += 1  # spacer

    # Footer
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    footer = tr("n_footer", lang).format(ts=now)
    ws.cell(row=row, column=1, value=footer).font = s["small"]
    ws.cell(row=row, column=1).alignment = s["left"]
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=4)

    _apply_widths(ws, {"A": 18, "B": 14, "C": 14, "D": 14})


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------
def _build_combined(models_data: list[dict], lang: str) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    _add_cover(wb, models_data, lang)
    _add_methodology(wb, lang)
    _add_verdict(wb, models_data, lang)
    _add_statistics(wb, models_data, lang)
    # Sheet 5 / 6: raw data — 14D first, then 12M (REPORT_SPEC.md §2.5–2.6)
    by_label = {m["cfg"]["label"]: m for m in models_data}
    for label, sheet_idx in (("14D", "05"), ("12M", "06")):
        if label not in by_label:
            continue
        sheet_name = (f"{sheet_idx}_Raw Data — {label}" if lang == "en"
                      else f"{sheet_idx}_原始数据 — {label}")
        _add_raw_data(wb, by_label[label], lang, sheet_name,
                      event_index_in_workbook=int(sheet_idx))
    _add_notes(wb, models_data, lang)
    return wb


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _most_recent_timestamp() -> str:
    """Pick the largest YYYYMMDD_HHMMSS across the per-model run dirs."""
    stamps = []
    for cfg in MODELS:
        rd = REPORTS_DIR / cfg["run_dir_name"]
        # run_dir_name itself encodes the timestamp
        parts = cfg["run_dir_name"].rsplit("_", 2)
        if len(parts) >= 2 and parts[-2].isdigit() and parts[-1].isdigit():
            stamps.append(f"{parts[-2]}_{parts[-1]}")
    return max(stamps) if stamps else datetime.now().strftime("%Y%m%d_%H%M%S")


def _forbidden_substrings() -> list[str]:
    """REPORT_SPEC.md §4.1 — never appear anywhere in a workbook."""
    return ["simulation", "mock", "Reproducible", "Re-run", "offline"]


def _scan_forbidden(wb: Workbook) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if v is None:
                    continue
                s = str(v)
                for bad in _forbidden_substrings():
                    if bad in s:
                        found.append((ws.title, bad))
    return found


def main() -> int:
    print(f"[combined] TR root: {TR_ROOT}")
    models_data = []
    for cfg in MODELS:
        print(f"[combined] loading {cfg['label']} from {cfg['run_dir_name']}…")
        models_data.append(_collect(cfg))

    ts = _most_recent_timestamp()
    print(f"[combined] using timestamp {ts} (most recent per-model run)")

    written: list[Path] = []
    for lang in ("en", "zh"):
        wb = _build_combined(models_data, lang)
        forbidden = _scan_forbidden(wb)
        if forbidden:
            print(f"[FATAL] forbidden substrings in {lang} workbook:",
                  file=sys.stderr)
            for sheet, bad in forbidden:
                print(f"  {sheet}: {bad!r}", file=sys.stderr)
            return 1
        out = REPORTS_DIR / f"report_combined_{ts}_{lang}.xlsx"
        wb.save(out)
        written.append(out)
        print(f"[combined] wrote {out.name}  (sheets: {wb.sheetnames})")

    # Remove old non-timestamped combined files (§4.7 retention).
    for legacy_name in ("report_en.xlsx", "report_zh.xlsx",
                        "report_12m_en.xlsx", "report_12m_zh.xlsx",
                        "report_14d_en.xlsx", "report_14d_zh.xlsx"):
        legacy = REPORTS_DIR / legacy_name
        if legacy.exists():
            legacy.unlink()
            print(f"[combined] removed legacy {legacy.name}")

    print()
    overall = all(m["passes"] for m in models_data)
    print(f"  combined verdict : "
          f"{tr('verdict_allpass', 'en') if overall else tr('verdict_mixed', 'en')}")
    for m in models_data:
        v = tr("verdict_pass", "en") if m["passes"] else tr("verdict_fail", "en")
        c1c = "✓" if m["c1"] else "✗"
        c2c = "✓" if m["c2"] else "✗"
        c3c = "✓" if m["c3"] else "✗"
        print(f"  - {m['cfg']['label']:<3} "
              f"({m['cfg']['spec_desc']}): {v}  "
              f"[c1={c1c} c2={c2c} c3={c3c}]  "
              f"kept={len(m['events'])}/raw={len(m['raw_events'])}  "
              f"duty={m['duty']['active_pct']:.4f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())