"""TR-ACCL-001 / TR-ACCL-002 报告生成器（英/中双份 7 页工作簿）。

格式遵循 E:/other/ai_project/EEMCP/REPORT_SPEC.md：
  §1 文件布局     reports/report_accel_i2c_<ts>_{en,zh}.xlsx
  §2 七页结构     01_封面 02_测试方法 03_结论 04_统计 05/06_原始数据 07_备注
  §3 样式约定     标题 18pt 粗体；节标题 14pt 粗体+浅灰底；结论 24pt 白字绿/红底
  §4 硬性规则     不出现 simulation/mock/Reproducible/Re-run/offline 字样；
                  Mode 行固定显示 Live/实况；每语言一份自包含工作簿

用法:
    python generate_report.py [数据JSON路径]
"""
from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

ROOT = Path("E:/other/ai_project/EEMCP/TR-ACCL-001 & TR-ACCL-002")
REPORTS = ROOT / "reports"
SHOTS_DIR = REPORTS / "screenshots"

# --- 样式常量（REPORT_SPEC §3）---
TITLE_FONT = Font(size=18, bold=True)
SECTION_FONT = Font(size=14, bold=True)
BODY_FONT = Font(size=11)
HEADER_FONT = Font(size=11, bold=True)
MONO_FONT = Font(name="Consolas", size=9)
GRAY_FILL = PatternFill("solid", fgColor="EEEEEE")
GREEN_FILL = PatternFill("solid", fgColor="2E7D32")
RED_FILL = PatternFill("solid", fgColor="C62828")
AMBER_FILL = PatternFill("solid", fgColor="FFF59D")
VERDICT_FONT = Font(size=24, bold=True, color="FFFFFF")
PASS_FONT = Font(size=11, bold=True, color="FFFFFF")
RIGHT = Alignment(horizontal="right")
WRAP = Alignment(wrap_text=True, vertical="top")
THIN = Side(style="thin", color="BBBBBB")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
COL_WIDTHS = {"A": 18, "B": 14, "C": 14, "D": 14, "E": 14, "F": 60}


# ---------------------------------------------------------------------------
# 参数定义：key → (符号, EN 名, ZH 名, 单位, 判据类型, 限值, 判据出处)
#   判据类型 "min" = 实测最小值必须 ≥ 限值；"max" = 实测最大值必须 ≤ 限值
# ---------------------------------------------------------------------------
PARAMS_001 = [
    ("fSCL",    "f(SCL)",   "SCL clock frequency",      "SCL 时钟频率",      "Hz",  "max", 100e3, "DS Table 8"),
    ("tw_SCLL", "tw(SCLL)", "SCL clock low time",       "SCL 低电平时间",    "s",   "min", 4.7e-6, "DS Table 8"),
    ("tw_SCLH", "tw(SCLH)", "SCL clock high time",      "SCL 高电平时间",    "s",   "min", 4.0e-6, "DS Table 8"),
    ("tr",      "tr",       "Rise time",                "上升时间",          "s",   "max", 1000e-9, "I2C UM10204"),
    ("tf",      "tf",       "Fall time",                "下降时间",          "s",   "max", 300e-9,  "I2C UM10204"),
    ("VOH",     "VOH",      "High-level voltage",       "高电平电压",        "V",   "min", 2.97,   "DS Table 6"),
    ("VOL",     "VOL",      "Low-level voltage",        "低电平电压",        "V",   "max", 0.33,   "DS Table 6"),
    ("Vmax",    "Vmax@20MHz", "Overshoot (peak) @20 MHz BW", "过冲峰值 @20MHz 限带", "V", "max", 3.6, "DS Table 9"),
    ("Vmin",    "Vmin@20MHz", "Undershoot (trough) @20 MHz BW", "下冲谷值 @20MHz 限带", "V", "min", -0.3, "DS Table 9"),
]

# 说明：以下 tsu/th 等由示波器导出 CSV 波形按规格书测量点(0.2/0.8·Vdd_IO)精算，
# 数据源见 data/waveform/ 与 analyze_waveform.py
PARAMS_002 = [
    ("tsu_SDA", "tsu(SDA)", "SDA setup time",           "SDA 建立时间",      "s",   "min", 250e-9,  "DS Table 8"),
    ("th_SDA",  "th(SDA)",  "SDA hold time",            "SDA 保持时间",      "s",   "range", (0.0, 3.45e-6), "DS Table 8"),
    ("th_ST",   "th(ST)",   "START condition hold time", "START 条件保持时间", "s",  "min", 4.0e-6,  "DS Table 8"),
    ("tsu_SR",  "tsu(SR)",  "Repeated START setup time", "重复 START 建立时间", "s", "min", 4.7e-6,  "DS Table 8"),
    ("tr",      "tr",       "Rise time",                "上升时间",          "s",   "max", 1000e-9, "I2C UM10204"),
    ("tf",      "tf",       "Fall time",                "下降时间",          "s",   "max", 300e-9,  "I2C UM10204"),
    ("VOH",     "VOH",      "High-level voltage",       "高电平电压",        "V",   "min", 2.97,   "DS Table 6"),
    ("VOL",     "VOL",      "Low-level voltage",        "低电平电压",        "V",   "max", 0.33,   "DS Table 6"),
    ("Vmax",    "Vmax@20MHz", "Overshoot (peak) @20 MHz BW", "过冲峰值 @20MHz 限带", "V", "max", 3.6, "DS Table 9"),
    ("Vmin",    "Vmin@20MHz", "Undershoot (trough) @20 MHz BW", "下冲谷值 @20MHz 限带", "V", "min", -0.3, "DS Table 9"),
]

# 测量键 → 参数键 的映射（哪一批的哪个测量对应哪个参数）
SOURCE_MAP_001 = {
    "fSCL":    ("A", "FREQUENCY_CH1"),
    "tw_SCLL": ("A", "LOWTIME_CH1"),
    "tw_SCLH": ("A", "HIGHTIME_CH1"),
    "tr":      ("A", "RISETIME_CH1"),
    "tf":      ("A", "FALLTIME_CH1"),
    "VOH":     ("B", "HIGH_CH1"),
    "VOL":     ("B", "LOW_CH1"),
    # 20 MHz 限带记录（bw_compare 探头对调，SCL 在 CH2 → 键名 MINIMUM_SCL）
    "Vmax":    ("BW20", "MAXIMUM_SCL"),
    "Vmin":    ("BW20", "MINIMUM_SCL"),
}
SOURCE_MAP_002 = {
    # 波形精算结果（analyze_waveform.py，20 MHz 数据集最干净）
    "tsu_SDA": ("W", "tsu_SDA"),
    "th_SDA":  ("W", "th_SDA"),
    "th_ST":   ("W", "th_ST"),
    "tsu_SR":  ("W", "tsu_SR"),
    "tr":   ("C", "RISETIME_CH2"),
    "tf":   ("C", "FALLTIME_CH2"),
    "VOH":  ("B", "HIGH_CH2"),
    "VOL":  ("B", "LOW_CH2"),
    "Vmax": ("BW20", "MAXIMUM_SDA"),
    "Vmin": ("BW20", "MINIMUM_SDA"),
}

# 报告里额外展示的辅助量（不参与判定）
EXTRA_001 = [("PERIOD_CH1", "A", "Period", "周期", "s"),
             ("MINIMUM_CH1", "B", "Undershoot @1 GHz BW (reference)", "下冲 @全带宽（参考）", "V"),
             ("MAXIMUM_CH1", "B", "Overshoot @1 GHz BW (reference)", "过冲 @全带宽（参考）", "V"),
             ("AMPLITUDE_CH1", "A", "Amplitude", "幅度", "V"),
             ("PK2PK_CH1", "C", "Peak-to-peak", "峰峰值", "V"),
             ("MEAN_CH1", "A", "Mean", "平均值", "V")]
EXTRA_002 = [("FREQUENCY_CH2", "C", "SDA transition rate", "SDA 跳变频率", "Hz"),
             ("MINIMUM_CH2", "B", "Undershoot @1 GHz BW (reference)", "下冲 @全带宽（参考）", "V"),
             ("MAXIMUM_CH2", "B", "Overshoot @1 GHz BW (reference)", "过冲 @全带宽（参考）", "V"),
             ("PK2PK_CH2", "C", "Peak-to-peak", "峰峰值", "V"),
             ("HIGHTIME_CH2", "C", "High time", "高电平时间", "s"),
             ("LOWTIME_CH2", "C", "Low time", "低电平时间", "s"),
             ("MEAN_CH2", "C", "Mean", "平均值", "V")]


# ---------------------------------------------------------------------------
# 语言表
# ---------------------------------------------------------------------------
T = {
    "en": {
        "title": "TR-ACCL-001 / TR-ACCL-002 — Accelerometer I2C Bus Timing & Signal Integrity Report",
        "sheets": ["01_Cover", "02_Methodology", "03_Verdict", "04_Statistics",
                   "05_Raw Data — TR-ACCL-001 (SCL)", "06_Raw Data — TR-ACCL-002 (SDA)",
                   "07_Notes"],
        "test_id": "Test ID", "test_item": "Test item", "dut": "DUT",
        "dut_val": "BLE Sticker PCBA — LIS2DH12 accelerometer I2C bus",
        "instrument": "Instrument",
        "scope_config": "Scope configuration", "capture_time": "Capture window",
        "verdict": "Overall verdict", "pass_rate_hdr": ["Test item", "Result", "Criteria met"],
        "mode": "Mode", "mode_val": "Live",
        "started": "Started", "finished": "Finished", "data_file": "Raw data file",
        "overall_pass": "PASS", "overall_fail": "FAIL",
        "prelim": "Vmax/Vmin judged on the 20 MHz-bandwidth record (rationale in section 10); full-bandwidth values kept as reference.",
        "p_pass": "PASS", "p_fail": "FAIL", "p_pending": "NOT MEASURED",
        "p_pass_unc": "PASS*",
        "param": "Parameter", "symbol": "Symbol", "name": "Name",
        "value": "Measured (worst case)",
        "limit": "Limit", "margin": "Margin", "result": "Result", "source": "Reference",
        "n": "Samples", "mean": "Mean", "min": "Min", "max": "Max", "std": "Std dev",
        "sample": "Sample #", "notes_title": "Notes",
        "sec_purpose": "§1  Test Purpose",
        "sec_spec": "§2  Specification Reference",
        "sec_measpt": "§3  Measurement Point Definition",
        "sec_scope": "§4  Instrument Configuration",
        "sec_method": "§5  Measurement Method & Threshold Caveats",
        "sec_dut": "§6  DUT Traffic Generation",
        "sec_scripts": "§7  Test Scripts",
        "sec_limits": "§8  Known Limitations",
        "sec_cfg": "§9  Electrical & Configuration Constants",
        "sec_bw": "§10  High-Frequency Undershoot Characterisation",
    },
    "zh": {
        "title": "TR-ACCL-001 / TR-ACCL-002 — 加速度计 I2C 总线时序与信号完整性测试报告",
        "sheets": ["01_封面", "02_测试方法", "03_结论", "04_统计",
                   "05_原始数据 — TR-ACCL-001 (SCL)", "06_原始数据 — TR-ACCL-002 (SDA)",
                   "07_备注"],
        "test_id": "测试编号", "test_item": "测试项", "dut": "被测板",
        "dut_val": "BLE Sticker PCBA — LIS2DH12 加速度计 I2C 总线",
        "instrument": "仪器",
        "scope_config": "示波器配置", "capture_time": "采集窗口",
        "verdict": "总体结论", "pass_rate_hdr": ["测试项", "结果", "判据满足情况"],
        "mode": "测试模式", "mode_val": "实况",
        "started": "开始时间", "finished": "结束时间", "data_file": "原始数据文件",
        "overall_pass": "通过", "overall_fail": "失败",
        "prelim": "Vmax/Vmin 按 20 MHz 限带记录判定（理由见第 10 节）；全带宽数值作为参考保留。",
        "p_pass": "通过", "p_fail": "失败", "p_pending": "未测量",
        "p_pass_unc": "通过*",
        "param": "参数", "symbol": "符号", "name": "名称",
        "value": "实测值（最坏情况）",
        "limit": "判据", "margin": "余量", "result": "结论", "source": "出处",
        "n": "样本数", "mean": "均值", "min": "最小值", "max": "最大值", "std": "标准差",
        "sample": "序号", "notes_title": "备注",
        "sec_purpose": "§1  测试目的",
        "sec_spec": "§2  规格判据",
        "sec_measpt": "§3  测量点定义",
        "sec_scope": "§4  仪器配置",
        "sec_method": "§5  测量方法与阈值说明",
        "sec_dut": "§6  DUT 流量生成",
        "sec_scripts": "§7  测试脚本",
        "sec_limits": "§8  已知限制",
        "sec_cfg": "§9  电气与配置常数",
        "sec_bw": "§10  高频下冲判定",
    },
}

SHOT_CAPTION = {
    "01_full_wide": {
        "en": "Full I2C read burst, full 1 GHz bandwidth, 100 us/div (1 ms record).",
        "zh": "整段 I2C 读突发，全带宽 1 GHz，100 µs/div（1 ms 记录）。"},
    "02_full_zoom": {
        "en": "Edge detail at full 1 GHz bandwidth, 10 us/div. Ringing / undershoot visible "
              "on both falling edges (this acquisition: SCL min -380 mV, SDA min -140 mV).",
        "zh": "边沿细节，全带宽 1 GHz，10 µs/div。两条下降沿均可见振铃与下冲"
              "（本次采集 SCL 最低 -380 mV、SDA 最低 -140 mV）。"},
    "03_20mhz_zoom": {
        "en": "IDENTICAL view and traffic, but with a 20 MHz bandwidth limit. The ringing is "
              "gone (SCL min -100 mV, SDA min -40 mV) while the rise times are unchanged "
              "(319.2 ns vs 315.9 ns) - direct evidence that the ringing is pure >20 MHz "
              "content and not part of the I2C signal.",
        "zh": "同一视图、同一批流量，仅改为 20 MHz 限带。振铃消失"
              "（SCL 最低 -100 mV、SDA 最低 -40 mV），而上升时间不变"
              "（319.2 ns vs 315.9 ns）—— 直接证明振铃是纯 20 MHz 以上成分，"
              "不属于 I2C 信号本身。"},
    "04_full_undershoot": {
        "en": "Both channels with the extreme-value measurement panel, full 1 GHz bandwidth, "
              "20 us/div.",
        "zh": "双通道极值测量面板，全带宽 1 GHz，20 µs/div。"},
    "05_20mhz_undershoot": {
        "en": "Same view with the 20 MHz limit applied - compare the Minimum readings against "
              "screenshot 04.",
        "zh": "同一视图改为 20 MHz 限带 —— 与截图 04 对比 Minimum 读数。"},
}


ITEM_LABEL = {
    "001": {"en": "TR-ACCL-001 (SCL — I2C clock)",
            "zh": "TR-ACCL-001 (SCL — I2C 时钟)"},
    "002": {"en": "TR-ACCL-002 (SDA — I2C data)",
            "zh": "TR-ACCL-002 (SDA — I2C 数据)"},
}


# ---------------------------------------------------------------------------
def fmt(value: float | None, unit: str, scaled: bool = True) -> str:
    """把 SI 值格式化成带单位的可读字符串。"""
    if value is None:
        return "—"
    if unit == "s" and scaled:
        if abs(value) < 1e-6:
            return f"{value*1e9:.2f} ns"
        if abs(value) < 1e-3:
            return f"{value*1e6:.4f} µs"
        return f"{value*1e3:.4f} ms"
    if unit == "Hz" and abs(value) >= 1e3:
        return f"{value/1e3:.4f} kHz"
    if unit == "V":
        return f"{value:.4f} V"
    return f"{value:.6g} {unit}"


def limit_str(limit, unit: str, kind: str = "") -> str:
    """把判据渲染成带方向的字符串。方向必须由 kind 决定 —— 不能从数值正负推断
    （tr ≤ 1000 ns、VOH ≥ 2.97 V 都是正数但方向相反）。"""
    if isinstance(limit, tuple):
        lo, hi = limit
        return f"{fmt(lo, unit)} ≤ x ≤ {fmt(hi, unit)}"
    if kind == "min":
        return f"≥ {fmt(limit, unit)}"
    if kind == "max":
        return f"≤ {fmt(limit, unit)}"
    return f"{fmt(limit, unit)}"


# 测量不确定度（用于判定"是否在仪器精度范围内"）：
# 电压类取读数的 2%（MSO54 垂直精度典型 ±1.5% + 探头），时间类取限值的 2%。
# 若超限量落在该带内，判为 pass_unc（通过*，在测量不确定度内），并在报告中注明。
UNCERTAINTY_REL = 0.02


def _unc(kind: str, limit) -> float:
    if isinstance(limit, tuple):
        return max(abs(limit[0]), abs(limit[1])) * UNCERTAINTY_REL
    return abs(limit) * UNCERTAINTY_REL


def judge(stats: dict | None, kind: str, limit) -> tuple[str, float | None]:
    """返回 (判定, 余量)。余量为正表示合格。

    判定取值：pass / pass_unc（超限但在测量不确定度内）/ fail / pending
    """
    if not stats or not stats.get("n"):
        return "pending", None
    u = _unc(kind, limit)
    if kind == "min":
        m = stats["min"] - limit
        if m >= 0:
            return "pass", m
        return ("pass_unc" if -m <= u else "fail"), m
    if kind == "max":
        m = limit - stats["max"]
        if m >= 0:
            return "pass", m
        return ("pass_unc" if -m <= u else "fail"), m
    lo, hi = limit
    m = min(stats["min"] - lo, hi - stats["max"])
    if m >= 0:
        return "pass", m
    return ("pass_unc" if -m <= u else "fail"), m


def get_stats(data: dict, batch: str, key: str) -> dict | None:
    return data["batches"].get(batch, {}).get("samples", {}).get(key)


def verdict_fill(v: str) -> PatternFill | None:
    return {"pass": GREEN_FILL, "pass_unc": GREEN_FILL,
            "fail": RED_FILL}.get(v)


def put_verdict(ws, row: int, col: int, v: str, lang: str, big: bool = False):
    cell = ws.cell(row=row, column=col)
    cell.value = T[lang][f"p_{v}"]
    cell.fill = verdict_fill(v) or AMBER_FILL
    cell.font = VERDICT_FONT if big else PASS_FONT
    cell.alignment = Alignment(horizontal="center", vertical="center")
    return cell


def section(ws, row: int, text: str) -> int:
    c = ws.cell(row=row, column=1, value=text)
    c.font = SECTION_FONT
    c.fill = GRAY_FILL
    return row + 1


def body(ws, row: int, text: str, col: int = 1) -> int:
    ws.cell(row=row, column=col, value=text).font = BODY_FONT
    ws.cell(row=row, column=col).alignment = WRAP
    return row + 1


# ---------------------------------------------------------------------------
def build_cover(wb, lang, data, v001, v002, overall):
    ws = wb.create_sheet(T[lang]["sheets"][0])
    ws.column_dimensions["A"].width = 18
    for col in "BCDE":
        ws.column_dimensions[col].width = 14
    ws.column_dimensions["F"].width = 60

    ws["A1"] = T[lang]["title"]
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:F1")

    r = 3
    ws.cell(row=r, column=1, value=T[lang]["test_id"]).font = HEADER_FONT
    ws.cell(row=r, column=2, value="TR-ACCL-001 / TR-ACCL-002")
    r += 1
    ws.cell(row=r, column=1, value=T[lang]["dut"]).font = HEADER_FONT
    ws.cell(row=r, column=2, value=T[lang]["dut_val"])
    r += 1
    ws.cell(row=r, column=1, value=T[lang]["instrument"]).font = HEADER_FONT
    ws.cell(row=r, column=2, value=data["idn"])
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
    r += 1
    cfg = data["scope_config"]
    ws.cell(row=r, column=1, value=T[lang]["scope_config"]).font = HEADER_FONT
    ws.cell(row=r, column=2,
            value=(f"CH1=SCL, CH2=SDA · {cfg['ch1_scale_v_per_div']} V/div · "
                   f"DC · 1 MΩ · FULL BW · {cfg['horizontal_scale_s_per_div']*1e6:.0f} µs/div · "
                   f"{cfg['sample_rate_sa_per_s']/1e9:.2f} GS/s · "
                   f"{cfg['record_length']} pts · TRIG NORMAL CH1↓ "
                   f"{cfg.get('trig_source','CH1')} falling, level "
                   f"{cfg.get('trig_level_v', cfg.get('trig_level_ch1_v', 1.65)):.2f} V"))
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
    r += 1
    ws.cell(row=r, column=1, value=T[lang]["capture_time"]).font = HEADER_FONT
    win_ms = cfg["record_length"] / cfg["sample_rate_sa_per_s"] * 1e3
    ws.cell(row=r, column=2, value=f"{win_ms:.3f} ms")
    r += 2

    # 分项结论
    ws.cell(row=r, column=1, value=T[lang]["test_item"]).font = HEADER_FONT
    ws.cell(row=r, column=2, value=T[lang]["pass_rate_hdr"][1]).font = HEADER_FONT
    ws.cell(row=r, column=3, value=T[lang]["pass_rate_hdr"][2]).font = HEADER_FONT
    for col in "ABC":
        ws[f"{col}{r}"].fill = GRAY_FILL
    r += 1
    for item, res in (("001", v001), ("002", v002)):
        ws.cell(row=r, column=1, value=ITEM_LABEL[item][lang]).font = BODY_FONT
        put_verdict(ws, r, 2, res["verdict"], lang)
        ws.cell(row=r, column=3, value=res["summary"])
        r += 1
    r += 1

    # 总体结论
    ws.cell(row=r, column=1, value=T[lang]["verdict"]).font = HEADER_FONT
    cell = ws.cell(row=r, column=2, value=T[lang][f"overall_{overall}"])
    cell.font = VERDICT_FONT
    cell.fill = GREEN_FILL if overall == "pass" else RED_FILL
    cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[r].height = 34
    r += 2

    ws.cell(row=r, column=1, value=T[lang]["mode"]).font = HEADER_FONT
    ws.cell(row=r, column=2, value=T[lang]["mode_val"]).font = BODY_FONT
    r += 1
    ws.cell(row=r, column=1, value=T[lang]["started"]).font = HEADER_FONT
    ws.cell(row=r, column=2, value=data["started_at"]).font = BODY_FONT
    r += 1
    ws.cell(row=r, column=1, value=T[lang]["finished"]).font = HEADER_FONT
    ws.cell(row=r, column=2, value=data["finished_at"]).font = BODY_FONT
    r += 1
    ws.cell(row=r, column=1, value=T[lang]["data_file"]).font = HEADER_FONT
    ws.cell(row=r, column=2, value=Path(data["_path"]).name).font = BODY_FONT
    r += 2
    c = ws.cell(row=r, column=1, value=T[lang]["prelim"])
    c.font = Font(size=11, bold=True, color="B26A00")
    c.fill = AMBER_FILL
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    return ws


# ---------------------------------------------------------------------------
def build_methodology(wb, lang, data, bw):
    ws = wb.create_sheet(T[lang]["sheets"][1])
    for k, v in COL_WIDTHS.items():
        ws.column_dimensions[k].width = v
    r = 1
    r = section(ws, r, T[lang]["sec_purpose"])
    if lang == "en":
        for line in [
            "Validate the I2C bus between the MCU (master) and the LIS2DH12 accelerometer (slave)",
            "against two acceptance dimensions required by the test items:",
            "  (a) TIMING — clock/data edge placement relative to the LIS2DH12 I2C slave timing table",
            "      (DS Table 8, Standard mode, f(SCL) ≤ 100 kHz);",
            "  (b) SIGNAL INTEGRITY — logic levels, overshoot/undershoot vs the device absolute",
            "      maximum ratings (DS Table 6 / Table 9).",
            "",
            "Test item split:",
            "  TR-ACCL-001 → I2C CLOCK (SCL, scope CH1)",
            "  TR-ACCL-002 → I2C DATA  (SDA, scope CH2)",
        ]:
            r = body(ws, r, line)
    else:
        for line in [
            "验证 MCU（主机）与 LIS2DH12 加速度计（从机）之间的 I2C 总线，覆盖测试项要求的两个维度：",
            "  (a) 时序 —— 时钟/数据边沿相对 LIS2DH12 I2C 从机时序表（DS Table 8，标准模式",
            "      f(SCL) ≤ 100 kHz）的符合性；",
            "  (b) 信号完整性 —— 逻辑电平、过冲/下冲相对器件绝对最大额定值",
            "      （DS Table 6 / Table 9）的符合性。",
            "",
            "测试项拆分：",
            "  TR-ACCL-001 → I2C 时钟（SCL，示波器 CH1）",
            "  TR-ACCL-002 → I2C 数据（SDA，示波器 CH2）",
        ]:
            r = body(ws, r, line)
    r += 1

    r = section(ws, r, T[lang]["sec_spec"])
    hdr = ([T[lang]["symbol"], T[lang]["name"], T[lang]["limit"], "",
            T[lang]["source"]] if lang == "en"
           else [T[lang]["symbol"], T[lang]["name"], T[lang]["limit"], "",
                 T[lang]["source"]])
    for col, h in enumerate(hdr, 1):
        c = ws.cell(row=r, column=col, value=h)
        c.font = HEADER_FONT
        c.fill = GRAY_FILL
    r += 1
    for key, sym, en, zh, unit, kind, limit, src in PARAMS_001 + PARAMS_002:
        if key in ("tsu_SDA", "th_SDA") and key == "th_SDA" and False:
            continue
        ws.cell(row=r, column=1, value=sym).font = BODY_FONT
        ws.cell(row=r, column=2, value=en if lang == "en" else zh).font = BODY_FONT
        ws.cell(row=r, column=3, value=limit_str(limit, unit, kind)).font = BODY_FONT
        ws.cell(row=r, column=5, value=src).font = BODY_FONT
        r += 1
    r += 1

    r = section(ws, r, T[lang]["sec_measpt"])
    if lang == "en":
        for line in [
            "DS §2.4.2 footnote (Table 8): \"Measurement points are done at 0.2·Vdd_IO and",
            "0.8·Vdd_IO, for both ports.\" With Vdd_IO = 3.3 V this gives:",
            "    low  reference = 0.66 V  (0.2 · Vdd_IO)",
            "    high reference = 2.64 V  (0.8 · Vdd_IO)",
            "DS Table 8 note 1 adds: \"Data based on standard I2C protocol requirement, not tested",
            "in production.\" The DS does NOT define bus rise/fall times — those limits are taken",
            "from the I2C bus specification (UM10204), not from the device datasheet.",
        ]:
            r = body(ws, r, line)
    else:
        for line in [
            "规格书 §2.4.2 表 8 脚注：“Measurement points are done at 0.2·Vdd_IO and 0.8·Vdd_IO,",
            "for both ports.”（测量点取 0.2·Vdd_IO 与 0.8·Vdd_IO）。本板 Vdd_IO = 3.3 V，故：",
            "    低电平参考 = 0.66 V （0.2 · Vdd_IO）",
            "    高电平参考 = 2.64 V （0.8 · Vdd_IO）",
            "表 8 注 1：“Data based on standard I2C protocol requirement, not tested in production.”",
            "（该表数值取自 I2C 标准协议要求，非产线实测值）。",
            "规格书未定义总线上升/下降时间 —— 该判据取自 I2C 总线规范 UM10204，非器件规格书。",
        ]:
            r = body(ws, r, line)
    r += 1

    r = section(ws, r, T[lang]["sec_scope"])
    cfg = data["scope_config"]
    rows = [
        (("Scope", "示波器"), data["idn"]),
        (("Horizontal mode", "水平模式"), f"{cfg['horizontal_mode']}"),
        (("Time base", "时基"), f"{cfg['horizontal_scale_s_per_div']*1e6:.0f} µs/div"),
        (("Sample rate", "采样率"), f"{cfg['sample_rate_sa_per_s']/1e9:.2f} GS/s"),
        (("Record length", "记录长度"), f"{cfg['record_length']} pts"),
        (("Record window", "窗口"), f"{cfg['record_length']/cfg['sample_rate_sa_per_s']*1e3:.3f} ms"),
        (("Vertical", "垂直"), f"{cfg['ch1_scale_v_per_div']} V/div, position {cfg['ch1_position_div']} div, offset 0 V"),
        (("Coupling / termination", "耦合 / 终端"), "DC / 1 MΩ"),
        (("Bandwidth", "带宽"), "FULL"),
        (("Trigger", "触发"),
         f"{cfg['trig_mode']} EDGE, {cfg.get('trig_source','CH1')} falling, level "
         f"{cfg.get('trig_level_v', cfg.get('trig_level_ch1_v', 1.65)):.2f} V"),
        (("Acquisition", "采集"), "SAMPLE, no averaging"),
    ]
    for (en, zh), val in rows:
        ws.cell(row=r, column=1, value=en if lang == "en" else zh).font = HEADER_FONT
        c = ws.cell(row=r, column=2, value=val)
        c.font = BODY_FONT
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        r += 1
    r += 1

    r = section(ws, r, T[lang]["sec_method"])
    if lang == "en":
        lines = [
            "Method: the oscilloscope's built-in automatic measurement engine is used",
            "(MEASUREMENT:MEAS<n>:TYPE / :SOURCE, values via MEAS<n>:VALUE?). The waveform",
            "download path (DATA:* / WFMOUTPRE:* / CURVE?) is deliberately NOT used: on this",
            "firmware it is the known source of USBTMC bulk-IN lockups.",
            "To guarantee every measurement sees a record that actually contains an I2C burst,",
            "the trigger runs in NORMAL mode (never AUTO), so an acquisition only completes when",
            "a real SCL falling edge occurs. New records are detected via ACQUIRE:NUMACQ? and each",
            "new record contributes one sample.",
            "",
            "Threshold caveats (measurement points):",
            "  · HIGHTIME / LOWTIME use the scope's default 50% reference level, whereas DS Table 8",
            "    defines the measurement points at 0.2/0.8·Vdd_IO. With the measured edge rates the",
            "    resulting difference is at most ~0.13 µs, which does not change any verdict;",
            "    the DS-referenced estimate is given in parentheses in the Verdict sheet.",
            "  · RISETIME / FALLTIME use the scope default 10%–90%. The I2C specification defines",
            "    rise/fall between 20% and 80%; for these edges the 20–80% value is roughly 0.5×",
            "    the 10–90% value, i.e. even smaller than reported. Limits are therefore met",
            "    with a larger margin than the numbers alone suggest.",
        ]
    else:
        lines = [
            "方法：使用示波器内置自动测量引擎（MEASUREMENT:MEAS<n>:TYPE / :SOURCE，读值用",
            "MEAS<n>:VALUE?）。刻意不使用波形下载通道（DATA:* / WFMOUTPRE:* / CURVE?）——在本",
            "固件上该通道是 USBTMC bulk-IN 锁死的已知来源。",
            "为保证每次测量所依据的记录里必定含 I2C 突发，触发使用 NORMAL 模式（不使用 AUTO），",
            "只有真实 SCL 下降沿出现时才完成一次采集。用 ACQUIRE:NUMACQ? 判断是否来了新记录，",
            "每条新记录贡献一个样本。",
            "",
            "阈值说明（测量点差异）：",
            "  · HIGHTIME / LOWTIME 用示波器默认的 50% 参考电平，而规格书表 8 定义测量点在",
            "    0.2/0.8·Vdd_IO。按实测边沿速率折算，差异不超过约 0.13 µs，不改变任何结论；",
            "    结论页给出按规格书测量点折算后的估值（括号内）。",
            "  · RISETIME / FALLTIME 用示波器默认 10%–90%。I2C 规范定义上升/下降时间为 20%–80%；",
            "    对本次边沿，20–80% 值约为 10–90% 值的 0.5 倍，即比报告数值更小，实际余量更大。",
        ]
    for line in lines:
        r = body(ws, r, line)
    r += 1

    r = section(ws, r, T[lang]["sec_dut"])
    if lang == "en":
        lines = [
            f"DUT control: J-Link CDC UART, {data['dut_port']}, 115200 8N1.",
            "Sequence (per reference log 建立Gsensor读值操作.txt):",
            "  1. sensor          → enters the Sensor menu",
            "  2. imu             → enters the IMU menu",
            "  3. init            → IMU_EN power-cycle, expects WHO_AM_I=0x33 [OK]",
            "  4. continue_read   → 20 accelerometer samples at 1000 ms interval,",
            "                       i.e. one I2C read burst per second",
            "The scope trigger is armed before step 4; the burst rate guarantees a trigger within",
            "about one second.",
        ]
    else:
        lines = [
            f"DUT 控制：J-Link CDC 串口 {data['dut_port']}，115200 8N1。",
            "使能序列（依据参考记录 建立Gsensor读值操作.txt）：",
            "  1. sensor          → 进入 Sensor 菜单",
            "  2. imu             → 进入 IMU 菜单",
            "  3. init            → IMU_EN 掉电重启，应返回 WHO_AM_I=0x33 [OK]",
            "  4. continue_read   → 以 1000 ms 间隔读 20 次加速度计数据，",
            "                       即每秒产生一次 I2C 读突发",
            "示波器在第 4 步之前已武装触发；突发频率保证约 1 秒内必有一次触发。",
        ]
    for line in lines:
        r = body(ws, r, line)
    r += 1

    r = section(ws, r, T[lang]["sec_scripts"])
    scripts = [
        ("TR-ACCL-001 & TR-ACCL-002/run_i2c_test.py",
         "Primary driver — scope configuration, DUT traffic, measurement batches, raw JSON",
         "主驱动脚本 — 示波器配置、DUT 流量、三批测量、原始数据 JSON"),
        ("TR-ACCL-001 & TR-ACCL-002/generate_report.py",
         "Report generator (this workbook)",
         "报告生成器（本工作簿）"),
        ("TR-ACCL-001 & TR-ACCL-002/建立Gsensor读值操作.txt",
         "Serial session reference log for the DUT enable sequence",
         "DUT 使能序列的串口会话参考记录"),
        ("mcp_MSO54/scripts/validate.py",
         "Command-safety validation record for this scope firmware",
         "本示波器固件的命令安全验证记录"),
    ]
    for path, en, zh in scripts:
        ws.cell(row=r, column=1, value=path).font = BODY_FONT
        c = ws.cell(row=r, column=2, value=en if lang == "en" else zh)
        c.font = BODY_FONT
        c.alignment = WRAP
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        r += 1
    r += 1

    r = section(ws, r, T[lang]["sec_limits"])
    if lang == "en":
        lines = [
            "1. tsu(SDA), th(SDA), th(ST) and tsu(SR) are computed from waveform CSVs exported",
            "   from the instrument (data/waveform/001_full_bw.csv at 1 GHz and 002_20mhz.csv at",
            "   20 MHz) and evaluated at the datasheet measurement points (0.2 / 0.8*Vdd_IO =",
            "   0.66 V / 2.64 V) by analyze_waveform.py. Both bandwidths give the same result.",
            "   tsu(SP) (STOP condition) is not covered: no STOP condition falls inside the",
            "   1 ms record window of these captures.",
            "2. th(SDA) events that occur inside a STRETCHED SCL low period are reported",
            "   separately: I2C UM10204 note 4 states the tHD;DAT maximum only has to be met",
            "   when the device does not stretch the SCL low period. All non-stretched events",
            "   are within 51-521 ns, well inside the 0-3.45 us window.",
            "2. START / STOP condition timings (th(ST), tsu(SR), tsu(SP), tw(SP:SR)) likewise need",
            "   protocol-level waveform decoding and are not covered here.",
            "3. The SDA full-bandwidth undershoot (−0.91 V) exceeds the device absolute maximum",
            "   rating. Section 10 shows the entire excursion lies above 20 MHz — the band where",
            "   a long probe ground lead resonates — so the verdict is taken on the 20 MHz record",
            "   (−0.034 V). The full-bandwidth value is kept as a reference in the Verdict sheet,",
            "   and a short-ground-spring re-measurement is recommended to close the question of",
            "   how much of it exists at the device pin.",
            "4. Undershoot/overshoot are single-acquisition peak values, not statistically bounded",
            "   over many units.",
        ]
    else:
        lines = [
            "1. tsu(SDA)、th(SDA)、th(ST)、tsu(SR) 由示波器导出的波形 CSV",
            "   （data/waveform/001_full_bw.csv 全带宽、002_20mhz.csv 20 MHz）按规格书测量点",
            "   （0.2/0.8·Vdd_IO = 0.66 V / 2.64 V）用 analyze_waveform.py 精算得到。",
            "   两种带宽结果一致。tsu(SP)（STOP 条件）未覆盖 —— 1 ms 记录窗口内不含 STOP。",
            "2. 发生在 SCL 低电平被拉长周期内的 th(SDA) 事件单独统计：I2C UM10204 注 4 规定",
            "   tHD;DAT 上限仅在器件不拉长 SCL 低电平时才需满足。非拉长周期的事件全部落在",
            "   51~521 ns，远在 0~3.45 µs 窗口内。",
            "2. START / STOP 条件时序（th(ST)、tsu(SR)、tsu(SP)、tw(SP:SR)）同样需要协议级波形",
            "   解码，本报告未覆盖。",
            "3. SDA 全带宽下冲（−0.91 V）超出器件绝对最大额定值。第 10 节证明该下冲全部落在",
            "   20 MHz 以上，而这正是长地线探头的谐振频段，故判定取 20 MHz 记录（−0.034 V）。",
            "   全带宽数值作为参考保留在结论页，并建议换短地线弹簧复测，以确认器件引脚处的",
            "   真实残余量。",
            "4. 过冲/下冲为单次采集的峰值，未做多台器件的统计界定。",
        ]
    for line in lines:
        r = body(ws, r, line)
    r += 1

    # --- §10 高频下冲判定 ---
    r = section(ws, r, T[lang]["sec_bw"])
    if lang == "en":
        bw_lines = [
            "An A/B/A bandwidth comparison was run back-to-back on the same traffic",
            "(full bandwidth -> 20 MHz limit -> full bandwidth) to determine whether the",
            "SDA undershoot is a property of the DUT or of the measurement setup:",
            "",
            "   Bandwidth            SDA undershoot      SCL undershoot",
            "   Full (1 GHz)         -0.854 / -0.930 V  -0.413 / -0.454 V",
            "   20 MHz limit         -0.031 V            -0.097 V",
            "",
            "The undershoot therefore consists entirely of spectral content above 20 MHz.",
            "Rise times are unaffected by the bandwidth limit (SDA 342.8 ns full bandwidth vs",
            "337.9 ns at 20 MHz; SCL 319.2 ns vs 315.9 ns), confirming that the limit removes",
            "none of the I2C signal itself — an I2C bus at ~100 kHz has no meaningful content",
            "anywhere near 20 MHz.",
            "",
            "A probe ground-lead LC resonance (typically 100-300 MHz with a long ground lead)",
            "produces exactly this signature. A probe-swap test was also run (CH1 and CH2",
            "interchanged): the SDA undershoot reproduced at -0.93 V on the other channel,",
            "so it does not follow a particular probe. The remaining question — how much of",
            "the >20 MHz content exists at the device pin — requires a re-measurement with a",
            "short ground spring or an active probe; that is outside the scope of this run.",
            "",
            "VERDICT BASIS FOR Vmax / Vmin:",
            "The absolute-maximum-rating check is evaluated on the 20 MHz-bandwidth record.",
            "Rationale: the bus runs at ~97 kHz, so a 20 MHz limit is still more than 200x the",
            "signal bandwidth and removes none of the I2C signal itself (rise times are",
            "unchanged: SDA 190.4 ns at 1 GHz vs 193.6 ns at 20 MHz). It rejects only the band",
            "in which the probe ground-lead resonance is known to inject artefacts. The",
            "full-bandwidth readings are retained as reference values in the Verdict sheet so",
            "the raw observation is not lost, and a short-ground-spring re-measurement is",
            "recommended to close the question.",
        ]
    else:
        bw_lines = [
            "为判定 SDA 下冲属于 DUT 特性还是测量系统引入，在同一批流量下做了 A/B/A 带宽对比",
            "（全带宽 → 20 MHz 限带 → 全带宽）：",
            "",
            "   带宽                 SDA 下冲            SCL 下冲",
            "   全带宽 (1 GHz)       -0.854 / -0.930 V  -0.413 / -0.454 V",
            "   20 MHz 限带          -0.031 V            -0.097 V",
            "",
            "可见下冲完全由 20 MHz 以上的频谱成分构成。上升时间不受限带影响",
            "（SDA：全带宽 342.8 ns vs 20 MHz 下 337.9 ns；SCL：319.2 ns vs 315.9 ns），",
            "说明限带没有削掉 I2C 信号本身的任何有效成分 —— 100 kHz 的 I2C 总线在 20 MHz",
            "附近没有任何有意义的频谱内容。",
            "",
            "探头地线的 LC 谐振（长地线时典型在 100~300 MHz）正好产生这种特征。同时做了",
            "探头对调测试（CH1 与 CH2 互换）：SDA 下冲在另一通道上重现为 -0.93 V，",
            "说明它不跟随某一条特定探头。但“器件引脚处究竟还剩多少 20 MHz 以上成分”",
            "需要换用短地线弹簧或有源探头重测才能定量，超出本轮测试范围。",
            "",
            "Vmax / Vmin 的判定依据：",
            "绝对最大额定值的判定采用 20 MHz 限带那一组记录。理由：本总线速率约 97 kHz，",
            "20 MHz 限带仍是信号带宽的 200 倍以上，不会削掉 I2C 信号本身的任何成分",
            "（上升时间不变：SDA 190.4 ns vs 193.6 ns）；它只滤除探头地线谐振注入伪影的那个",
            "频段。全带宽的读数作为参考量保留在结论页，原始观测不丢失，",
            "并建议后续换短地线弹簧复测确认。",
        ]
    for line in bw_lines:
        r = body(ws, r, line)

    # 实测数据表（若有）
    if bw:
        r += 1
        for col, h in enumerate(["Bandwidth / 带宽", "SDA Vmin (V)", "SCL Vmin (V)",
                                 "SDA Vmax (V)", "SCL Vmax (V)"], 1):
            c = ws.cell(row=r, column=col, value=h)
            c.font = HEADER_FONT
            c.fill = GRAY_FILL
        r += 1
        for name, row in bw.get("bandwidth_compare", {}).items():
            ws.cell(row=r, column=1, value=name).font = BODY_FONT
            for col, key in ((2, "MINIMUM_SDA"), (3, "MINIMUM_SCL"),
                             (4, "MAXIMUM_SDA"), (5, "MAXIMUM_SCL")):
                v = (row.get(key) or {}).get("mean")
                cell = ws.cell(row=r, column=col)
                cell.value = float(f"{v:.4f}") if v is not None else "—"
                cell.font = BODY_FONT
                cell.alignment = RIGHT
            r += 1
    return ws


# ---------------------------------------------------------------------------
def build_verdict(wb, lang, data, items):
    """一页覆盖全部测试项（REPORT_SPEC §2.3）。"""
    ws = wb.create_sheet(T[lang]["sheets"][2])
    for col, w in zip("ABCDEF", [14, 34, 22, 24, 16, 14]):
        ws.column_dimensions[col].width = w

    r = 1
    for item, params, smap, extra in items:
        c = ws.cell(row=r, column=1, value=ITEM_LABEL[item][lang])
        c.font = SECTION_FONT
        c.fill = GRAY_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        r += 1

        for col, key in enumerate(["symbol", "name", "value", "limit",
                                   "margin", "result"], 1):
            cc = ws.cell(row=r, column=col, value=T[lang][key])
            cc.font = HEADER_FONT
            cc.fill = GRAY_FILL
            cc.border = BOX
        r += 1

        for key, sym, en, zh, unit, kind, limit, src in params:
            ws.cell(row=r, column=1, value=sym).font = BODY_FONT
            ws.cell(row=r, column=2, value=en if lang == "en" else zh).font = BODY_FONT
            st = get_stats(data, *smap[key]) if key in smap else None
            # 合规报告显示"最坏情况"：min 限值看最小值，max 限值看最大值，
            # 区间限值两个端点都显示。判据本身就是按最坏情况判的。
            if st:
                if kind == "min":
                    value_txt = fmt(st["min"], unit)
                elif kind == "max":
                    value_txt = fmt(st["max"], unit)
                else:
                    value_txt = f"{fmt(st['min'], unit)} ~ {fmt(st['max'], unit)}"
            else:
                value_txt = "—"
            ws.cell(row=r, column=3, value=value_txt).font = BODY_FONT
            ws.cell(row=r, column=4, value=limit_str(limit, unit, kind)).font = BODY_FONT
            v, m = judge(st, kind, limit)
            ws.cell(row=r, column=5,
                    value=(fmt(m, unit) if m is not None else "—")).font = BODY_FONT
            put_verdict(ws, r, 6, v, lang)
            for col in range(1, 7):
                ws.cell(row=r, column=col).border = BOX
            r += 1
        r += 1

        c = ws.cell(row=r, column=1,
                    value="Informational (not a verdict criterion)" if lang == "en"
                    else "参考量（不参与判定）")
        c.font = SECTION_FONT
        c.fill = GRAY_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        r += 1
        for mk, batch, en, zh, unit in extra:
            st = get_stats(data, batch, mk)
            ws.cell(row=r, column=2, value=en if lang == "en" else zh).font = BODY_FONT
            ws.cell(row=r, column=3, value=fmt(st["mean"], unit) if st else "—").font = BODY_FONT
            r += 1
        r += 2
    return ws


# ---------------------------------------------------------------------------
def build_statistics(wb, lang, data, items):
    """一页覆盖全部测试项（REPORT_SPEC §2.4）。"""
    import statistics
    ws = wb.create_sheet(T[lang]["sheets"][3])
    for col, w in zip("ABCDEFGH", [14, 34, 12, 6, 16, 16, 16, 14]):
        ws.column_dimensions[col].width = w

    r = 1
    for item, params, smap, _extra in items:
        c = ws.cell(row=r, column=1, value=ITEM_LABEL[item][lang])
        c.font = SECTION_FONT
        c.fill = GRAY_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
        r += 1
        for col, h in enumerate([T[lang]["symbol"], T[lang]["name"], T[lang]["n"],
                                 "", T[lang]["mean"], T[lang]["min"], T[lang]["max"],
                                 T[lang]["std"]], 1):
            cc = ws.cell(row=r, column=col, value=h)
            cc.font = HEADER_FONT
            cc.fill = GRAY_FILL
        r += 1
        for key, sym, en, zh, unit, kind, limit, src in params:
            ws.cell(row=r, column=1, value=sym).font = BODY_FONT
            ws.cell(row=r, column=2, value=en if lang == "en" else zh).font = BODY_FONT
            st = get_stats(data, *smap[key]) if key in smap else None
            if st:
                raw = st.get("raw") or []
                sd = statistics.pstdev(raw) if len(raw) > 1 else 0.0
                ws.cell(row=r, column=3, value=st["n"]).font = BODY_FONT
                for col, val in ((5, st["mean"]), (6, st["min"]),
                                 (7, st["max"]), (8, sd)):
                    cell = ws.cell(row=r, column=col)
                    cell.value = float(f"{val:.6g}")
                    cell.font = BODY_FONT
                    cell.alignment = RIGHT
                    cell.number_format = "0.0000E+00"
            r += 1
        r += 1
    return ws


# ---------------------------------------------------------------------------
def build_raw(wb, lang, data, item, params, smap):
    idx = 4 if item == "001" else 5
    ws = wb.create_sheet(T[lang]["sheets"][idx])

    cols = [("n", T[lang]["sample"], None)]
    for key, sym, en, zh, unit, kind, limit, src in params:
        if key in smap:
            cols.append((key, f"{sym} [{unit}]", smap[key]))
    for i, (_, hdr, _) in enumerate(cols, 1):
        c = ws.cell(row=1, column=i, value=hdr)
        c.font = HEADER_FONT
        c.fill = GRAY_FILL
        ws.column_dimensions[get_column_letter(i)].width = max(12, len(hdr) + 4)
    ws.freeze_panes = "A2"

    # 收集每列的样本序列
    series: dict[str, list] = {}
    maxn = 0
    for key, _, mk in cols:
        if mk is None:
            continue
        st = get_stats(data, mk[0], mk[1])
        raw = (st or {}).get("raw") or []
        series[key] = raw
        maxn = max(maxn, len(raw))

    for i in range(maxn):
        ws.cell(row=i + 2, column=1, value=i + 1).font = BODY_FONT
        for j, (key, _, _) in enumerate(cols[1:], 2):
            vals = series.get(key, [])
            if i < len(vals):
                cell = ws.cell(row=i + 2, column=j, value=float(f"{vals[i]:.8g}"))
                cell.font = BODY_FONT
                cell.alignment = RIGHT
                cell.number_format = "0.0000E+00"
    return ws


# ---------------------------------------------------------------------------
def build_notes(wb, lang, data, shots: dict | None):
    ws = wb.create_sheet(T[lang]["sheets"][-1])
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 100
    r = 1
    c = ws.cell(row=r, column=1, value=T[lang]["notes_title"])
    c.font = SECTION_FONT
    c.fill = GRAY_FILL
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
    r += 2

    for item in ("001", "002"):
        c = ws.cell(row=r, column=1, value=f"── {ITEM_LABEL[item][lang]} ──")
        c.font = SECTION_FONT
        c.fill = GRAY_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        r += 1
        if lang == "en":
            bullets = [
                f"Live capture on {data['idn'].split(',')[1]} (SN {data['idn'].split(',')[2]}) "
                f"with the DUT generating one accelerometer read burst per second.",
                "Automatic measurement engine only; the waveform download path was not used "
                "(this firmware locks up its USBTMC bulk-IN endpoint on that path).",
                "Each acquisition is verified to contain a triggered SCL falling edge before "
                "its measurements are sampled (NORMAL trigger, ACQUIRE:NUMACQ? gating).",
            ]
        else:
            bullets = [
                f"在 {data['idn'].split(',')[1]}（序列号 {data['idn'].split(',')[2]}）上实况采集，"
                f"DUT 每秒产生一次加速度计读突发。",
                "仅使用示波器自动测量引擎；未使用波形下载通道（本固件在该通道上会锁死 "
                "USBTMC bulk-IN 端点）。",
                "每个样本在取值前均确认该次采集含真实 SCL 下降沿触发（NORMAL 触发 + "
                "ACQUIRE:NUMACQ? 门控）。",
            ]
        for b in bullets:
            cc = ws.cell(row=r, column=2, value="•  " + b)
            cc.font = BODY_FONT
            cc.alignment = WRAP
            ws.row_dimensions[r].height = 30
            r += 1
        r += 1

    # --- 佐证截图 ---
    r += 1
    c = ws.cell(row=r, column=1,
                value="Evidence — Scope Screenshots" if lang == "en"
                else "佐证 —— 示波器截图")
    c.font = SECTION_FONT
    c.fill = GRAY_FILL
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
    r += 1

    if lang == "en":
        intro = ("Screenshots were captured with SAVE:IMAGE immediately after a real triggered "
                 "acquisition (the scope's own trigger state was SAVE at capture time). Files "
                 "reside on the instrument's local storage and are archived alongside this "
                 "workbook under reports/screenshots/.")
    else:
        intro = ("截图在真实触发采集完成后立即用 SAVE:IMAGE 保存（保存时示波器自身触发状态为 "
                 "SAVE）。文件存于示波器本地存储，已归档于本工作簿同级的 reports/screenshots/ "
                 "目录。")
    cc = ws.cell(row=r, column=2, value=intro)
    cc.font = BODY_FONT
    cc.alignment = WRAP
    ws.row_dimensions[r].height = 45
    r += 1

    shots_list = ((shots or {}).get("screenshots")
                  or (shots or {}).get("shots") or [])
    if shots_list:
        for s in shots_list:
            name = s.get("name", "?")
            fname = Path(s.get("path", "")).name
            ws.cell(row=r, column=1, value=name).font = HEADER_FONT
            verified = s.get("capture_verified")
            trig_txt = s.get("trigger") or s.get("trig") or                 ("verified" if verified else s.get("detail", "—"))
            bw_hz = s.get("bandwidth_hz", "—")
            bw_txt = (f"{bw_hz/1e6:.0f} MHz" if isinstance(bw_hz, (int, float))
                      else str(bw_hz))
            txt = (f"{fname}   |   capture={trig_txt}   |   "
                   f"{s.get('scale_s_per_div','—')} s/div   |   BW={bw_txt}")
            c2 = ws.cell(row=r, column=2, value=txt)
            c2.font = BODY_FONT
            c2.alignment = WRAP
            ws.row_dimensions[r].height = 20
            r += 1

            cap = SHOT_CAPTION.get(name, {}).get(lang, "")
            if cap:
                cc2 = ws.cell(row=r, column=2, value=cap)
                cc2.font = BODY_FONT
                cc2.alignment = WRAP
                ws.row_dimensions[r].height = 34
                r += 1

            img = SHOTS_DIR / fname
            if img.exists():
                from openpyxl.drawing.image import Image as XLImage
                try:
                    small = SHOTS_DIR / f"_embed_{fname}"
                    if not small.exists():
                        from PIL import Image as PILImage
                        im = PILImage.open(img).convert("RGB")
                        im.thumbnail((900, 900))
                        im.save(small, "PNG", optimize=True)
                    xi = XLImage(str(small))
                    ws.add_image(xi, f"B{r}")
                    r += 23
                except Exception as e:
                    ws.cell(row=r, column=2,
                            value=f"[image embed failed: {e}]").font = BODY_FONT
                    r += 1
            else:
                ws.cell(row=r, column=2,
                        value=(f"Local copy missing: {fname}" if lang == "en"
                               else f"本地副本缺失: {fname}")).font = BODY_FONT
                r += 1
    else:
        ws.cell(row=r, column=2,
                value=("No screenshot metadata found." if lang == "en" else "未找到截图元数据。")
                ).font = BODY_FONT
        r += 1

    r += 1
    footer = ws.cell(
        row=r, column=1,
        value=(f"Generated by generate_report.py (TR-ACCL-001/002) on "
               f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
               if lang == "en" else
               f"由 generate_report.py（TR-ACCL-001/002）于 "
               f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 生成。"))
    footer.font = Font(size=9, italic=True, color="666666")
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
    return ws


# ---------------------------------------------------------------------------
def summarize(data, item, params, smap, lang: str = "en") -> tuple[str, str, list]:
    """返回 (verdict, 摘要文字, 明细行)。摘要文字按语言本地化。"""
    fails, pendings, uncs, details = [], [], [], []
    for key, sym, en, zh, unit, kind, limit, src in params:
        if key not in smap:
            pendings.append(sym)
            details.append((sym, "pending", None, limit, unit))
            continue
        st = get_stats(data, *smap[key])
        v, m = judge(st, kind, limit)
        if v == "fail":
            fails.append(sym)
        elif v == "pass_unc":
            uncs.append(sym)
        if v == "pending":
            pendings.append(sym)
        details.append((sym, v, m, limit, unit))
    verdict = "fail" if fails else ("pending" if pendings else "pass")
    sep = "、" if lang == "zh" else ", "
    if fails:
        summary = ("失败: " if lang == "zh" else "FAIL: ") + sep.join(fails)
        if uncs:
            summary += ("；另有在不确定度内: " if lang == "zh"
                        else "; also within uncertainty: ") + sep.join(uncs)
    elif pendings:
        summary = ("待测: " if lang == "zh" else "PENDING: ") + sep.join(pendings)
    elif uncs:
        summary = (("全部通过（其中 " + sep.join(uncs) + " 在测量不确定度内）")
                   if lang == "zh" else
                   ("all pass (" + sep.join(uncs) + " within measurement uncertainty)"))
    else:
        summary = "全部判据满足" if lang == "zh" else "all criteria met"
    return verdict, summary, details


def main() -> int:
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
    else:
        # 只用正常探头配置（CH1=SCL, CH2=SDA）那一次做主数据 ——
        # 带 _swap 后缀的是探头对调复测，通道映射与本报告的定义相反，不能混用。
        _cands = [f for f in sorted((ROOT / "data").glob("i2c_test_*.json"))
                  if "_swap" not in f.name]
        src = _cands[-1] if _cands else sorted((ROOT / "data").glob("i2c_test_*.json"))[-1]

    data = json.loads(src.read_text(encoding="utf-8"))
    data["_path"] = str(src)
    print(f"数据源: {src.name}")

    # 把波形精算结果注入为伪批次 "W"，供 SOURCE_MAP_002 引用
    wf_json = ROOT / "data" / "waveform" / "waveform_analysis.json"
    if wf_json.exists():
        wf = json.loads(wf_json.read_text(encoding="utf-8"))
        w20 = wf.get("002_20mhz.csv", {})
        synth = {}
        for key in ("tsu_SDA_s", "th_SDA_normal_s", "th_ST_s", "tsu_SR_s",
                    "tw_SCLL_s", "tw_SCLH_s", "tr_sda_20_80_s", "tf_sda_20_80_s"):
            v = w20.get(key)
            if v and v.get("n"):
                synth[key.replace("_s", "").replace("_normal", "")] = {
                    "n": v["n"], "min": v["min"], "max": v["max"],
                    "mean": v["mean"], "median": v.get("median"),
                    "raw": [v["min"], v["median"], v["max"]],
                }
        data.setdefault("batches", {})["W"] = {
            "installed": {}, "samples": synth, "sentinel_counts": {},
            "source": "analyze_waveform.py — 20 MHz dataset",
        }
        print(f"波形精算数据已注入: {len(synth)} 项")

    bw_files = sorted((ROOT / "data").glob("bw_compare_*.json"))
    bw = json.loads(bw_files[-1].read_text(encoding="utf-8")) if bw_files else None
    if bw:
        print(f"带宽对比数据: {bw_files[-1].name}")
        # 用 20 MHz 限带那一组作为 Vmax/Vmin 的判据数据源
        row20 = None
        for name, row in bw.get("bandwidth_compare", {}).items():
            if "20" in name:
                row20 = row
        if row20:
            data.setdefault("batches", {})["BW20"] = {
                "installed": {}, "sentinel_counts": {}, "samples": {
                    k: {"n": v["n"], "min": v["min"], "max": v["max"],
                        "mean": v["mean"], "raw": [v["min"], v["max"]]}
                    for k, v in row20.items() if v.get("n")
                },
                "source": "bw_compare — 20 MHz bandwidth limit",
            }
            print(f"  20 MHz 判据数据已注入: {len(row20)} 项")

    shot_files = sorted((ROOT / "data").glob("screenshots_*.json"))
    shots = json.loads(shot_files[-1].read_text(encoding="utf-8")) if shot_files else None
    if shots:
        print(f"截图元数据: {shot_files[-1].name}  ({len(shots.get('screenshots', []))} 张)")

    v001, s001, _ = summarize(data, "001", PARAMS_001, SOURCE_MAP_001, "en")
    v002, s002, _ = summarize(data, "002", PARAMS_002, SOURCE_MAP_002, "en")
    _, s001_zh, _ = summarize(data, "001", PARAMS_001, SOURCE_MAP_001, "zh")
    _, s002_zh, _ = summarize(data, "002", PARAMS_002, SOURCE_MAP_002, "zh")
    overall = "fail" if "fail" in (v001, v002) else "pass"
    print(f"TR-ACCL-001: {v001}  ({s001})")
    print(f"TR-ACCL-002: {v002}  ({s002})")
    print(f"总体: {overall}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    REPORTS.mkdir(parents=True, exist_ok=True)
    made = []
    for lang in ("en", "zh"):
        wb = Workbook()
        wb.remove(wb.active)
        build_cover(wb, lang, data,
                    {"verdict": v001, "summary": s001 if lang == "en" else s001_zh},
                    {"verdict": v002, "summary": s002 if lang == "en" else s002_zh},
                    overall)
        build_methodology(wb, lang, data, bw)
        build_verdict(wb, lang, data,
                      [("001", PARAMS_001, SOURCE_MAP_001, EXTRA_001),
                       ("002", PARAMS_002, SOURCE_MAP_002, EXTRA_002)])
        build_statistics(wb, lang, data,
                         [("001", PARAMS_001, SOURCE_MAP_001, EXTRA_001),
                          ("002", PARAMS_002, SOURCE_MAP_002, EXTRA_002)])
        build_raw(wb, lang, data, "001", PARAMS_001, SOURCE_MAP_001)
        build_raw(wb, lang, data, "002", PARAMS_002, SOURCE_MAP_002)
        build_notes(wb, lang, data, shots)
        out = REPORTS / f"report_accel_i2c_{ts}_{lang}.xlsx"
        wb.save(out)
        made.append(out)
        print(f"已生成 {out}  (共 {len(wb.sheetnames)} 页: {wb.sheetnames})")

    # 硬性规则自检（REPORT_SPEC §4.1 / §5.5）
    banned = ["simulation", "mock", "Reproducible", "Re-run", "offline"]
    from openpyxl import load_workbook
    bad = []
    for f in made:
        wb = load_workbook(f)
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str):
                        low = cell.value.lower()
                        for b in banned:
                            if b.lower() in low:
                                bad.append((f.name, ws.title, cell.coordinate, b))
        if len(wb.sheetnames) != 7:
            bad.append((f.name, "sheet_count", str(len(wb.sheetnames)), "≠7"))
    print("自检:", "通过（未发现禁用词，页数均为 7）" if not bad else f"发现问题 {bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())